#!/usr/bin/env python3
"""
近畿日本ツーリスト クーポン監視スクリプト（シンプル版）
================================================================
獲得クーポン + クーポンコードの一覧ページからクーポンを収集し、
各詳細ページから割引額・条件等を抽出。前回との差分（新規・消失）を検出する。

使い方:
  python knt_coupon_monitor.py           # 通常実行
  python knt_coupon_monitor.py --init    # 初回セットアップ
"""

import requests
from bs4 import BeautifulSoup
import json
import sys
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs
import time
import re

# ============================================================
# 設定
# ============================================================
BASE_URL = "https://www.knt.co.jp"

COUPON_PAGES = [
    {
        "name": "獲得クーポン",
        "url": f"{BASE_URL}/coupon/get/",
    },
    {
        "name": "クーポンコード",
        "url": f"{BASE_URL}/coupon/code/",
    },
]

DATA_DIR = Path("./knt_coupon_data")
MASTER_FILE = DATA_DIR / "master_ids.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ja,en;q=0.9",
}

REQUEST_DELAY = 2

# フィルタ: ダミーエントリやトップページリンクを除外
SKIP_PATHS = {"/", "/coupon/", "/coupon/get/", "/coupon/code/", "/contents/fukkou/"}


# ============================================================
# ユーティリティ
# ============================================================
def setup_dirs():
    DATA_DIR.mkdir(exist_ok=True)


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def make_coupon_id(url):
    """URLからユニークなIDを生成"""
    parsed = urlparse(url)
    # campaign.html?cmpgncd=XXX → cmpgncd値
    qs = parse_qs(parsed.query)
    if "cmpgncd" in qs:
        return f"cmpgn-{qs['cmpgncd'][0]}"
    # /yado/sp/xxx/ → パス末尾
    path = parsed.path.rstrip("/")
    if path:
        # 末尾2セグメントをIDに（例: yado-sp-okinawa_kcp）
        parts = [p for p in path.split("/") if p]
        slug = "-".join(parts[-3:]) if len(parts) >= 3 else "-".join(parts)
        return slug
    return hashlib.md5(url.encode()).hexdigest()[:12]


def is_valid_detail_url(href):
    """ダミーや無効なリンクを除外"""
    if not href:
        return False
    parsed = urlparse(href)
    path = parsed.path.rstrip("/")
    if path in {"", "/"} or path + "/" in SKIP_PATHS:
        return False
    # knt.co.jp 内部リンクのみ（外部サイトは除外）
    if parsed.netloc and "knt.co.jp" not in parsed.netloc:
        return False
    return True


# ============================================================
# マスターID管理
# ============================================================
def load_master_ids():
    if MASTER_FILE.exists():
        with open(MASTER_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"last_updated": "", "ids": {}}


def save_master_ids(master):
    master["last_updated"] = datetime.now().isoformat()
    with open(MASTER_FILE, "w", encoding="utf-8") as f:
        json.dump(master, f, ensure_ascii=False, indent=2)


# ============================================================
# 都道府県リスト
# ============================================================
PREFECTURES = [
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県",
    "茨城県", "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県",
    "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県",
    "岐阜県", "静岡県", "愛知県", "三重県",
    "滋賀県", "京都府", "大阪府", "兵庫県", "奈良県", "和歌山県",
    "鳥取県", "島根県", "岡山県", "広島県", "山口県",
    "徳島県", "香川県", "愛媛県", "高知県",
    "福岡県", "佐賀県", "長崎県", "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
    # 略称
    "沖縄", "北海道", "九州", "東北",
]


