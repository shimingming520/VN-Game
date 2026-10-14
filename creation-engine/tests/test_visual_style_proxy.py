from types import SimpleNamespace

import pytest
from fastapi import HTTPException

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
    creation._check_allowed(method, path)
    assert creation._visual_style_task_id(path) == "task-1"


def test_visual_style_proxy_rejects_unlisted_kind_and_method():
    with pytest.raises(HTTPException) as invalid_kind:
        creation._check_allowed(
            "POST", "tasks/task-1/style-anchors/cg/generate"
        )
    assert invalid_kind.value.status_code == 404

    with pytest.raises(HTTPException) as invalid_method:
        creation._check_allowed("DELETE", "tasks/task-1/visual-style")
    assert invalid_method.value.status_code == 404


def test_binary_anchor_headers_are_relayed_without_credentials():
    response = creation._relay(
        SimpleNamespace(
            status_code=200,
            content=b"png",
            headers={
                "Content-Type": "image/png",
                "Content-Disposition": 'inline; filename="anchor.png"',
                "Cache-Control": "private, no-store",
                "Set-Cookie": "must-not-leak=1",
            },
        )
    )

    assert response.media_type == "image/png"
    assert response.headers["content-disposition"].startswith("inline")
    assert response.headers["cache-control"] == "private, no-store"
    assert "set-cookie" not in response.headers
