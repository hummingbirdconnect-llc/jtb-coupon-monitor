# OTAクーポン自動監視 - 運用マニュアル

> **リポジトリ**: https://github.com/hummingbirdconnect-llc/jtb-coupon-monitor
>
> **最終更新**: 2026年7月10日

---

## 2026-07 OTA監視拡張

### 実行頻度

- 毎日: HIS、JTB、KNT、JALパック、るるぶ、ゆこゆこ、楽天、じゃらん、Yahoo!トラベル、一休、Booking.com、Agoda、Expedia、Hotels.com、Trip.com、KKday、Klookの17社
- 5日ごと: 残り27社。会社IDから日を分散し、5日間に1回だけ実行
- 正本: `config/provider_registry.json` の `schedule`

### 公式取得とCodex監査

GitHub Actionsは公式API・埋め込みJSON・HTML・Playwrightの順で取得し、ページ内容のhashが変わった場合に `codex_audit_queue/` へ監査候補JSONを保存します。GitHub ActionsからOpenAI APIやWordPress更新は実行しません。

Codex定期実行は監査候補を読み、`ota-official-deal-researcher` の基準で「掲載可・条件付き・掲載不可・終了済み」を分類します。公式URL、ページ内の根拠文、金額、コード、日付は `deal_audit_schema.py` でも再検証し、推測値、根拠文にない値、公式ドメイン外URLは採用しません。

公式ページ監査の初期対象は、じゃらん宿泊、KKday、Klook、楽天トラベル、Trip.comです。これは機能上の上限ではなく、公式取得元が設定済みの会社です。44社の取得スケジュールは維持し、公式取得元を追加できた会社からCodex監査対象へ昇格します。初回監査は比較用の基準データを作るだけで、WordPress下書きは作りません。

### 必須GitHub Secrets

| Secret | 用途 |
|--------|------|
| `GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD` / `NOTIFY_EMAIL` | GitHub Actionsの差分通知 |

WordPress認証情報はGitHub Actionsへ渡さず、Codexを実行するローカル環境だけに置きます。認証情報はGitへ保存しません。

### WordPress安全条件

- Codex監査が `high_confidence=true` かつ意味のある差分と判定した場合だけ処理
- 自動下書きはJSTで1日最大5件。6件目以降は保留キューへ送り、ユーザー確認を求める
- 公開記事は変更せず、`<slug>-coupon-update` の `draft` を作成または更新
- title/H1、H2/H3、本文、FAQ、CTA、slug、canonical、affiliate URLは保持
- 終了情報や前回データが公式ページから消えただけでは削除せず「要確認」で保持
- 自動生成後に人が下書きを編集した場合はhash不一致で停止
- 自動公開処理はない

状態確認:

```bash
python3 provider_check_runner.py --scope due
python3 generate_dashboard.py
python3 codex_audit_runner.py pending --json
python3 codex_audit_runner.py apply-all --dry-run
python3 wp_review_orchestrator.py --dry-run
```

6件目以降を同日に追加する場合だけ、ユーザー承認後の手動実行で `--approved-extra-drafts N` を指定します。認証設定や人手下書きの確認後に保留候補を再試行する場合は `--retry-attention` を使います。定期実行ではどちらも指定しません。

---

## 1. システム全体像

```
┌─ GitHub Actions（毎朝9:00 JST 自動実行）────────────────┐
│                                                           │
│  ① JTBクーポンページをスクレイピング                      │
│     ├─ 国内クーポン一覧（CSSセレクタ方式）               │
│     ├─ 海外クーポン一覧（CSSセレクタ方式）               │
│     └─ Stock API で配布状況を判定                         │
│                                                           │
│  ② KNTクーポンページをスクレイピング                      │
│     ├─ 獲得クーポン一覧                                  │
│     └─ クーポンコード一覧                                │
│                                                           │
│  ③ 前日との差分を検出                                    │
│     ├─ 🆕 新規: 今日初めて出現したクーポン               │
│     ├─ ❌ 消失: 昨日まであったが今日消えた               │
│     ├─ 🔴 配布終了: Stock API で終了判定（JTBのみ）      │
│     └─ 🟢 配布再開: 終了→再開（JTBのみ）                │
│                                                           │
│  ④ 結果を保存                                            │
│     ├─ GitHub: master_ids.json + 日次JSON + レポート     │
│     ├─ Google Sheets: 4つのシート                        │
│     └─ メール: 変動があれば差分レポートを送信            │
└───────────────────────────────────────────────────────────┘

監視対象URL:
  JTB国内: https://www.jtb.co.jp/myjtb/campaign/coupon/
  JTB海外: https://www.jtb.co.jp/myjtb/campaign/kaigaicoupon/
  KNT獲得: https://www.knt.co.jp/coupon/get/
  KNTコード: https://www.knt.co.jp/coupon/code/
```

