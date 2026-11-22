#!/usr/bin/env python3
"""登録URLが404・403・トップへの転送になったとき、移転先の候補を探す。

- きっかけ: 登録URLが 404 / 410 / 403 を返す、またはサイトのトップや別の区画へ転送される。
- 探し方（この順。1社あたりの通信は max_requests 回まで、間隔を空ける）:
  a. 末尾の「/」を付け外しした形と、http→https の形
  b. robots.txt の Sitemap 行（無ければ /sitemap.xml）。索引サイトマップは1階層だけ辿る
  c. サイトのトップと、登録した一覧ページ（discovery_listing_urls）のリンク
- robots.txt を守り、同じドメイン（登録可能ドメイン）のURLだけを扱う。
- 計測用ホスト（BLOCKED_TRACKING_HOSTS）と一休・Yahoo!トラベルには一切通信しない。
- 候補は 200 で開けて同じドメインに留まったものだけを記録する。
- 登録URL（config/provider_registry.json）は書き換えない。変更はオーナー確認のPRで行う。
"""

from __future__ import annotations

import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Callable
from urllib import robotparser
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

URL_CHANGE_LABEL = "URL変更の可能性"

# 計測・成果報酬用のホスト。候補・転送先・リンクのどれでも通信しない。
BLOCKED_TRACKING_HOSTS = frozenset(
    {
        "ck.jp.ap.valuecommerce.com",
        "ad.jp.ap.valuecommerce.com",
        "px.a8.net",
        "t.afi-b.com",
    }
)

# 自動の取得を断っている2社。会社IDでもホストでも外す（本人の方針 2026-11-10〜）。
EXCLUDED_PROVIDER_IDS = frozenset({"ikkyu", "yahoo_travel"})
EXCLUDED_HOST_SUFFIXES = ("ikyu.com", "travel.yahoo.co.jp")

TRIGGER_STATUSES = frozenset({403, 404, 410})
DEFAULT_MAX_REQUESTS = 10
DEFAULT_DELAY_SECONDS = 1.0
DEFAULT_TIMEOUT_SECONDS = 10
MAX_REDIRECTS = 5
MAX_REPORTED_CANDIDATES = 5
ROBOTS_AGENT = "HBConnectCouponMonitor"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; HBConnectCouponMonitor/2.0; "
    "+https://hummingbirdconnect-llc.github.io/jtb-coupon-monitor/dashboard/)"
)

GENERIC_KEYWORDS = (
    "ブラックフライデー",
    "blackfriday",
    "black-friday",
    "black_friday",
    "blackflyday",
    "クーポン",
    "coupon",
    "キャンペーン",
    "campaign",
)
# パスから語を作るときに捨てる語（言語・拡張子・汎用語）
_STOP_TOKENS = {
    "www", "jp", "ja", "en", "index", "html", "htm", "php", "aspx", "top", "page",
    "pages", "air", "fair", "feature", "special", "sp", "pc", "lp",
}
_ASSET_SUFFIXES = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".pdf", ".css", ".js",
    ".zip", ".mp4", ".ico", ".xml", ".gz",
)
_SECOND_LEVEL_JP = {"co", "ne", "or", "ac", "go", "lg", "ed", "gr", "ad"}
_SECOND_LEVEL_GENERIC = {"com", "net", "org", "gov", "edu"}


class DiscoveryBlocked(RuntimeError):
    """通信してはいけないURLへ通信しようとした。"""


def host_of(url: str) -> str:
    return (urlsplit(str(url or "")).hostname or "").lower().rstrip(".")


def _host_in(host: str, suffixes: tuple[str, ...] | frozenset[str]) -> bool:
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


def is_blocked_host(host: str) -> bool:
    """計測用ホストか、一休・Yahoo!トラベルのホストなら True。"""
    host = (host or "").lower().rstrip(".")
    return _host_in(host, BLOCKED_TRACKING_HOSTS) or _host_in(host, EXCLUDED_HOST_SUFFIXES)


def is_request_allowed(url: str) -> bool:
    parts = urlsplit(str(url or ""))
    if parts.scheme not in {"http", "https"}:
        return False
    host = host_of(url)
    return bool(host) and not is_blocked_host(host)


def is_excluded_provider(provider: dict[str, Any]) -> bool:
    """一休・Yahoo!トラベルは、IDでも公式ドメインでも探索の対象外にする。"""
    if str(provider.get("id") or "") in EXCLUDED_PROVIDER_IDS:
        return True
    for domain in provider.get("official_domains") or []:
        if is_blocked_host(str(domain)):
            return True
    for source in provider.get("official_sources") or []:
        if is_blocked_host(host_of(source.get("url", ""))):
            return True
    return False


