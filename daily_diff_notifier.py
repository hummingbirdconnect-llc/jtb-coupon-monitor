#!/usr/bin/env python3
"""
日次クーポン差分通知スクリプト
================================================================
JTB・KNTのクーポン日次JSONスナップショットを比較し、
フィールドレベルの変更を検出してHTMLメールで通知する。

使い方:
  python daily_diff_notifier.py           # 通常実行
  python daily_diff_notifier.py --dry-run # メール送信せずにHTMLをstdoutに出力
================================================================
"""

import json
import os
import smtplib
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# ============================================================
# 設定
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

SERVICES = {
    "JTB": {
        "data_dir": BASE_DIR / "jtb_coupon_data",
        "top_fields": {
            "title": "タイトル",
            "discount": "割引額",
            "area": "エリア",
            "type": "タイプ",
            "booking_period": "予約対象期間",
            "stay_period": "宿泊/出発対象期間",
            "store_available": "店舗利用可",
            "stock_status": "配布状況",
        },
        "detail_fields": {
            "coupon_codes": "クーポンコード",
            "passwords": "パスワード",
            "conditions": "条件",
        },
        "has_stock_status": True,
    },
    "KNT": {
        "data_dir": BASE_DIR / "knt_coupon_data",
        "top_fields": {
            "title": "タイトル",
            "discount": "割引額",
            "area": "エリア",
            "type": "タイプ",
        },
        "detail_fields": {
            "discount": "割引額(詳細)",
            "conditions": "条件",
            "booking_period": "申込期間",
            "stay_period": "宿泊/出発対象期間",
            "coupon_codes": "クーポンコード",
        },
        "has_stock_status": False,
    },
}

# ============================================================
# データ構造
# ============================================================


@dataclass
class FieldChange:
    field: str
    label: str
    old: str
    new: str


@dataclass
class CouponModification:
    coupon_id: str
    title: str
    category: str
    changes: list = field(default_factory=list)


@dataclass
class DiffResult:
    service_name: str
    today_date: str
    prev_date: str
    added: list = field(default_factory=list)
    removed: list = field(default_factory=list)
    stock_changes: list = field(default_factory=list)
    field_changes: list = field(default_factory=list)
    today_count: int = 0
    prev_count: int = 0

    @property
    def total_changes(self):
        return (
            len(self.added)
            + len(self.removed)
            + len(self.stock_changes)
            + len(self.field_changes)
        )

    @property
    def has_changes(self):
        return self.total_changes > 0


# ============================================================
# データ読み込み
# ============================================================


def load_json(filepath):
    """JSONファイルを読み込む。存在しない場合はNoneを返す。"""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"  Warning: {filepath} の読み込みに失敗: {e}")
        return None


def find_snapshot_files(data_dir, today_str):
    """今日と直前のスナップショットファイルを探す。"""
    today_file = data_dir / f"coupons_{today_str}.json"

    # 直前のファイルを検索（昨日とは限らない）
    all_files = sorted(data_dir.glob("coupons_*.json"))
    prev_files = [f for f in all_files if f.name < f"coupons_{today_str}.json"]

    prev_file = prev_files[-1] if prev_files else None

    return today_file, prev_file


# ============================================================
# 値の正規化・比較ヘルパー
# ============================================================


def normalize_value(val):
    """比較用に値を正規化する。"""
    if val is None:
        return ""
    if isinstance(val, bool):
        return "はい" if val else "いいえ"
    if isinstance(val, list):
        # リスト要素を正規化してソート
        normalized = sorted([str(v).strip() for v in val if str(v).strip()])
        return ", ".join(normalized)
    return str(val).strip()


def format_display_value(val):
    """表示用に値をフォーマットする。"""
    normalized = normalize_value(val)
    return normalized if normalized else "(なし)"


# ============================================================
# 差分検出エンジン
# ============================================================


