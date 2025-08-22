#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, time, html, re, requests, feedparser
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from readability import Document
from bs4 import BeautifulSoup
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==== 환경변수 ====
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID   = os.getenv("CHAT_ID")
SHEET_ID  = os.getenv("SHEET_ID_NEWS")
GOOGLE_APPLICATION_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

# ==== 시간/상수 ====
KST = timezone(timedelta(hours=9))
NOW_KST = datetime.now(KST)
YESTERDAY_KST = (NOW_KST - timedelta(days=1)).date()
TODAY_KST = NOW_KST.date()

# '1일' ~ '31일' → OR 검색(한 번에 요청)
DAY_TERMS = [f"{d}일" for d in range(1, 32)]
OR_QUERY = " OR ".join(DAY_TERMS)

BASE = "https://news.google.com/rss/search"
COMMON_QS = "hl=ko&gl=KR&ceid=KR:ko"

# 숫자+일 패턴(정확도 높임)
DATE_TERM_RE = re.compile(r"(?<!\d)(?:[1-9]|[12]\d|3[01])일(?!\d)")

# HTTP
REQ_TIMEOUT = 12
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

# 병렬 파싱 제어
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "8"))

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
        ws = sh.add_worksheet(title=date_str, rows=2000, cols=3)
    return ws

def write_sheet_all(sh, items):
    """
    전체 기사(제목/URL/게시시각KST)를 날짜별 탭에 기록.
    - URL 동일 제거
    - 제목 유사도 ≥0.70 제거
    """
    items = dedupe_for_sheet(items, title_similarity_threshold=0.70)

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
def fetch_entries_combined_or():
    """'1일 OR 2일 OR ... 31일' 한 번만 RSS 호출"""
    url = f"{BASE}?q={quote(OR_QUERY)}&{COMMON_QS}"
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
    """링크 기준 중복 제거(수집 단계)"""
    seen = set()
    out = []
    for it in items:
        key = it["link"]
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out

def title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.strip(), b.strip()).ratio()

def dedupe_for_sheet(items, title_similarity_threshold=0.70):
    """시트 적재 전: URL 동일/제목 유사(≥threshold) 제거"""
    seen_urls = set()
    kept = []
    kept_titles = []
    for it in items:
        url = it["link"]
        title = it["title"]
        if url in seen_urls:
            continue
        if kept_titles:
            sim = max(title_similarity(title, t) for t in kept_titles)
            if sim >= title_similarity_threshold:
                continue
        kept.append(it)
        kept_titles.append(title)
        seen_urls.add(url)
    return kept

# ==== 본문 추출 (병렬) ====
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

def extract_bodies_parallel(urls):
    """URL 리스트를 병렬로 파싱 → {url: body}"""
    out = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(extract_article_text, u): u for u in urls}
        for fut in as_completed(futures):
            u = futures[fut]
            try:
                out[u] = fut.result()
            except Exception:
                out[u] = ""
    return out

# ==== “주가 영향” 스코어 ====
MAJOR_COMPANY_EVENTS = [
    r"실적|영업이익|순이익|가이던스|목표가|상향|하향|컨센서스",
    r"인수|합병|M&A|매각|지분 취득|지분 매각|전략적 제휴|JV",
    r"대규모\s*수주|수주 공시|공급 계약|장기 계약|납품",
    r"리콜|품질 문제|사고|화재|공장|라인 중단|파업",
    r"CEO|대표이사|사임|해임|선임|횡령|배임|수사|압수수색",
    r"증자|유상증자|감자|CB|BW|전환사채|배당|자사주|신규 상장|상장폐지|관리종목",
    r"FDA|품목허가|허가 취소|임상\s*(성공|실패)|긴급사용승인|식약처|EMA",
    r"공정위|과징금|제재|담합|조사 착수|검찰",
]
INDUSTRY_WIDE = [
    r"업황|사이클|수요 둔화|수요 회복|가격 인상|가격 인하|감산|증산",
    r"메모리|DRAM|NAND|반도체 장비|리튬|니켈|코발트|원자재",
    r"보조금|규제|완화|의무화|친환경|RE100|탄소|수출입 규제",
]
GLOBAL_MACRO = [
    r"연준|Fed|금리\s*(인상|인하|동결)|FOMC|ECB|BOJ|중국\s*부양|환율|달러|엔화|위안",
    r"유가|WTI|브렌트|OPEC|감산",
    r"전쟁|무력|분쟁|우크라이나|중동|대만|제재|수출통제|관세",
]
# 지도자/정치: 환경변수로 동적으로 확장 가능 (기본에 트럼프/이재명 포함)
LEADER_NAMES = os.getenv("LEADER_NAMES", "트럼프|Trump|이재명|대통령|백악관|청와대")
POLITICAL = [
    rf"{LEADER_NAMES}|정상회담|행정명령|대책|특별법|추경|예산|정책 발표|국무회의|국회",
]

WEIGHTS = {
    "MAJOR_COMPANY_EVENTS": 4,
    "INDUSTRY_WIDE": 2,
    "GLOBAL_MACRO": 3,
    "POLITICAL": 3,
    "DATE_CONTEXT": 1,
    "TIME_CONTEXT": 1,
}
DATE_CONTEXT = r"부터|까지|자|시행|마감|공고|발표|개최|접수|시한|효력|효과"
TIME_CONTEXT = r"오전|오후|\d{1,2}\s*시|\d{1,2}\s*분"

