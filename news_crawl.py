#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, time, html, re, requests, feedparser
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from readability import Document
from bs4 import BeautifulSoup

# ==== 환경변수 ====
BOT_TOKEN = os.getenv("BOT_TOKEN")         # 텔레그램 봇 토큰
CHAT_ID   = os.getenv("CHAT_ID")           # 텔레그램 채팅 ID
SHEET_ID  = os.getenv("SHEET_ID_NEWS")     # 구글 스프레드시트 ID
GOOGLE_APPLICATION_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

# ==== 시간/상수 ====
KST = timezone(timedelta(hours=9))
NOW_KST = datetime.now(KST)
YESTERDAY_KST = (NOW_KST - timedelta(days=1)).date()
TODAY_KST = NOW_KST.date()

DAY_TERMS = [f"{d}일" for d in range(1, 32)]  # '1일' ~ '31일'

BASE = "https://news.google.com/rss/search"
COMMON_QS = "hl=ko&gl=KR&ceid=KR:ko"

# 숫자+일 패턴(정확도 높임: 숫자 1~31 + '일' 단어 경계)
DATE_TERM_RE = re.compile(r"(?<!\d)(?:[1-9]|[12]\d|3[01])일(?!\d)")

REQ_TIMEOUT = 15
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
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_file(GOOGLE_APPLICATION_CREDENTIALS, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)
    return sh

def get_or_create_daily_ws(sh, date_str: str):
    """날짜 탭(YYYY-MM-DD) 없으면 생성, 있으면 반환"""
    try:
        ws = sh.worksheet(date_str)
    except Exception:
        ws = sh.add_worksheet(title=date_str, rows=1000, cols=3)
    return ws

def write_sheet_all(sh, items):
    """전체 기사(제목/URL/게시시각KST)를 날짜별 탭에 기록 후 링크 반환"""
    date_tab = TODAY_KST.strftime("%Y-%m-%d")
    ws = get_or_create_daily_ws(sh, date_tab)
    # 초기화 후 헤더 + 데이터
    ws.clear()
    ws.append_row(["기사 제목", "URL", "게시시각(KST)"])
    rows = []
    for it in items:
        rows.append([it["title"], it["link"], it["published_kst"].strftime("%Y-%m-%d %H:%M")])
    if rows:
        # batch append: 성능 좋고 안정적
        ws.append_rows(rows, value_input_option="RAW")
    # 시트 링크(gid 포함)
    sheet_url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid={ws.id}"
    return sheet_url

# ==== 수집/필터 ====
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

def dedupe(items):
    seen = set()
    out = []
    for it in items:
        key = it["link"]
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out

def extract_article_text(url: str) -> str:
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

def build_top10_message(items, sheet_url: str):
    total = len(items)
    top = items[:10]
    if not top:
        header = f"📰 어제/오늘 '1일~31일' 키워드(제목/본문) 포함 기사: 0건\n"
        header += f"📊 전체 목록: {html.escape(sheet_url)}"
        return [header]

    # 1행 요약 + TOP10
    header = f"📰 '1일~31일' 포함 기사 요약 (어제/오늘 기준)\n총 {total}건 · TOP 10 아래 ⬇️\n"
    lines = []
    for it in top:
        title = it['title'].strip()
        # 텔레그램 길이 위험 대비: 제목 너무 길면 컷
        if len(title) > 150:
            title = title[:147] + "…"
        title_esc = html.escape(title)
        link_esc  = html.escape(it["link"])
        ts = it["published_kst"].strftime("%Y-%m-%d %H:%M")
        lines.append(f"• {title_esc}\n{link_esc}  <i>({ts} KST)</i>")

    body = "\n\n".join(lines)
    footer = f"\n\n📊 전체 목록(전체 {total}건): {html.escape(sheet_url)}"
    text = header + "\n" + body + footer

    # 텔레그램 4096자 제한 고려해 분할
    max_len = 4096
    if len(text) <= max_len:
        return [text]

    # 길면 헤더 + 항목 분할
    chunks, cur, size = [], [header], len(header)
    for block in lines + [footer]:
        block = ("\n\n" + block)
        if size + len(block) > max_len:
            chunks.append("".join(cur))
            cur, size = [block], len(block)
        else:
            cur.append(block)
            size += len(block)
    if cur:
        chunks.append("".join(cur))
    return chunks

# ==== 메인 ====
def main():
    # 1) 수집
    candidates = []
    for term in DAY_TERMS:
        for e in fetch_entries_for_term(term):
            title = e.get("title", "").strip()
            link  = e.get("link", "").strip()
            if not title or not link:
                continue
            pub_utc = parse_published(e)
            if not is_within_yesterday_or_today(pub_utc):
                continue
            candidates.append({
                "title": title,
                "link": link,
                "published_kst": pub_utc.astimezone(KST)
            })

    # 2) 중복 제거
    candidates = dedupe(candidates)

    # 3) 제목/본문에 날짜 패턴 최종 검사
    results = []
    for it in candidates:
        if DATE_TERM_RE.search(it["title"]):
            results.append(it)
            continue
        text = extract_article_text(it["link"])
        if text and DATE_TERM_RE.search(text):
            results.append(it)

    # 4) 최신순 정렬
    results.sort(key=lambda x: x["published_kst"], reverse=True)

    # 5) 전체 → 구글 시트 적재
    sh = ensure_gspread()
    sheet_url = write_sheet_all(sh, results)

    # 6) TOP10만 텔레그램
    texts = build_top10_message(results, sheet_url)
    for t in texts:
        send_tg(t)

if __name__ == "__main__":
    main()
