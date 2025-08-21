import os, time, requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone

BOT = os.environ["BOT_TOKEN"]
CHAT = os.environ["CHAT_ID"]
TG_URL = f"https://api.telegram.org/bot{BOT}/sendMessage"

def send(msg, parse_mode=None):
    data = {"chat_id": CHAT, "text": msg}
    if parse_mode: data["parse_mode"] = parse_mode
    r = requests.post(TG_URL, data=data, timeout=15)
    r.raise_for_status()

def tidy_rows(rows, limit=10):
    out = []
    for i, r in enumerate(rows[:limit], start=1):
        name = r.get("name","?")
        amt  = r.get("amount","")
        mk   = r.get("market","")
        out.append(f"{i}. {name} {('(' + mk + ')') if mk else ''} {amt}")
    return "\n".join(out) if out else "데이터 없음"

NAV_HEADERS = {
    "User-Agent":"Mozilla/5.0",
    "Referer":"https://finance.naver.com/"
}

def fetch_from_naver():
    """
    네이버 금융 > 투자자별 매매동향 상위
    investor_gubun: 1000=외국인, 2000=기관
    sosok: 0=KOSPI, 1=KOSDAQ
    마지막 컬럼 '거래금액(백만)' 사용
    """
    base = "https://finance.naver.com/sise/sise_deal_rank.naver"
    results = {"외국인(KOSPI)":[], "외국인(KOSDAQ)":[], "기관(KOSPI)":[], "기관(KOSDAQ)":[]}

    for inv, inv_name in [(1000,"외국인"), (2000,"기관")]:
        for sosok, mk in [(0,"KOSPI"), (1,"KOSDAQ")]:
            url = f"{base}?investor_gubun={inv}&sosok={sosok}"
            resp = requests.get(url, headers=NAV_HEADERS, timeout=15)
            # ★ 핵심: 네이버는 EUC-KR. 인코딩 지정 없으면 표 파싱 실패할 수 있음
            resp.encoding = "euc-kr"
            soup = BeautifulSoup(resp.text, "html.parser")

            table = soup.select_one("table.type_2")
            if not table:
                continue

            rows=[]
            # 헤더 2줄 스킵, 데이터는 tr 안의 td 개수로 판단
            for tr in table.select("tr"):
                tds = tr.find_all("td")
                if len(tds) < 7:
                    continue
                name = tds[1].get_text(strip=True)
                amt  = tds[-1].get_text(strip=True)  # 거래금액(백만)
                if not name or name == "합계":
                    continue
                rows.append({"name": name, "amount": f"{amt}백만", "market": mk})

            results[f"{inv_name}({mk})"] = rows

    return results

def main():
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst)
    today = now.strftime("%Y-%m-%d (%a)")

    # 주말은 바로 안내 후 종료 (원하면 공휴일 테이블도 추가 가능)
    if now.weekday() >= 5:
        send(f"📈 {today}\n오늘은 주말이라 장이 없습니다.")
        return

    # 장마감 직후 지연 대비: 최대 5분간 30초 간격 재시도 (총 10회)
    attempts, data = 10, None
    for i in range(attempts):
        try:
            data = fetch_from_naver()
            # 4개 섹션 중 하나라도 데이터가 있으면 성공으로 판단
            if any(len(v) > 0 for v in data.values()):
                break
        except Exception as e:
            pass
        # 마지막 시도 전까지는 대기 후 재시도
        if i < attempts - 1:
            time.sleep(30)

    header = f"📈 {today} 장마감 수급 요약"
    if data is None or not any(len(v) > 0 for v in data.values()):
        # 완전 실패 시
        send(header + "\n데이터가 아직 준비되지 않았습니다. 잠시 후 다시 시도해 주세요.")
        return

    sections = []
    order = ["외국인(KOSPI)","외국인(KOSDAQ)","기관(KOSPI)","기관(KOSDAQ)"]
    for key in order:
        sections.append(f"🔹 {key} 순매수 TOP 10\n{tidy_rows(data.get(key, []), 10)}")

    send(header + " (집계 지연 대비 자동확인)\n\n" + "\n\n".join(sections))

if __name__ == "__main__":
    main()
