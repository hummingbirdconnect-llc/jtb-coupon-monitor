#!/usr/bin/env python3
"""
coupon-master Markdown をダッシュボード用の暫定JSONへ変換する。

このスクリプトは公式ページを取得しない。既存の手元マスターを
`manual_master` 由来のスナップショットとして保存し、未自動化会社も
ダッシュボードに載せるための補助データを作る。
"""

from __future__ import annotations

import hashlib
import html as html_lib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent
REGISTRY = ROOT / "config" / "provider_registry.json"

TITLE_KEYS = [
    "正式名称",
    "名称",
    "セール名",
    "キャンペーン名",
    "クーポン名",
    "カード名",
    "ポイントサイト",
    "ランク名",
    "自治体",
    "種類",
    "項目",
]
CODE_KEYS = ["コード", "クーポンコード", "プロモコード"]
DISCOUNT_KEYS = [
    "割引額／率",
    "割引額/率",
    "割引額",
    "割引",
    "割引内容",
    "割引率",
    "現行還元率（取得日）",
    "還元率・条件",
    "特典内容",
    "内容",
]
PRODUCT_KEYS = ["対象商品", "対象", "提携OTA条件"]
CONDITION_KEYS = ["取得条件", "条件", "利用条件", "承認ルール", "上限・枚数制限", "併用可否"]
BOOKING_KEYS = ["予約期間", "配布期間", "開催時期", "開催日"]
TRAVEL_KEYS = ["有効期限", "宿泊/出発対象期間", "対象期間", "終了日 or 最終確認日"]
URL_KEYS = ["出典URL", "URL", "詳細URL"]
STATUS_KEYS = ["ステータス", "開催ステータス"]
SOURCE_KEYS = ["source", "取得ツール"]

ARTICLE_TITLE_KEYS = [
    "クーポン・キャンペーン名",
    "キャンペーン/クーポン名",
    "キャンペーン名",
    "クーポン名",
    "クーポンの種類",
    "クーポン種別",
    "クーポン・クーポンコード",
    "クーポンコード",
    "カードブランド",
    "カード",
    "VIPランク",
    "種類",
    "制度名",
    "割引手段",
    "確認する割引",
    "おすすめクーポン",
    "出発・目的地",
    "状況 / 開催期間",
    "対象",
]
ARTICLE_DISCOUNT_KEYS = [
    "有効期限・割引など",
    "割引/特典内容",
    "割引内容 / 特典",
    "割引・特典内容",
    "割引内容",
    "割引率",
    "割引額",
    "割引額の目安",
    "金額感の目安",
    "内容・割引額",
    "内容",
    "特典内容",
    "概算還元率",
    "ホテル割引率",
    "パッケージ割引額",
    "直前割",
    "神戸⇔那覇増便",
]
ARTICLE_PRODUCT_KEYS = ["対象商品", "対象", "使える人", "獲得ページ", "割引対象"]
ARTICLE_CONDITION_KEYS = [
    "条件",
    "対象者・条件",
    "期間/条件",
    "有効期限 / 条件 / 詳細",
    "取得方法",
    "主な入手方法",
    "入手条件",
    "予約前の見方",
    "備考",
    "補足",
]
ARTICLE_PERIOD_KEYS = [
    "有効期限",
    "有効期限・割引など",
    "クーポン利用期限",
    "宿泊対象期限",
    "現在の状況（2026年4月時点）",
    "実施期間",
    "期間/条件",
    "有効期限 / 条件 / 詳細",
]
ARTICLE_BLOCK_HEADERS = ["症状", "主な原因", "対処", "ミス", "ブラウザ", "支払い方法", "用途", "電話", "受付時間", "メール", "チャネル"]
ARTICLE_SIGNAL_RE = re.compile(r"クーポン|キャンペーン|割引|特典|還元|ポイント|OFF|円|直前割|セール|無料|優待")
HEADING_BLOCK_RE = re.compile(
    r"比較|使い方|取得方法|使えない|対処|併用|口コミ|評判|FAQ|Q&A|よくある|まとめ|注意|キャンセル|問い合わせ|支払い|どこで|可能|使えます|上限|JTB|HIS|近畿日本|JALパック|他社|旅行会社"
)


