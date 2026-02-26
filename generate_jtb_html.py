#!/usr/bin/env python3
"""
JTB クーポン JSON → SWELL WordPress ブロック HTML 自動生成スクリプト

記事フォーマット（balloon_box2 サブセクション・初回限定アコーディオン・割引額表示）に準拠。
ValueCommerce アフィリエイトリンクを detail_url から自動構築。

Usage:
    python generate_jtb_html.py

Output:
    html_output/jtb_coupons.html
"""

import glob
import json
import os
import re
from datetime import datetime
from urllib.parse import quote

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config", "jtb_affiliate_links.json")
JTB_DATA_DIR = os.path.join(SCRIPT_DIR, "jtb_coupon_data")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "html_output")

# ---------------------------------------------------------------------------
# Section Definitions
# ---------------------------------------------------------------------------
# spacer_before: True ならセクション描画前に 50px スペーサーを挿入
DOMESTIC_SECTIONS = [
    {"name": "全国共通クーポン", "spacer_before": False},
    {"name": "チェーンホテル限定クーポン", "spacer_before": True},
    {"name": "北海道・東北エリア", "spacer_before": True},
    {"name": "関東エリア", "spacer_before": False},
    {"name": "甲信越・中部・東海エリア", "spacer_before": False},
    {"name": "関西エリア", "spacer_before": False},
    {"name": "中国・四国・九州・沖縄エリア", "spacer_before": False},
]

OVERSEAS_SECTIONS = [
    {"name": "海外ツアー（ルックJTB MySTYLE）", "spacer_before": False},
    {"name": "海外航空券＋ホテル", "spacer_before": True},
    {"name": "海外航空券", "spacer_before": False},
    {"name": "海外オプショナルツアー", "spacer_before": False},
    {"name": "U-29旅（海外）", "spacer_before": True},
]

# 初回限定セクションに振り分けるキーワード（title に含まれるか）
FIRST_TIME_KEYWORDS = ["初回", "新規会員"]

# balloon_box2 の id 属性（一部セクションのみ）
SECTION_IDS = {
    "関東エリア": "kanto",
    "関西エリア": "kansai",
}


# ---------------------------------------------------------------------------
# Data Loading
# ---------------------------------------------------------------------------
def load_latest_coupons():
    """最新の JTB クーポン JSON を読み込む"""
    pattern = os.path.join(JTB_DATA_DIR, "coupons_*.json")
    files = sorted(glob.glob(pattern))
    if not files:
        raise FileNotFoundError(f"No coupon files found in {JTB_DATA_DIR}")
    latest = files[-1]
    with open(latest, "r", encoding="utf-8") as f:
        return json.load(f), os.path.basename(latest)


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Affiliate URL Construction
# ---------------------------------------------------------------------------
def build_affiliate_url(detail_url: str, category: str, config: dict) -> str:
    """detail_url から ValueCommerce アフィリエイトURLを構築"""
    vc = config["valuecommerce"]
    sid = vc["sid"]

    if category == "海外":
        pid = vc["overseas_pid"]
        utm = vc["overseas_utm"]
    else:
        pid = vc["domestic_pid"]
        utm = vc["domestic_utm"]

    sep = "&" if "?" in detail_url else "?"
    target_url = f"{detail_url}{sep}{utm}"
    encoded_url = quote(target_url, safe="")

    return f"//ck.jp.ap.valuecommerce.com/servlet/referral?sid={sid}&amp;pid={pid}&amp;vc_url={encoded_url}"


def build_tracking_pixel(category: str, config: dict) -> str:
    """トラッキングピクセル HTML を生成"""
    vc = config["valuecommerce"]
    sid = vc["sid"]
    pid = vc["overseas_pid"] if category == "海外" else vc["domestic_pid"]
    return (
        f'<img src="//ad.jp.ap.valuecommerce.com/servlet/gifbanner'
        f'?sid={sid}&amp;pid={pid}" height="1" width="0" border="0">'
    )


