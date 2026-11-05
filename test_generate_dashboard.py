#!/usr/bin/env python3
"""ダッシュボードHTMLと機械向けJSONの同時生成テスト。"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import generate_dashboard
from generate_dashboard import (
    JST,
    attach_recent_logs,
    build_provider_payload,
    dashboard_manual_sources,
    format_coupon_row,
    load_pending_audit_counts,
    official_audit_display,
    scoped_count_labels,
    write_dashboard_outputs,
)


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


SAMPLE_PROVIDER = {
    "id": "sample_ota",
    "label": "サンプル",
    "data_dir": "official_coupon_data/sample_ota",
    "coverage_status": "official_codex",
    "check_frequency": "daily",
    "freshness_sla_hours": 30,
}


class RecentChangeTableTest(unittest.TestCase):
    """主な変化の表と件数に、在庫の出入りだけの行を混ぜないこと。"""

    DAYS = [{"key": "d0", "label": "今日", "date": "2026-10-20"}]

    def test_minor_rows_stay_out_of_the_cross_provider_table(self) -> None:
        providers = [
            {
                "id": "jtb",
                "label": "JTB",
                "logs": [
                    {"日付": "2026-10-20", "ID": "toggle", "種別": "🟢 配布再開", "_kind": "reopened", "_minor": True},
                    {"日付": "2026-10-20", "ID": "fresh", "種別": "🆕 新規", "_kind": "new", "_minor": False},
                ],
            }
        ]
        all_rows = attach_recent_logs(providers, self.DAYS)
        self.assertEqual([row["ID"] for row in all_rows], ["fresh"])
        # 速報の「在庫の出入り」で出せるよう、会社ごとの行には残す。
        self.assertEqual([row["ID"] for row in providers[0]["recent_logs"]], ["toggle", "fresh"])


class AuditReflectionTest(unittest.TestCase):
    """公式監査の反映が、次の取得を待たずにダッシュボードへ出ること。"""

    def test_pending_audit_counts_only_latest_unapplied_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "codex_audit_queue"
            results = root / "codex_audit_results"
            ledger = root / "codex_audit_data" / "applied-candidates.json"
            # じゃらん: 古い未適用候補が残っていても、最新が適用済みなら0件。
            # ファイル名の順と取得時刻の順をわざと逆にしてある。
            for candidate_id, fetched_at in [
                ("jalan-zz-old", "2026-08-01T05:00:00+09:00"),
                ("jalan-yy-old", "2026-09-01T05:00:00+09:00"),
                ("jalan-aa-latest", "2026-10-15T05:00:00+09:00"),
            ]:
                write_json(
                    queue / "jalan" / f"{candidate_id}.json",
                    {"candidate_id": candidate_id, "provider_id": "jalan", "fetched_at": fetched_at},
                )
            # 楽天トラベル: 最新が未適用なら、古い候補の数に関係なく1件。
            for candidate_id, fetched_at in [
                ("rakuten_travel-old", "2026-08-11T05:00:00+09:00"),
                ("rakuten_travel-mid", "2026-09-11T05:00:00+09:00"),
                ("rakuten_travel-new", "2026-10-15T05:00:00+09:00"),
            ]:
                write_json(
                    queue / "rakuten_travel" / f"{candidate_id}.json",
                    {
                        "candidate_id": candidate_id,
                        "provider_id": "rakuten_travel",
                        "fetched_at": fetched_at,
                    },
                )
            # 読売旅行: 最新に監査結果があれば、適用前でも監査待ちではない。
            write_json(
                queue / "yomiuri_travel" / "yomiuri_travel-new.json",
                {
                    "candidate_id": "yomiuri_travel-new",
                    "provider_id": "yomiuri_travel",
                    "fetched_at": "2026-10-15T05:00:00+09:00",
                },
            )
            write_json(results / "yomiuri_travel" / "yomiuri_travel-new.json", {})
            write_json(
                ledger,
                {"candidates": {"jalan-aa-latest": {}, "rakuten_travel-old": {}}},
            )

            with mock.patch.multiple(
                generate_dashboard,
                AUDIT_QUEUE_ROOT=queue,
                AUDIT_RESULT_ROOT=results,
                AUDIT_LEDGER=ledger,
            ):
                counts = load_pending_audit_counts()

        self.assertEqual(counts, {"rakuten_travel": 1})

    def test_applied_audit_file_newer_than_check_status_sets_date_and_freshness(self) -> None:
        today = datetime.now(JST).date().isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            coupon = {"id": "sample-1", "title": "サンプルクーポン", "source_type": "official_codex_audit"}
            data_dir = root / "official_coupon_data" / "sample_ota"
            write_json(data_dir / "coupons_2026-09-29.json", [coupon])
            write_json(data_dir / f"coupons_{today}.json", [coupon])
            # GitHub Actions が前回の取得時に書いた記録（監査の反映より古い）。
            write_json(
                root / "provider_check_data" / "sample_ota" / "latest.json",
                {
                    "check_type": "official_page_candidate",
                    "data_date": "2026-09-29",
                    "freshness_status": "stale",
                },
            )
            with mock.patch.multiple(
                generate_dashboard,
                ROOT=root,
                CHECK_STATUS_ROOT=root / "provider_check_data",
            ):
                payload = build_provider_payload(SAMPLE_PROVIDER)

        self.assertEqual(payload["latest_file"], f"coupons_{today}.json")
        self.assertEqual(payload["data_date"], today)
        self.assertEqual(payload["freshness_status"], "fresh")
        self.assertEqual(payload["summary"]["データ日"], today)
        self.assertEqual(payload["summary"]["鮮度"], "新鮮")

    def test_check_status_newer_than_latest_file_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(
                root / "official_coupon_data" / "sample_ota" / "coupons_2026-08-11.json",
                [{"id": "sample-1", "title": "サンプルクーポン"}],
            )
            write_json(
                root / "provider_check_data" / "sample_ota" / "latest.json",
                {
                    "check_type": "official_page_candidate",
                    "data_date": "2026-09-29",
                    "freshness_status": "stale",
                },
            )
            with mock.patch.multiple(
                generate_dashboard,
                ROOT=root,
                CHECK_STATUS_ROOT=root / "provider_check_data",
            ):
                payload = build_provider_payload(SAMPLE_PROVIDER)

        self.assertEqual(payload["data_date"], "2026-09-29")
        self.assertEqual(payload["freshness_status"], "stale")

    def test_on_demand_held_audit_is_not_shown_as_not_run(self) -> None:
        # 最新候補が監査済み（保留）なら、古い候補が残っていても「未実施」としない。
        # 公式データがまだ無く旧データを出している間は、取得記録の鮮度を変えない。
        provider = {
            **SAMPLE_PROVIDER,
            "coverage_status": "official_on_demand",
            "check_frequency": "on_demand",
            "freshness_sla_hours": 720,
            "legacy_data_dir": "manual_coupon_data/sample_ota",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(
                root / "manual_coupon_data" / "sample_ota" / "coupons_2026-05-20.json",
                [{"id": "sample-1", "title": "サンプルクーポン"}],
            )
            write_json(
                root / "official_source_data" / "sample_ota" / "state.json",
                {"last_audit_status": "held"},
            )
            write_json(
                root / "provider_check_data" / "sample_ota" / "latest.json",
                {
                    "check_type": "official_page_candidate",
                    "data_date": "",
                    "freshness_status": "unknown",
                },
            )
            with mock.patch.multiple(
                generate_dashboard,
                ROOT=root,
                CHECK_STATUS_ROOT=root / "provider_check_data",
            ):
                payload = build_provider_payload(provider, {})

        self.assertTrue(payload["legacy_used"])
        self.assertEqual(payload["ai_label"], "監査保留")
        self.assertEqual(payload["data_date"], "2026-05-20")
        self.assertEqual(payload["freshness_status"], "unknown")


class DashboardOutputTest(unittest.TestCase):
    def test_representative_scope_is_explicit_in_count_labels(self) -> None:
        provider = {
            "count_scope_label": "代表クーポン",
            "count_scope_short_label": "代表",
        }
        self.assertEqual(
            scoped_count_labels(provider, "3"),
            ("代表クーポン 3", "代表3", "代表クーポン"),
        )
        self.assertEqual(scoped_count_labels({}, "3"), ("3", "3", "全"))

    def test_manual_source_queue_explains_login_dependency(self) -> None:
        sources = dashboard_manual_sources(
            {
                "manual_sources": [
                    {
                        "url": "https://www.jalan.net/activity/theme/coupon/",
                        "purpose": "遊び・体験クーポン",
                        "access_mode": "login_required",
                        "reason": "リクルートIDが必要",
                    }
                ]
            }
        )
        self.assertEqual(sources[0]["access_label"], "ログイン確認待ち")
        self.assertEqual(sources[0]["reason"], "リクルートIDが必要")

    def test_official_audit_pending_and_held_are_not_reported_as_zero(self) -> None:
        pending = official_audit_display(
            "official_codex",
            [],
            [],
            {"codex_audit_required": True},
            {"last_audit_status": "pending"},
        )
        self.assertEqual(pending, ("pending", "未確定", "Codex監査の確定前", "監査待ち"))

        held = official_audit_display(
            "official_codex",
            [],
            [],
            {"codex_audit_required": True},
            {
                "last_audit_status": "held",
                "queued_candidate_id": "sample-new",
                "last_audit_candidate_id": "sample-new",
            },
        )
        self.assertEqual(held, ("held", "保留", "公式根拠または条件が不足", "監査保留"))

        confirmed_empty = official_audit_display(
            "official_codex",
            [],
            [],
            {"codex_audit_required": True},
            {"last_audit_status": "processed"},
        )
        self.assertEqual(
            confirmed_empty,
            ("confirmed", "0", "監査済み・掲載対象なし", "監査済み"),
        )

    def test_his_rows_expose_region_codes_for_dashboard_filter(self) -> None:
        legacy = format_coupon_row(
            {"id": "legacy", "title": "旧首都圏版"},
            {"id": "his"},
            "coupons",
        )
        hokkaido = format_coupon_row(
            {
                "id": "hokkaido-only",
                "title": "北海道限定",
                "region_codes": ["hokkaido"],
            },
            {"id": "his"},
            "coupons",
        )

        self.assertEqual(legacy["_region_codes"], ["kanto"])
        self.assertEqual(hokkaido["_region_codes"], ["hokkaido"])

    def test_html_and_json_are_generated_from_the_same_data(self) -> None:
        data = {
            "schema_version": 1,
            "generated_at": "2026-08-01 08:07 JST",
            "providers": [
                {
                    "id": "jtb",
                    "label": "JTB",
                    "check_status": {"status": "success"},
                    "freshness_sla_hours": 30,
                    "freshness_status": "fresh",
                    "rows": [
                        {"ID": "2607young", "タイトル": "国内ツアークーポン"}
                    ],
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmp:
            html_file, json_file = write_dashboard_outputs(
                data, Path(tmp) / "dashboard"
            )

            self.assertEqual(html_file.name, "index.html")
            self.assertEqual(json_file.name, "latest.json")
            self.assertEqual(
                json.loads(json_file.read_text(encoding="utf-8")), data
            )

            html = html_file.read_text(encoding="utf-8")
            match = re.search(r"const DATA = (\{.*?\});\n", html, flags=re.DOTALL)
            self.assertIsNotNone(match)
            assert match is not None
            self.assertEqual(json.loads(match.group(1)), data)

    def test_missing_freshness_sla_stops_generation(self) -> None:
        data = {
            "schema_version": 1,
            "generated_at": "2026-08-01 08:07 JST",
            "providers": [
                {"id": "jtb", "check_status": {"status": "success"}, "rows": []}
            ],
        }

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "freshness SLA"):
                write_dashboard_outputs(data, Path(tmp) / "dashboard")


class FreshnessDateTest(unittest.TestCase):
    """最終確認日と内容の最終更新日を分けて出し、期限超過は最終確認日で測ること。"""

    def test_unchanged_content_with_todays_check_is_not_stale(self) -> None:
        checked_at = datetime.now(JST).replace(microsecond=0).isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(
                root / "official_coupon_data" / "sample_ota" / "coupons_2026-09-29.json",
                [{"id": "sample-1", "title": "サンプルクーポン"}],
            )
            write_json(
                root / "official_source_data" / "sample_ota" / "state.json",
                {
                    "last_checked_at": checked_at,
                    "last_changed_at": "2026-09-29T17:55:53+09:00",
                },
            )
            write_json(
                root / "provider_check_data" / "sample_ota" / "latest.json",
                {
                    "check_type": "official_page_candidate",
                    "status": "unchanged",
                    "data_date": "2026-09-29",
                    "freshness_status": "stale",
                    "last_checked_at": checked_at,
                    "content_last_changed_at": "2026-09-29T17:55:53+09:00",
                },
            )
            with mock.patch.multiple(
                generate_dashboard,
                ROOT=root,
                CHECK_STATUS_ROOT=root / "provider_check_data",
            ):
                payload = build_provider_payload(SAMPLE_PROVIDER)

        # 取得できている日の記録があるので、データ日が古くても期限超過にしない。
        self.assertEqual(payload["freshness_status"], "fresh")
        self.assertEqual(payload["data_date"], "2026-09-29")
        self.assertEqual(payload["content_last_changed_label"], "9/29")
        self.assertEqual(payload["summary"]["内容の最終更新日"], "9/29")
        self.assertEqual(payload["last_checked_label"], payload["summary"]["最終確認日"])
        self.assertIn("内容変化なし", payload["freshness_text"])

    def test_long_unchanged_content_is_flagged_separately(self) -> None:
        checked_at = datetime.now(JST).replace(microsecond=0).isoformat()
        changed_at = (datetime.now(JST) - timedelta(days=40)).replace(microsecond=0).isoformat()
        # 毎日監視の会社（指定時だけ深掘りする会社は変化が無いのが普通なので対象外）。
        provider = {**SAMPLE_PROVIDER, "cadence_days": 1}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(
                root / "official_coupon_data" / "sample_ota" / "coupons_2026-09-29.json",
                [{"id": "sample-1", "title": "サンプルクーポン"}],
            )
            write_json(
                root / "provider_check_data" / "sample_ota" / "latest.json",
                {
                    "check_type": "official_page_candidate",
                    "status": "unchanged",
                    "data_date": "2026-09-29",
                    "last_checked_at": checked_at,
                    "content_last_changed_at": changed_at,
                },
            )
            with mock.patch.multiple(
                generate_dashboard,
                ROOT=root,
                CHECK_STATUS_ROOT=root / "provider_check_data",
            ):
                payload = build_provider_payload(provider)

        self.assertEqual(payload["freshness_status"], "fresh")
        self.assertEqual(payload["content_status"], "long_unchanged")
        self.assertEqual(payload["content_unchanged_days"], 40)
        self.assertEqual(payload["summary"]["内容の変化"], "要確認：長期間変化なし（40日）")

        chip = generate_dashboard.freshness_chip(payload, None)
        self.assertEqual(chip["state"], "attention")
        self.assertIn("要確認：長期間変化なし（40日）", chip["text"])

    def test_old_last_checked_at_is_still_stale(self) -> None:
        checked_at = (datetime.now(JST) - timedelta(hours=50)).replace(microsecond=0).isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(
                root / "official_coupon_data" / "sample_ota" / "coupons_2026-09-29.json",
                [{"id": "sample-1", "title": "サンプルクーポン"}],
            )
            write_json(
                root / "provider_check_data" / "sample_ota" / "latest.json",
                {
                    "check_type": "official_page_candidate",
                    "status": "unchanged",
                    "data_date": "2026-09-29",
                    "last_checked_at": checked_at,
                    "content_last_changed_at": "2026-09-29T17:55:53+09:00",
                },
            )
            with mock.patch.multiple(
                generate_dashboard,
                ROOT=root,
                CHECK_STATUS_ROOT=root / "provider_check_data",
            ):
                payload = build_provider_payload(SAMPLE_PROVIDER)

        self.assertEqual(payload["freshness_status"], "stale")
        chip = generate_dashboard.freshness_chip(payload, None)
        self.assertEqual(chip["state"], "stale")
        self.assertIn("取得が止まっています", chip["text"])


if __name__ == "__main__":
    unittest.main()