def compare_snapshots(prev_coupons, today_coupons, service_config):
    """2日分のスナップショットを比較し、差分を返す。"""
    top_fields = service_config["top_fields"]
    detail_fields = service_config["detail_fields"]
    has_stock = service_config["has_stock_status"]

    prev_map = {c["id"]: c for c in prev_coupons}
    today_map = {c["id"]: c for c in today_coupons}

    prev_ids = set(prev_map.keys())
    today_ids = set(today_map.keys())

    # 新規・消失
    added = [today_map[cid] for cid in sorted(today_ids - prev_ids)]
    removed = [prev_map[cid] for cid in sorted(prev_ids - today_ids)]

    # 継続クーポンのフィールド比較
    stock_changes = []
    field_changes = []

    for cid in sorted(today_ids & prev_ids):
        prev = prev_map[cid]
        curr = today_map[cid]
        changes = []

        # トップレベルフィールドの比較
        for fld, label in top_fields.items():
            old_val = normalize_value(prev.get(fld))
            new_val = normalize_value(curr.get(fld))
            if old_val != new_val:
                if fld == "stock_status" and has_stock:
                    stock_changes.append(
                        {
                            "id": cid,
                            "title": curr.get("title", ""),
                            "category": curr.get("category", ""),
                            "old_status": format_display_value(prev.get(fld)),
                            "new_status": format_display_value(curr.get(fld)),
                        }
                    )
                else:
                    changes.append(
                        FieldChange(
                            field=fld,
                            label=label,
                            old=format_display_value(prev.get(fld)),
                            new=format_display_value(curr.get(fld)),
                        )
                    )

        # detail_data フィールドの比較
        prev_detail = prev.get("detail_data") or {}
        curr_detail = curr.get("detail_data") or {}
        for fld, label in detail_fields.items():
            old_val = normalize_value(prev_detail.get(fld))
            new_val = normalize_value(curr_detail.get(fld))
            if old_val != new_val:
                changes.append(
                    FieldChange(
                        field=f"detail.{fld}",
                        label=label,
                        old=format_display_value(prev_detail.get(fld)),
                        new=format_display_value(curr_detail.get(fld)),
                    )
                )

        if changes:
            field_changes.append(
                CouponModification(
                    coupon_id=cid,
                    title=curr.get("title", ""),
                    category=curr.get("category", ""),
                    changes=changes,
                )
            )

    return added, removed, stock_changes, field_changes


# ============================================================
# HTML メール生成
# ============================================================

STYLE = """
<style>
body { font-family: 'Helvetica Neue', Arial, 'Hiragino Sans', sans-serif; color: #333; margin: 0; padding: 0; background: #f5f5f5; }
.container { max-width: 700px; margin: 20px auto; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
.header { background: #2c3e50; color: #fff; padding: 20px 24px; }
.header h1 { margin: 0; font-size: 20px; font-weight: 600; }
.header .date { font-size: 13px; opacity: 0.8; margin-top: 4px; }
.summary { padding: 16px 24px; background: #f8f9fa; border-bottom: 1px solid #e9ecef; }
.summary-row { display: flex; justify-content: space-between; align-items: center; padding: 4px 0; }
.summary-label { font-weight: 600; font-size: 15px; }
.badge { display: inline-block; padding: 2px 10px; border-radius: 12px; font-size: 13px; font-weight: 600; margin-left: 6px; }
.badge-new { background: #d4edda; color: #155724; }
.badge-removed { background: #f8d7da; color: #721c24; }
.badge-stock { background: #fff3cd; color: #856404; }
.badge-modified { background: #e2e3e5; color: #383d41; }
.section { padding: 16px 24px; border-bottom: 1px solid #eee; }
.section-title { font-size: 17px; font-weight: 700; margin: 0 0 12px 0; padding-bottom: 8px; border-bottom: 2px solid #2c3e50; }
.subsection { margin: 12px 0; }
.subsection-title { font-size: 14px; font-weight: 600; margin: 0 0 8px 0; }
table { width: 100%; border-collapse: collapse; font-size: 13px; margin-bottom: 8px; }
th { background: #f1f3f5; text-align: left; padding: 6px 10px; border: 1px solid #dee2e6; font-weight: 600; }
td { padding: 6px 10px; border: 1px solid #dee2e6; vertical-align: top; }
tr.added { background: #d4edda; }
tr.removed { background: #f8d7da; }
.change-item { margin: 8px 0; padding: 10px 14px; background: #f8f9fa; border-radius: 6px; border-left: 3px solid #6c757d; }
.change-item-title { font-weight: 600; font-size: 14px; margin-bottom: 6px; }
.change-item-id { font-size: 12px; color: #6c757d; }
.change-detail { font-size: 13px; margin: 3px 0; }
.old-val { color: #dc3545; text-decoration: line-through; }
.new-val { color: #28a745; font-weight: 600; }
.arrow { color: #6c757d; margin: 0 4px; }
.footer { padding: 12px 24px; background: #f8f9fa; font-size: 12px; color: #6c757d; text-align: center; }
.no-changes { padding: 20px; text-align: center; color: #6c757d; font-size: 14px; }
</style>
"""


