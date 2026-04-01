#!/usr/bin/env python3
"""
セクション×カテゴリ マッチングモジュール
========================================
H3見出しテキストとクーポンJSONのcategoryをファジーマッチし、
各テーブルに挿入すべきクーポンを決定する。
"""

import re


# OTA別のカテゴリ→セクションキーワード マッピング（フォールバック用）
# generate_his_html.py の CATEGORY_TO_SECTION を参考に構築
HIS_CATEGORY_KEYWORDS = {
    "海外旅行": ["海外ツアー"],
    "海外航空券": ["海外航空券"],
    "海外eSIM": ["海外その他", "eSIM"],
    "国内ツアー": ["国内ツアー"],
    "国内航空券＋ホテル": ["国内ツアー", "航空券＋ホテル", "航空券+ホテル"],
    "国内添乗員同行ツアー": ["添乗員"],
    "国内バスツアー": ["バス"],
    "高速バス・夜行バス": ["バス"],
    "国内ホテル": ["ホテル", "宿泊"],
    "国内航空券": ["国内航空券", "航空券"],
    "グランピング": ["ホテル", "宿泊", "グランピング"],
}

# 汎用キーワードマッチング（H3テキストから抽出するキーワード）
SECTION_KEYWORDS = [
    "海外航空券",
    "海外ツアー",
    "海外",
    "国内ツアー",
    "航空券＋ホテル",
    "航空券+ホテル",
    "添乗員",
    "ホテル",
    "宿泊",
    "航空券",
    "バス",
    "eSIM",
    "レンタカー",
]


def match_sections_to_coupons(
    sections: list[dict],
    coupons: list[dict],
    ota: str = "his",
) -> list[dict]:
    """
    H3見出し付きテーブルと、クーポンJSONをマッチングする。

    Args:
        sections: gutenberg_parser.parse_page_sections() の結果
        coupons: クーポンJSONリスト（配布中のみ推奨）
        ota: "his" | "jtb" | "knt"

    Returns:
        テーブルブロックのリスト。各要素に "matched_coupons" が追加される。
        マッチしなかったクーポンは最後の要素の "unmatched_coupons" に格納。
    """
    # テーブルブロックだけ抽出
    tables = [b for b in sections if b["type"] == "table"]

    if not tables:
        return []

    # 配布中のみフィルタ
    active_coupons = [c for c in coupons if c.get("stock_status") == "配布中"]

    # 各クーポンの使用済みフラグ
    used = set()

    for table in tables:
        table["matched_coupons"] = []
        h3_text = table.get("parent_h3", "")
        h3_keywords = _extract_keywords(h3_text)

        for i, coupon in enumerate(active_coupons):
            if i in used:
                continue
            category = coupon.get("category", "")
            title = coupon.get("title", "")

            if _is_match(h3_keywords, h3_text, category, title, ota):
                table["matched_coupons"].append(coupon)
                used.add(i)

    # マッチしなかったクーポン
    unmatched = [c for i, c in enumerate(active_coupons) if i not in used]

    return tables, unmatched


def _extract_keywords(h3_text: str) -> list[str]:
    """H3テキストからマッチング用キーワードを抽出。"""
    found = []
    for kw in SECTION_KEYWORDS:
        if kw in h3_text:
            found.append(kw)
    return found


def _is_match(
    h3_keywords: list[str],
    h3_text: str,
    category: str,
    title: str,
    ota: str,
) -> bool:
    """クーポンがこのセクションに属するか判定。"""
    text = f"{category} {title}"

    # 学生系は専用記事のためスキップ
    if "学生" in category or "学生" in title:
        return False

    # H3にキーワードがなければスキップ（非クーポンセクション）
    if not h3_keywords:
        return False

    # カテゴリキーワードと H3 キーワードの照合
    for h3_kw in h3_keywords:
        # 直接マッチ
        if h3_kw in category or h3_kw in title:
            return True

        # OTA固有マッピングでのマッチ
        if ota == "his":
            for cat_key, section_kws in HIS_CATEGORY_KEYWORDS.items():
                if cat_key in category and h3_kw in section_kws:
                    return True

    # JTB/KNT: category に「国内」「海外」+ type でマッチ
    if ota in ("jtb", "knt"):
        coupon_cat = category
        coupon_type = ""
        # JTB は top-level に type がある
        # KNT は detail_data に type がある場合がある
        for h3_kw in h3_keywords:
            if h3_kw in coupon_cat or h3_kw in coupon_type:
                return True

    return False
