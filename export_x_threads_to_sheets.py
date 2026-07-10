#!/usr/bin/env python3
"""
X投稿ツリー × Googleスプレッドシート連携
================================================================
「X投稿管理」スプレッドシートに対して毎朝以下を行う:

1. 投稿キュー書き出し … 当日の投稿文（磨き版があれば磨き版・なければテンプレ版）を
   「投稿キュー」タブの先頭に挿入（1投稿=1行・本文セルをコピーして投稿する）
2. 実績入力の行追加 … 「実績入力」タブに1ツリー=1行を追加（インプレ列は空欄。
   ユーザーが数字を入れるだけでよい）
3. 実績の読み戻し … 「実績入力」でインプレが記入され「反映済み」が空の行を
   tweets_output/x_perf_log.json に同期し、パターン淘汰エンジンに反映
4. パターン成績の更新 … 「パターン成績」タブを最新集計で全面更新

初回セットアップ（1回だけ手作業）:
  1. https://sheets.new で新規スプレッドシートを作成し「X投稿管理」と命名
  2. 共有に claude-seo-analyst@claude-seo-analyst.iam.gserviceaccount.com を「編集者」で追加
  3. config/x_thread_sheets.json の spreadsheet_id にIDを記入

使い方:
  vault_blog/.venv/bin/python3 export_x_threads_to_sheets.py            # 今日の分
  vault_blog/.venv/bin/python3 export_x_threads_to_sheets.py --date 2026-07-10
================================================================
"""

import argparse
import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

from record_x_perf import aggregate_stats

JST = timezone(timedelta(hours=9))
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "tweets_output"
SHEETS_CONFIG = BASE_DIR / "config" / "x_thread_sheets.json"
PATTERNS_CONFIG = BASE_DIR / "config" / "x_thread_patterns.json"
USAGE_LOG = OUTPUT_DIR / "x_pattern_usage.json"
PERF_LOG = OUTPUT_DIR / "x_perf_log.json"

SITE_DISPLAY = {
    "yakushimafan": "屋久島ファン",
    "welltrip": "ウェルトリップ",
    "tripbooking": "トリップブッキング",
}
DISPLAY_TO_ID = {v: k for k, v in SITE_DISPLAY.items()}

QUEUE_SHEET = "投稿キュー"
PERF_SHEET = "実績入力"
STATS_SHEET = "パターン成績"
HOWTO_SHEET = "使い方"

QUEUE_HEADER = ["日付", "サイト", "ツリー", "投稿順", "パターン", "本文（セルをコピーして投稿）",
                "投稿済みメモ", "投稿状況"]
PERF_HEADER = ["日付", "サイト", "ツリー", "パターン", "OTA", "タイトル",
               "インプレッション", "いいね", "リンククリック", "反映済み(自動)"]
STATS_HEADER = ["サイト", "パターン", "使用回数", "記録数", "平均スコア", "判定"]

# サイト別背景色（薄・濃の2段でツリーの区切りを見分ける）
SITE_COLORS = {
    "屋久島ファン": ({"red": 0.910, "green": 0.961, "blue": 0.914},   # 薄緑
                {"red": 0.784, "green": 0.902, "blue": 0.788}),  # 濃緑
    "ウェルトリップ": ({"red": 0.890, "green": 0.949, "blue": 0.992},   # 薄青
                 {"red": 0.733, "green": 0.871, "blue": 0.984}),  # 濃青
    "トリップブッキング": ({"red": 1.0, "green": 0.953, "blue": 0.878},     # 薄橙
                  {"red": 1.0, "green": 0.878, "blue": 0.698}),   # 濃橙
}
GREY_TEXT = {"red": 0.62, "green": 0.62, "blue": 0.62}

