#!/usr/bin/env python3
"""
HIS クーポン JSON → SWELL WordPress ブロック HTML 自動生成スクリプト

Usage:
    python generate_his_html.py

Output:
    html_output/his_coupons.html  (セクション別テーブルHTML)
"""

import glob
import json
import os
import re
from datetime import datetime
from html import escape as html_escape

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config", "his_affiliate_links.json")
HIS_DATA_DIR = os.path.join(SCRIPT_DIR, "his_coupon_data")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "html_output")

# ---------------------------------------------------------------------------
# JSON category → 記事セクション マッピング
# ---------------------------------------------------------------------------
# キーワードベースのセクション振り分け（カテゴリマッチより優先）
# タイトル or カテゴリに含まれるキーワードでセクションを決定
KEYWORD_SECTION_OVERRIDES = [
    ("TAViCA", "海外その他"),
    ("TAVICA", "海外その他"),
    ("eSIM", "海外その他"),
]

# 直接マッチ（JSON の category 値 → 記事セクション名）
CATEGORY_TO_SECTION = {
    "海外旅行": "海外ツアー",
    "添乗員同行トルコツアー": "海外ツアー",
    "海外eSIM": "海外その他",
    "国内ツアー": "国内ツアー・航空券＋ホテル",
    "国内航空券＋ホテル": "国内ツアー・航空券＋ホテル",
    "沖縄行き航空券＋ホテル": "国内ツアー・航空券＋ホテル",
    "石川行き航空券＋ホテル": "国内ツアー・航空券＋ホテル",
    "能登（石川）行き航空券＋ホテル": "国内ツアー・航空券＋ホテル",
    "奄美群島行き航空券＋ホテル": "国内ツアー・航空券＋ホテル",
    "福島行きツアー": "国内ツアー・航空券＋ホテル",
    "国内添乗員同行ツアー": "国内添乗員同行ツアー",
    "国内バスツアー": "国内バスツアー",
    "高速バス・夜行バス": "国内バスツアー",
    "北海道・福岡県ホテル": "国内ホテル",
    "グランピング・コテージ・貸し別荘宿泊": "国内ホテル",
}