def build_coupon_table(coupons, css_class=""):
    """クーポンリストをHTMLテーブルに変換する。"""
    if not coupons:
        return ""
    rows = []
    for c in coupons:
        cat = _esc(c.get("category", ""))
        title = _esc(c.get("title", ""))
        discount = _esc(c.get("discount", "")) or _esc(
            (c.get("detail_data") or {}).get("discount", "")
        )
        area = _esc(c.get("area", ""))
        stock = _esc(c.get("stock_status", ""))
        rows.append(
            f'<tr class="{css_class}">'
            f"<td>{cat}</td><td>{title}</td>"
            f"<td>{discount or '(なし)'}</td><td>{area or '-'}</td>"
            f"<td>{stock or '-'}</td></tr>"
        )
    return (
        "<table>"
        "<tr><th>分類</th><th>タイトル</th><th>割引</th><th>エリア</th><th>状態</th></tr>"
        + "".join(rows)
        + "</table>"
    )


def build_stock_change_table(stock_changes):
    """配布状況変更テーブルを生成する。"""
    if not stock_changes:
        return ""
    rows = []
    for sc in stock_changes:
        title = _esc(sc["title"])
        old_s = _esc(sc["old_status"])
        new_s = _esc(sc["new_status"])
        rows.append(
            f"<tr><td>{_esc(sc['category'])}</td><td>{title}</td>"
            f'<td><span class="old-val">{old_s}</span>'
            f'<span class="arrow"> → </span>'
            f'<span class="new-val">{new_s}</span></td></tr>'
        )
    return (
        "<table>"
        "<tr><th>分類</th><th>タイトル</th><th>変更</th></tr>"
        + "".join(rows)
        + "</table>"
    )


def build_field_changes_html(field_changes):
    """フィールド変更をHTMLに変換する。"""
    if not field_changes:
        return ""
    items = []
    for mod in field_changes:
        change_lines = []
        for ch in mod.changes:
            change_lines.append(
                f'<div class="change-detail">'
                f"<strong>{_esc(ch.label)}:</strong> "
                f'<span class="old-val">{_esc(ch.old)}</span>'
                f'<span class="arrow"> → </span>'
                f'<span class="new-val">{_esc(ch.new)}</span>'
                f"</div>"
            )
        items.append(
            f'<div class="change-item">'
            f'<div class="change-item-title">{_esc(mod.title)}</div>'
            f'<div class="change-item-id">ID: {_esc(mod.coupon_id)} / {_esc(mod.category)}</div>'
            + "".join(change_lines)
            + "</div>"
        )
    return "".join(items)


def build_service_section(diff):
    """1サービス分のHTMLセクションを生成する。"""
    if not diff.has_changes:
        return (
            f'<div class="section">'
            f'<h2 class="section-title">{_esc(diff.service_name)}</h2>'
            f'<div class="no-changes">変動なし（{diff.today_count}件）</div>'
            f"</div>"
        )

    parts = []
    parts.append(f'<div class="section">')
    parts.append(
        f'<h2 class="section-title">{_esc(diff.service_name)}'
        f" ({diff.prev_count}件 → {diff.today_count}件)</h2>"
    )

    if diff.added:
        parts.append(f'<div class="subsection">')
        parts.append(
            f'<h3 class="subsection-title">&#x1F195; 新規 ({len(diff.added)}件)</h3>'
        )
        parts.append(build_coupon_table(diff.added, "added"))
        parts.append("</div>")

    if diff.removed:
        parts.append(f'<div class="subsection">')
        parts.append(
            f'<h3 class="subsection-title">&#x274C; 消失 ({len(diff.removed)}件)</h3>'
        )
        parts.append(build_coupon_table(diff.removed, "removed"))
        parts.append("</div>")

    if diff.stock_changes:
        parts.append(f'<div class="subsection">')
        parts.append(
            f'<h3 class="subsection-title">&#x1F534;&#x1F7E2; 配布状況変更 ({len(diff.stock_changes)}件)</h3>'
        )
        parts.append(build_stock_change_table(diff.stock_changes))
        parts.append("</div>")

    if diff.field_changes:
        parts.append(f'<div class="subsection">')
        parts.append(
            f'<h3 class="subsection-title">&#x270F;&#xFE0F; フィールド変更 ({len(diff.field_changes)}件)</h3>'
        )
        parts.append(build_field_changes_html(diff.field_changes))
        parts.append("</div>")

    parts.append("</div>")
    return "".join(parts)