def clean_cell(value: str) -> str:
    value = value.strip()
    value = re.sub(r"<br\s*/?>", " / ", value, flags=re.IGNORECASE)
    value = re.sub(r"`([^`]+)`", r"\1", value)
    value = re.sub(r"\*\*([^*]+)\*\*", r"\1", value)
    value = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def split_markdown_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [clean_cell(cell) for cell in line.split("|")]


def is_separator(line: str) -> bool:
    cells = split_markdown_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in cells)


def heading_text(line: str) -> str | None:
    match = re.match(r"^(#{1,6})\s+(.+)$", line.strip())
    if not match:
        return None
    return clean_cell(match.group(2))


def first_value(row: dict[str, str], keys: list[str]) -> str:
    for key in keys:
        if key in row and row[key].strip():
            return row[key].strip()
    return ""


def status_from_text(text: str) -> str:
    if any(token in text for token in ["❌", "終了済", "終了明示", "期日経過"]):
        return "配布終了"
    if "終了" in text and "次回未発表" not in text and "終了日" not in text:
        return "配布終了"
    if any(token in text for token in ["⚠", "❓", "要確認", "未確認", "不明", "次回未発表", "断定不可"]):
        return "要確認"
    if any(token in text for token in ["✅", "稼働中", "常設", "通年"]):
        return "配布中"
    return "要確認"


def confidence_from_status(status: str) -> str:
    if status == "配布中":
        return "medium"
    if status == "配布終了":
        return "medium"
    return "low"


def make_coupon_id(provider_id: str, title: str, code: str, source_url: str) -> str:
    seed = "|".join([provider_id, title, code, source_url])
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]
    return f"{provider_id}-{digest}"


def parse_tables(markdown: str) -> list[tuple[str, dict[str, str]]]:
    lines = markdown.splitlines()
    section = ""
    rows: list[tuple[str, dict[str, str]]] = []
    idx = 0
    while idx < len(lines):
        heading = heading_text(lines[idx])
        if heading:
            section = heading
            idx += 1
            continue
        if "|" not in lines[idx] or idx + 1 >= len(lines) or not is_separator(lines[idx + 1]):
            idx += 1
            continue

        headers = split_markdown_row(lines[idx])
        idx += 2
        while idx < len(lines) and "|" in lines[idx].strip():
            values = split_markdown_row(lines[idx])
            if len(values) < len(headers):
                values += [""] * (len(headers) - len(values))
            row = dict(zip(headers, values[: len(headers)]))
            rows.append((section, row))
            idx += 1
    return rows


def normalize_coupon(provider: dict, section: str, row: dict[str, str]) -> dict | None:
    title = first_value(row, TITLE_KEYS)
    code = first_value(row, CODE_KEYS)
    discount = first_value(row, DISCOUNT_KEYS)
    product = first_value(row, PRODUCT_KEYS)
    booking_period = first_value(row, BOOKING_KEYS)
    travel_period = first_value(row, TRAVEL_KEYS)
    source_url = first_value(row, URL_KEYS)
    status_text = " ".join(filter(None, [first_value(row, STATUS_KEYS), title, section]))
    conditions = [first_value(row, CONDITION_KEYS)]

    if not title or title in {"―", "-", "なし", "該当なし"}:
        return None
    if title.startswith("注記") or title.startswith("裏取り"):
        return None

    stock_status = status_from_text(status_text)
    coupon_id = make_coupon_id(provider["id"], title, code, source_url)
    source = first_value(row, SOURCE_KEYS) or "coupon-master"

    return {
        "id": coupon_id,
        "provider": provider["id"],
        "provider_label": provider["label"],
        "site_targets": provider.get("site_targets", []),
        "article_slug": "",
        "title": title,
        "category": section,
        "discount": discount,
        "stock_status": stock_status,
        "product_type": product,
        "booking_period": booking_period,
        "travel_period": travel_period,
        "coupon_codes": [code] if code and code not in {"―", "-"} else [],
        "conditions": [item for item in conditions if item and item not in {"―", "-"}],
        "source_url": source_url,
        "detail_url": source_url,
        "source_type": "manual_master",
        "fetch_method": "coupon_master",
        "last_checked": datetime.now(JST).strftime("%Y-%m-%d"),
        "confidence": confidence_from_status(stock_status),
        "display_type": "table",
        "placement_hint": "",
        "detail_data": {
            "source": source,
            "raw_status": first_value(row, STATUS_KEYS),
        },
    }


