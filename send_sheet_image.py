import os
import io
import re
import json
import subprocess
import datetime as dt
import requests
import holidays
import gspread
from google.oauth2.service_account import Credentials

# ──────────────────────────────────────────────────────────
# 기본 유틸
# ──────────────────────────────────────────────────────────
def is_korean_holiday_or_weekend(today_kst: dt.date) -> bool:
    kr_holidays = holidays.country_holidays(os.getenv("HOLIDAY_COUNTRY", "KR"))
    return today_kst.weekday() >= 5 or today_kst in kr_holidays  # 토(5), 일(6) 또는 공휴일

def today_kst_date() -> dt.date:
    now_utc = dt.datetime.utcnow()
    kst = now_utc + dt.timedelta(hours=9)
    return kst.date()

def col_letters_to_index(s: str) -> int:
    # A1 → 0-index
    s = s.upper()
    n = 0
    for ch in s:
        if 'A' <= ch <= 'Z':
            n = n * 26 + (ord(ch) - ord('A') + 1)
    return n - 1  # 0-index

def parse_a1_range(a1: str):
    """
    '시트명!A1:D20' → (sheet_name, r1,c1,r2,c2) (모두 0-index, r2/c2는 포함 경계 아님)
    Google export r/c 파라미터는 0-index이고 r2/c2는 'exclusive'가 아니라 'inclusive'처럼 동작하는데
    실제 시트 수식과 혼동 방지를 위해 여기서는 r2/c2를 'exclusive-1'로 조정해 URL 만들 때 +1 안 하도록 맞춥니다.
    (즉 r1=0,c1=0,r2=20,c2=4 → A1:D20 범위)
    """
    if '!' not in a1:
        # 시트명만 온 경우
        return a1, None, None, None, None

    sheet_name, rng = a1.split('!', 1)
    m = re.match(r'^([A-Za-z]+)(\d+):([A-Za-z]+)(\d+)$', rng.replace('$',''))
    if not m:
        # 단일 시작셀만 온 경우 (예: 시트1!B3)
        m2 = re.match(r'^([A-Za-z]+)(\d+)$', rng.replace('$',''))
        if m2:
            c1 = col_letters_to_index(m2.group(1))
            r1 = int(m2.group(2)) - 1
            # 끝값 미지정 → 전체로 간주 (export URL에서 r/c 지정 생략)
            return sheet_name, r1, c1, None, None
        else:
            # 범위 파싱 실패 → 전체 시트
            return sheet_name, None, None, None, None

    c1 = col_letters_to_index(m.group(1))
    r1 = int(m.group(2)) - 1
    c2 = col_letters_to_index(m.group(3))
    r2 = int(m.group(4)) - 1
    # r2/c2는 포함 범위이므로 그대로 유지
    return sheet_name, r1, c1, r2, c2

def tg_send_photo(bot_token: str, chat_id: str, img_path: str, caption: str = ""):
    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    with open(img_path, "rb") as f:
        files = {"photo": (os.path.basename(img_path), f, "image/png")}
        data = {"chat_id": chat_id, "caption": caption}
        resp = requests.post(url, data=data, files=files, timeout=120)
    resp.raise_for_status()

