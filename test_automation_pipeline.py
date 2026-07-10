#!/usr/bin/env python3
"""取得候補・Codex監査・WP日次上限・下書き保護の回帰テスト。"""

from __future__ import annotations

import json
import tempfile
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch

import codex_audit_runner as audit_runner
import official_deal_monitor as monitor
import provider_check_runner as runner
import wp_coupon_updater as updater
import wp_review_orchestrator as wp_orchestrator
from deal_audit_schema import validate_audit_result

SOURCE_URL = "https://example.com/deals"
SOURCE_TEXT = (
    "夏セール 2026年7月10日から2026年7月31日まで 最大3,000円OFF "
    "クーポンコード SUMMER3000 対象商品限定"
)


def valid_result(candidate_id: str, discount: str = "最大3,000円OFF") -> dict:
    evidence = SOURCE_TEXT.replace("3,000", "4,000") if "4,000" in discount else SOURCE_TEXT
    return {
        "schema_version": 1,
        "candidate_id": candidate_id,
        "provider_id": "sample",
        "page_summary": "夏セール",
        "change_summary": "割引内容を確認",
        "recommendation": "draft",
        "priority": 70,
        "uncertainty_reasons": [],
        "audit_notes": "公式ページ本文だけで確認",
        "deals": [
            {
                "title": "夏セール",
                "campaign_type": "coupon",
                "status": "active",
                "classification": "publishable",
                "discount": discount,
                "coupon_code": "SUMMER3000",
                "booking_start": "2026-07-10",
                "booking_end": "2026-07-31",
                "travel_start": None,
                "travel_end": None,
                "eligibility": "対象商品限定",
                "official_url": SOURCE_URL,
                "evidence_quote": evidence,
                "confidence": "high",
            }
        ],
    }


def test_registry_frequency_counts() -> None:
    providers = runner.load_registry()
    assert len(providers) == 44
    assert sum(provider["cadence_days"] == 1 for provider in providers) == 17
    assert sum(provider["cadence_days"] == 5 for provider in providers) == 27
    five_day = [provider for provider in providers if provider["cadence_days"] == 5]
    start = date(2026, 7, 10)
    for provider in five_day:
        due_count = sum(
            runner.provider_due(provider, date.fromordinal(start.toordinal() + offset))
            for offset in range(5)
        )
        assert due_count == 1, provider["id"]


def test_http_404_and_429_are_not_success() -> None:
    response_404 = Mock(status_code=404)
    response_429 = Mock(status_code=429)
    with patch.object(runner.requests, "head", side_effect=[response_404, response_429]):
        not_found = runner.check_url("https://example.com/missing", 1)
        rate_limited = runner.check_url("https://example.com/limited", 1)
    assert not not_found["ok"] and not_found["classification"] == "not_found"
    assert not rate_limited["ok"] and rate_limited["classification"] == "rate_limited"


def test_official_fetch_queues_candidate_then_codex_applies_update() -> None:
    provider = {
        "id": "sample",
        "label": "Sample",
        "data_dir": "official_coupon_data/sample",
        "official_domains": ["example.com"],
        "official_sources": [{"url": SOURCE_URL, "fetch_method": "auto"}],
    }
    page_text = {"value": SOURCE_TEXT}

    def fake_fetch(_source, timeout=30):
        return {
            "url": SOURCE_URL,
            "ok": True,
            "status_code": 200,
            "fetch_method": "html",
            "text": page_text["value"],
            "error": "",
        }

    old_monitor_root = monitor.ROOT
    old_audit_root = audit_runner.ROOT
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        monitor.configure_root(root)
        audit_runner.configure_root(root)
        try:
            with patch.object(monitor, "fetch_official_source", side_effect=fake_fetch):
                baseline = monitor.run_official_deal_monitor(provider)
                assert baseline["status"] == "baseline_pending"
                assert baseline["codex_audit_required"]
                assert not list((root / provider["data_dir"]).glob("coupons_*.json"))

                candidate_path = root / baseline["audit_candidate_path"]
                candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
                result_path = audit_runner.result_path_for(candidate)
                audit_runner.write_json(result_path, valid_result(candidate["candidate_id"]))
                baseline_summary = audit_runner.apply_all()
                assert not baseline_summary["eligible_candidates"]
                assert list((root / provider["data_dir"]).glob("coupons_*.json"))

                page_text["value"] = SOURCE_TEXT.replace("3,000", "4,000")
                changed = monitor.run_official_deal_monitor(provider)
                assert changed["status"] == "audit_pending"
                changed_candidate_path = root / changed["audit_candidate_path"]
                changed_candidate = json.loads(changed_candidate_path.read_text(encoding="utf-8"))
                changed_result_path = audit_runner.result_path_for(changed_candidate)
                audit_runner.write_json(
                    changed_result_path,
                    valid_result(changed_candidate["candidate_id"], "最大4,000円OFF"),
                )
                changed_summary = audit_runner.apply_all()
                assert len(changed_summary["eligible_candidates"]) == 1
                assert changed_summary["eligible_candidates"][0]["semantic_changed"]
        finally:
            monitor.configure_root(old_monitor_root)
            audit_runner.configure_root(old_audit_root)


