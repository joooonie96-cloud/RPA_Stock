#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, time, html, re, requests, feedparser
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from readability import Document
from bs4 import BeautifulSoup

# ==== 환경변수 ====
BOT_TOKEN = os.getenv("BOT_TOKEN")          # 텔레그램 봇 토큰
CHAT_ID   = os.getenv("CHAT_ID")            # 텔레그램 채팅 ID
SHEET_ID  = os.getenv("SHEET_ID_NEWS")      # 구글 시트 ID
GOOGLE_APPLICATION_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

# ==== 시간/상수 ====
KST = timezone(timedelta(hours=9))
NOW_KST = datetime.now(KST)
YESTERDAY_KST = (NOW_KST - timedelta(days=1)).date()
TODAY_KST = NOW_KST.date()

# '1일' ~ '31일' (각각 검색)
DAY_TERMS = [f"{d}일" for d in range(1, 32)]

BASE = "https://news.google.com/rss/search"
COMMON_QS = "hl=ko&gl=KR&ceid=KR:ko"

# 숫자+일 패턴
DATE_TERM_RE = re.compile(r"(?<!\d)(?:[1-9]|[12]\d|3[01])일(?!\d)")

# HTTP
REQ_TIMEOUT = 12
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

# ==== Google Sheets ====
def ensure_gspread():
    if not GOOGLE_APPLICATION_CREDENTIALS or not SHEET_ID:
        raise RuntimeError("Google Sheets 인증/ID 환경변수가 없습니다.")
    import gspread
    from google.oauth2.service_account import Credentials
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(GOOGLE_APPLICATION_CREDENTIALS, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)
    return sh

def get_or_create_daily_ws(sh, date_str: str):
    import gspread
    try:
        ws = sh.worksheet(date_str)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=date_str, rows=4000, cols=3)
    return ws

def write_sheet_all(sh, items):
    """전체 기사(제목/URL/게시시각KST)를 날짜별 탭에 기록(중복/선별 없음)."""
    date_tab = TODAY_KST.strftime("%Y-%m-%d")
    ws = get_or_create_daily_ws(sh, date_tab)
    ws.clear()
    ws.append_row(["기사 제목", "URL", "게시시각(KST)"])
    if items:
        rows = [[it["title"], it["link"], it["published_kst"].strftime("%Y-%m-%d %H:%M")] for it in items]
        ws.append_rows(rows, value_input_option="RAW")
    sheet_url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid={ws.id}"
    return sheet_url, len(items)

# ==== 수집 ====
def fetch_entries_for_term(term: str):
    url = f"{BASE}?q={quote(term)}&{COMMON_QS}"
    feed = feedparser.parse(url)
    return feed.entries or []

def parse_published(entry):
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        return datetime.fromtimestamp(time.mktime(entry.published_parsed), tz=timezone.utc)
    return datetime.now(timezone.utc)

def is_within_yesterday_or_today(pub_dt_utc: datetime) -> bool:
    kst = pub_dt_utc.astimezone(KST)
    return kst.date() in {YESTERDAY_KST, TODAY_KST}

def extract_article_text(url: str) -> str:
    """제목에 날짜가 없을 때만 호출 (순차 파싱)"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQ_TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
        doc = Document(resp.text or "")
        summary_html = doc.summary()
        soup = BeautifulSoup(summary_html, "lxml")
        text = soup.get_text(separator="\n", strip=True)
        text = re.sub(r"\s+\n", "\n", text)
        text = re.sub(r"\n{2,}", "\n\n", text)
        return text[:15000]
    except Exception:
        return ""

# ==== 텔레그램 ====
def send_tg(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("BOT_TOKEN/CHAT_ID 환경변수가 설정되지 않았습니다.")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    r = requests.post(url, data={
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }, timeout=30)
    r.raise_for_status()

def build_done_message(sheet_url: str, count: int):
    hdr = "✅ 뉴스 적재가 완료되었습니다."
    rng = "(범위: 어제~오늘 KST)"
    link = f"📊 전체 목록: {html.escape(sheet_url)}"
    cnt  = f"총 적재 건수: {count}건"
    return [f"{hdr}\n{rng}\n{cnt}\n\n{link}"]

# ==== 메인 ====
def main():
    # 1) '1일'~'31일' 각각 RSS 호출 → 후보 생성
    candidates = []
    for term in DAY_TERMS:
        for e in fetch_entries_for_term(term):
            title = (e.get("title") or "").strip()
            link  = (e.get("link") or "").strip()
            if not title or not link:
                continue
            pub_utc = parse_published(e)
            if not is_within_yesterday_or_today(pub_utc):
                continue
            candidates.append({
                "title": title,
                "link": link,
                "published_kst": pub_utc.astimezone(KST),
            })

    # 2) 날짜 키워드 필터: 제목 통과 + (제목 미통과는 본문 순차 파싱)
    results = []
    for it in candidates:
        if DATE_TERM_RE.search(it["title"]):
            results.append(it)
        else:
            body = extract_article_text(it["link"])
            if body and DATE_TERM_RE.search(body):
                results.append(it)

    # 3) 최신순 정렬
    results.sort(key=lambda x: x["published_kst"], reverse=True)

    # 4) 스프레드시트 전량 적재
    sh = ensure_gspread()
    sheet_url, cnt = write_sheet_all(sh, results)

    # 5) 텔레그램: 완료 알림 + 시트 링크만
    for t in build_done_message(sheet_url, cnt):
        send_tg(t)

if __name__ == "__main__":
    main()
