# Yahoo!トラベル・一休.com を定期実行に加える案（2026-10-21）

> 対象: `yahoo_travel_coupon_monitor.py`・`ikkyu_coupon_monitor.py`（共通部品 `ikyu_platform_coupons.py`）。
> この文書は案。定義ファイル（コード・ワークフロー・テスト）の変更は、ユーザーの承認を得てから行う。

## 0. 結果（2026-10-21 同日の追記）

- 案Aを承認後、GitHub 側で PR #26（cron 廃止・平日2回の手動実行・海外IPから拒否される会社は日本の Mac から取得）と PR #24（一休の公式ページ登録）が先に入った
- そのため案Aの「毎日2回の cron」はやめ、2社とも `jp_daily_provider_ids`（日本の Mac から 6:45 / 15:30 JST）に置いた。Yahoo!トラベルは一休と同じ仕組みで、同じ道具・同じ順（一休→Yahoo）で回す必要があるため、一休と同じ置き場にした
- 実行表（`REAL_MONITOR_COMMANDS`）への2行追加、登録表の `auto_daily` と保存先、ワークフローの生成物一覧、テストの集合、運用文書の8社→12社の書き換えは入れた
- 残り: 日本の Mac の `provider-fetch-jp/run.sh`（作業場の外）は、2社の保存先を commit 対象に足し、対象に `yahoo_travel` を加える必要がある。これを入れるまで、Mac の定期取得で一休を回すと保存先の変更が「対象外の変更」として捨てられる。下の第3節以降は当時の案のまま残す

## 1. 今日までに直したこと（設定だけ）

`config/provider_registry.json` の2社を、既存の自動取得の会社（ゆこゆこ・るるぶ）と同じ書き方にそろえた。

| 会社 | coverage_status | data_dir（保存先） | legacy_data_dir（旧の保存先） |
| --- | --- | --- | --- |
| yahoo_travel | official_on_demand → auto_on_demand | official_coupon_data/yahoo_travel → yahoo_travel_coupon_data | manual_coupon_data/yahoo_travel → official_coupon_data/yahoo_travel |
| ikkyu | article_exists → auto_on_demand | manual_coupon_data/ikkyu → ikkyu_coupon_data | （無し）→ manual_coupon_data/ikkyu |

確かめた結果:

- ダッシュボード（`generate_dashboard.py`）は2社の新しいデータを読める。Yahoo!トラベル 99件、一休 72件、日付 2026-10-21。出力先を一時フォルダにして確かめ、`dashboard/` は書き換えていない
- 既存テスト（`test_generate_dashboard.py`・`test_automation_pipeline.py`・`test_ikyu_platform_coupons.py`・`test_validator.py`）46件が通る。pytest は `~/agent-tools/coupon-daily/.venv/bin/python` にだけ入っている
- `provider_check_runner.py` の選択は変わらない。`--scope due` は従来の6社（HIS・JTB・近畿日本ツーリスト・JALパック・じゃらん・楽天トラベル）だけ

ただし、2社の取得道具はまだ `provider_check_runner.py` の実行表（`REAL_MONITOR_COMMANDS`）に載っていない。そのため、定期実行でも `--provider-id ikkyu` の指定でも道具は動かない（一休は記事URLの生存確認、Yahoo!トラベルはキャンペーン予定ページの深掘りだけになる）。今、道具を動かす方法は直接起動だけ。

```bash
python3 ikkyu_coupon_monitor.py && python3 yahoo_travel_coupon_monitor.py && python3 generate_dashboard.py
```

一休を先に走らせる理由は、同じ管理IDの印（`detail_data.shared_with`）が相手の保存済みファイルを読んで付くため。

## 2. 定期実行の仕組みの現状

