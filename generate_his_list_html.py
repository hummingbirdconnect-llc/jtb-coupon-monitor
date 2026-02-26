#!/usr/bin/env python3
"""
HIS クーポン JSON → SWELL WordPress ブロック HTML 自動生成スクリプト（リスト形式）

既存の HIS クーポン記事と同一フォーマット（タブ構造＋リスト形式）で出力。
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
# 各セクションの描画属性を宣言的に定義
# heading_type: "h3" = wp:heading, "p" = wp:paragraph
# list_type: "ol" = ordered, "ul" = unordered
# list_class: リストの追加CSSクラス（空文字なら付与しない）
# ---------------------------------------------------------------------------
OVERSEAS_SECTIONS = [
    {
        "key": "海外航空券",
        "display_name": "海外航空券",
        "heading_type": "h3",
        "list_type": "ol",
        "list_class": "-list-under-dashed",
        "id": "71",
    },
    {
        "key": "海外ツアー",
        "display_name": "海外ツアー",
        "heading_type": "h3",
        "list_type": "ol",
        "list_class": "-list-under-dashed",
        "id": "",
    },
    {
        "key": "海外eSIM",
        "display_name": "【海外eSIM】",
        "heading_type": "p",
        "list_type": "ul",
        "list_class": "",
        "id": "",
    },
    {
        "key": "海外オプショナルツアー",
        "display_name": "【海外オプショナルツアー】",
        "heading_type": "p",
        "list_type": "ul",
        "list_class": "",
        "id": "",
    },
]

DOMESTIC_SECTIONS = [
    {
        "key": "国内ツアー",
        "display_name": "国内ツアー",
        "heading_type": "h3",
        "list_type": "ol",
        "list_class": "-list-under-dashed",
        "id": "80",
    },
    {
        "key": "国内ホテル",
        "display_name": "ホテル・宿泊",
        "heading_type": "h3",
        "list_type": "ul",
        "list_class": "-list-under-dashed",
        "id": "hotel",
    },
    {
        "key": "国内航空券",
        "display_name": "国内航空券",
        "heading_type": "h3",
        "list_type": "ul",
        "list_class": "-list-under-dashed",
        "id": "",
    },
    {
        "key": "国内バスツアー",
        "display_name": "バスツアー・高速バス",
        "heading_type": "h3",
        "list_type": "ul",
        "list_class": "-list-under-dashed",
        "id": "bus",
    },
    {
        "key": "国内添乗員同行ツアー",
        "display_name": "添乗員同行ツアー",
        "heading_type": "h3",
        "list_type": "ul",
        "list_class": "-list-under-dashed",
        "id": "tenjyouin",
    },
]

# ---------------------------------------------------------------------------
# セクション分類
# ---------------------------------------------------------------------------
KEYWORD_SECTION_OVERRIDES = [
    ("TAViCA", "海外ツアー"),
    ("TAVICA", "海外ツアー"),
    ("eSIM", "海外eSIM"),
    ("オプショナルツアー", "海外オプショナルツアー"),
]

CATEGORY_TO_SECTION = {
    # 海外
    "海外旅行": "海外ツアー",
    "添乗員同行トルコツアー": "海外ツアー",
    "海外航空券・AirZ(海外航空券+ホテル)": "海外航空券",
    "海外eSIM": "海外eSIM",
    # 国内ツアー系
    "国内ツアー": "国内ツアー",
    "国内航空券＋ホテル": "国内ツアー",
    "沖縄行き航空券＋ホテル": "国内ツアー",
    "石川行き航空券＋ホテル": "国内ツアー",
    "能登（石川）行き航空券＋ホテル": "国内ツアー",
    "奄美群島行き航空券＋ホテル": "国内ツアー",
    "福島行きツアー": "国内ツアー",
    # 国内航空券（独立セクション）
    "国内航空券": "国内航空券",
    # 国内添乗員同行ツアー
    "国内添乗員同行ツアー": "国内添乗員同行ツアー",
    # バス
    "国内バスツアー": "国内バスツアー",
    "高速バス・夜行バス": "国内バスツアー",
    # ホテル
    "国内ホテル": "国内ホテル",
    "北海道・福岡県ホテル": "国内ホテル",
    "グランピング・コテージ・貸し別荘宿泊": "国内ホテル",
    "変なホテル": "国内ホテル",
    "満天ノ 辻のや": "国内ホテル",
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
    # フォールバック（キーワードベース）
    if "eSIM" in category:
        return "海外eSIM"
    if "海外" in category:
        return "海外ツアー"
    if "バス" in category:
        return "国内バスツアー"
    if "ホテル" in category or "グランピング" in category or "コテージ" in category:
        return "国内ホテル"
    if "添乗員" in category:
        return "国内添乗員同行ツアー"
    if "航空券" in category and "国内" in category:
        return "国内航空券"
    if "国内" in category or "行き" in category:
        return "国内ツアー"
    return "海外ツアー"


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
    fallback = config.get("category_links", {}).get("海外ツアー", {})
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
# Text Cleanup Helpers
# ---------------------------------------------------------------------------
def clean_condition(cond: str) -> str:
    """条件テキストを整形: '出発の180日以上前まで' → '180日以上前'"""
    cond = re.sub(r"^出発の", "", cond)
    cond = re.sub(r"^宿泊の", "", cond)
    cond = re.sub(r"まで$", "", cond)
    return cond


def extract_discount_and_minimum(disc: str) -> tuple:
    """割引テキストから割引額と最低金額を分離
    例: 'お1人様5,000円割引お1人様の旅行代金30,000円以上'
        → ('お1人様5,000円割引', '3万円以上')
    """
    # 割引部分を抽出
    match = re.search(r"(.*?(?:割引|％OFF|%OFF))", disc)
    if not match:
        return disc, ""
    discount_part = match.group(1)
    remainder = disc[match.end():]
    # 最低金額を抽出
    min_match = re.search(r"(\d[\d,]+)円以上", remainder)
    if min_match:
        amount_str = min_match.group(1).replace(",", "")
        amount = int(amount_str)
        if amount >= 10000:
            man = amount // 10000
            return discount_part, f"{man}万円以上"
        else:
            return discount_part, f"{amount:,}円以上"
    return discount_part, ""


# ---------------------------------------------------------------------------
# List Item Generation
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
            # 複数コード → ネストされたサブリストを生成
            code_sub_items = []
            for cc in codes:
                code = cc.get("code", "")
                cond = cc.get("condition", "")
                disc = cc.get("discount", "")
                # 条件を整形、割引から最低金額を分離
                cleaned_cond = clean_condition(cond) if cond else ""
                discount_amount, minimum = extract_discount_and_minimum(disc) if disc else ("", "")
                # 条件＋最低金額を結合
                cond_with_min = cleaned_cond
                if minimum and cond_with_min:
                    cond_with_min = f"{cond_with_min}・{minimum}"
                elif minimum:
                    cond_with_min = minimum
                # 説明テキスト: 条件→割引額
                desc_parts = []
                if cond_with_min:
                    desc_parts.append(html_escape(cond_with_min))
                if discount_amount:
                    desc_parts.append(html_escape(discount_amount))
                desc = "→".join(desc_parts) if desc_parts else ""
                if desc:
                    code_sub_items.append(
                        f"<strong>{html_escape(code)}</strong>（{desc}）"
                    )
                else:
                    code_sub_items.append(f"<strong>{html_escape(code)}</strong>")

            # ネストリストHTML構築
            nested_items_html = ""
            for item in code_sub_items:
                nested_items_html += (
                    f"<!-- wp:list-item -->\n"
                    f"<li>{item}</li>\n"
                    f"<!-- /wp:list-item -->\n\n"
                )
            nested_list_html = (
                "<!-- wp:list -->\n"
                f'<ul class="wp-block-list">{nested_items_html}</ul>\n'
                "<!-- /wp:list -->"
            )
            sub_items.append(f"クーポンコード：{nested_list_html}")

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


def generate_section_html(section_def: dict, coupons: list, config: dict) -> str:
    """セクション定義dictに基づき、見出し + リストを生成"""
    active = [c for c in coupons if c.get("stock_status") == "配布中"]
    if not active:
        return ""

    display_name = section_def["display_name"]
    heading_type = section_def["heading_type"]
    list_type = section_def["list_type"]
    list_class = section_def["list_class"]
    h3_id = section_def["id"]

    lines = []

    # --- 見出し部分 ---
    if heading_type == "h3":
        id_attr = f' id="{h3_id}"' if h3_id else ""
        lines.append('<!-- wp:heading {"level":3,"className":"is-style-section_ttl"} -->')
        lines.append(
            f'<h3 class="wp-block-heading is-style-section_ttl"{id_attr}>'
            f"<strong>{display_name}</strong></h3>"
        )
        lines.append("<!-- /wp:heading -->")
        lines.append("")
    elif heading_type == "p":
        lines.append("<!-- wp:paragraph -->")
        lines.append(f"<p><strong>{display_name}</strong></p>")
        lines.append("<!-- /wp:paragraph -->")
        lines.append("")

    # --- リスト部分 ---
    class_attr = f" {list_class}" if list_class else ""

    if list_type == "ol":
        json_parts = ['"ordered":true']
        if list_class:
            json_parts.append(f'"className":"{list_class}"')
        lines.append(f'<!-- wp:list {{{",".join(json_parts)}}} -->')
        lines.append(f'<ol class="wp-block-list{class_attr}">')
    else:
        if list_class:
            lines.append(f'<!-- wp:list {{"className":"{list_class}"}} -->')
        else:
            lines.append("<!-- wp:list -->")
        lines.append(f'<ul class="wp-block-list{class_attr}">')

    for coupon in active:
        lines.append(generate_coupon_list_item(coupon, config))
        lines.append("")

    if list_type == "ol":
        lines.append("</ol>")
    else:
        lines.append("</ul>")
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
    """HIS クーポン記事と同一のタブ構造HTMLを生成"""
    # セクション振り分け
    all_section_keys = (
        [s["key"] for s in OVERSEAS_SECTIONS]
        + [s["key"] for s in DOMESTIC_SECTIONS]
    )
    sections = {k: [] for k in all_section_keys}
    for coupon in coupons:
        section = get_section(coupon.get("category", ""), coupon.get("title", ""))
        if section is None:
            continue
        if section in sections:
            sections[section].append(coupon)

    # CTA ボタン設定
    cta_config = config.get("cta_buttons", {}).get("main", {})
    cta_url = cta_config.get("url", "https://t.afi-b.com/visit.php?a=Q10113i-m6912001_H&p=X653459L")
    cta_pixel = cta_config.get("pixel", "https://t.afi-b.com/lead/Q10113i/X653459L/m6912001_H")
    cta_text = cta_config.get("text", "HISクーポンを一覧を見る→")

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

    for sec_def in OVERSEAS_SECTIONS:
        sec_html = generate_section_html(sec_def, sections.get(sec_def["key"], []), config)
        if sec_html:
            lines.append(sec_html)
            lines.append("")

    # 海外CTA
    lines.append(
        generate_cta_button(cta_url, cta_text, cta_pixel)
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

    for sec_def in DOMESTIC_SECTIONS:
        sec_html = generate_section_html(sec_def, sections.get(sec_def["key"], []), config)
        if sec_html:
            lines.append(sec_html)
            lines.append("")

    # 国内CTA
    lines.append(
        generate_cta_button(cta_url, cta_text, cta_pixel)
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
