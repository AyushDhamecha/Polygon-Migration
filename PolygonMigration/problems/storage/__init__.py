from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from .base import StorageBackend


def get_storage() -> StorageBackend:
    """Build the backend selected by STORAGE_PROVIDER. Provider SDKs are imported lazily."""
    provider = (settings.STORAGE_PROVIDER or '').strip().lower()

    if provider == 'azure':
        from .azure_blob import AzureBlobStorage
        return AzureBlobStorage(
            account_url=settings.AZURE_STORAGE_ACCOUNT_URL,
            tenant_id=settings.AZURE_TENANT_ID,
            client_id=settings.AZURE_CLIENT_ID,
            username=settings.AZURE_USERNAME,
            password=settings.AZURE_PASSWORD,
            container_name=settings.AZURE_CONTAINER_NAME,
        )

    if provider == 'gdrive':
        from .google_drive import GoogleDriveStorage
        return GoogleDriveStorage(
            credentials_file=settings.GOOGLE_DRIVE_CREDENTIALS_FILE,
            token_file=settings.GOOGLE_DRIVE_TOKEN_FILE,
            root_folder_id=settings.GOOGLE_DRIVE_FOLDER_ID,
        )

    raise ImproperlyConfigured(f"Unsupported STORAGE_PROVIDER '{provider}'. Use 'azure' or 'gdrive'.")
