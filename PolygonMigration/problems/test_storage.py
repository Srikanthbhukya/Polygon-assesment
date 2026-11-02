from unittest import mock

from django.test import SimpleTestCase, override_settings

from . import storage
from .S3Testcase import S3StorageManager
from .polygon_api import PolygonAPI


class FakeS3Client:
    """A tiny in-memory stand-in for a boto3 S3 client (no network, no credentials needed)."""

    def __init__(self):
        self.objects = {}  # (bucket, key) -> bytes

    def put_object(self, Bucket, Key, Body):
        self.objects[(Bucket, Key)] = Body

    def delete_object(self, Bucket, Key):
        self.objects.pop((Bucket, Key), None)

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        client = self

        class Paginator:
            def paginate(self, Bucket, Prefix):
                keys = sorted(k for (b, k) in client.objects if b == Bucket and k.startswith(Prefix))
                yield {"Contents": [{"Key": k} for k in keys]}

        return Paginator()


class RecordingBackend(storage.StorageBackend):
    """Records the calls the migration flow makes."""

    def __init__(self):
        self.calls = []

    def upload_test_case(self, container_name, db_problem_id, test_number, input_data, output_data):
        self.calls.append(("upload_test_case", container_name, db_problem_id, test_number, input_data, output_data))

    def upload_file(self, container_name, blob_name, data):
        self.calls.append(("upload_file", container_name, blob_name, data))

    def empty_blob(self, container_name, problem_id):
        self.calls.append(("empty_blob", container_name, problem_id))


class S3StorageManagerTests(SimpleTestCase):
    def setUp(self):
        self.client = FakeS3Client()
        self.s3 = S3StorageManager(None, None, None, client=self.client)

    def test_upload_test_case_uses_required_layout(self):
        self.s3.upload_test_case("bucket", 7, 1, "1 2\n", "3\n")
        self.assertEqual(self.client.objects[("bucket", "test_cases/7/01")], b"1 2\n")
        self.assertEqual(self.client.objects[("bucket", "test_cases/7/01.a")], b"3\n")

    def test_test_numbers_are_zero_padded_to_two_digits(self):
        self.s3.upload_test_case("bucket", 7, 12, "x", "y")
        self.assertIn(("bucket", "test_cases/7/12"), self.client.objects)
        self.assertIn(("bucket", "test_cases/7/12.a"), self.client.objects)

    def test_empty_blob_only_removes_the_given_problem(self):
        self.s3.upload_test_case("bucket", 7, 1, "a", "b")
        self.s3.upload_test_case("bucket", 70, 1, "a", "b")  # must not match prefix test_cases/7/
        self.s3.empty_blob("bucket", 7)
        self.assertEqual(sorted(k for (_, k) in self.client.objects), ["test_cases/70/01", "test_cases/70/01.a"])

    def test_upload_file_accepts_text_and_bytes(self):
        self.s3.upload_file("bucket", "test_cases/7/custom_checker.cpp", "int main(){}")
        self.s3.upload_file("bucket", "test_cases/7/custom_checker", b"\x7fELF")
        self.assertEqual(self.client.objects[("bucket", "test_cases/7/custom_checker.cpp")], b"int main(){}")
        self.assertEqual(self.client.objects[("bucket", "test_cases/7/custom_checker")], b"\x7fELF")

    def test_upload_failure_is_raised_not_swallowed(self):
        broken = mock.Mock()
        broken.put_object.side_effect = RuntimeError("boom")
        s3 = S3StorageManager(None, None, None, client=broken)
        with self.assertRaises(RuntimeError):
            s3.upload_test_case("bucket", 7, 1, "a", "b")

    def test_missing_credentials_are_rejected(self):
        with self.assertRaises(ValueError):
            S3StorageManager("https://example.com", "", "", "us-east-1")


class ProviderSwitchTests(SimpleTestCase):
    @override_settings(
        STORAGE_PROVIDER="s3",
        AWS_ACCESS_KEY_ID="test-key",
        AWS_SECRET_ACCESS_KEY="test-secret",
        AWS_REGION="us-east-1",
        S3_ENDPOINT_URL="https://example.invalid/storage/v1/s3",
        AWS_S3_BUCKET_NAME="testcases",
    )
    def test_s3_provider_is_selected_from_settings(self):
        self.assertIsInstance(storage.get_storage(), S3StorageManager)
        self.assertEqual(storage.get_container_name(), "testcases")

    @override_settings(STORAGE_PROVIDER="azure", AZURE_CONTAINER_NAME="az-container")
    def test_azure_container_name_is_used_for_azure(self):
        self.assertEqual(storage.get_container_name(), "az-container")

    @override_settings(STORAGE_PROVIDER="ftp")
    def test_unknown_provider_is_rejected(self):
        with self.assertRaises(ValueError):
            storage.get_storage()
        with self.assertRaises(ValueError):
            storage.get_container_name()


@override_settings(POLYGON_API_KEY="k", POLYGON_API_SECRET="s", CUSTOM_CHECKER_DIR=None)
class MigrationFlowTests(SimpleTestCase):
    def test_migrate_uploads_only_complete_tests_after_clearing_old_files(self):
        backend = RecordingBackend()
        cases = [
            {"input": "1 2\n", "output": "3\n"},
            {"input": "5 5\n", "output": ""},  # incomplete: must be skipped
            {"input": "7 8\n", "output": "15\n"},
        ]
        api = PolygonAPI()
        with mock.patch("problems.polygon_api.get_storage", return_value=backend), \
                mock.patch.object(PolygonAPI, "get_test_cases_from_redis", return_value=cases), \
                mock.patch.object(PolygonAPI, "get_custom_checker_info", return_value=None):
            api.migrate_to_cloud_storage("620749", "testcases", 9)

        self.assertEqual(backend.calls[0], ("empty_blob", "testcases", 9))
        uploads = [c for c in backend.calls if c[0] == "upload_test_case"]
        self.assertEqual([c[3] for c in uploads], [1, 3])
        self.assertEqual(uploads[0][1:3], ("testcases", 9))

    def test_delete_problem_files_never_raises(self):
        api = PolygonAPI()
        with mock.patch("problems.polygon_api.get_storage", side_effect=RuntimeError("no credentials")):
            api.delete_problem_files(9)  # must only log the failure
