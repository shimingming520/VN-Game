from pathlib import Path

from gateway import assets


def test_local_mode_lists_assets_without_initializing_oss(monkeypatch, tmp_path):
    monkeypatch.delenv("OSS_DISABLED", raising=False)
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path))
    assets._cache.clear()
    assets._bucket = None

    asset = tmp_path / "image-backups" / "character" / "hero.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"local-image")

    items = assets._list_all("image-backups/character/")

    assert len(items) == 1
    assert items[0]["key"] == "image-backups/character/hero.png"
    assert items[0]["name"] == "hero.png"
    assert items[0]["url"] == "/api/v1/local-assets/image-backups/character/hero.png"
    assert items[0]["size"] == len(b"local-image")
    assert assets._bucket is None


def test_local_mode_downloads_asset_from_storage(monkeypatch, tmp_path):
    monkeypatch.delenv("OSS_DISABLED", raising=False)
    monkeypatch.setenv("STORAGE_PATH", str(tmp_path))

    asset = tmp_path / "image-backups" / "scene" / "room.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"local-scene")

    response = assets.download_local_asset("image-backups/scene/room.png")

    assert response.path == Path(asset)
    assert response.media_type == "image/png"
    assert response.headers["Content-Disposition"].startswith("inline;")