def _score_with_patterns(text: str, patterns, weight: int):
    score = 0
    for pat in patterns:
        if re.search(pat, text, re.IGNORECASE):
            score += weight
    return score

def market_moving_score(title: str, body: str) -> int:
    full = (title or "") + "\n" + (body or "")
    score = 0
    score += _score_with_patterns(full, MAJOR_COMPANY_EVENTS, WEIGHTS["MAJOR_COMPANY_EVENTS"])
    score += _score_with_patterns(full, INDUSTRY_WIDE, WEIGHTS["INDUSTRY_WIDE"])
    score += _score_with_patterns(full, GLOBAL_MACRO, WEIGHTS["GLOBAL_MACRO"])
    score += _score_with_patterns(full, POLITICAL, WEIGHTS["POLITICAL"])
    if re.search(DATE_CONTEXT, full): score += WEIGHTS["DATE_CONTEXT"]
    if re.search(TIME_CONTEXT, full): score += WEIGHTS["TIME_CONTEXT"]
    return score

def filter_market_moving(items, body_cache):
    """
    시장영향 기사만 선별: 제목에서 강신호 없으면 본문(캐시 or 병렬 결과)로 보강.
    임계치 4 이상만 채택.
    """
    high_sig_regex = "|".join([*MAJOR_COMPANY_EVENTS, *GLOBAL_MACRO, *POLITICAL])
    kept = []
    for it in items:
        title = it["title"]
        link  = it["link"]
        needs_body = not re.search(high_sig_regex, title, re.IGNORECASE)
        body = body_cache.get(link, "") if needs_body else ""
        score = market_moving_score(title, body)
        if score >= 4:
            it["mm_score"] = score
            kept.append(it)
    return kept

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

def build_top5_message(items, sheet_url: str, sheet_count: int):
    total = len(items)
    top = items[:5]
    header = f"📈 시장영향 가능성 높은 기사 (어제/오늘)\n선별 {total}건 중 TOP 5 아래 ⬇️\n"

    if not top:
        text = f"{header}\n(해당 기사 없음)\n\n📊 전체 목록({sheet_count}건): {html.escape(sheet_url)}"
        return [text]

    lines = []
    for it in top:
        title = it['title'].strip()
        if len(title) > 150:
            title = title[:147] + "…"
        link = it["link"]
        ts = it["published_kst"].strftime("%Y-%m-%d %H:%M")
        score = it.get("mm_score", 0)
        lines.append(f"• {html.escape(title)}\n{html.escape(link)}  <i>({ts} KST · score {score})</i>")

    body = "\n\n".join(lines)
    footer = f"\n\n📊 전체 목록({sheet_count}건): {html.escape(sheet_url)}"
    text = header + "\n" + body + footer

    if len(text) <= 4096:
        return [text]

    chunks, cur, size = [], [header], len(header)
    for block in lines + [footer]:
        block = "\n\n" + block
        if size + len(block) > 4096:
            chunks.append("".join(cur))
            cur, size = [block], len(block)
        else:
            cur.append(block); size += len(block)
    if cur: chunks.append("".join(cur))
    return chunks

# ==== 메인 ====
def main():
    # 1) OR 검색으로 한 번만 RSS 호출
    entries = fetch_entries_combined_or()

    # 2) 후보 구성 (어제/오늘만)
    candidates = []
    for e in entries:
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
            "published_kst": pub_utc.astimezone(KST),
        })

    # 3) 링크 중복 제거
    candidates = dedupe(candidates)

    # 4) 날짜 패턴 필터: 제목 통과 + (제목 미통과는 본문 병렬 추출로 2차 필터)
    title_pass = [it for it in candidates if DATE_TERM_RE.search(it["title"])]
    need_body  = [it for it in candidates if not DATE_TERM_RE.search(it["title"])]

    bodies_for_date = {}
    if need_body:
        bodies_for_date = extract_bodies_parallel([it["link"] for it in need_body])

    date_filtered = []
    date_filtered.extend(title_pass)
    for it in need_body:
        body = bodies_for_date.get(it["link"], "")
        if body and DATE_TERM_RE.search(body):
            date_filtered.append(it)

    # 5) 최신순 정렬
    date_filtered.sort(key=lambda x: x["published_kst"], reverse=True)

    # 6) 시장영향 기사 선별: 제목 강신호 없는 것만 추가로 **병렬** 본문 추출
    high_sig_regex = "|".join([*MAJOR_COMPANY_EVENTS, *GLOBAL_MACRO, *POLITICAL])
    to_fetch = [it["link"] for it in date_filtered if not re.search(high_sig_regex, it["title"], re.IGNORECASE)]
    body_cache = extract_bodies_parallel(to_fetch) if to_fetch else {}

    market_items = filter_market_moving(date_filtered, body_cache)
    market_items.sort(key=lambda x: (x.get("mm_score", 0), x["published_kst"]), reverse=True)

    # 7) 전체(중복 억제 버전) → 시트 기록
    sh = ensure_gspread()
    sheet_url, sheet_count = write_sheet_all(sh, date_filtered)

    # 8) 텔레그램: 시장영향 TOP 5만
    texts = build_top5_message(market_items, sheet_url, sheet_count)
    for t in texts:
        send_tg(t)

if __name__ == "__main__":
    main()