# ---------------------------------------------------------------------------
# Section Classification
# ---------------------------------------------------------------------------
def is_first_time_coupon(coupon: dict) -> bool:
    """初回限定クーポンかどうか判定"""
    title = coupon.get("title", "")
    return any(kw in title for kw in FIRST_TIME_KEYWORDS)


def classify_domestic_section(coupon: dict, config: dict) -> str:
    """国内クーポンをセクションに分類（JR セクション廃止 → 全国共通 or エリアへ）"""
    title = coupon.get("title", "")
    area = coupon.get("area", "")

    # 1. ホテルチェーン判定（タイトルベース）
    for kw in config.get("hotel_chain_keywords", []):
        if kw in title:
            return "チェーンホテル限定クーポン"

    # 2. タイトルキーワードでエリア判定
    kw_map = config.get("title_keyword_to_section", {})
    for kw, section in kw_map.items():
        if kw in title:
            return section

    # 3. area フィールドで都道府県判定
    area_map = config.get("area_to_section", {})
    for pref, section in area_map.items():
        if pref in area:
            return section

    # 4. デフォルト：全国共通
    return "全国共通クーポン"


def classify_overseas_section(coupon: dict, config: dict) -> str:
    """海外クーポンをセクションに分類"""
    title = coupon.get("title", "")
    kw_map = config.get("overseas_title_to_section", {})

    for kw, section in kw_map.items():
        if kw in title:
            return section

    if "航空券" in title and "ホテル" in title:
        return "海外航空券＋ホテル"
    if "航空券" in title:
        return "海外航空券"
    return "海外ツアー（ルックJTB MySTYLE）"


# ---------------------------------------------------------------------------
# SWELL Block HTML Generation
# ---------------------------------------------------------------------------
def generate_coupon_list_item(coupon: dict, config: dict) -> str:
    """1つのクーポンを wp:list-item として生成（割引額付き）"""
    title = coupon.get("title", "")
    category = coupon.get("category", "国内")
    detail_url = coupon.get("detail_url", "")
    booking = coupon.get("booking_period", "")
    stay = coupon.get("stay_period", "")
    discount = coupon.get("discount", "")
    detail = coupon.get("detail_data", {})
    codes = detail.get("coupon_codes", [])
    passwords = detail.get("passwords", [])

    # アフィリエイトURL構築
    aff_url = build_affiliate_url(detail_url, category, config) if detail_url else ""
    pixel = build_tracking_pixel(category, config)

    # メインリンク
    if aff_url:
        link_html = f'<a href="{aff_url}" rel="nofollow">{pixel}{title}</a>'
    else:
        link_html = f"<strong>{title}</strong>"

    # 割引額テキスト（タイトル直後に括弧で表示）
    discount_text = ""
    if discount:
        discount_text = f"（<strong>{discount}</strong>）"

    # サブリスト（期間・コード・パスワード）
    sub_items = []
    if booking:
        sub_items.append(f"<li>予約期間：{booking}</li>")
    if stay:
        if "宿泊" in coupon.get("type", ""):
            label = "対象宿泊期間"
        elif category == "海外":
            label = "対象出発期間"
        else:
            label = "対象出発期間"
        sub_items.append(f"<li>{label}：{stay}</li>")

    # クーポンコード＋パスワード
    if codes:
        code_str = ", ".join(codes)
        pass_str = ", ".join(passwords) if passwords else ""
        code_line = f"クーポンコード：<strong>{code_str}</strong>"
        if pass_str:
            code_line += f"　パスワード：<strong>{pass_str}</strong>"
        sub_items.append(f"<li>{code_line}</li>")

    sub_list = ""
    if sub_items:
        # ターゲットHTML準拠: 各 list-item の間に空行、最後は </ul> に密着
        parts = []
        for i, item in enumerate(sub_items):
            parts.append(f"<!-- wp:list-item -->\n{item}\n<!-- /wp:list-item -->")
        items_html = "\n\n".join(parts)
        sub_list = (
            "<!-- wp:list -->\n"
            f'<ul class="wp-block-list">{items_html}</ul>\n'
            "<!-- /wp:list -->"
        )

    return (
        "<!-- wp:list-item -->\n"
        f"<li><strong>→{link_html}</strong>{discount_text}"
        f"{sub_list}</li>\n"
        "<!-- /wp:list-item -->"
    )


