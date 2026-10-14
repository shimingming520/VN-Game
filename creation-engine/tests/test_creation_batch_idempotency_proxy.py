from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from common.models import PlatformUser
from gateway import creation


@pytest.fixture
def batch_proxy(monkeypatch):
    captured = {}
    user = PlatformUser(main_site_uid="trusted-uid", username="可信用户")

    def fake_post(url, **kwargs):
        captured.clear()
        captured.update(url=url, **kwargs)
        return SimpleNamespace(
            status_code=200,
            content=b'{"ok":true}',
            headers={"Content-Type": "application/json"},
        )

    monkeypatch.setattr(creation, "PLATFORM_SERVICE_TOKEN", "trusted-token")
    monkeypatch.setattr(creation.requests, "post", fake_post)

    app = FastAPI()
    app.include_router(creation.router)
    app.dependency_overrides[creation.get_current_user] = lambda: user

    with TestClient(app) as client:
        yield client, captured


def test_batch_proxy_forwards_idempotency_header_and_batch_id(batch_proxy):
    client, captured = batch_proxy

    response = client.post(
        "/api/v1/creation/novels",
        headers={
            "Idempotency-Key": " batch-request-001 ",
            "X-Service-Token": "forged-token",
        },
        data={"batch_id": "batch-form-001"},
        files={"novels": ("book.txt", b"novel", "text/plain")},
    )

    assert response.status_code == 200
    assert captured["data"]["batch_id"] == "batch-form-001"
    assert captured["headers"]["Idempotency-Key"] == "batch-request-001"
    assert captured["headers"]["X-Service-Token"] == "trusted-token"
    assert captured["headers"]["X-Platform-User-Uid"] == "trusted-uid"
    assert captured["timeout"] == 300


def test_batch_proxy_keeps_missing_idempotency_key_backward_compatible(batch_proxy):
    client, captured = batch_proxy

    response = client.post(
        "/api/v1/creation/novels",
        files={"novels": ("book.txt", b"novel", "text/plain")},
    )

    assert response.status_code == 200
    assert "Idempotency-Key" not in captured["headers"]
    assert "batch_id" not in captured["data"]


def test_batch_proxy_reuses_the_same_idempotency_key(batch_proxy):
    client, captured = batch_proxy
    seen = []

    for _ in range(2):
        response = client.post(
            "/api/v1/creation/novels",
            headers={"Idempotency-Key": "stable-retry-key"},
            files={"novels": ("book.txt", b"novel", "text/plain")},
        )
        assert response.status_code == 200
        seen.append(captured["headers"]["Idempotency-Key"])

    assert seen == ["stable-retry-key", "stable-retry-key"]
