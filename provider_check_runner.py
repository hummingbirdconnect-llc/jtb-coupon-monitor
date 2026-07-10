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

from official_deal_monitor import run_official_deal_monitor

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
    low_defaults = schedule.get("every_5_days") or {
        "check_frequency": "every_5_days",
        "cadence_days": 5,
        "freshness_sla_hours": 144,
    }
    providers = []
    for raw in config["providers"]:
        provider = dict(raw)
        defaults = daily_defaults if provider["id"] in daily_ids else low_defaults
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


def freshness_status(data_date: str, sla_hours: int, *, official: bool) -> str:
    if not official:
        return "snapshot_only"
    if not data_date:
        return "unknown"
    try:
        data_day = datetime.strptime(data_date, "%Y-%m-%d").date()
    except ValueError:
        return "unknown"
    age_hours = (now_jst().date() - data_day).days * 24
    return "fresh" if age_hours <= sla_hours else "stale"


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
        status = "error"
        error = f"exit_code={exc.returncode}"
    finally:
        completed = now_jst()
        print("::endgroup::")

    coupons, latest_file = load_latest_coupons(provider)
    data_date = data_date_from_filename(latest_file)
    payload = {
        "provider_id": provider["id"],
        "provider_label": provider["label"],
        "check_type": "official_monitor",
        "status": status,
        "started_at": started.isoformat(),
        "completed_at": completed.isoformat(),
        "official_fetched_at": completed.isoformat() if status == "success" else "",
        "data_date": data_date,
        "freshness_status": freshness_status(
            data_date, int(provider.get("freshness_sla_hours", 30)), official=True
        ),
        "coupon_count": len(coupons),
        "latest_file": latest_file,
        "ai_used": False,
        "wp_review_eligible": False,
        "error": error,
    }
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


def provider_due(provider: dict, current_date: date) -> bool:
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
    if scope == "daily":
        return [provider for provider in registry if int(provider.get("cadence_days", 5)) == 1]
    if scope in {"every_5_days", "weekly"}:
        return [provider for provider in registry if int(provider.get("cadence_days", 5)) == 5]
    if scope == "due":
        today = current_date or now_jst().date()
        return [provider for provider in registry if provider_due(provider, today)]
    return registry


def run_provider(provider: dict, timeout: int, max_urls: int) -> dict:
    if provider["id"] in REAL_MONITOR_COMMANDS:
        return run_real_monitor(provider)
    if provider.get("official_sources"):
        payload = run_official_deal_monitor(provider, timeout=max(timeout, 30))
        payload.setdefault(
            "freshness_status",
            freshness_status(
                payload.get("data_date", ""),
                int(provider.get("freshness_sla_hours", 30)),
                official=True,
            ),
        )
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
    payload = {
        "completed_at": now_jst().isoformat(),
        "scope": scope,
        "provider_count": len(summaries),
        "error_count": sum(item.get("status") == "error" for item in summaries),
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
        choices=["due", "daily", "every_5_days", "weekly", "all"],
        default="due",
    )
    parser.add_argument("--provider-id", default="", help="特定会社だけ実行する場合の provider id")
    parser.add_argument("--timeout", type=int, default=10, help="URL生存確認のタイムアウト秒")
    parser.add_argument("--max-urls", type=int, default=30, help="記事抽出系で確認するURL上限")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv or sys.argv[1:])
    registry = load_registry()
    providers = select_providers(registry, args.scope, args.provider_id)
    print(f"selected providers: {', '.join(provider['id'] for provider in providers)}")
    summaries = [
        run_provider(provider, timeout=args.timeout, max_urls=args.max_urls) for provider in providers
    ]
    write_run_summary(args.scope, summaries)
    if any(item.get("status") == "error" for item in summaries):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
