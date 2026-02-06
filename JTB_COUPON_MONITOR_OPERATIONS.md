# JTB クーポン自動監視 — 運用マニュアル

> **リポジトリ**: https://github.com/hummingbirdconnect-llc/jtb-coupon-monitor
>
> **最終更新**: 2026年2月7日

---

## 1. システム全体像

```
┌─ GitHub Actions（毎朝9:00 JST 自動実行）──────────────┐
│                                                         │
│  ① JTBクーポンページをスクレイピング                    │
│     ├─ 一覧ページから全クーポンを取得                   │
│     └─ 各詳細ページでコード・パスワード・条件を抽出    │
│                                                         │
│  ② 前日データと比較                                    │
│     ├─ 新規追加されたクーポン                          │
│     ├─ 終了/削除されたクーポン                         │
│     └─ 内容が変更されたクーポン（コード変更含む）      │
│                                                         │
│  ③ 結果を2箇所に保存                                  │
│     ├─ GitHub リポジトリ（JSONデータ＋テキストレポート）│
│     └─ Google Sheets（一覧＋変動ログ）                 │
└─────────────────────────────────────────────────────────┘
```

### 生成されるファイル

| 場所 | ファイル | 内容 |
|------|---------|------|
| GitHub | `jtb_coupon_data/coupons_YYYY-MM-DD.json` | その日のクーポン全データ（生データ） |
| GitHub | `jtb_coupon_data/reports/report_YYYY-MM-DD.txt` | 前日比の差分レポート |
| Google Sheets | 「クーポン一覧」シート | 最新のクーポン全件（毎日上書き更新） |
| Google Sheets | 「変動ログ」シート | 追加・削除・変更の時系列ログ（毎日追記） |

---

## 2. 毎日の運用（やることは1つだけ）

### 通常時: 何もしなくてOK

毎朝9時にGitHub Actionsが自動実行されます。変化がなければ何も起きません。

### 変化を確認したいとき

**方法A: Google Sheets を見る（最も簡単・推奨）**

1. スプレッドシートを開く
2. 「変動ログ」シートを見る
3. 最新の行に今日の状況が記録されている

```
日付        | 種別     | タイトル                    | 割引額        | 変更内容
2026-02-07 | ─ 変化なし |                            |              | 全15件に変更なし
2026-02-08 | 🆕 追加   | 春の北海道旅行クーポン       | 最大5,000円引 | コード: HOKKA2026
2026-02-08 | ❌ 終了   | 冬のスキー割引クーポン       | 最大20,000円引|
2026-02-09 | ✏️ 変更   | 3月までの国内宿泊クーポン    | 最大3,000円引 | discount: 最大2,000円引 → 最大3,000円引
```

「クーポン一覧」シートには常に最新のクーポン全件が入っているので、いつでも現状を一覧できます。

**方法B: GitHub でレポートを見る**

1. https://github.com/hummingbirdconnect-llc/jtb-coupon-monitor を開く
2. `jtb_coupon_data/reports/` フォルダをクリック
3. 最新の `report_YYYY-MM-DD.txt` を開く

**方法C: Actions の実行ログを見る**

1. https://github.com/hummingbirdconnect-llc/jtb-coupon-monitor/actions を開く
2. 最新の実行をクリック
3. 「クーポン監視スクリプトを実行」のログを展開

---

## 3. よく使う操作

### 3-1. 手動で今すぐ実行したい

旅行シーズン前など、定時を待たずに最新データが欲しいとき。

1. https://github.com/hummingbirdconnect-llc/jtb-coupon-monitor/actions を開く
2. 左サイドバー「JTB Coupon Monitor」をクリック
3. 右上「Run workflow」→「Run workflow」（緑ボタン）

### 3-2. 実行時間を変更したい

`.github/workflows/jtb-coupon-monitor.yml` の `cron` を編集します。

```yaml
# 例: 朝7時（JST）に変更 → UTC 22:00
- cron: '0 22 * * *'

# 例: 1日2回（朝9時 + 夕方18時 JST）
- cron: '0 0,9 * * *'

# 例: 平日のみ（月〜金）朝9時
- cron: '0 0 * * 1-5'
```