def normalize_bullet(provider: dict, section: str, line: str) -> dict | None:
    """表がないmaster向けに、限定された箇条書きセクションから暫定行を作る。"""
    text = clean_cell(line.lstrip("-").strip())
    if not text or text.startswith("`"):
        return None
    title, _, note = text.partition(":")
    title = title.strip()
    note = note.strip()
    if not title or title in {"―", "-", "なし", "該当なし"}:
        return None

    section_status = section
    if "終了" in section_status:
        stock_status = "配布終了"
    elif "要確認" in section_status or "補足" in section_status:
        stock_status = "要確認"
    else:
        stock_status = "配布中"

    discount_match = re.search(r"(最大)?\d{1,3}(?:,\d{3})*円|[0-9]+(?:\.[0-9]+)?%|半額|ポイントUP", title)
    discount = discount_match.group(0) if discount_match else ""
    coupon_id = make_coupon_id(provider["id"], title, "", section)

    return {
        "id": coupon_id,
        "provider": provider["id"],
        "provider_label": provider["label"],
        "site_targets": provider.get("site_targets", []),
        "article_slug": "",
        "title": title,
        "category": section,
        "discount": discount,
        "stock_status": stock_status,
        "product_type": "",
        "booking_period": "",
        "travel_period": "",
        "coupon_codes": [],
        "conditions": [note] if note else [],
        "source_url": "",
        "detail_url": "",
        "source_type": "manual_master",
        "fetch_method": "coupon_master_bullet",
        "last_checked": datetime.now(JST).strftime("%Y-%m-%d"),
        "confidence": "low",
        "display_type": "table",
        "placement_hint": "",
        "detail_data": {
            "source": "coupon-master",
            "raw_status": section,
        },
    }


def parse_bullet_fallback(markdown: str, provider: dict) -> list[dict]:
    """表がないマスターだけに使う、箇条書きの暫定抽出。"""
    allowed_sections = ["採用候補", "直近終了", "要確認", "補足扱い", "終了済みとして扱う"]
    blocked_sections = ["除外", "H2/H3", "FAQ", "内部リンク", "DataForSEO", "見出し"]
    section = ""
    rows = []
    seen = set()
    for raw_line in markdown.splitlines():
        heading = heading_text(raw_line)
        if heading:
            section = heading
            continue
        if not raw_line.strip().startswith("- "):
            continue
        if not any(key in section for key in allowed_sections):
            continue
        if any(key in section for key in blocked_sections):
            continue
        coupon = normalize_bullet(provider, section, raw_line)
        if not coupon or coupon["id"] in seen:
            continue
        seen.add(coupon["id"])
        rows.append(coupon)
    return rows


def strip_html(fragment: str) -> str:
    fragment = re.sub(r"<br\s*/?>", " / ", fragment, flags=re.IGNORECASE)
    fragment = re.sub(r"<[^>]+>", "", fragment)
    return clean_cell(html_lib.unescape(fragment))


def extract_href(fragment: str) -> str:
    match = re.search(r'href=["\']([^"\']+)["\']', fragment, flags=re.IGNORECASE)
    return html_lib.unescape(match.group(1)) if match else ""


def read_article_html(path: Path) -> str:
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload.get("content") or payload.get("content_raw") or payload.get("html") or ""
    return path.read_text(encoding="utf-8")


def parse_article_tables(html: str) -> list[tuple[list[str], list[list[str]], list[str]]]:
    """HTML内のテーブルを、ヘッダー・テキスト行・行HTMLに分解する。"""
    parsed = []
    for table_match in re.finditer(r"<table\b.*?</table>", html, flags=re.IGNORECASE | re.DOTALL):
        table_html = table_match.group(0)
        tr_blocks = re.findall(r"<tr\b.*?</tr>", table_html, flags=re.IGNORECASE | re.DOTALL)
        if len(tr_blocks) < 2:
            continue
        headers = [
            strip_html(cell)
            for cell in re.findall(r"<t[hd]\b[^>]*>(.*?)</t[hd]>", tr_blocks[0], flags=re.IGNORECASE | re.DOTALL)
        ]
        if not headers:
            continue
        rows: list[list[str]] = []
        row_htmls: list[str] = []
        for tr in tr_blocks[1:]:
            cells = [
                strip_html(cell)
                for cell in re.findall(r"<t[hd]\b[^>]*>(.*?)</t[hd]>", tr, flags=re.IGNORECASE | re.DOTALL)
            ]
            if not cells:
                continue
            if len(cells) < len(headers):
                cells += [""] * (len(headers) - len(cells))
            rows.append(cells[: len(headers)])
            row_htmls.append(tr)
        parsed.append((headers, rows, row_htmls))
    return parsed


