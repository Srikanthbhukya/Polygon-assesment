"""
Storage layer for test-case files.

The migration flow (polygon_api.py / views.py) only talks to the small interface defined
here, so the cloud provider can be changed from the .env file without touching that code.

Supported providers (selected with STORAGE_PROVIDER in .env):
    azure -> AzureBlobManager   (problems/AzureTestcase.py)
    s3    -> S3StorageManager   (problems/S3Testcase.py); works with any S3-compatible
             service: AWS S3, Supabase Storage, Cloudflare R2, MinIO, ...

Files are always laid out the same way, whatever the provider:
    test_cases/{problem_id}/{NN}      input file
    test_cases/{problem_id}/{NN}.a    output (answer) file
"""
import logging
from abc import ABC, abstractmethod

from django.conf import settings

logger = logging.getLogger(__name__)

SUPPORTED_PROVIDERS = ("azure", "s3")


def test_case_keys(db_problem_id, test_number):
    """Returns (input_key, output_key) for a test case. Shared so every provider uses one layout."""
    base = f"test_cases/{db_problem_id}/{test_number:02d}"
    return base, f"{base}.a"


def problem_prefix(problem_id):
    """Key prefix that holds every file of one problem (note the trailing slash)."""
    return f"test_cases/{problem_id}/"


class StorageBackend(ABC):
    """What the migration flow needs from a storage provider.

    `container_name` is the Azure container or the S3 bucket.
    """

    @abstractmethod
    def upload_test_case(self, container_name, db_problem_id, test_number, input_data, output_data):
        """Store one test case's input and output (strings) under the standard layout."""

    @abstractmethod
    def upload_file(self, container_name, blob_name, data):
        """Store raw bytes at an exact key (used for the custom checker)."""

    @abstractmethod
    def empty_blob(self, container_name, problem_id):
        """Delete every file stored for a problem."""


def get_provider_name():
    """The provider chosen in .env (defaults to azure, the starter code's original behaviour)."""
    return (getattr(settings, "STORAGE_PROVIDER", None) or "azure").strip().lower()


def get_container_name():
    """The container/bucket name for the active provider."""
    provider = get_provider_name()
    if provider == "azure":
        return settings.AZURE_CONTAINER_NAME
    if provider == "s3":
        return settings.AWS_S3_BUCKET_NAME
    raise ValueError(
        f"Unsupported STORAGE_PROVIDER '{provider}'. Supported: {', '.join(SUPPORTED_PROVIDERS)}."
    )


def get_storage():
    """Builds the storage backend selected by STORAGE_PROVIDER. Provider libraries are imported
    lazily, so only the one in use has to be installed and configured."""
    provider = get_provider_name()
    logger.info("Using storage provider: %s", provider)

    if provider == "azure":
        from .AzureTestcase import AzureBlobManager

        return AzureBlobManager(
            account_url=settings.AZURE_STORAGE_ACCOUNT_URL,
            tenant_id=settings.AZURE_TENANT_ID,
            client_id=settings.AZURE_CLIENT_ID,
            username=settings.AZURE_USERNAME,
            password=settings.AZURE_PASSWORD,
        )

    if provider == "s3":
        from .S3Testcase import S3StorageManager

        return S3StorageManager(
            endpoint_url=getattr(settings, "S3_ENDPOINT_URL", None),
            access_key_id=getattr(settings, "AWS_ACCESS_KEY_ID", None),
            secret_access_key=getattr(settings, "AWS_SECRET_ACCESS_KEY", None),
            region_name=getattr(settings, "AWS_REGION", None),
        )

    raise ValueError(
        f"Unsupported STORAGE_PROVIDER '{provider}'. Supported: {', '.join(SUPPORTED_PROVIDERS)}."
    )
