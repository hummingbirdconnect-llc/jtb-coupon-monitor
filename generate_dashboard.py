#!/usr/bin/env python3
"""
クーポン監視ダッシュボード HTML 生成スクリプト。

`config/provider_registry.json` を正本として、日次取得済みの会社、
coupon-master由来の暫定データ、未整備の会社を同じダッシュボードに集約する。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent
REGISTRY = ROOT / "config" / "provider_registry.json"

COVERAGE_LABELS = {
    "auto_daily": "自動取得",
    "master_import": "手元マスター",
    "manual_queue": "半自動確認待ち",
    "article_exists": "記事あり・取得未整備",
    "not_started": "未着手",
}

COMMON_COLUMNS = [
    "詳細URL",
    "タイトル",
    "カテゴリ",
    "ID",
    "割引額",
    "配布状況",
    "対象商品",
    "予約期間",
    "出発/宿泊期間",
    "クーポンコード",
    "データ元",
    "取得方法",
    "確度",
    "条件",
]

SUMMARY_COLUMNS = [
    "会社",
    "対象サイト",
    "分類",
    "取得状態",
    "件数",
    "配布中",
    "配布終了",
    "要確認",
    "最新データ",
    "データ元",
    "次アクション",
]

LOG_COLUMNS = ["日付", "種別", "カテゴリ", "ID", "タイトル", "エリア/割引"]


def load_registry() -> list[dict[str, Any]]:
    with REGISTRY.open("r", encoding="utf-8") as handle:
        return json.load(handle)["providers"]


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_latest_data(data_dir: str | None) -> tuple[list[dict[str, Any]], str, str]:
    """最新の通常スナップショットを優先し、なければDRY-RUNを読む。"""
    if not data_dir:
        return [], "", ""
    path = ROOT / data_dir
    if not path.exists():
        return [], "", ""

    normal_files = sorted(path.glob("coupons_*.json"), reverse=True)
    if normal_files:
        file_path = normal_files[0]
        return load_json(file_path), file_path.name, "coupons"

    dry_run_files = sorted(path.glob("dry_run_coupons_*.json"), reverse=True)
    if dry_run_files:
        file_path = dry_run_files[0]
        return load_json(file_path), file_path.name, "dry_run"

    return [], "", ""


def load_change_log(data_dir: str | None) -> list[dict[str, Any]]:
    if not data_dir:
        return []
    path = ROOT / data_dir / "change_log.json"
    if not path.exists():
        return []
    return load_json(path)


def first_value(source: dict[str, Any], keys: list[str]) -> str:
    for key in keys:
        value = source.get(key)
        if value is None:
            continue
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
    return ""


def normalize_codes(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        codes = []
        for item in value:
            if isinstance(item, dict):
                code = item.get("code") or item.get("coupon_code") or ""
                if code:
                    codes.append(str(code))
            elif item:
                codes.append(str(item))
        return " / ".join(codes)
    return str(value)


def normalize_conditions(coupon: dict[str, Any]) -> str:
    detail = coupon.get("detail_data") or {}
    parts: list[str] = []
    for value in [coupon.get("conditions"), detail.get("conditions"), detail.get("notes")]:
        if isinstance(value, list):
            parts.extend(str(item) for item in value if item)
        elif value:
            parts.append(str(value))
    for item in coupon.get("coupon_codes") or []:
        if isinstance(item, dict):
            text = "→".join(str(item.get(key, "")) for key in ["condition", "discount"] if item.get(key))
            if text:
                parts.append(text)
    return " / ".join(dict.fromkeys(part.strip() for part in parts if part and part.strip()))


def format_coupon_row(coupon: dict[str, Any], provider: dict[str, Any], file_kind: str) -> dict[str, str]:
    detail = coupon.get("detail_data") or {}
    status = first_value(coupon, ["stock_status", "status"]) or "要確認"
    if status == "active":
        status = "配布中"
    elif status in {"ended", "expired"}:
        status = "配布終了"

    source_type = first_value(coupon, ["source_type"]) or first_value(detail, ["source"]) or file_kind
    codes = normalize_codes(coupon.get("coupon_codes") or detail.get("coupon_codes"))
    return {
        "詳細URL": first_value(coupon, ["detail_url", "source_url"]),
        "タイトル": first_value(coupon, ["title", "name"]),
        "カテゴリ": first_value(coupon, ["category", "area"]),
        "ID": first_value(coupon, ["id", "coupon_id"]),
        "割引額": first_value(coupon, ["discount"]) or first_value(detail, ["discount"]),
        "配布状況": status,
        "対象商品": first_value(coupon, ["product_type", "type", "target"]),
        "予約期間": first_value(coupon, ["booking_period"]) or first_value(detail, ["booking_period"]),
        "出発/宿泊期間": first_value(coupon, ["travel_period", "stay_period"]) or first_value(detail, ["stay_period"]),
        "クーポンコード": codes,
        "データ元": source_type,
        "取得方法": first_value(coupon, ["fetch_method"]) or ("手元マスター" if source_type == "manual_master" else ""),
        "確度": first_value(coupon, ["confidence"]),
        "条件": normalize_conditions(coupon),
    }


def format_log_rows(change_log: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows = []
    for item in sorted(change_log, key=lambda row: row.get("date", ""), reverse=True):
        rows.append({
            "日付": item.get("date", ""),
            "種別": item.get("type", ""),
            "カテゴリ": item.get("category", ""),
            "ID": item.get("id", ""),
            "タイトル": item.get("title", ""),
            "エリア/割引": item.get("discount", item.get("area", "")),
        })
    return rows


def next_action(provider: dict[str, Any], rows: list[dict[str, str]]) -> str:
    status = provider.get("coverage_status", "")
    if status == "auto_daily":
        return "日次監視を継続。差分が出たら記事更新候補へ回す。"
    if status == "master_import" and rows:
        return "公式取得スクレイパー化の候補。まず暫定データを目視確認。"
    if status == "article_exists" and rows:
        return "記事抽出データを目視確認し、公式取得元を決める。"
    if status == "article_exists":
        return "記事本文から現行クーポン枠を抽出し、取得元を決める。"
    if status == "manual_queue":
        return "公式/ASP/手入力の確認表を作る。"
    return "取得可否の初回調査が必要。"


def build_provider_payload(provider: dict[str, Any]) -> dict[str, Any]:
    coupons, latest_file, file_kind = load_latest_data(provider.get("data_dir"))
    rows = [format_coupon_row(coupon, provider, file_kind) for coupon in coupons]
    log_rows = format_log_rows(load_change_log(provider.get("data_dir")))
    active = sum(1 for row in rows if row["配布状況"] == "配布中")
    ended = sum(1 for row in rows if row["配布状況"] == "配布終了")
    review = sum(1 for row in rows if row["配布状況"] not in {"配布中", "配布終了"})
    coverage = provider.get("coverage_status", "")
    source_label = "未整備"
    if rows:
        if coverage == "auto_daily":
            source_label = "日次JSON"
        elif coverage == "master_import":
            source_label = "coupon-master暫定JSON"
        elif coverage == "article_exists":
            source_label = "記事抽出暫定JSON"
        else:
            source_label = "暫定JSON"
    elif provider.get("article_paths"):
        source_label = "記事HTMLあり"

    return {
        "id": provider["id"],
        "label": provider["label"],
        "site_targets": provider.get("site_targets", []),
        "classification": provider.get("classification", ""),
        "coverage_status": coverage,
        "coverage_label": COVERAGE_LABELS.get(coverage, coverage),
        "note": provider.get("note", ""),
        "latest_file": latest_file,
        "source_label": source_label,
        "article_count": len(provider.get("article_paths", [])),
        "rows": rows,
        "logs": log_rows,
        "summary": {
            "会社": provider["label"],
            "対象サイト": " / ".join(provider.get("site_targets", [])),
            "分類": provider.get("classification", ""),
            "取得状態": COVERAGE_LABELS.get(coverage, coverage),
            "件数": str(len(rows)),
            "配布中": str(active),
            "配布終了": str(ended),
            "要確認": str(review),
            "最新データ": latest_file or "なし",
            "データ元": source_label,
            "次アクション": next_action(provider, rows),
        },
    }


def build_dashboard_data() -> dict[str, Any]:
    providers = [build_provider_payload(provider) for provider in load_registry()]
    return {
        "generated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M JST"),
        "providers": providers,
        "summary_rows": [provider["summary"] for provider in providers],
        "columns": {
            "summary": SUMMARY_COLUMNS,
            "coupons": COMMON_COLUMNS,
            "logs": LOG_COLUMNS,
        },
    }


def generate_html(data: dict[str, Any]) -> str:
    data_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>旅行会社クーポン監視ダッシュボード</title>
<link href="https://unpkg.com/gridjs/dist/theme/mermaid.min.css" rel="stylesheet" />
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f6f7f9; color: #24313f; }}
.header {{ background: #172033; color: #fff; padding: 20px 24px; }}
.header h1 {{ font-size: 1.35rem; font-weight: 700; letter-spacing: 0; }}
.header .updated {{ font-size: 0.85rem; opacity: 0.78; margin-top: 5px; }}
.tabs {{ display: flex; flex-wrap: wrap; gap: 4px; background: #fff; border-bottom: 1px solid #dfe4ea; padding: 8px 12px 0; position: sticky; top: 0; z-index: 100; }}
.tab {{ padding: 10px 12px; cursor: pointer; border: 1px solid transparent; border-bottom: 3px solid transparent; background: none; color: #516071; font-size: 0.86rem; line-height: 1.2; }}
.tab:hover {{ background: #f4f7fb; color: #202b38; }}
.tab.active {{ color: #0f5caa; border-bottom-color: #0f5caa; font-weight: 700; }}
.tab-count {{ color: #738092; font-size: 0.75rem; margin-left: 3px; }}
.tab-content {{ display: none; padding: 20px 24px; }}
.tab-content.active {{ display: block; }}
.section {{ margin-bottom: 26px; }}
.section h2 {{ font-size: 1.08rem; margin-bottom: 12px; padding-left: 10px; border-left: 4px solid #0f5caa; }}
.note {{ background: #fff; border: 1px solid #dfe4ea; border-radius: 8px; padding: 12px; margin-bottom: 14px; color: #47566a; line-height: 1.6; font-size: 0.88rem; }}
.stats {{ display: flex; gap: 10px; margin-bottom: 14px; flex-wrap: wrap; }}
.stat {{ padding: 8px 12px; border-radius: 8px; font-size: 0.86rem; font-weight: 700; background: #fff; border: 1px solid #dfe4ea; }}
.stat.active {{ color: #146c43; background: #e9f7ef; border-color: #bde5cf; }}
.stat.ended {{ color: #a52834; background: #fdecef; border-color: #f4c2ca; }}
.stat.review {{ color: #7a5200; background: #fff5d6; border-color: #f0d98b; }}
.toolbar {{ display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; align-items: center; }}
.filter-btn,.copy-btn,.col-toggle-btn {{ padding: 6px 12px; border: 1px solid #cbd5e1; border-radius: 6px; background: #fff; cursor: pointer; font-size: 0.83rem; color: #2c3b4c; }}
.filter-btn.active {{ border-color: #0f5caa; background: #eaf3ff; color: #0f5caa; font-weight: 700; }}
.filter-btn.active-green {{ border-color: #16834f; background: #e9f7ef; color: #146c43; font-weight: 700; }}
.filter-btn.active-red {{ border-color: #b02a37; background: #fdecef; color: #a52834; font-weight: 700; }}
.filter-btn.active-yellow {{ border-color: #a66b00; background: #fff5d6; color: #7a5200; font-weight: 700; }}
.copy-btn {{ margin-left: auto; border-color: #0f5caa; background: #0f5caa; color: #fff; }}
.copy-btn.copied {{ background: #146c43; border-color: #146c43; }}
.col-panel {{ display: none; background: #fff; border: 1px solid #dfe4ea; border-radius: 8px; padding: 10px; margin-bottom: 12px; }}
.col-panel.open {{ display: flex; flex-wrap: wrap; gap: 6px; }}
.col-chip {{ padding: 4px 9px; border: 1px solid #cbd5e1; border-radius: 16px; background: #fff; cursor: pointer; font-size: 0.78rem; user-select: none; }}
.col-chip.on {{ border-color: #0f5caa; background: #eaf3ff; color: #0f5caa; }}
.col-chip.off {{ background: #f1f3f5; color: #9099a5; text-decoration: line-through; }}
.gridjs-td {{ font-size: 0.84rem; line-height: 1.5; max-width: 430px; white-space: normal; word-break: break-word; }}
.gridjs-th {{ white-space: nowrap; }}
.gridjs-wrapper {{ overflow-x: auto; }}
.gridjs-table {{ width: auto !important; min-width: 100%; }}
.status-active,.status-ended,.status-review {{ display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 0.78rem; font-weight: 700; }}
.status-active {{ background: #e9f7ef; color: #146c43; }}
.status-ended {{ background: #fdecef; color: #a52834; }}
.status-review {{ background: #fff5d6; color: #7a5200; }}
.empty {{ color: #7a8696; padding: 20px; background: #fff; border: 1px solid #dfe4ea; border-radius: 8px; }}
@media (max-width: 768px) {{
  .tab-content {{ padding: 12px; }}
  .tab {{ padding: 9px 10px; font-size: 0.8rem; }}
  .header {{ padding: 16px; }}
}}
</style>
</head>
<body>
<div class="header">
  <h1>旅行会社クーポン監視ダッシュボード</h1>
  <div class="updated">最終更新: {data["generated_at"]}</div>
</div>
<div class="tabs" id="tabs"></div>
<main id="contents"></main>
<script src="https://unpkg.com/gridjs/dist/gridjs.umd.js"></script>
<script>
const DATA = {data_json};

function escapeHtml(value) {{
  return String(value ?? '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
}}

function statusCell(value) {{
  if (value === '配布中') return gridjs.html('<span class="status-active">配布中</span>');
  if (value === '配布終了') return gridjs.html('<span class="status-ended">配布終了</span>');
  if (value) return gridjs.html('<span class="status-review">' + escapeHtml(value) + '</span>');
  return '';
}}

function linkCell(value) {{
  if (!value) return '';
  return gridjs.html(`<a href="${{escapeHtml(value)}}" target="_blank" rel="noopener" style="color:#0f5caa;">開く</a>`);
}}

function copyTableData(rows, columns, button) {{
  const header = columns.join('\\t');
  const body = rows.map(row => columns.map(col => String(row[col] || '').replace(/\\n/g, ' ')).join('\\t')).join('\\n');
  navigator.clipboard.writeText(header + '\\n' + body).then(() => {{
    if (!button) return;
    const text = button.textContent;
    button.textContent = 'コピー完了';
    button.classList.add('copied');
    setTimeout(() => {{
      button.textContent = text;
      button.classList.remove('copied');
    }}, 1600);
  }});
}}

function buildColumns(columns) {{
  return columns.map(col => {{
    const base = {{ name: col }};
    if (col === '詳細URL') {{ base.formatter = cell => linkCell(cell); base.width = '70px'; }}
    if (col === '配布状況') base.formatter = cell => statusCell(cell);
    if (['タイトル', '次アクション'].includes(col)) base.attributes = () => ({{ style: 'min-width:260px' }});
    if (col === '条件') base.attributes = () => ({{ style: 'min-width:300px' }});
    return base;
  }});
}}

function statsHtml(rows) {{
  const active = rows.filter(row => row['配布状況'] === '配布中').length;
  const ended = rows.filter(row => row['配布状況'] === '配布終了').length;
  const review = rows.filter(row => row['配布状況'] && !['配布中', '配布終了'].includes(row['配布状況'])).length;
  return `<div class="stats">
    <span class="stat">全 ${{rows.length}} 件</span>
    <span class="stat active">配布中 ${{active}} 件</span>
    <span class="stat ended">配布終了 ${{ended}} 件</span>
    <span class="stat review">要確認 ${{review}} 件</span>
  </div>`;
}}

function renderGrid(container, rows, columns, options = {{}}) {{
  if (!rows || rows.length === 0) {{
    container.innerHTML += '<div class="empty">表示できるクーポンデータはまだありません。</div>';
    return;
  }}
  let currentFilter = 'all';
  let visibleCols = [...columns];
  let grid = null;

  const toolbar = document.createElement('div');
  toolbar.className = 'toolbar';
  toolbar.innerHTML = `
    ${{options.filter ? '<button class="filter-btn active" data-filter="all">すべて</button><button class="filter-btn" data-filter="active">配布中</button><button class="filter-btn" data-filter="ended">配布終了</button><button class="filter-btn" data-filter="review">要確認</button>' : ''}}
    <button class="col-toggle-btn" type="button">列の表示</button>
    <button class="copy-btn" type="button">コピー</button>
  `;
  container.appendChild(toolbar);

  const colPanel = document.createElement('div');
  colPanel.className = 'col-panel';
  columns.forEach(col => {{
    const chip = document.createElement('span');
    chip.className = 'col-chip on';
    chip.textContent = col;
    chip.addEventListener('click', () => {{
      if (chip.classList.contains('on')) {{
        if (visibleCols.length <= 1) return;
        chip.className = 'col-chip off';
        visibleCols = visibleCols.filter(item => item !== col);
      }} else {{
        chip.className = 'col-chip on';
        const index = columns.indexOf(col);
        visibleCols.splice(visibleCols.reduce((pos, item) => columns.indexOf(item) < index ? pos + 1 : pos, 0), 0, col);
      }}
      rebuild();
    }});
    colPanel.appendChild(chip);
  }});
  container.appendChild(colPanel);

  const gridDiv = document.createElement('div');
  container.appendChild(gridDiv);

  function filteredRows() {{
    if (currentFilter === 'active') return rows.filter(row => row['配布状況'] === '配布中');
    if (currentFilter === 'ended') return rows.filter(row => row['配布状況'] === '配布終了');
    if (currentFilter === 'review') return rows.filter(row => row['配布状況'] && !['配布中', '配布終了'].includes(row['配布状況']));
    return rows;
  }}

  function rebuild() {{
    const body = filteredRows();
    grid.updateConfig({{
      columns: buildColumns(visibleCols),
      data: body.map(row => visibleCols.map(col => row[col] || '')),
    }}).forceRender();
  }}

  toolbar.querySelector('.col-toggle-btn').addEventListener('click', () => colPanel.classList.toggle('open'));
  toolbar.querySelector('.copy-btn').addEventListener('click', event => copyTableData(filteredRows(), visibleCols, event.currentTarget));
  toolbar.querySelectorAll('.filter-btn').forEach(button => {{
    button.addEventListener('click', () => {{
      currentFilter = button.dataset.filter;
      toolbar.querySelectorAll('.filter-btn').forEach(item => item.className = 'filter-btn');
      if (currentFilter === 'active') button.classList.add('active-green');
      else if (currentFilter === 'ended') button.classList.add('active-red');
      else if (currentFilter === 'review') button.classList.add('active-yellow');
      else button.classList.add('active');
      rebuild();
    }});
  }});

  grid = new gridjs.Grid({{
    columns: buildColumns(visibleCols),
    data: rows.map(row => visibleCols.map(col => row[col] || '')),
    search: true,
    sort: true,
    pagination: {{ limit: options.limit || 50 }},
    fixedHeader: true,
    language: {{
      search: {{ placeholder: '検索...' }},
      pagination: {{ previous: '前へ', next: '次へ', showing: '', of: '/', to: '〜', results: () => '件' }},
    }},
  }});
  grid.render(gridDiv);
}}

function renderSummary(container) {{
  const totalProviders = DATA.providers.length;
  const withRows = DATA.providers.filter(provider => provider.rows.length > 0).length;
  const autoProviders = DATA.providers.filter(provider => provider.coverage_status === 'auto_daily').length;
  const totalCoupons = DATA.providers.reduce((sum, provider) => sum + provider.rows.length, 0);
  container.innerHTML = `
    <div class="section">
      <h2>全社サマリー</h2>
      <div class="stats">
        <span class="stat">対象会社 ${{totalProviders}} 社</span>
        <span class="stat active">データあり ${{withRows}} 社</span>
        <span class="stat">自動取得 ${{autoProviders}} 社</span>
        <span class="stat">総クーポン ${{totalCoupons}} 件</span>
      </div>
    </div>
  `;
  renderGrid(container.querySelector('.section'), DATA.summary_rows, DATA.columns.summary, {{ limit: 50 }});
}}

function renderProvider(container, provider) {{
  const active = provider.rows.filter(row => row['配布状況'] === '配布中').length;
  const ended = provider.rows.filter(row => row['配布状況'] === '配布終了').length;
  const review = provider.rows.filter(row => row['配布状況'] && !['配布中', '配布終了'].includes(row['配布状況'])).length;
  container.innerHTML = `
    <div class="section">
      <h2>${{escapeHtml(provider.label)}}</h2>
      <div class="note">
        対象サイト: ${{escapeHtml(provider.site_targets.join(' / ') || '未設定')}}<br>
        取得状態: ${{escapeHtml(provider.coverage_label)}} / 分類: ${{escapeHtml(provider.classification)}} / 最新データ: ${{escapeHtml(provider.latest_file || 'なし')}}<br>
        ${{escapeHtml(provider.note || '')}}
      </div>
      <div class="stats">
        <span class="stat">全 ${{provider.rows.length}} 件</span>
        <span class="stat active">配布中 ${{active}} 件</span>
        <span class="stat ended">配布終了 ${{ended}} 件</span>
        <span class="stat review">要確認 ${{review}} 件</span>
      </div>
    </div>
  `;
  const section = container.querySelector('.section');
  renderGrid(section, provider.rows, DATA.columns.coupons, {{ filter: true, limit: 50 }});
  if (provider.logs && provider.logs.length > 0) {{
    const logSection = document.createElement('div');
    logSection.className = 'section';
    logSection.innerHTML = '<h2>変動ログ</h2>';
    container.appendChild(logSection);
    renderGrid(logSection, provider.logs, DATA.columns.logs, {{ limit: 50 }});
  }}
}}

function activate(tabId) {{
  document.querySelectorAll('.tab').forEach(tab => tab.classList.toggle('active', tab.dataset.tab === tabId));
  document.querySelectorAll('.tab-content').forEach(content => content.classList.toggle('active', content.id === tabId));
}}

function init() {{
  const tabs = document.getElementById('tabs');
  const contents = document.getElementById('contents');
  const summaryTab = document.createElement('button');
  summaryTab.className = 'tab active';
  summaryTab.dataset.tab = 'summary';
  summaryTab.textContent = '全社サマリー';
  tabs.appendChild(summaryTab);
  const summaryContent = document.createElement('div');
  summaryContent.id = 'summary';
  summaryContent.className = 'tab-content active';
  contents.appendChild(summaryContent);
  renderSummary(summaryContent);

  DATA.providers.forEach(provider => {{
    const tab = document.createElement('button');
    tab.className = 'tab';
    tab.dataset.tab = provider.id;
    tab.innerHTML = `${{escapeHtml(provider.label)}} <span class="tab-count">${{provider.rows.length}}</span>`;
    tabs.appendChild(tab);
    const content = document.createElement('div');
    content.id = provider.id;
    content.className = 'tab-content';
    contents.appendChild(content);
    renderProvider(content, provider);
  }});

  tabs.querySelectorAll('.tab').forEach(tab => tab.addEventListener('click', () => activate(tab.dataset.tab)));
}}

init();
</script>
</body>
</html>"""


def main() -> None:
    print("dashboard build")
    data = build_dashboard_data()
    out_dir = ROOT / "dashboard"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / "index.html"
    out_file.write_text(generate_html(data), encoding="utf-8")

    provider_count = len(data["providers"])
    provider_with_data = sum(1 for provider in data["providers"] if provider["rows"])
    coupon_count = sum(len(provider["rows"]) for provider in data["providers"])
    print(f"- providers: {provider_count}")
    print(f"- providers_with_data: {provider_with_data}")
    print(f"- coupons: {coupon_count}")
    print(f"- output: {out_file.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
