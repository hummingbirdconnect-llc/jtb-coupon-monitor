# JTB クーポン自動監視 — 運用マニュアル（ライフサイクル追跡版）

> **リポジトリ**: https://github.com/hummingbirdconnect-llc/jtb-coupon-monitor
>
> **最終更新**: 2026年2月7日

---

## 1. システム全体像

```
┌─ GitHub Actions（毎朝9:00 JST 自動実行）────────────────┐
│                                                           │
│  ① JTBクーポンページをスクレイピング                      │
│     ├─ 国内クーポン一覧 + 各詳細ページ                   │
│     └─ 海外クーポン一覧 + 各詳細ページ                   │
│                                                           │
│  ② マスター台帳と照合してステータスを判定                │
│     ├─ 🟢 配布中:   ページにあり、終了表記なし            │
│     ├─ 🔴 配布終了: ページに「配布終了」表記あり          │
│     ├─ ⚫ ページ消滅: 前日まであったが今日消えた           │
│     └─ 🔄 復活:    一度終了/消滅したが再び配布中          │
│                                                           │
│  ③ 結果を保存                                            │
│     ├─ GitHub: マスター台帳 + 日次JSON + レポート         │
│     └─ Google Sheets: 3つのシート                         │
│        ├─ 「現在のクーポン」← 今すぐ取得可能なもの       │
│        ├─ 「マスター台帳」 ← 全件（終了・消滅含む）     │
│        └─ 「変動ログ」    ← 日々の追加・終了・復活の履歴│
└───────────────────────────────────────────────────────────┘

監視対象URL:
  国内: https://www.jtb.co.jp/myjtb/campaign/coupon/
  海外: https://www.jtb.co.jp/myjtb/campaign/kaigaicoupon/
```

### 従来版との違い

| | 従来版（スナップショット方式） | 新版（ライフサイクル追跡） |
|---|---|---|
| データ構造 | 毎日のデータは独立ファイル | マスター台帳に全履歴を蓄積 |
| 配布終了の扱い | 「消えた=削除」としてログに記録 | 終了/消滅を区別して保持し続ける |
| 復活の検出 | 不可能（過去との紐付けがない） | 同じIDで再登場すれば自動検出 |
| Sheetsの見え方 | 全件一覧（配布中のみ） | 「今取れるもの」と「全件」を分離 |

### 生成されるファイル

| 場所 | ファイル | 内容 |
|------|---------|------|
| GitHub | `jtb_coupon_data/master_coupons.json` | **マスター台帳**（核心。全履歴を保持） |
| GitHub | `jtb_coupon_data/coupons_YYYY-MM-DD.json` | その日のスクレイピング生データ |
| GitHub | `jtb_coupon_data/reports/report_YYYY-MM-DD.txt` | 日次レポート |
| Google Sheets | 「現在のクーポン」シート | 🟢配布中 + 🔄復活 のみ（毎日上書き） |
| Google Sheets | 「マスター台帳」シート | 全件・全ステータス（色分け付き） |
| Google Sheets | 「変動ログ」シート | 追加・終了・復活・消滅の時系列（追記） |

---

## 2. 毎日の運用

### 通常時: 何もしなくてOK

毎朝9時にGitHub Actionsが自動実行されます。

### 結果の確認（Google Sheets）

**「現在のクーポン」シートを開くだけで、今取得可能なクーポンが全部わかります。**

```
ステータス | カテゴリ | タイトル                          | 割引額          | クーポンコード
🟢 配布中  | 国内    | 3月までの国内宿泊に使える割引クーポン | 最大3,000円引   | SPRING2026
🟢 配布中  | 海外    | 海外航空券+ホテルで使える割引クーポン | 最大200,000円引 | QF2026
🔄 復活    | 国内    | 全国のヒルトンで使える割引クーポン   | 最大20,000円引  | HILTON26
```

**「マスター台帳」シートは全件（終了・消滅含む）の台帳です。**

