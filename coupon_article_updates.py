"""公開記事の保存履歴でクーポン条件が変わった日を探す。本文は保存しない。"""
from __future__ import annotations

import html
import json
import re
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

SITES = {"welltrip": "ウェルトリップ", "yakushimafan": "屋久島ファン"}
JST = timezone(timedelta(hours=9))
WINDOW = 8
SCHEMA = 2
META = "id,slug,title.raw,status,password,link,date_gmt,modified_gmt"
FAILURES = (requests.RequestException, KeyError, ValueError, OSError)
COUPON = re.compile(r"クーポン|coupon", re.I)
REVIEW = re.compile(r"^(?:\d{4}[-/]\d{2}[-/]\d{2}\s*)?(?:【?(?:追加分(?:・訂正)?|訂正分|差し替え用|更新用下書き)】?\s*)?")
REWRITE = re.compile(r"-(?:rewrite|coupon-review)(?:-[\w-]+)?$")
AMOUNT = re.compile(r"(?:最大|一律)?\d+(?:\.\d+)?(?:万?円(?:引(?:き)?|割引|off|オフ|以上|クーポン)|%(?:off|オフ|割引|引き|還元)|倍(?:ポイント|還元))|(?:ポイント|割引率|還元率)\s*[:：]?\s*\d+(?:\.\d+)?[%倍]", re.I)
DATES = re.compile(r"(?<![0-9])(?:(20\d{2})[./年-])?(1[0-2]|0?[1-9])[./月-](3[01]|[12]\d|0?[1-9])日?(?![0-9])")
STOCK = re.compile(r"(?:残り|残数|残枚数|先着)\s*[:：]?\s*(\d+)枚|(\d+)枚(?:が|の)?残")
CODE = re.compile(r"(?:クーポンコード|割引コード|合言葉|パスワード)\s*[:：は「【]?\s*([A-Za-z0-9][A-Za-z0-9_-]{3,39})(?![A-Za-z0-9_-])", re.I)
CODE_ONLY = re.compile(r"(?=[A-Z0-9_-]*[A-Z])(?=[A-Z0-9_-]*[0-9])[A-Z0-9_-]{6,40}")
DATE_CONTEXT = re.compile(r"(?:予約|出発|宿泊|配布|対象|利用|有効)(?:対象)?(?:期間|期限|締切|日程)|(?:受付|予約|出発|宿泊|配布|利用)[:：]|まで(?:に予約|利用|有効)|締め切り")
NOT_FACT = re.compile(r"最終更新|記事更新|更新日|確認日時|最終確認")


def jst_time(value):
    stamp = datetime.fromisoformat(value.removesuffix("Z")).replace(tzinfo=timezone.utc)
    return stamp.astimezone(JST).isoformat(timespec="seconds")


def coupon_facts(raw):
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup.select("script,style,template,noscript,[hidden],del,s"):
        tag.decompose()
    facts = defaultdict(set)
    for tag in soup.select("p,li,tr,dt,dd,h2,h3,h4,.yf-coupon-g__offer,.yf-coupon-g__valid,.yf-coupon-g__code-row,code"):
        # Nested list entries are evaluated independently.
        if tag.name == "li" and tag.find(["ul", "ol"]):
            continue
        txt = unicodedata.normalize("NFKC", tag.get_text(" ", strip=True))
        txt = re.sub(r"(?<=\d),(?=\d)", "", txt)
        for m in AMOUNT.finditer(txt):
            facts["割引・利用金額"].add(m[0].lower().removeprefix("一律").replace("引き", "引"))
        for clause in re.split(r"[。;；]", txt):
            if DATE_CONTEXT.search(clause) and not NOT_FACT.search(clause):
                for year, month, day in DATES.findall(clause):
                    facts["予約・利用期間"].add(f"{year}/{int(month)}/{int(day)}")
        for m in STOCK.finditer(txt):
            facts["配布枚数"].add(m.group(1) or m.group(2))
        facts["クーポンコード"].update(CODE.findall(txt))
        if tag.name == "code" or "code-row" in " ".join(tag.get("class", [])):
            facts["クーポンコード"].update(t for t in txt.split() if CODE_ONLY.fullmatch(t))
    return {k: vals for k, vals in facts.items() if vals}