# 記事セクションの出力順
SECTION_ORDER = [
    "海外ツアー",
    "海外その他",
    "国内ツアー・航空券＋ホテル",
    "国内添乗員同行ツアー",
    "国内ホテル",
    "国内バスツアー",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def load_latest_coupons():
    """最新の HIS クーポン JSON を読み込む"""
    pattern = os.path.join(HIS_DATA_DIR, "coupons_*.json")
    files = sorted(glob.glob(pattern))
    if not files:
        raise FileNotFoundError(f"No coupon files found in {HIS_DATA_DIR}")
    latest = files[-1]
    with open(latest, "r", encoding="utf-8") as f:
        return json.load(f), os.path.basename(latest)


def load_config():
    """アフィリエイトリンク設定を読み込む"""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get_section(category: str, title: str = "") -> str | None:
    """JSON の category を記事セクションに振り分ける"""
    # 学生キャンペーンはスキップ（別記事で使用）
    if "学生" in category:
        return None

    # キーワードオーバーライド（タイトル・カテゴリから判定）
    text = f"{category} {title}"
    for keyword, section in KEYWORD_SECTION_OVERRIDES:
        if keyword in text:
            return section

    # 直接マッチ
    if category in CATEGORY_TO_SECTION:
        return CATEGORY_TO_SECTION[category]

    # キーワードフォールバック
    if "海外" in category or "eSIM" in category:
        return "海外その他"
    if "バス" in category:
        return "国内バスツアー"
    if "ホテル" in category or "グランピング" in category or "コテージ" in category:
        return "国内ホテル"
    if "添乗員" in category:
        return "国内添乗員同行ツアー"
    if "国内" in category or "行き" in category:
        return "国内ツアー・航空券＋ホテル"

    # どこにも該当しない → 海外その他
    return "海外その他"


def get_affiliate_link(coupon: dict, config: dict) -> tuple[str, str]:
    """クーポンに適切なアフィリエイトリンクを返す (url, pixel)"""
    category = coupon.get("category", "")
    title = coupon.get("title", "")
    text = f"{category} {title}"

    # 1) keyword_overrides を上から順にチェック
    for override in config.get("keyword_overrides", []):
        if override["keyword"] in text:
            return override.get("url", ""), override.get("pixel", "")

    # 2) セクション別デフォルトリンク
    section = get_section(category)
    if section and section in config.get("category_links", {}):
        link = config["category_links"][section]
        return link.get("url", ""), link.get("pixel", "")

    # 3) フォールバック
    fallback = config.get("category_links", {}).get("海外その他", {})
    return fallback.get("url", ""), fallback.get("pixel", "")


# ---------------------------------------------------------------------------
# 日付フォーマット
# ---------------------------------------------------------------------------
def simplify_period(text: str) -> str:
    """日付文字列を簡潔にする
    '2026年2月3日(火)10:00～2026年3月31日(火)23:59'
    → '2026/2/3～2026/3/31'
    """
    if not text:
        return ""
    # 曜日を除去
    s = re.sub(r"\([月火水木金土日祝]\)", "", text)
    # 時刻を除去
    s = re.sub(r"\s*\d{1,2}:\d{2}", "", s)
    # 年月日 → スラッシュ
    s = re.sub(r"(\d{4})年(\d{1,2})月(\d{1,2})日?", r"\1/\2/\3", s)
    # 余分な空白
    s = re.sub(r"\s+", " ", s).strip()
    return s


def format_period_cell(coupon: dict) -> str:
    """期間セルの HTML を生成"""
    booking = simplify_period(coupon.get("booking_period", ""))
    travel = simplify_period(coupon.get("travel_period", ""))
    parts = []
    if booking:
        parts.append(f"予約: {html_escape(booking)}")
    if travel:
        parts.append(f"出発: {html_escape(travel)}")
    return "<br>".join(parts) if parts else "-"


# ---------------------------------------------------------------------------
# テーブルセル生成
# ---------------------------------------------------------------------------
def format_name_cell(coupon: dict, aff_url: str, pixel_url: str) -> str:
    """クーポン名セル（アフィリエイトリンク付き）"""
    title = coupon.get("title", "")
    # タイトルを短縮（長すぎる場合）
    display_title = title
    if len(display_title) > 50:
        display_title = display_title[:47] + "…"

    if aff_url:
        cell = f'→<a href="{html_escape(aff_url)}">{html_escape(display_title)}</a>'
        if pixel_url:
            cell += f'<img width="1" height="1" src="{html_escape(pixel_url)}">'
    else:
        cell = f"→<strong>{html_escape(display_title)}</strong>"
    return cell


def extract_discount_amount(discount_text: str) -> str:
    """割引テキストから金額部分だけを抽出する
    'お1人様5,000円割引お1人様の旅行代金30,000円以上' → 'お1人様5,000円割引'
    '1グループ10,000円割引旅行代金総額100,000円以上' → '1グループ10,000円割引'
    '1グループ20％割引' → '1グループ20％割引'
    """
    # 「割引」「OFF」で区切って最初の部分を取る
    m = re.match(r"(.+?(?:割引|OFF|引))", discount_text)
    if m:
        return m.group(1)
    return discount_text


def format_discount_cell(coupon: dict) -> str:
    """特典セル"""
    codes = coupon.get("coupon_codes", [])
    if not codes:
        return html_escape(coupon.get("discount", "-"))

    if len(codes) == 1:
        raw = codes[0].get("discount", coupon.get("discount", "-"))
        return html_escape(extract_discount_amount(raw))

    # 複数段階
    parts = []
    for cc in codes:
        cond = cc.get("condition", "")
        disc = extract_discount_amount(cc.get("discount", ""))
        if cond:
            # 条件を簡潔に
            short_cond = shorten_condition(cond)
            parts.append(f"{html_escape(short_cond)}: {html_escape(disc)}")
        else:
            parts.append(html_escape(disc))
    return "<br>".join(parts)


def shorten_condition(cond: str) -> str:
    """条件テキストを簡潔にする"""
    cond = cond.replace("出発の", "").replace("以上前まで", "前")
    cond = cond.replace("同時予約あり", "有").replace("同時予約なし", "無")
    return cond


def format_code_cell(coupon: dict) -> str:
    """クーポンコードセル"""
    codes = coupon.get("coupon_codes", [])
    if not codes:
        return "不要"

    if len(codes) == 1:
        return html_escape(codes[0].get("code", "-"))

    parts = []
    for cc in codes:
        code = cc.get("code", "")
        cond = cc.get("condition", "")
        if cond:
            short_cond = shorten_condition(cond)
            parts.append(f"({html_escape(short_cond)}) {html_escape(code)}")
        else:
            parts.append(html_escape(code))
    return "<br>".join(parts)


def format_conditions_cell(coupon: dict) -> str:
    """主な条件セル"""
    target = coupon.get("target", "")
    if not target:
        return "-"
    # 【対象外】以降を除去
    target = re.split(r"【対象外】", target)[0].strip()
    # 冗長な表現を短縮
    target = target.replace("HISが企画・実施する", "")
    target = target.replace("HISが旅行企画・実施する", "")
    # 長すぎる場合は短縮（エスケープ前に）
    if len(target) > 60:
        target = target[:57] + "…"
    # エスケープしてから改行タグを追加
    result = html_escape(target)
    result = result.replace("※", "<br>※")
    return result


# ---------------------------------------------------------------------------
# SWELL テーブルブロック生成
# ---------------------------------------------------------------------------
SWELL_TABLE_OPEN = (
    '<!-- wp:table {"className":"min_width10_ is-thead-centered is-all-centered",'
    '"swlScrollable":"both","swlTableWidth":"1200px","swlFz":"14px"} -->\n'
    '<figure class="wp-block-table min_width10_ is-thead-centered is-all-centered">'
    '<table class="has-fixed-layout"><thead><tr>'
    "<th>クーポン名</th><th>特典</th><th>クーポンコード</th><th>期間</th><th>主な条件</th>"
    "</tr></thead><tbody>"
)

SWELL_TABLE_CLOSE = "</tbody></table></figure>\n<!-- /wp:table -->"


def generate_table(coupons: list[dict], config: dict) -> str:
    """クーポンリストから SWELL テーブル HTML を生成"""
    # 配布中のみ
    active = [c for c in coupons if c.get("stock_status") == "配布中"]
    if not active:
        return ""

    rows = []
    for coupon in active:
        aff_url, pixel_url = get_affiliate_link(coupon, config)

        name = format_name_cell(coupon, aff_url, pixel_url)
        discount = format_discount_cell(coupon)
        code = format_code_cell(coupon)
        period = format_period_cell(coupon)
        conditions = format_conditions_cell(coupon)

        rows.append(
            f"<tr><th>{name}</th>"
            f"<td>{discount}</td>"
            f"<td>{code}</td>"
            f"<td>{period}</td>"
            f"<td>{conditions}</td></tr>"
        )

    return SWELL_TABLE_OPEN + "\n".join(rows) + "\n" + SWELL_TABLE_CLOSE


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------
def main():
    coupons, filename = load_latest_coupons()
    config = load_config()

    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", filename)
    data_date = date_match.group(1) if date_match else "unknown"

    print(f"📊 {len(coupons)} 件のクーポンを読み込み ({filename})")

    # セクション振り分け
    sections: dict[str, list] = {s: [] for s in SECTION_ORDER}
    skipped = []

    for coupon in coupons:
        section = get_section(coupon.get("category", ""), coupon.get("title", ""))
        if section is None:
            skipped.append(coupon.get("title", "?"))
            continue
        if section in sections:
            sections[section].append(coupon)
        else:
            sections.setdefault(section, []).append(coupon)

    for name, items in sections.items():
        active = [c for c in items if c.get("stock_status") == "配布中"]
        print(f"  {name}: {len(active)} 件（配布中）/ {len(items)} 件（合計）")
    if skipped:
        print(f"  スキップ（学生等）: {len(skipped)} 件")

    # HTML 生成
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    parts: list[str] = []

    parts.append(f"<!-- HIS クーポン SWELL HTML（自動生成） -->")
    parts.append(f"<!-- データ: {filename} / 生成: {datetime.now().strftime('%Y-%m-%d %H:%M')} -->")
    parts.append("")

    for section_name in SECTION_ORDER:
        section_coupons = sections.get(section_name, [])
        parts.append(f"<!-- ========== {section_name} ========== -->")

        table_html = generate_table(section_coupons, config)
        if table_html:
            parts.append(table_html)
        else:
            parts.append(f"<!-- {section_name}: 配布中のクーポンなし -->")
        parts.append("")

    output_path = os.path.join(OUTPUT_DIR, "his_coupons.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))

    print(f"\n✅ 出力: {output_path}")
    print(f"   セクション数: {len(SECTION_ORDER)}")


if __name__ == "__main__":
    main()
