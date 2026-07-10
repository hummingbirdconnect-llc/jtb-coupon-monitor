#!/usr/bin/env python3
"""
デイリーX投稿ツリー生成スクリプト
================================================================
dashboard/index.html の const DATA（全OTA正規化済みクーポンデータ）から、
サイト別（屋久島ファン / ウェルトリップ / トリップブッキング）に
重要度トップ3クーポンを選定し、各クーポン1ツリー（3投稿）の
X投稿文面を生成する。

ツリー構成（Xアルゴリズム対応）:
  1投稿目: フック（数字先出し・リンクなし）
  2投稿目: 条件詳細（✅箇条書き・リンクなし）
  3投稿目: 自社クーポン記事リンク + ハッシュタグ

出力:
  tweets_output/x_threads_YYYY-MM-DD.md   (コピペ用)
  tweets_output/x_threads_YYYY-MM-DD.json (構造化データ)

使い方:
  python generate_x_threads.py                 # ローカルの dashboard/index.html を使用
  python generate_x_threads.py --source remote # GitHub Pages から最新を取得
  python generate_x_threads.py --date 2026-07-10
================================================================
"""

import argparse
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

JST = timezone(timedelta(hours=9))
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "tweets_output"
DASHBOARD_HTML = BASE_DIR / "dashboard" / "index.html"
REMOTE_DASHBOARD_URL = "https://hummingbirdconnect-llc.github.io/jtb-coupon-monitor/dashboard/"
SITES_CONFIG = BASE_DIR / "config" / "x_thread_sites.json"
PROVIDER_REGISTRY = BASE_DIR / "config" / "provider_registry.json"

TOP_N = 3
MAX_PER_OTA = 2  # トップ3内の同一OTA上限

# X の weighted 文字数上限（全角2/半角1/URL23換算で280）。安全側に270で切る
MAX_WEIGHT = 270

# ハッシュタグ用のOTA名変換（X で使えない記号を除去・通称化）
HASHTAG_MAP = {
    "近畿日本ツーリスト": "近畿日本ツーリスト",
    "Yahoo!トラベル": "ヤフートラベル",
    "Trip.com": "Tripcom",
    "Hotels.com": "ホテルズドットコム",
    "Booking.com": "ブッキングドットコム",
    "Agoda": "アゴダ",
    "Expedia": "エクスペディア",
    "ゆこゆこネット": "ゆこゆこ",
    "JAMJAMライナー": "ジャムジャムライナー",
    "JR東海ツアーズ": "JR東海ツアーズ",
}

# サイト別トーン定義
TONES = {
    "jitsuyou": {  # 屋久島ファン: 実用・数字・直球
        "hook_close": "詳しい条件はこの下に👇",
        "post2_lead": "使える条件はここだけ見ればOKです。",
        "post3_lead": "取得の手順と使い方はここにまとめています",
        "post3_close": "予約前にチェックしてみてください。",
        "domestic_note": "屋久島旅行の予約にも使えます。",
    },
    "teian": {  # ウェルトリップ: 温かい提案型
        "hook_close": "使える条件をまとめました👇",
        "post2_lead": "予約前に見ておきたい条件はこちらです。",
        "post3_lead": "取得方法と使い方の手順はこちらに",
        "post3_close": "旅の予定がある方はのぞいてみてください。",
        "domestic_note": "",
    },
    "kyakkan": {  # トリップブッキング: 客観・データ
        "hook_close": "条件と注意点は次の投稿で👇",
        "post2_lead": "適用条件は以下のとおりです。",
        "post3_lead": "コードの使い方と最新一覧はこちら",
        "post3_close": "主要OTAのクーポンを毎日確認しています。",
        "domestic_note": "",
    },
}

PREF_PATTERN = re.compile(
    r"(北海道|青森|岩手|宮城|秋田|山形|福島|茨城|栃木|群馬|埼玉|千葉|東京|神奈川"
    r"|新潟|富山|石川|福井|山梨|長野|岐阜|静岡|愛知|三重|滋賀|京都|大阪|兵庫|奈良"
    r"|和歌山|鳥取|島根|岡山|広島|山口|徳島|香川|愛媛|高知|福岡|佐賀|長崎|熊本"
    r"|大分|宮崎|東北|関東|甲信越|北陸|東海|関西|近畿|中国地方|四国)"
)
# domestic_focus サイト（屋久島文脈）で許容する地域語
DOMESTIC_OK_PATTERN = re.compile(r"(全国|鹿児島|九州|沖縄|離島|屋久島)")
# 会員ランク限定の判定
RANK_PATTERN = re.compile(r"(ゴールド|プラチナ|シルバー|ダイヤモンド|エリート|VIP|上級会員|Genius)", re.IGNORECASE)
# データ由来の内部管理名（記事ファイル名など）の判定
INTERNAL_NAME_PATTERN = re.compile(r"(_SWELL|_HTML|_\d{2,}_|SWELL_|ワイヤー|下書き)")