def registrable_domain(host: str) -> str:
    """example.co.jp → example.co.jp、www.jal.co.jp → jal.co.jp、a.b.com → b.com。"""
    labels = [label for label in (host or "").lower().rstrip(".").split(".") if label]
    if len(labels) <= 2:
        return ".".join(labels)
    if len(labels[-1]) == 2 and (
        labels[-2] in _SECOND_LEVEL_JP or labels[-2] in _SECOND_LEVEL_GENERIC
    ):
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def same_site(url_a: str, url_b: str) -> bool:
    host_a, host_b = host_of(url_a), host_of(url_b)
    return bool(host_a and host_b) and registrable_domain(host_a) == registrable_domain(host_b)


def _path_segments(url: str) -> list[str]:
    segments = [segment for segment in urlsplit(url).path.split("/") if segment]
    if segments and re.fullmatch(r"index\.\w+", segments[-1]):
        segments = segments[:-1]
    return segments


def _canonical(url: str) -> str:
    parts = urlsplit(str(url or "").strip())
    path = parts.path.rstrip("/") or "/"
    return f"{parts.netloc.lower()}{path}"


def redirected_to_generic_page(registered_url: str, final_url: str) -> bool:
    """登録URLから、トップや別の区画へ転送されたとみなすか。"""
    if not final_url or _canonical(final_url) == _canonical(registered_url):
        return False
    if not same_site(registered_url, final_url):
        return True
    registered = _path_segments(registered_url)
    final = _path_segments(final_url)
    if not final:
        return True
    if not registered:
        return False
    if registered[: len(final)] == final and len(final) < len(registered):
        # 登録URLの上の階層（一覧やサイトの言語別トップ）へ戻された
        return True
    return final[0] != registered[0]


def needs_discovery(registered_url: str, status: int | str | None, final_url: str = "") -> str:
    """探索のきっかけになる理由を返す。不要なら空文字。"""
    try:
        code = int(status) if status not in (None, "") else 0
    except (TypeError, ValueError):
        code = 0
    if code in TRIGGER_STATUSES:
        return f"http_{code}"
    if 200 <= code < 400 and final_url and redirected_to_generic_page(registered_url, final_url):
        return "redirect_to_generic_page"
    return ""


def derive_keywords(provider: dict[str, Any], registered_url: str) -> dict[str, int]:
    """語→重み。会社別の語（discovery_keywords）を最優先し、旧パスの語、汎用語の順。"""
    weights: dict[str, int] = {}
    for keyword in GENERIC_KEYWORDS:
        weights[keyword.lower()] = 1
    for segment in _path_segments(registered_url):
        for token in re.split(r"[-_.]+", segment.lower()):
            if len(token) < 4 or token in _STOP_TOKENS or token.isdigit():
                continue
            weights[token] = max(weights.get(token, 0), 2)
    for keyword in provider.get("discovery_keywords") or []:
        keyword = str(keyword).strip().lower()
        if keyword:
            weights[keyword] = 3
    return weights


def score_candidate(url: str, text: str, keywords: dict[str, int], registered_url: str) -> int:
    haystack = f"{urlsplit(url).path} {text}".lower()
    score = sum(weight for keyword, weight in keywords.items() if keyword in haystack)
    if score <= 0:
        return 0
    # 旧URLの最後の語と綴りが近いパス（blackfriday → blackflyday など）を上げる
    old_tail = [s.lower() for s in _path_segments(registered_url)[-1:]]
    new_segments = [s.lower() for s in _path_segments(url)]
    for old in old_tail:
        if len(old) < 5:
            continue
        if any(SequenceMatcher(None, old, new).ratio() >= 0.75 for new in new_segments):
            score += 2
    return score


@dataclass
class FetchResult:
    url: str
    status: int = 0
    final_url: str = ""
    text: str = ""
    error: str = ""


