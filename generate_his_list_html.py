#!/usr/bin/env python3
"""
HIS クーポン JSON → SWELL WordPress ブロック HTML 自動生成スクリプト（リスト形式）

既存の HIS_クーポンリスト.md と同一フォーマット（タブ構造＋リスト形式）で出力。
テーブル形式の generate_his_html.py とは別に、記事埋め込み用リストHTMLを生成する。

Usage:
    python generate_his_list_html.py

Output:
    html_output/his_coupons_list.html
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
# セクション定義（海外タブ / 国内タブ）
# ---------------------------------------------------------------------------
OVERSEAS_SECTIONS = ["海外ツアー", "海外その他"]
DOMESTIC_SECTIONS = [
    "国内ツアー・航空券＋ホテル",
    "国内添乗員同行ツアー",
    "国内ホテル",
    "国内バスツアー",
]

# 記事内の h3 セクション名とID
SECTION_DISPLAY = {
    "海外ツアー": {"name": "海外ツアー", "id": ""},
    "海外その他": {"name": "海外その他", "id": ""},
    "国内ツアー・航空券＋ホテル": {"name": "国内ツアー", "id": "80"},
    "国内添乗員同行ツアー": {"name": "添乗員同行ツアー", "id": "tenjyouin"},
    "国内ホテル": {"name": "ホテル・宿泊", "id": "hotel"},
    "国内バスツアー": {"name": "バスツアー・高速バス", "id": "bus"},
}

# h3 で使う既存IDマッピング（既存記事のアンカーを維持）
SECTION_H3_IDS = {
    "海外航空券": "71",
    "海外ツアー": "",
    "国内ツアー・航空券＋ホテル": "80",
    "国内添乗員同行ツアー": "tenjyouin",
    "国内ホテル": "hotel",
    "国内バスツアー": "bus",
}

# ---------------------------------------------------------------------------
# セクション分類（generate_his_html.py と同一ロジック）
# ---------------------------------------------------------------------------
KEYWORD_SECTION_OVERRIDES = [
    ("TAViCA", "海外その他"),
    ("TAVICA", "海外その他"),
    ("eSIM", "海外その他"),
]

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


def get_section(category: str, title: str = ""):
    if "学生" in category:
        return None
    text = f"{category} {title}"
    for keyword, section in KEYWORD_SECTION_OVERRIDES:
        if keyword in text:
            return section
    if category in CATEGORY_TO_SECTION:
        return CATEGORY_TO_SECTION[category]
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
    return "海外その他"


# ---------------------------------------------------------------------------
# Affiliate Link
# ---------------------------------------------------------------------------
def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get_affiliate_link(coupon: dict, config: dict):
    category = coupon.get("category", "")
    title = coupon.get("title", "")
    text = f"{category} {title}"
    for override in config.get("keyword_overrides", []):
        if override["keyword"] in text:
            return override.get("url", ""), override.get("pixel", "")
    section = get_section(category)
    if section and section in config.get("category_links", {}):
        link = config["category_links"][section]
        return link.get("url", ""), link.get("pixel", "")
    fallback = config.get("category_links", {}).get("海外その他", {})
    return fallback.get("url", ""), fallback.get("pixel", "")


# ---------------------------------------------------------------------------
# Data Loading
# ---------------------------------------------------------------------------
def load_latest_coupons():
    pattern = os.path.join(HIS_DATA_DIR, "coupons_*.json")
    files = sorted(glob.glob(pattern))
    if not files:
        raise FileNotFoundError(f"No coupon files found in {HIS_DATA_DIR}")
    latest = files[-1]
    with open(latest, "r", encoding="utf-8") as f:
        return json.load(f), os.path.basename(latest)


# ---------------------------------------------------------------------------
# List Item Generation (matching HIS_クーポンリスト.md format)
# ---------------------------------------------------------------------------
def generate_coupon_list_item(coupon: dict, config: dict) -> str:
    """1つのクーポンを wp:list-item として生成"""
    title = coupon.get("title", "")
    aff_url, pixel_url = get_affiliate_link(coupon, config)
    booking = coupon.get("booking_period", "")
    travel = coupon.get("travel_period", "")
    target = coupon.get("target", "")
    codes = coupon.get("coupon_codes", [])

    # リンク部分
    if aff_url:
        link_html = f'<a href="{html_escape(aff_url)}">{html_escape(title)}</a>'
        if pixel_url:
            link_html += f'<img width="1" height="1" src="{html_escape(pixel_url)}">'
    else:
        link_html = html_escape(title)

    # サブリスト項目
    sub_items = []

    if booking:
        sub_items.append(f"予約期間：{html_escape(booking)}")
    if travel:
        sub_items.append(f"対象出発期間：{html_escape(travel)}")
    if target:
        # 簡潔にする
        short_target = target.replace("HISが企画・実施する", "")
        short_target = re.split(r"【対象外】", short_target)[0].strip()
        if len(short_target) > 80:
            short_target = short_target[:77] + "…"
        sub_items.append(f"対象商品：{html_escape(short_target)}")

    # クーポンコード
    if codes:
        if len(codes) == 1:
            code = codes[0].get("code", "")
            sub_items.append(f"クーポンコード：<strong>{html_escape(code)}</strong>")
        else:
            for cc in codes:
                code = cc.get("code", "")
                cond = cc.get("condition", "")
                if cond:
                    cond = cond.replace("出発の", "").replace("以上前まで", "前")
                    sub_items.append(
                        f"クーポンコード（{html_escape(cond)}）：<strong>{html_escape(code)}</strong>"
                    )
                else:
                    sub_items.append(
                        f"クーポンコード：<strong>{html_escape(code)}</strong>"
                    )

    # サブリストHTML
    sub_html = ""
    if sub_items:
        items_html = ""
        for item in sub_items:
            items_html += f"<!-- wp:list-item -->\n<li>{item}</li>\n<!-- /wp:list-item -->\n\n"
        sub_html = (
            "<!-- wp:list -->\n"
            f'<ul class="wp-block-list">{items_html}</ul>\n'
            "<!-- /wp:list -->"
        )

    return (
        "<!-- wp:list-item -->\n"
        f"<li><strong>→{link_html}</strong>{sub_html}</li>\n"
        "<!-- /wp:list-item -->"
    )


def generate_section_html(section_name: str, coupons: list, config: dict) -> str:
    """セクション（h3 + ordered list）を生成"""
    active = [c for c in coupons if c.get("stock_status") == "配布中"]
    if not active:
        return ""

    display = SECTION_DISPLAY.get(section_name, {"name": section_name, "id": ""})
    h3_name = display["name"]
    h3_id = display["id"]
    id_attr = f' id="{h3_id}"' if h3_id else ""

    lines = []
    lines.append('<!-- wp:heading {"level":3,"className":"is-style-section_ttl"} -->')
    lines.append(
        f'<h3 class="wp-block-heading is-style-section_ttl"{id_attr}>'
        f"<strong>{h3_name}</strong></h3>"
    )
    lines.append("<!-- /wp:heading -->")
    lines.append("")
    lines.append('<!-- wp:list {"ordered":true,"className":"-list-under-dashed"} -->')
    lines.append('<ol class="wp-block-list -list-under-dashed">')

    for coupon in active:
        lines.append(generate_coupon_list_item(coupon, config))
        lines.append("")

    lines.append("</ol>")
    lines.append("<!-- /wp:list -->")
    return "\n".join(lines)


def generate_cta_button(url: str, text: str, pixel_url: str = "") -> str:
    pixel_html = ""
    if pixel_url:
        pixel_html = f'<img src="{html_escape(pixel_url)}" width="1" height="1" style="border:none;">'

    lines = []
    lines.append('<!-- wp:paragraph {"align":"center","className":"u-mb-ctrl u-mb-0"} -->')
    lines.append(
        '<p class="has-text-align-center u-mb-ctrl u-mb-0">'
        '<strong class="tdfocus-1757457777982">'
        '<span class="swl-fz u-fz-s">＼HISクーポンをまとめて見るなら／</span>'
        "</strong></p>"
    )
    lines.append("<!-- /wp:paragraph -->")
    lines.append("")

    escaped_url = html_escape(url)
    lines.append(
        f'<!-- wp:loos/button {{"hrefUrl":"{url}","color":"blue","className":"is-style-btn_shiny u-mb-ctrl u-mb-0"}} -->'
    )
    lines.append(
        f'<div class="swell-block-button -html blue_ is-style-btn_shiny u-mb-ctrl u-mb-0">'
        f'<a href="{escaped_url}" rel="nofollow">{text}</a>'
        f"{pixel_html}</div>"
    )
    lines.append("<!-- /wp:loos/button -->")
    lines.append("")
    lines.append('<!-- wp:paragraph {"align":"center","className":"u-mb-ctrl u-mb-30"} -->')
    lines.append(
        '<p class="has-text-align-center u-mb-ctrl u-mb-30"><strong>★誰でも使える★</strong></p>'
    )
    lines.append("<!-- /wp:paragraph -->")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tab Structure
# ---------------------------------------------------------------------------
def generate_full_html(coupons: list, config: dict, filename: str) -> str:
    """HIS_クーポンリスト.md と同一のタブ構造HTMLを生成"""
    # セクション振り分け
    all_sections = OVERSEAS_SECTIONS + DOMESTIC_SECTIONS
    sections = {s: [] for s in all_sections}
    for coupon in coupons:
        section = get_section(coupon.get("category", ""), coupon.get("title", ""))
        if section is None:
            continue
        if section in sections:
            sections[section].append(coupon)

    # CTA ボタン設定
    cta_overseas = config.get("cta_buttons", {}).get("海外セール", {})
    cta_domestic = config.get("cta_buttons", {}).get("国内セール", {})
    # フォールバック
    cta_url = "https://t.afi-b.com/visit.php?a=Q10113i-m6912001_H&p=X653459L"

    tab_id = "97716cc7"

    lines = []
    # Header comment
    lines.append(f"<!-- HIS クーポン SWELL リストHTML（自動生成） -->")
    lines.append(
        f"<!-- データ: {filename} / 生成: {datetime.now().strftime('%Y-%m-%d %H:%M')} -->"
    )

    # Tab container
    lines.append(
        f'<!-- wp:loos/tab {{"tabId":"{tab_id}","tabWidthPC":"flex-auto","tabWidthSP":"flex-auto",'
        f'"tabHeaders":["\\u003cspan class=\\u0022swl-fz u-fz-l\\u0022\\u003e\\u003cstrong\\u003eHIS海外クーポン\\u003c/strong\\u003e\\u003c/span\\u003e",'
        f'"\\u003cspan class=\\u0022swl-fz u-fz-l\\u0022\\u003e\\u003cstrong\\u003eHIS国内クーポン\\u003c/strong\\u003e\\u003c/span\\u003e"],'
        f'"className":"is-style-balloon"}} -->'
    )
    lines.append(
        f'<div class="swell-block-tab is-style-balloon" data-width-pc="flex-auto" data-width-sp="flex-auto">'
        f'<ul class="c-tabList" role="tablist">'
        f'<li class="c-tabList__item" role="presentation">'
        f'<button role="tab" class="c-tabList__button" aria-selected="true" '
        f'aria-controls="tab-{tab_id}-0" data-onclick="tabControl">'
        f'<span class="swl-fz u-fz-l"><strong>HIS海外クーポン</strong></span></button></li>'
        f'<li class="c-tabList__item" role="presentation">'
        f'<button role="tab" class="c-tabList__button" aria-selected="false" '
        f'aria-controls="tab-{tab_id}-1" data-onclick="tabControl">'
        f'<span class="swl-fz u-fz-l"><strong>HIS国内クーポン</strong></span></button></li>'
        f'</ul><div class="c-tabBody">'
    )

    # --- Tab 0: 海外 ---
    lines.append(f'<!-- wp:loos/tab-body {{"tabId":"{tab_id}"}} -->')
    lines.append(f'<div id="tab-{tab_id}-0" class="c-tabBody__item" aria-hidden="false">')
    lines.append('<!-- wp:group {"backgroundColor":"swl-pale-04","layout":{"type":"constrained"}} -->')
    lines.append('<div class="wp-block-group has-swl-pale-04-background-color has-background">')
    lines.append('<!-- wp:heading {"textAlign":"center"} -->')
    lines.append('<h2 class="wp-block-heading has-text-align-center" id="kaigai">【海外】HISの誰でも使える割引クーポンコード一覧</h2>')
    lines.append("<!-- /wp:heading -->")
    lines.append("")
    lines.append("<!-- wp:paragraph -->")
    lines.append("<p>下記は、今すぐ誰でも使えるHISの割引情報と配布中クーポンを一覧にしました。</p>")
    lines.append("<!-- /wp:paragraph -->")
    lines.append("")

    for sec_name in OVERSEAS_SECTIONS:
        sec_html = generate_section_html(sec_name, sections.get(sec_name, []), config)
        if sec_html:
            lines.append(sec_html)
            lines.append("")

    # 海外CTA
    lines.append(
        generate_cta_button(cta_url, "HISクーポンを一覧を見る→")
    )

    # 学生リンク
    lines.append("")
    lines.append('<!-- wp:paragraph {"className":"is-style-balloon_box2"} -->')
    lines.append('<p class="is-style-balloon_box2"><strong>海外の学生限定クーポン・特典はこちら♪</strong></p>')
    lines.append("<!-- /wp:paragraph -->")
    lines.append("")
    lines.append(
        '<!-- wp:loos/post-link {"className":"u-mb-ctrl u-mb-50",'
        '"linkData":{"title":"HISの学生割引クーポンコード＆キャンペーンまとめ【2025年10月】",'
        '"id":83394,"url":"https://yakushima.fun/his-gakusei/",'
        '"kind":"post-type","type":"post"},"icon":"link"} /-->'
    )

    lines.append("</div>")
    lines.append("<!-- /wp:group -->")
    lines.append("</div>")
    lines.append("<!-- /wp:loos/tab-body -->")

    # --- Tab 1: 国内 ---
    lines.append(f'<!-- wp:loos/tab-body {{"id":1,"tabId":"{tab_id}"}} -->')
    lines.append(f'<div id="tab-{tab_id}-1" class="c-tabBody__item" aria-hidden="true">')
    lines.append('<!-- wp:group {"backgroundColor":"swl-pale-04","layout":{"type":"constrained"}} -->')
    lines.append('<div class="wp-block-group has-swl-pale-04-background-color has-background">')
    lines.append('<!-- wp:heading {"textAlign":"center","className":"is-style-default"} -->')
    lines.append('<h2 class="wp-block-heading has-text-align-center is-style-default" id="kokunai1">【国内】HISの誰でも使える割引クーポンコード一覧</h2>')
    lines.append("<!-- /wp:heading -->")
    lines.append("")
    lines.append("<!-- wp:paragraph -->")
    lines.append("<p>下記は今すぐ誰でも使えるHISの割引情報と配布中クーポンを一覧にしました。</p>")
    lines.append("<!-- /wp:paragraph -->")
    lines.append("")

    for sec_name in DOMESTIC_SECTIONS:
        sec_html = generate_section_html(sec_name, sections.get(sec_name, []), config)
        if sec_html:
            lines.append(sec_html)
            lines.append("")

    # 国内CTA
    lines.append(
        generate_cta_button(cta_url, "HISクーポンを一覧を見る→")
    )

    # 学生リンク
    lines.append("")
    lines.append('<!-- wp:paragraph {"className":"is-style-balloon_box2"} -->')
    lines.append('<p class="is-style-balloon_box2"><strong>国内の学生限定クーポン・特典はこちら♪</strong></p>')
    lines.append("<!-- /wp:paragraph -->")
    lines.append("")
    lines.append(
        '<!-- wp:loos/post-link {"linkData":{"title":"HISの学生割引クーポンコード＆キャンペーンまとめ【2025年10月】",'
        '"id":83394,"url":"https://yakushima.fun/his-gakusei/",'
        '"kind":"post-type","type":"post"},"icon":"link"} /-->'
    )

    lines.append("</div>")
    lines.append("<!-- /wp:group -->")
    lines.append("</div>")
    lines.append("<!-- /wp:loos/tab-body -->")

    # Close tab
    lines.append("</div></div>")
    lines.append("<!-- /wp:loos/tab -->")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    coupons, filename = load_latest_coupons()
    config = load_config()

    print(f"📊 {len(coupons)} 件のクーポンを読み込み ({filename})")

    html = generate_full_html(coupons, config, filename)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    output_path = os.path.join(OUTPUT_DIR, "his_coupons_list.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    active_count = sum(1 for c in coupons if c.get("stock_status") == "配布中")
    print(f"✅ 出力: {output_path}")
    print(f"   配布中: {active_count} 件")


if __name__ == "__main__":
    main()
