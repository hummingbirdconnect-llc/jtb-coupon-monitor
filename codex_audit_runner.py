#!/usr/bin/env python3
"""Codex監査結果を検証し、クーポンJSONとWP候補へ安全に反映する。"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from deal_audit_schema import (
    AUDIT_SCHEMA_VERSION,
    approved_deals,
    audit_is_high_confidence,
    validate_audit_result,
)
from official_deal_monitor import _merge_without_deleting, _semantic_hash, convert_deals

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent
QUEUE_ROOT = ROOT / "codex_audit_queue"
RESULT_ROOT = ROOT / "codex_audit_results"
AUDIT_ROOT = ROOT / "codex_audit_data"
APPLIED_LEDGER = AUDIT_ROOT / "applied-candidates.json"
RUN_SUMMARY = AUDIT_ROOT / "run-latest.json"
DRY_RUN_SUMMARY = AUDIT_ROOT / "run-dry-run-latest.json"


def now_jst() -> datetime:
    return datetime.now(JST)


def configure_root(root: Path) -> None:
    """テストや別checkout向けに入出力ルートをまとめて切り替える。"""
    global ROOT, QUEUE_ROOT, RESULT_ROOT, AUDIT_ROOT, APPLIED_LEDGER, RUN_SUMMARY, DRY_RUN_SUMMARY
    ROOT = root
    QUEUE_ROOT = root / "codex_audit_queue"
    RESULT_ROOT = root / "codex_audit_results"
    AUDIT_ROOT = root / "codex_audit_data"
    APPLIED_LEDGER = AUDIT_ROOT / "applied-candidates.json"
    RUN_SUMMARY = AUDIT_ROOT / "run-latest.json"
    DRY_RUN_SUMMARY = AUDIT_ROOT / "run-dry-run-latest.json"


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


def load_applied_ledger() -> dict[str, Any]:
    return load_json(APPLIED_LEDGER, {"candidates": {}})


def result_path_for(candidate: dict[str, Any]) -> Path:
    return RESULT_ROOT / str(candidate["provider_id"]) / f"{candidate['candidate_id']}.json"


def candidate_files() -> list[Path]:
    return sorted(QUEUE_ROOT.glob("*/*.json")) if QUEUE_ROOT.exists() else []


def pending_candidates() -> list[dict[str, Any]]:
    applied = load_applied_ledger().get("candidates") or {}
    pending: list[dict[str, Any]] = []
    for path in candidate_files():
        candidate = load_json(path, {})
        candidate_id = str(candidate.get("candidate_id") or "")
        if not candidate_id or candidate_id in applied:
            continue
        result_path = result_path_for(candidate)
        pending.append(
            {
                "candidate_id": candidate_id,
                "provider_id": candidate.get("provider_id"),
                "provider_label": candidate.get("provider_label"),
                "change_kind": candidate.get("change_kind"),
                "fetched_at": candidate.get("fetched_at"),
                "candidate_path": str(path.relative_to(ROOT)),
                "result_path": str(result_path.relative_to(ROOT)),
                "result_status": "ready" if result_path.exists() else "needs_codex_audit",
            }
        )
    return pending


def result_template(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "candidate_id": candidate["candidate_id"],
        "provider_id": candidate["provider_id"],
        "page_summary": "",
        "change_summary": "",
        "recommendation": "hold",
        "priority": 0,
        "uncertainty_reasons": ["Codex監査未実施"],
        "audit_notes": "",
        "deals": [],
    }


def _latest_coupons(data_dir: str, fallback: list[dict[str, Any]]) -> list[dict[str, Any]]:
    path = ROOT / data_dir
    files = sorted(path.glob("coupons_*.json"), reverse=True) if path.exists() else []
    if not files:
        return fallback
    loaded = load_json(files[0], fallback)
    return loaded if isinstance(loaded, list) else fallback


def _write_coupons(data_dir: str, coupons: list[dict[str, Any]], at: datetime) -> Path:
    path = ROOT / data_dir / f"coupons_{at.strftime('%Y-%m-%d')}.json"
    write_json(path, coupons)
    return path


def _state_path(provider_id: str) -> Path:
    return ROOT / "official_source_data" / provider_id / "state.json"


def _record_state(
    candidate: dict[str, Any],
    *,
    status: str,
    output: Path | None = None,
    semantic_hash: str = "",
    semantic_changed: bool = False,
) -> None:
    path = _state_path(str(candidate["provider_id"]))
    state = load_json(path, {})
    at = now_jst()
    state.update(
        {
            "last_audit_status": status,
            "last_audited_at": at.isoformat(),
            "last_audit_candidate_id": candidate["candidate_id"],
        }
    )
    if status == "processed":
        state.update(
            {
                "processed_hash": candidate["content_hash"],
                "last_processed_at": at.isoformat(),
                "last_changed_at": at.isoformat()
                if semantic_changed
                else state.get("last_changed_at", ""),
                "latest_file": output.name if output else state.get("latest_file", ""),
                "semantic_hash": semantic_hash,
                "queued_hash": "",
                "queued_candidate_id": "",
            }
        )
    write_json(path, state)


def apply_candidate(
    candidate: dict[str, Any], result: dict[str, Any], *, dry_run: bool = False
) -> dict[str, Any]:
    errors = validate_audit_result(candidate, result)
    high_confidence = audit_is_high_confidence(result, errors)
    base = {
        "candidate_id": candidate.get("candidate_id"),
        "provider_id": candidate.get("provider_id"),
        "provider_label": candidate.get("provider_label"),
        "change_kind": candidate.get("change_kind"),
        "recommendation": result.get("recommendation"),
        "priority": result.get("priority", 0),
        "change_summary": result.get("change_summary", ""),
        "validation_errors": errors,
        "high_confidence": high_confidence,
        "wp_review_eligible": False,
    }
    if errors:
        return {**base, "status": "validation_error"}

    recommendation = result.get("recommendation")
    if (
        recommendation == "ignore"
        and candidate.get("change_kind") == "update"
        and not (result.get("uncertainty_reasons") or [])
        and not approved_deals(result)
    ):
        previous = _latest_coupons(
            str(candidate["data_dir"]),
            list(candidate.get("previous_coupons") or []),
        )
        if not dry_run:
            _record_state(
                candidate,
                status="processed",
                semantic_hash=_semantic_hash(previous),
                semantic_changed=False,
            )
        return {
            **base,
            "status": "dry_run_ignored" if dry_run else "processed",
            "semantic_changed": False,
            "coupon_count": len(previous),
            "ignored_non_deal_change": True,
        }

    if recommendation == "hold" or not high_confidence:
        if not dry_run:
            _record_state(candidate, status="held")
        return {
            **base,
            "status": "dry_run_held" if dry_run else "held",
            "hold_reasons": result.get("uncertainty_reasons") or ["high confidenceではありません"],
        }

    accepted = approved_deals(result)
    fetched_at = now_jst()
    current_coupons = convert_deals(
        str(candidate["provider_id"]),
        {"deals": accepted},
        fetched_at.isoformat(),
        "codex-scheduled-audit",
    )
    previous = _latest_coupons(
        str(candidate["data_dir"]),
        list(candidate.get("previous_coupons") or []),
    )
    merged, retained_missing = _merge_without_deleting(current_coupons, previous)
    semantic_hash = _semantic_hash(merged)
    semantic_changed = semantic_hash != _semantic_hash(previous)
    output = None
    if not dry_run:
        output = _write_coupons(str(candidate["data_dir"]), merged, fetched_at)
        _record_state(
            candidate,
            status="processed",
            output=output,
            semantic_hash=semantic_hash,
            semantic_changed=semantic_changed,
        )

    wp_eligible = bool(
        candidate.get("change_kind") == "update"
        and result.get("recommendation") == "draft"
        and semantic_changed
        and high_confidence
        and not retained_missing
    )
    return {
        **base,
        "status": "dry_run_validated" if dry_run else "processed",
        "semantic_changed": semantic_changed,
        "data_date": fetched_at.strftime("%Y-%m-%d"),
        "latest_file": output.name if output else "",
        "coupon_count": len(merged),
        "retained_missing_ids": retained_missing,
        "wp_review_eligible": wp_eligible,
        "wp_block_reason": "前回データから消えた項目を要確認で保持"
        if retained_missing
        else "",
    }


def apply_all(*, dry_run: bool = False) -> dict[str, Any]:
    ledger = load_applied_ledger()
    applied = ledger.setdefault("candidates", {})
    audits: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    for item in pending_candidates():
        candidate_path = ROOT / item["candidate_path"]
        result_path = ROOT / item["result_path"]
        candidate = load_json(candidate_path, {})
        if not result_path.exists():
            waiting.append(item)
            continue
        result = load_json(result_path, {})
        audit = apply_candidate(candidate, result, dry_run=dry_run)
        audits.append(audit)
        if not dry_run and audit["status"] in {"processed", "held"}:
            applied[str(candidate["candidate_id"])] = {
                "provider_id": candidate["provider_id"],
                "status": audit["status"],
                "recommendation": audit.get("recommendation"),
                "applied_at": now_jst().isoformat(),
                "result_path": str(result_path.relative_to(ROOT)),
            }

    eligible = [audit for audit in audits if audit.get("wp_review_eligible")]
    summary = {
        "completed_at": now_jst().isoformat(),
        "dry_run": dry_run,
        "audit_count": len(audits),
        "waiting_count": len(waiting),
        "validation_error_count": sum(audit["status"] == "validation_error" for audit in audits),
        "wp_review_eligible_provider_ids": sorted(
            {str(audit["provider_id"]) for audit in eligible}
        ),
        "eligible_candidates": eligible,
        "audits": audits,
        "waiting_candidates": waiting,
    }
    write_json(DRY_RUN_SUMMARY if dry_run else RUN_SUMMARY, summary)
    if not dry_run:
        write_json(APPLIED_LEDGER, ledger)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Codex公式クーポン監査の検証・適用")
    subparsers = parser.add_subparsers(dest="command", required=True)

    pending = subparsers.add_parser("pending", help="未監査候補を一覧表示")
    pending.add_argument("--json", action="store_true")

    template = subparsers.add_parser("template", help="監査結果テンプレートを作成")
    template.add_argument("--candidate", required=True)
    template.add_argument("--output", default="")

    validate = subparsers.add_parser("validate", help="監査結果を検証")
    validate.add_argument("--candidate", required=True)
    validate.add_argument("--result", required=True)

    apply_parser = subparsers.add_parser("apply-all", help="監査済み候補を一括適用")
    apply_parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "pending":
        items = pending_candidates()
        if args.json:
            print(json.dumps(items, ensure_ascii=False, indent=2))
        else:
            for item in items:
                print(f"{item['candidate_id']}: {item['result_status']} -> {item['result_path']}")
        return

    if args.command == "template":
        candidate_path = Path(args.candidate)
        candidate = load_json(candidate_path, {})
        payload = result_template(candidate)
        output = Path(args.output) if args.output else result_path_for(candidate)
        write_json(output, payload)
        print(output)
        return

    if args.command == "validate":
        candidate = load_json(Path(args.candidate), {})
        result = load_json(Path(args.result), {})
        errors = validate_audit_result(candidate, result)
        print(json.dumps({"ok": not errors, "errors": errors}, ensure_ascii=False, indent=2))
        if errors:
            raise SystemExit(1)
        return

    summary = apply_all(dry_run=args.dry_run)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary["validation_error_count"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