@dataclass
class DiscoveryClient:
    """回数・間隔・robots.txt・遮断ホストを守る小さな取得役。"""

    session: Any = None
    max_requests: int = DEFAULT_MAX_REQUESTS
    delay_seconds: float = DEFAULT_DELAY_SECONDS
    timeout: int = DEFAULT_TIMEOUT_SECONDS
    user_agent: str = DEFAULT_USER_AGENT
    sleep: Callable[[float], None] = time.sleep
    requested_urls: list[str] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    _robots: dict[str, Any] = field(default_factory=dict)
    _sitemaps: dict[str, list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.session is None:
            self.session = requests.Session()

    @property
    def requests_left(self) -> int:
        return max(0, self.max_requests - len(self.requested_urls))

    def _raw_get(self, url: str) -> FetchResult:
        if not is_request_allowed(url):
            # ここへ来たら呼び出し側の誤り。通信せずに止める。
            raise DiscoveryBlocked(url)
        if self.requests_left <= 0:
            return FetchResult(url=url, error="request budget exhausted")
        if self.requested_urls and self.delay_seconds > 0:
            self.sleep(self.delay_seconds)
        self.requested_urls.append(url)
        try:
            response = self.session.get(
                url,
                headers={"User-Agent": self.user_agent, "Accept-Language": "ja,en;q=0.8"},
                timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            return FetchResult(url=url, error=exc.__class__.__name__)
        text = ""
        status = int(getattr(response, "status_code", 0) or 0)
        if status == 200:
            text = getattr(response, "text", "") or ""
        location = ""
        headers = getattr(response, "headers", {}) or {}
        if 300 <= status < 400:
            location = str(headers.get("Location") or headers.get("location") or "")
        return FetchResult(url=url, status=status, final_url=location, text=text)

    def _robot_parser(self, url: str) -> Any:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin in self._robots:
            return self._robots[origin]
        parser = robotparser.RobotFileParser()
        robots_url = origin + "/robots.txt"
        result = self._raw_get(robots_url)
        for _ in range(3):  # robots.txt の転送は同じサイト内だけ辿る
            if not (300 <= result.status < 400 and result.final_url):
                break
            target = urljoin(robots_url, result.final_url)
            if not is_request_allowed(target) or not same_site(target, origin + "/"):
                result = FetchResult(url=robots_url, error="robots.txt redirected off site")
                break
            robots_url = target
            result = self._raw_get(robots_url)
        if result.error:
            parser = None  # 確かめられないので、このサイトでは何も取らない
        elif result.status in (401, 403):
            parser.disallow_all = True
        elif result.status == 200:
            parser.parse(result.text.splitlines())
        else:
            parser.allow_all = True
        self._robots[origin] = parser
        if parser is not None:
            self._sitemaps[origin] = list(parser.site_maps() or [])
        return parser

    def robots_allows(self, url: str) -> bool:
        parser = self._robot_parser(url)
        if parser is None:
            return False
        return bool(parser.can_fetch(ROBOTS_AGENT, url))

    def sitemaps_for(self, url: str) -> list[str]:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        self._robot_parser(url)
        return list(self._sitemaps.get(origin) or [])

    def fetch(self, url: str, *, site_url: str) -> FetchResult:
        """遮断ホスト・robots.txt・同じドメインを毎回（転送先も）確かめて取る。"""
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            if not is_request_allowed(current):
                self.skipped.append({"url": current, "reason": "blocked_host"})
                return FetchResult(url=url, error="blocked host")
            if not same_site(current, site_url):
                self.skipped.append({"url": current, "reason": "other_domain"})
                return FetchResult(url=url, final_url=current, error="left the site")
            if self.requests_left <= 0:
                return FetchResult(url=url, error="request budget exhausted")
            if not self.robots_allows(current):
                self.skipped.append({"url": current, "reason": "robots_disallowed"})
                return FetchResult(url=url, error="robots.txt disallows")
            result = self._raw_get(current)
            if result.error:
                return FetchResult(url=url, final_url=current, error=result.error)
            if 300 <= result.status < 400 and result.final_url:
                current = urljoin(current, result.final_url)
                continue
            return FetchResult(url=url, status=result.status, final_url=current, text=result.text)
        return FetchResult(url=url, final_url=current, error="too many redirects")


def slash_and_scheme_variants(url: str) -> list[str]:
    """末尾「/」を付け外しした形と、http→https の形（元のURLは除く）。"""
    parts = urlsplit(url)
    path = parts.path or "/"

    def toggled(p: str) -> str:
        if p == "/" or "." in p.rsplit("/", 1)[-1]:
            return p
        return p.rstrip("/") if p.endswith("/") else p + "/"

    schemes = [parts.scheme] + (["https"] if parts.scheme == "http" else [])
    variants: list[str] = []
    for scheme in schemes:
        for candidate_path in (toggled(path), path):
            variant = urlunsplit((scheme, parts.netloc, candidate_path, parts.query, ""))
            if variant != url and variant not in variants:
                variants.append(variant)
    return variants


def _usable_candidate(url: str, registered_url: str) -> bool:
    if not is_request_allowed(url) or not same_site(url, registered_url):
        return False
    path = urlsplit(url).path.lower()
    if path.endswith(_ASSET_SUFFIXES):
        return False
    if not _path_segments(url):
        return False  # トップページ自体は候補にしない
    return _canonical(url) != _canonical(registered_url)


def _sitemap_locs(xml_text: str) -> tuple[list[str], bool]:
    """(loc一覧, 索引サイトマップか)。"""
    try:
        root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except ET.ParseError:
        return [], False
    is_index = root.tag.lower().endswith("sitemapindex")
    locs = [
        (node.text or "").strip()
        for node in root.iter()
        if node.tag.lower().endswith("loc") and (node.text or "").strip()
    ]
    return locs, is_index


def _links_from_html(html: str, base_url: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for anchor in soup.find_all("a", href=True):
        href = urljoin(base_url, str(anchor.get("href") or "")).split("#", 1)[0]
        text = " ".join(anchor.get_text(" ", strip=True).split())
        title = str(anchor.get("title") or "")
        links.append((href, f"{text} {title}".strip()))
    return links


def discover_candidates(
    provider: dict[str, Any],
    registered_url: str,
    *,
    client: DiscoveryClient,
) -> list[dict[str, Any]]:
    """探し方 a→b→c で候補を集め、200で開けたものだけを返す（点数の高い順）。"""
    keywords = derive_keywords(provider, registered_url)
    verified: list[dict[str, Any]] = []
    seen: set[str] = {registered_url, _canonical(registered_url)}

    def verify(url: str, method: str, reason: str, score: int) -> None:
        # 末尾「/」の違いを試す a だけは、URLをそのままの形で区別する
        key = url if method == "slash_scheme_variant" else _canonical(url)
        if key in seen or client.requests_left <= 0:
            return
        seen.add(key)
        result = client.fetch(url, site_url=registered_url)
        if result.status != 200 or not same_site(result.final_url or url, registered_url):
            return
        if redirected_to_generic_page(url, result.final_url):
            return
        verified.append(
            {
                "url": url,
                "final_url": result.final_url or url,
                "status": result.status,
                "method": method,
                "reason": reason,
                "score": score,
            }
        )

    # a. 末尾「/」の付け外しと https
    for variant in slash_and_scheme_variants(registered_url):
        if is_request_allowed(variant) and same_site(variant, registered_url):
            verify(variant, "slash_scheme_variant", "末尾の/またはhttpsの違い", 10)
    if verified:
        return verified

    pending: list[tuple[int, str, str, str]] = []  # (score, url, method, reason)

    def add_pending(url: str, text: str, method: str, reason: str) -> None:
        if not _usable_candidate(url, registered_url):
            return
        score = score_candidate(url, text, keywords, registered_url)
        if score > 0:
            pending.append((score, url, method, reason))

    origin_parts = urlsplit(registered_url)
    origin = f"https://{origin_parts.netloc}"

    # b. robots.txt の Sitemap 行、なければ /sitemap.xml（索引は1階層だけ）
    if client.requests_left > 0:
        sitemap_urls = client.sitemaps_for(origin + "/") or [origin + "/sitemap.xml"]
        for sitemap_url in sitemap_urls[:2]:
            if client.requests_left <= 1 or not same_site(sitemap_url, registered_url):
                continue
            result = client.fetch(sitemap_url, site_url=registered_url)
            if result.status != 200:
                continue
            locs, is_index = _sitemap_locs(result.text)
            if is_index:
                children = sorted(
                    (loc for loc in locs if same_site(loc, registered_url)),
                    key=lambda loc: -score_candidate(loc, "", keywords, registered_url),
                )
                for child in children[:1]:
                    if client.requests_left <= 1:
                        break
                    child_result = client.fetch(child, site_url=registered_url)
                    if child_result.status == 200:
                        child_locs, _ = _sitemap_locs(child_result.text)
                        for loc in child_locs:
                            add_pending(loc, "", "sitemap", f"サイトマップ {child}")
            else:
                for loc in locs:
                    add_pending(loc, "", "sitemap", f"サイトマップ {sitemap_url}")

    # c. トップページと一覧ページのリンク
    pages = [origin + "/"] + [
        str(url) for url in provider.get("discovery_listing_urls") or [] if url
    ]
    for page_url in pages:
        if client.requests_left <= 1 or not same_site(page_url, registered_url):
            continue
        result = client.fetch(page_url, site_url=registered_url)
        if result.status != 200:
            continue
        for href, text in _links_from_html(result.text, result.final_url or page_url):
            add_pending(href, text, "page_link", f"{page_url} のリンク「{text[:40]}」")

    best: dict[str, tuple[int, str, str, str]] = {}
    for item in pending:
        key = _canonical(item[1])
        if key not in best or item[0] > best[key][0]:
            best[key] = item
    for score, url, method, reason in sorted(best.values(), key=lambda item: -item[0]):
        if client.requests_left <= 0 or len(verified) >= MAX_REPORTED_CANDIDATES:
            break
        verify(url, method, reason, score)
    verified.sort(key=lambda item: -item["score"])
    return verified


def discovery_disabled() -> bool:
    return os.environ.get("URL_DISCOVERY_DISABLED", "").strip() in {"1", "true", "yes"}


def check_registered_url(
    provider: dict[str, Any],
    registered_url: str,
    *,
    status: int | str | None,
    final_url: str = "",
    client: DiscoveryClient | None = None,
) -> dict[str, Any] | None:
    """1つの登録URLを判定し、きっかけがあれば候補を探す。不要なら None。"""
    if is_excluded_provider(provider) or not is_request_allowed(registered_url):
        return None
    trigger = needs_discovery(registered_url, status, final_url)
    if not trigger:
        return None
    entry: dict[str, Any] = {
        "registered_url": registered_url,
        "status": status,
        "final_url": final_url,
        "trigger": trigger,
        "candidates": [],
        "requests_used": 0,
    }
    if discovery_disabled():
        entry["note"] = "URL_DISCOVERY_DISABLED"
        return entry
    client = client or DiscoveryClient()
    before = len(client.requested_urls)
    try:
        entry["candidates"] = discover_candidates(provider, registered_url, client=client)
    except DiscoveryBlocked as exc:  # pragma: no cover - 防御（ここへは来ない設計）
        entry["note"] = f"blocked: {exc}"
    entry["requests_used"] = len(client.requested_urls) - before
    entry["suspected"] = is_suspected(entry)
    return entry


def is_suspected(entry: dict[str, Any]) -> bool:
    """「URL変更の可能性」とするか。

    403 はサイト全体が自動取得を断っているだけのことが多い（例: robots.txt も403）。
    403 は移転先の候補が見つかったときだけ可能性ありとし、それ以外は記録だけ残す。
    """
    if entry.get("trigger") == "http_403":
        return bool(entry.get("candidates"))
    return bool(entry.get("trigger"))


def summarize_url_checks(checks: list[dict[str, Any]]) -> dict[str, Any]:
    """取得結果・状態ファイルへ足す項目。きっかけが無ければ空の dict。"""
    checks = [check for check in checks if check]
    if not checks:
        return {}
    suspected = [check for check in checks if check.get("suspected", is_suspected(check))]
    if not suspected:
        # 403 で候補なし（サイトが自動取得を断っている等）。記録だけ残し、表示はしない。
        return {"url_change_suspected": False, "url_checks": checks}
    candidates = []
    for check in suspected:
        for candidate in check.get("candidates") or []:
            candidates.append({**candidate, "registered_url": check["registered_url"]})
    first = suspected[0]
    status = first.get("status")
    return {
        "url_change_suspected": True,
        "url_change_label": URL_CHANGE_LABEL,
        "url_check_status": status if status not in (None, "") else first.get("trigger", ""),
        "url_candidates": candidates[:MAX_REPORTED_CANDIDATES],
        "url_checks": checks,
    }


def check_sources(
    provider: dict[str, Any],
    sources: list[dict[str, Any]],
    results: list[dict[str, Any]],
    *,
    client: DiscoveryClient | None = None,
) -> dict[str, Any]:
    """official_deal_monitor の取得結果（http_status・final_url つき）から判定する。"""
    if is_excluded_provider(provider):
        return {}
    client_holder: list[DiscoveryClient] = [client] if client else []
    checks = []
    for source, result in zip(sources, results):
        if result.get("ok") and not result.get("final_url"):
            continue
        url = str(source.get("url") or "")
        status = result.get("http_status")
        final_url = str(result.get("final_url") or "")
        if not needs_discovery(url, status, final_url):
            continue
        if not client_holder:
            client_holder.append(DiscoveryClient())  # 1社で回数上限を共有する
        checks.append(
            check_registered_url(
                provider, url, status=status, final_url=final_url, client=client_holder[0]
            )
        )
    return summarize_url_checks(checks)
