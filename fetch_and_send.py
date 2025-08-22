import os
import re
import json
import datetime as dt
import requests
import gspread
import fitz  # PyMuPDF
from PIL import Image
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import Request

# ───────────────────────────────────────────────
# 유틸
# ───────────────────────────────────────────────
def is_korean_holiday_or_weekend(today_kst: dt.date) -> bool:
    # holidays는 액션 실행 환경에 설치되어 있음
    import holidays
    kr_holidays = holidays.country_holidays("KR")
    return today_kst.weekday() >= 5 or today_kst in kr_holidays

def today_kst_date() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).astimezone(
        dt.timezone(dt.timedelta(hours=9))
    ).date()

def col_letters_to_index(s: str) -> int:
    s = s.upper()
    n = 0
    for ch in s:
        if "A" <= ch <= "Z":
            n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1

def parse_a1_range(a1: str):
    if "!" not in a1:
        return a1, None, None, None, None
    sheet_name, rng = a1.split("!", 1)
    m = re.match(r"^([A-Za-z]+)(\d+):([A-Za-z]+)(\d+)$", rng.replace("$", ""))
    if not m:
        return sheet_name, None, None, None, None
    c1 = col_letters_to_index(m.group(1))
    r1 = int(m.group(2)) - 1
    c2 = col_letters_to_index(m.group(3))
    r2 = int(m.group(4)) - 1
    return sheet_name, r1, c1, r2, c2

def tg_send_text(bot_token: str, chat_id: str, text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data={"chat_id": chat_id, "text": text},
            timeout=60,
        )
    except Exception:
        # 액션 로그에만 남김
        print("[WARN] telegram sendMessage failed", flush=True)

def tg_send_photo(bot_token: str, chat_id: str, img_path: str, caption: str = ""):
    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    with open(img_path, "rb") as f:
        files = {"photo": (os.path.basename(img_path), f, "image/png")}
        data = {"chat_id": chat_id, "caption": caption}
        resp = requests.post(url, data=data, files=files, timeout=120)
    resp.raise_for_status()

# ───────────────────────────────────────────────
# Google Sheets → PDF → PNG (지정 범위 한 장 PNG)
# ───────────────────────────────────────────────
def export_sheet_range_as_png(creds_json: str, sheet_id: str, sheet_range: str, scale: int = 2) -> str:
    creds_info = json.loads(creds_json)

    # 시트 접근 (gid 얻기)
    scopes_sheets = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    credentials_sheets = Credentials.from_service_account_info(creds_info, scopes=scopes_sheets)
    gc = gspread.authorize(credentials_sheets)
    sh = gc.open_by_key(sheet_id)

    sheet_name, r1, c1, r2, c2 = parse_a1_range(sheet_range)
    try:
        worksheet = sh.worksheet(sheet_name)
    except gspread.WorksheetNotFound:
        worksheet = sh.sheet1
    gid = worksheet._properties.get("sheetId")

    # 드라이브 토큰
    scopes_drive = ["https://www.googleapis.com/auth/drive.readonly"]
    credentials_drive = Credentials.from_service_account_info(creds_info, scopes=scopes_drive)
    if not credentials_drive.valid:
        credentials_drive.refresh(Request())
    access_token = credentials_drive.token

    # PDF export
    base = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export"
    params = {
        "format": "pdf",
        "gid": str(gid),
        "portrait": "false",
        "fitw": "true",
        "sheetnames": "false",
        "printtitle": "false",
        "pagenumbers": "false",
        "gridlines": "true",
        "fzr": "false",
        "scale": str(max(1, min(scale, 4))),
        "paper": "5",  # A3. 필요시 8(B4) 등으로 조절 가능
        "top_margin": "0.00",
        "bottom_margin": "0.00",
        "left_margin": "0.00",
        "right_margin": "0.00",
    }
    if None not in (r1, c1, r2, c2):
        params.update({"r1": str(r1), "c1": str(c1), "r2": str(r2), "c2": str(c2)})

    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get(base, headers=headers, params=params, timeout=120)
    resp.raise_for_status()

    # PDF → PNG (첫 페이지만: 지정 범위는 한 페이지로 출력됨)
    doc = fitz.open("pdf", resp.content)
    if len(doc) == 0:
        raise RuntimeError("PDF 페이지 없음")
    page = doc[0]
    pix = page.get_pixmap(dpi=150)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

    out_dir = "out"
    os.makedirs(out_dir, exist_ok=True)
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    png_path = os.path.join(out_dir, f"sheet_{gid}_{ts}.png")
    img.save(png_path, "PNG")
    return png_path

# ───────────────────────────────────────────────
# 메인
# ───────────────────────────────────────────────
def main():
    # KST 기준 주말/공휴일 skip (원치 않으면 주석)
    if is_korean_holiday_or_weekend(today_kst_date()):
        print("KR weekend/holiday. Skipping.")
        return

    # === Secrets (GitHub Actions에서 주입) ===
    bot_token = os.environ["BOT_TOKEN"]
    chat_id = os.environ["CHAT_ID"]
    sheet_id = os.environ["SHEET_ID"]
    creds_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]

    # 범위: K29에서 절단하여 두 번 export → 세로 합치기
    sheet_range_top = os.getenv("SHEET_RANGE_TOP", "최종!A1:K29")
    sheet_range_bottom = os.getenv("SHEET_RANGE_BOTTOM", "최종!A30:K58")
    export_scale = int(os.getenv("EXPORT_SCALE", "2"))

    # 각 파트 PNG 생성
    try:
        top_png = export_sheet_range_as_png(creds_json, sheet_id, sheet_range_top, export_scale)
        bot_png = export_sheet_range_as_png(creds_json, sheet_id, sheet_range_bottom, export_scale)
    except Exception as e:
        tg_send_text(bot_token, chat_id, f"❌ 시트 이미지 내보내기 실패: {e}")
        raise

    # 합치기(여백 없이)
    img_top = Image.open(top_png)
    img_bot = Image.open(bot_png)

    total_width = max(img_top.width, img_bot.width)
    total_height = img_top.height + img_bot.height
    final_img = Image.new("RGB", (total_width, total_height), (255, 255, 255))

    y = 0
    final_img.paste(img_top, (0, y)); y += img_top.height
    final_img.paste(img_bot, (0, y))

    out_dir = "out"
    os.makedirs(out_dir, exist_ok=True)
    final_path = os.path.join(out_dir, "final_report.png")
    final_img.save(final_path, "PNG")

    # 캡션
    now_kst = dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=9)))
    caption = f"📊 {now_kst.strftime('%Y-%m-%d')} 수급 현황 레포트"

    tg_send_photo(bot_token, chat_id, final_path, caption=caption)
    print(f"Sent image: {final_path}")

if __name__ == "__main__":
    main()
