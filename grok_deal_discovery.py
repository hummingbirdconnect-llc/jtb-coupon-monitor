#!/usr/bin/env python3
"""Grokで「ダッシュボードに無いクーポン・セール・キャンペーン」の候補を集める。

- Grok CLI（~/.grok/bin/grok）の web 検索を使い、会社ごとに直近の情報を探す
- 結果は **未確認の候補** として `discovery_hints/<provider_id>/hints_YYYY-MM-DD.json` に保存する
- 既知のクーポン題名・登録済み公式URLと突き合わせて `status`（new / known_title / known_url）を付ける
- Codex監査の候補（codex_audit_queue）には入れない。公式ページで裏付けが取れたものだけ、人が
  `config/provider_registry.json` の `official_sources` へ登録する

実行例:
    python3 grok_deal_discovery.py --provider-id relux
    python3 grok_deal_discovery.py --scope auto      # 常時6社は毎日、指定時の会社は月曜だけ
    python3 grok_deal_discovery.py --scope daily     # 常時6社だけ
    python3 grok_deal_discovery.py --scope weekly    # 指定時の会社（official_sources あり）だけ
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from coupon_validator import _title_similarity
from provider_check_runner import (
    is_on_demand,
    latest_coupon_file,
    load_registry,
)

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent
HINTS_ROOT = ROOT / "discovery_hints"
PROMPT_TEMPLATE = ROOT / "templates" / "grok_discovery_prompt.md"
GROK_CANONICAL = Path.home() / ".grok" / "bin" / "grok"
DEFAULT_DAYS = 14
DEFAULT_TIMEOUT = 900
MAX_TURNS = 40  # 検索→閲覧を繰り返すため。6では「max turns reached」で止まった
KNOWN_TITLE_THRESHOLD = 0.80
KINDS = ["coupon", "sale", "campaign", "points", "member_benefit", "other"]
CONFIDENCES = ["high", "medium", "low"]

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "kind": {"type": "string", "enum": KINDS},
                    "found_url": {"type": "string"},
                    "official_url": {"type": "string"},
                    "evidence": {"type": "string"},
                    "observed_date": {"type": "string"},
                    "discount": {"type": "string"},
                    "period": {"type": "string"},
                    "confidence": {"type": "string", "enum": CONFIDENCES},
                },
                "required": ["title", "kind", "found_url", "evidence", "confidence"],
            },
        }
    },
    "required": ["candidates"],
}


def now_jst() -> datetime:
    return datetime.now(JST)


def configure_root(root: Path) -> None:
    """テスト用に出力先を切り替える。"""
    global ROOT, HINTS_ROOT
    ROOT = root
    HINTS_ROOT = root / "discovery_hints"


# ---------------------------------------------------------------------------
# 対象会社の選択
# ---------------------------------------------------------------------------


def discovery_eligible(provider: dict[str, Any]) -> bool:
    """Grok検索の対象にする会社か（常時監視、または公式ページを登録済みの指定時会社）。"""
    if not is_on_demand(provider):
        return True
    return bool(provider.get("official_sources"))


def select_providers(
    registry: list[dict[str, Any]], scope: str, provider_id: str, today: date | None = None
) -> list[dict[str, Any]]:
    if provider_id and provider_id != "all":
        selected = [provider for provider in registry if provider["id"] == provider_id]
        if not selected:
            raise SystemExit(f"unknown provider_id: {provider_id}")
        return selected
    eligible = [provider for provider in registry if discovery_eligible(provider)]
    daily = [provider for provider in eligible if not is_on_demand(provider)]
    weekly = [provider for provider in eligible if is_on_demand(provider)]
    if scope == "daily":
        return daily
    if scope == "weekly":
        return weekly
    if scope == "auto":
        current = today or now_jst().date()
        return daily + (weekly if current.weekday() == 0 else [])
    return eligible


# ---------------------------------------------------------------------------
# プロンプト作成
# ---------------------------------------------------------------------------


def _load_coupons(data_dir: str | None) -> list[dict[str, Any]]:
    path = latest_coupon_file(data_dir)
    if not path:
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def known_coupons(provider: dict[str, Any]) -> list[dict[str, Any]]:
    coupons = _load_coupons(provider.get("data_dir"))
    if not coupons and provider.get("legacy_data_dir"):
        coupons = _load_coupons(provider.get("legacy_data_dir"))
    return coupons


def known_titles(provider: dict[str, Any]) -> list[str]:
    titles: list[str] = []
    for coupon in known_coupons(provider):
        title = str(coupon.get("title") or "").strip()
        if title and title not in titles:
            titles.append(title)
    return titles


def canonical_url(url: str) -> str:
    parts = urlsplit(str(url or "").strip())
    if not parts.scheme or not parts.netloc:
        return ""
    path = parts.path.rstrip("/") or "/"
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}{path}"


def registered_urls(provider: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for source in provider.get("official_sources") or []:
        url = canonical_url(source.get("url", ""))
        if url and url not in urls:
            urls.append(url)
    for coupon in known_coupons(provider):
        for key in ("detail_url", "source_url"):
            url = canonical_url(coupon.get(key, ""))
            if url and url not in urls:
                urls.append(url)
    return urls


def build_prompt(provider: dict[str, Any], days: int = DEFAULT_DAYS, template: str | None = None) -> str:
    text = template if template is not None else PROMPT_TEMPLATE.read_text(encoding="utf-8")
    domains = ", ".join(provider.get("official_domains") or []) or "（未登録）"
    sources = [str(source.get("url")) for source in provider.get("official_sources") or [] if source.get("url")]
    titles = known_titles(provider)[:60]
    replacements = {
        "{{label}}": provider["label"],
        "{{days}}": str(days),
        "{{official_domains}}": domains,
        "{{registered_urls}}": "\n".join(f"  - {url}" for url in sources) or "  - （なし）",
        "{{known_titles}}": "\n".join(f"  - {title}" for title in titles) or "  - （なし）",
    }
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text


# ---------------------------------------------------------------------------
# Grok 実行
# ---------------------------------------------------------------------------


def resolve_grok_binary(explicit: str | None = None) -> Path:
    candidates = [explicit, os.environ.get("GROK_BIN"), str(GROK_CANONICAL), shutil.which("grok")]
    for candidate in candidates:
        if candidate and Path(candidate).expanduser().exists():
            return Path(candidate).expanduser()
    raise FileNotFoundError("grok CLI が見つかりません（~/.grok/bin/grok）。`grok login` 済みか確認してください。")


def run_grok_cli(prompt: str, *, binary: Path, model: str = "", timeout: int = DEFAULT_TIMEOUT) -> str:
    """Grok CLI を1回だけ実行し、標準出力を返す。web検索は有効のまま。"""
    with tempfile.TemporaryDirectory(prefix="grok-discovery-") as tmp:
        prompt_path = Path(tmp) / "prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        command = [
            str(binary),
            "--prompt-file",
            str(prompt_path),
            "--json-schema",
            json.dumps(OUTPUT_SCHEMA, ensure_ascii=False),
            "--output-format",
            "json",
            "--permission-mode",
            "dontAsk",
            "--no-memory",
            "--no-subagents",
            "--no-plan",
            "--max-turns",
            str(MAX_TURNS),
            "--cwd",
            tmp,
        ]
        if model:
            command += ["--model", model]
        completed = subprocess.run(
            command,
            cwd=tmp,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()[-600:]
            raise RuntimeError(f"grok exit={completed.returncode}: {detail}")
        return completed.stdout


def _find_candidates(obj: Any) -> list[dict[str, Any]] | None:
    """CLIの出力の入れ子から candidates 配列を探す。"""
    if isinstance(obj, dict):
        if isinstance(obj.get("candidates"), list):
            return obj["candidates"]
        for value in obj.values():
            found = _find_candidates(value)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_candidates(value)
            if found is not None:
                return found
    elif isinstance(obj, str) and "candidates" in obj:
        try:
            return _find_candidates(json.loads(obj))
        except json.JSONDecodeError:
            return None
    return None


def parse_grok_output(stdout: str) -> list[dict[str, Any]]:
    text = (stdout or "").strip()
    if not text:
        raise ValueError("grok の出力が空でした")
    try:
        found = _find_candidates(json.loads(text))
        if found is not None:
            return found
    except json.JSONDecodeError:
        pass
    # 1行1JSON（streaming系）や前後に文章が付く場合に備えて、JSONらしい塊を順に試す
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            found = _find_candidates(json.loads(line))
        except json.JSONDecodeError:
            continue
        if found is not None:
            return found
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            found = _find_candidates(json.loads(match.group(0)))
            if found is not None:
                return found
        except json.JSONDecodeError:
            pass
    raise ValueError("grok の出力から candidates を読み取れませんでした")


# ---------------------------------------------------------------------------
# 既知判定と保存
# ---------------------------------------------------------------------------


def _host_matches(url: str, domains: list[str]) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return False
    return any(host == domain or host.endswith("." + domain) for domain in (d.lower() for d in domains))


def classify_candidate(
    candidate: dict[str, Any], provider: dict[str, Any], *, titles: list[str], urls: list[str]
) -> dict[str, Any]:
    title = str(candidate.get("title") or "").strip()
    official_url = str(candidate.get("official_url") or "").strip()
    found_url = str(candidate.get("found_url") or "").strip()
    domains = provider.get("official_domains") or []
    status = "new"
    matched_title = ""
    for url in (official_url, found_url):
        if url and canonical_url(url) in urls:
            status = "known_url"
            break
    if status == "new" and title:
        best = 0.0
        for known in titles:
            score = _title_similarity(title, known)
            if score > best:
                best, matched_title = score, known
        if best >= KNOWN_TITLE_THRESHOLD:
            status = "known_title"
    kind = candidate.get("kind") if candidate.get("kind") in KINDS else "other"
    confidence = candidate.get("confidence") if candidate.get("confidence") in CONFIDENCES else "low"
    return {
        "title": title[:80],
        "kind": kind,
        "found_url": found_url,
        "official_url": official_url,
        "official_domain_match": bool(official_url) and _host_matches(official_url, domains),
        "evidence": str(candidate.get("evidence") or "")[:120],
        "observed_date": str(candidate.get("observed_date") or "")[:10],
        "discount": str(candidate.get("discount") or "")[:60],
        "period": str(candidate.get("period") or "")[:80],
        "confidence": confidence,
        "status": status,
        "matched_title": matched_title if status == "known_title" else "",
    }


def dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    result = []
    for candidate in candidates:
        key = (candidate["title"], canonical_url(candidate.get("official_url") or candidate.get("found_url") or ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


def write_hints(provider: dict[str, Any], payload: dict[str, Any], at: datetime) -> Path:
    out_dir = HINTS_ROOT / provider["id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"hints_{at.strftime('%Y-%m-%d')}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def latest_hints_file(provider_id: str) -> Path | None:
    out_dir = HINTS_ROOT / provider_id
    if not out_dir.exists():
        return None
    files = sorted(out_dir.glob("hints_*.json"), reverse=True)
    return files[0] if files else None


def discover_provider(
    provider: dict[str, Any],
    *,
    runner: Callable[[str], str],
    days: int = DEFAULT_DAYS,
    model: str = "",
    at: datetime | None = None,
) -> dict[str, Any]:
    at = at or now_jst()
    titles = known_titles(provider)
    urls = registered_urls(provider)
    payload: dict[str, Any] = {
        "provider_id": provider["id"],
        "provider_label": provider["label"],
        "generated_at": at.isoformat(),
        "search_days": days,
        "model": model or "default",
        "source": "grok_web_search",
        "verification": "unverified",
        "status": "ok",
        "error": "",
        "known_title_count": len(titles),
        "registered_url_count": len(urls),
        "candidates": [],
    }
    try:
        stdout = runner(build_prompt(provider, days=days))
        raw = parse_grok_output(stdout)
        classified = [
            classify_candidate(item, provider, titles=titles, urls=urls)
            for item in raw
            if isinstance(item, dict) and str(item.get("title") or "").strip()
        ]
        payload["candidates"] = dedupe_candidates(classified)
    except Exception as exc:  # noqa: BLE001 - 1社の失敗で全体を止めない
        payload["status"] = "error"
        payload["error"] = f"{exc.__class__.__name__}: {exc}"[:400]
    payload["new_count"] = sum(item["status"] == "new" for item in payload["candidates"])
    payload["candidate_count"] = len(payload["candidates"])
    payload["hints_file"] = str(write_hints(provider, payload, at).relative_to(ROOT))
    return payload


def write_run_summary(scope: str, results: list[dict[str, Any]], at: datetime) -> Path:
    HINTS_ROOT.mkdir(parents=True, exist_ok=True)
    summary = {
        "completed_at": at.isoformat(),
        "scope": scope,
        "provider_count": len(results),
        "error_count": sum(item["status"] == "error" for item in results),
        "candidate_count": sum(item["candidate_count"] for item in results),
        "new_count": sum(item["new_count"] for item in results),
        "results": [
            {
                key: item[key]
                for key in (
                    "provider_id",
                    "provider_label",
                    "status",
                    "error",
                    "candidate_count",
                    "new_count",
                    "hints_file",
                )
            }
            for item in results
        ],
    }
    path = HINTS_ROOT / "run-latest.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Grokで未確認のクーポン・セール候補を集める")
    parser.add_argument("--scope", choices=["auto", "daily", "weekly", "all"], default="auto")
    parser.add_argument("--provider-id", default="", help="1社だけ実行する場合の provider id")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="何日前までの情報を探すか")
    parser.add_argument("--model", default="", help="Grokのモデルを固定する場合")
    parser.add_argument("--grok-bin", default="", help="grok CLI の場所（既定: ~/.grok/bin/grok）")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="1社あたりの秒数上限")
    parser.add_argument("--dry-run", action="store_true", help="Grokを呼ばずプロンプトだけ表示する")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    registry = load_registry()
    providers = select_providers(registry, args.scope, args.provider_id)
    print(f"selected providers: {', '.join(provider['id'] for provider in providers) or '(none)'}")
    if args.dry_run:
        for provider in providers:
            print(f"\n===== {provider['id']} =====\n{build_prompt(provider, days=args.days)}")
        return 0
    binary = resolve_grok_binary(args.grok_bin or None)

    def runner(prompt: str) -> str:
        return run_grok_cli(prompt, binary=binary, model=args.model, timeout=args.timeout)

    at = now_jst()
    results = []
    for provider in providers:
        result = discover_provider(provider, runner=runner, days=args.days, model=args.model, at=at)
        print(
            f"{provider['id']}: {result['status']} / candidates={result['candidate_count']} / "
            f"new={result['new_count']}" + (f" / {result['error']}" if result["error"] else "")
        )
        results.append(result)
    summary = write_run_summary(args.scope, results, at)
    print(f"summary: {summary.relative_to(ROOT)}")
    return 1 if results and all(item["status"] == "error" for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
