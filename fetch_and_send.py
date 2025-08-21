# -*- coding: utf-8 -*-
# 네이버 증권 "외국인/기관 × 코스피/코스닥" 순매수 상위 크롤링 → 텔레그램 발송 (날짜 탐색 강화)
# pip install requests beautifulsoup4

import os, re, requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone

# ───────────────── 텔레그램 설정 ─────────────────
BOT = os.getenv("BOT_TOKEN")
CHAT = os.getenv("CHAT_ID")
if not BOT or not CHAT:
    raise RuntimeError("환경변수 BOT_TOKEN / CHAT_ID 가 설정되어 있지 않습니다.")
TG_URL = f"https://api.telegram.org/bot{BOT}/sendMessage"

def send(msg: str):
    try:
        r = requests.post(TG_URL, data={"chat_id": CHAT, "text": msg}, timeout=20)
        r.raise_for_status()
    except Exception as e:
        print("텔레그램 전송 실패:", e)

# ───────────────── HTTP 헤더 / 대상 URL ─────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/115.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.naver.com/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}

# key: 화면 라벨, value: (url, investor)
URLS = {
    "기관(KOSPI)"  : ("https://finance.naver.com/sise/sise_deal_rank.naver?sosok=01&investor_gubun=1000", "기관"),
    "기관(KOSDAQ)" : ("https://finance.naver.com/sise/sise_deal_rank.naver?sosok=02&investor_gubun=1000", "기관"),
    "외국인(KOSPI)": ("https://finance.naver.com/sise/sise_deal_rank.naver?sosok=01&investor_gubun=2000", "외국인"),
    "외국인(KOSDAQ)":("https://finance.naver.com/sise/sise_deal_rank.naver?sosok=02&investor_gubun=2000", "외국인"),
}

# ───────────────── 유틸 ─────────────────
DATE_REGEX = re.compile(r"\b\d{2}\.\d{2}\.\d{2}\b")  # YY.MM.DD

def find_page_date(soup: BeautifulSoup, raw_html: str) -> str | None:
    """
    가능한 날짜 위치를 순차 탐색:
      1) div.sise_guide_date
      2) 상단 안내/가이드 영역 추정 선택자들
      3) 페이지 전체 텍스트 정규식 탐색 (YY.MM.DD)
    """
    # 1) 가장 확실한 기존 셀렉터
    cand = soup.select_one("div.sise_guide_date")
    if cand:
        txt = cand.get_text(strip=True)
        if DATE_REGEX.search(txt):
            return txt

    # 2) 다른 상단 후보 셀렉터 시도 (페이지 변형 대응)
    candidates = [
        "div.guide_info", "div.guide", "div#content > div h3", "div#content > h3",
        "div.section_sise_top", "div.subtop_sise_graph2", "div.wrap_cont > div"
    ]
    for sel in candidates:
        el = soup.select_one(sel)
        if not el:
            continue
        txt = el.get_text(" ", strip=True)
        m = DATE_REGEX.search(txt)
        if m:
            return m.group(0)

    # 3) 전체 텍스트에서 최종 탐색
    m = DATE_REGEX.search(soup.get_text(" ", strip=True))
    if m:
        return m.group(0)

    # 그래도 실패 → raw_html 앞부분을 디버그로 확인하기 좋게 반환 None
    return None

# ───────────────── 파서 ─────────────────
def parse_page(url: str):
    """
    - 날짜 검증: find_page_date()로 찾은 값 == 오늘(YY.MM.DD)
    - 표 파싱: table.type_2 의 tbody > tr 에서
        종목명: 첫 번째 td 내부의 p > a
        금액  : 세 번째 td (백만 단위, 콤마 제거 후 int, 음수 허용)
    - 반환: [(name:str, amount:int)]
    """
    resp = requests.get(url, headers=HEADERS, timeout=20)
    if resp.status_code != 200:
        raise ValueError(f"HTTP 상태코드 {resp.status_code}")
    resp.encoding = "euc-kr"
    soup = BeautifulSoup(resp.text, "html.parser")

    # ✅ 날짜 확인 (YY.MM.DD)
    today = datetime.now(timezone(timedelta(hours=9))).strftime("%y.%m.%d")
    page_date = find_page_date(soup, resp.text)
    if not page_date:
        snippet = resp.text[:200].replace("\n", " ")
        raise ValueError(f"날짜 탐색 실패. 응답 앞부분: {snippet}")
    if page_date != today:
        raise ValueError(f"날짜 불일치 (today={today}, page={page_date})")

    # ✅ 표 파싱 (type_2 테이블 중 첫 번째 표 기준)
    table = soup.select_one("table.type_2")
    if not table:
        snippet = resp.text[:200].replace("\n", " ")
        raise ValueError(f"테이블 없음(table.type_2). 응답 앞부분: {snippet}")

    data = []
    for tr in table.select("tbody > tr"):
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue
        name_tag = tds[0].select_one("p > a")
        amt_tag  = tds[2]
        if not name_tag or not amt_tag:
            continue
        name = name_tag.get_text(strip=True)
        amt_str = amt_tag.get_text(strip=True).replace(",", "")
        if not amt_str or not amt_str.replace("-", "").isdigit():
            continue
        amt = int(amt_str)
        data.append((name, amt))
    return data

# ───────────────── 메인 ─────────────────
def main():
    kst = timezone(timedelta(hours=9))
    today_label = datetime.now(kst).strftime("%y.%m.%d")

    foreign = []  # 외국인 합산
    inst = []     # 기관 합산
    total = 0

    for label, (url, investor) in URLS.items():
        try:
            rows = parse_page(url)
        except Exception as e:
            send(f"❌ {label} 처리 실패: {e}")
            return
        total += len(rows)
        if investor == "외국인":
            foreign.extend(rows)
        else:
            inst.extend(rows)

    # 80종목(외40+기40) 검증
    if total != 80 or len(foreign) != 40 or len(inst) != 40:
        send(f"❌ 오류발생: 종목 수 불일치 (총={total}, 외국인={len(foreign)}, 기관={len(inst)})")
        return

    # 투자자별 상위 25
    top25_foreign = sorted(foreign, key=lambda x: x[1], reverse=True)[:25]
    top25_inst    = sorted(inst,    key=lambda x: x[1], reverse=True)[:25]

    lines = [f"📈 {today_label} 장마감 순매수 상위 (네이버 증권)"]
    lines.append("")
    lines.append("🔹 외국인 TOP25")
    for i, (name, amt) in enumerate(top25_foreign, 1):
        lines.append(f"{i}. {name} {amt:,}백만")
    lines.append("")
    lines.append("🔹 기관 TOP25")
    for i, (name, amt) in enumerate(top25_inst, 1):
        lines.append(f"{i}. {name} {amt:,}백만")

    send("\n".join(lines))

if __name__ == "__main__":
    main()
