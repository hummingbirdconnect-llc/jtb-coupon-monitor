#!/usr/bin/env python3
"""検証済みのHIS監視フィードをWordPressへ保存し、読戻しで確認する。"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests


REST_NAMESPACE = "travel-coupon-card/v1"
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
MAX_FUTURE_SKEW = timedelta(minutes=5)
DEFAULT_MAX_SYNC_AGE_HOURS = 3
DEFAULT_FEED_PATH = Path(__file__).resolve().with_name("his-monitor-feed.json")


class FeedValidationError(ValueError):
    """安全条件を満たさない監視フィード。"""


def normalize_application_password(value: str) -> str:
    """WordPressの表示用区切りを認証に含めない。"""

    return "".join(value.split())


def parse_iso_datetime(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise FeedValidationError(f"{label}がありません。")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FeedValidationError(f"{label}の日時形式が不正です。") from exc
    if parsed.tzinfo is None:
        raise FeedValidationError(f"{label}にタイムゾーンがありません。")
    return parsed.astimezone(timezone.utc)


def load_feed(feed_path: Path) -> dict[str, Any]:
    if not feed_path.is_file():
        raise FeedValidationError("WordPress用のHIS監視フィードがありません。")
    if feed_path.stat().st_size > MAX_PAYLOAD_BYTES:
        raise FeedValidationError("WordPress用のHIS監視フィードが2MBを超えています。")
    try:
        payload = json.loads(feed_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FeedValidationError("WordPress用のHIS監視フィードを読めません。") from exc
    if not isinstance(payload, dict):
        raise FeedValidationError("WordPress用のHIS監視フィードがオブジェクトではありません。")
    return payload


def validate_feed(
    payload: dict[str, Any],
    *,
    now: datetime | None = None,
    max_sync_age_hours: int = DEFAULT_MAX_SYNC_AGE_HOURS,
    allow_stale: bool = False,
) -> dict[str, Any]:
    """送信前に件数、鮮度、識別子、コードの完全性を確認する。"""

    if payload.get("schemaVersion") != 1:
        raise FeedValidationError("監視フィードの形式番号が不正です。")

    source = payload.get("source")
    items = payload.get("items")
    counts = payload.get("counts")
    if not isinstance(source, dict) or not isinstance(items, list):
        raise FeedValidationError("監視フィードの取得元または明細がありません。")
    if not isinstance(counts, dict):
        raise FeedValidationError("監視フィードの件数情報がありません。")
    if source.get("monitorSourceId") != "his-official-monitor":
        raise FeedValidationError("HIS公式監視以外のデータは同期できません。")
    if source.get("providerSlug") != "his":
        raise FeedValidationError("監視会社がHISではありません。")

    verified_at = parse_iso_datetime(source.get("verifiedAt"), "公式確認時刻")
    monitor_fetched_at = parse_iso_datetime(
        source.get("monitorFetchedAt"), "監視取得時刻"
    )
    if verified_at != monitor_fetched_at:
        raise FeedValidationError("公式確認時刻と監視取得時刻が一致しません。")

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if verified_at > current + MAX_FUTURE_SKEW:
        raise FeedValidationError("公式確認時刻が未来になっています。")
    if max_sync_age_hours < 1:
        raise FeedValidationError("送信可能な最大経過時間が不正です。")
    if not allow_stale and current - verified_at > timedelta(
        hours=max_sync_age_hours
    ):
        raise FeedValidationError(
            f"公式確認から{max_sync_age_hours}時間を超えているため同期しません。"
        )

    freshness_sla = source.get("freshnessSlaHours")
    if (
        not isinstance(freshness_sla, int)
        or isinstance(freshness_sla, bool)
        or freshness_sla < 1
        or freshness_sla > 168
    ):
        raise FeedValidationError("WordPress側の鮮度期限が不正です。")

    comparison = source.get("officialComparison")
    if not isinstance(comparison, dict):
        raise FeedValidationError("公式画面との照合結果がありません。")
    item_count = len(items)
    for key in ("liveCount", "sourceCount", "matchedCount"):
        if comparison.get(key) != item_count:
            raise FeedValidationError("公式画面との照合件数が一致しません。")
    if comparison.get("codeMismatchCount") != 0:
        raise FeedValidationError("クーポンコードの不一致があります。")
    if comparison.get("periodMismatchCount") != 0:
        raise FeedValidationError("利用期間の不一致があります。")

    safety = source.get("safetyChecks")
    if not isinstance(safety, dict):
        raise FeedValidationError("自動同期の安全確認結果がありません。")
    if not allow_stale and safety.get("currentRunFresh") is not True:
        raise FeedValidationError("今回の監視実行で生成されたデータではありません。")
    for key in ("allItemsOfficialVisible", "uniqueIds"):
        if safety.get(key) is not True:
            raise FeedValidationError("公式画面の表示確認または識別子確認に失敗しています。")

    if counts.get("total") != item_count:
        raise FeedValidationError("監視フィードの合計件数が一致しません。")
    selectable_count = sum(item.get("selectable") is True for item in items)
    if counts.get("selectable") != selectable_count:
        raise FeedValidationError("通常選択できるクーポンの件数が一致しません。")
    if counts.get("needsReview") != item_count - selectable_count:
        raise FeedValidationError("要確認クーポンの件数が一致しません。")

    ids: set[str] = set()
    codes_by_id: dict[str, list[str]] = {}
    for item in items:
        if not isinstance(item, dict):
            raise FeedValidationError("クーポン明細がオブジェクトではありません。")
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id.startswith("his:"):
            raise FeedValidationError("HIS監視元の識別子がないクーポンがあります。")
        if item_id in ids:
            raise FeedValidationError("同じHISクーポンが重複しています。")
        ids.add(item_id)
        if (
            item.get("monitorSourceId") != "his-official-monitor"
            or item.get("providerSlug") != "his"
        ):
            raise FeedValidationError("クーポン明細の監視元がHISではありません。")
        if item.get("status") not in {"active", "upcoming", "ended"}:
            raise FeedValidationError("クーポンの配布状態が不正です。")
        if item.get("sourceUpdatedAt") != source.get("verifiedAt"):
            raise FeedValidationError("クーポン明細の確認時刻が揃っていません。")
        raw_codes = item.get("codes")
        if not isinstance(raw_codes, list) or not raw_codes:
            raise FeedValidationError("クーポンコードがないHISクーポンがあります。")
        codes: list[str] = []
        for code_entry in raw_codes:
            if not isinstance(code_entry, dict):
                raise FeedValidationError("クーポンコードの形式が不正です。")
            code = code_entry.get("code")
            if not isinstance(code, str) or not code.strip():
                raise FeedValidationError("空のクーポンコードがあります。")
            codes.append(code.strip())
        if len(codes) != len(set(codes)):
            raise FeedValidationError("同じクーポン内でコードが重複しています。")
        codes_by_id[item_id] = codes

    if item_count < 10:
        raise FeedValidationError("HIS公式表示クーポンが10件未満のため同期しません。")

    return {
        "verifiedAt": source["verifiedAt"],
        "itemCount": item_count,
        "ids": ids,
        "codesById": codes_by_id,
    }


def validated_site_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or not parsed.netloc:
        raise FeedValidationError("WordPressの接続先はHTTPSのURLにしてください。")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise FeedValidationError("WordPressの接続先URLに認証情報等を含めないでください。")
    return value.strip().rstrip("/")


def safe_error_text(value: Any, limit: int = 160) -> str:
    """監視ログに認証情報を含めず、障害理由だけを短く表示する。"""

    return " ".join(str(value or "").split())[:limit]


def http_error_summary(response: requests.Response) -> str:
    """WordPressの権限拒否と経路上の拒否を安全に区別する。"""

    content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
    detail = ""
    if content_type.lower() == "application/json":
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            code = safe_error_text(body.get("code"), 80)
            message = safe_error_text(body.get("message"))
            if code and message:
                detail = f" {code}: {message}"
            elif code or message:
                detail = f" {code or message}"

    route_signals = []
    if response.headers.get("CF-RAY"):
        route_signals.append("Cloudflare経由")
    if response.headers.get("X-Sucuri-ID"):
        route_signals.append("Sucuri経由")
    if response.headers.get("WWW-Authenticate"):
        route_signals.append("認証要求あり")
    if not detail and route_signals:
        detail = f"（{'、'.join(route_signals)}）"
    elif not detail and content_type:
        detail = f"（応答形式: {content_type}）"

    return f"WordPressが同期を拒否しました（HTTP {response.status_code}）。{detail}"


def request_json(
    session: requests.Session,
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    attempts: int = 3,
) -> dict[str, Any]:
    """接続失敗とサーバー障害だけを短く再試行する。"""

    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.request(
                method,
                url,
                json=payload,
                timeout=(10, 35),
                headers={"Accept": "application/json"},
            )
        except requests.RequestException as exc:
            last_error = exc
            if attempt == attempts:
                break
            time.sleep(attempt * 2)
            continue

        if response.status_code >= 500 and attempt < attempts:
            time.sleep(attempt * 2)
            continue
        if response.status_code >= 400:
            raise RuntimeError(http_error_summary(response))
        try:
            result = response.json()
        except ValueError as exc:
            raise RuntimeError("WordPressの応答をJSONとして読めません。") from exc
        if not isinstance(result, dict):
            raise RuntimeError("WordPressの応答形式が不正です。")
        return result

    raise RuntimeError("WordPressへ接続できませんでした。") from last_error


def verify_readback(
    response: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    sources = response.get("sources")
    items = response.get("items")
    if not isinstance(sources, list) or not isinstance(items, list):
        raise RuntimeError("WordPressの読戻し結果に監視データがありません。")

    his_sources = [
        source
        for source in sources
        if isinstance(source, dict) and source.get("id") == "his-official-monitor"
    ]
    if len(his_sources) != 1:
        raise RuntimeError("WordPressの読戻し結果でHIS監視元を一意に確認できません。")
    source = his_sources[0]
    if source.get("status") != "fresh":
        raise RuntimeError("WordPressのHIS監視データが最新状態ではありません。")
    if source.get("verifiedAt") != expected["verifiedAt"]:
        raise RuntimeError("送信時刻とWordPressの読戻し時刻が一致しません。")
    if source.get("itemCount") != expected["itemCount"]:
        raise RuntimeError("送信件数とWordPressの読戻し件数が一致しません。")

    current_items: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("monitorSourceId") != "his-official-monitor":
            continue
        if item.get("lifecycleState") == "missing":
            continue
        current_items.setdefault(str(item.get("id", "")), []).append(item)

    if set(current_items) != expected["ids"]:
        raise RuntimeError("WordPressの現在一覧と送信した識別子が一致しません。")
    for item_id, matched in current_items.items():
        if len(matched) != 1:
            raise RuntimeError("WordPressの現在一覧に同じクーポンが重複しています。")
        returned_codes = [
            str(entry.get("code", "")).strip()
            for entry in matched[0].get("codes", [])
            if isinstance(entry, dict)
        ]
        if returned_codes != expected["codesById"][item_id]:
            raise RuntimeError("WordPressの読戻しでクーポンコードが変わっています。")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="検証済みHIS監視フィードをWordPressへ同期する"
    )
    parser.add_argument("--feed", type=Path, default=DEFAULT_FEED_PATH)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--max-sync-age-hours",
        type=int,
        default=DEFAULT_MAX_SYNC_AGE_HOURS,
    )
    args = parser.parse_args()

    try:
        payload = load_feed(args.feed.resolve())
        expected = validate_feed(
            payload,
            max_sync_age_hours=args.max_sync_age_hours,
            allow_stale=args.dry_run,
        )
        encoded_size = len(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        if encoded_size > MAX_PAYLOAD_BYTES:
            raise FeedValidationError("送信内容が2MBを超えています。")
    except FeedValidationError as exc:
        print(f"❌ 送信前検証に失敗しました: {exc}", file=sys.stderr)
        return 1

    print(
        f"✅ WordPress送信前検証: {expected['itemCount']}件、"
        f"確認時刻 {expected['verifiedAt']}"
    )
    if args.dry_run:
        print("🧪 事前確認のため、WordPressへの送信は行いません。")
        return 0

    site_url = os.environ.get("YF_WP_URL", "")
    username = os.environ.get("YF_WP_USER", "")
    app_password = normalize_application_password(
        os.environ.get("YF_WP_APP_PASSWORD", "")
    )
    if not site_url or not username or not app_password:
        print("❌ WordPress同期用の認証設定が不足しています。", file=sys.stderr)
        return 1

    try:
        site_url = validated_site_url(site_url)
        with requests.Session() as session:
            session.auth = (username, app_password)
            stored = request_json(
                session,
                "POST",
                f"{site_url}/wp-json/{REST_NAMESPACE}/monitor-feed",
                payload=payload,
            )
            if (
                stored.get("stored") is not True
                or stored.get("itemCount") != expected["itemCount"]
                or stored.get("verifiedAt") != expected["verifiedAt"]
            ):
                raise RuntimeError("WordPressの保存結果が送信内容と一致しません。")
            readback = request_json(
                session,
                "GET",
                f"{site_url}/wp-json/{REST_NAMESPACE}/coupons"
                f"?provider=his&refresh={int(time.time())}",
            )
        verify_readback(readback, expected)
    except (FeedValidationError, RuntimeError) as exc:
        print(f"❌ WordPress同期に失敗しました: {exc}", file=sys.stderr)
        return 1

    print(
        f"✅ WordPressへ{expected['itemCount']}件を保存し、"
        "読戻しまで確認しました。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