def generate_spacer(height: str = "50px") -> str:
    """スペーサーブロックを生成"""
    return (
        f'<!-- wp:spacer {{"height":"{height}"}} -->\n'
        f'<div style="height:{height}" aria-hidden="true" class="wp-block-spacer"></div>\n'
        f"<!-- /wp:spacer -->"
    )


def generate_balloon_heading(text: str, section_id: str = "") -> str:
    """balloon_box2 段落見出しを生成"""
    id_attr = f' id="{section_id}"' if section_id else ""
    return (
        '<!-- wp:paragraph {"className":"is-style-balloon_box2"} -->\n'
        f'<p class="is-style-balloon_box2"{id_attr}><strong>{text}</strong></p>\n'
        "<!-- /wp:paragraph -->"
    )


def generate_h3_heading(text: str, h3_id: str = "") -> str:
    """h3 セクション見出しを生成"""
    id_attr = f' id="{h3_id}"' if h3_id else ""
    return (
        '<!-- wp:heading {"level":3,"className":"is-style-section_ttl"} -->\n'
        f'<h3 class="wp-block-heading is-style-section_ttl"{id_attr}>'
        f"{text}</h3>\n"
        "<!-- /wp:heading -->"
    )


def generate_ordered_list(coupons: list, config: dict) -> str:
    """ol リスト（-list-under-dashed）を生成"""
    if not coupons:
        return ""

    # ターゲットHTML準拠: <ol> と最初の list-item を密着
    items = []
    for coupon in coupons:
        items.append(generate_coupon_list_item(coupon, config))

    items_html = "\n\n".join(items)

    return (
        '<!-- wp:list {"ordered":true,"className":"-list-under-dashed"} -->\n'
        f'<ol class="wp-block-list -list-under-dashed">{items_html}</ol>\n'
        "<!-- /wp:list -->"
    )


def generate_first_time_section(coupons: list, config: dict) -> str:
    """初回限定セクション（h3 + ol + アコーディオン）を生成"""
    if not coupons:
        return ""

    lines = []

    # h3 見出し
    lines.append(generate_h3_heading("初回限定", "first-time"))
    lines.append("")

    # クーポンリスト
    lines.append(generate_ordered_list(coupons, config))
    lines.append("")

    # アコーディオン（初回限定の利用条件 - 固定コンテンツ）
    # 初回限定クーポンの detail_url からCTAボタン用URLを生成
    first_coupon = coupons[0]
    cta_url = ""
    if first_coupon.get("detail_url"):
        cta_url = build_affiliate_url(
            first_coupon["detail_url"], "国内", config
        )

    accordion_html = _build_first_time_accordion(cta_url)
    lines.append(accordion_html)
    lines.append("")

    return "\n".join(lines)