def test_audit_validation_rejects_unquoted_evidence() -> None:
    candidate = {
        "schema_version": 1,
        "candidate_id": "sample-123",
        "provider_id": "sample",
        "official_domains": ["example.com"],
        "sources": [
            {
                "url": SOURCE_URL,
                "verification_result": "confirmed",
                "text": SOURCE_TEXT,
            }
        ],
    }
    result = valid_result(candidate["candidate_id"])
    result["deals"][0]["evidence_quote"] = "候補ファイルに存在しない文章"
    errors = validate_audit_result(candidate, result)
    assert any("evidence_quote" in error for error in errors)


def test_ignore_non_deal_change_marks_update_processed() -> None:
    candidate = {
        "schema_version": 1,
        "candidate_id": "sample-ignore-1",
        "provider_id": "sample",
        "provider_label": "Sample",
        "change_kind": "update",
        "content_hash": "ignored-hash",
        "data_dir": "official_coupon_data/sample",
        "official_domains": ["example.com"],
        "sources": [
            {
                "url": SOURCE_URL,
                "verification_result": "confirmed",
                "text": SOURCE_TEXT,
            }
        ],
        "previous_coupons": [{"id": "existing", "title": "既存クーポン"}],
    }
    result = {
        "schema_version": 1,
        "candidate_id": candidate["candidate_id"],
        "provider_id": "sample",
        "page_summary": "ナビゲーション変更のみ",
        "change_summary": "クーポン情報の変更なし",
        "recommendation": "ignore",
        "priority": 0,
        "uncertainty_reasons": [],
        "audit_notes": "",
        "deals": [],
    }
    old_root = audit_runner.ROOT
    with tempfile.TemporaryDirectory() as tmp:
        audit_runner.configure_root(Path(tmp))
        try:
            applied = audit_runner.apply_candidate(candidate, result)
            assert applied["status"] == "processed"
            assert applied["ignored_non_deal_change"]
            state = json.loads(
                (Path(tmp) / "official_source_data/sample/state.json").read_text(encoding="utf-8")
            )
            assert state["processed_hash"] == "ignored-hash"
        finally:
            audit_runner.configure_root(old_root)


def test_wp_daily_limit_queues_sixth_and_later_targets() -> None:
    pages = [
        {
            "ota": "sample",
            "slug": f"sample-{index}",
            "label": f"Sample {index}",
            "auto_review_enabled": True,
            "update_enabled": True,
        }
        for index in range(7)
    ]
    sites = {"sites": {"welltrip": {"pages": pages}}}
    run_summary = {
        "eligible_candidates": [
            {
                "candidate_id": "sample-update-1",
                "provider_id": "sample",
                "priority": 70,
                "change_summary": "新規クーポン",
            }
        ]
    }
    site_config = {
        "site_id": "welltrip",
        "wp_url": "https://example.com",
        "wp_user": "user",
        "wp_app_password": "password",
    }

    def fake_review(_site_config, page, dry_run=False):
        return {
            "site_id": "welltrip",
            "slug": page["slug"],
            "ota": page["ota"],
            "status": "dry_run" if dry_run else "review_ready",
        }

    old_root = wp_orchestrator.ROOT
    with tempfile.TemporaryDirectory() as tmp:
        wp_orchestrator.configure_root(Path(tmp))
        try:
            with patch.object(wp_orchestrator, "load_sites_config", return_value=sites), patch.object(
                wp_orchestrator, "load_site_config", return_value=site_config
            ), patch.object(wp_orchestrator, "review_page", side_effect=fake_review):
                current = datetime(2026, 7, 10, 9, 0, tzinfo=wp_orchestrator.JST)
                dry_run_summary = wp_orchestrator.run_reviews(
                    run_summary,
                    dry_run=True,
                    current=current,
                )
                assert dry_run_summary["overflow"]["pending_count"] == 2
                assert not wp_orchestrator.LEDGER_FILE.exists()

                summary = wp_orchestrator.run_reviews(run_summary, current=current)
                assert sum(result.get("status") == "review_ready" for result in summary["results"]) == 5
                assert summary["overflow"]["pending_count"] == 2
                assert summary["overflow"]["needs_user_input"]

                second = wp_orchestrator.run_reviews({}, current=current)
                assert not second["results"]
                assert second["overflow"]["pending_count"] == 2
                assert second["drafts_created_before_run"] == 5
        finally:
            wp_orchestrator.configure_root(old_root)


def test_human_edit_guard() -> None:
    site = {"site_id": "welltrip"}
    slug = "sample-coupon-update"
    content = "generated content"
    post = {"id": 10, "content": {"raw": content}}
    with tempfile.TemporaryDirectory() as tmp:
        old_state_file = updater.DRAFT_STATE_FILE
        updater.DRAFT_STATE_FILE = Path(tmp) / "wp_draft_state.json"
        try:
            updater.record_generated_draft(site, slug, 10, content)
            assert updater.human_edit_guard_reason(site, post, slug) == ""
            post["content"]["raw"] = "human edited content"
            assert "編集されています" in updater.human_edit_guard_reason(site, post, slug)
        finally:
            updater.DRAFT_STATE_FILE = old_state_file


def main() -> None:
    tests = [
        test_registry_frequency_counts,
        test_http_404_and_429_are_not_success,
        test_official_fetch_queues_candidate_then_codex_applies_update,
        test_audit_validation_rejects_unquoted_evidence,
        test_ignore_non_deal_change_marks_update_processed,
        test_wp_daily_limit_queues_sixth_and_later_targets,
        test_human_edit_guard,
    ]
    for test in tests:
        test()
        print(f"  PASSED {test.__name__}")
    print("automation pipeline tests passed")


if __name__ == "__main__":
    main()
