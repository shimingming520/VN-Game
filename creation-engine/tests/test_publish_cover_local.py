from pathlib import Path


def test_publish_cover_is_saved_to_local_storage(monkeypatch, tmp_path):
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path))

    from common.local_storage import local_asset_url, local_path_for_key

    key = "publish_covers/user-1/fixed-cover-id.png"
    path = local_path_for_key(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"image-bytes")

    assert local_asset_url(key) == (
        "/api/v1/local-assets/publish_covers/user-1/fixed-cover-id.png"
    )
    assert path.read_bytes() == b"image-bytes"