HOWTO_ROWS = [
    ["X投稿管理シートの使い方"],
    [""],
    ["【毎朝の投稿手順】"],
    ["1.", "「投稿キュー」タブを開く（新しい日付が一番上に積み重なります）"],
    ["2.", "背景色が同じ行 = 同じツリー（3投稿で1セット）。色はサイト別: 緑=屋久島ファン / 青=ウェルトリップ / 橙=トリップブッキング。濃淡でツリーの区切りを表します"],
    ["3.", "投稿順1の本文セルをコピー → Xでそのまま投稿（リンクなしでOK）"],
    ["4.", "投稿順2を「1つ目の自分の投稿への返信」として投稿 → 投稿順3も同様に返信で続ける"],
    ["5.", "リンクは3投稿目だけ（1投稿目にリンクを貼るとXの仕様でインプレッションが大きく下がります）"],
    ["6.", "投稿し終えたら「投稿状況」をプルダウンで「完了」に → 行の文字がグレーになり、未投稿分と見分けられます"],
    [""],
    ["【実績の記録（任意・やると投稿が育ちます）】"],
    ["1.", "投稿から2〜3日後、Xのアナリティクスを見て「実績入力」タブのインプレッション列に数字を入れるだけ"],
    ["2.", "いいね・リンククリックは任意（空欄でOK）"],
    ["3.", "翌朝の自動同期で反映され、成績の良い投稿パターンが自動的に出やすくなります（反映済み列に日付が入ります）"],
    [""],
    ["【パターン成績タブの見方】"],
    ["・", "平均スコア = そのサイトのインプレッション中央値と比べた倍率（1.0が平均的）"],
    ["・", "⭐好調 = スコア1.3以上 ／ 🔴引退候補 = 記録3件以上でスコア0.6未満"],
    ["・", "引退させたいパターンが出たら、Claudeに「○○型を引退させて」と伝えれば設定を変更します"],
    [""],
    ["【こんなときは】"],
    ["・", "朝になってもシートに今日の分が無い → Macが起動していなかった日は同期が走りません。GitHubの tweets_output フォルダに毎朝のファイルは必ずあります"],
    ["・", "GitHub: https://github.com/hummingbirdconnect-llc/jtb-coupon-monitor/tree/main/tweets_output"],
    ["・", "文面の直し・パターン追加・その他の相談 → Claudeへ"],
]


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default
    return default


def connect():
    conf = load_json(SHEETS_CONFIG, {})
    sid = conf.get("spreadsheet_id", "")
    if not sid:
        raise SystemExit(
            "spreadsheet_id が未設定です。\n"
            "1) https://sheets.new で「X投稿管理」を作成\n"
            "2) claude-seo-analyst@claude-seo-analyst.iam.gserviceaccount.com を編集者で共有\n"
            f"3) {SHEETS_CONFIG} の spreadsheet_id にIDを記入"
        )
    creds_file = Path(conf.get("service_account_file", "~/.config/gcloud/claude-seo-analyst.json")).expanduser()
    creds = Credentials.from_service_account_file(
        str(creds_file), scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return gspread.authorize(creds).open_by_key(sid)


def get_or_create_ws(sh, title, header, cols=12):
    try:
        ws = sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=1000, cols=cols)
    first_row = ws.row_values(1)
    if first_row[: len(header)] != header:
        ws.update(range_name="A1", values=[header])
        ws.format("1:1", {"textFormat": {"bold": True}})
        ws.freeze(rows=1)
    return ws


def parse_threads_md(path):
    """テンプレ版/磨き版共通のmd構造から投稿を抽出する。"""
    text = path.read_text(encoding="utf-8")
    threads = []
    site_id = None
    current = None
    in_code = False
    code_lines = []
    for line in text.splitlines():
        m = re.match(r"^## (.+?)（", line)
        if m and m.group(1) in DISPLAY_TO_ID:
            site_id = DISPLAY_TO_ID[m.group(1)]
            continue
        m = re.match(r"^### ツリー(\d+):", line)
        if m and site_id:
            current = {"site": site_id, "tree": int(m.group(1)), "pattern": "-", "posts": []}
            threads.append(current)
            continue
        if current and "パターン:" in line and not in_code:
            current["pattern"] = line.split("パターン:")[-1].strip()
            continue
        if line.startswith("```"):
            if in_code and current is not None:
                current["posts"].append("\n".join(code_lines).strip())
            in_code = not in_code
            code_lines = []
            continue
        if in_code:
            code_lines.append(line)
    return threads


