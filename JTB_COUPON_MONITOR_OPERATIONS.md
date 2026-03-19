# JTB・KNT クーポン自動監視 — 運用マニュアル

> **リポジトリ**: https://github.com/hummingbirdconnect-llc/jtb-coupon-monitor
>
> **最終更新**: 2026年3月7日

---

## 1. システム全体像

```
┌─ GitHub Actions（毎朝6:00 JST 自動実行）────────────────┐
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

毎朝6時にGitHub Actionsが自動実行されます。変動があればメール通知されます。

### 結果の確認（Google Sheets）

**「JTB_現在のクーポン」シートを開くだけで、今取得可能なクーポンが全部わかります。**

- 🟢 配布中のクーポンが上に、🔴 配布終了が下に表示
- 配布終了行は薄赤で色付け
- 割引額・クーポンコード・パスワード・期間を一覧表示

---

## 3. よく使う操作

### 3-1. 手動で今すぐ実行

1. https://github.com/hummingbirdconnect-llc/jtb-coupon-monitor/actions
2. 「Coupon Monitor (JTB + KNT + HIS)」→「Run workflow」→「Run workflow」

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
# 朝6時（JST）に変更 → UTC 21:00
- cron: '0 21 * * *'

# 1日2回（朝6時 + 夕方18時）
- cron: '0 9,21 * * *'
```

### 3-4. 一時停止/再開

Actions タブ → 左サイドバー「Coupon Monitor (JTB + KNT + HIS)」→ 右上「...」→「Disable workflow」

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

すべて無料枠内で運用可能。

| 項目 | コスト |
|------|--------|
| GitHub Actions | 無料（月2,000分。本ツールは月90分程度） |
| Google Cloud | 無料 |
| Google Sheets API | 無料（1日300リクエスト。本ツールは約8リクエスト/日） |
| Gmail SMTP | 無料（1日500通。本ツールは最大1通/日） |
