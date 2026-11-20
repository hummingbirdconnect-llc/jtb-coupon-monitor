"""Publishable, positive-only HIS code comparison. No fetching or posting."""
from collections import Counter
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from article_watch import stamp

PRODUCER = "his-visible-code-v1"
MAX_AGE = timedelta(hours=18)
TARGETS = {
    "welltrip": (4954, "https://travel.hb-connect.co.jp/his-couponcode/"),
    "yakushimafan": (53330, "https://yakushima.fun/his-coupon/"),
}
CODE = re.compile(r"(?=[A-Za-z0-9]{5,40}$)(?=.*[A-Za-z])(?=.*\d)[A-Z0-9]+")
MONEY = re.compile(r"(?:\d[\d,\s]*円|\d+\s*[%％])\s*(?:引き|割引|OFF)", re.I)
ENDED = re.compile(r"配布終了|受付終了|終了しました|現在利用できません")
HIDDEN = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)


def as_time(value):
    value = stamp(value)
    return datetime.fromisoformat(value) if value else None


def within(value, now):
    t = as_time(value)
    return t if t and now - MAX_AGE < t <= now else None


def official_codes(root, now):
    try:
        meta = json.loads((root / "provider_check_data/his/latest.json").read_text())
        fetch_at = within(meta.get("official_fetched_at"), now)
        data_day = date.fromisoformat(meta["data_date"])
        name = "coupons_" + data_day.isoformat() + ".json"
        if not (fetch_at and meta.get("provider_id") == "his" and meta.get("status") == "success"
                and meta.get("freshness_status") == "fresh" and meta.get("needs_review") is False
                and meta.get("ai_used") is False and meta.get("regional_coverage_status") == "complete"
                and meta.get("region_count") == meta.get("successful_region_count") == 11
                and meta.get("latest_file") == name
                and data_day == fetch_at.astimezone(timezone(timedelta(hours=9))).date()):
            return {}
        rows = json.loads((root / "his_coupon_data" / name).read_text())
        if not isinstance(rows, list) or len(rows) != meta.get("coupon_count"):
            return {}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}

    counts = Counter()
    row_ids = Counter()
    valid = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("coupon_codes"), list):
            return {}
        rid = row.get("id")
        if not isinstance(rid, str) or not rid or len(rid) > 200:
            return {}
        row_ids[rid] += 1
        checked = within(row.get("official_checked_at"), now)
        source = row.get("source_url")
        try:
            parsed = urlsplit(source) if isinstance(source, str) else None
        except ValueError:
            parsed = None
        public = (parsed and parsed.scheme == "https" and parsed.netloc == "www.his-j.com"
                  and parsed.path.startswith("/campaign/") and not parsed.query and not parsed.fragment)
        for entry in row["coupon_codes"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("code"), str):
                return {}
            code = entry["code"]
            if not CODE.fullmatch(code):
                continue
            counts[code] += 1
            if (checked and timedelta(0) <= fetch_at - checked <= timedelta(hours=1)
                and public and row.get("source_type") == "official_visible_dom"
                and row.get("official_visibility") == "visible"
                and row.get("confidence") == "公式画面表示確認済み"
                and row.get("stock_status") == "配布中"):
                valid[code] = (rid, checked)
    return {code: proof for code, proof in valid.items()
            if counts[code] == 1 and row_ids[proof[0]] == 1}


def displayed_codes(site, rendered):
    if not isinstance(rendered, str) or len(rendered) > 2_000_000:
        return set()
    page = BeautifulSoup(rendered, "html.parser")
    for elt in page.find_all(True):
        if elt.parent is None or elt.name is None:
            continue
        if (elt.name in {"script", "style", "template", "noscript", "del", "s"}
                or "hidden" in elt.attrs or elt.get("aria-hidden") == "true"
                or HIDDEN.search(elt.get("style", ""))):
            elt.decompose()
    seen = Counter()
    for anchor in page.select("li > a"):
        # Exact rendered label, not links, comments, metadata, partial codes or prose.
        code = anchor.get_text(strip=True)
        if not CODE.fullmatch(code):
            continue
        card = anchor.parent
        heading = card.find_previous(["h2", "h3", "h4"])
        if (ENDED.search(card.get_text(" ", strip=True))
                or (heading and ENDED.search(heading.get_text(" ", strip=True)))):
            continue
        if site == "welltrip":
            if "afb-section-coupon-code" not in anchor.get("class", []):
                continue
        elif site == "yakushimafan":
            if not MONEY.search(card.get_text(" ", strip=True)):
                continue
        else:
            continue
        seen[code] += 1
    return {code for code, count in seen.items() if count == 1}


def build_proof(root, scope, observations, now):
    findings = []
    official = official_codes(root, now)
    for s in scope.get("sites", []):
        site = s.get("site")
        if site not in TARGETS or s.get("status") != "取得済み" or not within(s.get("checked_at"), now):
            continue
        pid, url = TARGETS[site]
        matches = [a for a in s.get("articles", []) if a.get("post_id") == pid and a.get("url") == url]
        post = observations.get((site, url))
        if len(matches) != 1 or not isinstance(post, dict):
            continue
        article = matches[0]
        modified = stamp(article.get("modified"))
        if not (modified and post.get("id") == pid and post.get("link") == url
                and post.get("status") == "publish" and post.get("password") == ""
                and post.get("modified_at") == modified):
            continue
        observed = within(post.get("observed_at"), now)
        if not observed or observed < as_time(modified):
            continue
        matched_offers = {official[code] for code in
                          displayed_codes(site, post.get("rendered")) & official.keys()}
        for rid, checked in sorted(matched_offers):
            findings.append(dict(site=site, post_id=pid,
                offer_id=hashlib.sha256(("his-public-offer:" + rid).encode()).hexdigest(),
                field="code", status="match", article_verified=True, official_verified=True,
                official_url="https://www.his-j.com/", checked_at=max(observed, checked).isoformat(),
                article_modified=modified, expires_at=(min(observed, checked) + MAX_AGE).isoformat()))
    return dict(schema_version=1, producer=PRODUCER, public_export=True, findings=findings)
