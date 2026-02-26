#!/usr/bin/env python3
"""
JTB クーポン JSON → SWELL WordPress ブロック HTML 自動生成スクリプト v4

Jinja2 テンプレートで SWELL ブロック構造を管理し、
Python 側は ViewModel 構築・分類・バリデーションに専念する。

Usage:
    python generate_jtb_html.py

Output:
    html_output/jtb_coupons.html
"""

import glob
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime
from html import escape

import jinja2

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config", "jtb_affiliate_links.json")
JTB_DATA_DIR = os.path.join(SCRIPT_DIR, "jtb_coupon_data")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "html_output")
TEMPLATE_DIR = os.path.join(SCRIPT_DIR, "templates")

TAB_ID = "b3c7e9f1"


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
def build_aff_url(detail_url, category, config):
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
    target = detail_url + sep + utm
    encoded = urllib.parse.quote(target, safe="")
    return f"//ck.jp.ap.valuecommerce.com/servlet/referral?sid={sid}&pid={pid}&vc_url={encoded}"


def build_pixel(category, config):
    """トラッキングピクセル HTML を生成"""
    vc = config["valuecommerce"]
    sid = vc["sid"]
    pid = vc["overseas_pid"] if category == "海外" else vc["domestic_pid"]
    return (
        f'<img src="//ad.jp.ap.valuecommerce.com/servlet/gifbanner'
        f'?sid={sid}&amp;pid={pid}" height="1" width="0" border="0">'
    )


# ---------------------------------------------------------------------------
# Classification Keywords & Two-pass Logic
# ---------------------------------------------------------------------------
HOTEL_KW = [
    "モントレ", "リッチモンド", "メルキュール", "プリンス",
    "ORIX", "ルートイン", "ソラーレ", "東急ホテル",
    "大江戸温泉", "共立リゾート", "ヒルトン", "星野リゾート",
    "マリオット", "APA", "ドーミーイン", "三井ガーデン",
]
WEST_KW = [
    "岡山", "広島", "山口", "鳥取", "島根", "徳島", "香川",
    "愛媛", "高知", "福岡", "佐賀", "長崎", "熊本", "大分",
    "宮崎", "鹿児島", "沖縄", "九州", "四国", "山陽",
    "離島", "石垣", "宮古", "竹富", "西表", "小浜",
    "ラーケーション", "西日本", "小豆島",
]
KANTO_KW = [
    "東京", "横浜", "神奈川", "関東", "舞浜", "新浦安", "幕張",
    "八丈島", "ディズニー", "房総", "千葉", "伊豆箱根",
    "とっておきのお部屋", "栃木", "群馬", "茨城", "埼玉",
]
CHUBU_KW = [
    "新潟", "山梨", "長野", "草津", "四万", "伊香保",
    "下田", "静岡", "信州", "甲信越", "北陸",
    "にいがた", "中部",
]
HOKKAIDO_TOHOKU_KW = [
    "北海道", "道北", "道南", "道東", "道央",
    "東北", "青森", "秋田", "岩手", "山形", "宮城", "福島",
    "スキー", "日本の旬 東北", "日本の旬　東北",
]
KANSAI_KW = [
    "京都", "大阪", "兵庫", "滋賀", "奈良", "和歌山",
    "ユニバーサル", "USJ", "近江", "びわ湖",
]


def kw_match(text, keywords, safe_kyoto=True):
    """Check if text contains any keyword. safe_kyoto avoids 京都 matching 東京都."""
    for kw in keywords:
        if kw in text:
            if safe_kyoto and kw == "京都" and "東京" in text:
                continue
            return True
    return False


