#!/usr/bin/env python3
"""
テーブル行生成モジュール
========================
クーポンJSONから2列テーブルの<tbody>HTMLを生成する。
HIS/JTB/KNT共通で使える。
"""

import re
from html import escape as h


def render_table_body(
    coupons: list[dict],
    aff_config: dict,
    ota: str = "his",
) -> str:
    """
    クーポンリストから <tbody>...</tbody> を生成。

    Args:
        coupons: マッチ済みクーポンリスト
        aff_config: アフィリエイト設定（category_links, keyword_overrides等）
        ota: "his" | "jtb" | "knt"
    """
    if not coupons:
        return "<tbody></tbody>"

    rows = []
    for coupon in coupons:
        aff_url, pixel_url = _get_affiliate_link(coupon, aff_config)
        row = _render_2col_row(coupon, aff_url, pixel_url, ota)
        rows.append(row)

    return "<tbody>" + "".join(rows) + "</tbody>"


def _render_2col_row(
    coupon: dict,
    aff_url: str,
    pixel_url: str,
    ota: str,
) -> str:
    """2列テーブルの<tr>を1件分生成。"""
    # --- 列1: クーポン名（リンク付き）+ 期限 ---
    title = coupon.get("title", "")
    # 長すぎるタイトルを短縮
    display_title = title if len(title) <= 40 else title[:37] + "…"

    period = _format_period(coupon, ota)

    if aff_url:
        name_link = f'→<a href="{h(aff_url)}">{h(display_title)}</a>'
        if pixel_url:
            name_link += f'<img width="1" height="1" src="{h(pixel_url)}">'
    else:
        name_link = f"→<strong>{h(display_title)}</strong>"

    col1 = name_link
    if period:
        col1 += f'<br><span style="font-size:80%">{h(period)}</span>'

    # --- 列2: 割引内容 + クーポンコード + 条件 ---
    col2 = _format_discount_and_codes(coupon, ota)

    return f"<tr><th>{col1}</th><td>{col2}</td></tr>"


def _format_period(coupon: dict, ota: str) -> str:
    """期限テキストを簡潔に生成。"""
    if ota == "his":
        booking = coupon.get("booking_period", "")
        if booking:
            return _simplify_date(booking)
    elif ota == "jtb":
        booking = coupon.get("booking_period", "")
        if booking:
            return _simplify_date(booking)
    elif ota == "knt":
        detail = coupon.get("detail_data") or {}
        booking = detail.get("booking_period", "")
        if booking:
            return _simplify_date(booking)
    return ""


def _simplify_date(text: str) -> str:
    """日付文字列を簡潔にする。"""
    if not text:
        return ""
    s = re.sub(r"\([月火水木金土日祝]\)", "", text)
    s = re.sub(r"\s*\d{1,2}:\d{2}", "", s)
    s = re.sub(r"(\d{4})年(\d{1,2})月(\d{1,2})日?", r"\1/\2/\3", s)
    # 「～」の前後を取って「〜YYYY/M/Dまで」形式に
    parts = re.split(r"[～〜~]", s)
    if len(parts) == 2:
        end = parts[1].strip()
        return f"〜{end}まで"
    return s.strip()


def _format_discount_and_codes(coupon: dict, ota: str) -> str:
    """割引内容+クーポンコード+条件のHTMLを生成。"""
    parts = []

    if ota == "his":
        codes = coupon.get("coupon_codes", [])
        discount = coupon.get("discount", "")
        target = coupon.get("target", "")

        if len(codes) == 0:
            parts.append(h(discount))
        elif len(codes) == 1:
            cc = codes[0]
            disc = _extract_discount(cc.get("discount", discount))
            parts.append(h(disc))
            code = cc.get("code", "")
            if code:
                parts.append(f"<strong>クーポンコード：</strong>{h(code)}")
        else:
            for i, cc in enumerate(codes):
                cond = cc.get("condition", "")
                disc = _extract_discount(cc.get("discount", ""))
                code = cc.get("code", "")
                line = ""
                if cond:
                    line += f"{h(cond)}："
                line += f"{h(disc)}"
                if code:
                    line += f" / クーポンコード：{h(code)}"
                parts.append(line)

        if target:
            short_target = _shorten_target(target)
            parts.append(f'<span style="font-size:80%">条件：{h(short_target)}</span>')

    elif ota == "jtb":
        discount = coupon.get("discount", "")
        detail = coupon.get("detail_data") or {}
        codes = detail.get("coupon_codes", [])
        passwords = detail.get("passwords", [])
        notes = detail.get("notes", [])

        parts.append(h(discount))
        for i, code in enumerate(codes):
            label = "クーポンコード"
            parts.append(f"<strong>{label}：</strong>{h(code)}")
        for pw in passwords:
            parts.append(f"<strong>パスワード：</strong>{h(pw)}")
        if notes:
            short = _shorten_target(notes[0]) if notes else ""
            if short:
                parts.append(f'<span style="font-size:80%">条件：{h(short)}</span>')

    elif ota == "knt":
        discount = coupon.get("discount", "")
        detail = coupon.get("detail_data") or {}
        codes = detail.get("coupon_codes", [])
        conditions = detail.get("conditions", [])

        parts.append(h(discount))
        for code in codes:
            parts.append(f"<strong>クーポンコード：</strong>{h(code)}")
        if conditions:
            short = _shorten_target(conditions[0])
            if short:
                parts.append(f'<span style="font-size:80%">条件：{h(short)}</span>')

    return "<br>".join(parts)


def _extract_discount(text: str) -> str:
    """割引テキストから金額部分だけを抽出する。"""
    m = re.match(r"(.+?(?:割引|OFF|引|％割引|%OFF))", text)
    if m:
        return m.group(1)
    return text


def _shorten_target(text: str) -> str:
    """条件テキストを短縮する。"""
    text = re.split(r"【対象外】", text)[0].strip()
    text = text.replace("HISが企画・実施する", "")
    text = text.replace("HISが旅行企画・実施する", "")
    if len(text) > 80:
        text = text[:77] + "…"
    return text


def _get_affiliate_link(coupon: dict, config: dict) -> tuple[str, str]:
    """クーポンに適切なアフィリエイトリンクを返す (url, pixel)。"""
    if not config:
        return "", ""

    category = coupon.get("category", "")
    title = coupon.get("title", "")
    text = f"{category} {title}"

    # 1) keyword_overrides
    for override in config.get("keyword_overrides", []):
        if override.get("keyword", "") in text:
            return override.get("url", ""), override.get("pixel", "")

    # 2) category_links（generate_his_html.py と同じロジック）
    from section_matcher import HIS_CATEGORY_KEYWORDS
    for cat_key in HIS_CATEGORY_KEYWORDS:
        if cat_key in category:
            # マップ先のセクション名で category_links を引く
            section_names = HIS_CATEGORY_KEYWORDS[cat_key]
            for sn in section_names:
                if sn in config.get("category_links", {}):
                    link = config["category_links"][sn]
                    return link.get("url", ""), link.get("pixel", "")

    # 3) フォールバック
    for section_name in ("海外その他", "default"):
        if section_name in config.get("category_links", {}):
            link = config["category_links"][section_name]
            return link.get("url", ""), link.get("pixel", "")

    return "", ""