def _build_first_time_accordion(cta_url: str) -> str:
    """初回限定クーポンの注意事項アコーディオン（固定コンテンツ）"""
    cta_button = ""
    if cta_url:
        # JSON属性では & を使い、HTML属性では &amp; を使う
        cta_url_json = cta_url.replace("&amp;", "&")
        cta_button = (
            f'<!-- wp:loos/button {{"hrefUrl":"{cta_url_json}","isNewTab":true,'
            f'"color":"blue","btnSize":"l","className":"is-style-btn_normal"}} -->\n'
            f'<div class="swell-block-button blue_ -size-l is-style-btn_normal">'
            f'<a href="{cta_url}" target="_blank" rel="noopener noreferrer" '
            f'class="swell-block-button__link"><span>【初回限定】 JTBクーポン ページ→</span></a></div>\n'
            f"<!-- /wp:loos/button -->"
        )

    return f"""<!-- wp:loos/accordion {{"className":"is-style-default"}} -->
<div class="swell-block-accordion is-style-default"><!-- wp:loos/accordion-item -->
<details class="swell-block-accordion__item" data-swl-acc="wrapper"><summary class="swell-block-accordion__title" data-swl-acc="header"><span class="swell-block-accordion__label"><strong>注意：初回割引クーポンコードを使う条件</strong></span><span class="swell-block-accordion__icon c-switchIconBtn" data-swl-acc="icon" aria-hidden="true" data-opened="false"><i class="__icon--closed icon-caret-down"></i><i class="__icon--opened icon-caret-up"></i></span></summary><div class="swell-block-accordion__body" data-swl-acc="body"><!-- wp:group {{"className":"is-style-bg_stripe","layout":{{"type":"constrained"}}}} -->
<div class="wp-block-group is-style-bg_stripe"><!-- wp:list {{"ordered":true}} -->
<ol class="wp-block-list"><!-- wp:list-item -->
<li><strong>対象者:</strong><!-- wp:list -->
<ul class="wp-block-list"><!-- wp:list-item -->
<li><strong>JTBトラベルメンバーに新規会員登録</strong>した方限定です。</li>
<!-- /wp:list-item -->

<!-- wp:list-item -->
<li>JTBのウェブサイトでの<strong>初回予約</strong>にのみ利用できます。</li>
<!-- /wp:list-item --></ul>
<!-- /wp:list --></li>
<!-- /wp:list-item -->

<!-- wp:list-item -->
<li><strong>利用条件:</strong><!-- wp:list -->
<ul class="wp-block-list"><!-- wp:list-item -->
<li>JTBの<strong>ウェブサイトからの予約限定</strong>です。店舗や電話での予約には使えません。</li>
<!-- /wp:list-item -->

<!-- wp:list-item -->
<li>対象となるプランは「JTB宿泊プラン」「るるぶトラベルプラン」「JR＋宿泊プラン」「飛行機＋宿泊プラン」など幅広く利用可能です。</li>
<!-- /wp:list-item -->

<!-- wp:list-item -->
<li>お一人様1回限りの利用となります。</li>
<!-- /wp:list-item -->

<!-- wp:list-item -->
<li>併用は合計10枚まで。（適応外とのクーポンとの場合は併用できない。）</li>
<!-- /wp:list-item -->

<!-- wp:list-item -->
<li>国内の宿泊とツアーのみでしか使えない。（海外は利用不可）</li>
<!-- /wp:list-item --></ul>
<!-- /wp:list --></li>
<!-- /wp:list-item -->

<!-- wp:list-item -->
<li><strong>他のキャンペーンとの併用がお得:</strong><!-- wp:list -->
<ul class="wp-block-list"><!-- wp:list-item -->
<li>JTBでは、初回クーポンと併用できる「初めての旅行＆エントリーでポイントキャンペーン」などを実施している場合があります。エントリーするだけでポイントがもらえるため、さらにお得になります。</li>
<!-- /wp:list-item --></ul>
<!-- /wp:list --></li>
<!-- /wp:list-item --></ol>
<!-- /wp:list -->

<!-- wp:paragraph -->
<p><span class="swl-fz u-fz-xs">※海外の初回限定の割引クーポンコードはありません。→<a href="#kaigai">海外JTB割引クーポンコード一覧</a></span></p>
<!-- /wp:paragraph -->

{cta_button}</div>
<!-- /wp:group --></div></details>
<!-- /wp:loos/accordion-item --></div>
<!-- /wp:loos/accordion -->"""


