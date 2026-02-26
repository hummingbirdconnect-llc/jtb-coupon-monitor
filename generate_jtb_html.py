#!/usr/bin/env python3
"""
JTB クーポン JSON → SWELL WordPress ブロック HTML 自動生成スクリプト v3

Two-pass classification + SWELL balloon_box2 / accordion 形式で出力。
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
import urllib.parse
from datetime import datetime
from html import escape

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config", "jtb_affiliate_links.json")
JTB_DATA_DIR = os.path.join(SCRIPT_DIR, "jtb_coupon_data")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "html_output")

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
    return f'<img src="//ad.jp.ap.valuecommerce.com/servlet/gifbanner?sid={sid}&amp;pid={pid}" height="1" width="0" border="0">'


# ---------------------------------------------------------------------------
# Classification Keywords
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


# ---------------------------------------------------------------------------
# Two-pass Classification
# ---------------------------------------------------------------------------
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
# SWELL Block HTML Generation
# ---------------------------------------------------------------------------
def period_label(coupon):
    t = coupon.get("type", "")
    if t == "ツアー" or t == "海外ツアー":
        return "対象出発期間"
    return "対象宿泊期間"


def gen_item(coupon, config):
    """1つのクーポンを wp:list-item として生成"""
    aff = escape(build_aff_url(coupon["detail_url"], coupon["category"], config), quote=True)
    px = build_pixel(coupon["category"], config)
    title = escape(coupon["title"])
    disc = escape(coupon["discount"])

    subs = []
    subs.append(f'<!-- wp:list-item -->\n<li>割引額：<strong>{disc}</strong></li>\n<!-- /wp:list-item -->')

    if coupon.get("booking_period"):
        subs.append(f'<!-- wp:list-item -->\n<li>予約期間：{escape(coupon["booking_period"])}</li>\n<!-- /wp:list-item -->')

    if coupon.get("stay_period"):
        subs.append(f'<!-- wp:list-item -->\n<li>{period_label(coupon)}：{escape(coupon["stay_period"])}</li>\n<!-- /wp:list-item -->')

    dd = coupon.get("detail_data", {})
    codes = dd.get("coupon_codes", [])
    pws = dd.get("passwords", [])
    if codes or pws:
        parts = []
        if codes:
            parts.append(f'クーポンコード：<strong>{escape(", ".join(codes))}</strong>')
        if pws:
            parts.append(f'パスワード：<strong>{escape(", ".join(pws))}</strong>')
        subs.append(f'<!-- wp:list-item -->\n<li>{chr(12288).join(parts)}</li>\n<!-- /wp:list-item -->')

    sub_html = "\n\n".join(subs)
    return f"""<!-- wp:list-item -->
<li><strong>\u2192<a href="{aff}" rel="nofollow">{px}{title}</a></strong><!-- wp:list -->
<ul class="wp-block-list">{sub_html}</ul>
<!-- /wp:list --></li>
<!-- /wp:list-item -->"""


def gen_ol(coupons, config):
    return f"""<!-- wp:list {{"ordered":true,"className":"-list-under-dashed"}} -->
<ol class="wp-block-list -list-under-dashed">{chr(10)+chr(10).join(gen_item(c, config) for c in coupons)}</ol>
<!-- /wp:list -->"""


def balloon(text, hid=None):
    ia = f' id="{hid}"' if hid else ""
    return f'<!-- wp:paragraph {{"className":"is-style-balloon_box2"}} -->\n<p class="is-style-balloon_box2"{ia}><strong>{text}</strong></p>\n<!-- /wp:paragraph -->'


def spacer(h="50px"):
    return f'<!-- wp:spacer {{"height":"{h}"}} -->\n<div style="height:{h}" aria-hidden="true" class="wp-block-spacer"></div>\n<!-- /wp:spacer -->'


def h3(text, hid=None):
    ia = f' id="{hid}"' if hid else ""
    return f'<!-- wp:heading {{"level":3,"className":"is-style-section_ttl"}} -->\n<h3 class="wp-block-heading is-style-section_ttl"{ia}>{text}</h3>\n<!-- /wp:heading -->'


def cta_btn(url, text):
    uj = url.replace("&", "\\u0026")
    uh = escape(url, quote=True)
    return f'<!-- wp:loos/button {{"hrefUrl":"{uj}","isNewTab":true,"color":"blue","btnSize":"l","className":"is-style-btn_shiny"}} -->\n<div class="swell-block-button blue_ -size-l is-style-btn_shiny"><a href="{uh}" target="_blank" rel="noopener noreferrer" class="swell-block-button__link"><span>{text}</span></a></div>\n<!-- /wp:loos/button -->'


def gen_first_time(dom_sections, config):
    """初回限定セクション（アコーディオン付き）"""
    if not dom_sections["first_time"]:
        return ""
    c = dom_sections["first_time"][0]
    au = build_aff_url(c["detail_url"], "国内", config)
    auh = escape(au, quote=True)
    auj = au.replace("&", "\\u0026")

    return f"""{h3("初回限定クーポンコード", "first-time")}

