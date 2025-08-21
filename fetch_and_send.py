import os
import requests
from bs4 import BeautifulSoup

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
PROXY_URL = os.getenv("PROXY_URL")

URLS = {
    "기관(KOSPI)"  : ("https://finance.naver.com/sise/sise_deal_rank.naver?sosok=01&investor_gubun=1000", "기관"),
    "기관(KOSDAQ)" : ("https://finance.naver.com/sise/sise_deal_rank.naver?sosok=02&investor_gubun=1000", "기관"),
    "외국인(KOSPI)": ("https://finance.naver.com/sise/sise_deal_rank.naver?sosok=01&investor_gubun=2000", "외국인"),
    "외국인(KOSDAQ)":("https://finance.naver.com/sise/sise_deal_rank.naver?sosok=02&investor_gubun=2000", "외국인"),
}

proxies = {
    "http": PROXY_URL,
    "https": PROXY_URL,
} if PROXY_URL else None

def fetch_top25(url, investor):
    try:
        res = requests.get(url, proxies=proxies, timeout=15)
        res.encoding = "euc-kr"  # 네이버 금융은 euc-kr
        if res.status_code != 200:
            return f"❌ {investor} 오류: 상태코드 {res.status_code}"

        soup = BeautifulSoup(res.text, "html.parser")
        table = soup.select_one("table.type_1")
        if not table:
            return f"❌ {investor} 오류: 테이블 못 찾음"

        rows = table.select("tr")[1:26]  # 헤더 제외, 상위 25개
        results = []
        for r in rows:
            cols = [c.get_text(strip=True) for c in r.select("td")]
            if len(cols) >= 5:
                rank, name, cur_price, diff, volume = cols[:5]
                results.append(f"{rank}. {name} ({cur_price}) {diff}")
        return "\n".join(results)

    except Exception as e:
        return f"❌ {investor} 오류: {e}"

def aggregate_data():
    messages = []
    for title, (url, investor) in URLS.items():
        print(f"[DEBUG] 크롤링 중: {title} → {url} (프록시={PROXY_URL})")
        data = fetch_top25(url, investor)
        messages.append(f"📊 {title} 순매수 TOP25\n{data}\n")
    return "\n".join(messages)

def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    res = requests.post(url, data={"chat_id": CHAT_ID, "text": message})
    if res.status_code == 200:
        print("✅ 텔레그램 전송 성공")
    else:
        print("❌ 텔레그램 전송 실패:", res.text)

if __name__ == "__main__":
    msg = aggregate_data()
    send_telegram(msg)
