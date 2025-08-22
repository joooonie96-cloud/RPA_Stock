#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, time, html, re, requests, feedparser
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from readability import Document
from bs4 import BeautifulSoup

# === 환경변수 (필수) ===
BOT_TOKEN = os.getenv("BOT_TOKEN")   # 텔레그램 봇 토큰
CHAT_ID   = os.getenv("CHAT_ID")     # 수신 채팅 ID(개인/그룹)

# === 상수 ===
KST = timezone(timedelta(hours=9))
NOW_KST = datetime.now(KST)
YESTERDAY_KST = (NOW_KST - timedelta(days=1)).date()
TODAY_KST = NOW_KST.date()

# '1일' ~ '31일' (검색어)
DAY_TERMS = [f"{d}일" for d in range(1, 32)]

# Google News RSS (한국어/한국)
BASE = "https://news.google.com/rss/search"
COMMON_QS = "hl=ko&gl=KR&ceid=KR:ko"

# 숫자+일 패턴(한국어 단어 경계 보완)
DATE_TERM_RE = re.compile(r"(?<!\d)(?:[1-9]|[12]\d|3[01])일(?!\d)")

# HTTP 공통
REQ_TIMEOUT = 15
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

def fetch_entries_for_term(term: str):
    """특정 term(예: '3일')으로 Google News RSS 검색 후 entry 리스트 반환"""
    url = f"{BASE}?q={quote(term)}&{COMMON_QS}"
    feed = feedparser.parse(url)
    return feed.entries or []

def is_within_yesterday_or_today(pub_dt_utc: datetime) -> bool:
    """UTC -> KST로 변환하여 어제/오늘 기사 여부 판단"""
    kst = pub_dt_utc.astimezone(KST)
    return kst.date() in {YESTERDAY_KST, TODAY_KST}

def parse_published(entry):
    """feedparser의 published_parsed를 datetime(UTC)로"""
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        return datetime.fromtimestamp(time.mktime(entry.published_parsed), tz=timezone.utc)
    # 없을 경우, 지금 시각으로 보정(드물게 발생)
    return datetime.now(timezone.utc)

def dedupe(items):
    """링크 기준 중복 제거"""
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
    """
    뉴스 페이지 HTML을 받아 main content 텍스트 추출.
    - readability로 본문 추출 → bs4로 텍스트화
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQ_TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
        # 일부 포털(특히 구글뉴스 중간 리다이렉트)이 HTML이 아닌 경우가 있어 가드
        html_text = resp.text or ""
        doc = Document(html_text)
        summary_html = doc.summary()
        soup = BeautifulSoup(summary_html, "lxml")
        text = soup.get_text(separator="\n", strip=True)
        # 과도한 공백 정리
        text = re.sub(r"\s+\n", "\n", text)
        text = re.sub(r"\n{2,}", "\n\n", text)
        return text[:15000]  # 안전상 최대 길이 제한
    except Exception:
        return ""

def build_message(items):
    """텔레그램 메시지(4096자 제한 고려, 길면 분할)"""
    if not items:
        return ["어제/오늘 기준으로 '1일'~'31일' 키워드가 포함된 기사가 없습니다."]

    lines = []
    for i, it in enumerate(items, 1):
        title = html.escape(it["title"])
        link  = html.escape(it["link"])
        kst_ts = it["published_kst"].strftime("%Y-%m-%d %H:%M")
        lines.append(f"• {title}\n{link}  <i>({kst_ts} KST)</i>")

    text = "\n\n".join(lines)
    max_len = 4096
    if len(text) <= max_len:
        return [text]

    # 너무 길면 적절히 분할
    chunks, chunk, size = [], [], 0
    for block in lines:
        block += "\n\n"
        if size + len(block) > max_len:
            chunks.append("".join(chunk))
            chunk, size = [block], len(block)
        else:
            chunk.append(block)
            size += len(block)
    if chunk:
        chunks.append("".join(chunk))
    return chunks

def send_telegram(text: str):
    """텔레그램 메시지 전송"""
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("BOT_TOKEN/CHAT_ID 환경변수가 설정되지 않았습니다.")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }, timeout=30)
    resp.raise_for_status()

def main():
    # 1) 각 키워드로 RSS 수집 (제목 기준 1차 필터)
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

    # 2) 링크 기준 중복 제거
    candidates = dedupe(candidates)

    # 3) 제목/본문에 날짜 패턴이 하나라도 있는지 최종 필터
    results = []
    for it in candidates:
        title = it["title"]
        if DATE_TERM_RE.search(title):
            results.append(it)
            continue
        # 제목에 없으면 본문을 내려받아 검사
        article_text = extract_article_text(it["link"])
        if article_text and DATE_TERM_RE.search(article_text):
            results.append(it)

    # 최신순 정렬
    results.sort(key=lambda x: x["published_kst"], reverse=True)

    # 4) 메시지 빌드 & 발송
    texts = build_message(results)
    header = f"📰 어제/오늘 '1일~31일' 키워드(제목/본문) 포함 기사 ({NOW_KST.strftime('%Y-%m-%d %H:%M')} 기준)"
    if texts:
        first = f"{header}\n\n{texts[0]}"
        send_telegram(first)
        for t in texts[1:]:
            send_telegram(t)

if __name__ == "__main__":
    main()
