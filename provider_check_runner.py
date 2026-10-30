#!/usr/bin/env python3
"""会社別クーポンチェックの実行入口。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

from coupon_validator import NEEDS_REVIEW_EXIT_CODE
from official_deal_monitor import run_official_deal_monitor
from his_regions import (
    ALL_REGION_CODES,
    HIS_REGIONS,
    PRIMARY_REGION_CODE,
    select_region_coupons,
)

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent
REGISTRY = ROOT / "config" / "provider_registry.json"
STATUS_ROOT = ROOT / "provider_check_data"

REAL_MONITOR_COMMANDS: dict[str, list[str]] = {
    "his": [sys.executable, "his_coupon_monitor.py"],
    "jtb": [sys.executable, "jtb_coupon_monitor.py"],
    "knt": [sys.executable, "knt_coupon_monitor.py"],
    "jalpack": [
        sys.executable,
        "jalpack_coupon_monitor.py",
        "--source",
        "official",
        "--fetch-method",
        "chrome",
    ],
    "rurubu_travel": [sys.executable, "rurubu_travel_coupon_monitor.py"],
    "yukoyuko": [sys.executable, "yukoyuko_coupon_monitor.py"],
    # 一休を先に回す。同じ管理IDの印（shared_with）は相手の保存済みファイルを読んで付くため、
    # 登録表（config/provider_registry.json）でも ikkyu を yahoo_travel より前に置く
    "ikkyu": [sys.executable, "ikkyu_coupon_monitor.py"],
    "yahoo_travel": [sys.executable, "yahoo_travel_coupon_monitor.py"],
}


def now_jst() -> datetime:
    return datetime.now(JST)


def load_registry_config() -> dict:
    return json.loads(REGISTRY.read_text(encoding="utf-8"))


def load_registry() -> list[dict]:
    config = load_registry_config()
    schedule = config.get("schedule") or {}
    daily_ids = set(schedule.get("daily_provider_ids") or [])
    daily_defaults = schedule.get("daily") or {
        "check_frequency": "daily",
        "cadence_days": 1,
        "freshness_sla_hours": 30,
    }
    jp_daily_ids = set(schedule.get("jp_daily_provider_ids") or [])
    jp_daily_defaults = schedule.get("jp_daily") or {
        "check_frequency": "daily",
        "cadence_days": 1,
        "freshness_sla_hours": 30,
        "fetch_location": "jp_mac",
    }
    five_day_ids = set(schedule.get("every_5_days_provider_ids") or [])
    low_defaults = schedule.get("every_5_days") or {
        "check_frequency": "every_5_days",
        "cadence_days": 5,
        "freshness_sla_hours": 144,
    }
    on_demand_defaults = schedule.get("on_demand") or {
        "check_frequency": "on_demand",
        "cadence_days": 0,
        "freshness_sla_hours": 720,
    }
    long_unchanged_days = int(
        schedule.get("content_long_unchanged_days", DEFAULT_CONTENT_LONG_UNCHANGED_DAYS)
    )
    providers = []
    for raw in config["providers"]:
        provider = dict(raw)
        provider.setdefault("content_long_unchanged_days", long_unchanged_days)
        if provider["id"] in daily_ids:
            defaults = daily_defaults
        elif provider["id"] in jp_daily_ids:
            defaults = jp_daily_defaults
        elif provider["id"] in five_day_ids:
            defaults = low_defaults
        else:
            defaults = on_demand_defaults
        for key, value in defaults.items():
            provider.setdefault(key, value)
        if provider["id"] in REAL_MONITOR_COMMANDS:
            provider.setdefault("source_type", "official_monitor")
        elif provider.get("official_sources"):
            provider.setdefault("source_type", "official_page_candidate")
        else:
            provider.setdefault("source_type", "snapshot_only")
        providers.append(provider)
    return providers


def latest_coupon_file(data_dir: str | None) -> Path | None:
    if not data_dir:
        return None
    path = ROOT / data_dir
    if not path.exists():
        return None
    normal = sorted(path.glob("coupons_*.json"), reverse=True)
    if normal:
        return normal[0]
    dry_runs = sorted(path.glob("dry_run_coupons_*.json"), reverse=True)
    return dry_runs[0] if dry_runs else None


def load_latest_coupons(provider: dict) -> tuple[list[dict], str]:
    path = latest_coupon_file(provider.get("data_dir"))
    if not path:
        return [], ""
    try:
        return json.loads(path.read_text(encoding="utf-8")), path.name
    except (json.JSONDecodeError, OSError):
        return [], path.name


def data_date_from_filename(filename: str) -> str:
    match = re.search(r"\d{4}-\d{2}-\d{2}", filename or "")
    return match.group(0) if match else ""


DEFAULT_CONTENT_LONG_UNCHANGED_DAYS = 14


def parse_jst_datetime(value: str) -> datetime | None:
    """ISO形式の日時、または YYYY-MM-DD を JST の日時として読む。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    return parsed.replace(tzinfo=JST) if parsed.tzinfo is None else parsed.astimezone(JST)


