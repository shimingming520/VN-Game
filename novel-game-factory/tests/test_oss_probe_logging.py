import logging

import oss2

from common import oss_utils


def _no_such_key():
    return oss2.exceptions.NoSuchKey(
        404,
        {},
        b"",
        {"Code": "NoSuchKey", "Message": "The specified key does not exist."},
    )


class _MissingObjectBucket:
    def get_object_meta(self, key):
        # The real oss2 client emits this INFO record before raising.  Emit the
        # same record here so the regression test covers our SDK-boundary filter.
        logging.getLogger("oss2.api").info("Exception: %s", _no_such_key())
        raise _no_such_key()


class _BrokenMetadataBucket:
    def get_object_meta(self, key):
        error = RuntimeError("temporary metadata failure")
        logging.getLogger("oss2.api").info("Exception: %s", error)
        raise error


class _MissingObjectExistsBucket:
    def object_exists(self, key):
        logging.getLogger("oss2.api").info("Exception: %s", _no_such_key())
        return False


class _MissingBucketMetadataBucket:
    def get_object_meta(self, key):
        error = oss2.exceptions.NoSuchBucket(
            404,
            {},
            b"",
            {"Code": "NoSuchBucket", "Message": "The specified bucket does not exist."},
        )
        logging.getLogger("oss2.api").info("Exception: %s", error)
        raise error


class _ExistingObjectBucket:
    def get_object_meta(self, key):
        return type("Meta", (), {"content_length": 1234})()


def test_missing_object_probe_is_quiet_and_returns_minus_one(monkeypatch, caplog):
    monkeypatch.delenv("OSS_DISABLED", raising=False)
    monkeypatch.setattr(oss_utils, "_client", _MissingObjectBucket())
    caplog.set_level(logging.DEBUG)

    assert oss_utils.oss_get_size("storage/outputs/task/new.png") == -1

    sdk_records = [
        record
        for record in caplog.records
        if record.name == "oss2.api" and record.getMessage().startswith("Exception:")
    ]
    assert sdk_records == []
    assert any(
        record.name == "common.oss_utils"
        and "远端对象不存在，准备首次上传" in record.getMessage()
        for record in caplog.records
    )


def test_existing_object_probe_keeps_size_result(monkeypatch):
    monkeypatch.delenv("OSS_DISABLED", raising=False)
    monkeypatch.setattr(oss_utils, "_client", _ExistingObjectBucket())

    assert oss_utils.oss_get_size("storage/outputs/task/existing.png") == 1234


def test_non_missing_metadata_error_remains_visible(monkeypatch, caplog):
    monkeypatch.delenv("OSS_DISABLED", raising=False)
    monkeypatch.setattr(oss_utils, "_client", _BrokenMetadataBucket())
    caplog.set_level(logging.INFO)

    assert oss_utils.oss_get_size("storage/outputs/task/new.png") == -1

    assert any(
        record.name == "common.oss_utils"
        and "OSS 元数据查询失败" in record.getMessage()
        for record in caplog.records
    )
    assert any(
        record.name == "oss2.api"
        and record.getMessage().startswith("Exception:")
        for record in caplog.records
    )


def test_missing_object_exists_probe_is_quiet(monkeypatch, caplog):
    monkeypatch.delenv("OSS_DISABLED", raising=False)
    monkeypatch.setattr(oss_utils, "_client", _MissingObjectExistsBucket())
    caplog.set_level(logging.INFO)

    assert oss_utils.oss_exists("storage/outputs/task/new.png") is False
    assert not any(
        record.name == "oss2.api" and "NoSuchKey" in record.getMessage()
        for record in caplog.records
    )


def test_missing_bucket_is_not_treated_as_missing_object(monkeypatch, caplog):
    monkeypatch.delenv("OSS_DISABLED", raising=False)
    monkeypatch.setattr(oss_utils, "_client", _MissingBucketMetadataBucket())
    caplog.set_level(logging.INFO)

    assert oss_utils.oss_get_size("storage/outputs/task/new.png") == -1
    assert any(
        record.name == "common.oss_utils"
        and "OSS 元数据查询失败" in record.getMessage()
        for record in caplog.records
    )
    assert any(
        record.name == "oss2.api"
        and "NoSuchBucket" in record.getMessage()
        for record in caplog.records
    )
