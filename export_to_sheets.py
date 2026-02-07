#!/usr/bin/env python3
"""
Google Sheets 書き出しスクリプト（シンプル版）
==============================================
シート1: 現在のクーポン（一覧ページに掲載されている全件）
シート2: 変動ログ（新規追加・消失の履歴）
"""

import json
import os
from pathlib import Path
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

DATA_DIR = Path("./jtb_coupon_data")


# ============================================================
# Google Sheets 接続
# ============================================================
def connect_sheets():
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    spreadsheet_id = os.environ.get("SPREADSHEET_ID", "")

    if not creds_json or not spreadsheet_id:
        raise RuntimeError("環境変数 GOOGLE_SERVICE_ACCOUNT_JSON / SPREADSHEET_ID が未設定")

    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write(creds_json)
        creds_file = f.name

    creds = Credentials.from_service_account_file(creds_file, scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
    ])
    gc = gspread.authorize(creds)
    os.unlink(creds_file)

    return gc.open_by_key(spreadsheet_id)


# ============================================================
# ヘルパー
# ============================================================
def get_or_create_sheet(spreadsheet, title):
    try:
        return spreadsheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title, rows=500, cols=20)


def set_col_widths(spreadsheet, ws, widths):
    requests = []
    for i, w in enumerate(widths):
        requests.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": ws.id,
                    "dimension": "COLUMNS",
                    "startIndex": i,
                    "endIndex": i + 1,
                },
                "properties": {"pixelSize": w},
                "fields": "pixelSize",
            }
        })
    if requests:
        spreadsheet.batch_update({"requests": requests})


# ============================================================
# 日次データ読み込み
# ============================================================
def load_today_data():
    today = datetime.now().strftime("%Y-%m-%d")
    daily_file = DATA_DIR / f"coupons_{today}.json"
    if not daily_file.exists():
        # 最新ファイルを探す
        files = sorted(DATA_DIR.glob("coupons_*.json"), reverse=True)
        if files:
            daily_file = files[0]
        else:
            return []

    with open(daily_file, "r", encoding="utf-8") as f:
        return json.load(f)


def load_change_log():
    log_file = DATA_DIR / "change_log.json"
    if log_file.exists():
        with open(log_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


# ============================================================
# シート1: 現在のクーポン
# ============================================================
def update_coupon_sheet(spreadsheet, coupons):
    ws = get_or_create_sheet(spreadsheet, "現在のクーポン")

    headers = [
        "更新日時", "カテゴリ", "ID", "詳細URL", "タイトル", "割引額",
        "エリア", "タイプ", "予約対象期間", "宿泊/出発対象期間", "店舗利用",
        "クーポンコード", "パスワード", "条件", "注意事項", "クーポンアフィリエイトリンク",
    ]

    today = datetime.now().strftime("%Y-%m-%d")

    # カテゴリ → エリア でソート
    coupons.sort(key=lambda x: (x.get("category", ""), x.get("area", "")))

    rows = [headers]
    for c in coupons:
        detail = c.get("detail_data") or {}
        rows.append([
            today,
            c.get("category", ""),
            c.get("id", ""),
            c.get("detail_url", ""),
            c.get("title", ""),
            c.get("discount", ""),
            c.get("area", ""),
            c.get("type", ""),
            c.get("booking_period", ""),
            c.get("stay_period", ""),
            "✅" if c.get("store_available") else "",
            ", ".join(detail.get("coupon_codes", [])),
            ", ".join(detail.get("passwords", [])),
            " / ".join(detail.get("conditions", [])),
            " / ".join(detail.get("notes", [])),
            "",  # クーポンアフィリエイトリンク（空欄）
        ])

    ws.clear()
    ws.update(range_name="A1", values=rows)

    # ヘッダー書式（16列: A〜P）
    ws.format("A1:P1", {
        "backgroundColor": {"red": 0.13, "green": 0.55, "blue": 0.13},
        "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
    })

    set_col_widths(spreadsheet, ws, [
        90,   # 更新日時
        60,   # カテゴリ
        130,  # ID
        300,  # 詳細URL
        400,  # タイトル
        110,  # 割引額
        100,  # エリア
        120,  # タイプ
        200,  # 予約対象期間
        200,  # 宿泊/出発対象期間
        60,   # 店舗利用
        150,  # クーポンコード
        100,  # パスワード
        200,  # 条件
        200,  # 注意事項
        250,  # アフィリエイトリンク
    ])

    print(f"  ✅ 「現在のクーポン」を更新（{len(coupons)}件）")


# ============================================================
# シート2: 変動ログ
# ============================================================
def update_change_log_sheet(spreadsheet, change_log):
    ws = get_or_create_sheet(spreadsheet, "変動ログ")

    headers = ["日付", "種別", "カテゴリ", "ID", "タイトル", "割引額"]

    # 新しい順
    change_log.sort(key=lambda x: x.get("date", ""), reverse=True)

    rows = [headers]
    for e in change_log:
        rows.append([
            e.get("date", ""),
            e.get("type", ""),
            e.get("category", ""),
            e.get("id", ""),
            e.get("title", ""),
            e.get("discount", ""),
        ])

    ws.clear()
    ws.update(range_name="A1", values=rows)

    # ヘッダー書式
    ws.format("A1:F1", {
        "backgroundColor": {"red": 0.8, "green": 0.4, "blue": 0.0},
        "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
    })

    # 新規=薄緑、消失=薄赤で色分け
    batch_requests = []
    for i, e in enumerate(change_log, start=2):
        if "新規" in e.get("type", ""):
            color = {"red": 0.85, "green": 1, "blue": 0.85}
        elif "消失" in e.get("type", ""):
            color = {"red": 1, "green": 0.85, "blue": 0.85}
        else:
            continue

        batch_requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": ws.id,
                    "startRowIndex": i - 1,
                    "endRowIndex": i,
                    "startColumnIndex": 0,
                    "endColumnIndex": 6,
                },
                "cell": {"userEnteredFormat": {"backgroundColor": color}},
                "fields": "userEnteredFormat.backgroundColor",
            }
        })

    if batch_requests:
        spreadsheet.batch_update({"requests": batch_requests})

    set_col_widths(spreadsheet, ws, [90, 80, 60, 130, 400, 110])

    print(f"  ✅ 「変動ログ」を更新（{len(change_log)}件）")


# ============================================================
# メイン
# ============================================================
def main():
    print("📊 Google Sheets 書き出し開始")

    spreadsheet = connect_sheets()
    coupons = load_today_data()
    change_log = load_change_log()

    domestic = [c for c in coupons if c.get("category") == "国内"]
    overseas = [c for c in coupons if c.get("category") == "海外"]
    print(f"   国内: {len(domestic)}件 / 海外: {len(overseas)}件 / 合計: {len(coupons)}件")

    update_coupon_sheet(spreadsheet, coupons)
    update_change_log_sheet(spreadsheet, change_log)

    print("✅ Google Sheets 書き出し完了")


if __name__ == "__main__":
    main()