def freshness_status(
    data_date: str,
    sla_hours: int,
    *,
    official: bool,
    last_checked_at: str = "",
) -> str:
    """期限超過かどうかを「最終確認日（最後に取得できた日時）」で判定する。

    中身が変わらない日はクーポンのJSONを書き直さないため、ファイル名の日付
    （data_date）で測ると、毎日取得できている会社まで期限超過に見えてしまう。
    最終確認日が分からない古い記録のときだけ、従来どおり data_date で測る。
    """
    if not official:
        return "snapshot_only"
    checked_at = parse_jst_datetime(last_checked_at)
    if checked_at:
        age_hours = (now_jst() - checked_at).total_seconds() / 3600
        return "fresh" if age_hours <= sla_hours else "stale"
    if not data_date:
        return "unknown"
    try:
        data_day = datetime.strptime(data_date, "%Y-%m-%d").date()
    except ValueError:
        return "unknown"
    age_hours = (now_jst().date() - data_day).days * 24
    return "fresh" if age_hours <= sla_hours else "stale"


def latest_change_log_date(data_dir: str | None) -> str:
    """change_log.json に残る最後の変化日（中身が最後に変わった日）を返す。"""
    if not data_dir:
        return ""
    path = ROOT / data_dir / "change_log.json"
    if not path.exists():
        return ""
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    dates = [
        str(entry.get("date") or "")
        for entry in entries
        if isinstance(entry, dict) and entry.get("date")
    ]
    return max(dates) if dates else ""


def content_change_summary(
    last_changed_at: str, *, long_unchanged_days: int
) -> dict[str, object]:
    """内容の最終更新日から、何日変わっていないかと要確認かどうかを出す。"""
    changed_at = parse_jst_datetime(last_changed_at)
    if not changed_at:
        return {
            "content_last_changed_at": "",
            "content_unchanged_days": None,
            "content_status": "unknown",
        }
    unchanged_days = (now_jst().date() - changed_at.date()).days
    threshold = max(1, int(long_unchanged_days))
    return {
        "content_last_changed_at": last_changed_at,
        "content_unchanged_days": unchanged_days,
        "content_status": "long_unchanged" if unchanged_days >= threshold else "changed",
    }


def coupon_url(coupon: dict) -> str:
    for key in ("detail_url", "source_url"):
        value = coupon.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    return ""


def is_checkable_url(url: str) -> bool:
    parsed = urlparse(url)
    return bool(parsed.scheme and parsed.netloc)