def source_md_for(date_str):
    """磨き版があれば磨き版、なければテンプレ版のmdを返す。"""
    polished = OUTPUT_DIR / f"x_threads_{date_str}_polished.md"
    plain = OUTPUT_DIR / f"x_threads_{date_str}.md"
    if polished.exists():
        return polished, "磨き版"
    if plain.exists():
        return plain, "テンプレ版"
    return None, None


def ensure_queue_layout(sh, ws):
    """投稿状況プルダウン・完了行グレー化の条件付き書式を冪等に整備する。"""
    sheet_id = ws.id
    status_col = len(QUEUE_HEADER) - 1  # H列（0始まり7）
    requests = []
    # 既存の条件付き書式を全削除（このタブは本スクリプト管理のため安全）
    meta = sh.fetch_sheet_metadata({"fields": "sheets(properties(sheetId),conditionalFormats)"})
    for s in meta.get("sheets", []):
        if s.get("properties", {}).get("sheetId") != sheet_id:
            continue
        for _ in s.get("conditionalFormats", []):
            requests.append({"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": 0}})
    # 投稿状況プルダウン（未投稿/完了）を H2:H に設定
    requests.append({"setDataValidation": {
        "range": {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 5000,
                  "startColumnIndex": status_col, "endColumnIndex": status_col + 1},
        "rule": {"condition": {"type": "ONE_OF_LIST",
                               "values": [{"userEnteredValue": "未投稿"},
                                          {"userEnteredValue": "完了"}]},
                 "showCustomUi": True, "strict": False},
    }})
    # 「完了」の行は文字をグレーにして未投稿分と見分ける
    requests.append({"addConditionalFormatRule": {"index": 0, "rule": {
        "ranges": [{"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": 5000,
                    "startColumnIndex": 0, "endColumnIndex": len(QUEUE_HEADER)}],
        "booleanRule": {
            "condition": {"type": "CUSTOM_FORMULA",
                          "values": [{"userEnteredValue": '=$H2="完了"'}]},
            "format": {"textFormat": {"foregroundColor": GREY_TEXT,
                                      "strikethrough": False}},
        },
    }}})
    sh.batch_update({"requests": requests})


def repaint_queue_colors(ws, limit=150):
    """投稿キュー上位N行に、サイト色×ツリー濃淡の背景色を塗り直す（冪等）。"""
    values = ws.get_all_values()[1: limit + 1]
    formats = []
    start = None
    prev_key = None
    prev_color = None
    for idx, row in enumerate(values, start=2):
        row = row + [""] * (4 - len(row))
        key = (row[0], row[1], row[2])  # 日付・サイト・ツリー
        pale, deep = SITE_COLORS.get(row[1], ({"red": 1, "green": 1, "blue": 1},) * 2)
        tree_no = int(row[2]) if str(row[2]).isdigit() else 0
        color = deep if tree_no % 2 == 0 else pale  # ツリー1=薄, 2=濃, 3=薄
        if key != prev_key:
            if prev_key is not None:
                formats.append({"range": f"A{start}:H{idx - 1}",
                                "format": {"backgroundColor": prev_color}})
            start, prev_key, prev_color = idx, key, color
    if prev_key is not None:
        formats.append({"range": f"A{start}:H{len(values) + 1}",
                        "format": {"backgroundColor": prev_color}})
    if formats:
        ws.batch_format(formats)


