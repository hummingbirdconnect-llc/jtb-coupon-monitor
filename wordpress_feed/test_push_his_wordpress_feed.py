#!/usr/bin/env python3
"""HIS WordPress同期の送信前検証と読戻し検証。"""

from __future__ import annotations

import copy
import unittest
from datetime import datetime, timedelta, timezone

from push_his_wordpress_feed import (
    FeedValidationError,
    http_error_summary,
    normalize_application_password,
    validate_feed,
    verify_readback,
)


class FakeResponse:
    def __init__(self, status_code: int, headers: dict, body: object) -> None:
        self.status_code = status_code
        self.headers = headers
        self.body = body

    def json(self) -> object:
        if isinstance(self.body, ValueError):
            raise self.body
        return self.body


def sample_payload(now: datetime) -> dict:
    verified_at = now.isoformat()
    item = {
        "id": "his:0123456789ab",
        "monitorSourceId": "his-official-monitor",
        "providerSlug": "his",
        "providerName": "HIS",
        "status": "active",
        "couponName": "国内ホテル宿泊クーポン",
        "discountLabel": "1,000円引き",
        "bookingPeriod": "2026年7月1日～2026年7月31日",
        "travelPeriod": "2026年7月2日～2026年8月31日",
        "displayTitle": "国内ホテル宿泊クーポン",
        "codes": [
            {
                "code": "37Y6V9DA7GTB84QB",
                "condition": "宿泊代金2万円以上",
                "discount": "1千円引き",
            }
        ],
        "note": "オンライン予約限定です。",
        "destinationUrl": "https://example.com/coupon",
        "buttonText": "HISクーポンを確認する",
        "sourceUpdatedAt": verified_at,
        "selectable": True,
        "warnings": [],
    }
    items = []
    for index in range(10):
        cloned = copy.deepcopy(item)
        cloned["id"] = f"his:{index:012x}"
        items.append(cloned)
    return {
        "schemaVersion": 1,
        "source": {
            "monitorSourceId": "his-official-monitor",
            "providerSlug": "his",
            "providerName": "HIS",
            "monitorFetchedAt": verified_at,
            "verifiedAt": verified_at,
            "freshnessSlaHours": 30,
            "officialComparison": {
                "liveCount": 10,
                "sourceCount": 10,
                "matchedCount": 10,
                "codeMismatchCount": 0,
                "periodMismatchCount": 0,
            },
            "safetyChecks": {
                "currentRunFresh": True,
                "allItemsOfficialVisible": True,
                "uniqueIds": True,
            },
        },
        "counts": {"total": 10, "selectable": 10, "needsReview": 0},
        "items": items,
    }


class FeedValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.payload = sample_payload(self.now)

    def test_valid_feed_and_readback(self) -> None:
        expected = validate_feed(self.payload, now=self.now)
        readback = {
            "sources": [
                {
                    "id": "his-official-monitor",
                    "status": "fresh",
                    "verifiedAt": expected["verifiedAt"],
                    "itemCount": expected["itemCount"],
                }
            ],
            "items": [
                {
                    **copy.deepcopy(item),
                    "lifecycleState": "present",
                }
                for item in self.payload["items"]
            ],
        }
        verify_readback(readback, expected)

    def test_application_password_spaces_are_removed(self) -> None:
        self.assertEqual(
            normalize_application_password("abcd efgh  ijkl\tmnop\n"),
            "abcdefghijklmnop",
        )

    def test_json_permission_error_is_safe_and_actionable(self) -> None:
        message = http_error_summary(
            FakeResponse(
                403,
                {"Content-Type": "application/json; charset=UTF-8"},
                {
                    "code": "rest_forbidden",
                    "message": "この操作を実行する権限がありません。",
                },
            )
        )
        self.assertIn("rest_forbidden", message)
        self.assertIn("HTTP 403", message)

    def test_non_json_rejection_reports_only_route_signal(self) -> None:
        message = http_error_summary(
            FakeResponse(
                403,
                {"Content-Type": "text/html", "CF-RAY": "test"},
                "<html>ignored</html>",
            )
        )
        self.assertIn("Cloudflare経由", message)
        self.assertNotIn("ignored", message)

    def test_duplicate_id_is_rejected(self) -> None:
        self.payload["items"][1]["id"] = self.payload["items"][0]["id"]
        with self.assertRaisesRegex(FeedValidationError, "重複"):
            validate_feed(self.payload, now=self.now)

    def test_count_mismatch_is_rejected(self) -> None:
        self.payload["source"]["officialComparison"]["matchedCount"] = 9
        with self.assertRaisesRegex(FeedValidationError, "照合件数"):
            validate_feed(self.payload, now=self.now)

    def test_stale_feed_is_rejected(self) -> None:
        old = (self.now - timedelta(hours=4)).isoformat()
        self.payload["source"]["verifiedAt"] = old
        self.payload["source"]["monitorFetchedAt"] = old
        for item in self.payload["items"]:
            item["sourceUpdatedAt"] = old
        with self.assertRaisesRegex(FeedValidationError, "3時間"):
            validate_feed(self.payload, now=self.now)

    def test_missing_readback_item_is_rejected(self) -> None:
        expected = validate_feed(self.payload, now=self.now)
        readback = {
            "sources": [
                {
                    "id": "his-official-monitor",
                    "status": "fresh",
                    "verifiedAt": expected["verifiedAt"],
                    "itemCount": expected["itemCount"],
                }
            ],
            "items": [],
        }
        with self.assertRaisesRegex(RuntimeError, "識別子"):
            verify_readback(readback, expected)


if __name__ == "__main__":
    unittest.main()
