#!/usr/bin/env python3
"""ダッシュボードHTMLと機械向けJSONの同時生成テスト。"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from generate_dashboard import format_coupon_row, write_dashboard_outputs


class DashboardOutputTest(unittest.TestCase):
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
