#!/usr/bin/env python3
"""
OAuth2 リフレッシュトークン取得スクリプト（ローカルで1回だけ実行）

手順:
  1. GCP コンソール → APIとサービス → 認証情報 → OAuth 2.0 クライアントID を作成
     - 種類: 「デスクトップアプリ」
     - Google Drive API を有効化しておくこと
  2. クライアントID / シークレット を以下の環境変数にセットして実行:
       export GDRIVE_OAUTH_CLIENT_ID="..."
       export GDRIVE_OAUTH_CLIENT_SECRET="..."
       python get_refresh_token.py
  3. ブラウザが開くので Google アカウントで認証
  4. 表示されたリフレッシュトークンを GitHub Secrets に登録:
       gh secret set GDRIVE_OAUTH_REFRESH_TOKEN --repo hummingbirdconnect-llc/jtb-coupon-monitor

必要パッケージ:
  pip install google-auth-oauthlib
"""

import os
import sys

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    print("❌ google-auth-oauthlib が必要です:")
    print("   pip install google-auth-oauthlib")
    sys.exit(1)

SCOPES = ["https://www.googleapis.com/auth/drive"]


def main():
    client_id = os.environ.get("GDRIVE_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("GDRIVE_OAUTH_CLIENT_SECRET")

    if not client_id or not client_secret:
        print("❌ 環境変数を設定してください:")
        print("   export GDRIVE_OAUTH_CLIENT_ID='...'")
        print("   export GDRIVE_OAUTH_CLIENT_SECRET='...'")
        sys.exit(1)

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")

    print("\n" + "=" * 60)
    print("✅ 認証成功！以下のリフレッシュトークンを GitHub Secrets に登録してください")
    print("=" * 60)
    print(f"\nGDRIVE_OAUTH_REFRESH_TOKEN:\n{creds.refresh_token}")
    print(f"\n登録コマンド:")
    print(f"  gh secret set GDRIVE_OAUTH_REFRESH_TOKEN --repo hummingbirdconnect-llc/jtb-coupon-monitor")
    print()


if __name__ == "__main__":
    main()
