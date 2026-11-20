import json
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from article_watch import build_article_watch
from his_public_code_proof import TARGETS, build_proof, stamp


class HisProofTest(TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = datetime.fromisoformat('2026-11-20T16:00:00+09:00')
        self.meta = dict(provider_id='his', status='success', freshness_status='fresh',
                         needs_review=False, ai_used=False, regional_coverage_status='complete',
                         region_count=11, successful_region_count=11, coupon_count=1,
                         data_date='2026-11-20', latest_file='coupons_2026-11-20.json',
                         official_fetched_at='2026-11-20T15:50:00+09:00')
        self.row = dict(id='private-source-id-not-exported',
                        coupon_codes=[{'code':'A12345'}], stock_status='配布中',
                        source_url='https://www.his-j.com/campaign/only-public-path/',
                        official_checked_at='2026-11-20T15:46:00+09:00',
                        source_type='official_visible_dom', official_visibility='visible',
                        confidence='公式画面表示確認済み')
        self.site = 'welltrip'
        pid, url = TARGETS[self.site]
        modified = '2026-11-20T08:00:00+09:00'
        self.article = dict(post_id=pid, url=url, modified=modified)
        self.scope = {'sites':[dict(site=self.site, status='取得済み', checked_at='2026-11-20T15:52:00+09:00',
                                    articles=[self.article])]}
        self.post = dict(id=pid, link=url, modified_at=stamp(modified), status='publish', password='',
                         observed_at='2026-11-20T15:56:00+09:00',
                         rendered='<li><a class="afb-section-coupon-code" href="https://t.afi-b.com/tracking">A12345</a></li>')
        self.observations = {(self.site,url):self.post}

    def run_proof(self, rows=None):
        rows = [self.row] if rows is None else rows
        for name, data in [('provider_check_data/his/latest.json', self.meta),
                           ('his_coupon_data/coupons_2026-11-20.json', rows)]:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data))
        return build_proof(self.root, self.scope, self.observations, self.now)

    def test_exact_case_and_public_data_projection(self):
        feed = self.run_proof()
        self.assertEqual(len(feed['findings']),1)
        match = feed['findings'][0]
        self.assertEqual((match['field'],match['status']),('code','match'))
        for forbidden in ('A12345', 'tracking', 'only-public-path', 'private-source-id'):
            self.assertNotIn(forbidden, json.dumps(feed))
        self.assertTrue(build_article_watch(self.scope, feed, self.now)['verification_available'])
        self.assertFalse(build_article_watch(self.scope, feed, self.now+timedelta(hours=18))['verification_available'])
        self.article['modified']='2026-11-20T15:57:00+09:00'
        self.assertFalse(build_article_watch(self.scope, feed, self.now)['verification_available'])
        self.assertFalse(self.run_proof()['findings'])

    def test_multiple_distinct_codes_collapse_to_one_official_offer(self):
        self.row['coupon_codes'].append({'code':'B12345'})
        self.post['rendered'] += self.post['rendered'].replace('A12345','B12345')
        self.assertEqual(len(self.run_proof()['findings']),1)

    def test_no_matches_on_stale_partial_or_ambiguous_official(self):
        self.meta['coupon_count'] = 2
        self.assertFalse(self.run_proof([self.row,self.row])['findings'])
        self.meta['coupon_count'] = 1
        self.meta['regional_coverage_status']='partial'
        self.assertFalse(self.run_proof()['findings'])
        self.meta['regional_coverage_status']='complete'
        self.row['official_checked_at']='2026-11-19T15:46:00+09:00'
        self.assertFalse(self.run_proof()['findings'])

    def test_no_matches_on_hidden_unclear_nonpublic_or_missing(self):
        rendered=self.post['rendered']
        for invalid in (f'<div hidden>{rendered}</div>', f'<div style="display:none">{rendered}</div>',
                        f'<h3>配布終了</h3>{rendered}', rendered+rendered, rendered.replace('A12345','付記 A12345')):
            self.post['rendered']=invalid
            self.assertFalse(self.run_proof()['findings'])
        self.post['rendered']=rendered
        self.post['status']='draft'
        self.assertFalse(self.run_proof()['findings'])
        self.post['status']='publish'
        self.observations.clear()
        self.assertFalse(self.run_proof()['findings'])
