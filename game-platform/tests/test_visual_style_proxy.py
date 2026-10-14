import asyncio
import io
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile

from auth.deps import TaskOwner
from gateway import creation


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "tasks/task-1/visual-style"),
        ("PUT", "tasks/task-1/visual-style"),
        ("POST", "tasks/task-1/style-anchors/character/generate"),
        ("POST", "tasks/task-1/style-anchors/scene/upload"),
        ("POST", "tasks/task-1/style-anchors/scene/confirm"),
        ("GET", "tasks/task-1/style-anchors/character/image"),
        ("GET", "tasks/task-1/style-anchor-jobs/job-1"),
    ],
)
def test_visual_style_proxy_paths_are_explicitly_allowed(method, path):
    assert creation._is_path_allowed(method, path) is True
    assert creation._visual_style_task_id(path) == "task-1"


def test_visual_style_proxy_rejects_unlisted_kind_and_method():
    assert creation._is_path_allowed(
        "POST", "tasks/task-1/style-anchors/cg/generate"
    ) is False
    assert creation._visual_style_task_id(
        "tasks/task-1/style-anchors/cg/generate"
    ) is None
    assert creation._is_path_allowed("DELETE", "tasks/task-1/visual-style") is False


def test_anchor_upload_is_reencoded_with_trusted_identity(monkeypatch):
    captured = {}

    monkeypatch.setattr(creation, "_ensure_owned_task", lambda task_id, owner: None)

    async def fake_forward(method, path, **kwargs):
        captured.update(method=method, path=path, **kwargs)
        return {"ok": True}

    monkeypatch.setattr(creation, "_forward_async", fake_forward)
    upload = UploadFile(filename="anchor.png", file=io.BytesIO(b"png"))
    owner = TaskOwner(user_id="user-1", user_name="策划")

    result = asyncio.run(
        creation.upload_style_anchor("task-1", "character", upload, owner)
    )

    assert result == {"ok": True}
    assert captured["method"] == "POST"
    assert captured["path"].endswith("/character/upload")
    assert captured["params"] == [("user_id", "user-1"), ("user_name", "策划")]
    assert captured["files"][0][0] == "image"
    assert captured["files"][0][1][1] == b"png"


def test_anchor_upload_rejects_invalid_kind_before_forward(monkeypatch):
    monkeypatch.setattr(
        creation,
        "_ensure_owned_task",
        lambda task_id, owner: (_ for _ in ()).throw(AssertionError("must not query")),
    )
    upload = UploadFile(filename="anchor.png", file=io.BytesIO(b"png"))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            creation.upload_style_anchor(
                "task-1",
                "cg",
                upload,
                TaskOwner(user_id="user-1", user_name="策划"),
            )
        )
    assert exc_info.value.status_code == 400


def test_visual_style_task_requires_matching_owner(monkeypatch):
    class _Rows:
        def __init__(self, row):
            self.row = row

        def mappings(self):
            return self

        def first(self):
            return self.row

    class _Connection:
        def __init__(self, row):
            self.row = row

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, *_args, **_kwargs):
            return _Rows(self.row)

    class _Engine:
        def __init__(self, row):
            self.row = row

        def connect(self):
            return _Connection(self.row)

    monkeypatch.setattr(
        creation,
        "engine",
        _Engine({"user_id": "user-1", "is_deleted": False, "is_purged": False}),
    )
    creation._ensure_owned_task(
        "task-1", TaskOwner(user_id="user-1", user_name="策划")
    )

    with pytest.raises(HTTPException) as exc_info:
        creation._ensure_owned_task(
            "task-1", TaskOwner(user_id="user-2", user_name="其他用户")
        )
    assert exc_info.value.status_code == 404


def test_binary_anchor_headers_are_relayed(monkeypatch):
    monkeypatch.setattr(
        creation.requests,
        "request",
        lambda *args, **kwargs: SimpleNamespace(
            status_code=200,
            content=b"png",
            headers={
                "Content-Type": "image/png",
                "Content-Disposition": 'inline; filename="anchor.png"',
                "Cache-Control": "private, no-store",
                "Set-Cookie": "must-not-leak=1",
            },
        ),
    )

    response = creation._forward("GET", "tasks/task-1/style-anchors/scene/image")

    assert response.media_type == "image/png"
    assert response.headers["content-disposition"].startswith("inline")
    assert response.headers["cache-control"] == "private, no-store"
    assert "set-cookie" not in response.headers