# ============================================================
# スクレイピング: 一覧ページ
# ============================================================
def scrape_list_page(page_config):
    """KNTの一覧ページからクーポンカード情報を抽出"""
    page_name = page_config["name"]
    page_url = page_config["url"]

    print(f"📡 [{page_name}] 一覧ページを取得中... {page_url}")
    resp = requests.get(page_url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding

    soup = BeautifulSoup(resp.text, "html.parser")
    coupons = []
    seen_urls = set()

    # h5 タイトル要素を起点にカードを探す
    for h5 in soup.find_all("h5"):
        title = h5.get_text(strip=True)
        if not title or "ああああ" in title:
            continue

        # h5の近傍から詳細リンクを探す
        # 親要素を遡ってカードコンテナを見つける
        card = h5
        detail_url = None
        for _ in range(6):
            parent = card.parent
            if parent is None:
                break
            card = parent
            # カード内の「詳細はこちら」「キャンペーン詳細はこちら」リンクを探す
            links = card.find_all("a", href=True)
            for link in links:
                link_text = link.get_text(strip=True)
                href = link.get("href", "")
                if ("詳細" in link_text or "campaign" in href) and is_valid_detail_url(href):
                    detail_url = urljoin(BASE_URL, href)
                    break
            if detail_url:
                break

        if not detail_url or detail_url in seen_urls:
            continue
        seen_urls.add(detail_url)

        card_text = card.get_text(separator="\n", strip=True)

        # 都道府県を抽出
        area = ""
        areas_found = []
        for pref in PREFECTURES:
            if pref in card_text[:200]:
                if pref not in areas_found:
                    areas_found.append(pref)
        if areas_found:
            area = "・".join(areas_found[:3])  # 最大3つまで

        # 対象タイプ
        type_keywords = [
            "国内ダイナミックパッケージ",
            "海外ダイナミックパッケージ",
            "国内ツアー", "国内宿泊",
        ]
        found_types = []
        for kw in type_keywords:
            if kw in card_text:
                found_types.append(kw)
        coupon_type = "・".join(found_types) if found_types else ""

        coupon_id = make_coupon_id(detail_url)

        coupons.append({
            "id": coupon_id,
            "category": page_name,
            "title": title,
            "area": area,
            "type": coupon_type,
            "detail_url": detail_url,
            "detail_data": None,
        })

    print(f"  ✅ [{page_name}] {len(coupons)}件検出")
    return coupons


def scrape_all_lists():
    all_coupons = []
    for page_config in COUPON_PAGES:
        time.sleep(REQUEST_DELAY)
        coupons = scrape_list_page(page_config)
        all_coupons.extend(coupons)

    # 重複排除（同じクーポンが両ページに載る場合）
    seen = {}
    deduped = []
    for c in all_coupons:
        if c["id"] not in seen:
            seen[c["id"]] = True
            deduped.append(c)
        else:
            print(f"  ℹ️ 重複スキップ: {c['id']}")
    return deduped


# ============================================================
# スクレイピング: 詳細ページ
# ============================================================
def scrape_detail_page(url):
    """KNTの詳細ページから割引額・条件・期間などを抽出
    
    注意: KNTのページは1ページ内に複数の期間セクション（例: 秋/冬、
    第1弾/第2弾、1～3月/4～6月）が混在することが多い。
    re.search()ではなくre.findall()で全件取得し、
    最も未来の日付を持つ情報を採用する。
    """
    try:
        time.sleep(REQUEST_DELAY)
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding

        soup = BeautifulSoup(resp.text, "html.parser")
        page_text = soup.get_text(separator="\n", strip=True)

        detail = {
            "discount": "",
            "conditions": [],
            "booking_period": "",
            "stay_period": "",
            "coupon_codes": [],
            "notes": [],
        }

        # ----- 割引額 -----
        # 「X,XXX円分クーポン」「X,XXX円割引」パターン（ページ全体から全件取得）
        discount_matches = re.findall(
            r'([0-9,]+)円(?:分クーポン|割引クーポン|割引)', page_text
        )
        if discount_matches:
            amounts = []
            for m in discount_matches:
                amt = m.replace(",", "")
                if amt.isdigit() and int(amt) >= 500:
                    amounts.append(int(amt))
            if amounts:
                max_amt = max(amounts)
                detail["discount"] = f"最大{max_amt:,}円割引" if len(amounts) > 1 else f"{max_amt:,}円割引"

        # ----- 条件（X円以上で使える / 先着X名様） -----
        cond_patterns = [
            r'([0-9,]+)円以上で使える',
            r'先着[\s]*([0-9,]+)[\s]*名様',
        ]
        for pat in cond_patterns:
            for match in re.finditer(pat, page_text):
                cond = match.group(0)
                if cond not in detail["conditions"]:
                    detail["conditions"].append(cond)

        # =============================================================
        # 期間抽出: ページ全体から全件取得 → 最新のものを採用
        # =============================================================
        detail["booking_period"] = _extract_latest_period(
            page_text,
            labels=["申込期間", "予約期間", "予約受付期間", "対象予約日"],
        )

        detail["stay_period"] = _extract_latest_period(
            page_text,
            labels=["出発期間", "対象宿泊日", "宿泊対象期間", "出発対象期間",
                     "宿泊期間", "対象期間", "対象出発日"],
        )

        # ----- クーポンコード -----
        code_patterns = [
            r'クーポンコード[：:\s]*([A-Za-z0-9_\-]+)',
            r'コード[：:\s]*([A-Za-z0-9_\-]+)',
        ]
        for pat in code_patterns:
            for match in re.finditer(pat, page_text, re.IGNORECASE):
                code = match.group(1)
                if code not in detail["coupon_codes"] and len(code) >= 3:
                    detail["coupon_codes"].append(code)

        # ----- 注意事項（重要キーワード周辺） -----
        note_keywords = ["併用不可", "1回限り", "先着", "枚数限定", "1予約につき", "なくなり次第"]
        for keyword in note_keywords:
            if keyword in page_text:
                idx = page_text.find(keyword)
                start = max(0, idx - 10)
                end = min(len(page_text), idx + 60)
                snippet = page_text[start:end].replace("\n", " ").strip()
                if snippet and snippet not in detail["notes"]:
                    detail["notes"].append(snippet)

        return detail

    except Exception as e:
        print(f"    ⚠️ 詳細ページ取得エラー: {e}")
        return {
            "discount": "",
            "conditions": [],
            "booking_period": "",
            "stay_period": "",
            "coupon_codes": [],
            "notes": [],
        }


def _extract_latest_period(page_text, labels):
    """
    ページテキストから指定ラベルに続く期間文字列を全件抽出し、
    有効な期間をすべてまとめて返す。

    KNTのページは1ページ内に以下のようなパターンが混在する:
      - 第1弾 予約期間: 2025/10/17～12/28  ← 終了済み
      - 第2弾 予約期間: 2025/11/20～2026/2/24  ← 有効
      - 第3弾 予約期間: 2025/12/15～3/28  ← 有効
    → 全部拾って、終了済みを除外し、有効な全弾をまとめて返す

    返却例:
      有効1件: "2025年12月15日(月)～3月28日(土)"
      有効複数: "【第2弾】～2026年2月24日 / 【第3弾】～3月28日"
      全終了: 最新の1件をそのまま返す
    """
    label_pattern = "|".join(re.escape(l) for l in labels)

    # 他の期間ラベルが改行なしで出現した場合にも終端させる（インライン終端）
    # 高知: "...3月8日(日）まで対象期間2026年..." → 「対象期間」で終端
    # 鹿児島: "...2月24日(火)なくなり次第終了ご予約は...宿泊期間2025年..." → 「宿泊期間」で終端
    inline_term = (
        r'(?:宿泊期間|出発期間|対象期間|対象商品|対象出発日|対象予約日|対象宿泊日'
        r'|申込期間|予約期間|予約受付期間|割引額|割引クーポン)'
    )

    patterns = [
        # 日本語形式: 2025年1月10日(金)～2025年3月12日(水)...
        # 終端: 空行、改行+キーワード、インラインの期間ラベル、金額表記
        rf'(?:{label_pattern})[：:\s]*\n?\s*(\d{{4}}年\d{{1,2}}月\d{{1,2}}日.+?)(?:\n\n|\n(?:※|対象|下記|割引|クーポン|本|第\d|【|\d[\d,]*円|$)|(?={inline_term}))',
        # 簡易形式: ラベルの直後の1行
        rf'(?:{label_pattern})[：:\s]*\n?\s*(.+?)(?:\n|$)',
    ]

    # セクション境界パターン（「第N弾は終了」は境界ではなく終了告知）
    sec_boundary_pattern = r'(?:第\d弾(?!は)|【[^】]+】)'
    # セクション名抽出パターン（「第1弾」「第2弾」「【秋】」「【冬】」など）
    sec_name_pattern = r'(第\d+弾|【[^】]+】)'

    all_periods = []

    for pat in patterns:
        for match in re.finditer(pat, page_text, re.DOTALL):
            raw = match.group(1).replace("\n", "").strip()
            if not raw or len(raw) < 8:
                continue

            pos = match.start()
            match_end = match.end()

            # --- セクション境界の特定 ---
            section_start = 0
            for sec_match in re.finditer(sec_boundary_pattern, page_text[:pos]):
                section_start = sec_match.start()

            # 直後のセクション区切りを探す
            # ただし「※【秋】...は終了いたしました」のような終了告知行は
            # セクション境界ではなく注釈なのでスキップする
            section_end = min(len(page_text), match_end + 300)
            search_from = match_end
            while True:
                next_sec = re.search(sec_boundary_pattern, page_text[search_from:])
                if not next_sec:
                    break
                # この境界を含む行が「終了」を含んでいたら注釈行→スキップ
                boundary_abs_pos = search_from + next_sec.start()
                line_start = page_text.rfind("\n", 0, boundary_abs_pos) + 1
                line_end = page_text.find("\n", boundary_abs_pos)
                if line_end == -1:
                    line_end = len(page_text)
                boundary_line = page_text[line_start:line_end]
                if "終了" in boundary_line:
                    # この境界はスキップして次を探す
                    search_from = boundary_abs_pos + len(next_sec.group())
                    continue
                section_end = boundary_abs_pos
                break

            # 同一セクション内で「終了」を判定
            section_text = page_text[section_start:section_end]
            is_ended = "終了いたしました" in section_text or "終了しました" in section_text

            # --- セクション名の取得 ---
            # マッチ位置の直前200文字からセクション名を探す
            pre_text = page_text[max(0, pos - 200):pos]
            sec_name_matches = re.findall(sec_name_pattern, pre_text)
            section_name = sec_name_matches[-1] if sec_name_matches else ""

            # 終了日を抽出してソート用のスコアにする
            end_date = _extract_end_date(raw)

            all_periods.append({
                "text": raw[:120],
                "end_date": end_date,
                "is_ended": is_ended,
                "section_name": section_name,
            })

        # 十分な結果が得られたら後続パターンは不要
        if all_periods:
            break

    if not all_periods:
        return ""

    # 終了セクションを除外（ただし全部終了なら仕方なく最新を使う）
    active = [p for p in all_periods if not p["is_ended"]]
    candidates = active if active else all_periods

    # 終了日の昇順でソート
    candidates.sort(key=lambda p: p["end_date"] or "0000-00-00")

    # --- 返却値の組み立て ---
    if len(candidates) == 1:
        # 有効1件: そのまま返す
        return candidates[0]["text"]

    # 有効複数件: セクション名付きで結合
    # セクション名がどの候補にもなければ番号を振る
    has_names = any(p["section_name"] for p in candidates)

    parts = []
    for i, p in enumerate(candidates):
        if has_names and p["section_name"]:
            # 「第2弾」→「【第2弾】」、「【冬】」はそのまま
            name = p["section_name"]
            if not name.startswith("【"):
                name = f"【{name}】"
            parts.append(f"{name}{p['text']}")
        elif not has_names and len(candidates) > 1:
            parts.append(p["text"])
        else:
            parts.append(p["text"])

    return " / ".join(parts)


def _extract_end_date(period_str):
    """
    期間文字列から終了日を抽出してYYYY-MM-DD形式で返す。
    
    KNTの期間表記は終了日の年を省略することが多い:
      「2025年12月15日(月)～3月28日(土)」→ 終了日は2026年3月28日
      「2026年1月4日(日)～2月28日(土)」→ 終了日は2026年2月28日
    
    ロジック:
    1. 年あり日付を全件取得
    2. ～の後にある年なし日付も取得
    3. 年なし日付には直前の年あり日付から年を推定
       （月が前の日付より小さければ翌年と判定）
    """
    # ステップ1: 年あり日付を全件取得
    dated_matches = re.findall(
        r'(\d{4})[年/](\d{1,2})[月/](\d{1,2})日?',
        period_str
    )
    
    dates = []
    last_year = None
    last_month = None
    
    for y, m, d in dated_matches:
        try:
            year, month, day = int(y), int(m), int(d)
            dates.append(f"{year:04d}-{month:02d}-{day:02d}")
            last_year = year
            last_month = month
        except ValueError:
            continue

    # ステップ2: ～の後にある年なし日付を取得
    # パターン: ～12月28日 or ～3月28日(土) （年なし）
    yearless_after_tilde = re.findall(
        r'[～〜~]\s*(\d{1,2})月(\d{1,2})日',
        period_str
    )
    
    if yearless_after_tilde and last_year:
        for m_str, d_str in yearless_after_tilde:
            try:
                month, day = int(m_str), int(d_str)
                # 年を推定: 月が開始月以下なら翌年（12月→3月 = 翌年）
                inferred_year = last_year
                if last_month and month < last_month:
                    inferred_year = last_year + 1
                dates.append(f"{inferred_year:04d}-{month:02d}-{day:02d}")
            except ValueError:
                continue

    return max(dates) if dates else None


# ============================================================
# 差分検出（新規・消失）
# ============================================================
def detect_changes(master_ids, current_coupons):
    prev_ids = set(master_ids.get("ids", {}).keys())
    curr_map = {c["id"]: c for c in current_coupons}
    curr_id_set = set(curr_map.keys())

    new_ids = curr_id_set - prev_ids
    gone_ids = prev_ids - curr_id_set

    events = []

    for cid in sorted(new_ids):
        c = curr_map[cid]
        events.append({
            "date": today_str(),
            "type": "🆕 新規",
            "id": cid,
            "category": c["category"],
            "title": c["title"],
            "area": c.get("area", ""),
        })

    for cid in sorted(gone_ids):
        prev_info = master_ids["ids"].get(cid, {})
        events.append({
            "date": today_str(),
            "type": "❌ 消失",
            "id": cid,
            "category": prev_info.get("category", ""),
            "title": prev_info.get("title", ""),
            "area": prev_info.get("area", ""),
        })

    return events


def update_master_ids(master_ids, current_coupons):
    new_ids = {}
    for c in current_coupons:
        new_ids[c["id"]] = {
            "category": c["category"],
            "title": c["title"],
            "area": c.get("area", ""),
        }
    master_ids["ids"] = new_ids
    return master_ids


# ============================================================
# データ保存
# ============================================================
def save_daily_data(coupons):
    today = today_str()
    daily_file = DATA_DIR / f"coupons_{today}.json"
    data = []
    for c in coupons:
        entry = {k: v for k, v in c.items() if k != "detail_data"}
        entry["detail_data"] = c.get("detail_data") or {}
        data.append(entry)

    with open(daily_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"💾 日次データ保存: {daily_file}（{len(data)}件）")


def save_change_log(events):
    log_file = DATA_DIR / "change_log.json"
    existing = []
    if log_file.exists():
        with open(log_file, "r", encoding="utf-8") as f:
            existing = json.load(f)

    existing.extend(events)
    cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    existing = [e for e in existing if e.get("date", "") >= cutoff]

    with open(log_file, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)


# ============================================================
# レポート
# ============================================================
def generate_report(coupons, events):
    today = today_str()
    get_coupons = [c for c in coupons if c["category"] == "獲得クーポン"]
    code_coupons = [c for c in coupons if c["category"] == "クーポンコード"]

    lines = [
        f"# KNTクーポンレポート {today}",
        f"",
        f"## 概要",
        f"- 獲得クーポン: {len(get_coupons)}件",
        f"- クーポンコード: {len(code_coupons)}件",
        f"- 合計: {len(coupons)}件",
        f"",
    ]

    if events:
        lines.append("## 変動")
        for e in events:
            lines.append(f"- {e['type']} [{e['category']}] {e['title']} ({e['id']})")
        lines.append("")
    else:
        lines.append("## 変動: なし")
        lines.append("")

    report_text = "\n".join(lines)
    report_file = DATA_DIR / f"report_{today}.md"
    with open(report_file, "w", encoding="utf-8") as f:
        f.write(report_text)
    print(f"📝 レポート保存: {report_file}")

    print("\n" + "=" * 60)
    for line in lines:
        print(line)
    print("=" * 60)


# ============================================================
# メイン
# ============================================================
def run_init():
    print("🔄 KNT 初期化モード")
    setup_dirs()

    coupons = scrape_all_lists()

    print(f"\n📄 詳細ページを取得中（{len(coupons)}ページ、約{len(coupons) * REQUEST_DELAY}秒）...")
    for i, coupon in enumerate(coupons):
        print(f"  [{i+1}/{len(coupons)}] [{coupon['category']}] {coupon['title'][:40]}...")
        detail = scrape_detail_page(coupon["detail_url"])
        coupon["detail_data"] = detail
        # 詳細ページの割引額をクーポンに反映
        if detail.get("discount") and not coupon.get("discount"):
            coupon["discount"] = detail["discount"]

    save_daily_data(coupons)

    master_ids = update_master_ids({"last_updated": "", "ids": {}}, coupons)
    save_master_ids(master_ids)

    generate_report(coupons, [])
    print(f"\n✅ KNT 初期化完了: {len(coupons)}件")


def run_full():
    setup_dirs()

    coupons = scrape_all_lists()

    print(f"\n📄 詳細ページを取得中（{len(coupons)}ページ、約{len(coupons) * REQUEST_DELAY}秒）...")
    for i, coupon in enumerate(coupons):
        print(f"  [{i+1}/{len(coupons)}] [{coupon['category']}] {coupon['title'][:40]}...")
        detail = scrape_detail_page(coupon["detail_url"])
        coupon["detail_data"] = detail
        if detail.get("discount") and not coupon.get("discount"):
            coupon["discount"] = detail["discount"]

    save_daily_data(coupons)

    master_ids = load_master_ids()
    events = detect_changes(master_ids, coupons)

    if events:
        print(f"\n📢 変動検出: {len(events)}件")
        for e in events:
            print(f"  {e['type']} [{e['category']}] {e['title']}")
    else:
        print("\n📢 変動なし")

    master_ids = update_master_ids(master_ids, coupons)
    save_master_ids(master_ids)

    if events:
        save_change_log(events)

    generate_report(coupons, events)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--init":
        run_init()
    else:
        run_full()


if __name__ == "__main__":
    main()
