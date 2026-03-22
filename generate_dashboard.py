#!/usr/bin/env python3
"""
ダッシュボード HTML 生成スクリプト
==================================
JTB / KNT / HIS のクーポンデータを読み込み、
Grid.js テーブル付きの自己完結型 HTML を dashboard/index.html に出力する。
外部 API 認証不要。GitHub からダウンロードしてブラウザで開くだけで使える。
"""

import json
import os
from pathlib import Path
from datetime import datetime


# ============================================================
# データ読み込み（export_to_sheets.py と同じロジック）
# ============================================================
def load_latest_data(data_dir):
    data_path = Path(data_dir)
    if not data_path.exists():
        return []
    today = datetime.now().strftime("%Y-%m-%d")
    daily_file = data_path / f"coupons_{today}.json"
    if not daily_file.exists():
        files = sorted(data_path.glob("coupons_*.json"), reverse=True)
        if files:
            daily_file = files[0]
        else:
            return []
    with open(daily_file, "r", encoding="utf-8") as f:
        return json.load(f)


def load_change_log(data_dir):
    log_file = Path(data_dir) / "change_log.json"
    if log_file.exists():
        with open(log_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


# ============================================================
# データ整形（export_to_sheets.py のヘッダー・ソート順を踏襲）
# ============================================================
def format_jtb_rows(coupons):
    coupons.sort(key=lambda x: (
        0 if x.get("stock_status") == "配布中" else 1,
        x.get("area", ""),
    ))
    rows = []
    for c in coupons:
        detail = c.get("detail_data") or {}
        rows.append({
            "ID": c.get("id", ""),
            "詳細URL": c.get("detail_url", ""),
            "タイトル": c.get("title", ""),
            "配布状況": c.get("stock_status", "不明"),
            "割引額": c.get("discount", ""),
            "エリア": c.get("area", ""),
            "タイプ": c.get("type", ""),
            "予約対象期間": c.get("booking_period", ""),
            "宿泊/出発対象期間": c.get("stay_period", ""),
            "店舗利用": "✅" if c.get("store_available") else "",
            "クーポンコード": ", ".join(detail.get("coupon_codes", [])),
            "パスワード": ", ".join(detail.get("passwords", [])),
            "条件": " / ".join(filter(None, detail.get("conditions", []) + detail.get("notes", []))),
        })
    return rows


def format_knt_rows(coupons):
    coupons.sort(key=lambda x: (
        0 if x.get("stock_status") == "配布中" else 1,
        x.get("category", ""),
        x.get("area", ""),
    ))
    rows = []
    for c in coupons:
        detail = c.get("detail_data") or {}
        rows.append({
            "詳細URL": c.get("detail_url", ""),
            "タイトル": c.get("title", ""),
            "カテゴリ": c.get("category", ""),
            "ID": c.get("id", ""),
            "割引額": c.get("discount", "") or detail.get("discount", ""),
            "配布状況": c.get("stock_status", "不明"),
            "エリア": c.get("area", ""),
            "タイプ": c.get("type", ""),
            "申込期間": detail.get("booking_period", ""),
            "宿泊/出発対象期間": detail.get("stay_period", ""),
            "クーポンコード": ", ".join(detail.get("coupon_codes", [])),
            "条件": " / ".join(filter(None, detail.get("conditions", []) + detail.get("notes", []))),
        })
    return rows


def format_his_rows(coupons):
    coupons.sort(key=lambda x: (
        0 if x.get("stock_status") == "配布中" else 1,
        x.get("category", ""),
    ))
    rows = []
    for c in coupons:
        codes = c.get("coupon_codes", [])
        code_strs = [cc["code"] for cc in codes if isinstance(cc, dict)]
        cond_strs = [
            f'{cc.get("condition", "")}→{cc.get("discount", "")}'
            for cc in codes
            if isinstance(cc, dict) and (cc.get("condition") or cc.get("discount"))
        ]
        rows.append({
            "施策ページ": "https://www.his-j.com/campaign/shisaku/",
            "タイトル": c.get("title", ""),
            "カテゴリ": c.get("category", ""),
            "割引額": c.get("discount", ""),
            "配布状況": c.get("stock_status", "不明"),
            "予約期間": c.get("booking_period", ""),
            "出発/宿泊期間": c.get("travel_period", ""),
            "クーポンコード": " / ".join(code_strs),
            "条件": " / ".join(cond_strs),
            "対象商品": c.get("target", ""),
        })
    return rows


def format_change_log(change_log):
    change_log.sort(key=lambda x: x.get("date", ""), reverse=True)
    rows = []
    for e in change_log:
        rows.append({
            "日付": e.get("date", ""),
            "種別": e.get("type", ""),
            "カテゴリ": e.get("category", ""),
            "ID": e.get("id", ""),
            "タイトル": e.get("title", ""),
            "エリア/割引": e.get("discount", e.get("area", "")),
        })
    return rows


# ============================================================
# HTML テンプレート生成
# ============================================================
def generate_html(data, updated_at):
    """データを埋め込んだ自己完結型HTMLを生成"""
    data_json = json.dumps(data, ensure_ascii=False, indent=None)

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>クーポン監視ダッシュボード</title>
<link href="https://unpkg.com/gridjs/dist/theme/mermaid.min.css" rel="stylesheet" />
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f5f5; color: #333; }}
.header {{ background: linear-gradient(135deg, #1a1a2e, #16213e); color: #fff; padding: 20px 24px; }}
.header h1 {{ font-size: 1.4rem; font-weight: 600; }}
.header .updated {{ font-size: 0.85rem; opacity: 0.7; margin-top: 4px; }}
.tabs {{ display: flex; flex-wrap: wrap; background: #fff; border-bottom: 2px solid #e0e0e0; padding: 0 16px; position: sticky; top: 0; z-index: 100; }}
.tab {{ padding: 12px 20px; cursor: pointer; border: none; background: none; font-size: 0.95rem; color: #666; border-bottom: 3px solid transparent; transition: all 0.2s; }}
.tab:hover {{ color: #333; background: #f9f9f9; }}
.tab.active {{ color: #1a73e8; border-bottom-color: #1a73e8; font-weight: 600; }}
.tab-content {{ display: none; padding: 20px 24px; }}
.tab-content.active {{ display: block; }}
.section {{ margin-bottom: 32px; }}
.section h2 {{ font-size: 1.15rem; margin-bottom: 12px; padding-left: 12px; border-left: 4px solid #1a73e8; }}
.section h2.log {{ border-left-color: #f57c00; }}
.stats {{ display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }}
.stat {{ padding: 8px 16px; border-radius: 8px; font-size: 0.9rem; font-weight: 500; }}
.stat.active {{ background: #e8f5e9; color: #2e7d32; }}
.stat.ended {{ background: #fce4ec; color: #c62828; }}
.stat.total {{ background: #e3f2fd; color: #1565c0; }}
.toolbar {{ display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; align-items: center; }}
.filter-btn {{ padding: 6px 14px; border: 2px solid #ddd; border-radius: 6px; background: #fff; cursor: pointer; font-size: 0.85rem; transition: all 0.2s; }}
.filter-btn:hover {{ border-color: #999; }}
.filter-btn.active {{ border-color: #1a73e8; background: #e8f0fe; color: #1a73e8; font-weight: 600; }}
.filter-btn.active-green {{ border-color: #2e7d32; background: #e8f5e9; color: #2e7d32; font-weight: 600; }}
.filter-btn.active-red {{ border-color: #c62828; background: #fce4ec; color: #c62828; font-weight: 600; }}
.copy-btn {{ padding: 6px 14px; border: 2px solid #1a73e8; border-radius: 6px; background: #1a73e8; color: #fff; cursor: pointer; font-size: 0.85rem; transition: all 0.2s; margin-left: auto; }}
.copy-btn:hover {{ background: #1557b0; }}
.copy-btn.copied {{ background: #2e7d32; border-color: #2e7d32; }}
.filter-label {{ font-size: 0.85rem; color: #666; font-weight: 500; }}
.col-toggle-btn {{ padding: 6px 14px; border: 2px solid #7c4dff; border-radius: 6px; background: #fff; color: #7c4dff; cursor: pointer; font-size: 0.85rem; transition: all 0.2s; }}
.col-toggle-btn:hover {{ background: #f3e8ff; }}
.col-panel {{ display: none; background: #fff; border: 1px solid #e0e0e0; border-radius: 8px; padding: 12px; margin-bottom: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
.col-panel.open {{ display: flex; flex-wrap: wrap; gap: 6px; }}
.col-chip {{ padding: 4px 10px; border: 1.5px solid #ddd; border-radius: 16px; background: #fff; cursor: pointer; font-size: 0.8rem; transition: all 0.15s; user-select: none; }}
.col-chip.on {{ border-color: #1a73e8; background: #e8f0fe; color: #1a73e8; }}
.col-chip.off {{ border-color: #ccc; background: #f5f5f5; color: #999; text-decoration: line-through; }}
.gridjs-th {{ white-space: nowrap; position: relative; }}
.gridjs-td {{ font-size: 0.85rem; line-height: 1.5; max-width: 400px; white-space: normal; word-break: break-word; }}
.gridjs-wrapper {{ overflow-x: auto; }}
.gridjs-table {{ width: auto !important; min-width: 100%; }}
.gridjs-th .resize-handle {{ position: absolute; right: 0; top: 0; bottom: 0; width: 5px; cursor: col-resize; background: transparent; }}
.gridjs-th .resize-handle:hover {{ background: #1a73e8; }}
.status-active {{ background: #e8f5e9; color: #2e7d32; padding: 2px 8px; border-radius: 4px; font-weight: 500; font-size: 0.8rem; }}
.status-ended {{ background: #fce4ec; color: #c62828; padding: 2px 8px; border-radius: 4px; font-weight: 500; font-size: 0.8rem; }}
.log-new {{ background: #e8f5e9; }}
.log-lost {{ background: #fce4ec; }}
.log-ended {{ background: #fff3e0; }}
.log-resumed {{ background: #e3f2fd; }}
@media (max-width: 768px) {{
  .tabs {{ padding: 0 8px; }}
  .tab {{ padding: 10px 12px; font-size: 0.85rem; }}
  .tab-content {{ padding: 12px; }}
}}
</style>
</head>
<body>

<div class="header">
  <h1>クーポン監視ダッシュボード</h1>
  <div class="updated">最終更新: {updated_at}</div>
</div>

<div class="tabs" id="tabs">
  <button class="tab active" data-tab="jtb-domestic">JTB 国内</button>
  <button class="tab" data-tab="jtb-overseas">JTB 海外</button>
  <button class="tab" data-tab="knt">KNT</button>
  <button class="tab" data-tab="his">HIS</button>
  <button class="tab" data-tab="jtb-log">JTB 変動ログ</button>
  <button class="tab" data-tab="knt-log">KNT 変動ログ</button>
  <button class="tab" data-tab="his-log">HIS 変動ログ</button>
</div>

<div class="tab-content active" id="jtb-domestic"></div>
<div class="tab-content" id="jtb-overseas"></div>
<div class="tab-content" id="knt"></div>
<div class="tab-content" id="his"></div>
<div class="tab-content" id="jtb-log"></div>
<div class="tab-content" id="knt-log"></div>
<div class="tab-content" id="his-log"></div>

<script src="https://unpkg.com/gridjs/dist/gridjs.umd.js"></script>
<script>
const DATA = {data_json};

// タブ切替
document.querySelectorAll('.tab').forEach(tab => {{
  tab.addEventListener('click', () => {{
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById(tab.dataset.tab).classList.add('active');
  }});
}});

function statusCell(val) {{
  if (val === '配布中') return gridjs.html('<span class="status-active">配布中</span>');
  if (val === '配布終了') return gridjs.html('<span class="status-ended">配布終了</span>');
  return val;
}}

function linkCell(val) {{
  if (!val) return '';
  return gridjs.html(`<a href="${{val}}" target="_blank" rel="noopener" style="color:#1a73e8;font-size:0.8rem;">🔗 開く</a>`);
}}

function makeStats(rows) {{
  const active = rows.filter(r => r['配布状況'] === '配布中').length;
  const ended = rows.filter(r => r['配布状況'] === '配布終了').length;
  return `<div class="stats">
    <span class="stat total">全 ${{rows.length}} 件</span>
    <span class="stat active">配布中 ${{active}} 件</span>
    <span class="stat ended">配布終了 ${{ended}} 件</span>
  </div>`;
}}

function copyTableData(rows, columns) {{
  const header = columns.join('\\t');
  const body = rows.map(r => columns.map(c => (r[c] || '').replace(/\\n/g, ' ')).join('\\t')).join('\\n');
  const text = header + '\\n' + body;
  navigator.clipboard.writeText(text).then(() => {{
    event.target.textContent = '✅ コピー完了';
    event.target.classList.add('copied');
    setTimeout(() => {{
      event.target.textContent = '📋 テーブルをコピー';
      event.target.classList.remove('copied');
    }}, 2000);
  }});
}}

function addResizeHandles(gridDiv) {{
  setTimeout(() => {{
    gridDiv.querySelectorAll('th.gridjs-th').forEach(th => {{
      if (th.querySelector('.resize-handle')) return;
      const handle = document.createElement('div');
      handle.className = 'resize-handle';
      th.style.position = 'relative';
      th.appendChild(handle);
      let startX, startW;
      handle.addEventListener('mousedown', (e) => {{
        e.preventDefault();
        e.stopPropagation();
        startX = e.pageX;
        startW = th.offsetWidth;
        const onMove = (e2) => {{
          th.style.width = Math.max(40, startW + e2.pageX - startX) + 'px';
          th.style.minWidth = th.style.width;
        }};
        const onUp = () => {{
          document.removeEventListener('mousemove', onMove);
          document.removeEventListener('mouseup', onUp);
        }};
        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
      }});
    }});
  }}, 300);
}}

function renderTable(containerId, allRows, columns) {{
  const el = document.getElementById(containerId);
  if (!allRows || allRows.length === 0) {{
    el.innerHTML = '<div class="section"><p style="color:#999;padding:20px;">データなし</p></div>';
    return;
  }}

  let currentFilter = 'all';
  let gridInstance = null;
  let visibleCols = [...columns];

  function getFilteredRows() {{
    if (currentFilter === 'active') return allRows.filter(r => r['配布状況'] === '配布中');
    if (currentFilter === 'ended') return allRows.filter(r => r['配布状況'] === '配布終了');
    return allRows;
  }}

  function buildCols(cols) {{
    const narrowCols = ['配布状況', '店舗利用', 'エリア'];
    const minWidthCols = {{ 'タイトル': '250px', 'ID': '100px' }};
    return cols.map(col => {{
      const base = {{ name: col }};
      if (col === '配布状況') base.formatter = (cell) => statusCell(cell);
      if (['詳細URL', '施策ページ'].includes(col)) {{ base.formatter = (cell) => linkCell(cell); base.width = '70px'; }}
      if (narrowCols.includes(col)) base.width = 'auto';
      if (minWidthCols[col]) base.attributes = (cell) => ({{ style: `min-width:${{minWidthCols[col]}}` }});
      return base;
    }});
  }}

  function rebuildGrid() {{
    const filtered = getFilteredRows();
    gridInstance.updateConfig({{
      columns: buildCols(visibleCols),
      data: filtered.map(r => visibleCols.map(c => r[c] || '')),
    }}).forceRender();
    addResizeHandles(gridDiv);
  }}

  // ヘッダー
  const statsHtml = makeStats(allRows);
  const tableDiv = document.createElement('div');
  tableDiv.className = 'section';
  tableDiv.innerHTML = `<h2>クーポン一覧</h2>${{statsHtml}}`;

  // ツールバー（フィルタ + 列切替 + コピー）
  const toolbar = document.createElement('div');
  toolbar.className = 'toolbar';
  toolbar.innerHTML = `
    <span class="filter-label">抽出:</span>
    <button class="filter-btn active" data-filter="all">すべて</button>
    <button class="filter-btn" data-filter="active">配布中のみ</button>
    <button class="filter-btn" data-filter="ended">配布終了のみ</button>
    <button class="col-toggle-btn" id="coltgl-${{containerId}}">⚙ 列の表示</button>
    <button class="copy-btn" id="copy-${{containerId}}">📋 コピー</button>
  `;
  tableDiv.appendChild(toolbar);

  // 列表示切替パネル
  const colPanel = document.createElement('div');
  colPanel.className = 'col-panel';
  colPanel.id = `colpanel-${{containerId}}`;
  columns.forEach(col => {{
    const chip = document.createElement('span');
    chip.className = 'col-chip on';
    chip.textContent = col;
    chip.dataset.col = col;
    chip.addEventListener('click', () => {{
      if (chip.classList.contains('on')) {{
        if (visibleCols.length <= 1) return;
        chip.classList.remove('on');
        chip.classList.add('off');
        visibleCols = visibleCols.filter(c => c !== col);
      }} else {{
        chip.classList.remove('off');
        chip.classList.add('on');
        const idx = columns.indexOf(col);
        visibleCols.splice(visibleCols.reduce((pos, c) => columns.indexOf(c) < idx ? pos + 1 : pos, 0), 0, col);
      }}
      rebuildGrid();
    }});
    colPanel.appendChild(chip);
  }});
  tableDiv.appendChild(colPanel);

  const gridDiv = document.createElement('div');
  tableDiv.appendChild(gridDiv);
  el.appendChild(tableDiv);

  // 列切替パネルの開閉
  document.getElementById(`coltgl-${{containerId}}`).addEventListener('click', () => {{
    colPanel.classList.toggle('open');
  }});

  // フィルタボタン
  toolbar.querySelectorAll('.filter-btn').forEach(btn => {{
    btn.addEventListener('click', () => {{
      currentFilter = btn.dataset.filter;
      toolbar.querySelectorAll('.filter-btn').forEach(b => {{
        b.classList.remove('active', 'active-green', 'active-red');
      }});
      if (currentFilter === 'active') btn.classList.add('active-green');
      else if (currentFilter === 'ended') btn.classList.add('active-red');
      else btn.classList.add('active');
      rebuildGrid();
    }});
  }});

  // コピーボタン
  document.getElementById(`copy-${{containerId}}`).addEventListener('click', (event) => {{
    copyTableData(getFilteredRows(), visibleCols);
  }});

  // Grid.js 初期描画
  gridInstance = new gridjs.Grid({{
    columns: buildCols(columns),
    data: allRows.map(r => columns.map(c => r[c] || '')),
    search: true,
    sort: true,
    pagination: {{ limit: 50 }},
    fixedHeader: true,
    language: {{
      search: {{ placeholder: '🔍 検索...' }},
      pagination: {{
        previous: '← 前へ',
        next: '次へ →',
        showing: '',
        of: '/',
        to: '〜',
        results: () => '件',
      }},
    }},
  }});
  gridInstance.render(gridDiv);
  addResizeHandles(gridDiv);
}}

function renderLogTable(containerId, rows, title) {{
  const el = document.getElementById(containerId);
  if (!rows || rows.length === 0) {{
    el.innerHTML = '<div class="section"><p style="color:#999;padding:20px;">データなし</p></div>';
    return;
  }}
  const tableDiv = document.createElement('div');
  tableDiv.className = 'section';
  tableDiv.innerHTML = `<h2 class="log">${{title}}</h2>`;

  const toolbar = document.createElement('div');
  toolbar.className = 'toolbar';
  toolbar.innerHTML = `<button class="copy-btn" id="copy-${{containerId}}">📋 テーブルをコピー</button>`;
  tableDiv.appendChild(toolbar);

  const gridDiv = document.createElement('div');
  tableDiv.appendChild(gridDiv);
  el.appendChild(tableDiv);

  const columns = ['日付', '種別', 'カテゴリ', 'ID', 'タイトル', 'エリア/割引'];

  document.getElementById(`copy-${{containerId}}`).addEventListener('click', (event) => {{
    copyTableData(rows, columns);
  }});

  new gridjs.Grid({{
    columns: columns.map(c => {{
      const base = {{ name: c }};
      if (c === 'タイトル') base.attributes = (cell) => ({{ style: 'min-width:250px' }});
      return base;
    }}),
    data: rows.map(r => columns.map(c => r[c] || '')),
    search: true,
    sort: true,
    pagination: {{ limit: 50 }},
    fixedHeader: true,
    className: {{
      tr: (row) => {{
        if (!row || !row.cells) return '';
        const type = row.cells[1]?.data || '';
        if (type.includes('新規')) return 'log-new';
        if (type.includes('消失')) return 'log-lost';
        if (type.includes('配布終了')) return 'log-ended';
        if (type.includes('再開')) return 'log-resumed';
        return '';
      }},
    }},
    language: {{
      search: {{ placeholder: '🔍 検索...' }},
      pagination: {{
        previous: '← 前へ',
        next: '次へ →',
        showing: '',
        of: '/',
        to: '〜',
        results: () => '件',
      }},
    }},
  }}).render(gridDiv);
}}

// テーブル描画
const jtbDomCols = ['ID', '詳細URL', 'タイトル', '配布状況', '割引額', 'エリア', 'タイプ', '予約対象期間', '宿泊/出発対象期間', '店舗利用', 'クーポンコード', 'パスワード', '条件'];
const kntCols = ['詳細URL', 'タイトル', 'カテゴリ', 'ID', '割引額', '配布状況', 'エリア', 'タイプ', '申込期間', '宿泊/出発対象期間', 'クーポンコード', '条件'];
const hisCols = ['施策ページ', 'タイトル', 'カテゴリ', '割引額', '配布状況', '予約期間', '出発/宿泊期間', 'クーポンコード', '条件', '対象商品'];

renderTable('jtb-domestic', DATA.jtb_domestic, jtbDomCols);
renderTable('jtb-overseas', DATA.jtb_overseas, jtbDomCols);
renderTable('knt', DATA.knt, kntCols);
renderTable('his', DATA.his, hisCols);
renderLogTable('jtb-log', DATA.jtb_log, 'JTB 変動ログ');
renderLogTable('knt-log', DATA.knt_log, 'KNT 変動ログ');
renderLogTable('his-log', DATA.his_log, 'HIS 変動ログ');
</script>
</body>
</html>"""
    return html


# ============================================================
# メイン
# ============================================================
def main():
    print("📊 ダッシュボード HTML 生成開始")

    # JTB
    jtb_coupons = load_latest_data("./jtb_coupon_data")
    jtb_log = load_change_log("./jtb_coupon_data")
    jtb_domestic = [c for c in jtb_coupons if c.get("category") == "国内"]
    jtb_overseas = [c for c in jtb_coupons if c.get("category") == "海外"]
    print(f"  JTB: {len(jtb_coupons)}件（国内={len(jtb_domestic)}, 海外={len(jtb_overseas)}）")

    # KNT
    knt_coupons = load_latest_data("./knt_coupon_data")
    knt_log = load_change_log("./knt_coupon_data")
    print(f"  KNT: {len(knt_coupons)}件")

    # HIS
    his_coupons = load_latest_data("./his_coupon_data")
    his_log = load_change_log("./his_coupon_data")
    print(f"  HIS: {len(his_coupons)}件")

    # データ整形
    data = {
        "jtb_domestic": format_jtb_rows(jtb_domestic),
        "jtb_overseas": format_jtb_rows(jtb_overseas),
        "knt": format_knt_rows(knt_coupons),
        "his": format_his_rows(his_coupons),
        "jtb_log": format_change_log(jtb_log),
        "knt_log": format_change_log(knt_log),
        "his_log": format_change_log(his_log),
    }

    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M JST")
    html = generate_html(data, updated_at)

    # 出力
    out_dir = Path("dashboard")
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / "index.html"
    out_file.write_text(html, encoding="utf-8")

    print(f"\n✅ ダッシュボード生成完了: {out_file}")
    print(f"   JTB国内: {len(data['jtb_domestic'])}件, JTB海外: {len(data['jtb_overseas'])}件")
    print(f"   KNT: {len(data['knt'])}件, HIS: {len(data['his'])}件")
    print(f"   変動ログ: JTB={len(data['jtb_log'])}件, KNT={len(data['knt_log'])}件, HIS={len(data['his_log'])}件")


if __name__ == "__main__":
    main()
