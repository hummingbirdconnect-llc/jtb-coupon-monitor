#!/usr/bin/env python3
"""Codex監査済み差分を1日5件までWordPressレビュー下書きへ送る。"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from wp_coupon_updater import RESULT_FILE as LEGACY_RESULT_FILE
from wp_coupon_updater import load_site_config, load_sites_config, review_page

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent
RUN_SUMMARY = ROOT / "codex_audit_data" / "run-latest.json"
QUEUE_FILE = ROOT / "codex_audit_data" / "wp-review-queue.json"
LEDGER_FILE = ROOT / "codex_audit_data" / "wp-draft-ledger.json"
OVERFLOW_FILE = ROOT / "codex_audit_data" / "wp-overflow-latest.json"
RESULT_FILE = ROOT / "wp_auto_review_result.json"
HARD_AUTO_DRAFT_LIMIT = 5


def now_jst() -> datetime:
    return datetime.now(JST)


def configure_root(root: Path) -> None:
    """テストや別checkout向けに入出力ルートをまとめて切り替える。"""
    global ROOT, RUN_SUMMARY, QUEUE_FILE, LEDGER_FILE, OVERFLOW_FILE, RESULT_FILE
    ROOT = root
    RUN_SUMMARY = root / "codex_audit_data" / "run-latest.json"
    QUEUE_FILE = root / "codex_audit_data" / "wp-review-queue.json"
    LEDGER_FILE = root / "codex_audit_data" / "wp-draft-ledger.json"
    OVERFLOW_FILE = root / "codex_audit_data" / "wp-overflow-latest.json"
    RESULT_FILE = root / "wp_auto_review_result.json"


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _credentials_ready(site_config: dict) -> bool:
    return all(site_config.get(key) for key in ("wp_url", "wp_user", "wp_app_password"))


def eligible_candidates(run_summary: dict) -> list[dict[str, Any]]:
    candidates = run_summary.get("eligible_candidates") or []
    if candidates:
        return [dict(candidate) for candidate in candidates]
    return [
        {
            "candidate_id": f"legacy-{provider_id}",
            "provider_id": provider_id,
            "priority": 50,
            "change_summary": "高確度の公式差分",
        }
        for provider_id in run_summary.get("wp_review_eligible_provider_ids") or []
    ]


def review_targets(sites_config: dict, provider_id: str) -> list[tuple[str, dict]]:
    targets: list[tuple[str, dict]] = []
    for site_id, site in (sites_config.get("sites") or {}).items():
        for page in site.get("pages") or []:
            if page.get("ota") != provider_id:
                continue
            if not page.get("update_enabled", True) or not page.get("auto_review_enabled", False):
                continue
            targets.append((site_id, page))
    return targets


def _expand_queue_items(run_summary: dict, sites_config: dict) -> tuple[list[dict], list[dict]]:
    items: list[dict] = []
    unmapped: list[dict] = []
    for candidate in eligible_candidates(run_summary):
        provider_id = str(candidate.get("provider_id") or "")
        targets = review_targets(sites_config, provider_id)
        if not targets:
            unmapped.append(
                {
                    "candidate_id": candidate.get("candidate_id"),
                    "provider_id": provider_id,
                    "status": "skipped",
                    "reason": "auto_review_enabled の記事マッピングなし",
                }
            )
            continue
        for site_id, page in targets:
            candidate_id = str(candidate.get("candidate_id") or f"legacy-{provider_id}")
            target_id = f"{candidate_id}|{site_id}|{page['slug']}"
            items.append(
                {
                    "target_id": target_id,
                    "candidate_id": candidate_id,
                    "provider_id": provider_id,
                    "site_id": site_id,
                    "slug": page["slug"],
                    "label": page.get("label", page["slug"]),
                    "priority": int(candidate.get("priority") or 0),
                    "change_summary": candidate.get("change_summary", ""),
                    "queued_at": now_jst().isoformat(),
                    "status": "pending",
                    "reason": "",
                }
            )
    return items, unmapped


def _merge_queue(existing: dict, new_items: list[dict]) -> dict:
    items = existing.setdefault("items", [])
    known = {item.get("target_id") for item in items}
    for item in new_items:
        if item["target_id"] not in known:
            items.append(item)
            known.add(item["target_id"])
    return existing


def _today_draft_count(ledger: dict, today: str) -> int:
    return sum(
        1
        for entry in ledger.get("entries") or []
        if entry.get("date") == today and entry.get("counts_toward_limit", True)
    )


def _sort_key(item: dict) -> tuple[int, str, str]:
    return (-int(item.get("priority") or 0), str(item.get("queued_at") or ""), item["target_id"])


def run_reviews(
    run_summary: dict,
    *,
    dry_run: bool = False,
    approved_extra_drafts: int = 0,
    retry_attention: bool = False,
    current: datetime | None = None,
) -> dict[str, Any]:
    current = current or now_jst()
    today = current.astimezone(JST).strftime("%Y-%m-%d")
    sites_config = load_sites_config()
    new_items, unmapped = _expand_queue_items(run_summary, sites_config)
    queue = _merge_queue(load_json(QUEUE_FILE, {"items": []}), new_items)
    if retry_attention and not dry_run:
        for item in queue.get("items") or []:
            if item.get("status") == "attention":
                item.update({"status": "pending", "reason": "", "retried_at": now_jst().isoformat()})
    ledger = load_json(LEDGER_FILE, {"entries": []})

    automatic_limit = HARD_AUTO_DRAFT_LIMIT
    effective_limit = automatic_limit + max(0, int(approved_extra_drafts))
    created_before = _today_draft_count(ledger, today)
    available = max(0, effective_limit - created_before)
    results: list[dict[str, Any]] = list(unmapped)
    simulated_handled_ids: set[str] = set()

    os.environ["WP_PROTECT_HUMAN_DRAFTS"] = "true"
    pending = sorted(
        [item for item in queue.get("items") or [] if item.get("status") == "pending"],
        key=_sort_key,
    )
    for item in pending:
        if available <= 0:
            continue
        site_id = item["site_id"]
        page = next(
            (
                page
                for page in (sites_config.get("sites", {}).get(site_id, {}).get("pages") or [])
                if page.get("slug") == item["slug"] and page.get("ota") == item["provider_id"]
            ),
            None,
        )
        if not page:
            result = {**item, "status": "attention", "reason": "記事マッピングが見つかりません"}
            results.append(result)
            if not dry_run:
                item.update({"status": "attention", "reason": result["reason"]})
            continue

        site_config = load_site_config(site_id)
        if not _credentials_ready(site_config):
            result = {**item, "status": "attention", "reason": "WordPress認証情報が未設定"}
            results.append(result)
            continue

        try:
            review = review_page(site_config, page, dry_run=dry_run)
        except Exception as exc:
            review = {
                "site_id": site_id,
                "slug": item["slug"],
                "ota": item["provider_id"],
                "status": "error",
                "reason": str(exc),
            }
        review.update(
            {
                "target_id": item["target_id"],
                "candidate_id": item["candidate_id"],
                "priority": item["priority"],
                "change_summary": item.get("change_summary", ""),
            }
        )
        results.append(review)

        status = review.get("status")
        if status in {"review_ready", "dry_run"}:
            available -= 1
            if dry_run:
                simulated_handled_ids.add(item["target_id"])
            if not dry_run:
                item.update(
                    {
                        "status": "completed",
                        "completed_at": now_jst().isoformat(),
                        "reason": "",
                    }
                )
                ledger.setdefault("entries", []).append(
                    {
                        "date": today,
                        "target_id": item["target_id"],
                        "candidate_id": item["candidate_id"],
                        "provider_id": item["provider_id"],
                        "site_id": site_id,
                        "slug": item["slug"],
                        "created_at": now_jst().isoformat(),
                        "counts_toward_limit": True,
                    }
                )
        elif status == "no_change":
            if dry_run:
                simulated_handled_ids.add(item["target_id"])
            if not dry_run:
                item.update(
                    {
                        "status": "completed",
                        "completed_at": now_jst().isoformat(),
                        "reason": "差分なし",
                    }
                )
        elif not dry_run and status == "error":
            item.update({"last_error": review.get("reason") or "一時的なWP処理エラー"})
        elif not dry_run:
            item.update(
                {
                    "status": "attention",
                    "reason": review.get("reason") or f"WP処理結果: {status}",
                }
            )

    pending_after = [
        item
        for item in queue.get("items") or []
        if item.get("status") == "pending" and item.get("target_id") not in simulated_handled_ids
    ]
    attention = [item for item in queue.get("items") or [] if item.get("status") == "attention"]
    attention_ids = {item.get("target_id") for item in attention}
    attention.extend(
        result
        for result in results
        if result.get("status") == "attention" and result.get("target_id") not in attention_ids
    )
    limit_reached = bool(pending_after and available <= 0)
    created_after = created_before + sum(
        result.get("status") in {"review_ready", "dry_run"} for result in results
    )
    overflow = {
        "generated_at": now_jst().isoformat(),
        "dry_run": dry_run,
        "date": today,
        "automatic_daily_limit": automatic_limit,
        "approved_extra_drafts": max(0, int(approved_extra_drafts)),
        "drafts_created_before_run": created_before,
        "drafts_created_or_simulated_after_run": created_after,
        "pending_count": len(pending_after),
        "attention_count": len(attention),
        "needs_user_input": bool(limit_reached or attention),
        "question": (
            f"本日の自動下書き上限{automatic_limit}件に達しました。"
            f"残り{len(pending_after)}件を本日追加実行しますか、それとも翌日へ回しますか。"
            if limit_reached
            else (f"確認が必要なWordPress候補が{len(attention)}件あります。内容を確認しますか。" if attention else "")
        ),
        "recommended_action": "翌日に公式情報を再確認して持ち越す"
        if limit_reached
        else ("認証またはブロック理由を確認してから再実行する" if attention else ""),
        "pending": pending_after,
        "attention": attention,
    }
    summary = {
        "completed_at": now_jst().isoformat(),
        "dry_run": dry_run,
        "automatic_daily_limit": automatic_limit,
        "approved_extra_drafts": max(0, int(approved_extra_drafts)),
        "drafts_created_before_run": created_before,
        "drafts_created_or_simulated_after_run": created_after,
        "results": results,
        "overflow": overflow,
    }

    if not dry_run:
        write_json(QUEUE_FILE, queue)
        write_json(LEDGER_FILE, ledger)
    write_json(OVERFLOW_FILE, overflow)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Codex監査済み差分のWordPressレビュー下書き連携")
    parser.add_argument("--run-summary", default=str(RUN_SUMMARY))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--approved-extra-drafts",
        type=int,
        default=0,
        help="ユーザー承認後の手動追加分。定期実行では指定しない",
    )
    parser.add_argument(
        "--retry-attention",
        action="store_true",
        help="ユーザー確認後、attention状態の候補を再試行する",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    path = Path(args.run_summary)
    run_summary = load_json(path, {})
    summary = run_reviews(
        run_summary,
        dry_run=args.dry_run,
        approved_extra_drafts=args.approved_extra_drafts,
        retry_attention=args.retry_attention,
    )
    write_json(RESULT_FILE, summary)
    write_json(LEGACY_RESULT_FILE, summary["results"])
    for result in summary["results"]:
        print(
            f"{result.get('site_id', '-')}/{result.get('slug', '-')}: "
            f"{result.get('status')} {result.get('reason', '')}"
        )
    if summary["overflow"]["needs_user_input"]:
        print(summary["overflow"]["question"])


if __name__ == "__main__":
    main()
