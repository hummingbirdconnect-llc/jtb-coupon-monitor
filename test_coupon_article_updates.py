import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from coupon_article_updates import coupon_facts, draft_dates, job_records, revision_change


class CouponArticleUpdateTests(TestCase):
    def test_revision_ignores_decoration_autosaves_and_general_edits(self):
        live = "<ul><li>予約期間：2026/11/20まで</li><li>3,000円引き</li></ul>"
        post = dict(id=15,date_gmt="2026-10-01T00:00:00",modified_gmt="2026-11-17T17:00:00",content={"raw":live})
        def rev(rid, hour, raw, slug="15-revision-v1"):
            return dict(id=rid, parent=15, date_gmt="2026-11-17T"+hour+":00:00", slug=slug, content={"raw":raw})
        revisions = [rev(4,"17",live+"<p>写真を追加</p>"),rev(3,"16","<p>500円引</p>",slug="15-autosave-v1"),
                     rev(2,"15",live.replace("2026/11/20","2026年11月20日").replace("3,000円引き","３０００円引")),
                     rev(1,"14",live.replace("11/20","11/01"))]
        change = revision_change(post, revisions)
        self.assertEqual(change["changed_at"], "2026-11-18T00:00:00+09:00")
        self.assertEqual(change["fields"], ["予約・利用期間"])
        self.assertNotIn("changed_at", revision_change(post, revisions[-1:]))

    def test_observation_day_does_not_become_a_deadline_change(self):
        raw = "<p>11月18日朝の時点で残り7枚です。予約期間は11月30日までです。</p>"
        self.assertEqual(coupon_facts(raw),coupon_facts(raw.replace("18日","17日")))
        self.assertNotEqual(coupon_facts(raw),coupon_facts(raw.replace("7枚","8枚")))

    def test_drafts_match_original_even_without_slug_and_ambiguous_titles_stay_unmatched(self):
        posts = [dict(id=1,slug="a",title={"raw":"同名クーポン"}),dict(id=2,slug="b",title={"raw":"同名クーポン"}),
                 dict(id=3,slug="c",title={"raw":"秋のクーポン"})]
        def draft(title,slug=""):
            return dict(title={"raw":title},slug=slug,status="draft",modified_gmt="2026-11-17T16:00:00")
        hits=draft_dates(posts,[draft("2026-11-18 追加分・訂正 秋のクーポン"),draft("同名クーポン"),
                                draft("東武の追加分（屋久島ファン2用）")],"yakushimafan")
        self.assertEqual(set(hits),{2,3})
        self.assertTrue(hits[2].startswith("2026-11-18"))

    def test_job_update_needs_successful_run_and_matching_readback(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            for day, rc in [("17","0"),("18","1")]:
                run=root/f"202611{day}-morning"
                check=run/"check"
                scope=check/"scope-111-final"
                scope.mkdir(parents=True)
                (run/"run.done").write_text(rc+" finished")
                (run/"stage3.ok").touch()
                (check/"pre-stats.json").write_text(json.dumps({"welltrip-111":{"status":"publish","sha256":"old"}}))
                (check/"final-stats.json").write_text(json.dumps({"welltrip-111":{"status":"publish","sha256":"new","same_as_stage2":True}}))
                (scope/"report.json").write_text(json.dumps({"exit_code":0,"source_sha256":"old","candidate_sha256":"new"}))
            self.assertEqual(job_records(root)["welltrip",111]["published_run"],"2026-11-17 朝")
            proof=root/"20261117-morning/check/scope-111-final/report.json"
            proof.write_text(json.dumps({"exit_code":0,"source_sha256":"old","candidate_sha256":"wrong"}))
            self.assertNotIn("published_run",job_records(root)["welltrip",111])

    def test_modern_compare_public_and_express_fail_closed(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root/'20261120-express-091500'
            folder = run/'check/stage3'; folder.mkdir(parents=True)
            (run/'run.done').write_text('0 completed')
            (run/'stage3.ok').touch()
            item = dict(status='publish',ok=True,backup_ok=True,same_as_pre=False,same_as_stage2_post=True)
            (folder/'compare.json').write_text(json.dumps({'ng':[], 'items':{'welltrip-12':item,'yakushimafan-12':{**item,'status':'draft'}}}))
            public = {'welltrip-12':dict(code=200,login=False,title=True,url='https://travel.hb-connect.co.jp/coupon/')}
            (folder/'public.json').write_text(json.dumps(public))
            out=job_records(root)
            self.assertEqual(out['welltrip',12]['published_run'],'2026-11-20 臨時')
            self.assertNotIn(('yakushimafan',12),out)
            public['welltrip-12']['login']=True
            (folder/'public.json').write_text(json.dumps(public))
            self.assertFalse(job_records(root))
