#!/usr/bin/env python3
"""HIS地域拡張後の公開系コンシューマー互換テスト。"""

import unittest

from generate_tweets import select_his_primary_events
from generate_x_threads import is_primary_his_dashboard_row


class HisRegionalConsumersTest(unittest.TestCase):
    def test_tweet_events_are_limited_to_primary_region(self) -> None:
        events = [
            {"id": "legacy"},
            {"id": "common", "region_codes": ["kanto", "hokkaido"]},
            {"id": "hokkaido", "region_codes": ["hokkaido"]},
        ]
        snapshot = [
            {"id": "legacy"},
            {"id": "common", "region_codes": ["kanto", "hokkaido"]},
            {"id": "hokkaido", "region_codes": ["hokkaido"]},
        ]

        selected_events, selected_snapshot = select_his_primary_events(
            events, snapshot
        )

        self.assertEqual(
            [entry["id"] for entry in selected_events], ["legacy", "common"]
        )
        self.assertEqual(
            [coupon["id"] for coupon in selected_snapshot], ["legacy", "common"]
        )

    def test_x_threads_keep_legacy_and_kanto_rows_only(self) -> None:
        self.assertTrue(is_primary_his_dashboard_row("his", {}))
        self.assertTrue(
            is_primary_his_dashboard_row(
                "his", {"_region_codes": ["kanto", "hokkaido"]}
            )
        )
        self.assertFalse(
            is_primary_his_dashboard_row(
                "his", {"_region_codes": ["hokkaido"]}
            )
        )
        self.assertTrue(
            is_primary_his_dashboard_row(
                "jtb", {"_region_codes": ["hokkaido"]}
            )
        )


if __name__ == "__main__":
    unittest.main()
