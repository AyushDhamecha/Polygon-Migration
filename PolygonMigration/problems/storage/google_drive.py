import io
import json
import logging
import os

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as UserCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from .base import StorageBackend

logger = logging.getLogger(__name__)

# Full scope is needed to write into a folder the user created (drive.file only sees app-created files).
SCOPES = ['https://www.googleapis.com/auth/drive']
FOLDER_MIME = 'application/vnd.google-apps.folder'
RESUMABLE_THRESHOLD = 5 * 1024 * 1024
NUM_RETRIES = 3


def load_credentials(credentials_file, token_file):
    """
    Service-account JSON is used directly (only works with Shared Drives - service accounts
    have no My Drive quota). An OAuth client JSON requires a token created by `manage.py gdrive_auth`.
    """
    with open(credentials_file, encoding='utf-8') as f:
        info = json.load(f)

    if info.get('type') == 'service_account':
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)

    if not token_file or not os.path.exists(token_file):
        raise RuntimeError(
            "Google Drive OAuth token not found. Run `python manage.py gdrive_auth` "
            "and set GOOGLE_DRIVE_TOKEN_FILE."
        )
    creds = UserCredentials.from_authorized_user_file(token_file, SCOPES)
    if not creds.valid:
        if not (creds.expired and creds.refresh_token):
            raise RuntimeError("Google Drive token is invalid. Re-run `python manage.py gdrive_auth`.")
        creds.refresh(Request())
        with open(token_file, 'w', encoding='utf-8') as f:
            f.write(creds.to_json())
    return creds


def _escape(name):
    return name.replace('\\', '\\\\').replace("'", "\\'")


class GoogleDriveStorage(StorageBackend):
    """Maps each '/'-separated path segment to a Drive folder under `root_folder_id`."""

    def __init__(self, credentials_file, token_file, root_folder_id):
        if not credentials_file or not root_folder_id:
            raise RuntimeError("GOOGLE_DRIVE_CREDENTIALS_FILE and GOOGLE_DRIVE_FOLDER_ID must be set.")
        creds = load_credentials(credentials_file, token_file)
        self.service = build('drive', 'v3', credentials=creds, cache_discovery=False)
        self.root_folder_id = root_folder_id
        self._folder_cache = {}

    def _find_child(self, parent_id, name, folder):
        query = f"name = '{_escape(name)}' and '{parent_id}' in parents and trashed = false"
        query += f" and mimeType {'=' if folder else '!='} '{FOLDER_MIME}'"
        result = self.service.files().list(
            q=query,
            fields='files(id)',
            pageSize=1,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute(num_retries=NUM_RETRIES)
        files = result.get('files', [])
        return files[0]['id'] if files else None

    def _resolve_folder(self, segments, create):
        parent_id = self.root_folder_id
        for i, name in enumerate(segments):
            key = tuple(segments[:i + 1])
            folder_id = self._folder_cache.get(key)
            if folder_id is None:
                folder_id = self._find_child(parent_id, name, folder=True)
                if folder_id is None:
                    if not create:
                        return None
                    folder_id = self.service.files().create(
                        body={'name': name, 'mimeType': FOLDER_MIME, 'parents': [parent_id]},
                        fields='id',
                        supportsAllDrives=True,
                    ).execute(num_retries=NUM_RETRIES)['id']
                self._folder_cache[key] = folder_id
            parent_id = folder_id
        return parent_id

    def upload_file(self, path, data):
        *folders, name = path.split('/')
        parent_id = self._resolve_folder(folders, create=True)
        media = MediaIoBaseUpload(
            io.BytesIO(data),
            mimetype='application/octet-stream',
            resumable=len(data) > RESUMABLE_THRESHOLD,
        )
        existing_id = self._find_child(parent_id, name, folder=False)
        if existing_id:
            self.service.files().update(
                fileId=existing_id, media_body=media, supportsAllDrives=True,
            ).execute(num_retries=NUM_RETRIES)
        else:
            self.service.files().create(
                body={'name': name, 'parents': [parent_id]},
                media_body=media,
                fields='id',
                supportsAllDrives=True,
            ).execute(num_retries=NUM_RETRIES)
        logger.info("Uploaded %s (%d bytes) to Google Drive", path, len(data))

    def delete_prefix(self, prefix):
        segments = [s for s in prefix.split('/') if s]
        folder_id = self._resolve_folder(segments, create=False)
        if folder_id is None:
            return
        # Deleting the folder removes everything under it.
        self.service.files().delete(fileId=folder_id, supportsAllDrives=True).execute(num_retries=NUM_RETRIES)
        for key in [k for k in self._folder_cache if k[:len(segments)] == tuple(segments)]:
            del self._folder_cache[key]
        logger.info("Deleted Google Drive folder %s", prefix)