cron書式: `分 時(UTC) 日 月 曜日`（JSTはUTC+9なので9時間引く）

### 3-3. 一時的に自動実行を止めたい

1. Actions タブ → 左サイドバー「JTB Coupon Monitor」
2. 右上の「...」→「Disable workflow」
3. 再開するときは同じ場所で「Enable workflow」

### 3-4. スクリプトを修正したい

ターミナルで：

```bash
cd ~/jtb-coupon-monitor

# ファイルを編集（お好みのエディタで）
# ...

# 変更をGitHubに反映
git add .
git commit -m "スクリプトを改善"
git push
```

次回の自動実行（または手動実行）から反映されます。

---

## 4. Google Sheets 連携のセットアップ

### 4-1. Google Cloud でサービスアカウントを作成（初回のみ・約10分）

「サービスアカウント」とは、人間ではなくプログラムが使う専用のGoogleアカウントです。このアカウントにスプレッドシートの編集権限を与えます。

**手順:**

1. **Google Cloud Console** にアクセス
   https://console.cloud.google.com/

2. **プロジェクトを作成**
   - 上部の「プロジェクトを選択」→「新しいプロジェクト」
   - プロジェクト名: `jtb-coupon-monitor`（何でもOK）
   - 「作成」をクリック

3. **Google Sheets API を有効化**
   - 左メニュー「APIとサービス」→「ライブラリ」
   - 「Google Sheets API」で検索 → クリック →「有効にする」
   - 同様に「Google Drive API」も検索して有効化

4. **サービスアカウントを作成**
   - 左メニュー「APIとサービス」→「認証情報」
   - 上部「+ 認証情報を作成」→「サービスアカウント」
   - サービスアカウント名: `jtb-coupon-bot`
   - 「作成して続行」→ ロールは設定不要 →「完了」

5. **JSONキーをダウンロード**
   - 作成したサービスアカウントをクリック
   - 「キー」タブ →「鍵を追加」→「新しい鍵を作成」
   - キーのタイプ: JSON →「作成」
   - JSONファイルが自動ダウンロードされる（**これが認証キー。大切に保管**）

6. **サービスアカウントのメールアドレスをメモ**
   - `jtb-coupon-bot@jtb-coupon-monitor.iam.gserviceaccount.com` のような形式
   - 次のステップで使います

### 4-2. スプレッドシートを準備

1. **Google Sheets で新しいスプレッドシートを作成**
   - https://sheets.google.com → 「空白」で新規作成
   - 名前: 「JTBクーポン監視」（任意）

2. **サービスアカウントに編集権限を付与**
   - スプレッドシートの右上「共有」をクリック
   - STEP 4-1 でメモしたサービスアカウントのメールアドレスを入力
   - 権限: 「編集者」
   - 「送信」

3. **スプレッドシートIDをメモ**
   - URL の `https://docs.google.com/spreadsheets/d/` と `/edit` の間の文字列がID
   - 例: `https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/edit`
   - → ID は `1AbCdEfGhIjKlMnOpQrStUvWxYz`

### 4-3. GitHub にシークレットを登録

1. https://github.com/hummingbirdconnect-llc/jtb-coupon-monitor/settings/secrets/actions を開く
2. 「New repository secret」をクリック

**1つ目のシークレット:**

- Name: `GOOGLE_SERVICE_ACCOUNT_JSON`
- Secret: ダウンロードしたJSONファイルの**中身をまるごとコピペ**
  - ターミナルで `cat ~/Downloads/jtb-coupon-monitor-xxxxx.json | pbcopy` とすればクリップボードにコピーされる
- 「Add secret」

**2つ目のシークレット:**

- Name: `SPREADSHEET_ID`
- Secret: STEP 4-2 でメモしたスプレッドシートID
- 「Add secret」

### 4-4. ファイルをリポジトリに追加

