import os
import json
import re
import time
import requests
from typing import List, Dict, Any, Optional

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID   = os.environ.get("CHAT_ID")

# ────────────────────────────────────────────────────────────────────
# 대상: 네이버 '투자자별 매매 상위' (모바일 라우트들, 비공식)
# KOSPI/KOSDAQ × (기관/외국인) 4조합
# ────────────────────────────────────────────────────────────────────
TARGETS = [
    ("기관(KOSPI)",  "KOSPI",  "기관"),
    ("기관(KOSDAQ)", "KOSDAQ", "기관"),
    ("외국인(KOSPI)","KOSPI",  "외국인"),
    ("외국인(KOSDAQ)","KOSDAQ","외국인"),
]

# 모바일 사이트 흉내 (해외 IP에서도 통할 확률 ↑)
COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/16.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.6,en;q=0.4",
    "Referer": "https://m.stock.naver.com/",
    "Connection": "keep-alive",
}

SESSION = requests.Session()
SESSION.headers.update(COMMON_HEADERS)
TIMEOUT = 15

# ────────────────────────────────────────────────────────────────────
# 1) 시도할 후보 엔드포인트들 (네이버 모바일 내부 라우트 추정/경험 기반)
#    - 일부는 REST, 일부는 BFF/Apollo 캐시 형태
#    - 응답 구조가 다를 수 있으므로, 아래에서 자동 추론 파서 사용
# ────────────────────────────────────────────────────────────────────
def candidate_urls(market: str, who: str) -> List[str]:
    # 투자자 타입 문자열 후보
    inv_words = []
    if who == "기관":
        inv_words = ["INSTITUTION", "INSTITUTIONAL", "기관", "1000"]
    else:
        inv_words = ["FOREIGN", "FOREIGNER", "외국인", "2000"]

    # 마켓 문자열 후보
    m_words = [market, market.lower(), market.capitalize()]
    if market.upper() == "KOSPI":
        m_words += ["STOCK", "KOSPI"]
    else:
        m_words += ["KOSDAQ"]

    # URL 후보들 (과거/현재 혼용)
    urls = []

    # v1: 직관적 REST 스타일
    for inv in inv_words:
        for mk in m_words:
            urls.append(
                f"https://m.stock.naver.com/api/sise/investorDealRank?market={mk}&period=DAY&investorType={inv}"
            )
            urls.append(
                f"https://m.stock.naver.com/api/sise/investor/deal-rank?market={mk}&period=DAY&investorType={inv}"
            )
            urls.append(
                f"https://m.stock.naver.com/api/sise/investorDealRank?market={mk}&periodType=day&investorType={inv}"
            )

    # v2: 페이지 경로 + 내장 JSON 추출 (SSR/Apollo 상태에서 긁기)
    for inv in inv_words:
        for mk in m_words:
            urls.append(
                f"https://m.stock.naver.com/sise/investor/deal-rank?market={mk}&investorType={inv}"
            )

    # 중복 제거, 순서 유지
    uniq = []
    seen = set()
    for u in urls:
        if u not in seen:
            uniq.append(u); seen.add(u)
    return uniq


