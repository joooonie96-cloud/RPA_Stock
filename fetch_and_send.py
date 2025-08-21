import requests
from bs4 import BeautifulSoup
import datetime
import os

# ======================
# 설정 (깃허브 시크릿에서 불러오기)
# ======================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID   = os.environ.get("CHAT_ID")

URLS = {
    "기관(KOSPI)"  : ("https://finance.naver.com/sise/sise_deal_rank.naver?sosok=01&investor_gubun=1000", "기관"),
    "기관(KOSDAQ)" : ("https://finance.naver.com/sise/sise_deal_rank.naver?sosok=02&investor_gubun=1000", "기관"),
    "외국인(KOSPI)": ("https://finance.naver.com/sise/sise_deal_rank.naver?sosok=01&investor_gubun=2000", "외국인"),
    "외국인(KOSDAQ)":("https://finance.naver.com/sise/sise_deal_rank.naver?sosok=02&investor_gubun=2000", "외국인"),
}


# ======================
# 크롤링 함수
# ======================
def fetch_data(url, investor_type):
    res = requests.get(url)
    res.raise_for_status()
    soup = BeautifulSoup(res.text, "html.parser")

    # 날짜 체크
    date_tag = soup.select_one("div.sise_guide_date")
    today = datetime.datetime.now().strftime("%y.%m.%d")
    if not date_tag or today not in date_tag.text:
        raise ValueError(f"[{investor_type}] 날짜 불일치! {date_tag.text if date_tag else '날짜없음'}")

    table = soup.select_one("table.type_2")
    rows = table.select("tr")[2:]  # 헤더 제외

    data = []
    for row in rows:
        cols = row.select("td")
        if len(cols) < 6:  # 데이터 없는 줄 skip
            continue

        stock = cols[1].get_text(strip=True)
        amount = cols[5].get_text(strip=True).replace(",", "")

        if not amount or not amount.lstrip("-").isdigit():
            continue

        data.append({
            "종목": stock,
            "순매수금액": int(amount),
            "투자자": investor_type
        })

    return data


# ======================
# 데이터 집계
# ======================
def aggregate_data():
    all_data = {"기관": [], "외국인": []}
    for name, (url, investor) in URLS.items():
        try:
            data = fetch_data(url, investor)
            all_data[investor].extend(data)
        except Exception as e:
            print(f"❌ {name} 오류: {e}")

    # 투자자별 상위 25개 추출
    result = {}
    for investor, items in all_data.items():
        sorted_items = sorted(items, key=lambda x: x["순매수금액"], reverse=True)
        result[investor] = sorted_items[:25]
    return result


# ======================
# 메시지 포맷팅
# ======================
def format_message(result):
    msg = []
    for investor, items in result.items():
        msg.append(f"📊 {investor} 순매수 TOP25")
        for i, item in enumerate(items, 1):
            msg.append(f"{i}. {item['종목']} ({item['순매수금액']:,}원)")
        msg.append("")
    return "\n".join(msg)


# ======================
# 텔레그램 전송
# ======================
def send_telegram_message(bot_token, chat_id, text):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text
    }
    res = requests.post(url, data=payload)
    if res.status_code != 200:
        print("❌ 텔레그램 전송 실패:", res.text)
    else:
        print("✅ 텔레그램 전송 성공")


# ======================
# 실행부
# ======================
if __name__ == "__main__":
    data = aggregate_data()
    message = format_message(data)
    send_telegram_message(BOT_TOKEN, CHAT_ID, message)