def is_coupon_like_table(headers: list[str], rows: list[list[str]]) -> bool:
    header_text = " ".join(headers)
    row_text = " ".join(" ".join(row) for row in rows[:8])
    has_signal = bool(ARTICLE_SIGNAL_RE.search(header_text + " " + row_text))
    has_positive_header = bool(ARTICLE_SIGNAL_RE.search(header_text))
    if not has_signal:
        return False
    if any(blocked in header_text for blocked in ARTICLE_BLOCK_HEADERS) and not has_positive_header:
        return False
    return True


def date_is_past(text: str) -> bool:
    today = datetime.now(JST).date()
    patterns = [
        r"(20\d{2})[/-](\d{1,2})[/-](\d{1,2})",
        r"(20\d{2})年(\d{1,2})月(\d{1,2})日",
    ]
    dates = []
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            try:
                dates.append(datetime(int(match.group(1)), int(match.group(2)), int(match.group(3))).date())
            except ValueError:
                continue
    return bool(dates) and max(dates) < today


def article_status(row: dict[str, str]) -> str:
    all_text = " ".join(row.values())
    valid_until = first_value(row, ["有効期限", "クーポン利用期限", "宿泊対象期限", "現在の状況（2026年4月時点）"])
    if "終了" in all_text or "確認できず" in all_text:
        return "要確認"
    if valid_until and date_is_past(valid_until):
        return "配布終了"
    if "公式サイトで確認" in all_text:
        return "要確認"
    return "配布中"


def normalize_article_row(provider: dict, source_path: Path, headers: list[str], values: list[str], row_html: str) -> dict | None:
    row = dict(zip(headers, values))
    title = first_value(row, ARTICLE_TITLE_KEYS)
    if not title or title in {"-", "―"}:
        return None
    if not ARTICLE_SIGNAL_RE.search(" ".join([title, " ".join(row.values())])):
        return None

    code = first_value(row, ["クーポンコード", "コード"])
    discount_parts = [first_value(row, ARTICLE_DISCOUNT_KEYS), first_value(row, ["割引上限"])]
    discount = " / ".join(part for part in discount_parts if part and part not in {"-", "―"})
    conditions = [first_value(row, ARTICLE_CONDITION_KEYS), first_value(row, ARTICLE_PERIOD_KEYS)]
    source_url = extract_href(row_html)
    stock_status = article_status(row)
    coupon_id = make_coupon_id(provider["id"], title, code, source_path.name)

    return {
        "id": coupon_id,
        "provider": provider["id"],
        "provider_label": provider["label"],
        "site_targets": provider.get("site_targets", []),
        "article_slug": "",
        "title": title,
        "category": source_path.stem,
        "discount": discount,
        "stock_status": stock_status,
        "product_type": first_value(row, ARTICLE_PRODUCT_KEYS),
        "booking_period": "",
        "travel_period": first_value(row, ARTICLE_PERIOD_KEYS),
        "coupon_codes": [code] if code and code not in {"-", "―", "専用ページで確認"} else [],
        "conditions": [item for item in conditions if item and item not in {"-", "―"}],
        "source_url": source_url,
        "detail_url": source_url,
        "source_type": "article_snapshot",
        "fetch_method": "existing_article_table",
        "last_checked": datetime.now(JST).strftime("%Y-%m-%d"),
        "confidence": "low" if stock_status == "要確認" else "medium",
        "display_type": "table",
        "placement_hint": "",
        "detail_data": {
            "source": str(source_path),
            "headers": headers,
        },
    }


