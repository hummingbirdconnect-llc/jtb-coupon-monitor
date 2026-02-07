#!/usr/bin/env python3
"""
JTB クーポンデータを Google Sheets に書き出すスクリプト
=====================================================
GitHub Actions から自動実行し、スプレッドシートを毎日更新する。

2つのシートを管理:
  - 「クーポン一覧」: 最新のクーポン全件（毎回上書き）
  - 「変動ログ」: 追加・削除・変更の履歴（追記）

必要な環境変数:
  GOOGLE_SERVICE_ACCOUNT_JSON: サービスアカウントのJSONキー（中身そのまま）
  SPREADSHEET_ID: 対象スプレッドシートのID
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError:
    print("❌ gspread / google-auth が未インストールです")
    print("   pip install gspread google-auth")
    sys.exit(1)


# ============================================================
# 設定
# ============================================================
DATA_DIR = Path("./jtb_coupon_data")
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def get_gspread_client():
    """サービスアカウントで認証してgspreadクライアントを返す"""
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not sa_json:
        print("❌ 環境変数 GOOGLE_SERVICE_ACCOUNT_JSON が設定されていません")
        sys.exit(1)

    sa_info = json.loads(sa_json)
    creds = Credentials.from_service_account_info(sa_info, scopes=SCOPES)
    return gspread.authorize(creds)


def get_spreadsheet(client):
    """スプレッドシートを取得"""
    spreadsheet_id = os.environ.get("SPREADSHEET_ID")
    if not spreadsheet_id:
        print("❌ 環境変数 SPREADSHEET_ID が設定されていません")
        sys.exit(1)

    return client.open_by_key(spreadsheet_id)


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def load_today_data():
    """今日のクーポンデータを読み込む"""
    filepath = DATA_DIR / f"coupons_{today_str()}.json"
    if not filepath.exists():
        print(f"⚠️ 今日のデータファイルが見つかりません: {filepath}")
        return None
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def load_report():
    """今日のレポート（差分情報）を読み込む"""
    filepath = DATA_DIR / "reports" / f"report_{today_str()}.txt"
    if not filepath.exists():
        return None
    with open(filepath, "r", encoding="utf-8") as f:
        return f.read()


# ============================================================
# シート1: クーポン一覧（最新状態を上書き）
# ============================================================
def update_coupon_list_sheet(spreadsheet, data):
    """「クーポン一覧」シートを最新データで上書き"""
    sheet_name = "クーポン一覧"

    # シートがなければ作成
    try:
        ws = spreadsheet.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=sheet_name, rows=100, cols=15)

    # ヘッダー行
    headers = [
        "更新日時",
        "ID",
        "タイトル",
        "割引額",
        "エリア",
        "タイプ",
        "予約対象期間",
        "宿泊/出発対象期間",
        "店舗利用",
        "クーポンコード",
        "パスワード",
        "条件",
        "注意事項",
        "詳細URL",
    ]

    rows = [headers]

    for c in data["coupons"]:
        detail = c.get("detail_data") or {}
        rows.append([
            data["scraped_at"][:16],
            c.get("id", ""),
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
            c.get("detail_url", ""),
        ])

    # シート全体をクリアして書き込み
    ws.clear()
    ws.update(range_name="A1", values=rows)

    # ヘッダー行の書式設定
    ws.format("A1:N1", {
        "backgroundColor": {"red": 0.2, "green": 0.4, "blue": 0.7},
        "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
    })

    # 列幅の自動調整（近似値で設定）
    # A:更新日時, B:ID, C:タイトル, D:割引額...
    col_widths = [120, 140, 400, 100, 60, 80, 200, 200, 60, 150, 100, 200, 200, 300]
    requests_body = []
    for i, width in enumerate(col_widths):
        requests_body.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": ws.id,
                    "dimension": "COLUMNS",
                    "startIndex": i,
                    "endIndex": i + 1,
                },
                "properties": {"pixelSize": width},
                "fields": "pixelSize",
            }
        })
    if requests_body:
        spreadsheet.batch_update({"requests": requests_body})

    print(f"  ✅ 「{sheet_name}」を更新（{len(data['coupons'])}件）")


# ============================================================
# シート2: 変動ログ（差分を追記）
# ============================================================
def update_change_log_sheet(spreadsheet, data):
    """「変動ログ」シートに今日の変化を追記"""
    sheet_name = "変動ログ"

    try:
        ws = spreadsheet.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=8)
        # 初回はヘッダーを書き込み
        headers = ["日付", "種別", "クーポンID", "タイトル", "割引額", "変更内容", "詳細URL", "総数"]
        ws.update(range_name="A1", values=[headers])
        ws.format("A1:H1", {
            "backgroundColor": {"red": 0.85, "green": 0.92, "blue": 0.83},
            "textFormat": {"bold": True},
        })

    # 前日データを読み込んで差分を計算
    today_file = DATA_DIR / f"coupons_{today_str()}.json"
    if not today_file.exists():
        return

    # 過去データを探す
    files = sorted(DATA_DIR.glob("coupons_*.json"), reverse=True)
    prev_data = None
    today_name = f"coupons_{today_str()}.json"
    for f in files:
        if f.name != today_name:
            with open(f, "r", encoding="utf-8") as fh:
                prev_data = json.load(fh)
            break

    if prev_data is None:
        # 初回は全件を「初期登録」として記録
        new_rows = []
        for c in data["coupons"]:
            new_rows.append([
                today_str(),
                "🟢 初期登録",
                c.get("id", ""),
                c.get("title", ""),
                c.get("discount", ""),
                "",
                c.get("detail_url", ""),
                str(data["total_count"]),
            ])
        if new_rows:
            ws.append_rows(new_rows)
            print(f"  ✅ 「{sheet_name}」に初期データ {len(new_rows)}件を記録")
        return

    # 差分計算
    old_ids = {c["id"]: c for c in prev_data["coupons"]}
    new_ids = {c["id"]: c for c in data["coupons"]}

    new_rows = []

    # 新規追加
    for cid in set(new_ids.keys()) - set(old_ids.keys()):
        c = new_ids[cid]
        codes = ", ".join((c.get("detail_data") or {}).get("coupon_codes", []))
        new_rows.append([
            today_str(),
            "🆕 追加",
            c.get("id", ""),
            c.get("title", ""),
            c.get("discount", ""),
            f"コード: {codes}" if codes else "",
            c.get("detail_url", ""),
            str(data["total_count"]),
        ])

    # 削除/終了
    for cid in set(old_ids.keys()) - set(new_ids.keys()):
        c = old_ids[cid]
        new_rows.append([
            today_str(),
            "❌ 終了",
            c.get("id", ""),
            c.get("title", ""),
            c.get("discount", ""),
            "",
            c.get("detail_url", ""),
            str(data["total_count"]),
        ])

    # 内容変更
    for cid in set(old_ids.keys()) & set(new_ids.keys()):
        old_c = old_ids[cid]
        new_c = new_ids[cid]
        changes = []
        for field in ["title", "discount", "booking_period", "stay_period"]:
            if old_c.get(field) != new_c.get(field):
                changes.append(f"{field}: {old_c.get(field)} → {new_c.get(field)}")

        old_hash = (old_c.get("detail_data") or {}).get("raw_text_hash", "")
        new_hash = (new_c.get("detail_data") or {}).get("raw_text_hash", "")
        if old_hash and new_hash and old_hash != new_hash:
            changes.append("詳細ページ内容が変更")

        if changes:
            new_rows.append([
                today_str(),
                "✏️ 変更",
                new_c.get("id", ""),
                new_c.get("title", ""),
                new_c.get("discount", ""),
                " / ".join(changes),
                new_c.get("detail_url", ""),
                str(data["total_count"]),
            ])

    # 変化なしの日もログに記録
    if not new_rows:
        new_rows.append([
            today_str(),
            "─ 変化なし",
            "",
            "",
            "",
            f"全{data['total_count']}件に変更なし",
            "",
            str(data["total_count"]),
        ])

    ws.append_rows(new_rows)
    print(f"  ✅ 「{sheet_name}」に{len(new_rows)}件を追記")


# ============================================================
# メイン
# ============================================================
def main():
    data = load_today_data()
    if data is None:
        print("❌ データがないため終了します")
        sys.exit(1)

    print(f"📊 Google Sheets 書き出し開始 - {today_str()}")
    print(f"   クーポン数: {data['total_count']}件")

    client = get_gspread_client()
    spreadsheet = get_spreadsheet(client)
    print(f"   スプレッドシート: {spreadsheet.title}")

    update_coupon_list_sheet(spreadsheet, data)
    update_change_log_sheet(spreadsheet, data)

    print(f"\n✅ Google Sheets 更新完了!")
    print(f"   https://docs.google.com/spreadsheets/d/{os.environ.get('SPREADSHEET_ID')}")


if __name__ == "__main__":
    main()
