#!/usr/bin/env python3
"""
Gutenberg HTML ページ構造解析モジュール
======================================
WordPress Gutenberg HTMLを解析し、H2/H3見出しとテーブルブロックの
対応関係を特定する。テーブル差替えのために使う。
"""

import re


def parse_page_sections(html: str) -> list[dict]:
    """
    Gutenberg HTMLを解析し、ブロック一覧を返す。

    各ブロックは以下の構造:
      - type: "heading" | "table"
      - level: 見出しレベル (heading のみ)
      - text: 見出しテキスト (heading のみ、タグ除去済み)
      - parent_h3: 直前のH3テキスト (table のみ)
      - parent_h2: 直前のH2テキスト (table のみ)
      - tbody_start / tbody_end: tbody の位置 (table のみ)
      - start / end: ブロック全体の位置
      - raw_html: ブロック全体のHTML
    """
    current_h2 = ""
    current_h3 = ""

    table_pattern = re.compile(
        r'(<!-- wp:table\s*(\{[^}]*\})?\s*-->)(.*?)(<!-- /wp:table -->)',
        re.DOTALL
    )
    heading_pattern = re.compile(
        r'<!-- wp:heading\s*(\{[^}]*\})?\s*-->\s*'
        r'<h(\d)[^>]*>(.*?)</h\d>\s*'
        r'<!-- /wp:heading -->',
        re.DOTALL
    )

    all_matches = []

    for m in heading_pattern.finditer(html):
        level = int(m.group(2))
        raw_text = m.group(3)
        clean_text = _strip_html_tags(raw_text)
        all_matches.append({
            "type": "heading",
            "level": level,
            "text": clean_text,
            "start": m.start(),
            "end": m.end(),
            "raw_html": m.group(0),
        })

    for m in table_pattern.finditer(html):
        table_html = m.group(0)
        inner = m.group(3)

        tbody_match = re.search(r'<tbody>(.*?)</tbody>', inner, re.DOTALL)
        tbody_start = None
        tbody_end = None
        if tbody_match:
            inner_offset = m.start() + len(m.group(1))
            tbody_start = inner_offset + tbody_match.start()
            tbody_end = inner_offset + tbody_match.end()

        all_matches.append({
            "type": "table",
            "start": m.start(),
            "end": m.end(),
            "raw_html": table_html,
            "tbody_start": tbody_start,
            "tbody_end": tbody_end,
        })

    all_matches.sort(key=lambda x: x["start"])

    for block in all_matches:
        if block["type"] == "heading":
            if block["level"] == 2:
                current_h2 = block["text"]
                current_h3 = ""
            elif block["level"] == 3:
                current_h3 = block["text"]
        elif block["type"] == "table":
            block["parent_h2"] = current_h2
            block["parent_h3"] = current_h3

    return all_matches


def extract_existing_affiliate_links(html: str) -> dict[str, list[dict]]:
    """
    ページ全体からafbリンクを抽出し、所属セクション(H3)ごとにグループ化。
    """
    sections = parse_page_sections(html)
    result: dict[str, list[dict]] = {}
    current_h3 = ""

    afb_pattern = re.compile(
        r'href="https?://t\.afi-b\.com/visit\.php\?a=Q10113i-([^&"]+)&amp;p=X653459L"'
    )

    for block in sections:
        if block["type"] == "heading" and block["level"] == 3:
            current_h3 = block["text"]
        elif block["type"] == "table":
            section_key = current_h3 or block.get("parent_h3", "")
            if section_key not in result:
                result[section_key] = []
            for m in afb_pattern.finditer(block["raw_html"]):
                result[section_key].append({"product_id": m.group(1)})

    return result


def count_table_rows(html: str) -> int:
    """HTML内のtbody内<tr>の数をカウント（安全チェック用）。"""
    tbody_chunks = re.findall(r'<tbody>.*?</tbody>', html, re.DOTALL)
    return sum(len(re.findall(r'<tr>', chunk)) for chunk in tbody_chunks)


def _strip_html_tags(text: str) -> str:
    """HTMLタグを除去してプレーンテキストを返す。"""
    return re.sub(r'<[^>]+>', '', text).strip()