def check_url(url: str, timeout: int) -> dict:
    result = {
        "url": url,
        "ok": False,
        "status_code": "",
        "classification": "request_error",
        "error": "",
    }
    try:
        response = requests.head(url, allow_redirects=True, timeout=timeout)
        if response.status_code in {403, 405}:
            response = requests.get(url, allow_redirects=True, timeout=timeout, stream=True)
        status_code = response.status_code
        result["status_code"] = str(status_code)
        result["ok"] = 200 <= status_code < 400
        if result["ok"]:
            result["classification"] = "reachable"
        elif status_code == 404:
            result["classification"] = "not_found"
        elif status_code == 429:
            result["classification"] = "rate_limited"
        elif status_code in {401, 403}:
            result["classification"] = "access_blocked"
        else:
            result["classification"] = "http_error"
    except requests.RequestException as exc:
        result["error"] = exc.__class__.__name__
    return result


def write_status(provider: dict, payload: dict) -> Path:
    out_dir = STATUS_ROOT / provider["id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    latest = out_dir / "latest.json"
    latest.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    history = out_dir / f"check_{now_jst().strftime('%Y-%m-%d_%H%M%S')}.json"
    history.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return latest


def run_real_monitor(provider: dict) -> dict:
    command = REAL_MONITOR_COMMANDS[provider["id"]]
    started = now_jst()
    status = "success"
    error = ""
    print(f"::group::{provider['label']} real monitor")
    print("command:", " ".join(command))
    try:
        subprocess.run(command, cwd=ROOT, check=True)
    except subprocess.CalledProcessError as exc:
        if exc.returncode == NEEDS_REVIEW_EXIT_CODE:
            # 取得件数が前回より大きく減ったケース。スクレイパー側が前回データを
            # 残しているので、失敗ではなく「要確認」として扱う。
            status = "needs_review"
            error = "mass_disappearance_detected"
        else:
            status = "error"
            error = f"exit_code={exc.returncode}"
    finally:
        completed = now_jst()
        print("::endgroup::")

    coupons, latest_file = load_latest_coupons(provider)
    data_date = data_date_from_filename(latest_file)
    # 最終確認日。取得そのものは成功しているのに中身が変わらない日も記録する。
    last_checked_at = completed.isoformat() if status in {"success", "needs_review"} else ""
    payload = {
        "provider_id": provider["id"],
        "provider_label": provider["label"],
        "check_type": "official_monitor",
        "status": status,
        "started_at": started.isoformat(),
        "completed_at": completed.isoformat(),
        "official_fetched_at": completed.isoformat() if status == "success" else "",
        "last_checked_at": last_checked_at,
        "needs_review": status == "needs_review",
        "data_date": data_date,
        "freshness_status": freshness_status(
            data_date,
            int(provider.get("freshness_sla_hours", 30)),
            official=True,
            last_checked_at=last_checked_at,
        ),
        **content_change_summary(
            latest_change_log_date(provider.get("data_dir")),
            long_unchanged_days=int(
                provider.get(
                    "content_long_unchanged_days", DEFAULT_CONTENT_LONG_UNCHANGED_DAYS
                )
            ),
        ),
        "coupon_count": len(coupons),
        "latest_file": latest_file,
        "ai_used": False,
        "wp_review_eligible": False,
        "error": error,
    }
    if provider["id"] == "his" and status == "success":
        visual_path = (
            ROOT / provider["data_dir"] / "visual_observation_latest.json"
        )
        try:
            visual_summary = json.loads(visual_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            payload["status"] = "error"
            payload["error"] = f"regional_visual_summary={exc.__class__.__name__}"
        else:
            region_count = int(visual_summary.get("region_count") or 0)
            successful_region_count = int(
                visual_summary.get("successful_region_count") or 0
            )
            coverage_status = visual_summary.get("coverage_status")
            observed_region_codes = {
                str(region.get("code"))
                for region in visual_summary.get("regions", [])
                if region.get("code")
            }
            if (
                coverage_status != "complete"
                or region_count != len(HIS_REGIONS)
                or successful_region_count != len(HIS_REGIONS)
                or observed_region_codes != set(ALL_REGION_CODES)
            ):
                payload["status"] = "error"
                payload["error"] = (
                    "regional_coverage_incomplete="
                    f"{successful_region_count}/{len(HIS_REGIONS)}"
                )
            payload.update({
                "regional_coverage_status": coverage_status or "unknown",
                "region_count": region_count,
                "successful_region_count": successful_region_count,
                "regional_visible_count": visual_summary.get(
                    "regional_visible_count", len(coupons)
                ),
                "unique_coupon_variant_count": len(coupons),
                "primary_region_code": PRIMARY_REGION_CODE,
                "primary_region_coupon_count": len(
                    select_region_coupons(coupons, PRIMARY_REGION_CODE)
                ),
            })
    write_status(provider, payload)
    return payload


def run_snapshot_check(provider: dict, timeout: int, max_urls: int) -> dict:
    started = now_jst()
    coupons, latest_file = load_latest_coupons(provider)
    urls = []
    seen = set()
    if max_urls > 0:
        for coupon in coupons:
            url = coupon_url(coupon)
            if not url or not is_checkable_url(url) or url in seen:
                continue
            seen.add(url)
            urls.append(url)
            if len(urls) >= max_urls:
                break

    results = [check_url(url, timeout) for url in urls]
    ok_count = sum(1 for item in results if item["ok"])
    failed = [item for item in results if not item["ok"]]
    status = "snapshot_checked"
    if coupons and failed:
        status = "warning"
    if not coupons:
        status = "no_data"
    data_date = data_date_from_filename(latest_file)

    payload = {
        "provider_id": provider["id"],
        "provider_label": provider["label"],
        "check_type": "snapshot_url_check",
        "status": status,
        "started_at": started.isoformat(),
        "completed_at": now_jst().isoformat(),
        "url_checked_at": now_jst().isoformat(),
        "official_fetched_at": "",
        "last_checked_at": "",
        "data_date": data_date,
        "freshness_status": "snapshot_only",
        "coupon_count": len(coupons),
        "latest_file": latest_file,
        "checked_url_count": len(results),
        "ok_url_count": ok_count,
        "failed_url_count": len(failed),
        "failed_urls": failed[:10],
        "ai_used": False,
        "wp_review_eligible": False,
        "note": "旧スナップショット内のURL確認であり、最新クーポンの公式取得ではありません。",
    }
    write_status(provider, payload)
    print(
        f"{provider['id']}: {status} / coupons={len(coupons)} / "
        f"urls={len(results)} / ok={ok_count} / failed={len(failed)}"
    )
    return payload


def cadence_bucket(provider_id: str, cadence_days: int) -> int:
    digest = hashlib.sha256(provider_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:2], "big") % cadence_days


def is_on_demand(provider: dict) -> bool:
    """定期実行の対象外（--provider-id 指定のときだけ動く会社）か。"""
    return int(provider.get("cadence_days", 0)) <= 0 or provider.get("check_frequency") == "on_demand"


def is_jp_only(provider: dict) -> bool:
    """海外IPのGitHub Actionsからは取れず、日本のMacから取る会社か。

    まとめ実行（--scope due / daily / all など）からは外す。取得したいときは
    `--scope jp_mac` か `--provider-id <会社ID>` で明示する。
    """
    return provider.get("fetch_location") == "jp_mac"


def provider_due(provider: dict, current_date: date) -> bool:
    if is_on_demand(provider):
        return False
    cadence_days = max(1, int(provider.get("cadence_days", 5)))
    if cadence_days == 1:
        return True
    return current_date.toordinal() % cadence_days == cadence_bucket(provider["id"], cadence_days)


def select_providers(
    registry: list[dict], scope: str, provider_id: str, current_date: date | None = None
) -> list[dict]:
    if provider_id and provider_id != "all":
        selected = [provider for provider in registry if provider["id"] == provider_id]
        if not selected:
            raise SystemExit(f"unknown provider_id: {provider_id}")
        return selected
    if scope == "jp_mac":
        return [provider for provider in registry if is_jp_only(provider)]
    # 日本のMacから取る会社は、まとめ実行では必ず外す（海外IPでは拒否される）
    candidates = [provider for provider in registry if not is_jp_only(provider)]
    if scope == "daily":
        return [provider for provider in candidates if int(provider.get("cadence_days", 5)) == 1]
    if scope in {"every_5_days", "weekly"}:
        return [provider for provider in candidates if int(provider.get("cadence_days", 5)) == 5]
    if scope == "due":
        today = current_date or now_jst().date()
        return [provider for provider in candidates if provider_due(provider, today)]
    if scope == "on_demand":
        return [provider for provider in candidates if is_on_demand(provider)]
    return candidates


def deep_dive_allowed(provider: dict, *, deep: bool, explicit: bool) -> bool:
    """公式ページの深掘り（official_deal_monitor）を走らせてよいか。

    - 定期対象（daily / every_5_days）で official_sources がある会社は従来どおり常に走る
    - 指定時だけの会社（on_demand）は --deep か --provider-id の直指定があるときだけ走る
    """
    if not provider.get("official_sources"):
        return False
    if not is_on_demand(provider):
        return True
    return deep or explicit


def run_provider(
    provider: dict, timeout: int, max_urls: int, *, deep: bool = False, explicit: bool = False
) -> dict:
    if provider["id"] in REAL_MONITOR_COMMANDS:
        payload = run_real_monitor(provider)
        if deep or explicit:
            payload["deep_dive"] = True
            payload["deep_dive_at"] = payload.get("completed_at", "")
            write_status(provider, payload)
        return payload
    if deep_dive_allowed(provider, deep=deep, explicit=explicit):
        payload = run_official_deal_monitor(provider, timeout=max(timeout, 30))
        payload.setdefault(
            "freshness_status",
            freshness_status(
                payload.get("data_date", ""),
                int(provider.get("freshness_sla_hours", 30)),
                official=True,
                last_checked_at=str(payload.get("last_checked_at") or ""),
            ),
        )
        # 内容の最終更新日は official_source_data の state を見て official_deal_monitor が返す。
        # ここでは「何日変わっていないか」と要確認かどうかだけ足す。
        for key, value in content_change_summary(
            str(payload.get("content_last_changed_at") or ""),
            long_unchanged_days=int(
                provider.get(
                    "content_long_unchanged_days", DEFAULT_CONTENT_LONG_UNCHANGED_DAYS
                )
            ),
        ).items():
            payload[key] = value
        if is_on_demand(provider):
            payload["deep_dive"] = True
            payload["deep_dive_at"] = payload.get("completed_at", "")
        write_status(provider, payload)
        return payload
    return run_snapshot_check(provider, timeout=timeout, max_urls=max_urls)


def write_run_summary(scope: str, summaries: list[dict]) -> Path:
    audit_candidates = [
        {
            "candidate_id": item.get("audit_candidate_id"),
            "provider_id": item.get("provider_id"),
            "candidate_path": item.get("audit_candidate_path"),
            "change_kind": item.get("change_kind"),
            "status": item.get("status"),
        }
        for item in summaries
        if item.get("audit_candidate_id") and item.get("codex_audit_required")
    ]
    error_provider_ids = [
        item.get("provider_id") for item in summaries if item.get("status") == "error"
    ]
    needs_review_provider_ids = [
        item.get("provider_id") for item in summaries if item.get("status") == "needs_review"
    ]
    payload = {
        "completed_at": now_jst().isoformat(),
        "scope": scope,
        "provider_count": len(summaries),
        "error_count": len(error_provider_ids),
        "error_provider_ids": error_provider_ids,
        "needs_review_count": len(needs_review_provider_ids),
        "needs_review_provider_ids": needs_review_provider_ids,
        "codex_audit_candidate_count": len(audit_candidates),
        "codex_audit_candidates": audit_candidates,
        "wp_review_eligible_provider_ids": [],
        "summaries": summaries,
    }
    STATUS_ROOT.mkdir(parents=True, exist_ok=True)
    latest = STATUS_ROOT / "run-latest.json"
    latest.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    history = STATUS_ROOT / f"run_{now_jst().strftime('%Y-%m-%d_%H%M%S')}.json"
    history.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return latest


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="会社別クーポンチェック実行")
    parser.add_argument(
        "--scope",
        choices=["due", "daily", "every_5_days", "weekly", "on_demand", "jp_mac", "all"],
        default="due",
    )
    parser.add_argument("--provider-id", default="", help="特定会社だけ実行する場合の provider id")
    parser.add_argument("--timeout", type=int, default=10, help="URL生存確認のタイムアウト秒")
    parser.add_argument("--max-urls", type=int, default=30, help="記事抽出系で確認するURL上限")
    parser.add_argument(
        "--deep",
        action="store_true",
        help="指定時だけの会社でも公式ページを深掘り取得する（--scope all と併用可）",
    )
    parser.add_argument(
        "--fail-on-error",
        action="store_true",
        help="1社でも取得に失敗したら終了コード1で終わる（既定は失敗を記録して続行）",
    )
    return parser.parse_args(argv)


