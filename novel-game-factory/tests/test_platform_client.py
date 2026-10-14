from common import platform_client


class _Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {"ok": True}


def test_platform_submission_uses_versioned_visual_parallel_template(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return _Response()

    monkeypatch.delenv("PLATFORM_PIPELINE_TEMPLATE", raising=False)
    monkeypatch.setattr(platform_client.requests, "post", fake_post)

    assert platform_client.submit_platform_task("task-1") == {"ok": True}
    assert captured["json"]["template_name"] == "visual-parallel-v1"


def test_platform_template_can_roll_back_by_environment(monkeypatch):
    captured = {}
    monkeypatch.setenv("PLATFORM_PIPELINE_TEMPLATE", "default")
    monkeypatch.setattr(
        platform_client.requests,
        "post",
        lambda url, **kwargs: captured.update(kwargs) or _Response(),
    )

    platform_client.submit_platform_task("task-1")

    assert captured["json"]["template_name"] == "default"