def classify_domestic(coupon):
    """国内クーポンをセクションに分類（two-pass: title→area fallback）"""
    title = coupon["title"]
    area = coupon.get("area", "")

    if "hpnew" in coupon["id"] or "新規会員" in title:
        return "first_time"
    if kw_match(title, HOTEL_KW):
        return "hotel"

    # Pass 1: Title-only
    if kw_match(title, WEST_KW):
        return "west"
    if kw_match(title, KANTO_KW):
        return "kanto"
    if kw_match(title, CHUBU_KW):
        return "chubu"
    if kw_match(title, HOKKAIDO_TOHOKU_KW):
        return "hokkaido_tohoku"
    if kw_match(title, KANSAI_KW):
        return "kansai"

    # Pass 2: Area-only fallback
    if kw_match(area, KANTO_KW):
        return "kanto"
    if kw_match(area, HOKKAIDO_TOHOKU_KW):
        return "hokkaido_tohoku"
    if kw_match(area, CHUBU_KW):
        return "chubu"
    if kw_match(area, WEST_KW):
        return "west"
    if kw_match(area, KANSAI_KW):
        return "kansai"

    return "zenkoku"


def classify_overseas(coupon):
    """海外クーポンをタイプ別に分類"""
    t = coupon.get("type", "")
    if "オプショナル" in t:
        return "optional"
    if "海外航空券＋ホテル" in t:
        return "air_hotel"
    if "海外航空券" in t:
        return "air"
    return "tour"


# ---------------------------------------------------------------------------
# ViewModel Construction
# ---------------------------------------------------------------------------
def period_label(coupon):
    t = coupon.get("type", "")
    if t == "ツアー" or t == "海外ツアー":
        return "対象出発期間"
    return "対象宿泊期間"


def build_coupon_vm(coupon, config):
    """クーポンデータ → テンプレート用 dict（全フィールド前処理済み）"""
    aff_url_raw = build_aff_url(coupon["detail_url"], coupon["category"], config)

    # コード＋パスワード行
    dd = coupon.get("detail_data", {})
    codes = dd.get("coupon_codes", [])
    pws = dd.get("passwords", [])
    code_line = ""
    if codes or pws:
        parts = []
        if codes:
            parts.append(f'クーポンコード：<strong>{escape(", ".join(codes))}</strong>')
        if pws:
            parts.append(f'パスワード：<strong>{escape(", ".join(pws))}</strong>')
        code_line = "\u3000".join(parts)  # 全角スペース区切り

    return {
        "title": escape(coupon["title"]),
        "discount": escape(coupon["discount"]),
        "aff_url": escape(aff_url_raw, quote=True),
        "pixel": build_pixel(coupon["category"], config),
        "booking_period": escape(coupon.get("booking_period", "")),
        "stay_period": escape(coupon.get("stay_period", "")),
        "period_label": period_label(coupon),
        "code_line": code_line,
    }


def build_url_pair(url_raw, pixel_html=""):
    """URLの2形式 + ピクセルHTMLを返す"""
    return {
        "url_html": escape(url_raw, quote=True),
        "url_json": url_raw.replace("&", "\\u0026"),
        "pixel": pixel_html,
    }


