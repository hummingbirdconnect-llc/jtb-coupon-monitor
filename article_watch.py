"""Public-only article watch projection. No network, credentials, WP writes or scheduling.

Input worker feed must already be approved for public export. Never give this
module raw analytics, private notes, drafts or proposed replacement text.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

SITES = {'welltrip': ('ウェルトリップ', 'travel.hb-connect.co.jp'),
         'yakushimafan': ('屋久島ファン', 'yakushima.fun')}
FIELDS = {'amount': '割引・利用金額', 'period': '予約・利用期間', 'stock': '配布枚数',
          'code': 'クーポンコード', 'link': '公式リンク', 'eligibility': '利用条件'}
STATUSES = {'match': '公開記事と公式を照合済み', 'difference': '公開情報に差異あり',
            'resolved': '公開記事で解消を確認'}
OFFICIAL = {'his-j.com', 'jtb.co.jp', 'knt.co.jp', 'jal.co.jp', 'jalpack.co.jp',
            'jalan.net', 'rakuten.co.jp', 'ikyu.com', 'yahoo.co.jp', 'asoview.com',
            'tobutoptours.jp', 'booking.com', 'rurubu.travel', 'klook.com',
            'nta.co.jp', 'jrtours.co.jp', 'jtrip.co.jp', 'skypaktours.co.jp',
            'club-t.com', 'surpricenow.com', 'expedia.co.jp', 'hotels.com'}

def stamp(value):
    if not isinstance(value, str): return None
    try:
        d = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return d.astimezone(timezone.utc).isoformat(timespec='seconds') if d.tzinfo else None
    except ValueError: return None

def article_url(value, host):
    if not isinstance(value, str): return None
    u = urlsplit(value)
    if u.scheme != 'https' or u.netloc != host or u.query or u.fragment: return None
    if any(x in u.path.lower() for x in ('wp-admin', 'wp-json', 'login', 'preview')): return None
    return value

def official_origin(value):
    if not isinstance(value, str): return None
    u = urlsplit(value)
    if u.scheme != 'https' or u.username or u.password or u.port not in (None, 443): return None
    h = u.hostname or ''
    if not any(h == d or h.endswith('.' + d) for d in OFFICIAL): return None
    # Deliberately never export paths, query strings, fragments or redirect URLs.
    return 'https://' + h + '/'

def build_article_watch(scope, feed=None, now=None):
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")
    records = {}
    for site in scope.get('sites', []):
        sid = site.get('site')
        if sid not in SITES: continue
        for a in site.get('articles', []):
            pid = a.get('post_id')
            if type(pid) is not int or pid <= 0: continue
            url = article_url(a.get('url'), SITES[sid][1])
            if not url: continue
            key = (sid, pid)
            if key in records: continue
            # Title/URL are from the existing public scope, never worker text.
            records[key] = dict(site=sid, site_name=SITES[sid][0], post_id=pid,
                title=str(a.get('title', ''))[:300], url=url,
                modified=stamp(a.get('modified')), scope_checked_at=stamp(site.get('checked_at')),
                scope_current=site.get('status') == '取得済み', issues=[])
            h = a.get('history', {})
            records[key]['public_change_at'] = stamp(h.get('changed_at'))
            records[key]['public_change_fields'] = [v for v in h.get('fields', []) if v in FIELDS.values()]
            published = a.get('job', {}).get('published_run', '')
            import re
            if isinstance(published, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2} (朝|夕|臨時)', published):
                records[key]['published_run'] = published
    accepted = {}
    if isinstance(feed, dict) and feed.get('schema_version') == 1 and feed.get('public_export') is True:
        producer = feed.get('producer')
        findings = feed.get('findings')
        if not isinstance(findings, list) or producer not in (None, 'his-visible-code-v1'):
            findings = []
        for f in findings:
            if not isinstance(f, dict): continue
            if not isinstance(f.get('site'), str) or type(f.get('post_id')) is not int:
                continue
            key = (f['site'], f['post_id'])
            if key not in records or f.get('article_verified') is not True or f.get('official_verified') is not True: continue
            if not all(isinstance(f.get(k), str) for k in ('field', 'status')): continue
            if f['field'] not in FIELDS or f['status'] not in STATUSES: continue
            offer = f.get('offer_id'); at = stamp(f.get('checked_at'))
            try: origin = official_origin(f.get('official_url'))
            except ValueError: origin = None
            if not isinstance(offer, str) or not offer.strip() or not at or not origin: continue
            expires = None
            if producer == 'his-visible-code-v1':
                expires = stamp(f.get('expires_at'))
                if (f['field'] != 'code' or f['status'] != 'match' or not expires or
                        not at <= now < expires or origin != 'https://www.his-j.com/' or
                        not records[key]['scope_current'] or not records[key]['modified'] or
                        stamp(f.get('article_modified')) != records[key]['modified']):
                    continue
            # Never copy a private offer label/ID to the public dashboard.
            oid = hashlib.sha256(offer.encode()).hexdigest()[:16]
            k = (*key, oid, f['field'])
            entry = dict(offer_key=oid, field=f['field'], field_label=FIELDS[f['field']],
                         status=f['status'], status_label=STATUSES[f['status']],
                         checked_at=at, official_origin=origin)
            if expires:
                entry.update(expires_at=expires, status_label='公開記事のコード表記が公式と一致')
            if k not in accepted or at > accepted[k]['checked_at']: accepted[k] = entry
    for (sid, pid, _, _), issue in sorted(accepted.items()): records[sid, pid]['issues'].append(issue)
    return {'schema_version': 1, 'articles': list(records.values()),
            'verification_available': bool(accepted)}

def load_article_watch(root):
    root = Path(root)
    def read(name, default):
        try: return json.loads((root / 'editorial_data' / name).read_text())
        except (OSError, ValueError): return default
    return build_article_watch(read('coupon-articles.json', {'sites': []}),
                               read('article-watch-public.json', None))
