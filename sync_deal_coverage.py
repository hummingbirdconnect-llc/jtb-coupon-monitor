"""手元のX実績と公開記事を読み、案件の照合に必要な項目だけを保存する。"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

from deal_catalog import build_catalog, normalize, read, web_url
from generate_dashboard import JST, ROOT, build_provider_payload, load_registry


def save(path, data):
    path.parent.mkdir(exist_ok=True)
    with NamedTemporaryFile("w", dir=path.parent, encoding="utf-8", delete=False) as out:
        json.dump(data, out, ensure_ascii=False, indent=1)
        out.write("\n")
    Path(out.name).replace(path)


def x_receipts(workspace, providers):
    source = workspace / "docs/operations/welltrip-x-loop/state.sqlite3"
    aliases = {"rakuten": "rakuten_travel", "nta": "nippon_travel"}
    domains = defaultdict(set)
    pids = {p["id"] for p in providers}
    for p in providers:
        for r in p["rows"]:
            host = urlsplit(web_url(r["詳細URL"])).hostname
            if host:
                domains[host].add(p["id"])
    rows = []
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as db:
        db.execute("BEGIN")
        # A public post must belong to the saved version of this draft on X.
        published = {}
        sql = """SELECT m.postset_id, m.payload FROM x_metrics m
                 JOIN typefully_saves t ON m.postset_id=t.postset_id AND m.draft_id=t.draft_id
                 JOIN postsets s ON t.postset_id=s.id AND t.body_sha256=s.body_sha256
                 WHERE s.post_type='news' ORDER BY m.post_id"""
        for postset_id, payload in db.execute(sql):
            m = json.loads(payload)
            url = m.get("url", "")
            if m.get("platform") == "x" and re.fullmatch(r"https://x\.com/\w+/status/" + re.escape(str(m["post_id"])), url):
                posted_day = datetime.fromisoformat(m["created_at"].replace("Z", "+00:00")).astimezone(JST).date()
                published.setdefault(postset_id, (url, posted_day.isoformat()))
        for postset_id, day, payload in db.execute("SELECT id,day,payload FROM postsets WHERE post_type='news' ORDER BY day,id"):
            post = json.loads(payload)
            tree = post.get("coupon_tree") or {}
            items = tree.get("coupons", [])
            public_url, pub_day = published.get(postset_id, ("", ""))
            if not items and public_url and (post.get("source") or {}).get("kind") == "official_page":
                items = [{"name": post["theme"], "url": post["source"]["url"]}]
            for item in items:
                url, title = web_url(item.get("url")), (item.get("name") or "").strip()
                if not url or not title:
                    continue
                owners = domains[urlsplit(url).hostname]
                company = tree.get("company")
                pid = aliases.get(company, company)
                if pid not in pids:
                    if len(owners) != 1:
                        continue
                    pid = next(iter(owners))
                if owners and pid not in owners:
                    continue
                kind = "クーポン" if tree.get("coupons") or "クーポン" in title else "その他"
                rows.append(dict(provider=pid, title=title, url=url, kind=kind, site="ウェルトリップ",
                                 stage="posted" if public_url else "draft", date=pub_day or day, post_url=public_url))
    return rows


def article_targets():
    targets = {}
    for site, config in read(ROOT / "config/x_thread_sites.json")["sites"].items():
        for pid, url in config["article_map"].items():
            targets[site, url] = pid
    for site, config in read(ROOT / "config/wp_sites.json")["sites"].items():
        for page in config["pages"]:
            targets[site, page["url"]] = page["ota"]
    # 編集先の設定は保持し、掲載の照合には移転先の公開記事を使う。
    for alias, canonical in read(ROOT / "config/article_coverage_redirects.json").items():
        for site, url in list(targets):
            if url == alias:
                pid = targets.pop((site, url))
                targets[canonical["site"], canonical["url"]] = pid
    return targets


def article_matches(rows, codes, html):
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("script,style,template,noscript,[hidden]"):
        node.decompose()
    # Avoid matching fragments on either side of separate paragraphs or table cells.
    blocks = [normalize(node.get_text()) for node in soup.select("p,li,td,th,h2,h3,h4,dt,dd,figcaption")
              if not node.select_one("p,li,td,th,h2,h3,h4,dt,dd,figcaption")]
    text = "\n".join(blocks)
    code_text = unicodedata.normalize("NFKC", soup.get_text(" ")).casefold()
    title_counts = Counter(normalize(r["title"]) for r in rows)
    code_counts = Counter(c for r in rows for c in codes.get(r["key"], set()))
    found = []
    for r in rows:
        title = normalize(r["title"])
        basis = ""
        if len(title) >= 12 and title_counts[title] == 1 and title in text:
            basis = "名称一致"
        match_code = None
        for code in sorted(codes.get(r["key"], set())):
            if code_counts[code] == 1 and re.search(r"(?<![a-z0-9_-])" + re.escape(code) + r"(?![a-z0-9_-])", code_text):
                basis = "コード一致"
                match_code = code
                break
        if basis:
            match = dict(key=r["key"], title=r["title"], basis=basis)
            if match_code:
                match["code"] = match_code
            found.append(match)
    return found


def sync_articles(workspace, providers):
    sys.path.insert(0, str(workspace / "tools"))
    from wp_env import load_wp_config, build_wp_auth
    names = read(ROOT / "config/x_thread_sites.json")["sites"]
    by_provider = defaultdict(list)
    for row in build_catalog(ROOT, providers)["rows"]:
        by_provider[row["provider"]].append(row)
    codes = {}
    for p in providers:
        for r in p["rows"]:
            codes[p["id"] + "/" + r["ID"]] = {normalize(c) for c in r["クーポンコード"].split(" / ")
                                               if re.fullmatch(r"(?i)[a-z0-9_-]{5,}", c) and re.search("[a-zA-Z]", c)}
    results = []
    day = datetime.now(JST).date().isoformat()
    for (site, url), pid in sorted(article_targets().items()):
        if pid not in by_provider:
            continue
        rec = dict(site=names[site]["display_name"], provider=pid, url=url, date=day, status="未取得", matches=[])
        try:
            cfg = load_wp_config(site)
            if urlsplit(cfg["WP_URL"]).hostname != urlsplit(url).hostname:
                raise ValueError("対象ホスト不一致")
            slug = urlsplit(url).path.strip("/").split("/")[-1]
            resp = requests.get(cfg["WP_URL"] + "/wp-json/wp/v2/posts", auth=build_wp_auth(cfg),
                                params=dict(context="edit", status="publish", slug=slug,
                                            _fields="id,slug,status,link,content,password"), timeout=20)
            resp.raise_for_status()
            for post in resp.json():
                if (post["status"] == "publish" and not post.get("password") and post["slug"] == slug
                        and post["link"].rstrip("/") == url.rstrip("/")):
                    rec["matches"] = article_matches(by_provider[pid], codes, post["content"]["rendered"])
                    rec.update(status="照合済み", post_id=post["id"])
                    break
        except (OSError, requests.RequestException, ValueError, KeyError):
            pass
        results.append(rec)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    providers = [build_provider_payload(p) for p in load_registry()]
    rows = x_receipts(workspace, providers)
    dest = ROOT / "editorial_data"
    save(dest / "x.json", rows)
    checks = sync_articles(workspace, providers)
    save(dest / "articles.json", checks)
    checked = sum(c["status"] == "照合済み" for c in checks)
    print(f"Xの取材・投稿記録 {len(rows)}件。公開記事 {checked}/{len(checks)}本の本文を照合。")
    if len(checks) != checked:
        print("未取得の記事:", ", ".join(c["url"] for c in checks if c["status"] == "未取得"))


if __name__ == "__main__":
    main()