def clean_target(row):
    """対象表示用テキスト。内部管理名を除外し、【対象外】以降を切る。"""
    for key in ("対象商品", "カテゴリ"):
        raw = str(row.get(key, "")).strip()
        if not raw or INTERNAL_NAME_PATTERN.search(raw):
            continue
        raw = re.split(r"【対象外】|※対象外|（対象外", raw)[0].strip()
        if raw:
            return raw
    return ""


def weighted_len(text):
    """X の weighted 文字数（全角2・半角1・URLは23固定）を概算する。"""
    total = 0
    for part in re.split(r"(https?://\S+)", text):
        if part.startswith("http"):
            total += 23
            continue
        for ch in part:
            total += 1 if ord(ch) < 0x2000 else 2
    return total


def truncate_jp(text, limit):
    """表示文字数で切り詰め（超過時は末尾…）。"""
    text = re.sub(r"\s+", " ", str(text)).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def load_dashboard_data(source):
    """dashboard の const DATA を読み込む。"""
    if source == "remote":
        req = urllib.request.Request(REMOTE_DASHBOARD_URL, headers={"User-Agent": "x-threads-gen"})
        with urllib.request.urlopen(req, timeout=30) as res:
            html = res.read().decode("utf-8")
    else:
        html = DASHBOARD_HTML.read_text(encoding="utf-8")
    m = re.search(r"const DATA = (\{.*?\});", html, re.DOTALL)
    if not m:
        raise RuntimeError("dashboard HTML から const DATA を抽出できませんでした")
    return json.loads(m.group(1))