def build_summary_badges(diff):
    """サマリー用のバッジHTMLを生成する。"""
    badges = []
    if diff.added:
        badges.append(f'<span class="badge badge-new">新規 {len(diff.added)}</span>')
    if diff.removed:
        badges.append(
            f'<span class="badge badge-removed">消失 {len(diff.removed)}</span>'
        )
    if diff.stock_changes:
        badges.append(
            f'<span class="badge badge-stock">配布変更 {len(diff.stock_changes)}</span>'
        )
    if diff.field_changes:
        badges.append(
            f'<span class="badge badge-modified">変更 {len(diff.field_changes)}</span>'
        )
    if not badges:
        badges.append('<span style="color:#6c757d">変動なし</span>')
    return " ".join(badges)


def build_html_email(diffs, today_str):
    """全サービスのHTMLメールを生成する。"""
    # サマリー
    summary_rows = []
    for diff in diffs:
        summary_rows.append(
            f'<div class="summary-row">'
            f'<span class="summary-label">{_esc(diff.service_name)}</span>'
            f"<span>{build_summary_badges(diff)}</span>"
            f"</div>"
        )

    # 各サービスセクション
    sections = [build_service_section(diff) for diff in diffs]

    # 比較対象日
    prev_dates = set(d.prev_date for d in diffs if d.prev_date)
    comparison_info = (
        f"{', '.join(sorted(prev_dates))} → {today_str}"
        if prev_dates
        else today_str
    )

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head><meta charset="UTF-8">{STYLE}</head>
<body>
<div class="container">
  <div class="header">
    <h1>クーポン変動レポート</h1>
    <div class="date">{_esc(today_str)}</div>
  </div>
  <div class="summary">
    {''.join(summary_rows)}
  </div>
  {''.join(sections)}
  <div class="footer">
    送信時刻: {_esc(now_str)} / 比較対象: {_esc(comparison_info)}
  </div>
