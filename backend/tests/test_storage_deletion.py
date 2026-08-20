from __future__ import annotations

import pytest

from app.services import storage


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeBucket:
    def __init__(self, existing: set[str]) -> None:
        self.existing = existing
        self.deleted: list[tuple[str, dict | None]] = []

    def object_exists(self, key: str) -> bool:
        return key in self.existing

    def delete_object(self, key: str, params: dict | None = None) -> None:
        self.deleted.append((key, params))
        self.existing.discard(key)


@pytest.mark.anyio
async def test_delete_oss_object_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    bucket = FakeBucket({"raw/test.eml"})
    monkeypatch.setattr(storage.settings, "OSS_ACCESS_KEY", "test")
    monkeypatch.setattr(storage.settings, "OSS_SECRET_KEY", "test")
    monkeypatch.setattr(storage, "_build_bucket", lambda **kwargs: bucket)

    first = await storage.delete_oss_object(bucket="test", object_key="raw/test.eml")
    second = await storage.delete_oss_object(bucket="test", object_key="raw/test.eml")

    assert first.deleted is True
    assert first.already_missing is False
    assert second.deleted is True
    assert second.already_missing is True
    assert bucket.deleted == [("raw/test.eml", None)]


@pytest.mark.anyio
async def test_delete_oss_object_uses_version(monkeypatch: pytest.MonkeyPatch) -> None:
    bucket = FakeBucket({"pdf/rma.pdf"})
    monkeypatch.setattr(storage.settings, "OSS_ACCESS_KEY", "test")
    monkeypatch.setattr(storage.settings, "OSS_SECRET_KEY", "test")
    monkeypatch.setattr(storage, "_build_bucket", lambda **kwargs: bucket)

    await storage.delete_oss_object(
        bucket="test",
        object_key="pdf/rma.pdf",
        object_version="v1",
    )
    assert bucket.deleted == [("pdf/rma.pdf", {"versionId": "v1"})]


@pytest.mark.anyio
async def test_delete_oss_object_treats_concurrent_no_such_key_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ConcurrentDeleteBucket(FakeBucket):
        def delete_object(self, key: str, params: dict | None = None) -> None:
            error = RuntimeError("object disappeared between exists and delete")
            error.status = 404
            error.code = "NoSuchKey"
            raise error

    monkeypatch.setattr(storage.settings, "OSS_ACCESS_KEY", "test")
    monkeypatch.setattr(storage.settings, "OSS_SECRET_KEY", "test")
    monkeypatch.setattr(
        storage,
        "_build_bucket",
        lambda **kwargs: ConcurrentDeleteBucket({"raw/test.eml"}),
    )

    result = await storage.delete_oss_object(bucket="test", object_key="raw/test.eml")

    assert result.deleted is True
    assert result.already_missing is True


@pytest.mark.anyio
async def test_delete_oss_objects_reports_partial_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_delete(**kwargs):
        if kwargs["object_key"] == "bad":
            raise storage.StorageDeleteError("OSS_DELETE_FAILED")
        return storage.OssDeleteResult(kwargs["bucket"], kwargs["object_key"], True)

    monkeypatch.setattr(storage, "delete_oss_object", fake_delete)
    results = await storage.delete_oss_objects(
        [{"bucket": "test", "object_key": "ok"}, {"bucket": "test", "object_key": "bad"}]
    )
    assert [row.deleted for row in results] == [True, False]
    assert results[1].error_code == "OSS_DELETE_FAILED"


@pytest.mark.anyio
async def test_delete_oss_object_maps_provider_permission_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class ForbiddenBucket(FakeBucket):
        def object_exists(self, key: str) -> bool:
            error = RuntimeError("provider denied delete access")
            error.status = 403
            raise error

    monkeypatch.setattr(storage.settings, "OSS_ACCESS_KEY", "test")
    monkeypatch.setattr(storage.settings, "OSS_SECRET_KEY", "test")
    monkeypatch.setattr(storage, "_build_bucket", lambda **kwargs: ForbiddenBucket(set()))

    with pytest.raises(storage.StorageDeleteError) as raised:
        await storage.delete_oss_object(bucket="test", object_key="forbidden")
    assert raised.value.code == "OSS_DELETE_FORBIDDEN"
    assert raised.value.retryable is False