def repaint_perf_colors(ws, limit=100):
    """実績入力の各行にサイト色（薄色）を塗る（冪等）。"""
    values = ws.get_all_values()[1: limit + 1]
    formats = []
    for idx, row in enumerate(values, start=2):
        if len(row) < 2:
            continue
        pale, _deep = SITE_COLORS.get(row[1], (None, None))
        if pale:
            formats.append({"range": f"A{idx}:J{idx}", "format": {"backgroundColor": pale}})
    if formats:
        ws.batch_format(formats)


def ensure_howto_sheet(sh):
    """「使い方」タブを整備する（無ければ作成・あれば触らない）。"""
    try:
        sh.worksheet(HOWTO_SHEET)
        return
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=HOWTO_SHEET, rows=40, cols=4)
    ws.update(range_name="A1", values=HOWTO_ROWS, value_input_option="RAW")
    ws.format("A1", {"textFormat": {"bold": True, "fontSize": 14}})
    ws.format("A3:A30", {"textFormat": {"bold": True}})
    sh.batch_update({"requests": [{"updateDimensionProperties": {
        "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2},
        "properties": {"pixelSize": 640}, "fields": "pixelSize"}}]})
    print("「使い方」タブを作成しました")


def push_queue(sh, date_str):
    """投稿キューに当日分を先頭挿入（同日分が既にあればスキップ）。新しい日付が常に上。"""
    path, kind = source_md_for(date_str)
    ws = get_or_create_ws(sh, QUEUE_SHEET, QUEUE_HEADER)
    if not path:
        print(f"⚠️ {date_str} の投稿ファイルがありません — 投稿キュー書き出しをスキップ")
        return ws
    existing_dates = set(ws.col_values(1)[1:])
    if date_str in existing_dates:
        print(f"投稿キュー: {date_str} は書き出し済み — スキップ")
        return ws
    rows = []
    for th in parse_threads_md(path):
        for order, post in enumerate(th["posts"], 1):
            rows.append([date_str, SITE_DISPLAY[th["site"]], th["tree"], order,
                         th["pattern"], post, "", "未投稿"])
    if not rows:
        print("⚠️ 投稿の抽出に失敗（md構造を確認してください）")
        return ws
    ws.insert_rows(rows, row=2, value_input_option="RAW")
    print(f"投稿キュー: {date_str} の{len(rows)}投稿を書き出し（{kind}）")
    return ws


def fill_missing_status(ws):
    """投稿状況が空の既存行に「未投稿」を補完する（列追加の遡及対応）。"""
    values = ws.get_all_values()
    updates = []
    for i, row in enumerate(values[1:], start=2):
        row = row + [""] * (8 - len(row))
        if row[5].strip() and not row[7].strip():  # 本文あり・状況空
            updates.append({"range": f"H{i}", "values": [["未投稿"]]})
    if updates:
        ws.batch_update(updates, value_input_option="RAW")


def push_perf_rows(sh, date_str):
    """実績入力タブに1ツリー=1行を追加（インプレ列は空欄で待つ）。"""
    usage = load_json(USAGE_LOG, [])
    todays = [u for u in usage if u.get("date") == date_str]
    if not todays:
        print(f"⚠️ 使用ログに {date_str} がありません — 実績行の追加をスキップ")
        return
    ws = get_or_create_ws(sh, PERF_SHEET, PERF_HEADER)
    existing = {(r[0], r[1], r[2]) for r in ws.get_all_values()[1:] if len(r) >= 3}
    rows = []
    for u in sorted(todays, key=lambda x: (x["site"], x["tree"])):
        key = (date_str, SITE_DISPLAY.get(u["site"], u["site"]), str(u["tree"]))
        if key in existing:
            continue
        rows.append([date_str, SITE_DISPLAY.get(u["site"], u["site"]), u["tree"],
                     u.get("pattern", ""), u.get("ota", ""), u.get("title", ""),
                     "", "", "", ""])
    if rows:
        ws.insert_rows(rows, row=2, value_input_option="RAW")
        print(f"実績入力: {date_str} の{len(rows)}ツリー分の行を追加（インプレ列にご記入ください）")
    else:
        print(f"実績入力: {date_str} は追加済み — スキップ")


