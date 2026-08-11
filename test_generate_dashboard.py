#!/usr/bin/env python3
"""ダッシュボードHTMLと機械向けJSONの同時生成テスト。"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from generate_dashboard import (
    dashboard_manual_sources,
    format_coupon_row,
    official_audit_display,
    scoped_count_labels,
    write_dashboard_outputs,
)


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
