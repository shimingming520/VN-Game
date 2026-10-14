from common import oss_utils
from common import output_mirror
from fastapi.testclient import TestClient
import pytest


def test_disabled_oss_uses_local_storage_adapter(monkeypatch, tmp_path):
    monkeypatch.delenv("OSS_DISABLED", raising=False)
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path))

    key = "storage/outputs/local-task/story_tree.json"
    payload = b'{"title":"local"}'

    assert oss_utils.oss_write(key, payload, content_type="application/json") is True
    local_path = tmp_path / "outputs" / "local-task" / "story_tree.json"
    assert local_path.is_file()
    assert local_path.read_bytes() == payload
    assert oss_utils.oss_read(key) == payload
    assert oss_utils.oss_exists(key) is True
    assert oss_utils.oss_get_size(key) == len(payload)
    assert oss_utils.oss_get_meta(key) == (
        len(payload),
        oss_utils.local_etag(local_path),
    )
    assert oss_utils.oss_list_keys("storage/outputs/local-task") == [key]

    assert oss_utils.oss_delete(key) is True
    assert not local_path.exists()
    assert oss_utils.oss_read(key) is None


def test_disabled_oss_keeps_output_mirror_local(monkeypatch, tmp_path):
    monkeypatch.delenv("OSS_DISABLED", raising=False)
    output_root = tmp_path / "outputs"
    artifact = output_root / "local-task" / "story_tree.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(output_mirror, "_outputs_root", lambda: output_root)

    assert output_mirror.mirror_paths([artifact], strict=True) == 1


def test_local_asset_url_is_api_relative_when_oss_is_disabled(monkeypatch):
    monkeypatch.delenv("OSS_DISABLED", raising=False)

    assert oss_utils.oss_public_url(
        "storage/outputs/local-task/images/scene.png"
    ) == "/api/v1/local-assets/storage/outputs/local-task/images/scene.png"


def test_local_asset_endpoint_returns_file_and_rejects_path_escape(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("OSS_DISABLED", raising=False)
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
    image_path = tmp_path / "outputs" / "local-task" / "images" / "scene.png"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nlocal-image")

    pytest.importorskip("sqlmodel")
    from services.api_gateway.main import app

    client = TestClient(app)

    response = client.get(
        "/api/v1/local-assets/storage/outputs/local-task/images/scene.png"
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == image_path.read_bytes()

    escaped = client.get(
        "/api/v1/local-assets/storage/outputs/../../secret.txt"
    )
    assert escaped.status_code in (400, 404)
