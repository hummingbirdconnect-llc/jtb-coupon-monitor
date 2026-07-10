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

QUEUE_HEADER = ["日付", "サイト", "ツリー", "投稿順", "パターン", "本文（セルをコピーして投稿）", "投稿済みメモ"]
PERF_HEADER = ["日付", "サイト", "ツリー", "パターン", "OTA", "タイトル",
               "インプレッション", "いいね", "リンククリック", "反映済み(自動)"]
STATS_HEADER = ["サイト", "パターン", "使用回数", "記録数", "平均スコア", "判定"]


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


def push_queue(sh, date_str):
    """投稿キューに当日分を先頭挿入（同日分が既にあればスキップ）。"""
    path, kind = source_md_for(date_str)
    if not path:
        print(f"⚠️ {date_str} の投稿ファイルがありません — 投稿キュー書き出しをスキップ")
        return
    ws = get_or_create_ws(sh, QUEUE_SHEET, QUEUE_HEADER)
    existing_dates = set(ws.col_values(1)[1:])
    if date_str in existing_dates:
        print(f"投稿キュー: {date_str} は書き出し済み — スキップ")
        return
    rows = []
    for th in parse_threads_md(path):
        for order, post in enumerate(th["posts"], 1):
            rows.append([date_str, SITE_DISPLAY[th["site"]], th["tree"], order,
                         th["pattern"], post, ""])
    if not rows:
        print("⚠️ 投稿の抽出に失敗（md構造を確認してください）")
        return
    ws.insert_rows(rows, row=2, value_input_option="RAW")
    print(f"投稿キュー: {date_str} の{len(rows)}投稿を書き出し（{kind}）")


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
    push_queue(sh, date_str)
    push_perf_rows(sh, date_str)
    sync_perf_back(sh)
    update_pattern_stats(sh)
    print(f"完了: {sh.url}")


if __name__ == "__main__":
    main()
