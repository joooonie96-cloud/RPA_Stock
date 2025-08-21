# -*- coding: utf-8 -*-
# 네이버 증권 "외국인/기관 × 코스피/코스닥" 순매수 상위 크롤링 → 텔레그램 발송
# 요구사항 반영:
#  - 날짜 검증: div.sise_guide_date == 오늘(YY.MM.DD), 불일치시 오류 메시지 전송
#  - 외국인/기관 × 코스피/코스닥 총 80종목(각 40) 수집 여부 점검
#  - 외국인 TOP25, 기관 TOP25를 '금액(백만)' 기준으로 별도 정렬/발송
# 필요 패키지: requests, beautifulsoup4
# pip install requests beautifulsoup4

import os, requests
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

# 네이버 실제 화면 매핑(사용자 확인 기준)
# key: 화면 라벨, value: (url, investor)  investor는 "외국인" 또는 "기관"
URLS = {
    "기관(KOSPI)"  : ("https://finance.naver.com/sise/sise_deal_rank.naver?sosok=01&investor_gubun=1000", "기관"),
    "기관(KOSDAQ)" : ("https://finance.naver.com/sise/sise_deal_rank.naver?sosok=02&investor_gubun=1000", "기관"),
    "외국인(KOSPI)": ("https://finance.naver.com/sise/sise_deal_rank.naver?sosok=01&investor_gubun=2000", "외국인"),
    "외국인(KOSDAQ)":("https://finance.naver.com/sise/sise_deal_rank.naver?sosok=02&investor_gubun=2000", "외국인"),
}

# ───────────────── 파서 ─────────────────
def parse_page(url: str):
    """
    - 날짜 검증: div.sise_guide_date == 오늘(YY.MM.DD)
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

    # ✅ 날짜 확인 (YY.MM.DD, 예: 25.08.21)
    today = datetime.now(timezone(timedelta(hours=9))).strftime("%y.%m.%d")
    date_elem = soup.select_one("div.sise_guide_date")
    if not date_elem:
        snippet = resp.text[:200].replace("\n", " ")
        raise ValueError(f"날짜 element 없음(div.sise_guide_date). 응답 앞부분: {snippet}")
    page_date = date_elem.get_text(strip=True)
    if page_date != today:
        raise ValueError(f"날짜 불일치 (today={today}, page={page_date})")

    # ✅ 표 파싱
    table = soup.select_one("table.type_2")
    if not table:
        snippet = resp.text[:200].replace("\n", " ")
        raise ValueError(f"테이블 없음(table.type_2). 응답 앞부분: {snippet}")

    data = []
    for tr in table.select("tbody > tr"):
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue
        # 종목명: 첫 번째 td > p > a
        name_tag = tds[0].select_one("p > a")
        # 금액: 세 번째 td
        amt_tag  = tds[2]
        if not name_tag or not amt_tag:
            continue
        name = name_tag.get_text(strip=True)
        amt_str = amt_tag.get_text(strip=True).replace(",", "")
        # 허용: 음수("-123")도 int 변환 가능하게
        if not amt_str or not amt_str.replace("-", "").isdigit():
            continue
        amt = int(amt_str)
        data.append((name, amt))
    return data

# ───────────────── 메인 로직 ─────────────────
def main():
    kst = timezone(timedelta(hours=9))
    today_label = datetime.now(kst).strftime("%y.%m.%d")

    # 네 섹션 수집 + 카테고리 분리
    foreign = []  # 외국인 (KOSPI+KOSDAQ)
    inst = []     # 기관   (KOSPI+KOSDAQ)

    total_count_check = 0
    for label, (url, investor) in URLS.items():
        try:
            rows = parse_page(url)  # [(name, amt)]
        except Exception as e:
            send(f"❌ {label} 처리 실패: {e}")
            return

        total_count_check += len(rows)
        if investor == "외국인":
            foreign.extend(rows)
        else:
            inst.extend(rows)

    # 3) 전체 80종목(외국인40 + 기관40) 검증
    if total_count_check != 80 or len(foreign) != 40 or len(inst) != 40:
        send(f"❌ 오류발생: 종목 수 불일치 (총={total_count_check}, 외국인={len(foreign)}, 기관={len(inst)})")
        return

    # 4) 금액 기준으로 각 카테고리 정렬 후 상위 25
    top25_foreign = sorted(foreign, key=lambda x: x[1], reverse=True)[:25]
    top25_inst    = sorted(inst,    key=lambda x: x[1], reverse=True)[:25]

    # 메시지 조립
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