```
ステータス   | カテゴリ | タイトル                   | 初回検出日  | 最終確認日 | ステータス履歴
🟢 配布中    | 国内    | 3月までの国内宿泊クーポン   | 2026-02-07 | 2026-02-10 | 2026-02-07:🟢配布中
🔄 復活      | 国内    | ヒルトン割引クーポン        | 2026-01-15 | 2026-02-10 | 01-15:🟢配布中 → 02-01:🔴配布終了 → 02-08:🔄復活
🔴 配布終了  | 海外    | ハワイ旅行特別クーポン      | 2026-01-10 | 2026-02-05 | 01-10:🟢配布中 → 02-05:🔴配布終了
⚫ ページ消滅 | 国内    | 冬のスキー割引クーポン      | 2025-12-01 | 2026-02-03 | 12-01:🟢配布中 → 02-03:⚫ページ消滅
```

色分け: 🔴終了=薄い赤、⚫消滅=グレー、🔄復活=薄い青、🟢配布中=白（デフォルト）

**「変動ログ」シートは日々の変化の時系列記録です。**

```
日付        | カテゴリ | 種別         | タイトル              | 配布中 | 配布終了 | 消滅 | 全件数
2026-02-07 |         | ─ 変化なし   |                      | 25    | 3       | 2   | 30
2026-02-08 | 海外    | 🆕 新規      | 春のハワイクーポン     | 26    | 3       | 2   | 31
2026-02-09 | 国内    | 🔴 配布終了  | 冬のスキー割引クーポン | 25    | 4       | 2   | 31
2026-02-10 | 国内    | 🔄 復活      | ヒルトン割引クーポン   | 26    | 3       | 2   | 31
```

---

## 3. よく使う操作

### 3-1. 手動で今すぐ実行

1. https://github.com/hummingbirdconnect-llc/jtb-coupon-monitor/actions
2. 「JTB Coupon Monitor」→「Run workflow」→「Run workflow」

### 3-2. ターミナルで確認

```bash
cd ~/jtb-coupon-monitor

# 今の状態をサマリー表示（スクレイピングなし・ローカルのマスター台帳を参照）
python3 jtb_coupon_monitor.py --status

# 手動でフル実行
python3 jtb_coupon_monitor.py
```

### 3-3. 実行時間の変更

`.github/workflows/jtb-coupon-monitor.yml` の `cron` を編集:

```yaml
# 朝7時（JST）に変更 → UTC 22:00
- cron: '0 22 * * *'

# 1日2回（朝9時 + 夕方18時）
- cron: '0 0,9 * * *'
```

### 3-4. 一時停止/再開

Actions タブ → 左サイドバー「JTB Coupon Monitor」→ 右上「...」→「Disable workflow」

---

## 4. Google Sheets 連携のセットアップ

### 4-1. Google Cloud でサービスアカウントを作成（初回のみ・約10分）

1. https://console.cloud.google.com/ にアクセス
2. プロジェクト作成（名前: `jtb-coupon-monitor` 等）
3. 「APIとサービス」→「ライブラリ」で **Google Sheets API** と **Google Drive API** を有効化
4. 「認証情報」→「+ 認証情報を作成」→「サービスアカウント」作成
5. サービスアカウント → 「キー」タブ → JSON キーをダウンロード
6. サービスアカウントのメールアドレスをメモ（`xxx@xxx.iam.gserviceaccount.com`）

### 4-2. スプレッドシートを準備

1. https://sheets.google.com で新規作成（名前: 「JTBクーポン監視」等）
2. 右上「共有」→ サービスアカウントのメールアドレスを「編集者」で追加
3. URLからスプレッドシートIDをメモ（`/d/` と `/edit` の間の文字列）

### 4-3. GitHub にシークレットを登録

https://github.com/hummingbirdconnect-llc/jtb-coupon-monitor/settings/secrets/actions

| Name | Secret |
|------|--------|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | JSONファイルの中身をまるごとコピペ |
| `SPREADSHEET_ID` | スプレッドシートID |

ターミナルでJSONをクリップボードにコピー:
```bash
cat ~/Downloads/jtb-coupon-monitor-xxxxx.json | pbcopy
```

### 4-4. ファイルをリポジトリに追加

