from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth.deps import TaskOwner
from gateway import creation


@pytest.fixture
def novels_proxy(monkeypatch):
    captured = {}
    owner = TaskOwner(user_id="trusted-user", user_name="可信用户")

    async def fake_forward(method, path, **kwargs):
        captured.clear()
        captured.update(method=method, path=path, **kwargs)
        return {"ok": True}

    monkeypatch.setattr(creation, "_forward_async", fake_forward)
    app = FastAPI()
    app.include_router(creation.router)
    app.dependency_overrides[creation.get_task_owner] = lambda: owner

    with TestClient(app) as client:
        yield client, captured


def test_novels_proxy_relays_idempotency_header_and_trusted_owner(novels_proxy):
    client, captured = novels_proxy

    response = client.post(
        "/api/v1/creation/novels",
        headers={"Idempotency-Key": " upload-request-001 "},
        data={"user_id": "forged-user", "user_name": "伪造用户"},
        files={"novels": ("book.txt", b"novel", "text/plain")},
    )

    assert response.status_code == 200
    assert captured["method"] == "POST"
    assert captured["path"] == "novels/batch"
    assert captured["headers"] == {"Idempotency-Key": "upload-request-001"}
    assert captured["data"]["user_id"] == "trusted-user"
    assert captured["data"]["user_name"] == "可信用户"


def test_novels_proxy_relays_batch_id_form_field(novels_proxy):
    client, captured = novels_proxy

    response = client.post(
        "/api/v1/creation/novels",
        data={"batch_id": "upload-batch-002"},
        files={"novels": ("book.txt", b"novel", "text/plain")},
    )

    assert response.status_code == 200
    assert captured["data"]["batch_id"] == "upload-batch-002"
    assert captured["headers"] is None


def test_forward_always_uses_internal_service_token(monkeypatch):
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return SimpleNamespace(
            status_code=200,
            content=b"{}",
            headers={"Content-Type": "application/json"},
        )

    monkeypatch.setattr(creation, "PLATFORM_SERVICE_TOKEN", "trusted-token")
    monkeypatch.setattr(creation.requests, "request", fake_request)

    creation._forward(
        "POST",
        "novels/batch",
        headers={
            "Idempotency-Key": "upload-request-003",
            "x-service-token": "forged-token",
        },
    )

    assert captured["headers"] == {
        "Idempotency-Key": "upload-request-003",
        "X-Service-Token": "trusted-token",
    }
    assert creation._SUBMIT_TIMEOUT_SEC == 300


@pytest.mark.parametrize(
    "relative_path",
    [
        "game-platform/k8s/07-platform-frontend-ingress.yaml",
        "creation-engine/k8s/05-engine-ingress.yaml",
    ],
)
def test_upload_ingress_limits(relative_path):
    repo_root = Path(__file__).resolve().parents[2]
    manifest = (repo_root / relative_path).read_text(encoding="utf-8")

    assert 'nginx.ingress.kubernetes.io/proxy-body-size: "80m"' in manifest
    assert 'nginx.ingress.kubernetes.io/proxy-read-timeout: "300"' in manifest
    assert 'nginx.ingress.kubernetes.io/proxy-send-timeout: "300"' in manifest


@pytest.mark.parametrize(
    "relative_path",
    [
        "game-platform/frontend/docker/nginx.conf",
        "creation-engine-frontend/docker/nginx.conf",
    ],
)
def test_frontend_nginx_upload_limits(relative_path):
    repo_root = Path(__file__).resolve().parents[2]
    config = (repo_root / relative_path).read_text(encoding="utf-8")

    assert "client_max_body_size 80m;" in config
    assert "client_body_timeout 300s;" in config
    assert "proxy_send_timeout 300s;" in config
    assert "proxy_read_timeout 300s;" in config
