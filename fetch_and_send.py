# fetch_and_send.py
import os, sys, time, requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone

BOT = os.environ["BOT_TOKEN"]
CHAT = os.environ["CHAT_ID"]

TG_URL = f"https://api.telegram.org/bot{BOT}/sendMessage"

# --------- 도우미 ----------
def send(msg, parse_mode=None):
    data = {"chat_id": CHAT, "text": msg}
    if parse_mode: data["parse_mode"] = parse_mode
    r = requests.post(TG_URL, data=data, timeout=15)
    r.raise_for_status()

def tidy_rows(rows, limit=10):
    out = []
    for i, r in enumerate(rows[:limit], start=1):
        name = r.get("name") or r.get("종목명") or r.get("item") or "?"
        val  = r.get("amount") or r.get("순매수") or r.get("대금") or r.get("net") or ""
        market = r.get("market","")
        out.append(f"{i}. {name} {('(' + market + ')') if market else ''} {val}")
    return "\n".join(out) if out else "데이터 없음"

# --------- 1차 소스: 네이버 금융 (서버렌더 HTML 파싱) ----------
def fetch_from_naver():
    # 외국인/기관 각각, 코스피/코스닥 별로 긁어오기
    # investor_gubun: 1000=외국인, 2000=기관
    # sosok: 0=코스피, 1=코스닥
    base = "https://finance.naver.com/sise/sise_deal_rank.naver"
    headers={"User-Agent":"Mozilla/5.0"}
    results = {"외국인(KOSPI)":[], "외국인(KOSDAQ)":[], "기관(KOSPI)":[], "기관(KOSDAQ)":[]}
    for inv, inv_name in [(1000,"외국인"), (2000,"기관")]:
        for sosok, mk in [(0,"KOSPI"), (1,"KOSDAQ")]:
            url = f"{base}?investor_gubun={inv}&sosok={sosok}"
            html = requests.get(url, headers=headers, timeout=15).text
            soup = BeautifulSoup(html, "html.parser")
            table = soup.select_one("table.type_2")
            if not table: continue
            rows=[]
            for tr in table.select("tr")[2:]:
                tds = [td.get_text(strip=True) for td in tr.select("td")]
                if len(tds) < 7: continue
                # 형식: 순위, 종목명, 검색비중, 현재가, 전일비, 등락률, 거래대금(백만)
                try:
                    name = tds[1]
                    amt  = tds[-1]
                    if name and amt and name != "합계":
                        rows.append({"name":name, "amount":f"{amt}백만", "market":mk})
                except: pass
            results[f"{inv_name}({mk})"] = rows
    return results

# --------- 2차 소스: 다음 금융 (동일 컨셉, HTML 파싱) ----------
def fetch_from_daum():
    # 페이지가 동적이라 간단 대체: 빈 구조만 리턴(필요시 고도화)
    return {}

# --------- 3차 소스: KRX 메뉴 존재 확인(참고용) ----------
# 자동 파싱은 로그인/파라미터 이슈로 불안정할 수 있어 본 스크립트는 네이버를 1순위로 사용합니다.

def main():
    # 한국 날짜 표시 (장 마감 기준)
    kst = timezone(timedelta(hours=9))
    today = datetime.now(kst).strftime("%Y-%m-%d (%a)")

    data = {}
    try:
        data = fetch_from_naver()
    except Exception as e:
        data = fetch_from_daum()

    # 메시지 구성
    header = f"📈 {today} 장마감 수급 요약 (15:45 기준)\n"
    body_parts = []

    for key in ["외국인(KOSPI)","외국인(KOSDAQ)","기관(KOSPI)","기관(KOSDAQ)"]:
        rows = data.get(key, [])
        body_parts.append(f"🔹 {key} 순매수 TOP 10\n" + tidy_rows(rows, 10))

    msg = header + "\n\n".join(body_parts)
    send(msg)

if __name__ == "__main__":
    main()
