# HIS公式監視を日本のMac miniで動かす（self-hostedランナー）

HIS公式サイトは海外IPのGitHub Actionsランナーへ403を返すため、HISの取得だけを
日本のMac miniへ移しました。ワークフローは `.github/workflows/his-monitor.yml` です。

## ランナー

| 項目 | 値 |
| --- | --- |
| ランナー名 | `macmini-his` |
| ラベル | `self-hosted` / `macOS` / `ARM64` / `his` |
| ワークフローの指定 | `runs-on: [self-hosted, his]` |
| 作業ディレクトリ | `~/actions-runner-his/_work/`（リポジトリのクローンもここ） |

## 実行する時刻

- 平日 7:00 JST（cron `0 22 * * 0-4`）と平日 15:45 JST（cron `45 6 * * 1-5`）
- 手動実行（`workflow_dispatch`）も可。地域の取り直しはこちらで行う
- `schedule` はデフォルトブランチ（main）にある定義だけが動く。PRブランチでは動かない
- **Mac miniは 7:00 / 15:45 JST に起動していること**が条件。スリープ中はジョブが待ち行列に残り、
  ランナーが戻るまで実行されない（24時間拾われないと失効する）。省電力設定で自動スリープを切り、
  ランナーはログイン中のユーザーセッションで動かす（HISの取得はheadlessではなく画面ありの
  Chromiumを使うため、画面のあるセッションが必要）
- GitHubの `schedule` は混雑時に数分〜十数分遅れることがある

## このワークフローがすること

1. `provider_check_runner.py --provider-id his --deep --fail-on-error` でHIS公式画面を11地域ぶん取得
2. HIS1社だけの実行記録で全社ぶんの `provider_check_data/run-latest.json` を上書きしないよう元に戻す
3. HISのブログ用HTML（テーブル・リスト）とWordPress用フィードを作り、`--dry-run` で検証する
   （WordPress本番への送信はしない。送信は国内Macの既存処理のまま）
4. HISのパスだけをコミットして main へ送る。`git pull --rebase` を最大3回まで試し、
   本線ワークフロー（`coupon-monitor.yml`）のコミットと衝突しても取り込んでから送る

コミット対象: `his_coupon_data/`、`provider_check_data/his/`、`html_output/his_coupons.html`、
`html_output/his_coupons_list.html`、`dashboard/evidence/his/`、`wordpress_feed/his-monitor-feed.json`

## 失敗したとき

- どのステップでも失敗するとジョブが失敗します（`continue-on-error` は付けていません）
- 失敗すると `his-monitor-failure` ラベルの付いたIssueを開き、2回目以降は同じIssueへコメントします。
  ラベルが無ければそのときに作ります。本文に実行ログのURLが入ります
- 確認する順番: Mac miniが起動してランナーが動いているか → HISが403を返していないか →
  地域カバレッジが11地域そろっているか（`provider_check_data/his/latest.json` の
  `regional_coverage_status`）

## 本線ワークフロー（`coupon-monitor.yml`）との関係

- 本線はHISを取得しません（`--exclude-provider-id his`）。HISの選択肢も手動実行から外しました。
  HISだけ取り直したいときは `his-monitor.yml` を手動実行します
- 本線はコミット済みのHISデータでダッシュボードを作ります。HISデータが24時間より古いときは
  `::warning::` を出しますが、本線は止めません
