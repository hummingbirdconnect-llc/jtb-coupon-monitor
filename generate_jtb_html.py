#!/usr/bin/env python3
"""
JTB クーポン JSON → SWELL WordPress ブロック HTML 自動生成スクリプト

既存の JTB_クーポンリスト.md と同一フォーマット（リスト形式）で出力する。
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

    # detail_url に utm パラメータを追加
    sep = "&" if "?" in detail_url else "?"
    target_url = f"{detail_url}{sep}{utm}"
    encoded_url = quote(target_url, safe="")

    return f"//ck.jp.ap.valuecommerce.com/servlet/referral?sid={sid}&pid={pid}&vc_url={encoded_url}"


def build_tracking_pixel(category: str, config: dict) -> str:
    """トラッキングピクセル HTML を生成"""
    vc = config["valuecommerce"]
    sid = vc["sid"]
    pid = vc["overseas_pid"] if category == "海外" else vc["domestic_pid"]
    return (
        f'<img src="//ad.jp.ap.valuecommerce.com/servlet/gifbanner'
        f'?sid={sid}&pid={pid}" height="1" width="0" border="0">'
    )


# ---------------------------------------------------------------------------
# Section Classification
# ---------------------------------------------------------------------------
def classify_domestic_section(coupon: dict, config: dict) -> str:
    """国内クーポンをセクションに分類"""
    title = coupon.get("title", "")
    area = coupon.get("area", "")

    # 1. ホテルチェーン判定（タイトルベース）
    for kw in config.get("hotel_chain_keywords", []):
        if kw in title:
            return "ホテルチェーン限定クーポン"

    # 2. JR・鉄道判定
    for kw in config.get("jr_keywords", []):
        if kw in title:
            return "JR・鉄道利用クーポン"

    # 3. タイトルキーワードでエリア判定
    kw_map = config.get("title_keyword_to_section", {})
    for kw, section in kw_map.items():
        if kw in title:
            return section

    # 4. area フィールドで都道府県判定
    area_map = config.get("area_to_section", {})
    for pref, section in area_map.items():
        if pref in area:
            return section

    # 5. デフォルト：全国共通
    return "全国共通クーポン"


def classify_overseas_section(coupon: dict, config: dict) -> str:
    """海外クーポンをセクションに分類"""
    title = coupon.get("title", "")
    kw_map = config.get("overseas_title_to_section", {})

    for kw, section in kw_map.items():
        if kw in title:
            return section

    # デフォルト
    if "航空券" in title and "ホテル" in title:
        return "海外航空券＋ホテル"
    if "航空券" in title:
        return "海外航空券"
    return "海外ツアー（ルックJTB MySTYLE）"


# ---------------------------------------------------------------------------
# SWELL Block HTML Generation
# ---------------------------------------------------------------------------
def generate_coupon_list_item(coupon: dict, config: dict) -> str:
    """1つのクーポンを wp:list-item として生成"""
    title = coupon.get("title", "")
    category = coupon.get("category", "国内")
    detail_url = coupon.get("detail_url", "")
    booking = coupon.get("booking_period", "")
    stay = coupon.get("stay_period", "")
    detail = coupon.get("detail_data", {})
    codes = detail.get("coupon_codes", [])
    passwords = detail.get("passwords", [])

    # アフィリエイトURL構築
    aff_url = build_affiliate_url(detail_url, category, config) if detail_url else ""
    pixel = build_tracking_pixel(category, config)

    # メインリンク
    link_html = (
        f'<a href="{aff_url}" rel="nofollow">{pixel}{title}</a>'
        if aff_url
        else f"<strong>{title}</strong>"
    )

    # サブリスト（期間・コード・パスワード）
    sub_items = []
    if booking:
        label = "予約期間" if category == "国内" else "予約期間"
        sub_items.append(f"<li>{label}：{booking}</li>")
    if stay:
        label = "対象宿泊期間" if "宿泊" in coupon.get("type", "") else "対象出発期間"
        if category == "海外":
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
        sub_list = (
            "<!-- wp:list -->\n"
            '<ul class="wp-block-list">'
            + "".join(
                f"<!-- wp:list-item -->\n{item}\n<!-- /wp:list-item -->\n"
                for item in sub_items
            )
            + "</ul>\n<!-- /wp:list -->"
        )

    return (
        "<!-- wp:list-item -->\n"
        f"<li><strong>→{link_html}</strong>"
        f"{sub_list}</li>\n"
        "<!-- /wp:list-item -->"
    )


def generate_section_html(
    section_name: str, coupons: list, config: dict, ordered: bool = True
) -> str:
    """セクション（h3 + リスト）を生成"""
    if not coupons:
        return ""

    h3_ids = config.get("section_h3_ids", {})
    h3_id = h3_ids.get(section_name, "")
    id_attr = f' id="{h3_id}"' if h3_id else ""

    lines = []

    # h3
    lines.append('<!-- wp:heading {"level":3,"className":"is-style-section_ttl"} -->')
    lines.append(
        f'<h3 class="wp-block-heading is-style-section_ttl"{id_attr}>'
        f"<strong>{section_name}</strong></h3>"
    )
    lines.append("<!-- /wp:heading -->")
    lines.append("")

    # リスト開始
    if ordered:
        lines.append(
            '<!-- wp:list {"ordered":true,"className":"-list-under-dashed"} -->'
        )
        lines.append('<ol class="wp-block-list -list-under-dashed">')
    else:
        lines.append('<!-- wp:list {"className":"-list-under-dashed"} -->')
        lines.append('<ul class="wp-block-list -list-under-dashed">')

    # 各クーポン
    for coupon in coupons:
        lines.append(generate_coupon_list_item(coupon, config))
        lines.append("")

    # リスト終了
    if ordered:
        lines.append("</ol>")
    else:
        lines.append("</ul>")
    lines.append("<!-- /wp:list -->")

    return "\n".join(lines)


def generate_cta_button(url: str, text: str, pixel_html: str = "") -> str:
    """CTA ボタンブロックを生成"""
    lines = []
    lines.append('<!-- wp:paragraph {"align":"center","className":"u-mb-ctrl u-mb-0"} -->')
    lines.append(
        '<p class="has-text-align-center u-mb-ctrl u-mb-0">'
        '<strong><span class="swl-fz u-fz-s">＼JTBクーポンをまとめて見るなら／</span></strong></p>'
    )
    lines.append("<!-- /wp:paragraph -->")
    lines.append("")
    lines.append(
        f'{{"hrefUrl":"{url}","color":"blue","className":"is-style-btn_shiny u-mb-ctrl u-mb-0"}}'
    )
    # SWELL button block
    lines_btn = []
    lines_btn.append(
        f'<!-- wp:loos/button {{"hrefUrl":"{url}","color":"blue","className":"is-style-btn_shiny u-mb-ctrl u-mb-0"}} -->'
    )
    lines_btn.append(
        f'<div class="swell-block-button -html blue_ is-style-btn_shiny u-mb-ctrl u-mb-0">'
        f'<a href="{url}" rel="nofollow">'
        f"{pixel_html}{text}</a></div>"
    )
    lines_btn.append("<!-- /wp:loos/button -->")
    lines_btn.append("")
    lines_btn.append('<!-- wp:paragraph {"align":"center","className":"u-mb-ctrl u-mb-30"} -->')
    lines_btn.append(
        '<p class="has-text-align-center u-mb-ctrl u-mb-30"><strong>★誰でも使える★</strong></p>'
    )
    lines_btn.append("<!-- /wp:paragraph -->")
    return "\n".join(lines_btn)


def generate_tab_html(
    domestic_html: str, overseas_html: str, tab_id: str = "b3c7e9f1"
) -> str:
    """国内/海外タブ構造全体を生成"""
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

    # タブ0: 国内
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
    lines.append('<!-- wp:heading {"textAlign":"center"} -->')
    lines.append(
        '<h2 class="wp-block-heading has-text-align-center">'
        "【国内】JTBの誰でも使える割引クーポンコード一覧</h2>"
    )
    lines.append("<!-- /wp:heading -->")
    lines.append("")
    lines.append("<!-- wp:paragraph -->")
    lines.append(
        "<p>下記は、今すぐ誰でも使えるJTBの割引情報と配布中クーポンを一覧にしました。</p>"
    )
    lines.append("<!-- /wp:paragraph -->")
    lines.append("")

    lines.append(domestic_html)

    # 国内 CTA ボタン
    vc = {"sid": "3448423", "pid": "885714709"}
    cta_url = (
        f'//ck.jp.ap.valuecommerce.com/servlet/referral?sid={vc["sid"]}&pid={vc["pid"]}'
        f"&vc_url=https%3A%2F%2Fwww.jtb.co.jp%2Fmyjtb%2Fcampaign%2Fcoupon%2F"
        f"%3Futm_source%3Dvcdom%26utm_medium%3Daffiliate"
    )
    pixel_html = (
        f'<img src="//ad.jp.ap.valuecommerce.com/servlet/gifbanner'
        f'?sid={vc["sid"]}&pid={vc["pid"]}" height="1" width="0" border="0">'
    )
    lines.append("")
    lines.append(generate_cta_button(cta_url, "JTB国内クーポンの一覧はこちら", pixel_html))

    lines.append("")
    lines.append("</div>")
    lines.append("<!-- /wp:group -->")
    lines.append("</div>")
    lines.append("<!-- /wp:loos/tab-body -->")

    # タブ1: 海外
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
    lines.append('<!-- wp:heading {"textAlign":"center"} -->')
    lines.append(
        '<h2 class="wp-block-heading has-text-align-center">'
        "【海外】JTBの誰でも使える割引クーポンコード一覧</h2>"
    )
    lines.append("<!-- /wp:heading -->")
    lines.append("")
    lines.append("<!-- wp:paragraph -->")
    lines.append(
        "<p>下記は、今すぐ誰でも使えるJTBの海外割引情報と配布中クーポンを一覧にしました。</p>"
    )
    lines.append("<!-- /wp:paragraph -->")
    lines.append("")

    lines.append(overseas_html)

    # 海外 CTA ボタン
    vc_o = {"sid": "3448423", "pid": "885714709"}
    cta_url_o = (
        f'//ck.jp.ap.valuecommerce.com/servlet/referral?sid={vc_o["sid"]}&pid={vc_o["pid"]}'
        f"&vc_url=https%3A%2F%2Fwww.jtb.co.jp%2Fmyjtb%2Fcampaign%2Fkaigaicoupon%2F"
        f"%3Futm_source%3Dvcdom%26utm_medium%3Daffiliate"
    )
    lines.append("")
    lines.append(
        generate_cta_button(cta_url_o, "JTB海外クーポンの一覧はこちら", pixel_html)
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

    # --- 国内セクション分類 ---
    domestic_sections = {s: [] for s in config["section_order_domestic"]}
    for c in domestic:
        section = classify_domestic_section(c, config)
        if section in domestic_sections:
            domestic_sections[section].append(c)
        else:
            domestic_sections["全国共通クーポン"].append(c)

    # --- 海外セクション分類 ---
    overseas_sections = {s: [] for s in config["section_order_overseas"]}
    for c in overseas:
        section = classify_overseas_section(c, config)
        if section in overseas_sections:
            overseas_sections[section].append(c)
        else:
            overseas_sections["海外ツアー（ルックJTB MySTYLE）"].append(c)

    # 統計表示
    print("\n  【国内】")
    for name, items in domestic_sections.items():
        if items:
            print(f"    {name}: {len(items)} 件")
    print(f"  【海外】")
    for name, items in overseas_sections.items():
        if items:
            print(f"    {name}: {len(items)} 件")

    # --- HTML 生成 ---
    domestic_parts = []
    for section_name in config["section_order_domestic"]:
        section_coupons = domestic_sections.get(section_name, [])
        if section_coupons:
            domestic_parts.append(
                generate_section_html(section_name, section_coupons, config)
            )
            domestic_parts.append("")

    overseas_parts = []
    for section_name in config["section_order_overseas"]:
        section_coupons = overseas_sections.get(section_name, [])
        if section_coupons:
            overseas_parts.append(
                generate_section_html(section_name, section_coupons, config)
            )
            overseas_parts.append("")

    domestic_html = "\n".join(domestic_parts)
    overseas_html = "\n".join(overseas_parts)

    full_html = generate_tab_html(domestic_html, overseas_html)

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
    print(f"   国内セクション: {sum(1 for v in domestic_sections.values() if v)} 個")
    print(f"   海外セクション: {sum(1 for v in overseas_sections.values() if v)} 個")


if __name__ == "__main__":
    main()