```bash
cd ~/jtb-coupon-monitor

# ダウンロードしたファイルで上書き
cp ~/Downloads/jtb_coupon_monitor.py ./
cp ~/Downloads/export_to_sheets.py ./
cp ~/Downloads/jtb-coupon-monitor.yml .github/workflows/

# GitHubに反映
git add .
git commit -m "ライフサイクル追跡版にアップグレード"
git push
```

### 4-5. テスト実行

GitHub → Actions → 「Run workflow」で手動実行。全ステップが ✅ なら成功。

---

## 5. ステータス判定ロジック（詳細）

```
毎日のスクレイピング結果
        │
        ▼
ページに存在するか？ ─── No ──→ 前日まで台帳にあったか？
        │                              │
       Yes                            Yes → ⚫ ページ消滅
        │                              No  → （何もしない）
        ▼
「配布終了」表記があるか？
        │
       Yes → 🔴 配布終了
        │
       No
        │
        ▼
予約対象期間が過ぎているか？
        │
       Yes → 🔴 配布終了（期間切れ）
        │
       No
        │
        ▼
前回のステータスは「終了」or「消滅」だったか？
        │
       Yes → 🔄 復活
        │
       No  → 🟢 配布中
```

JTBのクーポンページには注意書きとして「クーポン利用枚数が上限に達した場合、更新タイミングによって配布終了表記がない場合がございます」とあります。つまり「配布終了」表記が出るケースと、**表記なしでいきなりページから消えるケース**の両方があります。このスクリプトは両方を検出します。

---

## 6. トラブルシューティング

| 症状 | 原因 | 対処 |
|------|------|------|
| Actions が失敗 | 権限不足 | `permissions: contents: write` を確認 |
| Sheets更新されない | 認証エラー | `GOOGLE_SERVICE_ACCOUNT_JSON` の中身を再確認 |
| 0件になる | HTML構造変更 | Claudeに相談してスクリプト修正 |
| 配布終了が検出されない | 表記パターンの変更 | `ended_keywords` リストに追加 |
| マスター台帳が壊れた | JSONエラー | `master_coupons.json` を削除して `--init` で再作成 |

### 確認コマンド集

```bash
# ローカルでフル実行テスト
python3 jtb_coupon_monitor.py

# マスター台帳のサマリーだけ表示
python3 jtb_coupon_monitor.py --status

# 一覧だけ高速取得（詳細ページはスキップ）
python3 jtb_coupon_monitor.py --list-only

# マスター台帳のリセット（最初からやり直し）
rm jtb_coupon_data/master_coupons.json
python3 jtb_coupon_monitor.py --init
```

---

## 7. データの活用方法

### 記事作成への活用

- **「今月のJTBクーポンまとめ」記事**: 「現在のクーポン」シートをそのまま記事化
- **「先月終了したクーポン」**: マスター台帳でフィルターすれば一発
- **「復活クーポン速報」**: 変動ログの🔄復活をウォッチ → SNS投稿ネタ
- **「過去の傾向分析」**: ステータス履歴から「毎月月初に出るクーポン」等のパターン発見

### 他OTAへの横展開

このスクリプトの構造（スクレイピング → マスター台帳で追跡 → Sheets書き出し）は、エクスペディアやアゴダなど他OTAにも応用可能。

---

## 8. コスト

すべて無料枠内で運用可能。

| 項目 | コスト |
|------|--------|
| GitHub Actions | 無料（月2,000分。本ツールは月90分程度） |
| Google Cloud | 無料 |
| Google Sheets API | 無料（1日300リクエスト。本ツールは3〜4リクエスト/日） |

---

## 9. ファイル構成

```
jtb-coupon-monitor/
├── .github/
│   └── workflows/
│       └── jtb-coupon-monitor.yml    ← GitHub Actions 設定
├── jtb_coupon_monitor.py             ← メインスクリプト（スクレイピング + マスター更新）
├── export_to_sheets.py               ← Google Sheets 書き出し（3シート）
├── .gitignore
└── jtb_coupon_data/                  ← 自動生成
    ├── master_coupons.json           ← ★ マスター台帳（全履歴）
    ├── coupons_2026-02-07.json
    ├── coupons_2026-02-08.json
    └── reports/
        ├── report_2026-02-07.txt
        └── report_2026-02-08.txt
```
