#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, time, html, re, requests, feedparser
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from readability import Document
from bs4 import BeautifulSoup
from difflib import SequenceMatcher

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
YESTERDAY_STR = YESTERDAY_KST.strftime("%Y-%m-%d")
TODAY_STR = TODAY_KST.strftime("%Y-%m-%d")

# '1일' ~ '31일' (각각 검색; 제목에 없으면 본문 파싱 - 순차)
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

# 제목 유사도 임계
TITLE_SIM_THRESHOLD = 0.90

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
    """
    오늘 탭(YYYY-MM-DD)을 반환. 없으면 생성하고 헤더를 기록.
    있으면 헤더가 비어있을 때만 헤더를 보정.
    """
    import gspread
    try:
        ws = sh.worksheet(date_str)
        # 헤더 확인/보정
        first_row = ws.row_values(1)
        need_header = (len(first_row) < 4) or (first_row[:4] != ["기사 제목", "URL", "게시시각(KST)", "매칭 키워드"])
        if need_header:
            ws.insert_row(["기사 제목", "URL", "게시시각(KST)", "매칭 키워드"], 1)
        created = False
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=date_str, rows=6000, cols=4)
        ws.append_row(["기사 제목", "URL", "게시시각(KST)", "매칭 키워드"])
        created = True
    return ws, created

def load_existing_index(ws):
    """
    오늘 탭에 이미 적재된 URL/제목을 불러와서 set/list로 반환.
    - URL은 중복 방지의 절대 키
    - 제목은 유사도 90% 이상 중복 방지용
    """
    # 전체 값에서 첫 행은 헤더이므로 제외
    all_vals = ws.get_all_values()
    if not all_vals or len(all_vals) == 1:
        return set(), []
    rows = all_vals[1:]  # exclude header
    urls = set()
    titles = []
    for r in rows:
        # 컬럼 순서: [제목, URL, 게시시각, 매칭 키워드]
        if len(r) >= 2:
            url = r[1].strip()
            if url:
                urls.add(url)
        if len(r) >= 1:
            t = r[0].strip()
            if t:
                titles.append(t)
    return urls, titles

def title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.strip(), b.strip()).ratio()

def should_skip_by_title_sim(title: str, existing_titles: list) -> bool:
    if not existing_titles:
        return False
    return max(title_similarity(title, t) for t in existing_titles) >= TITLE_SIM_THRESHOLD

def write_sheet_append(sh, items):
    """
    오늘 탭에 '누적 append'.
    - 같은 URL은 스킵
    - 기존 제목과 유사도 90% 이상이면 스킵
    - 새로 들어간 건수와 오늘 탭 URL을 반환
    """
    ws, _ = get_or_create_daily_ws(sh, TODAY_STR)
    existing_urls, existing_titles = load_existing_index(ws)

    rows_to_append = []
    added_count = 0

    for it in items:
        title = it["title"].strip()
        url   = it["link"].strip()
        if not url or url in existing_urls:
            continue
        if should_skip_by_title_sim(title, existing_titles):
            continue
        rows_to_append.append([
            title,
            url,
            it["published_kst"].strftime("%Y-%m-%d %H:%M"),
            ", ".join(sorted(it["matched_terms"])) if it.get("matched_terms") else ""
        ])
        # 미리 집합/리스트에 반영해 같은 실행 안에서도 중복 방지
        existing_urls.add(url)
        existing_titles.append(title)
        added_count += 1

    if rows_to_append:
        ws.append_rows(rows_to_append, value_input_option="RAW")

    sheet_url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid={ws.id}"
    return sheet_url, added_count

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

def build_done_message(sheet_url: str, added: int, tried: int):
    hdr = "✅ 뉴스 적재가 완료되었습니다."
    rng = f"(범위: 어제 {YESTERDAY_STR} ~ 오늘 {TODAY_STR} · KST)"
    stat = f"이번 실행: 신규 {added}건 / 후보 {tried}건"
    link = f"📊 오늘 탭: {html.escape(sheet_url)}"
    return [f"{hdr}\n{rng}\n{stat}\n\n{link}"]

# ==== 메인 ====
def main():
    # 1) '1일'~'31일' 각각 RSS 호출 → 후보 생성 (matched_terms 포함)
    raw_candidates = []
    for term in DAY_TERMS:
        for e in fetch_entries_for_term(term):
            title = (e.get("title") or "").strip()
            link  = (e.get("link") or "").strip()
            if not title or not link:
                continue
            pub_utc = parse_published(e)
            if not is_within_yesterday_or_today(pub_utc):
                continue
            raw_candidates.append({
                "title": title,
                "link": link,
                "published_kst": pub_utc.astimezone(KST),
                "matched_terms": {term},   # 이 쿼리에서 잡혔다
            })

    # 2) 같은 URL이 여러 키워드로 잡힌 경우: URL 기준으로 합치고 키워드 병합
    by_url = {}
    for it in raw_candidates:
        key = it["link"]
        if key not in by_url:
            by_url[key] = it.copy()
        else:
            by_url[key]["matched_terms"].update(it["matched_terms"])
            if it["published_kst"] > by_url[key]["published_kst"]:
                by_url[key]["published_kst"] = it["published_kst"]
                by_url[key]["title"] = it["title"]

    candidates = list(by_url.values())

    # 3) 날짜 키워드 필터: 제목 통과 + (제목 미통과는 본문 파싱으로 확인; 본문에서 찾은 키워드도 기록)
    matched_items = []
    for it in candidates:
        if DATE_TERM_RE.search(it["title"]):
            found = DATE_TERM_RE.findall(it["title"])
            if found:
                it["matched_terms"].update(found)
            matched_items.append(it)
        else:
            body = extract_article_text(it["link"])
            if body and DATE_TERM_RE.search(body):
                found = DATE_TERM_RE.findall(body)
                if found:
                    it["matched_terms"].update(found)
                matched_items.append(it)

    # 4) 최신순 정렬
    matched_items.sort(key=lambda x: x["published_kst"], reverse=True)

    # 5) 오늘 탭에 누적 append (URL 중복/제목 유사 90% 이상 스킵)
    sh = ensure_gspread()
    sheet_url, added_count = write_sheet_append(sh, matched_items)

    # 6) 텔레그램: 완료 알림(어제/오늘 날짜 명시 + 이번 실행 통계 + 오늘 탭 링크)
    for t in build_done_message(sheet_url, added_count, len(matched_items)):
        send_tg(t)

if __name__ == "__main__":
    main()