### 生成されるファイル

| 場所 | ファイル | 内容 |
|------|---------|------|
| GitHub | `jtb_coupon_data/master_ids.json` | JTBクーポンID台帳（差分検出用） |
| GitHub | `jtb_coupon_data/coupons_YYYY-MM-DD.json` | JTB日次スクレイピングデータ |
| GitHub | `jtb_coupon_data/change_log.json` | JTB変動ログ（90日ローリング） |
| GitHub | `jtb_coupon_data/report_YYYY-MM-DD.md` | JTB日次レポート |
| GitHub | `knt_coupon_data/master_ids.json` | KNTクーポンID台帳 |
| GitHub | `knt_coupon_data/coupons_YYYY-MM-DD.json` | KNT日次スクレイピングデータ |
| GitHub | `knt_coupon_data/change_log.json` | KNT変動ログ（90日ローリング） |
| GitHub | `knt_coupon_data/report_YYYY-MM-DD.md` | KNT日次レポート |
| Google Sheets | 「JTB_現在のクーポン」 | JTB全クーポン一覧（配布状況色分け） |
| Google Sheets | 「JTB_変動ログ」 | JTB日々の変化記録 |
| Google Sheets | 「KNT_現在のクーポン」 | KNT全クーポン一覧 |
| Google Sheets | 「KNT_変動ログ」 | KNT日々の変化記録 |

※ 日次ファイル（coupons_*.json, report_*.md）は30日経過で自動削除されます。

---

## 2. 毎日の運用

### 通常時: 何もしなくてOK

毎朝9時にGitHub Actionsが自動実行されます。変動があればメール通知されます。

### 結果の確認（Google Sheets）

**「JTB_現在のクーポン」シートを開くだけで、今取得可能なクーポンが全部わかります。**

- 🟢 配布中のクーポンが上に、🔴 配布終了が下に表示
- 配布終了行は薄赤で色付け
- 割引額・クーポンコード・パスワード・期間を一覧表示

### 毎朝のX投稿（サイト別デイリーツリー）

毎朝の自動実行で `tweets_output/x_threads_YYYY-MM-DD.md` が生成されます。
屋久島ファン / ウェルトリップ / トリップブッキングの3サイト×重要度トップ3クーポン×各3投稿のツリーです。

1. GitHubで当日のファイルを開く（スマホ可）:
   `https://github.com/hummingbirdconnect-llc/jtb-coupon-monitor/blob/main/tweets_output/x_threads_YYYY-MM-DD.md`
2. サイトごとに、各ツリーの1投稿目を投稿 → 2投稿目・3投稿目を自分の投稿へのリプライとして続ける
3. リンクは3投稿目のみ（1投稿目にリンクを入れるとXのアルゴリズムでインプレッションが下がるため）

調整したいとき:
- 記事リンク先・対象OTAの追加削除: `config/x_thread_sites.json` の `article_map`
- クーポンコードを投稿に表示してよいOTA: 同ファイルの `trusted_code_providers`（データ取得時にコードが完全形と確認できたOTAだけ追加。JTB/HISは切り詰めを確認済みのため非表示）
- 文面テンプレ・スコアリング: `generate_x_threads.py` の `TONES` / `score_coupon()`
- 手動再生成: `python generate_x_threads.py --source remote`

---

## 3. よく使う操作

### 3-1. 手動で今すぐ実行

1. https://github.com/hummingbirdconnect-llc/jtb-coupon-monitor/actions
2. 「Coupon Monitor (JTB + KNT)」→「Run workflow」→「Run workflow」

### 3-2. ターミナルで確認

```bash
cd ~/jtb-coupon-monitor

# 手動でフル実行
python3 jtb_coupon_monitor.py
python3 knt_coupon_monitor.py

# 初回セットアップ（マスター台帳をリセットしたい場合）
python3 jtb_coupon_monitor.py --init
python3 knt_coupon_monitor.py --init
```

### 3-3. 実行時間の変更

`.github/workflows/coupon-monitor.yml` の `cron` を編集:

```yaml
# 朝7時（JST）に変更 → UTC 22:00
- cron: '0 22 * * *'

# 1日2回（朝9時 + 夕方18時）
- cron: '0 0,9 * * *'
```

### 3-4. 一時停止/再開

Actions タブ → 左サイドバー「Coupon Monitor (JTB + KNT)」→ 右上「...」→「Disable workflow」

---

## 4. Google Sheets 連携のセットアップ

### 4-1. Google Cloud でサービスアカウントを作成（初回のみ・約10分）

