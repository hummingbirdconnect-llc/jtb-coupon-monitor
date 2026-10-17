#!/usr/bin/env python3
"""ダッシュボードHTMLと機械向けJSONの同時生成テスト。"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

import generate_dashboard
from generate_dashboard import (
    JST,
    build_provider_payload,
    dashboard_manual_sources,
    format_coupon_row,
    freshness_chip,
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


class UnchangedPageFreshnessTest(unittest.TestCase):
    """ページが反映済みの内容から変わっていない日は、取得日で鮮度を計ること。"""

    def setUp(self) -> None:
        self.today = datetime.now(JST).date()

    def build_payload(
        self,
        *,
        status: str,
        data_day: date,
        fetched_day: date,
        pending_audit_counts: dict[str, int] | None = None,
    ) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(
                root / "official_coupon_data" / "sample_ota" / f"coupons_{data_day.isoformat()}.json",
                [{"id": "sample-1", "title": "サンプルクーポン", "source_type": "official_codex_audit"}],
            )
            # GitHub Actions の取得記録。確定データの日付で計った鮮度（stale）が入っている。
            write_json(
                root / "provider_check_data" / "sample_ota" / "latest.json",
                {
                    "check_type": "official_page_candidate",
                    "status": status,
                    "change_kind": status if status == "unchanged" else "update",
                    "official_fetched_at": f"{fetched_day.isoformat()}T04:37:46.213487+09:00",
                    "data_date": data_day.isoformat(),
                    "freshness_status": "stale",
                    "codex_audit_required": status != "unchanged",
                },
            )
            with mock.patch.multiple(
                generate_dashboard,
                ROOT=root,
                CHECK_STATUS_ROOT=root / "provider_check_data",
            ):
                return build_provider_payload(SAMPLE_PROVIDER, pending_audit_counts or {})

    def test_unchanged_page_without_pending_audit_is_fresh_from_fetch_date(self) -> None:
        data_day = self.today - timedelta(days=2)
        payload = self.build_payload(status="unchanged", data_day=data_day, fetched_day=self.today)

        label = f"新鮮（{self.today.month}/{self.today.day} 変化なしを確認）"
        self.assertEqual(payload["freshness_status"], "fresh")
        self.assertEqual(payload["freshness_label"], label)
        self.assertEqual(payload["summary"]["鮮度"], label)
        # データ日は確定データの日付のまま。取得日は別の欄に持つ。
        self.assertEqual(payload["data_date"], data_day.isoformat())
        self.assertEqual(
            payload["unchanged_confirmed_at"],
            f"{self.today.isoformat()}T04:37:46.213487+09:00",
        )

    def test_unchanged_page_with_pending_audit_keeps_data_date_freshness(self) -> None:
        payload = self.build_payload(
            status="unchanged",
            data_day=self.today - timedelta(days=2),
            fetched_day=self.today,
            pending_audit_counts={"sample_ota": 1},
        )

        self.assertEqual(payload["freshness_status"], "stale")
        self.assertEqual(payload["freshness_label"], "期限超過")
        self.assertEqual(payload["unchanged_confirmed_at"], "")

    def test_changed_page_keeps_data_date_freshness(self) -> None:
        payload = self.build_payload(
            status="audit_pending",
            data_day=self.today - timedelta(days=2),
            fetched_day=self.today,
        )

        self.assertEqual(payload["freshness_status"], "stale")
        self.assertEqual(payload["unchanged_confirmed_at"], "")

    def test_old_unchanged_fetch_is_stale(self) -> None:
        # 取得が止まれば、変化なしの確認も古くなり期限超過へ戻る。
        fetched_day = self.today - timedelta(days=2)
        payload = self.build_payload(
            status="unchanged",
            data_day=self.today - timedelta(days=4),
            fetched_day=fetched_day,
        )

        self.assertEqual(payload["freshness_status"], "stale")
        self.assertEqual(
            payload["freshness_label"],
            f"期限超過（{fetched_day.month}/{fetched_day.day} 変化なしを確認）",
        )

    def test_flash_chip_shows_content_date_and_unchanged_check(self) -> None:
        provider = {
            "id": "rakuten_travel",
            "label": "楽天トラベル",
            "data_date": "2026-10-15",
            "official_fetched_at": "2026-10-17T04:37:46.213487+09:00",
            "unchanged_confirmed_at": "2026-10-17T04:37:46.213487+09:00",
            "pending_audit_count": 0,
        }

        today_chip = freshness_chip(provider, date(2026, 10, 17))
        next_day_chip = freshness_chip(provider, date(2026, 10, 18))

        self.assertEqual(today_chip["state"], "fresh")
        self.assertEqual(today_chip["text"], "10/15 の内容・10/17 04:37 に変化なしを確認")
        # ほかの会社に新しい日のデータがあれば、確認が古いことを黄色で示す。
        self.assertEqual(next_day_chip["state"], "stale")
        self.assertEqual(next_day_chip["text"], "10/15 の内容・10/17 04:37 に変化なしを確認")


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


if __name__ == "__main__":
    unittest.main()
