# -*- coding: utf-8 -*-
# 네이버 증권 4개 URL 병렬 크롤링 → 날짜검증(YY.MM.DD) → 외국인/기관 각각 TOP25 텔레그램 발송
# 필요: httpx, beautifulsoup4

import os, re, asyncio, httpx
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone

# ───── 텔레그램 ─────
BOT = os.getenv("BOT_TOKEN")
CHAT = os.getenv("CHAT_ID")
if not BOT or not CHAT:
    raise RuntimeError("환경변수 BOT_TOKEN/CHAT_ID 가 필요합니다.")
TG_URL = f"https://api.telegram.org/bot{BOT}/sendMessage"

async def send_tg(client: httpx.AsyncClient, text: str):
    await client.post(TG_URL, data={"chat_id": CHAT, "text": text}, timeout=20)

# ───── 네이버 설정 ─────
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
DATE_RX = re.compile(r"\b\d{2}\.\d{2}\.\d{2}\b")  # YY.MM.DD

# key: 라벨, val: (url, investor)
URLS = {
    "기관(KOSPI)"  : ("https://finance.naver.com/sise/sise_deal_rank.naver?sosok=01&investor_gubun=1000", "기관"),
    "기관(KOSDAQ)" : ("https://finance.naver.com/sise/sise_deal_rank.naver?sosok=02&investor_gubun=1000", "기관"),
    "외국인(KOSPI)": ("https://finance.naver.com/sise/sise_deal_rank.naver?sosok=01&investor_gubun=2000", "외국인"),
    "외국인(KOSDAQ)":("https://finance.naver.com/sise/sise_deal_rank.naver?sosok=02&investor_gubun=2000", "외국인"),
}

def find_date(soup: BeautifulSoup, raw: str) -> str | None:
    # 1) 가장 안정적인 짧은 셀렉터
    node = soup.select_one("div.sise_guide_date")
    if node:
        txt = node.get_text(strip=True)
        if DATE_RX.search(txt): return txt
    # 2) fallback: 페이지 전체에서 YY.MM.DD
    m = DATE_RX.search(soup.get_text(" ", strip=True))
    return m.group(0) if m else None

def parse_rows(html: str, today_fmt: str) -> list[tuple[str, int]]:
    # EUC-KR 지정
    soup = BeautifulSoup(html, "html.parser")

    # 날짜 검증
    page_date = find_date(soup, html)
    if not page_date:
        raise ValueError("날짜 탐색 실패(div.sise_guide_date 없음)")
    if page_date != today_fmt:
        raise ValueError(f"날짜 불일치 (today={today_fmt}, page={page_date})")

    # 표 파싱 (가장 일반적인 첫 번째 type_2 테이블)
    table = soup.select_one("table.type_2")
    if not table:
        raise ValueError("table.type_2 없음")

    out = []
    # td 인덱스로 안전하게 접근: 0=종목명 영역, 2=금액 칼럼(백만 단위)
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
        out.append((name, amt))
    return out

async def fetch_one(client: httpx.AsyncClient, url: str) -> str:
    r = await client.get(url, headers=HEADERS, timeout=20)
    if r.status_code != 200:
        raise ValueError(f"HTTP {r.status_code}")
    # 강제 EUC-KR 디코딩
    r.encoding = "euc-kr"
    return r.text

async def main():
    kst = timezone(timedelta(hours=9))
    today_label = datetime.now(kst).strftime("%y.%m.%d")

    async with httpx.AsyncClient(http2=True) as client:
        # 4개 URL 동시 요청
        tasks = {label: fetch_one(client, u) for label, (u, _) in URLS.items()}
        html_map = {}
        try:
            htmls = await asyncio.gather(*tasks.values(), return_exceptions=True)
            for (label, _), html in zip(URLS.items(), htmls):
                if isinstance(html, Exception):
                    raise html
                html_map[label] = html
        except Exception as e:
            await send_tg(client, f"❌ 요청 실패: {e}")
            return

        # 파싱 + 날짜검증 + 카테고리 모으기
        foreign, inst = [], []
        total = 0
        for label, (url, who) in URLS.items():
            raw = html_map.get(label, "")
            try:
                rows = parse_rows(raw, today_label)
            except Exception as e:
                snippet = (raw[:180].replace("\n", " ") if raw else "응답 없음")
                await send_tg(client, f"❌ {label} 파싱 실패: {e}\n… {snippet}")
                return

            total += len(rows)
            if who == "외국인":
                foreign.extend(rows)
            else:
                inst.extend(rows)

        # 80개(외40+기40) 확인
        if total != 80 or len(foreign) != 40 or len(inst) != 40:
            await send_tg(client, f"❌ 종목 수 불일치: 총={total}, 외국인={len(foreign)}, 기관={len(inst)} (기대: 80/40/40)")
            return

        # 금액 기준 상위 25씩
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

        await send_tg(client, "\n".join(lines))

if __name__ == "__main__":
    asyncio.run(main())
