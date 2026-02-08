#!/usr/bin/env python3
"""
Google Sheets 書き出しスクリプト（JTB + KNT 統合版）
====================================================
1つのスプレッドシートに以下のタブを書き出す:
  - JTB_現在のクーポン
  - JTB_変動ログ
  - KNT_現在のクーポン
  - KNT_変動ログ
"""

import json
import os
from pathlib import Path
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials


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
def get_or_create_sheet(spreadsheet, title, rows=500, cols=20):
    try:
        return spreadsheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)


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
# データ読み込み
# ============================================================
def load_latest_data(data_dir):
    """指定ディレクトリから最新のcoupons_YYYY-MM-DD.jsonを読む"""
    data_path = Path(data_dir)
    if not data_path.exists():
        return []

    today = datetime.now().strftime("%Y-%m-%d")
    daily_file = data_path / f"coupons_{today}.json"
    if not daily_file.exists():
        files = sorted(data_path.glob("coupons_*.json"), reverse=True)
        if files:
            daily_file = files[0]
        else:
            return []

    with open(daily_file, "r", encoding="utf-8") as f:
        return json.load(f)


def load_change_log(data_dir):
    log_file = Path(data_dir) / "change_log.json"
    if log_file.exists():
        with open(log_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


# ============================================================
# JTB: 現在のクーポン
# ============================================================
def update_jtb_coupon_sheet(spreadsheet, coupons):
    ws = get_or_create_sheet(spreadsheet, "JTB_現在のクーポン")

    headers = [
        "更新日時", "カテゴリ", "ID", "詳細URL", "タイトル", "割引額",
        "エリア", "タイプ", "予約対象期間", "宿泊/出発対象期間", "店舗利用",
        "クーポンコード", "パスワード", "条件", "注意事項", "クーポンアフィリエイトリンク",
    ]

    today = datetime.now().strftime("%Y-%m-%d")
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
            "",
        ])

    ws.clear()
    ws.update(range_name="A1", values=rows)

    ws.format("A1:P1", {
        "backgroundColor": {"red": 0.13, "green": 0.55, "blue": 0.13},
        "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
    })

    set_col_widths(spreadsheet, ws, [
        90, 60, 130, 300, 400, 110, 100, 120, 200, 200, 60, 150, 100, 200, 200, 250,
    ])

    print(f"  ✅ JTB_現在のクーポン を更新（{len(coupons)}件）")


# ============================================================
# KNT: 現在のクーポン
# ============================================================
def update_knt_coupon_sheet(spreadsheet, coupons):
    ws = get_or_create_sheet(spreadsheet, "KNT_現在のクーポン")

    headers = [
        "更新日時", "カテゴリ", "ID", "詳細URL", "タイトル", "割引額",
        "エリア", "タイプ", "申込期間", "宿泊/出発対象期間",
        "クーポンコード", "条件", "注意事項", "クーポンアフィリエイトリンク",
    ]

    today = datetime.now().strftime("%Y-%m-%d")
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
            c.get("discount", "") or detail.get("discount", ""),
            c.get("area", ""),
            c.get("type", ""),
            detail.get("booking_period", ""),
            detail.get("stay_period", ""),
            ", ".join(detail.get("coupon_codes", [])),
            " / ".join(detail.get("conditions", [])),
            " / ".join(detail.get("notes", [])),
            "",
        ])

    ws.clear()
    ws.update(range_name="A1", values=rows)

    ws.format("A1:N1", {
        "backgroundColor": {"red": 0.0, "green": 0.44, "blue": 0.75},
        "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
    })

    set_col_widths(spreadsheet, ws, [
        90, 110, 180, 350, 400, 130, 120, 160, 200, 200, 150, 200, 200, 250,
    ])

    print(f"  ✅ KNT_現在のクーポン を更新（{len(coupons)}件）")


# ============================================================
# 変動ログ（JTB / KNT 共通処理）
# ============================================================
def update_change_log_sheet(spreadsheet, sheet_name, change_log, header_color):
    ws = get_or_create_sheet(spreadsheet, sheet_name)

    headers = ["日付", "種別", "カテゴリ", "ID", "タイトル", "エリア"]

    change_log.sort(key=lambda x: x.get("date", ""), reverse=True)

    rows = [headers]
    for e in change_log:
        rows.append([
            e.get("date", ""),
            e.get("type", ""),
            e.get("category", ""),
            e.get("id", ""),
            e.get("title", ""),
            e.get("discount", e.get("area", "")),
        ])

    ws.clear()
    ws.update(range_name="A1", values=rows)

    ws.format("A1:F1", {
        "backgroundColor": header_color,
        "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
    })

    # 新規=薄緑、消失=薄赤
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

    set_col_widths(spreadsheet, ws, [90, 80, 110, 180, 400, 130])

    print(f"  ✅ {sheet_name} を更新（{len(change_log)}件）")


# ============================================================
# メイン
# ============================================================
def main():
    print("📊 Google Sheets 書き出し開始（JTB + KNT 統合）")

    spreadsheet = connect_sheets()

    # ----- JTB -----
    jtb_data_dir = "./jtb_coupon_data"
    jtb_coupons = load_latest_data(jtb_data_dir)
    jtb_log = load_change_log(jtb_data_dir)

    if jtb_coupons:
        print(f"\n📦 JTB: {len(jtb_coupons)}件")
        update_jtb_coupon_sheet(spreadsheet, jtb_coupons)
        update_change_log_sheet(
            spreadsheet, "JTB_変動ログ", jtb_log,
            {"red": 0.8, "green": 0.4, "blue": 0.0},
        )
    else:
        print("\n📦 JTB: データなし（スキップ）")

    # ----- KNT -----
    knt_data_dir = "./knt_coupon_data"
    knt_coupons = load_latest_data(knt_data_dir)
    knt_log = load_change_log(knt_data_dir)

    if knt_coupons:
        print(f"\n📦 KNT: {len(knt_coupons)}件")
        update_knt_coupon_sheet(spreadsheet, knt_coupons)
        update_change_log_sheet(
            spreadsheet, "KNT_変動ログ", knt_log,
            {"red": 0.0, "green": 0.35, "blue": 0.65},
        )
    else:
        print("\n📦 KNT: データなし（スキップ）")

    print("\n✅ Google Sheets 書き出し完了")


if __name__ == "__main__":
    main()
