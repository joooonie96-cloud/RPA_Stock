# -*- coding: utf-8 -*-
import os, requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone

BOT = os.getenv("BOT_TOKEN")
CHAT = os.getenv("CHAT_ID")
TG_URL = f"https://api.telegram.org/bot{BOT}/sendMessage"

def send(msg: str):
    requests.post(TG_URL, data={"chat_id": CHAT, "text": msg}, timeout=20)

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://finance.naver.com/",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8"
}

URLS = {
    "기관(KOSPI)":   "https://finance.naver.com/sise/sise_deal_rank.naver?sosok=01&investor_gubun=1000",
    "기관(KOSDAQ)":  "https://finance.naver.com/sise/sise_deal_rank.naver?sosok=02&investor_gubun=1000",
    "외국인(KOSPI)": "https://finance.naver.com/sise/sise_deal_rank.naver?sosok=01&investor_gubun=2000",
    "외국인(KOSDAQ)":"https://finance.naver.com/sise/sise_deal_rank.naver?sosok=02&investor_gubun=2000",
}

def parse_page(url):
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.encoding = "euc-kr"
    soup = BeautifulSoup(resp.text, "html.parser")

    # ✅ 날짜 확인 (YY.MM.DD 형식, 예: 25.08.21)
    today = datetime.now(timezone(timedelta(hours=9))).strftime("%y.%m.%d")
    date_elems = soup.select("div.subtop_sise_graph2 > div")  # 두 개 div가 있음
    date_texts = [d.get_text(strip=True) for d in date_elems]
    if not any(today in t for t in date_texts):
        raise ValueError(f"날짜 불일치 (today={today}, page={date_texts})")

    # 종목명/금액 추출
    data = []
    for tr in soup.select("table.type_2 tr"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue
        name_tag = tds[0].select_one("p > a")
        amt_tag  = tds[2]
        if not name_tag or not amt_tag:
            continue
        name = name_tag.get_text(strip=True)
        amt_str = amt_tag.get_text(strip=True).replace(",", "")
        if not amt_str.isdigit():
            continue
        amt = int(amt_str)
        data.append((name, amt))
    return data

def main():
    all_data = []
    for key, url in URLS.items():
        try:
            rows = parse_page(url)
            all_data.extend(rows)
        except Exception as e:
            send(f"❌ {key} 처리 실패: {e}")
            return

    # ✅ 종목 수 확인
    if len(all_data) != 80:
        send(f"❌ 오류발생: 종목 수 불일치 (len={len(all_data)})")
        return

    # ✅ 금액 기준 정렬 후 상위 25
    top25 = sorted(all_data, key=lambda x: x[1], reverse=True)[:25]

    today = datetime.now(timezone(timedelta(hours=9))).strftime("%y.%m.%d")
    lines = [f"📈 {today} 장마감 순매수 상위 TOP25", ""]
    for i, (name, amt) in enumerate(top25, 1):
        lines.append(f"{i}. {name} {amt:,}백만")

    send("\n".join(lines))

if __name__ == "__main__":
    main()
