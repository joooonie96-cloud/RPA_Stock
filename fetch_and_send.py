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

def send(msg: str):
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
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}

# 네이버 실제 화면 매핑 (네가 확인한 매핑 기준)
URLS = {
    "기관(KOSPI)":   "https://finance.naver.com/sise/sise_deal_rank.naver?sosok=01&investor_gubun=1000",
    "기관(KOSDAQ)":  "https://finance.naver.com/sise/sise_deal_rank.naver?sosok=02&investor_gubun=1000",
    "외국인(KOSPI)": "https://finance.naver.com/sise/sise_deal_rank.naver?sosok=01&investor_gubun=2000",
    "외국인(KOSDAQ)":"https://finance.naver.com/sise/sise_deal_rank.naver?sosok=02&investor_gubun=2000",
}

def parse_table(table) -> list:
    """type_2 테이블 하나에서 TOP10 행을 파싱하여 반환."""
    rows = []
    for tr in table.select("tr"):
        tds = tr.find_all("td")
        if len(tds) < 7:
            continue
        name = tds[1].get_text(strip=True)
        amt  = tds[-1].get_text(strip=True)  # 거래금액(백만)
        if not name or name == "합계":
            continue
        rows.append(f"{len(rows)+1}. {name} {amt}백만")
        if len(rows) >= 10:
            break
    return rows

def fetch_one(url: str) -> dict:
    """
    반환 예:
    {
      "buy": [...],    # 순매수 표(있으면)
      "sell": [...],   # 순매도 표(있으면)
      "debug": "..."   # 문제시 HTML 앞부분
    }
    """
    out = {"buy": [], "sell": [], "debug": ""}
    resp = requests.get(url, headers=HEADERS, timeout=20)
    if resp.status_code != 200:
        out["debug"] = f"HTTP 오류 (status {resp.status_code})"
        return out

    resp.encoding = "euc-kr"
    soup = BeautifulSoup(resp.text, "html.parser")
    tables = soup.select("table.type_2")

    if not tables:
        out["debug"] = "테이블 없음. 응답 앞부분: " + resp.text[:300].replace("\n", " ")
        return out

    # 표가 여럿일 수 있으므로, 헤더에 '순매수/순매도'가 있는지 확인
    labeled = {"buy": None, "sell": None}
    unknown = []

    for idx, table in enumerate(tables):
        header_text = "".join(th.get_text(strip=True) for th in table.select("th"))
        data = parse_table(table)

        # 데이터 없는 표는 스킵
        if not data:
            continue

        if "순매수" in header_text or "매수" in header_text:
            labeled["buy"] = data
        elif "순매도" in header_text or "매도" in header_text:
            labeled["sell"] = data
        else:
            unknown.append((idx, data))

    # 헤더로 식별되면 그 값 사용
    if labeled["buy"]:
        out["buy"] = labeled["buy"]
    if labeled["sell"]:
        out["sell"] = labeled["sell"]

    # 헤더로 못 찾았으면: 1번째 표=순매수, 2번째 표=순매도로 가정
    if not out["buy"] and unknown:
        out["buy"] = unknown[0][1]
    if not out["sell"] and len(unknown) >= 2:
        out["sell"] = unknown[1][1]

    # 그래도 비어 있으면 디버그
    if not out["buy"] and not out["sell"]:
        out["debug"] = "유효 데이터 없음. 응답 앞부분: " + resp.text[:300].replace("\n", " ")

    return out

def fetch_from_naver() -> dict:
    """
    반환 예:
    {
      "외국인(KOSPI)": ["1. ...", ...],  # 순매수 TOP10 위주로 반환
      "외국인(KOSDAQ)": [...],
      "기관(KOSPI)":   [...],
      "기관(KOSDAQ)":  [...],
      "debug": {...}                # 섹션별 디버그 메시지(있을 때만)
    }
    """
    results = {"debug": {}}
    for key, url in URLS.items():
        try:
            one = fetch_one(url)
            if one["buy"]:
                results[key] = one["buy"]
            else:
                # 순매수 표가 없으면 그대로 이유를 남김
                results[key] = ["데이터 없음"]
                if one["debug"]:
                    results["debug"][key] = one["debug"]
        except Exception as e:
            results[key] = [f"에러: {e}"]
    return results

def main():
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)
    today = now.strftime("%Y-%m-%d (%a)")

    # 주말 스킵 (원하면 휴장일 테이블도 추가 가능)
    if now.weekday() >= 5:
        send(f"📈 {today}\n오늘은 주말이라 장이 없습니다.")
        return

    # 단 1회 시도
    res = fetch_from_naver()

    # 메시지 조립
    parts = []
    order = ["외국인(KOSPI)", "외국인(KOSDAQ)", "기관(KOSPI)", "기관(KOSDAQ)"]
    for key in order:
        body = "\n".join(res.get(key, ["데이터 없음"]))
        parts.append(f"🔹 {key} 순매수 TOP10\n{body}")

    text = f"📈 {today} 장마감 수급 요약 (네이버 증권)\n\n" + "\n\n".join(parts)

    # 섹션별 디버그(있을 때만, 말미에 첨부)
    dbg = res.get("debug", {})
    if dbg:
        text += "\n\n---\n(디버그)\n" + "\n".join([f"{k}: {v}" for k, v in dbg.items()])

    send(text)

if __name__ == "__main__":
    main()
