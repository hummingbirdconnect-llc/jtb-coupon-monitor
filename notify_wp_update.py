#!/usr/bin/env python3
"""
WP更新結果通知スクリプト
========================
wp_coupon_updater.py の結果JSONを読み、問題があればメールで通知する。
daily_diff_notifier.py のメール送信ロジックを流用。

Usage:
    python notify_wp_update.py
    python notify_wp_update.py --dry-run
"""

import json
import os
import smtplib
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
RESULT_FILE = SCRIPT_DIR / "wp_update_result.json"


def load_results() -> list[dict]:
    if not RESULT_FILE.exists():
        print("ℹ️ wp_update_result.json が見つかりません（WP更新未実行）")
        return []
    with open(RESULT_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def build_html(results: list[dict], today_str: str) -> str:
    rows = []
    has_issues = False

    for r in results:
        status = r.get("status", "?")
        ota = r.get("ota", "").upper()
        slug = r.get("slug", "")

        if status == "updated":
            emoji = "✅"
            detail = f'{r.get("tables_updated",0)}テーブル更新, {r.get("rows",0)}行'
        elif status == "dry_run":
            emoji = "📝"
            detail = "DRY-RUN（更新なし）"
        elif status == "blocked":
            emoji = "🚨"
            detail = f'ブロック: {r.get("reason","")}'
            has_issues = True
        elif status == "error":
            emoji = "❌"
            detail = f'エラー: {r.get("reason","")}'
            has_issues = True
        else:
            emoji = "❓"
            detail = status

        unmatched = r.get("unmatched_coupons", 0)
        link_issues = r.get("link_issues", [])

        row = f"<tr><td>{emoji} {ota}</td><td>{slug}</td><td>{detail}</td>"
        if unmatched > 0:
            has_issues = True
            titles = "<br>".join(
                f"- {li.get('title','')[:40]} ({li.get('category','')})"
                for li in link_issues[:5]
            )
            row += f"<td>⚠️ {unmatched}件未マッチ<br><small>{titles}</small></td>"
        else:
            row += "<td>-</td>"
        row += "</tr>"
        rows.append(row)

    table = (
        "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse;font-size:14px;'>"
        "<tr style='background:#2c3e50;color:#fff;'>"
        "<th>OTA</th><th>ページ</th><th>結果</th><th>アフィリエイト</th></tr>"
        + "".join(rows)
        + "</table>"
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head><body style="font-family:sans-serif;">
<h2>WordPress クーポンページ更新レポート</h2>
<p>日時: {today_str}</p>
{table}
<p style="color:#888;font-size:12px;">自動送信 by wp_coupon_updater</p>
</body></html>"""


def build_plain(results: list[dict], today_str: str) -> str:
    lines = [f"WPクーポン更新レポート {today_str}", "=" * 40]
    for r in results:
        lines.append(f"{r.get('ota','').upper()} ({r.get('slug','')}): {r.get('status','')}")
        if r.get("reason"):
            lines.append(f"  理由: {r['reason']}")
        if r.get("unmatched_coupons", 0) > 0:
            lines.append(f"  ⚠️ 未マッチ: {r['unmatched_coupons']}件")
    return "\n".join(lines)


def send_email(subject: str, html_body: str, plain_body: str) -> bool:
    gmail = os.environ.get("GMAIL_ADDRESS", "")
    app_pw = os.environ.get("GMAIL_APP_PASSWORD", "")
    to = os.environ.get("NOTIFY_EMAIL", "")

    if not all([gmail, app_pw, to]):
        print("  ⚠️ メール認証情報が未設定のためスキップ")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Coupon Monitor <{gmail}>"
    msg["To"] = to

    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    recipients = [a.strip() for a in to.split(",")]
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail, app_pw)
            server.sendmail(gmail, recipients, msg.as_string())
        print(f"  📧 メール送信成功 → {to}")
        return True
    except Exception as e:
        print(f"  ❌ メール送信失敗: {e}")
        return False


def main():
    dry_run = "--dry-run" in sys.argv
    today_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    results = load_results()
    if not results:
        print("通知対象なし")
        return

    # 問題があるかチェック
    has_issues = any(
        r.get("status") in ("blocked", "error") or r.get("unmatched_coupons", 0) > 0
        for r in results
    )

    html = build_html(results, today_str)
    plain = build_plain(results, today_str)

    prefix = "🚨" if has_issues else "✅"
    subject = f"{prefix} [WPクーポン更新] {datetime.now().strftime('%Y-%m-%d')}"

    if dry_run:
        print(f"件名: {subject}")
        print(plain)
    else:
        send_email(subject, html, plain)

    print("完了")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"通知エラー: {e}")
        sys.exit(0)  # 通知失敗でもワークフローは止めない