def generate_domestic_html(
    first_time_coupons: list,
    section_coupons: dict,
    config: dict,
) -> str:
    """国内タブの HTML を生成"""
    lines = []

    # イントロ段落（h2 なし）
    lines.append("<!-- wp:paragraph -->")
    lines.append(
        "<p>下記は、今すぐ誰でも使えるJTBの割引情報と配布中クーポンを一覧にしました。</p>"
    )
    lines.append("<!-- /wp:paragraph -->")
    lines.append("")

    # 初回限定セクション
    if first_time_coupons:
        lines.append(generate_first_time_section(first_time_coupons, config))
        lines.append("")

    # 「国内旅行」h3
    lines.append(generate_h3_heading("国内旅行"))
    lines.append("")

    # 各サブセクション（balloon_box2）
    for section_def in DOMESTIC_SECTIONS:
        name = section_def["name"]
        coupons = section_coupons.get(name, [])
        if not coupons:
            continue

        # スペーサー
        if section_def["spacer_before"]:
            lines.append(generate_spacer())
            lines.append("")

        # balloon_box2 見出し
        section_id = SECTION_IDS.get(name, "")
        lines.append(generate_balloon_heading(name, section_id))
        lines.append("")

        # クーポンリスト
        lines.append(generate_ordered_list(coupons, config))
        lines.append("")

    return "\n".join(lines)


def generate_overseas_html(
    section_coupons: dict,
    config: dict,
) -> str:
    """海外タブの HTML を生成"""
    lines = []

    # イントロ段落（h2 なし）
    lines.append("<!-- wp:paragraph -->")
    lines.append(
        "<p>下記は、今すぐ誰でも使えるJTBの海外割引情報と配布中クーポンを一覧にしました。</p>"
    )
    lines.append("<!-- /wp:paragraph -->")
    lines.append("")

    # 「海外旅行」h3
    lines.append(generate_h3_heading("海外旅行", "kaigai"))
    lines.append("")

    # 各サブセクション（balloon_box2）
    for section_def in OVERSEAS_SECTIONS:
        name = section_def["name"]
        coupons = section_coupons.get(name, [])
        if not coupons:
            continue

        if section_def["spacer_before"]:
            lines.append(generate_spacer())
            lines.append("")

        lines.append(generate_balloon_heading(name))
        lines.append("")

        lines.append(generate_ordered_list(coupons, config))
        lines.append("")

    return "\n".join(lines)


def generate_cta_button(url: str, text: str, pixel_html: str = "") -> str:
    """CTA ボタンブロックを生成

    url は &amp; エスケープ済みで渡されることを前提とする。
    JSON コメント内の hrefUrl には &amp; → & に戻したものを使う。
    """
    # JSON属性では & を使い、HTML属性では &amp; を使う
    url_for_json = url.replace("&amp;", "&")

    lines = []
    lines.append(
        '<!-- wp:paragraph {"align":"center","className":"u-mb-ctrl u-mb-0"} -->'
    )
    lines.append(
        '<p class="has-text-align-center u-mb-ctrl u-mb-0">'
        '<strong><span class="swl-fz u-fz-s">＼JTBクーポンをまとめて見るなら／</span></strong></p>'
    )
    lines.append("<!-- /wp:paragraph -->")
    lines.append("")
    lines.append(
        f'<!-- wp:loos/button {{"hrefUrl":"{url_for_json}","color":"blue",'
        f'"className":"is-style-btn_shiny u-mb-ctrl u-mb-0"}} -->'
    )
    lines.append(
        f'<div class="swell-block-button -html blue_ is-style-btn_shiny u-mb-ctrl u-mb-0">'
        f'<a href="{url}" rel="nofollow">'
        f"{pixel_html}{text}</a></div>"
    )
    lines.append("<!-- /wp:loos/button -->")
    lines.append("")
    lines.append(
        '<!-- wp:paragraph {"align":"center","className":"u-mb-ctrl u-mb-30"} -->'
    )
    lines.append(
        '<p class="has-text-align-center u-mb-ctrl u-mb-30"><strong>★誰でも使える★</strong></p>'
    )
    lines.append("<!-- /wp:paragraph -->")
    return "\n".join(lines)


