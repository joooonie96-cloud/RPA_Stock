import os
import time
import requests
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import chromedriver_autoinstaller

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID   = os.environ.get("CHAT_ID")

URLS = {
    "기관(KOSPI)"  : ("https://finance.naver.com/sise/sise_deal_rank.naver?sosok=01&investor_gubun=1000", "기관"),
    "기관(KOSDAQ)" : ("https://finance.naver.com/sise/sise_deal_rank.naver?sosok=02&investor_gubun=1000", "기관"),
    "외국인(KOSPI)": ("https://finance.naver.com/sise/sise_deal_rank.naver?sosok=01&investor_gubun=2000", "외국인"),
    "외국인(KOSDAQ)":("https://finance.naver.com/sise/sise_deal_rank.naver?sosok=02&investor_gubun=2000", "외국인"),
}

# ======================
# 드라이버 초기화
# ======================
def init_driver():
    chromedriver_autoinstaller.install()  # 크롬 버전에 맞는 드라이버 자동 설치
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    driver = webdriver.Chrome(options=options)
    return driver

# ======================
# 크롤링
# ======================
def fetch_data(url, investor_type, driver):
    driver.get(url)
    print(f"[DEBUG] 페이지 로딩 완료: {url}")

    # 👉 iframe 로딩 기다리고 진입
    try:
        iframe = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "iframe#frame_ex"))
        )
        driver.switch_to.frame(iframe)
        print(f"[DEBUG] {investor_type} iframe 진입 성공")
    except Exception:
        raise ValueError(f"[{investor_type}] iframe 로딩 실패")

    # 👉 iframe 안에서 테이블 로딩 기다리기
    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table.type_2"))
        )
    except Exception:
        driver.switch_to.default_content()
        raise ValueError(f"[{investor_type}] 테이블 로딩 실패")

    # 👉 테이블 파싱
    soup = BeautifulSoup(driver.page_source, "html.parser")
    table = soup.select_one("table.type_2")
    if not table:
        driver.switch_to.default_content()
        raise ValueError(f"[{investor_type}] 테이블 못 찾음 (iframe 안)")

    rows = table.select("tr")
    data = []
    for row in rows:
        cols = row.select("td")
        if len(cols) < 6:
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

    # 👉 다음 URL 크롤링을 위해 다시 메인 페이지로 복귀
    driver.switch_to.default_content()
    return data

# ======================
# 데이터 집계
# ======================
def aggregate_data():
    driver = init_driver()
    all_data = {"기관": [], "외국인": []}
    for name, (url, investor) in URLS.items():
        try:
            data = fetch_data(url, investor, driver)
            all_data[investor].extend(data)
        except Exception as e:
            print(f"❌ {name} 오류: {e}")
    driver.quit()

    result = {}
    for investor, items in all_data.items():
        sorted_items = sorted(items, key=lambda x: x["순매수금액"], reverse=True)
        result[investor] = sorted_items[:25]
    return result

# ======================
# 메시지 포맷
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
    payload = {"chat_id": chat_id, "text": text}
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
