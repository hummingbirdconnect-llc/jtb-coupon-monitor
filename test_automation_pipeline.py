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


ALWAYS_ON_IDS = {"his", "jtb", "knt", "jalpack", "jalan", "rakuten_travel"}
ON_DEMAND_DEEP_IDS = {
    "relux",
    "asoview",
    "activityjapan",
    "club_tourism",
    "jr_tokai_tours",
    "skypack_tours",
    "tobu_top_tours",
    "yomiuri_travel",
    "toku",
    "booking",
}


def test_registry_frequency_counts() -> None:
    providers = runner.load_registry()
    assert len(providers) == 44
    daily = {provider["id"] for provider in providers if provider["cadence_days"] == 1}
    assert daily == ALWAYS_ON_IDS
    assert sum(provider["cadence_days"] == 5 for provider in providers) == 0
    on_demand = [provider for provider in providers if runner.is_on_demand(provider)]
    assert len(on_demand) == 44 - len(ALWAYS_ON_IDS)
    for provider in on_demand:
        for offset in range(7):
            assert not runner.provider_due(provider, date(2026, 9, 18 + offset)), provider["id"]


def test_scope_due_selects_only_always_on_providers() -> None:
    providers = runner.load_registry()
    due = runner.select_providers(providers, "due", "", date(2026, 9, 18))
    assert {provider["id"] for provider in due} == ALWAYS_ON_IDS
    on_demand = runner.select_providers(providers, "on_demand", "", date(2026, 9, 18))
    assert ON_DEMAND_DEEP_IDS <= {provider["id"] for provider in on_demand}


def test_on_demand_providers_have_official_sources_and_deep_flag_gate() -> None:
    providers = {provider["id"]: provider for provider in runner.load_registry()}
    for provider_id in ON_DEMAND_DEEP_IDS:
        provider = providers[provider_id]
        assert provider["coverage_status"] == "official_on_demand"
        assert provider["official_sources"], provider_id
        # 公式ドメインが無いとCodex監査の結果が全件はじかれる
        assert provider.get("official_domains"), provider_id
        assert provider["data_dir"] == f"official_coupon_data/{provider_id}"
        assert provider["legacy_data_dir"] == f"manual_coupon_data/{provider_id}"
        # --scope all だけでは深掘りしない。--deep か --provider-id 直指定が必要
        assert not runner.deep_dive_allowed(provider, deep=False, explicit=False)
        assert runner.deep_dive_allowed(provider, deep=True, explicit=False)
        assert runner.deep_dive_allowed(provider, deep=False, explicit=True)
    # 常時監視で official_sources がある会社は従来どおり常に深掘り
    assert runner.deep_dive_allowed(providers["jalan"], deep=False, explicit=False)


def test_http_404_and_429_are_not_success() -> None:
    response_404 = Mock(status_code=404)
    response_429 = Mock(status_code=429)
    with patch.object(runner.requests, "head", side_effect=[response_404, response_429]):
        not_found = runner.check_url("https://example.com/missing", 1)
        rate_limited = runner.check_url("https://example.com/limited", 1)
    assert not not_found["ok"] and not_found["classification"] == "not_found"
    assert not rate_limited["ok"] and rate_limited["classification"] == "rate_limited"


def test_response_decoder_handles_shift_jis_and_utf8_despite_latin1_header() -> None:
    shift_jis_html = '<html><head><meta charset="Shift_JIS"></head><body>じゃらんクーポン</body></html>'
    shift_jis_response = Mock(
        content=shift_jis_html.encode("cp932"),
        encoding="ISO-8859-1",
        apparent_encoding="Windows-1252",
    )
    assert "じゃらんクーポン" in monitor._decode_response_html(shift_jis_response)

    utf8_html = '<html><head><meta charset="UTF-8"></head><body>楽天トラベルクーポン</body></html>'
    utf8_response = Mock(
        content=utf8_html.encode("utf-8"),
        encoding="ISO-8859-1",
        apparent_encoding="Windows-1252",
    )
    assert "楽天トラベルクーポン" in monitor._decode_response_html(utf8_response)


def test_jalan_discovery_keeps_only_allowlisted_representative_pages() -> None:
    source = {
        "url": "https://www.jalan.net/jalancponsum/",
        "discover_links": True,
        "discovery_path_patterns": [
            r"^/theme/jalancouponfes/?$",
            r"^/discountCoupon/CAM[0-9]+/?$",
        ],
        "max_discovered_links": 8,
    }
    html = """
    <a href="/theme/jalancouponfes/?ccnt=tracking">クーポンフェス</a>
    <a href="https://www.jalan.net/discountCoupon/CAM1322324/#detail">直接クーポン</a>
    <a href="/discountCoupon/CAM1322324/?ccnt=duplicate">重複</a>
    <a href="/jalancponsum/zenkoku/">個別宿1,000件</a>
    <a href="https://example.com/discountCoupon/CAM999/">外部</a>
    """

    assert monitor._discover_official_urls(html, source) == [
        "https://www.jalan.net/theme/jalancouponfes/",
        "https://www.jalan.net/discountCoupon/CAM1322324/",
    ]