def generate_tab_html(
    domestic_html: str, overseas_html: str, config: dict, tab_id: str = "b3c7e9f1"
) -> str:
    """国内/海外タブ構造全体を生成"""
    vc = config["valuecommerce"]
    sid = vc["sid"]
    dom_pid = vc["domestic_pid"]
    ovs_pid = vc["overseas_pid"]

    lines = []

    # タブコンテナ開始
    lines.append(
        f'<!-- wp:loos/tab {{"tabId":"{tab_id}","tabWidthPC":"flex-auto","tabWidthSP":"flex-auto",'
        f'"tabHeaders":["\\u003cspan class=\\u0022swl-fz u-fz-l\\u0022\\u003e\\u003cstrong\\u003eJTB国内クーポン\\u003c/strong\\u003e\\u003c/span\\u003e",'
        f'"\\u003cspan class=\\u0022swl-fz u-fz-l\\u0022\\u003e\\u003cstrong\\u003eJTB海外クーポン\\u003c/strong\\u003e\\u003c/span\\u003e"],'
        f'"className":"is-style-balloon"}} -->'
    )
    lines.append(
        f'<div class="swell-block-tab is-style-balloon" data-width-pc="flex-auto" data-width-sp="flex-auto">'
        f'<ul class="c-tabList" role="tablist">'
        f'<li class="c-tabList__item" role="presentation">'
        f'<button role="tab" class="c-tabList__button" aria-selected="true" aria-controls="tab-{tab_id}-0" data-onclick="tabControl">'
        f'<span class="swl-fz u-fz-l"><strong>JTB国内クーポン</strong></span></button></li>'
        f'<li class="c-tabList__item" role="presentation">'
        f'<button role="tab" class="c-tabList__button" aria-selected="false" aria-controls="tab-{tab_id}-1" data-onclick="tabControl">'
        f'<span class="swl-fz u-fz-l"><strong>JTB海外クーポン</strong></span></button></li>'
        f'</ul><div class="c-tabBody">'
    )

    # --- タブ0: 国内 ---
    lines.append(f'<!-- wp:loos/tab-body {{"tabId":"{tab_id}"}} -->')
    lines.append(
        f'<div id="tab-{tab_id}-0" class="c-tabBody__item" aria-hidden="false">'
    )
    lines.append(
        '<!-- wp:group {"backgroundColor":"swl-pale-04","layout":{"type":"constrained"}} -->'
    )
    lines.append(
        '<div class="wp-block-group has-swl-pale-04-background-color has-background">'
    )

    lines.append(domestic_html)

    # 国内 CTA ボタン
    dom_cta_utm = quote(
        "https://www.jtb.co.jp/myjtb/campaign/coupon/?utm_source=vcdom&utm_medium=affiliate",
        safe="",
    )
    dom_cta_url = f"//ck.jp.ap.valuecommerce.com/servlet/referral?sid={sid}&amp;pid={dom_pid}&amp;vc_url={dom_cta_utm}"
    dom_pixel = (
        f'<img src="//ad.jp.ap.valuecommerce.com/servlet/gifbanner'
        f'?sid={sid}&amp;pid={dom_pid}" height="1" width="0" border="0">'
    )
    lines.append("")
    lines.append(
        generate_cta_button(dom_cta_url, "JTB国内クーポンの一覧はこちら", dom_pixel)
    )

    lines.append("")
    lines.append("</div>")
    lines.append("<!-- /wp:group -->")
    lines.append("</div>")
    lines.append("<!-- /wp:loos/tab-body -->")

    # --- タブ1: 海外 ---
    lines.append(f'<!-- wp:loos/tab-body {{"id":1,"tabId":"{tab_id}"}} -->')
    lines.append(
        f'<div id="tab-{tab_id}-1" class="c-tabBody__item" aria-hidden="true">'
    )
    lines.append(
        '<!-- wp:group {"backgroundColor":"swl-pale-04","layout":{"type":"constrained"}} -->'
    )
    lines.append(
        '<div class="wp-block-group has-swl-pale-04-background-color has-background">'
    )

    lines.append(overseas_html)

    # 海外 CTA ボタン
    ovs_cta_utm = quote(
        "https://www.jtb.co.jp/myjtb/campaign/kaigaicoupon/?utm_source=vcdom&utm_medium=affiliate",
        safe="",
    )
    ovs_cta_url = f"//ck.jp.ap.valuecommerce.com/servlet/referral?sid={sid}&amp;pid={ovs_pid}&amp;vc_url={ovs_cta_utm}"
    ovs_pixel = (
        f'<img src="//ad.jp.ap.valuecommerce.com/servlet/gifbanner'
        f'?sid={sid}&amp;pid={ovs_pid}" height="1" width="0" border="0">'
    )
    lines.append("")
    lines.append(
        generate_cta_button(ovs_cta_url, "JTB海外クーポンの一覧はこちら", ovs_pixel)
    )

    lines.append("")
    lines.append("</div>")
    lines.append("<!-- /wp:group -->")
    lines.append("</div>")
    lines.append("<!-- /wp:loos/tab-body -->")

    # タブコンテナ終了
    lines.append("</div></div>")
    lines.append("<!-- /wp:loos/tab -->")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    coupons, filename = load_latest_coupons()
    config = load_config()

    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", filename)
    data_date = date_match.group(1) if date_match else "unknown"

    # 配布中のみフィルタ
    active = [c for c in coupons if c.get("stock_status") == "配布中"]
    print(f"📊 {len(active)} 件の配布中クーポンを読み込み ({filename})")

    # 国内・海外に分割
    domestic = [c for c in active if c.get("category") == "国内"]
    overseas = [c for c in active if c.get("category") == "海外"]

    # --- 国内: 初回限定を分離 ---
    first_time = [c for c in domestic if is_first_time_coupon(c)]
    domestic_regular = [c for c in domestic if not is_first_time_coupon(c)]

    # --- 国内セクション分類 ---
    section_names = [s["name"] for s in DOMESTIC_SECTIONS]
    domestic_sections = {s: [] for s in section_names}
    for c in domestic_regular:
        section = classify_domestic_section(c, config)
        if section in domestic_sections:
            domestic_sections[section].append(c)
        else:
            domestic_sections["全国共通クーポン"].append(c)

    # --- 海外セクション分類 ---
    overseas_section_names = [s["name"] for s in OVERSEAS_SECTIONS]
    overseas_sections = {s: [] for s in overseas_section_names}
    for c in overseas:
        section = classify_overseas_section(c, config)
        if section in overseas_sections:
            overseas_sections[section].append(c)
        else:
            overseas_sections["海外ツアー（ルックJTB MySTYLE）"].append(c)

    # 統計表示
    if first_time:
        print(f"\n  【初回限定】: {len(first_time)} 件")
    print("  【国内】")
    for name in section_names:
        items = domestic_sections[name]
        if items:
            print(f"    {name}: {len(items)} 件")
    print("  【海外】")
    for name in overseas_section_names:
        items = overseas_sections[name]
        if items:
            print(f"    {name}: {len(items)} 件")

    # --- HTML 生成 ---
    domestic_html = generate_domestic_html(first_time, domestic_sections, config)
    overseas_html = generate_overseas_html(overseas_sections, config)
    full_html = generate_tab_html(domestic_html, overseas_html, config)

    # ヘッダーコメント追加
    header = (
        f"<!-- JTB クーポン SWELL HTML（自動生成） -->\n"
        f"<!-- データ: {filename} / 生成: {datetime.now().strftime('%Y-%m-%d %H:%M')} -->\n"
    )

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    output_path = os.path.join(OUTPUT_DIR, "jtb_coupons.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(header + full_html)

    print(f"\n✅ 出力: {output_path}")
    print(
        f"   国内セクション: {sum(1 for v in domestic_sections.values() if v)} 個 "
        f"(+ 初回限定 {len(first_time)} 件)"
    )
    print(f"   海外セクション: {sum(1 for v in overseas_sections.values() if v)} 個")


if __name__ == "__main__":
    main()
