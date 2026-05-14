# WP下書き更新ボタンの設定メモ

ダッシュボードの「WP下書き更新」タブは、公開ページに秘密情報を置かないため、Cloudflare Worker などの中継APIを経由して GitHub Actions を起動します。

## 1. GitHub Secrets

Repository secrets に次を設定します。

- `YF_WP_URL`, `YF_WP_USER`, `YF_WP_APP_PASSWORD`: 屋久島ファンの WordPress REST API 用
- `WT_WP_URL`, `WT_WP_USER`, `WT_WP_APP_PASSWORD`: ウェルトリップの WordPress REST API 用
- `JTB_AFFILIATE_CONFIG`, `HIS_AFFILIATE_CONFIG`, `KNT_AFFILIATE_CONFIG`: 各アフィリエイト設定 JSON
- `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `NOTIFY_EMAIL`: 実行結果通知用

## 2. Cloudflare Worker

`workers/wp-coupon-update-dispatcher.js` を Worker に配置し、環境変数または secret として次を設定します。

- `GITHUB_TOKEN`: `actions:write` が可能な GitHub fine-grained token
- `ADMIN_KEY`: ダッシュボードから入力する管理用合言葉
- `GITHUB_REF`: 通常は `main`。検証ブランチを使う場合だけ変更
- `ALLOWED_ORIGINS`: 任意。例 `https://hummingbirdconnect-llc.github.io`

Worker は `site_id` と `page_slug` をホワイトリストで検証し、不正なサイトID・slug・合言葉を拒否します。

## 3. ダッシュボードでの使い方

1. ダッシュボードを開き、「WP下書き更新」タブを選びます。
2. 中継API URL と管理用合言葉を入力します。値は同じブラウザの `localStorage` に保存されます。
3. サイトと記事を選び、まずは `dry-run` のまま実行します。
4. GitHub Actions の実行結果と `wp_update_result.json` を確認します。
5. 問題がなければ `dry-run` を外して実行します。

## 4. 安全仕様

- 公開記事は直接更新せず、`<slug>-coupon-update` という下書きコピーを作成または更新します。
- 元記事が下書きの場合だけ、その下書きを直接更新します。
- 変更対象は Gutenberg の table `tbody` のみです。
- H1/H2/H3、本文、FAQ、CTA、内部リンク、アフィリエイトURL、計測タグが変わる場合は停止します。
- テーブル行が0件になる、または50%以上減る場合は停止します。
- 未マッチクーポン、ブロック、エラーがある場合、GitHub Actions は失敗扱いになります。
