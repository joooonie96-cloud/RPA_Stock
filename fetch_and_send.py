import os
import io
import re
import json
import datetime as dt
import requests
import holidays
import gspread
from google.oauth2.service_account import Credentials
import fitz  # PyMuPDF
from PIL import Image


# ──────────────────────────────────────────────────────────
# 기본 유틸
# ──────────────────────────────────────────────────────────
def is_korean_holiday_or_weekend(today_kst: dt.date) -> bool:
    kr_holidays = holidays.country_holidays(os.getenv("HOLIDAY_COUNTRY", "KR"))
    return today_kst.weekday() >= 5 or today_kst in kr_holidays


def today_kst_date() -> dt.date:
    now_utc = dt.datetime.now(dt.UTC)
    kst = now_utc + dt.timedelta(hours=9)
    return kst.date()


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
        m2 = re.match(r"^([A-Za-z]+)(\d+)$", rng.replace("$", ""))
        if m2:
            c1 = col_letters_to_index(m2.group(1))
            r1 = int(m2.group(2)) - 1
            return sheet_name, r1, c1, None, None
        else:
            return sheet_name, None, None, None, None

    c1 = col_letters_to_index(m.group(1))
    r1 = int(m.group(2)) - 1
    c2 = col_letters_to_index(m.group(3))
    r2 = int(m.group(4)) - 1
    return sheet_name, r1, c1, r2, c2


def tg_send_photo(bot_token: str, chat_id: str, img_path: str, caption: str = ""):
    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    with open(img_path, "rb") as f:
        files = {"photo": (os.path.basename(img_path), f, "image/png")}
        data = {"chat_id": chat_id, "caption": caption}
        resp = requests.post(url, data=data, files=files, timeout=120)
    resp.raise_for_status()


def getenv_int(name: str, default: int) -> int:
    raw = os.getenv(name, "")
    try:
        return int(raw) if raw and raw.strip() else default
    except Exception:
        return default


# ──────────────────────────────────────────────────────────
# 핵심: Google Sheets → PDF(범위 지정) → PNG 변환
# ──────────────────────────────────────────────────────────
def export_sheet_range_as_png(
    creds_json: str, sheet_id: str, sheet_range: str | None, scale: int = 2
) -> str:
    creds_info = json.loads(creds_json)

    scopes_sheets = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    credentials_sheets = Credentials.from_service_account_info(
        creds_info, scopes=scopes_sheets
    )
    gc = gspread.authorize(credentials_sheets)
    sh = gc.open_by_key(sheet_id)

    worksheet = None
    r1 = c1 = r2 = c2 = None
    if sheet_range and sheet_range.strip():
        sheet_name, r1, c1, r2, c2 = parse_a1_range(sheet_range.strip())
        try:
            worksheet = sh.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            worksheet = sh.sheet1
    else:
        worksheet = sh.sheet1

    gid = worksheet._properties.get("sheetId")

    scopes_drive = ["https://www.googleapis.com/auth/drive.readonly"]
    credentials_drive = Credentials.from_service_account_info(
        creds_info, scopes=scopes_drive
    )
    import google.auth.transport.requests

    request = google.auth.transport.requests.Request()
    credentials_drive.refresh(request)
    access_token = credentials_drive.token

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
        "top_margin": "0.00",
        "bottom_margin": "0.00",
        "left_margin": "0.00",
        "right_margin": "0.00",
        "paper": "5",  # A3
    }
    if None not in (r1, c1, r2, c2):
        params.update(
            {"r1": str(r1), "c1": str(c1), "r2": str(r2), "c2": str(c2)}
        )

    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get(base, headers=headers, params=params, timeout=120)
    resp.raise_for_status()

    out_dir = "out"
    os.makedirs(out_dir, exist_ok=True)
    ts = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    pdf_path = os.path.join(out_dir, f"sheet_{gid}_{ts}.pdf")
    with open(pdf_path, "wb") as f:
        f.write(resp.content)

    # PDF → PNG (한 장으로 합치기)
    pdf_doc = fitz.open(pdf_path)
    imgs = []
    for page in pdf_doc:
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale))
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        imgs.append(img)

    total_height = sum(im.height for im in imgs)
    max_width = max(im.width for im in imgs)
    combined = Image.new("RGB", (max_width, total_height), (255, 255, 255))

    y = 0
    for im in imgs:
        combined.paste(im, (0, y))
        y += im.height

    png_path = os.path.join(out_dir, f"sheet_{gid}_{ts}.png")
    combined.save(png_path)
    return png_path


# ──────────────────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────────────────
def main():
    if is_korean_holiday_or_weekend(today_kst_date()):
        print("KR weekend/holiday. Skipping.")
        return

    bot_token = os.environ["BOT_TOKEN"]
    chat_id = os.environ["CHAT_ID"]
    sheet_id = os.environ["SHEET_ID"]
    creds_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]

    sheet_range_top = os.getenv("SHEET_RANGE_TOP") or "최종!A1:K29"
    sheet_range_bottom = os.getenv("SHEET_RANGE_BOTTOM") or "최종!A30:K58"
    export_scale = getenv_int("EXPORT_SCALE", 2)

    try:
        png_top = export_sheet_range_as_png(
            creds_json=creds_json,
            sheet_id=sheet_id,
            sheet_range=sheet_range_top,
            scale=export_scale,
        )
        png_bottom = export_sheet_range_as_png(
            creds_json=creds_json,
            sheet_id=sheet_id,
            sheet_range=sheet_range_bottom,
            scale=export_scale,
        )

        img1 = Image.open(png_top)
        img2 = Image.open(png_bottom)
        total_height = img1.height + img2.height
        max_width = max(img1.width, img2.width)
        combined = Image.new("RGB", (max_width, total_height), (255, 255, 255))
        combined.paste(img1, (0, 0))
        combined.paste(img2, (0, img1.height))

        final_path = "out/final_combined.png"
        combined.save(final_path)

    except Exception as e:
        msg = f"❌ 시트 이미지 내보내기 실패: {e}"
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data={"chat_id": chat_id, "text": msg},
            timeout=60,
        )
        raise

    now_kst = dt.datetime.now(dt.UTC) + dt.timedelta(hours=9)
    caption = f"📊 오늘 날짜의 수급 현황 레포트 ({now_kst.strftime('%Y-%m-%d KST')})"
    tg_send_photo(bot_token, chat_id, final_path, caption=caption)
    print(f"Sent image: {final_path}")


if __name__ == "__main__":
    main()
