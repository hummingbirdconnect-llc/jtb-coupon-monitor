#!/usr/bin/env python3
"""速報タブ・キャンペーン欄・JTB残り枚数判定の回帰テスト。"""

from __future__ import annotations

import unittest
from datetime import date, datetime

from generate_dashboard import annotate_change_log, change_kind, period_is_over, stock_label
from jtb_coupon_monitor import JST, summarize_password_stock


class ChangeKindTest(unittest.TestCase):
    def test_normalizes_each_provider_wording(self) -> None:
        self.assertEqual(change_kind("🆕 新規"), "new")
        self.assertEqual(change_kind("new"), "new")
        self.assertEqual(change_kind("❌ 消失"), "gone")
        self.assertEqual(change_kind("gone"), "gone")
        self.assertEqual(change_kind("🔴 配布終了"), "ended")
        self.assertEqual(change_kind("ステータス変更 配布中→配布終了"), "ended")
        self.assertEqual(change_kind("🟢 配布再開"), "reopened")
        self.assertEqual(change_kind("ステータス変更 配布終了→配布中"), "reopened")


class AnnotateChangeLogTest(unittest.TestCase):
    def test_marks_reappearance_flaps_and_return(self) -> None:
        log = [
            {"date": "2026-10-01", "type": "🆕 新規", "id": "night"},
            {"date": "2026-10-02", "type": "❌ 消失", "id": "night"},
            {"date": "2026-10-02", "type": "🆕 新規", "id": "night"},
            {"date": "2026-10-03", "type": "🆕 新規", "id": "fresh"},
        ]
        rows = {(row["date"], row["type"], row["id"]): row for row in annotate_change_log(log)}

        first = rows[("2026-10-01", "🆕 新規", "night")]
        self.assertFalse(first["_reappeared"])
        gone = rows[("2026-10-02", "❌ 消失", "night")]
        self.assertTrue(gone["_came_back"])
        again = rows[("2026-10-02", "🆕 新規", "night")]
        self.assertTrue(again["_reappeared"])
        self.assertEqual(again["_flaps"], 3)
        self.assertFalse(rows[("2026-10-03", "🆕 新規", "fresh")]["_reappeared"])


class CampaignPeriodTest(unittest.TestCase):
    def test_only_drops_periods_whose_dates_are_all_past(self) -> None:
        today = date(2026, 10, 15)
        self.assertTrue(period_is_over("予約2026年2月17日～2026年9月30日、出発2026/4/1～2026/9/30", today))
        self.assertFalse(period_is_over("予約2026年8月17日～2026年11月16日", today))
        self.assertFalse(period_is_over("毎月20～29日（終了日は未確認）", today))


class PasswordStockTest(unittest.TestCase):
    NOW = datetime(2026, 10, 15, 9, 0, tzinfo=JST)

    def tier(self, stock: int, count: int, reserve_to: str = "202611010000") -> dict:
        return {
            "Password": "pw",
            "CouponCount": count,
            "StockCount": stock,
            "SoldOutFlag": 0 if stock else 1,
            "ReserveDateTimeFrom": "202609011000",
            "ReserveDateTimeTo": reserve_to,
            "DiscountAmount": 3000,
            "MinimumCharge": 20000,
        }

    def test_any_tier_with_stock_is_active(self) -> None:
        summary = summarize_password_stock([self.tier(0, 200), self.tier(12, 300)], now=self.NOW)
        self.assertEqual(summary["stock_status"], "配布中")
        self.assertEqual((summary["stock_remaining"], summary["stock_total"]), (12, 500))

    def test_all_sold_out_is_ended_with_reason(self) -> None:
        summary = summarize_password_stock([self.tier(0, 333)], now=self.NOW)
        self.assertEqual(summary["stock_status"], "配布終了")
        self.assertEqual(summary["stock_end_reason"], "品切れ")
        self.assertEqual(stock_label(summary), "0 / 333（品切れ）")

    def test_booking_window_closed_is_ended(self) -> None:
        summary = summarize_password_stock([self.tier(50, 100, reserve_to="202610010000")], now=self.NOW)
        self.assertEqual(summary["stock_status"], "配布終了")
        self.assertEqual(summary["stock_end_reason"], "予約受付の終了")

    def test_no_api_data_keeps_previous_judgement(self) -> None:
        self.assertIsNone(summarize_password_stock([], now=self.NOW))


if __name__ == "__main__":
    unittest.main()
