# -*- coding: utf-8 -*-
# 네이버 증권 "기관/외국인 순매매 상위 (코스피/코스닥)" 크롤링 → 텔레그램 발송
# 필요 패키지: requests, beautifulsoup4
# pip install requests beautifulsoup4

import os, requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone

BOT = os.getenv("BOT_TOKEN")
CHAT = os.getenv("CHAT_ID")

if not BOT or not CHAT:
    raise RuntimeError("환경변수 BOT_TOKEN/CHAT_ID가 설정되지 않았습니다.")

TG_URL = f"https://api.telegram.org/bot{BOT}/sendMessage"

def send(msg):
    try:
        r = requests.post(TG_URL, data={"chat_id": CHAT, "text": msg}, timeout=20)
        r.raise_for_status()
    except Exception as e:
        print("텔레그램 전송 실패:", e)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/115.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.naver.com/",
}

URLS = {
    "기관(KOSPI)":   "https://finance.naver.com/sise/sise_deal_rank.naver?sosok=01&investor_gubun=1000",
    "기관(KOSDAQ)":  "https://finance.naver.com/sise/sise_deal_rank.naver?sosok=02&investor_gubun=1000",
    "외국인(KOSPI)": "https://finance.naver.com/sise/sise_deal_rank.naver?sosok=01&investor_gubun=2000",
    "외국인(KOSDAQ)":"https://finance.naver.com/sise/sise_deal_rank.naver?sosok=02&investor_gubun=2000",
}

def fetch_from_naver():
    results = {}
    for key, url in URLS.items():
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            if resp.status_code != 200:
                results[key] = [f"HTTP 오류 (status {resp.status_code})"]
                continue

            resp.encoding = "euc-kr"
            soup = BeautifulSoup(resp.text, "html.parser")
            table = soup.select_one("table.type_2")
            if not table:
                results[key] = ["테이블 없음 (구조 변경 가능)"]
                continue

            rows = []
            for tr in table.select("tr"):
                tds = tr.find_all("td")
                if len(tds) < 7:
                    continue
                name = tds[1].get_text(strip=True)
                amt  = tds[-1].get_text(strip=True)
                if not name or name == "합계":
                    continue
                rows.append(f"{len(rows)+1}. {name} {amt}백만")
                if len(rows) >= 10:
                    break

            results[key] = rows if rows else ["데이터 없음"]
        except Exception as e:
            results[key] = [f"에러: {e}"]
    return results

def main():
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)
    today = now.strftime("%Y-%m-%d (%a)")

    if now.weekday() >= 5:
        send(f"📈 {today}\n오늘은 주말이라 장이 없습니다.")
        return

    # 단 1번만 시도
    results = fetch_from_naver()

    parts = []
    order = ["외국인(KOSPI)", "외국인(KOSDAQ)", "기관(KOSPI)", "기관(KOSDAQ)"]
    for key in order:
        body = "\n".join(results.get(key, ["데이터 없음"]))
        parts.append(f"🔹 {key} 순매수 TOP10\n{body}")

    text = f"📈 {today} 장마감 수급 요약 (네이버 증권)\n\n" + "\n\n".join(parts)
    send(text)

if __name__ == "__main__":
    main()