```bash
cd ~/jtb-coupon-monitor

# export_to_sheets.py をフォルダに配置（ダウンロードしたファイルをコピー）
cp ~/Downloads/export_to_sheets.py ./

# 更新版ワークフローを配置
cp ~/Downloads/jtb-coupon-monitor.yml .github/workflows/jtb-coupon-monitor.yml

# GitHubに反映
git add .
git commit -m "Google Sheets連携を追加"
git push
```

### 4-5. テスト実行

1. GitHub → Actions → 「Run workflow」で手動実行
2. 全ステップが ✅ になれば成功
3. Google Sheets を開いて「クーポン一覧」「変動ログ」シートが作成されていることを確認

---

## 5. トラブルシューティング

### Actions が失敗する

| 症状 | 原因 | 対処 |
|------|------|------|
| 「Permission denied to push」 | 書き込み権限不足 | ワークフローの `permissions: contents: write` を確認 |
| 「ModuleNotFoundError: gspread」 | パッケージ未インストール | ワークフローの pip install に `gspread google-auth` があるか確認 |
| 「403 Forbidden: Google Sheets」 | シートの共有設定 | サービスアカウントのメールアドレスに「編集者」権限を付与したか確認 |
| 「Spreadsheet not found」 | ID間違い | GitHub Secrets の `SPREADSHEET_ID` が正しいか確認 |
| 「Invalid credentials」 | JSONキー間違い | `GOOGLE_SERVICE_ACCOUNT_JSON` にJSONの中身がまるごと入っているか確認 |
| スクレイピングで0件になる | JTBのHTML構造変更 | Claudeに相談してスクリプトを修正 |

### 確認コマンド集

```bash
# ローカルでスクレイピングだけテスト
cd ~/jtb-coupon-monitor
python3 jtb_coupon_monitor.py --list-only

# ローカルでSheets書き出しテスト（環境変数を一時的に設定）
export GOOGLE_SERVICE_ACCOUNT_JSON=$(cat ~/Downloads/jtb-coupon-monitor-xxxxx.json)
export SPREADSHEET_ID="あなたのスプレッドシートID"
python3 export_to_sheets.py

# GitHub Actions のログを確認
open https://github.com/hummingbirdconnect-llc/jtb-coupon-monitor/actions
```

---

## 6. データの活用方法

### 記事作成への活用

Google Sheets にデータが溜まることで、以下のような記事ネタに使えます：

- **「今月のJTBクーポンまとめ」記事**: クーポン一覧シートからコピペするだけ
- **「先週追加された新クーポン」記事**: 変動ログでフィルターすれば一発
- **クーポン有効期限のアラート**: スプレッドシートの条件付き書式で期限間近を赤くハイライト

### 他OTAへの横展開

このスクリプトの構造（スクレイピング → JSON保存 → 差分比較 → Sheets書き出し）は、エクスペディアやアゴダなど他のOTAにもそのまま応用できます。`scrape_coupon_list()` と `scrape_detail_page()` の中身を各OTAのHTML構造に合わせて変えるだけです。

---

## 7. コスト

| 項目 | コスト |
|------|--------|
| GitHub Actions | 無料（月2,000分まで。本ツールは月90分程度） |
| Google Cloud サービスアカウント | 無料 |
| Google Sheets API | 無料（1日あたり300リクエストまで。本ツールは2〜3リクエスト/日） |

**すべて無料枠内で運用できます。**

---

## 8. ファイル構成

```
jtb-coupon-monitor/
├── .github/
│   └── workflows/
│       └── jtb-coupon-monitor.yml   ← GitHub Actions 設定
├── jtb_coupon_monitor.py            ← メインスクリプト（スクレイピング＋比較）
├── export_to_sheets.py              ← Google Sheets 書き出し
├── .gitignore
└── jtb_coupon_data/                 ← 自動生成されるデータフォルダ
    ├── coupons_2026-02-07.json
    ├── coupons_2026-02-08.json
    └── reports/
        ├── report_2026-02-08.txt
        └── report_2026-02-09.txt
```