# ────────────────────────────────────────────────────────────────────
# JSON에서 TOP25 추출 (키 자동 추론)
# ────────────────────────────────────────────────────────────────────
def parse_top25_from_json(payload: Any) -> Optional[List[Dict[str, Any]]]:
    """
    다양한 JSON 구조를 받아서 TOP25 리스트를 반환하려고 시도한다.
    각 아이템은 최소 {'rank':int, 'name':str, 'amount':int} 포함하도록 맵핑.
    """
    def normalize_item(it: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        # 후보 키들
        name_keys = ["name", "itmsNm", "stockName", "nm", "itemName", "srtnCdNm"]
        amt_keys  = ["netBuyAmount", "net_buy_amount", "netBp", "amount", "netAmount", "buyAmt", "val", "net"]
        rank_keys = ["rank", "rnk", "no", "order"]

        name = None
        amount = None
        rank = None

        # 이름 찾기
        for k in name_keys:
            if k in it and isinstance(it[k], str) and it[k].strip():
                name = it[k].strip()
                break

        # 금액 찾기 (숫자/문자)
        for k in amt_keys:
            if k in it:
                v = it[k]
                if isinstance(v, (int, float)):
                    amount = int(v)
                    break
                if isinstance(v, str):
                    vv = v.replace(",", "").replace("+", "").strip()
                    if vv.replace("-", "").isdigit():
                        amount = int(vv)
                        break

        # 순위 찾기
        for k in rank_keys:
            if k in it:
                v = it[k]
                try:
                    rank = int(v)
                except:
                    pass
                if isinstance(rank, int):
                    break

        if name is None or amount is None:
            return None

        return {"rank": rank, "name": name, "amount": amount}

    # payload 가 리스트인 경우
    if isinstance(payload, list):
        out = []
        for it in payload:
            if isinstance(it, dict):
                norm = normalize_item(it)
                if norm:
                    out.append(norm)
        if out:
            # rank가 비어있으면 amount desc 로 가짜 순위 부여
            if any(o["rank"] is None for o in out):
                out = sorted(out, key=lambda x: x["amount"], reverse=True)
                for i, o in enumerate(out, 1):
                    o["rank"] = i
            # TOP25
            return sorted(out, key=lambda x: x["rank"])[:25]

    # payload 가 dict 인 경우, 리스트가 들어있는 키를 찾아본다
    if isinstance(payload, dict):
        for v in payload.values():
            res = parse_top25_from_json(v)
            if res:
                return res
        # dict의 values 중 리스트가 있으면 각각 시도
        for k, v in payload.items():
            if isinstance(v, list):
                res = parse_top25_from_json(v)
                if res:
                    return res

    return None


# ────────────────────────────────────────────────────────────────────
# HTML 안에 내장된 JSON (예: window.__APOLLO_STATE__) 추출
# ────────────────────────────────────────────────────────────────────
def extract_json_from_html(html: str) -> Optional[Any]:
    # 1) <script> {...json...} </script> 패턴들 탐색
    # Apollo/Next.js 상태 값 추출 시도
    script_jsons = re.findall(r"<script[^>]*>\s*({[\s\S]*?})\s*</script>", html, re.I)
    for js in script_jsons:
        try:
            return json.loads(js)
        except Exception:
            continue

    # 2) window.__APOLLO_STATE__ = {...};
    m = re.search(r"__APOLLO_STATE__\s*=\s*({[\s\S]*?});", html)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass

    # 3) JSON 배열 형태
    m2 = re.search(r">(\[\s*{[\s\S]*?}\s*\])<", html)
    if m2:
        try:
            return json.loads(m2.group(1))
        except Exception:
            pass

    return None


# ────────────────────────────────────────────────────────────────────
# 단일 타겟 처리: 여러 URL 후보를 순차 시도
# ────────────────────────────────────────────────────────────────────
def fetch_block(title: str, market: str, who: str) -> str:
    attempts = candidate_urls(market, who)
    debug_msgs = [f"▶ {title} ({market}/{who}) 시도 URL 수: {len(attempts)}"]

    for idx, url in enumerate(attempts, 1):
        try:
            r = SESSION.get(url, timeout=TIMEOUT)
            code = r.status_code
            ct = r.headers.get("Content-Type", "")
            debug_msgs.append(f"  [{idx}] {url} -> {code} ({ct})")

            if code != 200:
                continue

            # JSON 응답?
            if "application/json" in ct or (r.text.strip().startswith("{") or r.text.strip().startswith("[")):
                try:
                    payload = r.json()
                except Exception:
                    # 혹시 JSON인데 인코딩 문제일 때
                    payload = json.loads(r.text)
                top25 = parse_top25_from_json(payload)
                if top25:
                    return format_block(title, top25, debug_msgs)
                else:
                    debug_msgs.append("    - JSON 파싱 실패(구조 불명)")
                    continue

            # HTML 응답 → 내장 JSON 추출 시도
            html = r.text
            embedded = extract_json_from_html(html)
            if embedded is not None:
                top25 = parse_top25_from_json(embedded)
                if top25:
                    return format_block(title, top25, debug_msgs)
                else:
                    debug_msgs.append("    - HTML 내장 JSON 파싱 실패")
                    continue

            # HTML 내 테이블 파싱(모바일은 드뭄)
            top25 = parse_table_from_html(html)
            if top25:
                return format_block(title, top25, debug_msgs)

            debug_msgs.append("    - HTML 테이블/JSON 둘 다 실패")

        except Exception as e:
            debug_msgs.append(f"  [{idx}] 예외: {e}")

        # 너무 빠른 연속 요청 방지
        time.sleep(0.5)

    # 전부 실패
    return f"📊 {title} 순매수 TOP25\n❌ 데이터 수집 실패\n" + "\n".join(debug_msgs[:10])


def parse_table_from_html(html: str) -> Optional[List[Dict[str, Any]]]:
    # 모바일에선 잘 안 쓰나, 혹시 대비
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        return None
    items = []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue
        rank_txt = tds[0].get_text(strip=True)
        name = tds[1].get_text(strip=True)
        # 뒤에서 숫자형 후보 찾기
        amt = None
        for td in reversed(tds):
            vv = td.get_text(strip=True).replace(",", "")
            if vv.replace("-", "").isdigit():
                amt = int(vv)
                break
        if rank_txt.isdigit() and name and amt is not None:
            items.append({"rank": int(rank_txt), "name": name, "amount": amt})
        if len(items) >= 25:
            break
    return items if items else None


def format_block(title: str, items: List[Dict[str, Any]], debug_msgs: Optional[List[str]] = None) -> str:
    # rank 없으면 amount desc로 부여
    if any(i.get("rank") in (None, "", 0) for i in items):
        items = sorted(items, key=lambda x: x["amount"], reverse=True)
        for i, it in enumerate(items, 1):
            it["rank"] = i
    items = sorted(items, key=lambda x: x["rank"])[:25]

    lines = [f"📊 {title} 순매수 TOP25"]
    for it in items:
        amt = it["amount"]
        lines.append(f"{it['rank']}. {it['name']} ({amt:,}원)")
    if debug_msgs:
        lines.append("")  # 빈 줄
        lines.append("🔎 DEBUG")
        lines.extend(debug_msgs[:8])  # 너무 길면 앞부분만
    return "\n".join(lines)


def send_telegram(text: str) -> None:
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        res = requests.post(url, data={"chat_id": CHAT_ID, "text": text[:4096]}, timeout=15)
        if res.status_code != 200:
            print("❌ 텔레그램 전송 실패:", res.text)
        else:
            print("✅ 텔레그램 전송 성공")
    except Exception as e:
        print("❌ 텔레그램 전송 예외:", e)


if __name__ == "__main__":
    blocks = []
    for title, market, who in TARGETS:
        blocks.append(fetch_block(title, market, who))
        time.sleep(0.7)
    message = "\n\n".join(blocks)
    send_telegram(message)