def revision_change(post, revisions):
    current = coupon_facts(post["content"]["raw"])
    published = post["date_gmt"]
    history = sorted((r for r in revisions if r.get("parent") == post["id"] and
                      "autosave" not in r.get("slug", "") and r.get("date_gmt") and
                      published <= r["date_gmt"] <= post["modified_gmt"]),
                     key=lambda r: (r["date_gmt"], r["id"]), reverse=True)
    if len(history) < 2:
        return {"result": "履歴不足"}
    if coupon_facts(history[0]["content"]["raw"]) != current:
        return {"result": "公開版と履歴が不一致"}
    for newer, older in zip(history, history[1:]):
        previous = coupon_facts(older["content"]["raw"])
        changed = sorted(k for k in current.keys() | previous.keys() if current.get(k) != previous.get(k))
        if changed:
            return dict(result="変更を確認", changed_at=jst_time(newer["date_gmt"]), fields=changed)
        current = previous
    return {"result": "調べた履歴に条件差なし"}


def list_posts(get, status):
    found = []
    page = 1
    while True:
        resp = get("", dict(status=status, page=page, per_page=100, orderby="id", order="asc", _fields=META))
        found.extend(resp.json())
        if page >= int(resp.headers["X-WP-TotalPages"]):
            return found
        page += 1


def name_key(title):
    return "".join(unicodedata.normalize("NFKC", html.unescape(title)).split())


def draft_dates(articles, drafts, site):
    by_title = defaultdict(list)
    by_slug = {p["slug"]: p["id"] for p in articles}
    for p in articles:
        by_title[name_key(p["title"]["raw"])].append(p["id"])
    ids = set(by_slug.values())
    latest = {}
    for draft in drafts:
        if draft["status"] != "draft" or draft.get("password"):
            continue
        title = draft["title"]["raw"]
        slug = draft["slug"]
        target = by_slug.get(REWRITE.sub("", slug)) if REWRITE.search(slug) else None
        target_id = re.search(re.escape(SITES[site]) + r"\s*[#＃]?\s*(\d{1,9})用", title)
        if target_id and int(target_id[1]) in ids:
            target = int(target_id[1])
        if not target:
            cleaned = REVIEW.sub("", title)
            matches = by_title.get(name_key(cleaned), [])
            if len(matches) == 1:
                target = matches[0]
        if target:
            stamp = jst_time(draft["modified_gmt"])
            latest[target] = max(stamp, latest.get(target, ""))
    return latest


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def job_records(run_dir):
    """完了した朝夕の回の再取得結果だけを使用する。公開差し替えと下書きを分ける。"""
    evidence = defaultdict(dict)
    runs = []
    for path in run_dir.glob("????????-*"):
        match = re.fullmatch(r"(\d{8})-(morning|evening)", path.name)
        if match and (path / "stage3.ok").is_file() and (path / "run.done").is_file():
            runs.append((match[1], match[2] == "evening", path))
    for ymd, evening, path in sorted(runs):
        checks = path / "check"
        final = checks / "final-stats.json"
        pre = checks / "pre-stats.json"
        if not final.is_file() or not pre.is_file():
            continue
        if not re.match(r"^0\s", (path / "run.done").read_text()):
            continue
        day = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"
        slot = day + (" 夕" if evening else " 朝")
        before, after = read_json(pre), read_json(final)
        if not isinstance(before, dict) or not isinstance(after, dict):
            continue
        mappings = checks / "yaku-verify.json"
        drafts = read_json(mappings) if mappings.is_file() else {}
        for key, check in after.items():
            m = re.fullmatch(r"(welltrip|yakushimafan)-(\d+)", key)
            if not m or not isinstance(check, dict) or check.get("same_as_stage2") is not True:
                continue
            pid = int(m[2])
            first = before.get(key, {})
            if not first.get("sha256") or not check.get("sha256"):
                continue
            changed = first["sha256"] != check["sha256"]
            if check.get("status") == "draft" and m[1] == "yakushimafan":
                mapping = drafts.get(str(pid), {}) if isinstance(drafts, dict) else {}
                if mapping.get("ok") is not True or not str(mapping.get("orig", "")).isdigit():
                    continue
                pid = int(mapping["orig"])
                if changed:
                    evidence[m[1], pid]["draft_run"] = slot
                continue
            if check.get("status") != "publish" or first.get("status") != "publish":
                continue
            entry = evidence[m[1], pid]
            result = "公開本文の変更なし"
            if changed:
                result = "本文差分あり・作業による更新は未確認"
                report = checks / f"scope-{pid}-final/report.json"
                if report.is_file():
                    proof = read_json(report)
                    if (proof.get("exit_code") == 0 and proof.get("candidate_sha256") == check["sha256"]
                            and proof.get("source_sha256") == first["sha256"]):
                        result = "公開本文を更新"
                        entry["published_run"] = slot
            entry.update(last_run=slot, run_result=result)
    return evidence


