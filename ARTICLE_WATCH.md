# 記事監視部隊（公開表示のみ）

既存 `#coupon-updates` / 「クーポン記事の更新」に統合ビューを追加する。
従来の履歴・日時・下書き保存日時・並べ替えは開閉式の既存ビューで保持。
収集、分析、WP公開、承認、スケジュールは変更しない。

## 自動の入力・出力

`generate_dashboard.py` → `article_watch.load_article_watch(ROOT)` が毎回
`editorial_data/coupon-articles.json` の公開対象を `(site, post_id)` でまとめる。
追加ファイルがなくても両サイト全対象・公開最終編集・公開条件変更履歴を生成。
未取得scopeは前回記録と表示。保存履歴の変更日は公式照合の証拠にはしない。
既存syncの `coupon_article_updates.job_records` は終了済み朝夕・臨時回の
`stage3.ok` + `run.done`成功 + `check/stage3/compare.json` + `public.json`を
認識する。公開status、backup_ok、ok、HTTP200、非ログインページ、題名検出、
同じサイト、stage2投稿との一致を満たす場合だけ公開更新を表示。draftは別。
これらは公開済み記事の履歴であり、未反映案や新たな公式照合ではない。

## 任意の公開照合feed

`editorial_data/article-watch-public.json` は **公開用に承認・検証した出力だけ**。
非公開の修正文、解析データ、運用報告をこのファイルへ渡してはいけない。
スキーマ例（説明用。ライブfeedとして設置しない）:

```json
{"schema_version":1,"public_export":true,"findings":[
 {"site":"welltrip","post_id":12,"offer_id":"public-stable-offer-key",
  "field":"period","status":"difference","article_verified":true,
  "official_verified":true,"official_url":"https://www.jtb.co.jp/",
  "checked_at":"2026-11-20T15:00:00+09:00"}
]}
```

- field: amount / period / stock / code / link / eligibility
- status: match / difference / resolved
- scope外、未検証、許可外field/status/公式hostは拒否。
- offer IDは一方向ハッシュで内部重複判定のみ。画面には出さない。
- 異なるoffer/fieldを潰さず、同じ組はUTCで新しい証拠を採用。
- 任意の説明文・金額・分析数値・修正案・内部リンクは投影しない。
- 公式URLは許可hostのoriginだけ出力し、path/query/fragmentは出さない。
- feed未配置は「公式との照合未確認」。空を成功や問題なしと見なさない。

現状は公開scopeと公開更新履歴が自動配線済み。公式照合feedの自動供給は
別途承認済みworkerの責務であり、この変更では追加の自動収集を起動しない。

## テスト・配布

`python3 -m unittest test_article_watch test_coupon_article_updates test_generate_dashboard`
`node --check templates/coupon_article_updates.js`

初回配布のdashboard出力は既存latest.jsonを維持してarticle_watchのみ追加。
他社データ、X、公開済み在庫、既存draft日時を取り直して混入させない。
通常の次回dashboard生成でも同じprojectionが更新される。