def normalize_heading_item(provider: dict, source_path: Path, title: str, body_html: str) -> dict | None:
    title = strip_html(title)
    body_text = strip_html(body_html)
    if not title or not re.search(r"クーポン|キャンペーン|セール|特典|PASS|LINE|ポイント", title, flags=re.IGNORECASE):
        return None
    if HEADING_BLOCK_RE.search(title):
        return None

    context = body_text[:500]
    discount_match = re.search(r"(最大)?\d{1,3}(?:,\d{3})*円|[0-9]+(?:\.[0-9]+)?%|ポイント[0-9]+倍|[0-9]+倍|無料|抽選", title + " " + context)
    discount = discount_match.group(0) if discount_match else ""
    source_url = extract_href(body_html)
    status = "配布終了" if date_is_past(title + " " + context) else "要確認"
    coupon_id = make_coupon_id(provider["id"], title, "", source_path.name)

    return {
        "id": coupon_id,
        "provider": provider["id"],
        "provider_label": provider["label"],
        "site_targets": provider.get("site_targets", []),
        "article_slug": "",
        "title": title,
        "category": source_path.stem,
        "discount": discount,
        "stock_status": status,
        "product_type": "",
        "booking_period": "",
        "travel_period": "",
        "coupon_codes": [],
        "conditions": [context] if context else [],
        "source_url": source_url,
        "detail_url": source_url,
        "source_type": "article_snapshot",
        "fetch_method": "existing_article_heading",
        "last_checked": datetime.now(JST).strftime("%Y-%m-%d"),
        "confidence": "low",
        "display_type": "table",
        "placement_hint": "",
        "detail_data": {
            "source": str(source_path),
        },
    }


def parse_heading_fallback(provider: dict, source_path: Path, html: str) -> list[dict]:
    rows = []
    for match in re.finditer(r"<h[23]\b[^>]*>(.*?)</h[23]>(.*?)(?=<h[23]\b|$)", html, flags=re.IGNORECASE | re.DOTALL):
        coupon = normalize_heading_item(provider, source_path, match.group(1), match.group(2))
        if coupon:
            rows.append(coupon)
    return rows


def parse_article_fallback(provider: dict) -> list[dict]:
    rows = []
    seen = set()
    for path_text in provider.get("article_paths", []):
        path = (ROOT / path_text).resolve()
        if not path.exists():
            continue
        html = read_article_html(path)
        for headers, table_rows, row_htmls in parse_article_tables(html):
            if not is_coupon_like_table(headers, table_rows):
                continue
            for values, row_html in zip(table_rows, row_htmls):
                coupon = normalize_article_row(provider, path, headers, values, row_html)
                if not coupon or coupon["id"] in seen:
                    continue
                seen.add(coupon["id"])
                rows.append(coupon)
        if rows:
            continue
        for coupon in parse_heading_fallback(provider, path, html):
            if coupon["id"] in seen:
                continue
            seen.add(coupon["id"])
            rows.append(coupon)
    return rows


def load_registry() -> list[dict]:
    with REGISTRY.open("r", encoding="utf-8") as handle:
        return json.load(handle)["providers"]


def build_provider(provider: dict) -> dict:
    master_path = provider.get("master_path")
    if not master_path and not provider.get("article_paths"):
        return {"provider": provider["id"], "status": "skipped", "reason": "master_pathなし", "count": 0}

    rows = []
    seen = set()
    if master_path:
        path = (ROOT / master_path).resolve()
        if not path.exists():
            return {"provider": provider["id"], "status": "missing", "reason": str(path), "count": 0}

        text = path.read_text(encoding="utf-8")
        for section, raw_row in parse_tables(text):
            coupon = normalize_coupon(provider, section, raw_row)
            if not coupon or coupon["id"] in seen:
                continue
            seen.add(coupon["id"])
            rows.append(coupon)
        if not rows:
            rows = parse_bullet_fallback(text, provider)
    elif provider.get("article_paths"):
        rows = parse_article_fallback(provider)

    data_dir = provider.get("data_dir")
    if not data_dir:
        return {"provider": provider["id"], "status": "no_data_dir", "count": len(rows)}
    out_dir = ROOT / data_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"coupons_{datetime.now(JST).strftime('%Y-%m-%d')}.json"
    out_file.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"provider": provider["id"], "status": "written", "file": str(out_file.relative_to(ROOT)), "count": len(rows)}


def main() -> None:
    results = []
    for provider in load_registry():
        if provider.get("coverage_status") not in {"master_import", "article_exists"}:
            continue
        results.append(build_provider(provider))

    out_file = ROOT / "manual_coupon_data" / "coverage_snapshot.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("manual provider data build")
    for item in results:
        print(f"- {item['provider']}: {item['status']} / {item.get('count', 0)}件")
    print(f"coverage: {out_file.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
