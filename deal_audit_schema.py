#!/usr/bin/env python3
"""Codexによる公式クーポン監査結果の検証ルール。"""

from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import urlparse

AUDIT_SCHEMA_VERSION = 1
RECOMMENDATIONS = {"draft", "hold", "ignore"}
CLASSIFICATIONS = {"publishable", "conditional", "unpublishable", "ended"}
CAMPAIGN_TYPES = {"coupon", "sale", "campaign", "points", "member_benefit"}
DEAL_STATUSES = {"active", "upcoming", "ended", "unknown"}
CONFIDENCE_LEVELS = {"high", "medium", "low"}
REQUIRED_DEAL_FIELDS = {
    "title",
    "campaign_type",
    "status",
    "classification",
    "discount",
    "coupon_code",
    "booking_start",
    "booking_end",
    "travel_start",
    "travel_end",
    "eligibility",
    "official_url",
    "evidence_quote",
    "confidence",
}


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def stable_deal_id(provider_id: str, deal: dict[str, Any]) -> str:
    material = "|".join(
        str(deal.get(key) or "").strip().lower()
        for key in ("title", "coupon_code", "official_url")
    )
    return f"{provider_id}-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:16]}"


def source_text(candidate: dict[str, Any]) -> str:
    return normalize_text(
        " ".join(str(source.get("text") or "") for source in candidate.get("sources") or [])
    )


def source_urls(candidate: dict[str, Any]) -> list[str]:
    return [
        str(source.get("url") or "")
        for source in candidate.get("sources") or []
        if source.get("verification_result") == "confirmed" and source.get("url")
    ]


def _official_domain(url: str, allowed_domains: set[str]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == domain or host.endswith(f".{domain}") for domain in allowed_domains)


def _date_tokens_supported(value: str, evidence: str) -> bool:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    year, month, day = value.split("-")
    variants = [
        f"{year}-{month}-{day}",
        f"{year}/{int(month)}/{int(day)}",
        f"{year}年{int(month)}月{int(day)}日",
    ]
    return any(variant in evidence for variant in variants)


def validate_audit_result(
    candidate: dict[str, Any], result: dict[str, Any]
) -> list[str]:
    """監査結果が候補ファイル内の公式根拠だけに基づくか検証する。"""
    errors: list[str] = []
    if result.get("schema_version") != AUDIT_SCHEMA_VERSION:
        errors.append("schema_version mismatch")
    if result.get("candidate_id") != candidate.get("candidate_id"):
        errors.append("candidate_id mismatch")
    if result.get("provider_id") != candidate.get("provider_id"):
        errors.append("provider_id mismatch")
    if result.get("recommendation") not in RECOMMENDATIONS:
        errors.append("recommendation is invalid")

    priority = result.get("priority")
    if not isinstance(priority, int) or isinstance(priority, bool) or not 0 <= priority <= 100:
        errors.append("priority must be an integer between 0 and 100")
    if not isinstance(result.get("uncertainty_reasons"), list):
        errors.append("uncertainty_reasons is not a list")

    deals = result.get("deals")
    if not isinstance(deals, list):
        return errors + ["deals is not a list"]

    normalized_source = source_text(candidate)
    allowed_urls = source_urls(candidate)
    allowed_domains = set(candidate.get("official_domains") or [])
    for index, deal in enumerate(deals):
        prefix = f"deal[{index}]"
        if not isinstance(deal, dict):
            errors.append(f"{prefix}: deal is not an object")
            continue
        missing = REQUIRED_DEAL_FIELDS - set(deal)
        if missing:
            errors.append(f"{prefix}: missing fields {', '.join(sorted(missing))}")
            continue
        if not str(deal.get("title") or "").strip():
            errors.append(f"{prefix}: title is empty")
        if deal.get("campaign_type") not in CAMPAIGN_TYPES:
            errors.append(f"{prefix}: campaign_type is invalid")
        if deal.get("status") not in DEAL_STATUSES:
            errors.append(f"{prefix}: status is invalid")
        if deal.get("classification") not in CLASSIFICATIONS:
            errors.append(f"{prefix}: classification is invalid")
        if deal.get("confidence") not in CONFIDENCE_LEVELS:
            errors.append(f"{prefix}: confidence is invalid")

        evidence = normalize_text(str(deal.get("evidence_quote") or ""))
        official_url = str(deal.get("official_url") or "")
        if not evidence or evidence not in normalized_source:
            errors.append(f"{prefix}: evidence_quote is not an exact source excerpt")
        if official_url not in allowed_urls or not _official_domain(official_url, allowed_domains):
            errors.append(f"{prefix}: official_url is not a confirmed source")

        code = deal.get("coupon_code")
        if code and str(code) not in evidence:
            errors.append(f"{prefix}: coupon_code is absent from evidence")

        discount = deal.get("discount")
        if discount:
            numeric_tokens = re.findall(r"\d[\d,]*(?:\.\d+)?", str(discount))
            if numeric_tokens and not all(token in evidence for token in numeric_tokens):
                errors.append(f"{prefix}: discount numbers are absent from evidence")

        for key in ("booking_start", "booking_end", "travel_start", "travel_end"):
            value = deal.get(key)
            if value and not _date_tokens_supported(str(value), evidence):
                errors.append(f"{prefix}: {key} is invalid or absent from evidence")

        for start_key, end_key in (
            ("booking_start", "booking_end"),
            ("travel_start", "travel_end"),
        ):
            start, end = deal.get(start_key), deal.get(end_key)
            if start and end and str(start) > str(end):
                errors.append(f"{prefix}: {start_key} is after {end_key}")

        classification = deal.get("classification")
        status = deal.get("status")
        if classification == "ended" and status != "ended":
            errors.append(f"{prefix}: ended classification requires ended status")
        if status == "ended" and classification not in {"ended", "unpublishable"}:
            errors.append(f"{prefix}: ended status requires ended classification")

    return errors


def approved_deals(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        deal
        for deal in result.get("deals") or []
        if deal.get("classification") in {"publishable", "conditional", "ended"}
    ]


def audit_is_high_confidence(result: dict[str, Any], errors: list[str]) -> bool:
    deals = approved_deals(result)
    return bool(deals) and not errors and not (result.get("uncertainty_reasons") or []) and all(
        deal.get("confidence") == "high" for deal in deals
    )
