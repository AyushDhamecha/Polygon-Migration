import logging

from azure.identity import UsernamePasswordCredential
from azure.storage.blob import BlobServiceClient

from .base import StorageBackend

logger = logging.getLogger(__name__)


class AzureBlobStorage(StorageBackend):
    def __init__(self, account_url, tenant_id, client_id, username, password, container_name):
        credential = UsernamePasswordCredential(
            tenant_id=tenant_id,
            client_id=client_id,
            username=username,
            password=password,
        )
        service = BlobServiceClient(account_url=account_url, credential=credential)
        self.container = service.get_container_client(container_name)

    def upload_file(self, path, data):
        self.container.upload_blob(name=path, data=data, overwrite=True)
        logger.info("Uploaded %s (%d bytes) to Azure", path, len(data))

    def delete_prefix(self, prefix):
        names = [b.name for b in self.container.list_blobs(name_starts_with=prefix)]
        for name in names:
            self.container.delete_blob(name)
        logger.info("Deleted %d blobs under %s", len(names), prefix)