def load_change_map(target_date):
    """全プロバイダの change_log から当日/前日の新規・再開をタイトル正規化キーで辞書化。"""
    try:
        providers = json.loads(PROVIDER_REGISTRY.read_text(encoding="utf-8"))["providers"]
    except (OSError, json.JSONDecodeError, KeyError):
        return {}
    prev_date = (datetime.strptime(target_date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    change_map = {}
    for p in providers:
        log_path = BASE_DIR / p.get("data_dir", "") / "change_log.json"
        if not log_path.exists():
            continue
        try:
            entries = json.loads(log_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for e in entries:
            etype = e.get("type", "")
            edate = e.get("date", "")
            if edate not in (target_date, prev_date):
                continue
            if "新規" not in etype and "再開" not in etype:
                continue
            key = re.sub(r"\s+", "", str(e.get("title", "")))
            if not key:
                continue
            kind = "new" if "新規" in etype else "restart"
            is_today = edate == target_date
            # 当日を前日より優先して記録
            if key not in change_map or is_today:
                change_map[key] = {"kind": kind, "today": is_today}
    return change_map


def parse_discount(row):
    """割引額(円)・割引率(%)・「最大」フラグ・表示ラベルを抽出する。"""
    fields = [str(row.get(k, "")) for k in ("割引額", "タイトル", "条件")]
    text = " ".join(fields).replace("￥", "¥")
    # 「¥1,000引き」表記を「1,000円引き」に正規化してから抽出
    text = re.sub(r"¥([\d,]+)", r"\1円", text)
    yen_hits = []
    for m in re.finditer(r"([\d,]+)\s*円\s*(引き?|OFF|オフ|割引|クーポン|助成)", text):
        yen_hits.append((int(m.group(1).replace(",", "")), m.start()))
    pct_hits = []
    for m in re.finditer(r"(\d{1,2})\s*[%％]\s*(OFF|オフ|引き?|割引)?", text):
        pct_hits.append((int(m.group(1)), m.start()))
    yen = max((v for v, _ in yen_hits), default=0)
    pct = max((v for v, _ in pct_hits), default=0)
    is_max = False
    for value, pos in yen_hits + pct_hits:
        window = text[max(0, pos - 10): pos]
        if "最大" in window:
            is_max = True
    # 表示ラベル: 割引額フィールド優先、なければ抽出値から組み立て
    label = str(row.get("割引額", "")).strip().replace("￥", "¥")
    label = re.sub(r"¥([\d,]+)", r"\1円", label)
    if not label:
        if yen:
            label = f"{'最大' if is_max else ''}{yen:,}円引き"
        elif pct:
            label = f"{'最大' if is_max else ''}{pct}%OFF"
        else:
            label = "割引あり（金額は公式で確認）"
    return yen, pct, is_max, label


def parse_deadline(row, today):
    """予約期間の終了日を抽出して残日数を返す。(days_left, 表示用文字列)"""
    period = str(row.get("予約期間", "")).strip()
    if not period:
        return None, ""
    dates = re.findall(r"(\d{4})[年/](\d{1,2})[月/](\d{1,2})", period)
    if not dates:
        return None, period[:20]
    y, mo, d = (int(x) for x in dates[-1])
    try:
        end = datetime(y, mo, d, tzinfo=JST)
    except ValueError:
        return None, period[:20]
    days_left = (end.date() - today.date()).days
    disp = f"{mo}/{d}まで"
    if "なくなり次第" in period:
        disp += "（なくなり次第終了）"
    return days_left, disp


def detect_caution(row):
    """2投稿目の⚠️注意点をデータから抽出する。"""
    text = " ".join(str(row.get(k, "")) for k in ("タイトル", "対象商品", "条件", "予約期間"))
    if RANK_PATTERN.search(text):
        return "会員ランク特典のため対象ランクの確認が必要です"
    if re.search(r"Mastercard|マスターカード|Visa|JCB|アメックス|AMEX|エポス|セゾン|カード", text, re.IGNORECASE):
        return "対象カードでの決済が条件です"
    m = re.search(r"([\d,]+)\s*円以上", text)
    if m:
        return f"{m.group(1)}円以上の予約が条件です"
    if "先着" in text:
        return "先着制のため早めの確認がおすすめです"
    if "なくなり次第" in text:
        return "なくなり次第終了です"
    if re.search(r"初回|初めて|新規会員", text):
        return "初めて利用する人限定です"
    if "アプリ" in text:
        return "アプリからの予約が条件です"
    if re.search(r"会員限定|要ログイン", text):
        return "会員登録（無料）が必要です"
    if "対象外" in text:
        return "対象外の商品があるので予約前に確認してください"
    return "配布状況は変わることがあるので公式ページで最新を確認してください"


def is_overseas_only(row):
    text = str(row.get("タイトル", "")) + str(row.get("カテゴリ", ""))
    return "海外" in text and "国内" not in text


def is_single_facility(row):
    """個別施設限定クーポン（ゆこゆこ型）の判定。"""
    title = str(row.get("タイトル", ""))
    if re.search(r"全国|エリア|地域|各地", title):
        return False
    return bool(re.search(r"(ホテル|旅館|リゾート|荘|亭|苑|閣)", title))


def score_coupon(row, site_conf, change_map, today):
    """重要度スコアを算出。(score, reasons[])"""
    score = 0
    reasons = []
    title_key = re.sub(r"\s+", "", str(row.get("タイトル", "")))
    change = change_map.get(title_key)
    if change:
        if change["today"] and change["kind"] == "new":
            score += 30
            reasons.append("本日新規")
        elif change["today"]:
            score += 25
            reasons.append("本日配布再開")
        else:
            score += 15
            reasons.append("昨日新着")
    yen, pct, is_max, _ = parse_discount(row)
    if yen >= 20000:
        score += 30
    elif yen >= 10000:
        score += 26
    elif yen >= 5000:
        score += 22
    elif yen >= 3000:
        score += 18
    elif yen >= 1000:
        score += 14
    elif yen > 0:
        score += 10
    elif pct >= 15:
        score += 24
    elif pct >= 10:
        score += 20
    elif pct >= 8:
        score += 16
    elif pct >= 5:
        score += 12
    elif pct > 0:
        score += 8
    else:
        score += 4
    if yen or pct:
        reasons.append(f"割引{yen:,}円" if yen else f"割引{pct}%")
    days_left, _ = parse_deadline(row, today)
    if days_left is not None:
        if 1 < days_left <= 3:
            score += 15
            reasons.append(f"残り{days_left}日")
        elif days_left <= 7:
            score += 12
            reasons.append(f"残り{days_left}日")
        elif days_left <= 14:
            score += 6
        elif days_left in (0, 1):
            score += 8
            reasons.append("本日/明日まで")
    text_all = str(row.get("タイトル", "")) + str(row.get("対象商品", "")) + str(row.get("カテゴリ", ""))
    if "全国" in text_all:
        score += 8
        reasons.append("全国対象")
    if is_single_facility(row):
        score -= 10
    if RANK_PATTERN.search(text_all):
        score -= 8  # 会員ランク限定は読者が使えないことが多い
    if str(row.get("クーポンコード", "")).strip():
        score += 3
    if site_conf.get("domestic_focus"):
        area_text = text_all + str(row.get("エリア", ""))
        if is_overseas_only(row):
            score -= 15
        elif PREF_PATTERN.search(area_text) and not DOMESTIC_OK_PATTERN.search(area_text):
            score -= 20  # 屋久島圏外の地域限定は読者が使えない
            reasons.append("地域限定")
    return score, reasons


def select_top(coupons_scored, top_n=TOP_N, max_per_ota=MAX_PER_OTA):
    """スコア順にOTA多様性を保ちながらトップNを選ぶ。"""
    selected = []
    ota_count = {}
    for item in sorted(coupons_scored, key=lambda x: x["score"], reverse=True):
        ota = item["provider_id"]
        if ota_count.get(ota, 0) >= max_per_ota:
            continue
        selected.append(item)
        ota_count[ota] = ota_count.get(ota, 0) + 1
        if len(selected) >= top_n:
            break
    return selected


def hashtag_for(label):
    name = HASHTAG_MAP.get(label, label)
    return re.sub(r"[!！.。・\s]", "", name)


def build_hook_line(item, today):
    """1投稿目のフック文（数字・限定性先出し）。"""
    row = item["row"]
    yen, pct, is_max, label = parse_discount(row)
    reasons = item["reasons"]
    if any("新規" in r for r in reasons):
        prefix = "新しいクーポンが出ました。"
    elif any("再開" in r for r in reasons):
        prefix = "終了していたクーポンが復活しています。"
    else:
        prefix = ""
    if yen >= 1000:
        core = f"{'最大' if is_max else ''}{yen:,}円引きクーポンが配布中です"
    elif pct:
        core = f"{'最大' if is_max else ''}{pct}%OFFクーポンが配布中です"
    else:
        core = f"{truncate_jp(label, 20)}のクーポンが配布中です"
    return prefix + core


def build_thread(item, site_conf, today, trusted_code_providers=()):
    """1クーポン分のツリー3投稿を生成する。"""
    row = item["row"]
    tone = TONES[site_conf["tone"]]
    ota_label = item["provider_label"]
    yen, pct, is_max, discount_label = parse_discount(row)
    days_left, deadline_disp = parse_deadline(row, today)
    target = truncate_jp(clean_target(row), 30)
    code = str(row.get("クーポンコード", "")).strip()
    caution = detect_caution(row)
    article_url = site_conf["article_map"][item["provider_id"]]

    # --- 1投稿目: フック（リンクなし） ---
    hook = build_hook_line(item, today)
    sub_lines = []
    if target:
        sub_lines.append(f"対象は{target}。")
    else:
        # 対象が取れない場合はタイトルを対象説明に転用（例: 「アプリ初回」「Mastercard」）
        title_as_target = truncate_jp(row.get("タイトル", ""), 26)
        if title_as_target:
            sub_lines.append(f"「{title_as_target}」向けのクーポンです。")
    if days_left is not None and 0 <= days_left <= 7:
        sub_lines.append(f"予約は{deadline_disp.split('（')[0]}、急ぎめです。")
    elif deadline_disp:
        sub_lines.append(f"予約期限は{deadline_disp.split('（')[0]}。")
    post1 = f"【{ota_label}】{hook}\n\n" + "\n".join(sub_lines) + f"\n\n{tone['hook_close']}\n(1/3)"

    # --- 2投稿目: 条件詳細（リンクなし） ---
    lines = [f"✅割引: {truncate_jp(discount_label, 24)}"]
    if target:
        lines.append(f"✅対象: {target}")
    lines.append(f"✅予約期限: {deadline_disp if deadline_disp else '公式ページに記載'}")
    # コードは切り詰めなしと確認済みのプロバイダ＆英数字形式のみ表示
    # （JTB等はデータ取得時に短縮される疑いがあり、誤コード拡散を防ぐため記事側へ誘導）
    if (
        code
        and item["provider_id"] in trusted_code_providers
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9\-_]{2,23}", code)
    ):
        lines.append(f"✅コード: {code}")
    post2 = f"{tone['post2_lead']}\n\n" + "\n".join(lines) + f"\n\n⚠️{caution}\n(2/3)"

    # --- 3投稿目: 自社記事リンク + CTA ---
    domestic_note = tone.get("domestic_note", "") if not is_overseas_only(row) else ""
    closing = (domestic_note + tone["post3_close"]).strip()
    tags = f"#旅行クーポン #{hashtag_for(ota_label)}"
    post3 = f"{tone['post3_lead']}\n→ {article_url}\n\n{closing}\n\n{tags}\n(3/3)"

    posts = []
    for text in (post1, post2, post3):
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        # 超過時はタイトル・対象を段階的に削る前提の簡易ガード（対象行を短縮）
        if weighted_len(text) > MAX_WEIGHT:
            text = text.replace(target, truncate_jp(target, 18)) if target else text
        posts.append({"text": text, "weight": weighted_len(text), "chars": len(text)})
    return posts


def collect_site_coupons(data, site_id, site_conf, change_map, today):
    """サイト対象×記事マッピング済みOTAの配布中クーポンを収集しスコアリング。"""
    scored = []
    for p in data.get("providers", []):
        pid = p.get("id", "")
        if site_id not in p.get("site_targets", []):
            continue
        if pid not in site_conf["article_map"]:
            continue
        for row in p.get("rows", []):
            if row.get("配布状況") != "配布中":
                continue
            score, reasons = score_coupon(row, site_conf, change_map, today)
            scored.append({
                "provider_id": pid,
                "provider_label": p.get("label", pid),
                "row": row,
                "score": score,
                "reasons": reasons,
            })
    return scored


def render_markdown(results, date_str, generated_at):
    out = [f"# デイリーX投稿ツリー {date_str}", ""]
    out.append(f"> データ元: クーポン監視ダッシュボード（{generated_at}生成） / 各ツリーは上から順にリプライで繋げて投稿")
    out.append("> 1投稿目にリンクを入れない・リンクは3投稿目のみ（Xのアルゴリズム対応）")
    out.append("")
    for site in results:
        out.append(f"## {site['display_name']}（{site['x_account']}）")
        out.append("")
        if not site["threads"]:
            out.append("（本日は配布中クーポンの選定候補がありませんでした）")
            out.append("")
            continue
        for i, th in enumerate(site["threads"], 1):
            reasons = "・".join(th["reasons"]) if th["reasons"] else "定番"
            out.append(f"### ツリー{i}: 【{th['provider_label']}】{th['title']}")
            out.append(f"重要度スコア: {th['score']}（{reasons}）")
            out.append("")
            for j, post in enumerate(th["posts"], 1):
                out.append(f"**{j}投稿目**（weight {post['weight']}/280）")
                out.append("```")
                out.append(post["text"])
                out.append("```")
            out.append("")
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=["local", "remote"], default="local")
    parser.add_argument("--date", help="YYYY-MM-DD（省略時は今日JST）")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    today = datetime.now(JST)
    if args.date:
        today = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=JST)
    date_str = today.strftime("%Y-%m-%d")

    config = json.loads(SITES_CONFIG.read_text(encoding="utf-8"))
    sites_conf = config["sites"]
    trusted_code_providers = tuple(config.get("trusted_code_providers", []))
    try:
        data = load_dashboard_data(args.source)
    except (OSError, RuntimeError) as e:
        if args.source == "local":
            print(f"ローカル読込失敗（{e}）→ remote にフォールバック")
            data = load_dashboard_data("remote")
        else:
            raise
    change_map = load_change_map(date_str)
    generated_at = data.get("generated_at", "不明")

    results = []
    for site_id, site_conf in sites_conf.items():
        scored = collect_site_coupons(data, site_id, site_conf, change_map, today)
        top = select_top(scored)
        threads = []
        for item in top:
            posts = build_thread(item, site_conf, today, trusted_code_providers)
            threads.append({
                "provider_id": item["provider_id"],
                "provider_label": item["provider_label"],
                "title": str(item["row"].get("タイトル", ""))[:60],
                "score": item["score"],
                "reasons": item["reasons"],
                "article_url": site_conf["article_map"][item["provider_id"]],
                "posts": posts,
            })
        results.append({
            "site_id": site_id,
            "display_name": site_conf["display_name"],
            "x_account": site_conf["x_account"],
            "candidates": len(scored),
            "threads": threads,
        })
        print(f"[{site_conf['display_name']}] 候補{len(scored)}件 → ツリー{len(threads)}本")

    md = render_markdown(results, date_str, generated_at)
    if args.dry_run:
        print(md)
        return

    OUTPUT_DIR.mkdir(exist_ok=True)
    md_path = OUTPUT_DIR / f"x_threads_{date_str}.md"
    json_path = OUTPUT_DIR / f"x_threads_{date_str}.json"
    md_path.write_text(md, encoding="utf-8")
    json_path.write_text(
        json.dumps({"date": date_str, "generated_at": generated_at, "sites": results},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"保存: {md_path}")
    print(f"保存: {json_path}")


if __name__ == "__main__":
    main()
