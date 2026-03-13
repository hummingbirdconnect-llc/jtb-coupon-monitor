#!/usr/bin/env python3
"""
Google Drive 同期スクリプト

生成した HTML ファイルを Google Drive の指定フォルダにアップロード/上書きする。
既存のサービスアカウント (GOOGLE_SERVICE_ACCOUNT_JSON) を再利用。

環境変数:
    GOOGLE_SERVICE_ACCOUNT_JSON: サービスアカウントのJSON鍵（文字列）
    GDRIVE_FOLDER_ID: アップロード先の Google Drive フォルダID
    GDRIVE_IMPERSONATE_EMAIL: (任意) ドメイン委任で偽装するユーザーのメール

Usage:
    python sync_to_gdrive.py

ファイルマッピング:
    html_output/jtb_coupons.html  → JTB_クーポンリスト.md
    html_output/his_coupons_list.html → HIS_クーポンリスト.md
"""

import json
import os
import sys

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "html_output")

# ローカルファイル → Drive上のファイル名
FILE_MAPPING = {
    "jtb_coupons.html": "JTB_クーポンリスト.md",
    "his_coupons_list.html": "HIS_クーポンリスト.md",
}

SCOPES = ["https://www.googleapis.com/auth/drive"]


# ---------------------------------------------------------------------------
# Google Drive Client
# ---------------------------------------------------------------------------
def get_drive_service():
    """サービスアカウントで認証した Drive API クライアントを返す

    GDRIVE_IMPERSONATE_EMAIL が設定されている場合、ドメイン委任で
    そのユーザーとして操作する（SAのストレージクォータ制限を回避）。
    """
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not sa_json:
        print("❌ GOOGLE_SERVICE_ACCOUNT_JSON が未設定です")
        sys.exit(1)

    sa_info = json.loads(sa_json)
    creds = Credentials.from_service_account_info(sa_info, scopes=SCOPES)

    # ドメイン委任（impersonation）対応
    impersonate_email = os.environ.get("GDRIVE_IMPERSONATE_EMAIL")
    if impersonate_email:
        creds = creds.with_subject(impersonate_email)
        print(f"👤 ドメイン委任: {impersonate_email}")

    return build("drive", "v3", credentials=creds)


def find_file_in_folder(service, folder_id: str, filename: str):
    """フォルダ内で指定ファイル名を検索。見つかればファイルIDを返す"""
    query = (
        f"'{folder_id}' in parents "
        f"and name = '{filename}' "
        f"and trashed = false"
    )
    results = (
        service.files()
        .list(
            q=query,
            fields="files(id, name)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )
    files = results.get("files", [])
    return files[0]["id"] if files else None


def upload_or_update(service, folder_id: str, local_path: str, drive_filename: str):
    """ファイルをアップロード（既存なら上書き、なければ新規作成）"""
    media = MediaFileUpload(local_path, mimetype="text/plain", resumable=True)

    existing_id = find_file_in_folder(service, folder_id, drive_filename)

    if existing_id:
        # 上書き更新
        file = (
            service.files()
            .update(
                fileId=existing_id,
                media_body=media,
                supportsAllDrives=True,
            )
            .execute()
        )
        print(f"  ✅ 更新: {drive_filename} (ID: {existing_id})")
    else:
        # 新規作成
        file_metadata = {
            "name": drive_filename,
            "parents": [folder_id],
        }
        file = (
            service.files()
            .create(
                body=file_metadata,
                media_body=media,
                fields="id",
                supportsAllDrives=True,
            )
            .execute()
        )
        print(f"  ✅ 新規作成: {drive_filename} (ID: {file.get('id')})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    folder_id = os.environ.get("GDRIVE_FOLDER_ID")
    if not folder_id:
        print("❌ GDRIVE_FOLDER_ID が未設定です")
        sys.exit(1)

    service = get_drive_service()
    print(f"📁 Google Drive フォルダ: {folder_id}")

    for local_name, drive_name in FILE_MAPPING.items():
        local_path = os.path.join(OUTPUT_DIR, local_name)
        if not os.path.exists(local_path):
            print(f"  ⚠️  スキップ（ファイルなし）: {local_path}")
            continue
        upload_or_update(service, folder_id, local_path, drive_name)

    print("\n✅ Google Drive 同期完了")


if __name__ == "__main__":
    main()
