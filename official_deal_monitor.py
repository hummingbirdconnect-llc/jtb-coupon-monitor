#!/usr/bin/env python3
"""公式ページを取得し、変更時にCodex監査候補を作成する。"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

from deal_audit_schema import normalize_text, stable_deal_id

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent
STATE_ROOT = ROOT / "official_source_data"
AUDIT_QUEUE_ROOT = ROOT / "codex_audit_queue"
USER_AGENT = (
    "Mozilla/5.0 (compatible; HBConnectCouponMonitor/2.0; "
    "+https://hummingbirdconnect-llc.github.io/jtb-coupon-monitor/dashboard/)"
)
MIN_PAGE_TEXT = 250
MAX_AUDIT_SOURCE_CHARS = 80_000
RELEVANT_PATTERN = re.compile(
    r"クーポン|coupon|割引|off|セール|sale|キャンペーン|campaign|ポイント|point|特典|benefit",
    re.IGNORECASE,
)


class OfficialFetchError(RuntimeError):
    """公式ソースを正常に取得できなかった。"""


def now_jst() -> datetime:
    return datetime.now(JST)


def configure_root(root: Path) -> None:
    """テストや別checkout向けに入出力ルートをまとめて切り替える。"""
    global ROOT, STATE_ROOT, AUDIT_QUEUE_ROOT
    ROOT = root
    STATE_ROOT = root / "official_source_data"
    AUDIT_QUEUE_ROOT = root / "codex_audit_queue"


def _visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for node in soup(["script", "style", "noscript", "svg", "header", "nav", "footer"]):
        node.decompose()
    return normalize_text(soup.get_text(" ", strip=True))


def _embedded_json_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    chunks: list[str] = []
    for script in soup.find_all("script"):
        script_type = str(script.get("type") or "").lower()
        script_id = str(script.get("id") or "")
        body = script.string or script.get_text(" ", strip=True)
        if not body:
            continue
        if script_type in {"application/json", "application/ld+json"} or script_id in {
            "__NEXT_DATA__",
            "__NUXT_DATA__",
        }:
            chunks.append(normalize_text(body))
    return normalize_text(" ".join(chunks))


def _requests_fetch(url: str, timeout: int = 30, retries: int = 2) -> tuple[str, int]:
    last_error = ""
    for attempt in range(retries + 1):
        try:
            response = requests.get(
                url,
                headers={"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.8"},
                timeout=timeout,
            )
            if response.status_code == 429:
                last_error = "HTTP 429 rate limited"
                if attempt < retries:
                    time.sleep(2 ** attempt)
                    continue
            if response.status_code != 200:
                raise OfficialFetchError(f"HTTP {response.status_code}")
            return response.text, response.status_code
        except requests.RequestException as exc:
            last_error = exc.__class__.__name__
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
    raise OfficialFetchError(last_error or "request failed")


def _playwright_fetch(url: str, timeout: int = 45) -> str:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise OfficialFetchError("Playwright is not installed") from exc

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent=USER_AGENT,
                locale="ja-JP",
                viewport={"width": 1280, "height": 1600},
            )
            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            page.wait_for_timeout(1500)
            status = response.status if response else 0
            if status != 200:
                raise OfficialFetchError(f"Playwright HTTP {status}")
            html = page.content()
            browser.close()
            return html
    except OfficialFetchError:
        raise
    except Exception as exc:
        raise OfficialFetchError(f"Playwright {exc.__class__.__name__}") from exc


def fetch_official_source(source: dict[str, Any], timeout: int = 30) -> dict[str, Any]:
    url = source["url"]
    requested_method = source.get("fetch_method", "auto")
    html = ""
    request_error = ""

    if requested_method in {"auto", "html", "embedded_json"}:
        try:
            html, status_code = _requests_fetch(url, timeout=timeout)
            embedded = _embedded_json_text(html)
            visible = _visible_text(html)
            if requested_method == "embedded_json" and len(embedded) >= MIN_PAGE_TEXT:
                return {
                    "url": url,
                    "ok": True,
                    "status_code": status_code,
                    "fetch_method": "embedded_json",
                    "text": embedded,
                    "error": "",
                }
            if len(visible) >= MIN_PAGE_TEXT:
                combined = visible
                if embedded and embedded not in visible:
                    combined = normalize_text(f"{visible} {embedded}")
                return {
                    "url": url,
                    "ok": True,
                    "status_code": status_code,
                    "fetch_method": "html",
                    "text": combined,
                    "error": "",
                }
            request_error = f"page text too short ({len(visible)})"
        except OfficialFetchError as exc:
            request_error = str(exc)

    if requested_method in {"auto", "playwright"}:
        try:
            html = _playwright_fetch(url, timeout=max(timeout, 45))
            visible = _visible_text(html)
            if len(visible) < MIN_PAGE_TEXT:
                raise OfficialFetchError(f"Playwright page text too short ({len(visible)})")
            return {
                "url": url,
                "ok": True,
                "status_code": 200,
                "fetch_method": "playwright",
                "text": visible,
                "error": "",
            }
        except OfficialFetchError as exc:
            request_error = f"{request_error}; {exc}".strip("; ")

    return {
        "url": url,
        "ok": False,
        "status_code": "",
        "fetch_method": requested_method,
        "text": "",
        "error": request_error or "unsupported fetch method",
    }


def _state_path(provider_id: str) -> Path:
    return STATE_ROOT / provider_id / "state.json"


def relevant_excerpt(text: str, limit: int) -> str:
    normalized = normalize_text(text)
    if len(normalized) <= limit:
        return normalized
    pieces = [normalized[: min(2000, limit)]]
    seen = set()
    used = len(pieces[0])
    for match in RELEVANT_PATTERN.finditer(normalized):
        start = max(0, match.start() - 300)
        end = min(len(normalized), match.end() + 1200)
        chunk = normalized[start:end]
        key = hashlib.sha256(chunk[:300].encode("utf-8")).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        remaining = limit - used - 1
        if remaining <= 0:
            break
        pieces.append(chunk[:remaining])
        used += len(pieces[-1]) + 1
    return normalize_text(" ".join(pieces))[:limit]


def build_audit_source_text(source_results: list[dict[str, Any]]) -> str:
    successful = [source for source in source_results if source.get("ok")]
    if not successful:
        return ""
    per_source_limit = max(4000, MAX_AUDIT_SOURCE_CHARS // len(successful))
    chunks = [
        f"SOURCE {source['url']} {relevant_excerpt(source['text'], per_source_limit)}"
        for source in successful
    ]
    return normalize_text(" ".join(chunks))[:MAX_AUDIT_SOURCE_CHARS]


# 旧テスト・手動コマンドとの互換名。OpenAI APIは呼び出さない。
build_ai_source_text = build_audit_source_text
MAX_AI_SOURCE_CHARS = MAX_AUDIT_SOURCE_CHARS


def load_state(provider_id: str) -> dict[str, Any]:
    path = _state_path(provider_id)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(provider_id: str, state: dict[str, Any]) -> None:
    path = _state_path(provider_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _latest_coupon_file(data_dir: str) -> Path | None:
    path = ROOT / data_dir
    files = sorted(path.glob("coupons_*.json"), reverse=True) if path.exists() else []
    return files[0] if files else None


def _load_previous_coupons(data_dir: str) -> list[dict[str, Any]]:
    latest = _latest_coupon_file(data_dir)
    if not latest:
        return []
    try:
        return json.loads(latest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _period(start: str | None, end: str | None) -> str:
    if start and end:
        return f"{start} - {end}"
    return start or end or ""


def _stock_status(status: str) -> str:
    return {
        "active": "配布中",
        "upcoming": "配布予定",
        "ended": "配布終了",
        "unknown": "要確認",
    }.get(status, "要確認")


def convert_deals(provider_id: str, result: dict[str, Any], fetched_at: str, model: str) -> list[dict]:
    coupons = []
    for deal in result.get("deals") or []:
        code = deal.get("coupon_code")
        coupon = {
            "id": stable_deal_id(provider_id, deal),
            "title": deal["title"],
            "category": deal["campaign_type"],
            "type": deal["campaign_type"],
            "discount": deal.get("discount") or "",
            "stock_status": _stock_status(deal.get("status", "unknown")),
            "booking_period": _period(deal.get("booking_start"), deal.get("booking_end")),
            "travel_period": _period(deal.get("travel_start"), deal.get("travel_end")),
            "conditions": deal.get("eligibility") or "",
            "coupon_codes": [code] if code else [],
            "detail_url": deal["official_url"],
            "source_url": deal["official_url"],
            "source_type": "official_codex_audit",
            "fetch_method": model,
            "confidence": deal["confidence"],
            "classification": deal.get("classification", "conditional"),
            "evidence_quote": deal["evidence_quote"],
            "official_fetched_at": fetched_at,
        }
        coupons.append(coupon)
    return coupons


def _semantic_hash(coupons: list[dict[str, Any]]) -> str:
    fields = []
    for coupon in sorted(coupons, key=lambda item: item.get("id", "")):
        fields.append(
            {
                key: coupon.get(key)
                for key in (
                    "id",
                    "title",
                    "discount",
                    "stock_status",
                    "booking_period",
                    "travel_period",
                    "conditions",
                    "coupon_codes",
                    "detail_url",
                )
            }
        )
    encoded = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _merge_without_deleting(
    current: list[dict[str, Any]], previous: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    current_ids = {coupon.get("id") for coupon in current}
    retained_missing: list[str] = []
    merged = list(current)
    for coupon in previous:
        if coupon.get("id") in current_ids:
            continue
        retained = dict(coupon)
        retained["stock_status"] = "要確認"
        retained["confidence"] = "low"
        retained["missing_from_latest"] = True
        retained_missing.append(str(coupon.get("id") or coupon.get("title") or "unknown"))
        merged.append(retained)
    return merged, retained_missing


def _write_coupons(data_dir: str, coupons: list[dict[str, Any]], at: datetime) -> Path:
    out_dir = ROOT / data_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"coupons_{at.strftime('%Y-%m-%d')}.json"
    path.write_text(json.dumps(coupons, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _candidate_path(provider_id: str, candidate_id: str) -> Path:
    return AUDIT_QUEUE_ROOT / provider_id / f"{candidate_id}.json"


def _candidate_sources(source_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    successful = [source for source in source_results if source.get("ok")]
    if not successful:
        return []
    per_source_limit = max(4000, MAX_AUDIT_SOURCE_CHARS // len(successful))
    candidates: list[dict[str, Any]] = []
    for source in successful:
        text = relevant_excerpt(str(source.get("text") or ""), per_source_limit)
        candidates.append(
            {
                "url": source["url"],
                "status_code": source.get("status_code"),
                "fetch_method": source.get("fetch_method", ""),
                "verification_method": source.get("fetch_method", ""),
                "verification_result": "confirmed",
                "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "text": text,
            }
        )
    return candidates


def write_audit_candidate(
    provider: dict[str, Any],
    *,
    candidate_id: str,
    content_hash: str,
    change_kind: str,
    source_results: list[dict[str, Any]],
    fetched_at: datetime,
) -> Path:
    payload = {
        "schema_version": 1,
        "candidate_id": candidate_id,
        "provider_id": provider["id"],
        "provider_label": provider["label"],
        "change_kind": change_kind,
        "fetched_at": fetched_at.isoformat(),
        "content_hash": content_hash,
        "data_dir": provider["data_dir"],
        "official_domains": provider.get("official_domains") or [],
        "sources": _candidate_sources(source_results),
        "previous_coupons": _load_previous_coupons(provider["data_dir"]),
        "audit_contract": {
            "allowed_recommendations": ["draft", "hold", "ignore"],
            "allowed_classifications": [
                "publishable",
                "conditional",
                "unpublishable",
                "ended",
            ],
            "official_evidence_only": True,
            "baseline_must_not_create_wp_draft": True,
        },
    }
    path = _candidate_path(provider["id"], candidate_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def run_official_deal_monitor(provider: dict[str, Any], timeout: int = 30) -> dict[str, Any]:
    started = now_jst()
    source_results = [fetch_official_source(source, timeout=timeout) for source in provider["official_sources"]]
    failed_sources = [source for source in source_results if not source["ok"]]
    successful_sources = [source for source in source_results if source["ok"]]
    public_source_results = []
    for source in source_results:
        public = {key: value for key, value in source.items() if key != "text"}
        public["text_chars"] = len(source.get("text", ""))
        public["content_hash"] = (
            hashlib.sha256(source["text"].encode("utf-8")).hexdigest() if source.get("text") else ""
        )
        public_source_results.append(public)

    base_payload: dict[str, Any] = {
        "provider_id": provider["id"],
        "provider_label": provider["label"],
        "check_type": "official_page_candidate",
        "started_at": started.isoformat(),
        "completed_at": now_jst().isoformat(),
        "official_fetched_at": now_jst().isoformat() if successful_sources else "",
        "source_results": public_source_results,
        "failed_source_count": len(failed_sources),
        "ai_used": False,
        "codex_audit_required": False,
        "wp_review_eligible": False,
    }
    if not successful_sources:
        return {
            **base_payload,
            "status": "error",
            "error": "; ".join(source["error"] for source in failed_sources),
            "coupon_count": len(_load_previous_coupons(provider["data_dir"])),
        }

    full_source_material = normalize_text(
        " ".join(f"SOURCE {source['url']} {source['text']}" for source in successful_sources)
    )
    current_hash = hashlib.sha256(full_source_material.encode("utf-8")).hexdigest()
    state = load_state(provider["id"])
    previous_processed_hash = state.get("processed_hash", "")
    previous_queued_hash = state.get("queued_hash", "")
    change_kind = "baseline" if not previous_processed_hash else "update"
    previous_coupons = _load_previous_coupons(provider["data_dir"])
    latest_file = _latest_coupon_file(provider["data_dir"])

    state.update(
        {
            "provider_id": provider["id"],
            "observed_hash": current_hash,
            "last_checked_at": now_jst().isoformat(),
            "sources": base_payload["source_results"],
        }
    )

    if current_hash == previous_processed_hash:
        state["last_audit_status"] = "processed"
        save_state(provider["id"], state)
        return {
            **base_payload,
            "status": "unchanged",
            "change_kind": "unchanged",
            "data_date": latest_file.stem.replace("coupons_", "") if latest_file else "",
            "latest_file": latest_file.name if latest_file else "",
            "coupon_count": len(previous_coupons),
            "content_hash": current_hash,
            "error": "",
        }

    candidate_id = f"{provider['id']}-{current_hash[:16]}"
    candidate_path = _candidate_path(provider["id"], candidate_id)
    if current_hash != previous_queued_hash or not candidate_path.exists():
        candidate_path = write_audit_candidate(
            provider,
            candidate_id=candidate_id,
            content_hash=current_hash,
            change_kind=change_kind,
            source_results=successful_sources,
            fetched_at=now_jst(),
        )
        state.update(
            {
                "queued_hash": current_hash,
                "queued_candidate_id": candidate_id,
                "queued_at": now_jst().isoformat(),
                "last_audit_status": "pending",
            }
        )
    save_state(provider["id"], state)

    held = current_hash == previous_queued_hash and state.get("last_audit_status") == "held"
    return {
        **base_payload,
        "status": "audit_held" if held else (
            "baseline_pending" if change_kind == "baseline" else "audit_pending"
        ),
        "change_kind": change_kind,
        "content_hash": current_hash,
        "data_date": latest_file.stem.replace("coupons_", "") if latest_file else "",
        "latest_file": latest_file.name if latest_file else "",
        "coupon_count": len(previous_coupons),
        "codex_audit_required": not held,
        "audit_candidate_id": candidate_id,
        "audit_candidate_path": str(candidate_path.relative_to(ROOT)),
        "error": "",
    }
