#!/usr/bin/env python3
"""
JTB クーポンデータを Google Sheets に書き出すスクリプト（マスター台帳対応版）
==========================================================================
3つのシートを管理:
  - 「現在のクーポン」: 今取得可能なクーポン（配布中＋復活のみ）
  - 「マスター台帳」: 全クーポンの最新ステータス（配布終了・消滅含む）
  - 「変動ログ」: 追加・終了・復活・変更の時系列ログ

必要な環境変数:
  GOOGLE_SERVICE_ACCOUNT_JSON: サービスアカウントのJSONキー
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
MASTER_FILE = DATA_DIR / "master_coupons.json"
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def get_gspread_client():
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not sa_json:
        print("❌ 環境変数 GOOGLE_SERVICE_ACCOUNT_JSON が設定されていません")
        sys.exit(1)
    sa_info = json.loads(sa_json)
    creds = Credentials.from_service_account_info(sa_info, scopes=SCOPES)
    return gspread.authorize(creds)


def get_spreadsheet(client):
    spreadsheet_id = os.environ.get("SPREADSHEET_ID")
    if not spreadsheet_id:
        print("❌ 環境変数 SPREADSHEET_ID が設定されていません")
        sys.exit(1)
    return client.open_by_key(spreadsheet_id)


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def load_master():
    if MASTER_FILE.exists():
        with open(MASTER_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def get_or_create_sheet(spreadsheet, name, rows=500, cols=20):
    try:
        return spreadsheet.worksheet(name)
    except gspread.exceptions.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=name, rows=rows, cols=cols)


def set_col_widths(spreadsheet, ws, widths):
    requests_body = []
    for i, width in enumerate(widths):
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


# ============================================================
# シート1: 現在のクーポン（配布中＋復活のみ）
# ============================================================
def update_available_sheet(spreadsheet, master):
    ws = get_or_create_sheet(spreadsheet, "現在のクーポン")

    headers = [
        "更新日時", "カテゴリ", "ID", "詳細URL", "タイトル", "割引額",
        "エリア", "タイプ", "予約対象期間", "宿泊/出発対象期間", "店舗利用",
        "クーポンコード", "パスワード", "条件", "注意事項", "クーポンアフィリエイトリンク",
    ]

    mc = master.get("coupons", {})
    available = [c for c in mc.values() if c["status"] in ["🟢 配布中", "🔄 復活"]]
    available.sort(key=lambda x: (x.get("category", ""), x.get("area", "")))

    rows = [headers]
    for c in available:
        detail = c.get("detail_data") or {}
        rows.append([
            c.get("last_updated", ""),
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

    set_col_widths(spreadsheet, ws, [90, 60, 130, 300, 400, 110, 70, 120, 200, 200, 60, 150, 100, 200, 200, 250])

    print(f"  ✅ 「現在のクーポン」を更新（{len(available)}件）")


# ============================================================
# シート2: マスター台帳（全件）
# ============================================================
def update_master_sheet(spreadsheet, master):
    ws = get_or_create_sheet(spreadsheet, "マスター台帳")

    headers = [
        "ステータス", "更新日時", "カテゴリ", "ID", "詳細URL", "タイトル", "割引額",
        "エリア", "タイプ", "予約対象期間", "宿泊/出発対象期間", "店舗利用",
        "クーポンコード", "パスワード", "条件", "注意事項",
        "クーポンアフィリエイトリンク", "初回検出日", "最終確認日", "ステータス履歴",
    ]

    mc = master.get("coupons", {})
    status_order = {"🟢 配布中": 0, "🔄 復活": 1, "🔴 配布終了": 2, "⚫ ページ消滅": 3}
    all_coupons = sorted(
        mc.values(),
        key=lambda x: (status_order.get(x.get("status", ""), 9), x.get("category", ""), x.get("area", ""))
    )

    rows = [headers]
    for c in all_coupons:
        detail = c.get("detail_data") or {}
        history = c.get("status_history", [])
        history_str = " → ".join([f"{h['date']}:{h['to']}" for h in history[-5:]])

        rows.append([
            c.get("status", ""),
            c.get("last_updated", ""),
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
            c.get("first_seen", ""),
            c.get("last_seen", ""),
            history_str,
        ])

    ws.clear()
    ws.update(range_name="A1", values=rows)

    # ヘッダー書式（20列: A〜T）
    ws.format("A1:T1", {
        "backgroundColor": {"red": 0.2, "green": 0.4, "blue": 0.7},
        "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
    })

    # ステータス別に行の背景色を設定
    batch_requests = []
    for i, c in enumerate(all_coupons, start=2):  # 2行目から（1行目はヘッダー）
        status = c.get("status", "")
        if status == "🔴 配布終了":
            color = {"red": 1, "green": 0.9, "blue": 0.9}  # 薄い赤
        elif status == "⚫ ページ消滅":
            color = {"red": 0.85, "green": 0.85, "blue": 0.85}  # グレー
        elif status == "🔄 復活":
            color = {"red": 0.85, "green": 0.93, "blue": 1}  # 薄い青
        else:
            continue  # 配布中はデフォルト（白）

        batch_requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": ws.id,
                    "startRowIndex": i - 1,
                    "endRowIndex": i,
                    "startColumnIndex": 0,
                    "endColumnIndex": 20,
                },
                "cell": {"userEnteredFormat": {"backgroundColor": color}},
                "fields": "userEnteredFormat.backgroundColor",
            }
        })

    if batch_requests:
        spreadsheet.batch_update({"requests": batch_requests})

    set_col_widths(spreadsheet, ws, [90, 90, 60, 130, 300, 400, 110, 70, 120, 200, 200, 60, 150, 100, 200, 200, 250, 90, 90, 300])

    print(f"  ✅ 「マスター台帳」を更新（全{len(all_coupons)}件）")


# ============================================================
# シート3: 変動ログ（追記）
# ============================================================
def update_change_log_sheet(spreadsheet, master):
    ws = get_or_create_sheet(spreadsheet, "変動ログ", rows=2000)

    # ヘッダーが空なら初回
    existing = ws.get_all_values()
    if not existing:
        headers = ["日付", "カテゴリ", "種別", "クーポンID", "タイトル", "割引額",
                   "変更内容", "詳細URL", "配布中", "配布終了", "消滅", "全件数"]
        ws.update(range_name="A1", values=[headers])
        ws.format("A1:L1", {
            "backgroundColor": {"red": 0.85, "green": 0.92, "blue": 0.83},
            "textFormat": {"bold": True},
        })

    # 今日の変動ログファイルを読む
    report_file = DATA_DIR / "reports" / f"report_{today_str()}.txt"
    if not report_file.exists():
        print("  ⚠️ 今日のレポートがないためログ追記をスキップ")
        return

    mc = master.get("coupons", {})
    active_count = sum(1 for c in mc.values() if c["status"] in ["🟢 配布中", "🔄 復活"])
    ended_count = sum(1 for c in mc.values() if c["status"] == "🔴 配布終了")
    gone_count = sum(1 for c in mc.values() if c["status"] == "⚫ ページ消滅")

    # 今日更新されたクーポンを変動ログに追記
    new_rows = []
    for cid, c in mc.items():
        if c.get("last_updated") != today_str():
            continue
        history = c.get("status_history", [])
        today_events = [h for h in history if h.get("date") == today_str()]

        for ev in today_events:
            event_type = ""
            if not ev.get("from"):
                event_type = "🆕 新規"
            elif "配布中" in ev.get("to", "") and ("終了" in ev.get("from", "") or "消滅" in ev.get("from", "")):
                event_type = "🔄 復活"
            elif "配布終了" in ev.get("to", ""):
                event_type = "🔴 配布終了"
            elif "消滅" in ev.get("to", ""):
                event_type = "⚫ ページ消滅"
            else:
                event_type = "✏️ ステータス変更"

            detail = c.get("detail_data") or {}
            codes = ", ".join(detail.get("coupon_codes", []))

            new_rows.append([
                today_str(),
                c.get("category", ""),
                event_type,
                cid,
                c.get("title", ""),
                c.get("discount", ""),
                f"{ev.get('from', '(なし)')} → {ev.get('to', '')}" + (f" コード:{codes}" if codes else ""),
                c.get("detail_url", ""),
                str(active_count),
                str(ended_count),
                str(gone_count),
                str(len(mc)),
            ])

    if not new_rows:
        new_rows.append([
            today_str(), "", "─ 変化なし", "", "", "",
            f"配布中:{active_count} 終了:{ended_count} 消滅:{gone_count}",
            "", str(active_count), str(ended_count), str(gone_count), str(len(mc)),
        ])

    ws.append_rows(new_rows)
    print(f"  ✅ 「変動ログ」に{len(new_rows)}件を追記")


# ============================================================
# メイン
# ============================================================
def main():
    master = load_master()
    if master is None:
        print("❌ マスター台帳がないため終了します")
        sys.exit(1)

    mc = master.get("coupons", {})
    active = sum(1 for c in mc.values() if c["status"] in ["🟢 配布中", "🔄 復活"])
    ended = sum(1 for c in mc.values() if c["status"] == "🔴 配布終了")

    print(f"📊 Google Sheets 書き出し開始 - {today_str()}")
    print(f"   マスター台帳: 全{len(mc)}件（配布中:{active} 終了:{ended}）")

    client = get_gspread_client()
    spreadsheet = get_spreadsheet(client)
    print(f"   スプレッドシート: {spreadsheet.title}")

    update_available_sheet(spreadsheet, master)
    update_master_sheet(spreadsheet, master)
    update_change_log_sheet(spreadsheet, master)

    print(f"\n✅ Google Sheets 更新完了!")
    print(f"   https://docs.google.com/spreadsheets/d/{os.environ.get('SPREADSHEET_ID')}")


if __name__ == "__main__":
    main()