def build_viewmodels(dom_sections, ovs_sections, config, filename):
    """分類済みセクション → テンプレートコンテキストを構築"""

    def build_section_coupons(coupons):
        return [build_coupon_vm(c, config) for c in coupons]

    # 国内エリアセクション（動的リスト）
    area_defs = [
        ("hokkaido_tohoku", "北海道・東北エリア", ""),
        ("kanto", "関東エリア", "kanto"),
        ("chubu", "甲信越・中部・東海エリア", ""),
        ("kansai", "関西エリア", "kansai"),
        ("west", "中国・四国・九州・沖縄エリア", ""),
    ]
    area_sections = []
    for key, label, html_id in area_defs:
        if dom_sections[key]:
            area_sections.append({
                "label": label,
                "id": html_id,
                "coupons": build_section_coupons(dom_sections[key]),
            })

    # 初回限定
    first_time = None
    first_time_btn = {"url_html": "", "url_json": ""}
    if dom_sections["first_time"]:
        first_time = {
            "coupons": build_section_coupons(dom_sections["first_time"]),
        }
        ft_url = build_aff_url(
            dom_sections["first_time"][0]["detail_url"], "国内", config
        )
        first_time_btn = build_url_pair(ft_url)

    # CTA URLs（ピクセル付き）
    dom_cta_raw = build_aff_url(
        "https://www.jtb.co.jp/myjtb/campaign/coupon/", "国内", config
    )
    dom_pixel = build_pixel("国内", config)
    ovs_cta_raw = build_aff_url(
        "https://www.jtb.co.jp/myjtb/campaign/coupon/kaigaicoupon/", "海外", config
    )
    ovs_pixel = build_pixel("海外", config)

    return {
        "tab_id": TAB_ID,
        "data_filename": filename,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        # 国内
        "first_time": first_time,
        "first_time_btn": first_time_btn,
        "sections": {
            "zenkoku": build_section_coupons(dom_sections["zenkoku"]),
            "hotel": build_section_coupons(dom_sections["hotel"]),
        },
        "area_sections": area_sections,
        "domestic_cta": build_url_pair(dom_cta_raw, dom_pixel),
        # 海外
        "overseas_sections": {
            "tour": build_section_coupons(ovs_sections["tour"]),
            "air_hotel": build_section_coupons(ovs_sections["air_hotel"]),
            "air": build_section_coupons(ovs_sections["air"]),
            "optional": build_section_coupons(ovs_sections["optional"]),
        },
        "overseas_cta": build_url_pair(ovs_cta_raw, ovs_pixel),
    }


# ---------------------------------------------------------------------------
# Jinja2 Rendering
# ---------------------------------------------------------------------------
def create_jinja_env():
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATE_DIR),
        autoescape=False,
        keep_trailing_newline=True,
        lstrip_blocks=True,
        trim_blocks=True,
    )


def render_html(context):
    """ViewModel コンテキストから最終 HTML をレンダリング"""
    env = create_jinja_env()

    # 国内タブ・海外タブを個別にレンダリングしてからページに埋め込む
    dom_tmpl = env.get_template("jtb/domestic_tab.html.j2")
    ovs_tmpl = env.get_template("jtb/overseas_tab.html.j2")
    page_tmpl = env.get_template("jtb/page.html.j2")

    domestic_html = dom_tmpl.render(
        first_time=context["first_time"],
        first_time_btn=context["first_time_btn"],
        sections=context["sections"],
        area_sections=context["area_sections"],
        domestic_cta=context["domestic_cta"],
    )

    overseas_html = ovs_tmpl.render(
        sections=context["overseas_sections"],
        overseas_cta=context["overseas_cta"],
    )

    return page_tmpl.render(
        tab_id=context["tab_id"],
        data_filename=context["data_filename"],
        generated_at=context["generated_at"],
        domestic_html=domestic_html,
        overseas_html=overseas_html,
    )


# ---------------------------------------------------------------------------
# HTML Validation
# ---------------------------------------------------------------------------
def validate_block_comments(html):
    """<!-- wp:xxx --> と <!-- /wp:xxx --> の対応を検証"""
    errors = []
    stack = []

    for match in re.finditer(r"<!-- (/?)wp:([a-z0-9/_-]+).*?-->", html):
        full = match.group(0)
        is_close = match.group(1) == "/"
        block_name = match.group(2)

        # Self-closing blocks (e.g., <!-- wp:loos/post-link {...} /-->)
        if full.rstrip().endswith("/-->"):
            continue

        if is_close:
            if not stack:
                errors.append(
                    f"閉じブロック <!-- /wp:{block_name} --> に対応する開始ブロックなし"
                )
            elif stack[-1] != block_name:
                errors.append(
                    f"ブロック不整合: <!-- /wp:{block_name} --> が出現、"
                    f"期待は <!-- /wp:{stack[-1]} -->"
                )
                stack.pop()
            else:
                stack.pop()
        else:
            stack.append(block_name)

    for remaining in stack:
        errors.append(f"閉じられていないブロック: <!-- wp:{remaining} -->")

    return errors


