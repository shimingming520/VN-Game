import asyncio
import io
from types import SimpleNamespace

from fastapi import UploadFile
from PIL import Image

from common import visual_style
from common.visual_style import (
    ASSET_CHARACTER,
    ASSET_SCENE,
    default_visual_style_config,
    file_sha256,
    load_visual_style_config,
    save_visual_style_config,
    style_anchor_path,
)
from services.api_gateway import main as gateway


class _FirstResult:
    def __init__(self, value):
        self.value = value

    def first(self):
        return self.value


class _FakeSession:
    task = None

    def __init__(self, _engine):
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def exec(self, _statement):
        return _FirstResult(self.task)

    def add(self, _value):
        return None

    def commit(self):
        self.commits += 1


def _task(task_id="style-api-task"):
    return SimpleNamespace(
        task_id=task_id,
        image_model="gpt-image-2",
        is_published=False,
        celery_chain_id=None,
        info_confirm_stage="upload",
        overall_status=0,
    )


def _png_bytes(color):
    buffer = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _prepare(monkeypatch, tmp_path):
    task = _task()
    _FakeSession.task = task
    monkeypatch.setattr(gateway, "Session", _FakeSession)
    monkeypatch.setattr(visual_style, "OUTPUTS_PATH", str(tmp_path))
    monkeypatch.setattr(visual_style, "fetch_output_file", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway, "fetch_output_file", lambda *args, **kwargs: None)
    return task


def test_upload_and_confirm_both_anchors_before_run(monkeypatch, tmp_path):
    task = _prepare(monkeypatch, tmp_path)
    request = gateway.VisualStyleUpdateRequest(
        mode="custom",
        character_global_style="rough ink linework",
        scene_global_style="soft gouache atmosphere",
        remove_character_background=False,
    )

    initial = asyncio.run(gateway.update_visual_style(task.task_id, request))
    assert initial["ready_to_run"] is False
    assert initial["remove_character_background"] is False

    for kind, color in ((ASSET_CHARACTER, "red"), (ASSET_SCENE, "blue")):
        uploaded = asyncio.run(
            gateway.upload_style_anchor(
                task.task_id,
                kind,
                UploadFile(
                    filename=f"{kind}.png",
                    file=io.BytesIO(_png_bytes(color)),
                ),
            )
        )
        assert uploaded["sha256"]
        confirmed = asyncio.run(gateway.confirm_style_anchor(task.task_id, kind))
        assert confirmed[kind]["confirmed"] is True

    current = asyncio.run(gateway.get_visual_style(task.task_id))
    assert current["ready_to_run"] is True
    assert current[ASSET_CHARACTER]["status"] == "confirmed"
    assert current[ASSET_SCENE]["status"] == "confirmed"


def test_generate_anchor_accepts_empty_body_and_uses_task_model(monkeypatch, tmp_path):
    task = _prepare(monkeypatch, tmp_path)
    asyncio.run(
        gateway.update_visual_style(
            task.task_id,
            gateway.VisualStyleUpdateRequest(
                mode="custom",
                character_global_style="paper cut silhouette",
                scene_global_style="paper cut landscape",
            ),
        )
    )
    captured = {}

    class _Signature:
        def apply_async(self, *, task_id):
            captured["job_id"] = task_id
            return SimpleNamespace(id=task_id)

    class _Celery:
        def signature(self, name, *, args, queue):
            captured.update(name=name, args=args, queue=queue)
            return _Signature()

    monkeypatch.setattr(gateway, "celery_app", _Celery())

    response = asyncio.run(
        gateway.generate_style_anchor(task.task_id, ASSET_CHARACTER, None)
    )

    assert response.status == "generating"
    assert captured["name"] == "services.scene_painter.tasks.generate_style_anchor"
    assert captured["args"] == [
        task.task_id,
        ASSET_CHARACTER,
        "paper cut silhouette",
        task.image_model,
    ]
    assert captured["queue"] == "scene_queue"
    config = load_visual_style_config(
        task.task_id, outputs_path=tmp_path, fetch_remote=False
    )
    assert config[ASSET_CHARACTER]["job_id"] == response.job_id
    assert config[ASSET_CHARACTER]["status"] == "generating"


def test_job_polling_keeps_ready_config_when_redis_reports_pending(monkeypatch, tmp_path):
    task = _prepare(monkeypatch, tmp_path)
    job_id = "expired-result-job"
    anchor = style_anchor_path(
        task.task_id, ASSET_CHARACTER, outputs_path=tmp_path
    )
    anchor.parent.mkdir(parents=True, exist_ok=True)
    anchor.write_bytes(b"anchor")
    config = default_visual_style_config()
    config["mode"] = "custom"
    config[ASSET_CHARACTER].update(
        global_style="ink",
        source="generated",
        sha256=file_sha256(anchor),
        status="ready",
        job_id=job_id,
    )
    save_visual_style_config(
        task.task_id, config, outputs_path=tmp_path, mirror=False
    )

    class _ExpiredResult:
        state = "PENDING"

        def failed(self):
            return False

        def successful(self):
            return False

    monkeypatch.setattr(
        "celery.result.AsyncResult", lambda *args, **kwargs: _ExpiredResult()
    )

    response = asyncio.run(gateway.get_style_anchor_job(task.task_id, job_id))

    assert response.status == "ready"
    assert response.celery_state == "PENDING"
    assert response.preview_url
