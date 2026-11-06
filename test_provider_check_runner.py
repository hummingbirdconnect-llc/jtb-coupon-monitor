#!/usr/bin/env python3
"""会社別クーポンチェックの取得記録に書く鮮度のテスト。"""

from __future__ import annotations

import unittest
from datetime import datetime
from unittest import mock

import provider_check_runner as runner

PROVIDER = {
    "id": "sample_ota",
    "label": "サンプル",
    "data_dir": "official_coupon_data/sample_ota",
    "official_sources": [{"url": "https://example.com/coupon/"}],
    "check_frequency": "daily",
    "cadence_days": 1,
    "freshness_sla_hours": 30,
}


class OfficialPageFreshnessTest(unittest.TestCase):
    """公式ページ監視で、ページが反映済みの内容のままなら取得日で鮮度を計ること。"""

    def run_provider(self, status: str, official_fetched_at: str) -> dict:
        monitor_payload = {
            "provider_id": "sample_ota",
            "check_type": "official_page_candidate",
            "status": status,
            "official_fetched_at": official_fetched_at,
            "data_date": "2026-10-15",
        }
        now = datetime(2026, 10, 17, 4, 38, tzinfo=runner.JST)
        with mock.patch.object(
            runner, "run_official_deal_monitor", return_value=dict(monitor_payload)
        ), mock.patch.object(runner, "write_status"), mock.patch.object(
            runner, "now_jst", return_value=now
        ):
            return runner.run_provider(PROVIDER, timeout=10, max_urls=0)

    def test_unchanged_page_is_measured_from_fetch_date(self) -> None:
        payload = self.run_provider("unchanged", "2026-10-17T04:37:46+09:00")

        self.assertEqual(payload["freshness_status"], "fresh")
        self.assertEqual(payload["data_date"], "2026-10-15")

    def test_changed_page_is_measured_from_data_date(self) -> None:
        payload = self.run_provider("audit_pending", "2026-10-17T04:37:46+09:00")

        self.assertEqual(payload["freshness_status"], "stale")

    def test_failed_fetch_is_measured_from_data_date(self) -> None:
        payload = self.run_provider("error", "")

        self.assertEqual(payload["freshness_status"], "stale")


if __name__ == "__main__":
    unittest.main()
