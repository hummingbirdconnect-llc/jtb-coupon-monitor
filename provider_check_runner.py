#!/usr/bin/env python3
"""
会社別クーポンチェックの実行入口。

GitHub Actions から daily / weekly / provider 指定で呼び出し、
実スクレイパーがある会社は監視スクリプトを実行する。
記事抽出・手元マスター由来の会社は、既存JSON内のURL生存確認を行う。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent
REGISTRY = ROOT / "config" / "provider_registry.json"
STATUS_ROOT = ROOT / "provider_check_data"

DAILY_PROVIDERS = {"his", "jtb", "yukoyuko"}
REAL_MONITOR_COMMANDS: dict[str, list[str]] = {
    "his": [sys.executable, "his_coupon_monitor.py"],
    "jtb": [sys.executable, "jtb_coupon_monitor.py"],
    "knt": [sys.executable, "knt_coupon_monitor.py"],
    "jalpack": [sys.executable, "jalpack_coupon_monitor.py", "--source", "official", "--fetch-method", "chrome"],
    "rurubu_travel": [sys.executable, "rurubu_travel_coupon_monitor.py"],
    "yukoyuko": [sys.executable, "yukoyuko_coupon_monitor.py"],
}


def now_jst() -> datetime:
    return datetime.now(JST)


def load_registry() -> list[dict]:
    return json.loads(REGISTRY.read_text(encoding="utf-8"))["providers"]


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
    return json.loads(path.read_text(encoding="utf-8")), path.name


def coupon_url(coupon: dict) -> str:
    for key in ("detail_url", "source_url"):
        value = coupon.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    return ""


def is_checkable_url(url: str) -> bool:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return False
    # ASP計測URLはHEADが不安定なことがあるため、到達確認対象としては残す。
    return True


def check_url(url: str, timeout: int) -> dict:
    result = {"url": url, "ok": False, "status_code": "", "error": ""}
    try:
        response = requests.head(url, allow_redirects=True, timeout=timeout)
        if response.status_code in {403, 405}:
            response = requests.get(url, allow_redirects=True, timeout=timeout, stream=True)
        result["status_code"] = str(response.status_code)
        result["ok"] = response.status_code < 500
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
    completed = None
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
    payload = {
        "provider_id": provider["id"],
        "provider_label": provider["label"],
        "check_type": "official_monitor",
        "status": status,
        "started_at": started.isoformat(),
        "completed_at": completed.isoformat(),
        "coupon_count": len(coupons),
        "latest_file": latest_file,
        "error": error,
    }
    write_status(provider, payload)
    if status == "error":
        raise SystemExit(f"{provider['id']} monitor failed: {error}")
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
    status = "success"
    if coupons and failed:
        status = "warning"
    if not coupons:
        status = "no_data"

    payload = {
        "provider_id": provider["id"],
        "provider_label": provider["label"],
        "check_type": "snapshot_url_check",
        "status": status,
        "started_at": started.isoformat(),
        "completed_at": now_jst().isoformat(),
        "coupon_count": len(coupons),
        "latest_file": latest_file,
        "checked_url_count": len(results),
        "ok_url_count": ok_count,
        "failed_url_count": len(failed),
        "failed_urls": failed[:10],
        "note": "記事抽出・手元マスター由来の既存URL生存確認です。新規クーポンの公式取得ではありません。",
    }
    write_status(provider, payload)
    print(
        f"{provider['id']}: {status} / coupons={len(coupons)} / "
        f"urls={len(results)} / ok={ok_count} / failed={len(failed)}"
    )
    return payload


def select_providers(registry: list[dict], scope: str, provider_id: str) -> list[dict]:
    if provider_id and provider_id != "all":
        selected = [provider for provider in registry if provider["id"] == provider_id]
        if not selected:
            raise SystemExit(f"unknown provider_id: {provider_id}")
        return selected
    if scope == "daily":
        return [provider for provider in registry if provider["id"] in DAILY_PROVIDERS]
    if scope == "weekly":
        return [provider for provider in registry if provider["id"] not in DAILY_PROVIDERS]
    return registry


def run_provider(provider: dict, timeout: int, max_urls: int) -> dict:
    if provider["id"] in REAL_MONITOR_COMMANDS:
        return run_real_monitor(provider)
    return run_snapshot_check(provider, timeout=timeout, max_urls=max_urls)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="会社別クーポンチェック実行")
    parser.add_argument("--scope", choices=["daily", "weekly", "all"], default="all")
    parser.add_argument("--provider-id", default="", help="特定会社だけ実行する場合の provider id")
    parser.add_argument("--timeout", type=int, default=10, help="URL生存確認のタイムアウト秒")
    parser.add_argument("--max-urls", type=int, default=30, help="記事抽出系で確認するURL上限")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv or sys.argv[1:])
    registry = load_registry()
    providers = select_providers(registry, args.scope, args.provider_id)
    print(f"selected providers: {', '.join(provider['id'] for provider in providers)}")
    summaries = [run_provider(provider, timeout=args.timeout, max_urls=args.max_urls) for provider in providers]
    failed = [item for item in summaries if item["status"] == "error"]
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