def _to_int(value):
    v = str(value).strip().replace(",", "").replace("，", "")
    v = v.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    return int(v) if v.isdigit() else None


def sync_perf_back(sh):
    """実績入力タブの記入済み・未反映行を x_perf_log.json に同期する。"""
    ws = get_or_create_ws(sh, PERF_SHEET, PERF_HEADER)
    values = ws.get_all_values()
    perf = load_json(PERF_LOG, [])
    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    synced = 0
    cell_updates = []
    for i, row in enumerate(values[1:], start=2):
        row = row + [""] * (10 - len(row))
        date_s, site_disp, tree_s, pattern, ota, title, imp_s, likes_s, clicks_s, done = row[:10]
        imp = _to_int(imp_s)
        if not imp or done.strip():
            continue
        site_id = DISPLAY_TO_ID.get(site_disp.strip(), site_disp.strip())
        tree = _to_int(tree_s) or 0
        perf = [p for p in perf
                if not (p.get("date") == date_s and p.get("site") == site_id
                        and p.get("tree") == tree)]
        perf.append({
            "date": date_s, "site": site_id, "tree": tree,
            "pattern": pattern or "unknown", "ota": ota, "title": title,
            "impressions": imp, "likes": _to_int(likes_s), "link_clicks": _to_int(clicks_s),
            "recorded_at": today_str,
        })
        cell_updates.append({"range": f"J{i}", "values": [[today_str]]})
        synced += 1
    if synced:
        PERF_LOG.write_text(json.dumps(perf, ensure_ascii=False, indent=1), encoding="utf-8")
        ws.batch_update(cell_updates, value_input_option="RAW")
        print(f"実績読み戻し: {synced}件を淘汰エンジンに反映（次回生成から選択率に影響）")
    else:
        print("実績読み戻し: 新しい記入はありません")


def update_pattern_stats(sh):
    """パターン成績タブを最新集計で全面更新する。"""
    perf = load_json(PERF_LOG, [])
    usage = load_json(USAGE_LOG, [])
    patterns_conf = load_json(PATTERNS_CONFIG, {}).get("patterns", [])
    ws = get_or_create_ws(sh, STATS_SHEET, STATS_HEADER, cols=8)
    if not perf:
        print("パターン成績: 実績記録がまだないため未更新")
        return
    stats, retire = aggregate_stats(perf, usage, patterns_conf)
    rows = []
    for site, s in stats.items():
        for _pat, name, used, n, score, verdict in s["rows"]:
            rows.append([SITE_DISPLAY.get(site, site), name, used, n, round(score, 2), verdict])
    ws.batch_clear(["A2:F1000"])
    if rows:
        ws.update(range_name="A2", values=rows, value_input_option="RAW")
    print(f"パターン成績: {len(rows)}行を更新" + (f"（🔴引退候補 {len(retire)}件あり）" if retire else ""))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="YYYY-MM-DD（省略時は今日JST）")
    args = parser.parse_args()
    date_str = args.date or datetime.now(JST).strftime("%Y-%m-%d")

    sh = connect()
    ws_queue = push_queue(sh, date_str)
    ensure_queue_layout(sh, ws_queue)
    fill_missing_status(ws_queue)
    repaint_queue_colors(ws_queue)
    push_perf_rows(sh, date_str)
    sync_perf_back(sh)
    repaint_perf_colors(get_or_create_ws(sh, PERF_SHEET, PERF_HEADER))
    update_pattern_stats(sh)
    ensure_howto_sheet(sh)
    print(f"完了: {sh.url}")


if __name__ == "__main__":
    main()
