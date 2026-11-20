import copy
import json
import unittest
from article_watch import build_article_watch

class ArticleWatchTests(unittest.TestCase):
    def setUp(self):
        self.scope = {'sites': [dict(site=s, status='取得済み', checked_at='2026-11-20T15:00:00+09:00', articles=[dict(post_id=12,title='公開クーポン',url=u,modified='2026-11-20T14:00:00+09:00',draft_at='PRIVATE-DRAFT',revenue='SECRET-REVENUE',history={'fields':['クーポンコード','PRIVATE'],'changed_at':'2026-11-19T14:00:00+09:00'})]) for s,u in [('welltrip','https://travel.hb-connect.co.jp/coupon/'),('yakushimafan','https://yakushima.fun/coupon/')]]}
        self.f = dict(site='welltrip',post_id=12,offer_id='PRIVATE-OFFER',field='period',status='difference',article_verified=True,official_verified=True,checked_at='2026-11-20T15:00:00+09:00',official_url='https://www.jtb.co.jp/private-path?secret=abc',replacement='PRIVATE-DRAFT',analytics={'revenue':'SECRET-REVENUE'})
    def build(self, findings):
        return build_article_watch(self.scope,dict(schema_version=1,public_export=True,findings=findings))
    def test_scope_is_real_producer_and_site_id_grouping(self):
        out=self.build([]);self.assertEqual(len(out['articles']),2)
        self.assertFalse(out['verification_available'])
    def test_no_private_values_export(self):
        text=json.dumps(self.build([self.f]));
        for secret in ['PRIVATE','SECRET','secret=abc','private-path','analytics','draft_at']:
            self.assertNotIn(secret,text)
    def test_preserve_offers_fields_and_latest_dedupe(self):
        other={**self.f,'offer_id':'another'};field={**self.f,'field':'code'}
        out=self.build([self.f,self.f,other,field]);self.assertEqual(len(out['articles'][0]['issues']),3)
    def test_reject_unverified_out_of_scope_bad_host_and_fields(self):
        bad=[{**self.f,'official_verified':False},{**self.f,'article_verified':False},{**self.f,'post_id':13},{**self.f,'official_url':'https://jtb.co.jp.evil.test/a'},{**self.f,'field':'revenue'}]
        self.assertFalse(self.build(bad)['verification_available'])
    def test_private_feed_cannot_be_loaded_accidentally(self):
        self.assertFalse(build_article_watch(self.scope,{'schema_version':1,'findings':[self.f]})['verification_available'])
    def test_invalid_scope_link_excluded(self):
        self.scope['sites'][0]['articles'][0]['url']='https://travel.hb-connect.co.jp/wp-admin/'
        self.assertEqual(len(self.build([])['articles']),1)

    def test_latest_is_compared_in_utc(self):
        newer={**self.f,'checked_at':'2026-11-20T07:00:00+00:00','status':'resolved'}
        out=self.build([newer,self.f])
        self.assertEqual(out['articles'][0]['issues'][0]['status'],'resolved')

    def test_script_embedding_escapes_public_title(self):
        from generate_dashboard import generate_html
        from pathlib import Path
        self.scope['sites'][0]['articles'][0]['title']='</script><script>alert(1)</script>'
        data=json.loads(Path('dashboard/latest.json').read_text())
        data['article_watch']=self.build([])
        rendered=generate_html(data)
        self.assertNotIn('</script><script>alert(1)',rendered)
        self.assertIn(r'\u003c/script>',rendered)
