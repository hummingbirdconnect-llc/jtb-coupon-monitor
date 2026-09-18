# 会社別アフィリエイトリンク設定

- 1社1ファイル。ファイル名は `config/provider_registry.json` の会社ID（例: `relux.json`）。
- 形は `_template.json` と同じ。`category_links.default.url` に貼れば、その会社の全クーポンに効きます。
- URLを入れたファイルはGitへ入れません（`.gitignore` 済み）。GitHub Actions では Secrets `AFFILIATE_LINKS_CONFIG`（会社ID→設定 のJSON）から復元します。
- HIS / JTB / KNT は従来の `config/<id>_affiliate_links.json` のままで動きます。
- 枠だけ作り直すとき: `python affiliate_links.py relux booking ...`（既存ファイルは上書きしません）