1. https://console.cloud.google.com/ にアクセス
2. プロジェクト作成（名前: `coupon-monitor` 等）
3. 「APIとサービス」→「ライブラリ」で **Google Sheets API** と **Google Drive API** を有効化
4. 「認証情報」→「+ 認証情報を作成」→「サービスアカウント」作成
5. サービスアカウント → 「キー」タブ → JSON キーをダウンロード
6. サービスアカウントのメールアドレスをメモ

**⚠️ 重要: JSONキーファイルは絶対にGitリポジトリにコミットしないでください。**

### 4-2. スプレッドシートを準備

1. https://sheets.google.com で新規作成
2. 右上「共有」→ サービスアカウントのメールアドレスを「編集者」で追加
3. URLからスプレッドシートIDをメモ（`/d/` と `/edit` の間の文字列）

### 4-3. GitHub にシークレットを登録

https://github.com/hummingbirdconnect-llc/jtb-coupon-monitor/settings/secrets/actions

| Name | Secret |
|------|--------|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | JSONファイルの中身をまるごとコピペ |
| `SPREADSHEET_ID` | スプレッドシートID |
| `GMAIL_ADDRESS` | 通知元Gmailアドレス |
| `GMAIL_APP_PASSWORD` | Gmailアプリパスワード |
| `NOTIFY_EMAIL` | 通知先メールアドレス（カンマ区切りで複数可） |

---

## 5. JTBスクレイピング技術詳細

### HTML構造（2026-02時点）

```html
<div class="c-coupon__item" data-id="XXX" data-category='["宿泊","ツアー"]' data-pref='["全国"]'>
  <div class="c-coupon__head">
    <div class="c-coupon__area">全国</div>
    <div class="c-coupon__price">最大<em>3,000</em>円引<br/>クーポン</div>
  </div>
  <div class="c-coupon__bottom">
    <h3 class="c-coupon__title"><a href="/myjtb/campaign/coupon/detail/XXX/page.asp">...</a></h3>
    <p class="c-coupon__term">予約対象期間：... 宿泊対象期間：...</p>
  </div>
</div>
```

### Stock API

- エンドポイント: `/myjtb/campaign/coupon/api/groupkey-stock?groupkey=ID1,ID2,...`
- レスポンス: `StockFlag=1`（配布中）、`StockFlag=0`（配布終了）
- **重要**: バッチサイズは10件以下にすること。20件以上だと `Result=-20001` エラーになる

---

## 6. トラブルシューティング

| 症状 | 原因 | 対処 |
|------|------|------|
| Actions が失敗 | 権限不足 | `permissions: contents: write` を確認 |
| Sheets更新されない | 認証エラー | `GOOGLE_SERVICE_ACCOUNT_JSON` の中身を再確認 |
| 0件で異常終了 | HTML構造変更 | ソースを確認してCSSセレクタを修正 |
| Stock APIが全件不明 | バッチサイズ超過 | `BATCH_SIZE` が10以下か確認 |
| メール通知が来ない | Gmail設定 | `GMAIL_APP_PASSWORD`（アプリパスワード）を確認 |
| マスター台帳が壊れた | JSONエラー | `master_ids.json` を削除して `--init` で再作成 |

---

## 7. ファイル構成

```
jtb-coupon-monitor/
├── .github/
│   └── workflows/
│       └── coupon-monitor.yml       ← GitHub Actions 設定
├── jtb_coupon_monitor.py            ← JTBスクレイピング（CSSセレクタ方式 + Stock API）
├── knt_coupon_monitor.py            ← KNTスクレイピング
├── export_to_sheets.py              ← Google Sheets 書き出し（4シート）
├── daily_diff_notifier.py           ← 差分通知メール送信
├── requirements.txt                 ← Python依存パッケージ
├── .gitignore
└── jtb_coupon_data/                 ← 自動生成
│   ├── master_ids.json              ← JTBクーポンID台帳
│   ├── change_log.json              ← 変動ログ（90日ローリング）
│   ├── coupons_YYYY-MM-DD.json      ← 日次データ（30日保持）
│   └── report_YYYY-MM-DD.md         ← 日次レポート（30日保持）
└── knt_coupon_data/                 ← 自動生成
    ├── master_ids.json
    ├── change_log.json
    ├── coupons_YYYY-MM-DD.json
    └── report_YYYY-MM-DD.md
```

---

## 8. コスト

OpenAI APIは使用しません。Codex定期実行はChatGPT/Codex契約内の利用枠を使います。

| 項目 | コスト |
|------|--------|
| GitHub Actions | 無料（月2,000分。本ツールは月90分程度） |
| Google Cloud | 無料 |
| Google Sheets API | 無料（1日300リクエスト。本ツールは約8リクエスト/日） |
| Gmail SMTP | 無料（1日500通。本ツールは最大1通/日） |
| OpenAI API | 未使用 |
| Codex定期実行 | ChatGPT/Codex契約の利用上限に従う |