- 本体は GitHub Actions `.github/workflows/coupon-monitor.yml`。cron は `0 2,16 * * *`（UTC）＝ 毎日 11:00 と 01:00（日本時間）。`provider_check_runner.py --scope due` で `schedule.daily_provider_ids` の6社を取得し、生成物を main へ commit・push する
- 手元の Mac の launchd 3本（`com.hbconnect.jtb-coupon-table` 7:00、`com.hbconnect.coupon-daily-codex` 7:30、`com.hummingbird.jtb-trigger` 9:00）は、JTB表の下書き更新・Grok候補集め・JTB起動の役目で、6社の取得そのものは行っていない
- 運用文書 `JTB_COUPON_MONITOR_OPERATIONS.md` は「毎日6:00 JST、HISは11:05 / 19:05にも」のまま。9/26 の変更（6社を 11:00 / 01:00 に、HISの追加確認を廃止）が文書に反映されていない。この案と一緒に直したい

## 3. 案A（おすすめ）: 6社と同じ GitHub Actions の毎日2回に乗せる

変更する場所は6つ。1〜5はこの作業場の編集で、6だけが commit・push になる。

1. `provider_check_runner.py` の `REAL_MONITOR_COMMANDS` に2行足す（`ikkyu` を先、`yahoo_travel` を後。実行順は登録表の並び順なので、登録表でも一休を Yahoo!トラベルより前に置くか、実行表の順で回すよう1行直す）
2. `config/provider_registry.json`: `schedule.daily_provider_ids` に `ikkyu`・`yahoo_travel` を足す（6社→8社）。`schedule.note` の「6社」を「8社」に。2社の `coverage_status` を `auto_daily` に、note の「指定時だけ実行」を外す
3. `.github/workflows/coupon-monitor.yml`: commit する生成物の一覧に `yahoo_travel_coupon_data/` と `ikkyu_coupon_data/` を足す。cron のコメントを8社に直す
4. `test_automation_pipeline.py` の `ALWAYS_ON_IDS` に2社を足す（常時監視の集合をテストが固定しているため）
5. `JTB_COUPON_MONITOR_OPERATIONS.md`: 「常時監視の6社」を8社に。実行時刻の記述をワークフローに合わせる
6. 新しいファイル（monitor 2本・共通部品・テスト・データ2フォルダ）と上の変更を commit・push する。GitHub Actions は main を取り出して動くため、push しないと定期実行に乗らない

その結果:

- 毎日 11:00 と 01:00（日本時間）に2社も取得し、ダッシュボードの鮮度が「新鮮」で付く。今の一休の「旧スナップショット」表示（取得記録が記事URL確認のままのため）も消える
- 費用は Actions 1回あたり十数秒の追加（requests だけで、ブラウザは使わない）。無料枠内
- 差分通知メール（`daily_diff_notifier.py`）と X 投稿の生成（`generate_x_threads.py`）は会社が固定なので、2社の差分は通知されない。必要なら別の作業

案Aで起きる注意点:

- Yahoo!トラベルのキャンペーン予定ページ（`official_sources`、PayPay還元・セール日程）の深掘りは、実行表に載せた時点で走らなくなる（実行表の会社は深掘りの前に戻る作り）。旧データの「ポイント」11件は、今日の登録替えでダッシュボードの Yahoo!トラベルのタブから既に外れている。残したい場合は、キャンペーン予定ページの結果を別の置き場に書き、ダッシュボードで両方読む小さな改修が要る
- Codex 監査の出力先は `data_dir`。今の登録のまま Yahoo!トラベルの深掘りを手で走らせると、監査結果が `yahoo_travel_coupon_data` のその日のファイルに混ざる。案Aを入れるまで、Yahoo!トラベルの `--deep` は走らせない

## 4. 案B: 手元の Mac の launchd で毎朝1回

- 新しい plist を1本作り、2社の monitor と `generate_dashboard.py` を直接起動する
- 6社と時刻も場所も別になり、取得記録（`provider_check_data/`）が付かず鮮度が出ない。GitHub へ送る処理も別に要る。おすすめしない

## 5. 案C: 今のまま（人が起動する）

- 第1節のコマンドを人が実行する。何もしないと 10/21 のデータのまま古くなり、30日で保持切れになる

## 6. お願いすること

案Aの1〜5を進めてよいかの判断。6（commit・push）は内容を見てから別に承認を取る。