def validate_block_json(html):
    """ブロックコメント内の JSON 属性が有効か検証"""
    errors = []
    for match in re.finditer(r"<!-- wp:[a-z0-9/_-]+ ({.*?}) (?:/)?-->", html):
        json_str = match.group(1)
        try:
            json.loads(json_str)
        except json.JSONDecodeError as e:
            errors.append(f"JSON構文エラー: {json_str[:80]}... → {e}")
    return errors


def validate_tag_balance(html):
    """主要HTMLタグの開閉バランスを検証"""
    errors = []
    for tag in ["ol", "ul", "li", "div", "details", "p", "h3"]:
        opens = len(re.findall(rf"<{tag}[\s>]", html))
        closes = len(re.findall(rf"</{tag}>", html))
        if opens != closes:
            errors.append(f"<{tag}> 開閉不一致: 開{opens} / 閉{closes}")
    return errors


def validate_html(html):
    """全バリデーションを実行し、エラーリストを返す"""
    errors = []
    errors.extend(validate_block_comments(html))
    errors.extend(validate_block_json(html))
    errors.extend(validate_tag_balance(html))
    return errors


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    coupons, filename = load_latest_coupons()
    config = load_config()

    # 配布中のみフィルタ
    active = [c for c in coupons if c.get("stock_status") == "配布中"]
    domestic = [c for c in active if c.get("category") == "国内"]
    overseas = [c for c in active if c.get("category") == "海外"]

    print(f"📊 {len(active)} 件の配布中クーポンを読み込み ({filename})")

    # --- 分類 ---
    dom_sections = {
        "first_time": [], "zenkoku": [], "hotel": [],
        "hokkaido_tohoku": [], "kanto": [], "chubu": [],
        "kansai": [], "west": [],
    }
    for c in domestic:
        dom_sections[classify_domestic(c)].append(c)

    ovs_sections = {"tour": [], "air_hotel": [], "air": [], "optional": []}
    for c in overseas:
        ovs_sections[classify_overseas(c)].append(c)

    # --- 統計表示 ---
    labels = {
        "first_time": "初回限定", "zenkoku": "全国共通",
        "hotel": "チェーンホテル", "hokkaido_tohoku": "北海道・東北",
        "kanto": "関東", "chubu": "甲信越・中部・東海",
        "kansai": "関西", "west": "中国・四国・九州・沖縄",
    }
    print("\n  【国内】")
    for key, label in labels.items():
        if dom_sections[key]:
            ids = [c["id"] for c in dom_sections[key]]
            print(f"    {label}: {len(dom_sections[key])} 件 → {', '.join(ids)}")

    ovs_labels = {
        "tour": "ツアー", "air_hotel": "航空券+ホテル",
        "air": "航空券", "optional": "オプショナル",
    }
    print("  【海外】")
    for key, label in ovs_labels.items():
        if ovs_sections[key]:
            print(f"    {label}: {len(ovs_sections[key])} 件")

    dom_total = sum(len(v) for v in dom_sections.values())
    ovs_total = sum(len(v) for v in ovs_sections.values())
    print(f"\n  合計: 国内 {dom_total} + 海外 {ovs_total} = {dom_total + ovs_total}")

    # --- ViewModel 構築 → レンダリング ---
    context = build_viewmodels(dom_sections, ovs_sections, config, filename)
    html = render_html(context)

    # --- バリデーション ---
    errors = validate_html(html)
    if errors:
        print("\n⚠️  HTMLバリデーションエラー:", file=sys.stderr)
        for e in errors:
            print(f"  ❌ {e}", file=sys.stderr)
        print(f"\n  計 {len(errors)} 件のエラー", file=sys.stderr)
    else:
        print("\n✅ HTMLバリデーション: エラーなし")

    # --- 出力 ---
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    output_path = os.path.join(OUTPUT_DIR, "jtb_coupons.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"✅ 出力: {output_path}")

    # エラーがある場合は非ゼロで終了（CI検出用）
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