def test_jalan_registry_uses_representative_scope_and_manual_queue() -> None:
    jalan = next(provider for provider in runner.load_registry() if provider["id"] == "jalan")
    official_urls = {source["url"] for source in jalan["official_sources"]}

    assert jalan["coverage_scope"] == "curated_representative"
    assert jalan["count_scope_label"] == "代表クーポン"
    assert "https://www.jalan.net/jalancponsum/zenkoku/" not in official_urls
    assert "https://www.jalan.net/jalancponsum/" in official_urls
    assert len(jalan["manual_sources"]) == 7
    assert any(
        source["access_mode"] == "login_required"
        for source in jalan["manual_sources"]
    )


def test_latest_per_provider_applies_only_newest_and_never_backfills_old() -> None:
    old_root = audit_runner.ROOT
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        audit_runner.configure_root(root)
        try:
            for suffix, fetched_at, content_hash in [
                ("old", "2026-08-10T08:00:00+09:00", "old-hash"),
                ("new", "2026-08-11T08:00:00+09:00", "new-hash"),
            ]:
                candidate = {
                    "schema_version": 1,
                    "candidate_id": f"sample-{suffix}",
                    "provider_id": "sample",
                    "provider_label": "Sample",
                    "change_kind": "baseline",
                    "content_hash": content_hash,
                    "fetched_at": fetched_at,
                    "data_dir": "official_coupon_data/sample",
                    "official_domains": ["example.com"],
                    "sources": [
                        {
                            "url": SOURCE_URL,
                            "verification_result": "confirmed",
                            "text": SOURCE_TEXT,
                        }
                    ],
                    "previous_coupons": [],
                }
                audit_runner.write_json(
                    root / "codex_audit_queue" / "sample" / f"{candidate['candidate_id']}.json",
                    candidate,
                )
                audit_runner.write_json(
                    audit_runner.result_path_for(candidate),
                    valid_result(candidate["candidate_id"]),
                )

            pending = audit_runner.pending_candidates(latest_per_provider=True)
            assert [item["candidate_id"] for item in pending] == ["sample-new"]

            summary = audit_runner.apply_all(latest_per_provider=True)
            assert summary["selection_mode"] == "latest_per_provider"
            assert [audit["candidate_id"] for audit in summary["audits"]] == ["sample-new"]
            assert audit_runner.pending_candidates(latest_per_provider=True) == []
            assert [
                item["candidate_id"] for item in audit_runner.pending_candidates()
            ] == ["sample-old"]
        finally:
            audit_runner.configure_root(old_root)


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


def test_discovery_hub_is_not_audited_but_its_representative_page_is() -> None:
    hub_url = "https://www.jalan.net/jalancponsum/"
    campaign_url = "https://www.jalan.net/theme/jalancouponfes/"
    provider = {
        "id": "jalan",
        "label": "じゃらん",
        "data_dir": "official_coupon_data/jalan",
        "official_domains": ["jalan.net"],
        "coverage_scope": "curated_representative",
        "count_scope_label": "代表クーポン",
        "official_sources": [
            {
                "url": hub_url,
                "fetch_method": "auto",
                "include_in_audit": False,
                "discover_links": True,
            }
        ],
    }

    def fake_fetch(source, timeout=30):
        is_hub = source["url"] == hub_url
        return {
            "url": source["url"],
            "ok": True,
            "status_code": 200,
            "fetch_method": "html",
            "text": "入口ページ" if is_hub else SOURCE_TEXT,
            "error": "",
            "purpose": source.get("purpose", ""),
            "source_role": source.get("source_role", "official_source"),
            "include_in_audit": source.get("include_in_audit", True),
            "discovered_from": source.get("discovered_from", ""),
            "discovered_urls": [campaign_url] if is_hub else [],
        }

    old_root = monitor.ROOT
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        monitor.configure_root(root)
        try:
            with patch.object(monitor, "fetch_official_source", side_effect=fake_fetch):
                check = monitor.run_official_deal_monitor(provider)
            candidate = json.loads(
                (root / check["audit_candidate_path"]).read_text(encoding="utf-8")
            )

            assert check["discovered_source_count"] == 1
            assert check["audit_source_count"] == 1
            assert [source["url"] for source in candidate["sources"]] == [campaign_url]
            assert candidate["coverage_scope"] == "curated_representative"
        finally:
            monitor.configure_root(old_root)


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
        test_scope_due_selects_only_always_on_providers,
        test_on_demand_providers_have_official_sources_and_deep_flag_gate,
        test_http_404_and_429_are_not_success,
        test_response_decoder_handles_shift_jis_and_utf8_despite_latin1_header,
        test_latest_per_provider_applies_only_newest_and_never_backfills_old,
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