def collect_site(site, cfg, auth, known_urls, previous, now, jobs):
    base = cfg["WP_URL"].rstrip("/") + "/wp-json/wp/v2/posts"
    cache = {r["post_id"]: r for r in previous.get("articles", [])}
    with requests.Session() as session:
        session.auth = auth
        def get(path, params):
            resp = session.get(base + path, params={"context": "edit", **params}, timeout=25)
            resp.raise_for_status()
            return resp

        posts = [p for p in list_posts(get, "publish") if p["status"] == "publish" and not p.get("password")
                 and ((site, p["id"]) in jobs or p["link"] in known_urls or COUPON.search(p["slug"]) or COUPON.search(p["title"]["raw"]))
                 and urlsplit(p["link"]).hostname == urlsplit(base).hostname]
        drafts_ok = True
        try:
            drafts = draft_dates(posts, list_posts(get, "draft"), site)
        except FAILURES:
            drafts_ok, drafts = False, {}
        records = []
        for p in posts:
            pid = p["id"]
            modified = jst_time(p["modified_gmt"])
            cached = cache.get(pid, {})
            if modified == cached.get("modified") and cached.get("history", {}).get("result") not in {None, "履歴を取得できず"} and cached["history"].get("latest_check") != "履歴を取得できず":
                history = cached["history"]
            else:
                try:
                    live = get("/" + str(pid), dict(_fields="id,status,date_gmt,modified_gmt,content.raw,password")).json()
                    if live["status"] != "publish" or live.get("password"):
                        continue
                    modified = jst_time(live["modified_gmt"])
                    revisions = get(f"/{pid}/revisions", dict(per_page=WINDOW, orderby="date", order="desc",
                                    _fields="id,parent,date_gmt,slug,content.raw")).json()
                    history = revision_change(live, revisions)
                except FAILURES:
                    history = {"result": "履歴を取得できず"}
                earlier = cached.get("history", {})
                if not history.get("changed_at") and earlier.get("changed_at") and earlier["changed_at"] <= modified:
                    history = {**earlier, "result": "以前に確認した変更", "latest_check": history["result"]}
            row = dict(post_id=pid, title=html.unescape(p["title"]["raw"]), url=p["link"],
                       modified=modified, history=history)
            if (site, pid) in jobs:
                row["job"] = jobs[site, pid]
            if pid in drafts:
                row["draft_at"] = drafts[pid]
            records.append(row)
    return dict(site=site, name=SITES[site], checked_at=now, status="取得済み",
                drafts_checked=drafts_ok, articles=records)


def collect_updates(configure, make_auth, known_urls, previous, run_dir):
    now = datetime.now(JST).isoformat(timespec="seconds")
    cache = {r["site"]: r for r in previous.get("sites", [])} if previous.get("version") == SCHEMA else {}
    jobs = job_records(run_dir)
    def collect(site):
        prior = cache.get(site, {})
        try:
            cfg = configure(site)
            return collect_site(site, cfg, make_auth(cfg), known_urls, prior, now, jobs)
        except FAILURES:
            return {**prior, "site": site, "name": SITES[site], "status": "再取得できず",
                    "articles": prior.get("articles", []), "attempted_at": now}
    with ThreadPoolExecutor(max_workers=2) as pool:
        return {"version": SCHEMA, "sites": list(pool.map(collect, SITES))}
