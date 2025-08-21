import requests
import os

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID   = os.environ.get("CHAT_ID")

URLS = {
    "기관(KOSPI)"  : ("https://api.finance.naver.com/sise/investorDealRankJson.naver?investorType=1000&marketType=kospi", "기관"),
    "기관(KOSDAQ)" : ("https://api.finance.naver.com/sise/investorDealRankJson.naver?investorType=1000&marketType=kosdaq", "기관"),
    "외국인(KOSPI)": ("https://api.finance.naver.com/sise/investorDealRankJson.naver?investorType=2000&marketType=kospi", "외국인"),
    "외국인(KOSDAQ)":("https://api.finance.naver.com/sise/investorDealRankJson.naver?investorType=2000&marketType=kosdaq", "외국인"),
}

HEADERS = {"User-Agent": "Mozilla/5.0"}

def fetch_data(url, investor_type):
    res = requests.get(url, headers=HEADERS)
    res.raise_for_status()
    data = res.json()

    result = []
    for item in data:
        stock = item.get("name")
        amount = item.get("net_buy_amount")  # 순매수금액
        if stock and amount is not None:
            result.append({
                "종목": stock,
                "순매수금액": int(amount),
                "투자자": investor_type
            })
    return result

def aggregate_data():
    all_data = {"기관": [], "외국인": []}
    for name, (url, investor) in URLS.items():
        try:
            data = fetch_data(url, investor)
            all_data[investor].extend(data)
        except Exception as e:
            print(f"❌ {name} 오류: {e}")

    result = {}
    for investor, items in all_data.items():
        sorted_items = sorted(items, key=lambda x: x["순매수금액"], reverse=True)
        result[investor] = sorted_items[:25]
    return result

def format_message(result):
    msg = []
    for investor, items in result.items():
        msg.append(f"📊 {investor} 순매수 TOP25")
        for i, item in enumerate(items, 1):
            msg.append(f"{i}. {item['종목']} ({item['순매수금액']:,}원)")
        msg.append("")
    return "\n".join(msg)

def send_telegram_message(bot_token, chat_id, text):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    res = requests.post(url, data=payload)
    if res.status_code != 200:
        print("❌ 텔레그램 전송 실패:", res.text)
    else:
        print("✅ 텔레그램 전송 성공")

if __name__ == "__main__":
    data = aggregate_data()
    message = format_message(data)
    send_telegram_message(BOT_TOKEN, CHAT_ID, message)
