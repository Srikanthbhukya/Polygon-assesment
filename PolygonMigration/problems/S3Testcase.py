import logging

import boto3
from botocore.config import Config

from .storage import StorageBackend, problem_prefix, test_case_keys

logger = logging.getLogger(__name__)


class S3StorageManager(StorageBackend):
    """
    Stores test cases in any S3-compatible object store (AWS S3, Supabase Storage,
    Cloudflare R2, MinIO, ...). The service is chosen purely by configuration:
    set `endpoint_url` for non-AWS services and leave it empty for AWS S3.
    """

    def __init__(self, endpoint_url, access_key_id, secret_access_key, region_name=None, client=None):
        """
        Args:
            endpoint_url (str|None): The service's S3 endpoint, e.g.
                https://<project-ref>.storage.supabase.co/storage/v1/s3. None for AWS S3.
            access_key_id (str): S3 access key ID.
            secret_access_key (str): S3 secret access key.
            region_name (str|None): Region of the service.
            client: An existing boto3 S3 client (used by tests); built from the other args if omitted.

        Raises:
            ValueError: If the credentials are missing.
        """
        if client is not None:
            self.client = client
            return
        if not access_key_id or not secret_access_key:
            raise ValueError(
                "S3 credentials are missing. Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY in .env."
            )
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region_name or None,
            config=Config(
                signature_version="s3v4",
                # Third-party S3 services expect the bucket in the path, not in the hostname.
                s3={"addressing_style": "path" if endpoint_url else "auto"},
            ),
        )
        logger.info("S3 client created (endpoint=%s)", endpoint_url or "AWS default")

    def upload_file(self, container_name, blob_name, data):
        """Uploads raw bytes (or a string) to `blob_name` in the bucket, replacing any existing file."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        try:
            self.client.put_object(Bucket=container_name, Key=blob_name, Body=data)
        except Exception as e:
            logger.error("Error uploading %s to bucket %s: %s", blob_name, container_name, e)
            raise
        logger.info("Uploaded %s (%d bytes)", blob_name, len(data))

    def upload_test_case(self, container_name, db_problem_id, test_number, input_data, output_data):
        """Uploads a test case's input and output as two files. Raises if either upload fails,
        so the migration is reported as failed instead of silently losing a test."""
        input_key, output_key = test_case_keys(db_problem_id, test_number)
        self.upload_file(container_name, input_key, input_data.encode("utf-8"))
        self.upload_file(container_name, output_key, output_data.encode("utf-8"))
        logger.info("Uploaded test #%s of problem %s", test_number, db_problem_id)

    def empty_blob(self, container_name, problem_id):
        """Deletes every file under test_cases/{problem_id}/ in the bucket."""
        prefix = problem_prefix(problem_id)
        deleted = 0
        try:
            paginator = self.client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=container_name, Prefix=prefix):
                for obj in page.get("Contents", []):
                    logger.info("Deleting object: %s", obj["Key"])
                    self.client.delete_object(Bucket=container_name, Key=obj["Key"])
                    deleted += 1
        except Exception as e:
            logger.error("Error deleting objects for problem %s: %s", problem_id, e)
            raise
        logger.info("Deleted %d objects for problem %s in bucket %s", deleted, problem_id, container_name)