{gen_ol(dom_sections["first_time"], config)}

<!-- wp:loos/accordion {{"className":"is-style-default"}} -->
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

<!-- wp:loos/button {{"hrefUrl":"{auj}","isNewTab":true,"color":"blue","btnSize":"l","className":"is-style-btn_normal"}} -->
<div class="swell-block-button blue_ -size-l is-style-btn_normal"><a href="{auh}" target="_blank" rel="noopener noreferrer" class="swell-block-button__link"><span>【初回限定】 JTBクーポン ページ→</span></a></div>
<!-- /wp:loos/button --></div>
<!-- /wp:group --></div></details>
<!-- /wp:loos/accordion-item --></div>
<!-- /wp:loos/accordion -->"""


def shinkansen_link():
    return f"""{balloon("JTB新幹線クーポンはこちら")}

<!-- wp:group {{"layout":{{"type":"constrained"}}}} -->
<div class="wp-block-group"><!-- wp:loos/post-link {{"linkData":{{"title":"JTB 割引クーポン新幹線・JR日帰りはある？お得な方法をご案内！","id":50839,"url":"https://yakushima.fun/jtb-bullet-train/","kind":"post-type","type":"post"}},"icon":"link"}} /--></div>
<!-- /wp:group -->"""


def domestic_tab(dom_sections, config):
    """国内タブの中身を生成"""
    p = []
    p.append('<!-- wp:paragraph -->\n<p>下記は、今すぐ誰でも使えるJTBの割引情報と配布中クーポンを一覧にしました。</p>\n<!-- /wp:paragraph -->')
    p.append(gen_first_time(dom_sections, config))
    p.append(h3("国内旅行クーポンコード"))
    if dom_sections["zenkoku"]:
        p.append(balloon("全国共通クーポン"))
        p.append(gen_ol(dom_sections["zenkoku"], config))
    p.append(spacer())
    p.append(shinkansen_link())
    if dom_sections["hotel"]:
        p.append(balloon("チェーンホテル限定クーポン"))
        p.append(gen_ol(dom_sections["hotel"], config))
    p.append(spacer())
    if dom_sections["hokkaido_tohoku"]:
        p.append(balloon("北海道・東北エリア"))
        p.append(gen_ol(dom_sections["hokkaido_tohoku"], config))
    if dom_sections["kanto"]:
        p.append(balloon("関東エリア", "kanto"))
        p.append(gen_ol(dom_sections["kanto"], config))
    if dom_sections["chubu"]:
        p.append(balloon("甲信越・中部・東海エリア"))
        p.append(gen_ol(dom_sections["chubu"], config))
    if dom_sections["kansai"]:
        p.append(balloon("関西エリア", "kansai"))
        p.append(gen_ol(dom_sections["kansai"], config))
    if dom_sections["west"]:
        p.append(balloon("中国・四国・九州・沖縄エリア"))
        p.append(gen_ol(dom_sections["west"], config))
    p.append(spacer())
    du = build_aff_url("https://www.jtb.co.jp/myjtb/campaign/coupon/", "国内", config)
    p.append('<!-- wp:paragraph {"align":"center"} -->\n<p class="has-text-align-center"><span class="swl-fz u-fz-xs">誰でも使える</span></p>\n<!-- /wp:paragraph -->')
    p.append(cta_btn(du, "JTB 国内クーポン一覧ページ →"))
    return "\n\n".join(x for x in p if x)


def overseas_tab(ovs_sections, config):
    """海外タブの中身を生成"""
    p = []
    p.append('<!-- wp:paragraph -->\n<p id="kaigai">下記は、今すぐ誰でも使えるJTBの海外旅行向け割引クーポンを一覧にしました。</p>\n<!-- /wp:paragraph -->')
    p.append(h3("海外ツアー（ルックJTB MySTYLE）"))
    if ovs_sections["tour"]:
        p.append(gen_ol(ovs_sections["tour"], config))
    p.append(spacer())
    p.append(h3("海外航空券＋ホテル"))
    if ovs_sections["air_hotel"]:
        p.append(gen_ol(ovs_sections["air_hotel"], config))
    p.append(spacer())
    p.append(h3("海外航空券"))
    if ovs_sections["air"]:
        p.append(gen_ol(ovs_sections["air"], config))
    if ovs_sections["optional"]:
        p.append(spacer())
        p.append(h3("海外オプショナルツアー"))
        p.append(gen_ol(ovs_sections["optional"], config))
    p.append(spacer())
    ou = build_aff_url("https://www.jtb.co.jp/myjtb/campaign/coupon/kaigaicoupon/", "海外", config)
    p.append('<!-- wp:paragraph {"align":"center"} -->\n<p class="has-text-align-center"><span class="swl-fz u-fz-xs">誰でも使える</span></p>\n<!-- /wp:paragraph -->')
    p.append(cta_btn(ou, "JTB 海外クーポン一覧ページ →"))
    return "\n\n".join(x for x in p if x)


def full_html(dom_sections, ovs_sections, config):
    """タブ全体の HTML を生成"""
    d = domestic_tab(dom_sections, config)
    o = overseas_tab(ovs_sections, config)
    return f'''<!-- wp:loos/tab {{"tabId":"{TAB_ID}","tabWidthPC":"flex-auto","tabWidthSP":"flex-auto","tabHeaders":["\\u003cspan class=\\u0022swl-fz u-fz-l\\u0022\\u003e\\u003cstrong\\u003eJTB国内クーポン\\u003c/strong\\u003e\\u003c/span\\u003e","\\u003cspan class=\\u0022swl-fz u-fz-l\\u0022\\u003e\\u003cstrong\\u003eJTB海外クーポン\\u003c/strong\\u003e\\u003c/span\\u003e"],"className":"is-style-balloon"}} -->
<div class="swell-block-tab is-style-balloon" data-width-pc="flex-auto" data-width-sp="flex-auto"><ul class="c-tabList" role="tablist"><li class="c-tabList__item" role="presentation"><button role="tab" class="c-tabList__button" aria-selected="true" aria-controls="tab-{TAB_ID}-0" data-onclick="tabControl"><span class="swl-fz u-fz-l"><strong>JTB国内クーポン</strong></span></button></li><li class="c-tabList__item" role="presentation"><button role="tab" class="c-tabList__button" aria-selected="false" aria-controls="tab-{TAB_ID}-1" data-onclick="tabControl"><span class="swl-fz u-fz-l"><strong>JTB海外クーポン</strong></span></button></li></ul><div class="c-tabBody"><!-- wp:loos/tab-body {{"tabId":"{TAB_ID}"}} -->
<div id="tab-{TAB_ID}-0" class="c-tabBody__item" aria-hidden="false"><!-- wp:group {{"backgroundColor":"swl-pale-04","layout":{{"type":"constrained"}}}} -->
<div class="wp-block-group has-swl-pale-04-background-color has-background">{d}</div>
<!-- /wp:group --></div>
<div id="tab-{TAB_ID}-1" class="c-tabBody__item" aria-hidden="true"><!-- wp:group {{"backgroundColor":"swl-pale-04","layout":{{"type":"constrained"}}}} -->
<div class="wp-block-group has-swl-pale-04-background-color has-background">{o}</div>
<!-- /wp:group --></div>
<!-- /wp:loos/tab-body --></div></div>
<!-- /wp:loos/tab -->'''


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
    domestic = [c for c in active if c.get("category") == "国内"]
    overseas = [c for c in active if c.get("category") == "海外"]

    print(f"📊 {len(active)} 件の配布中クーポンを読み込み ({filename})")

    # --- 国内セクション分類 ---
    dom_sections = {
        "first_time": [], "zenkoku": [], "hotel": [],
        "hokkaido_tohoku": [], "kanto": [], "chubu": [],
        "kansai": [], "west": [],
    }
    for c in domestic:
        dom_sections[classify_domestic(c)].append(c)

    # --- 海外セクション分類 ---
    ovs_sections = {"tour": [], "air_hotel": [], "air": [], "optional": []}
    for c in overseas:
        ovs_sections[classify_overseas(c)].append(c)

    # 統計表示
    section_labels = {
        "first_time": "初回限定",
        "zenkoku": "全国共通",
        "hotel": "チェーンホテル",
        "hokkaido_tohoku": "北海道・東北",
        "kanto": "関東",
        "chubu": "甲信越・中部・東海",
        "kansai": "関西",
        "west": "中国・四国・九州・沖縄",
    }
    print("\n  【国内】")
    for key, label in section_labels.items():
        if dom_sections[key]:
            ids = [c["id"] for c in dom_sections[key]]
            print(f"    {label}: {len(dom_sections[key])} 件 → {', '.join(ids)}")
    print("  【海外】")
    ovs_labels = {"tour": "ツアー", "air_hotel": "航空券+ホテル", "air": "航空券", "optional": "オプショナル"}
    for key, label in ovs_labels.items():
        if ovs_sections[key]:
            print(f"    {label}: {len(ovs_sections[key])} 件")

    total = sum(len(v) for v in dom_sections.values()) + sum(len(v) for v in ovs_sections.values())
    print(f"\n  合計: 国内 {sum(len(v) for v in dom_sections.values())} + 海外 {sum(len(v) for v in ovs_sections.values())} = {total}")

    # --- HTML 生成 ---
    html = full_html(dom_sections, ovs_sections, config)

    header = (
        f"<!-- JTB クーポン SWELL HTML v3（自動生成） -->\n"
        f"<!-- データ: {filename} / 生成: {datetime.now().strftime('%Y-%m-%d %H:%M')} -->\n"
    )

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    output_path = os.path.join(OUTPUT_DIR, "jtb_coupons.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(header + html)

    print(f"\n✅ 出力: {output_path}")


if __name__ == "__main__":
    main()
