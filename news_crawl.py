#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, time, html, re, requests, feedparser
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from readability import Document
from bs4 import BeautifulSoup
from difflib import SequenceMatcher

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

# ==== Sheets ====
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
    import gspread
    try:
        ws = sh.worksheet(date_str)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=date_str, rows=2000, cols=3)
    return ws

def write_sheet_all(sh, items):
    """
    전체 기사(제목/URL/게시시각KST)를 날짜별 탭에 기록 후 링크 반환.
    여기에서 URL 중복 & 제목 유사도(≥0.70) 중복을 제거한다.
    """
    # 1) URL / 제목 유사도 중복 제거
    items = dedupe_for_sheet(items, title_similarity_threshold=0.70)

    date_tab = TODAY_KST.strftime("%Y-%m-%d")
    ws = get_or_create_daily_ws(sh, date_tab)

    # 초기화 → 헤더
    ws.clear()
    ws.append_row(["기사 제목", "URL", "게시시각(KST)"])

    if items:
        rows = [[it["title"], it["link"], it["published_kst"].strftime("%Y-%m-%d %H:%M")] for it in items]
        ws.append_rows(rows, value_input_option="RAW")

    sheet_url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid={ws.id}"
    return sheet_url, len(items)

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
    """
    시트 적재 전 중복 제거:
      - URL 완전 동일: 제외
      - 제목 유사도 ≥ threshold: 제외 (이미 선택된 것과 비교)
    """
    seen_urls = set()
    kept = []
    kept_titles = []

    for it in items:
        url = it["link"]
        title = it["title"]
        if url in seen_urls:
            continue
        # 제목 유사도 비교 (이미 채택된 것들과만)
        if kept_titles:
            sim = max(title_similarity(title, t) for t in kept_titles)
            if sim >= title_similarity_threshold:
                continue
        kept.append(it)
        kept_titles.append(title)
        seen_urls.add(url)
    return kept

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

# ==== “주가에 영향 줄 만한” 기사 선별 ====
# 간단한 규칙 기반 스코어러 (제목/본문 키워드 매칭)
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

POLITICAL = [
    r"대통령|이재명|트럼프|정상회담|행정명령|대책|특별법|추경|예산|정책 발표",
]

# 가중치
WEIGHTS = {
    "MAJOR_COMPANY_EVENTS": 4,
    "INDUSTRY_WIDE": 2,
    "GLOBAL_MACRO": 3,
    "POLITICAL": 3,
    # 보조 신호
    "DATE_CONTEXT": 1,   # '일부터/까지/자/시행/마감/공고/발표/개최/접수' 등
    "TIME_CONTEXT": 1,   # '오전|오후|시|분' 등
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
    """
    기사 제목/본문 기반 스코어. 높을수록 '주가에 영향' 가능성이 큼.
    """
    t = title or ""
    b = body or ""
    full = (t + "\n" + b)

    score = 0
    score += _score_with_patterns(full, MAJOR_COMPANY_EVENTS, WEIGHTS["MAJOR_COMPANY_EVENTS"])
    score += _score_with_patterns(full, INDUSTRY_WIDE, WEIGHTS["INDUSTRY_WIDE"])
    score += _score_with_patterns(full, GLOBAL_MACRO, WEIGHTS["GLOBAL_MACRO"])
    score += _score_with_patterns(full, POLITICAL, WEIGHTS["POLITICAL"])

    # 보조 신호
    if re.search(DATE_CONTEXT, full):
        score += WEIGHTS["DATE_CONTEXT"]
    if re.search(TIME_CONTEXT, full):
        score += WEIGHTS["TIME_CONTEXT"]

    return score

def filter_market_moving(items):
    """
    시장영향 기사만 선별: 제목/본문으로 스코어링해 임계치 이상만 채택.
    임계치는 경험치로 4 이상부터 통과(회사 이벤트 1개만 있어도 통과 가능).
    """
    kept = []
    for it in items:
        body = ""
        # 제목에 강한 신호가 없으면 본문 추출해서 재평가(비용 절약용)
        if not re.search("|".join([*MAJOR_COMPANY_EVENTS, *GLOBAL_MACRO, *POLITICAL]), it["title"], re.IGNORECASE):
            body = extract_article_text(it["link"])
        score = market_moving_score(it["title"], body)
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
    """
    시장영향 기사 중 TOP 5만 전송. 나머지는 시트 링크 안내.
    """
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

    # 4096자 분할
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

    # 2) 초기 중복 제거
    candidates = dedupe(candidates)

    # 3) 날짜 패턴(제목/본문) 필터
    date_filtered = []
    for it in candidates:
        if DATE_TERM_RE.search(it["title"]):
            date_filtered.append(it)
        else:
            body = extract_article_text(it["link"])
            if body and DATE_TERM_RE.search(body):
                date_filtered.append(it)

    # 4) 최신순 정렬
    date_filtered.sort(key=lambda x: x["published_kst"], reverse=True)

    # 5) 시장영향 선별 + 점수 부여, 점수 DESC → 최신순 tie-break
    market_items = filter_market_moving(date_filtered)
    market_items.sort(key=lambda x: (x.get("mm_score", 0), x["published_kst"]), reverse=True)

    # 6) 전체(중복제거 버전) → 구글 시트 적재 (URL 동일 · 제목 유사도 ≥0.70 제거)
    sh = ensure_gspread()
    sheet_url, sheet_count = write_sheet_all(sh, date_filtered)

    # 7) 텔레그램: 시장영향 TOP 5만 발송
    texts = build_top5_message(market_items, sheet_url, sheet_count)
    for t in texts:
        send_tg(t)

if __name__ == "__main__":
    main()