# ──────────────────────────────────────────────────────────
# 핵심: Google Sheets → PDF(범위 지정) → PNG 변환
# ──────────────────────────────────────────────────────────
def export_sheet_range_as_pdf_png(creds_json: str, sheet_id: str, sheet_range: str | None, scale: int = 2) -> str:
    """
    - 서비스 계정으로 OAuth 토큰 발급
    - Sheets PDF export URL 구성(범위/그리드라인/배율 등)
    - PDF 다운로드 후 pdftoppm 로 PNG 변환
    - 변환된 PNG 경로 반환
    """
    creds_info = json.loads(creds_json)

    # gspread로 gid 조회(시트명 → gid)
    scopes_sheets = ['https://www.googleapis.com/auth/spreadsheets.readonly']
    credentials_sheets = Credentials.from_service_account_info(creds_info, scopes=scopes_sheets)
    gc = gspread.authorize(credentials_sheets)
    sh = gc.open_by_key(sheet_id)

    # range 파싱
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

    gid = worksheet._properties.get("sheetId")  # int

    # Drive scope로 액세스 토큰 (export 호출은 Drive 권한 필요)
    scopes_drive = ['https://www.googleapis.com/auth/drive.readonly']
    credentials_drive = Credentials.from_service_account_info(creds_info, scopes=scopes_drive)
    access_token = credentials_drive.with_scopes(scopes_drive).token
    if not access_token:
        # 강제 refresh
        credentials_drive.refresh(requests.Request())
        access_token = credentials_drive.token

    # PDF Export URL 구성
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
        "scale": str(max(1, min(scale, 4))),  # 1~4 권장
        # 여백 최소화
        "top_margin": "0.00",
        "bottom_margin": "0.00",
        "left_margin": "0.00",
        "right_margin": "0.00",
        # 용지 크기(가로) A3가 넓게 보기 좋음 (0=Letter, 1=Tabloid, 2=Legal, 3=Statement, 4=Executive,
        # 5=A3, 6=A4, 7=A5, 8=B4, 9=B5)
        "paper": "5",
    }

    # 범위가 명시되었으면 r/c 파라미터 추가 (0-index, 끝은 포함 범위)
    # r1/c1/r2/c2 를 모두 알 때만 지정. 일부만 있으면 전체 출력로 둠.
    if None not in (r1, c1, r2, c2):
        params.update({
            "r1": str(r1),
            "c1": str(c1),
            "r2": str(r2),
            "c2": str(c2),
        })

    # 요청
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get(base, headers=headers, params=params, timeout=120)
    resp.raise_for_status()

    # 파일 저장
    out_dir = "out"
    os.makedirs(out_dir, exist_ok=True)
    ts = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    pdf_path = os.path.join(out_dir, f"sheet_{gid}_{ts}.pdf")
    with open(pdf_path, "wb") as f:
        f.write(resp.content)

    # PDF → PNG (첫 페이지만)
    png_prefix = os.path.join(out_dir, f"sheet_{gid}_{ts}")
    cmd = ["pdftoppm", "-png", "-singlefile", pdf_path, png_prefix]
    subprocess.run(cmd, check=True)
    png_path = png_prefix + ".png"
    return png_path

# ──────────────────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────────────────
def main():
    # 휴일/주말 스킵
    if is_korean_holiday_or_weekend(today_kst_date()):
        print("KR weekend/holiday. Skipping.")
        return

    bot_token = os.environ["BOT_TOKEN"]
    chat_id = os.environ["CHAT_ID"]
    sheet_id = os.environ["SHEET_ID"]
    creds_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    sheet_range = os.getenv("SHEET_RANGE", "").strip() or None
    export_scale = int(os.getenv("EXPORT_SCALE", "2"))

    try:
        png_path = export_sheet_range_as_pdf_png(
            creds_json=creds_json,
            sheet_id=sheet_id,
            sheet_range=sheet_range,
            scale=export_scale,
        )
    except Exception as e:
        # 오류 메시지 텔레그램 알림
        msg = f"❌ 시트 이미지 내보내기 실패: {e}"
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data={"chat_id": chat_id, "text": msg},
            timeout=60,
        )
        raise

    # 캡션(한국시간 기준)
    now_kst = dt.datetime.utcnow() + dt.timedelta(hours=9)
    caption = f"📄 스프레드시트 스냅샷 ({now_kst.strftime('%Y-%m-%d %H:%M KST')})"
    tg_send_photo(bot_token, chat_id, png_path, caption=caption)
    print(f"Sent image: {png_path}")

if __name__ == "__main__":
    main()