</div>
</body>
</html>"""
    return html


def build_plain_text(diffs, today_str):
    """プレーンテキストのフォールバックを生成する。"""
    lines = [f"クーポン変動レポート {today_str}", "=" * 40, ""]
    for diff in diffs:
        lines.append(f"■ {diff.service_name} ({diff.prev_count} → {diff.today_count}件)")
        if not diff.has_changes:
            lines.append("  変動なし")
        else:
            if diff.added:
                lines.append(f"  新規: {len(diff.added)}件")
                for c in diff.added:
                    lines.append(f"    - {c.get('title', '')} ({c.get('discount', '')})")
            if diff.removed:
                lines.append(f"  消失: {len(diff.removed)}件")
                for c in diff.removed:
                    lines.append(f"    - {c.get('title', '')} ({c.get('discount', '')})")
            if diff.stock_changes:
                lines.append(f"  配布状況変更: {len(diff.stock_changes)}件")
                for sc in diff.stock_changes:
                    lines.append(f"    - {sc['title']}: {sc['old_status']} → {sc['new_status']}")
            if diff.field_changes:
                lines.append(f"  フィールド変更: {len(diff.field_changes)}件")
                for mod in diff.field_changes:
                    lines.append(f"    [{mod.title}]")
                    for ch in mod.changes:
                        lines.append(f"      {ch.label}: {ch.old} → {ch.new}")
        lines.append("")
    return "\n".join(lines)


def _esc(text):
    """HTMLエスケープ。"""
    if text is None:
        return ""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ============================================================
# メール送信
# ============================================================


def send_email(subject, html_body, plain_body):
    """Gmail SMTP でメールを送信する。"""
    gmail_address = os.environ.get("GMAIL_ADDRESS", "")
    gmail_app_password = os.environ.get("GMAIL_APP_PASSWORD", "")
    notify_email = os.environ.get("NOTIFY_EMAIL", "")

    if not all([gmail_address, gmail_app_password, notify_email]):
        print("  Warning: メール認証情報が未設定のためスキップします")
        print(
            "  必要な環境変数: GMAIL_ADDRESS, GMAIL_APP_PASSWORD, NOTIFY_EMAIL"
        )
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"JTB/KNT Monitor <{gmail_address}>"
    msg["To"] = notify_email

    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    recipients = [addr.strip() for addr in notify_email.split(",")]

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_address, gmail_app_password)
            server.sendmail(gmail_address, recipients, msg.as_string())
        print(f"  メール送信成功 → {notify_email}")
        return True
    except smtplib.SMTPAuthenticationError:
        print("  Error: Gmail認証に失敗しました。アプリパスワードを確認してください。")
        return False
    except smtplib.SMTPException as e:
        print(f"  Error: SMTP送信エラー: {e}")
        return False


# ============================================================
# メイン処理
# ============================================================


def main():
    dry_run = "--dry-run" in sys.argv

    print("=" * 50)
    print("日次クーポン差分通知")
    print("=" * 50)

    today_str = datetime.now().strftime("%Y-%m-%d")
    print(f"対象日: {today_str}")

    all_diffs = []
    any_changes = False

    for service_name, config in SERVICES.items():
        print(f"\n--- {service_name} ---")
        data_dir = config["data_dir"]

        today_file, prev_file = find_snapshot_files(data_dir, today_str)

        if not today_file.exists():
            print(f"  今日のスナップショットが見つかりません: {today_file.name}")
            all_diffs.append(
                DiffResult(
                    service_name=service_name,
                    today_date=today_str,
                    prev_date="",
                )
            )
            continue

        today_data = load_json(today_file)
        if today_data is None:
            print(f"  今日のデータ読み込みに失敗")
            all_diffs.append(
                DiffResult(
                    service_name=service_name,
                    today_date=today_str,
                    prev_date="",
                )
            )
            continue

        print(f"  今日: {today_file.name} ({len(today_data)}件)")

        if prev_file is None:
            print("  前回のスナップショットが見つかりません（初回実行）")
            all_diffs.append(
                DiffResult(
                    service_name=service_name,
                    today_date=today_str,
                    prev_date="",
                    today_count=len(today_data),
                )
            )
            continue

        prev_data = load_json(prev_file)
        if prev_data is None:
            print(f"  前回のデータ読み込みに失敗")
            all_diffs.append(
                DiffResult(
                    service_name=service_name,
                    today_date=today_str,
                    prev_date="",
                    today_count=len(today_data),
                )
            )
            continue

        # 前回の日付をファイル名から抽出
        prev_date_str = prev_file.stem.replace("coupons_", "")
        print(f"  前回: {prev_file.name} ({len(prev_data)}件)")

        # 差分検出
        added, removed, stock_changes, field_changes = compare_snapshots(
            prev_data, today_data, config
        )

        diff = DiffResult(
            service_name=service_name,
            today_date=today_str,
            prev_date=prev_date_str,
            added=added,
            removed=removed,
            stock_changes=stock_changes,
            field_changes=field_changes,
            today_count=len(today_data),
            prev_count=len(prev_data),
        )
        all_diffs.append(diff)

        if diff.has_changes:
            any_changes = True

        # サマリー出力
        print(f"  新規: {len(added)}件, 消失: {len(removed)}件, "
              f"配布変更: {len(stock_changes)}件, フィールド変更: {len(field_changes)}件")

    # 変更がなければスキップ
    if not any_changes:
        print("\n全サービスで変動なし。メール送信をスキップします。")
        return

    # メール生成
    html_body = build_html_email(all_diffs, today_str)
    plain_body = build_plain_text(all_diffs, today_str)

    # 件名
    change_parts = []
    for diff in all_diffs:
        change_parts.append(f"{diff.service_name}: {diff.total_changes}件")
    subject = f"[クーポン変動] {today_str} {' / '.join(change_parts)}"

    if dry_run:
        print(f"\n--- DRY RUN ---")
        print(f"件名: {subject}")
        print(f"--- HTML ---")
        print(html_body)
        print(f"\n--- プレーンテキスト ---")
        print(plain_body)
    else:
        print(f"\n件名: {subject}")
        send_email(subject, html_body, plain_body)

    print("\n完了")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\nError: 差分通知スクリプトでエラーが発生: {e}")
        import traceback
        traceback.print_exc()
        print("ワークフローを続行します。")
        sys.exit(0)
