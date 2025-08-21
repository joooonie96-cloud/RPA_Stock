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
    실제
