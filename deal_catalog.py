"""案件単位で取得データ・AIの発見・編集記録をまとめる。ネットワークは使わない。"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from urllib.parse import urlsplit

KINDS = {"coupon": "クーポン", "sale": "セール", "campaign": "キャンペーン",
         "points": "ポイント", "member_benefit": "会員特典", "other": "その他"}


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def normalize(text):
    return "".join(unicodedata.normalize("NFKC", text or "").split()).casefold()


def web_url(value):
    try:
        parts = urlsplit(value or "")
        if parts.scheme in {"http", "https"} and parts.hostname and not parts.username and not parts.password:
            return value.strip()
    except ValueError:
        pass
    return ""


def group_key(pid, title, url):
    key = json.dumps([normalize(title), url], ensure_ascii=False)
    return f"{pid}/lead-{hashlib.sha256(key.encode()).hexdigest()[:20]}"


def blank(pid, title, url):
    return dict(provider=pid, title=title, url=url, kind="その他", date="", stock="要確認",
                discount="", period="", sources=[], x=[], articles=[])


def unique_add(items, value):
    if value not in items:
        items.append(value)


def build_catalog(root, providers):
    deals = {}
    by_title = defaultdict(list)
    by_url = defaultdict(list)
    current_codes = {}
    lead_aliases = {}
    labels = {p["id"]: p["label"] for p in providers}

    def insert(key, row):
        row["key"] = key
        deals[key] = row
        by_title[row["provider"], normalize(row["title"])].append(key)
        if row["url"]:
            by_url[row["provider"], row["url"]].append(key)
        return row

    def find_or_create(pid, title, url):
        # 一覧URLの共有だけでは同じ特典とみなさない。
        exact = [k for k in by_title[pid, normalize(title)] if deals[k]["url"] == url]
        if len(exact) == 1:
            return deals[exact[0]]
        links = by_url[pid, url]
        if (pid == "jtb" and re.fullmatch(r"https://www\.jtb\.co\.jp/myjtb/campaign/coupon/detail/[\w-]+/page\.asp", url)
                and len(links) == 1):
            return deals[links[0]]
        key = group_key(pid, title, url)
        return deals.get(key) or insert(key, blank(pid, title, url))

    for p in providers:
        for r in p["rows"]:
            title, url = r["タイトル"], web_url(r["詳細URL"])
            key = f"{p['id']}/{r['ID']}" if r["ID"] else group_key(p["id"], title, url)
            current_codes[key] = {normalize(code) for code in r.get("クーポンコード", "").split(" / ")}
            item = blank(p["id"], title, url)
            item.update(kind=r["種別"], discount=r["割引額"], stock=r["配布状況"],
                        period=r["予約期間"] or r["出発/宿泊期間"], date=p["data_date"])
            provisional = (r["データ元"] in {"manual_master", "article_snapshot", "dry_run"}
                           or p.get("legacy_used") or p.get("latest_file", "").startswith("dry_run"))
            item["sources"] = ["参考資料" if provisional else "監視データ"]
            item["scope"] = " ／ ".join(dict.fromkeys(filter(None, [r.get("カテゴリ"), r.get("表示地域")])))
            if r["データ元"] == "official_codex_audit":
                item["sources"].append("AI収集")
            insert(key, item)

    # 失敗・候補ゼロの新しい巡回でも、以前の発見は残す。
    reports = list((root / "discovery_hints").glob("*/hints_*.json"))
    reports.extend((root / "ai_intake").glob("*/*.json"))
    for path in sorted(reports):
        pid = path.parent.name
        if pid not in labels:
            if path.parent.parent.name == "ai_intake":
                raise ValueError(f"未登録の会社: {path}")
            continue
        report = read(path)
        for lead in report.get("candidates", []):
            title = (lead.get("title") or "").strip()
            if not title:
                continue
            url = web_url(lead.get("official_url")) or web_url(lead.get("found_url"))
            row = find_or_create(pid, title, url)
            lead_aliases[group_key(pid, title, url)] = row["key"]
            observed = (lead.get("observed_date") or report["generated_at"])[:10]
            # Keep the newest observation, regardless of same-day filename variants.
            if observed >= row.get("ai_date", ""):
                row.update(ai_date=observed, ai_evidence=lead.get("evidence", ""),
                           ai_url=web_url(lead.get("found_url")) or url)
                if not {"監視データ", "参考資料"}.intersection(row["sources"]):
                    row.update(date=observed, kind=KINDS.get(lead.get("kind"), "その他"),
                               period=lead.get("period", ""), discount=lead.get("discount", ""))
            unique_add(row["sources"], "AI収集")

    x_source = root / "editorial_data" / "x.json"
    for rec in read(x_source) if x_source.exists() else []:
        pid = rec["provider"]
        if pid not in labels:
            continue
        row = find_or_create(pid, rec["title"], web_url(rec["url"]))
        unique_add(row["sources"], "X取材")
        if row["sources"] == ["X取材"]:
            row.update(date=max(row["date"], rec["date"]), kind=rec["kind"])
        unique_add(row["x"], dict(stage=rec["stage"], date=rec["date"], site=rec["site"], url=rec["post_url"]))

    # 旧形式は生成当日の取得記録で1件に特定できた場合だけ結び付ける。
    paths = {p['id']: root / p['data_dir'] for p in read(root / 'config/provider_registry.json')['providers']
             if p.get('data_dir')}
    for path in sorted((root / "tweets_output").glob("x_threads_????-??-??.json")):
        payload = read(path)
        day = payload["date"]
        old_titles = {}
        for site in payload["sites"]:
            for thread in site["threads"]:
                pid = thread["provider_id"]
                coupon_id = thread.get("coupon_id")
                if not coupon_id and pid in paths:
                    if pid not in old_titles:
                        snapshot = paths[pid] / f"coupons_{day}.json"
                        titles = defaultdict(set)
                        for raw in read(snapshot) if snapshot.exists() else []:
                            titles[(raw.get("title") or raw.get("name") or "")[:60]].add(raw.get("id") or raw.get("coupon_id"))
                        old_titles[pid] = titles
                    hits = old_titles[pid][thread["title"]]
                    if len(hits) == 1:
                        coupon_id = next(iter(hits))
                row = deals.get(f"{pid}/{coupon_id}") if coupon_id else None
                if row:
                    unique_add(row["x"], dict(stage="draft", date=day, site=site["display_name"], url=""))

    # 他の自社Xアカウントも、案件を指定した公開投稿の記録で紐づけられる。
    manual = root / "editorial_data" / "publications.json"
    for receipt in read(manual) if manual.exists() else []:
        url = web_url(receipt["url"])
        if not re.fullmatch(r"https://(?:x|twitter)\.com/\w+/status/\d+", url):
            raise ValueError("Xの公開投稿URLが必要です")
        row = deals.get(lead_aliases.get(receipt["key"], receipt["key"]))
        if row:
            unique_add(row["x"], dict(stage="posted", url=url, site=receipt["site"], date=receipt["date"]))

    snapshot = root / "editorial_data" / "articles.json"
    checks = read(snapshot) if snapshot.exists() else []
    for check in checks:
        for match in check["matches"]:
            key = lead_aliases.get(match["key"], match["key"])
            if match["basis"] == "コード一致" and match.get("code") not in current_codes.get(key, set()):
                continue
            row = deals.get(key)
            if row and match["title"] == row["title"]:
                row["articles"].append(dict(site=check["site"], url=check["url"], date=check["date"],
                                            basis=match["basis"]))

    for row in deals.values():
        # One latest receipt per site and stage. A copy of a draft never proves publication.
        latest = {}
        for hit in sorted(row["x"], key=lambda h: h["date"]):
            latest[hit["site"], hit["stage"]] = hit
        row["x"] = list(latest.values())
    return {"rows": list(deals.values()), "checks": [{k: c[k] for k in ("provider", "site", "url", "date", "status")} for c in checks]}