def run_provider_safely(
    provider: dict, *, timeout: int, max_urls: int, deep: bool, explicit: bool
) -> dict:
    """1社の取得で例外が出ても、その会社だけ失敗扱いにして他社へ進む。"""
    try:
        return run_provider(
            provider, timeout=timeout, max_urls=max_urls, deep=deep, explicit=explicit
        )
    except Exception as exc:  # noqa: BLE001 - 1社の想定外エラーで全体を止めない
        payload = {
            "provider_id": provider["id"],
            "provider_label": provider.get("label", provider["id"]),
            "check_type": "unexpected_exception",
            "status": "error",
            "started_at": now_jst().isoformat(),
            "completed_at": now_jst().isoformat(),
            "official_fetched_at": "",
            "coupon_count": len(load_latest_coupons(provider)[0]),
            "error": f"{exc.__class__.__name__}: {exc}"[:300],
        }
        write_status(provider, payload)
        return payload


def report_run_outcome(summaries: list[dict], *, fail_on_error: bool) -> bool:
    """実行結果を注記付きで出力し、終了コード1にすべきかを返す。"""
    errors = [item for item in summaries if item.get("status") == "error"]
    reviews = [item for item in summaries if item.get("status") == "needs_review"]
    for item in reviews:
        print(
            f"::warning::{item.get('provider_label')}: 取得件数が前回より大きく減ったため"
            f"要確認にしました（前回データは残しています）"
        )
    for item in errors:
        print(
            f"::warning::{item.get('provider_label')}: 取得に失敗しました"
            f"（{item.get('error', '')[:120]}）。他社の保存とダッシュボード更新は続けます"
        )
    print(
        f"結果: {len(summaries)}社中 失敗{len(errors)}社 / 要確認{len(reviews)}社"
        + (f"（失敗: {', '.join(item['provider_id'] for item in errors)}）" if errors else "")
    )
    if not errors:
        return False
    if fail_on_error:
        return True
    # 全社が失敗したときだけは、後続の生成処理に意味がないので失敗で止める。
    return len(errors) == len(summaries)


def main(argv: list[str] | None = None) -> None:
    # 子プロセスの出力と順番が揃うように行単位で書き出す（ログで原因を追えるようにする）
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass
    args = parse_args(argv or sys.argv[1:])
    registry = load_registry()
    providers = select_providers(registry, args.scope, args.provider_id)
    print(f"selected providers: {', '.join(provider['id'] for provider in providers)}")
    explicit = bool(args.provider_id and args.provider_id != "all")
    summaries = [
        run_provider_safely(
            provider,
            timeout=args.timeout,
            max_urls=args.max_urls,
            deep=args.deep,
            explicit=explicit,
        )
        for provider in providers
    ]
    write_run_summary(args.scope, summaries)
    if report_run_outcome(summaries, fail_on_error=args.fail_on_error):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
