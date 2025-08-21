import requests
from bs4 import BeautifulSoup

url = "https://finance.naver.com/sise/sise_deal_rank.naver?sosok=01&investor_gubun=1000"  # 기관 KOSPI
headers = {"User-Agent": "Mozilla/5.0"}

res = requests.get(url, headers=headers)
print("HTTP 상태코드:", res.status_code)

soup = BeautifulSoup(res.text, "html.parser")

# 1. 날짜 태그 찍기
date_tag = soup.select_one("div.sise_guide_date")
print("날짜 태그:", date_tag.text if date_tag else "없음")

# 2. 테이블 유무 확인
table = soup.select_one("table.type_2")
if not table:
    print("❌ 테이블 못 찾음")
else:
    print("✅ 테이블 찾음")

    # 3. 첫 3줄만 찍어보기
    rows = table.select("tr")
    for row in rows[:5]:
        print([c.get_text(strip=True) for c in row.select("td")])
