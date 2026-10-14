"""调用 game-platform 的任务编排 API，替代本地 Celery chain 编排。"""
import os
import requests

from common.config import PLATFORM_API_URL, PLATFORM_SERVICE_TOKEN


def submit_platform_task(
    task_id: str,
    params: dict | None = None,
    pipeline_name: str | None = None,
) -> dict:
    """提交任务给平台编排（默认走 visual-parallel-v1 的 7 服务 DAG）。

    平台内部只按 task_id 去调用各能力（adapter 会再 send_task 到现有 Celery 服务），
    不需要传具体业务参数——跟现有 Celery chain 只传 task_id 的行为一致。
    params 只是给平台侧 task.params_json 留一份快照方便排查，不影响调度。

    PLATFORM_PIPELINE_TEMPLATE 可显式回退到 default 等历史模板。
    pipeline_name 决定每个能力用哪个实现：平台按它查 pipeline 表解析 impl_map。
    不传则各步自动挑健康实现。
    """
    template_name = os.getenv(
        "PLATFORM_PIPELINE_TEMPLATE", "visual-parallel-v1"
    ).strip() or "visual-parallel-v1"
    body = {"task_id": task_id, "template_name": template_name, "params": params or {}}
    if pipeline_name:
        body["pipeline_name"] = pipeline_name
    resp = requests.post(
        f"{PLATFORM_API_URL}/api/v1/tasks",
        json=body,
        headers={"X-Service-Token": PLATFORM_SERVICE_TOKEN},  # 平台侧强制鉴权
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def resolve_pipeline_name(mode: str | None, pipeline_name: str | None) -> str | None:
    """定管线：显式指定优先，否则按创作模式（效率/均衡/高质量）查平台的映射配置。

    映射是管理员在中台配的、随时可改，所以每次现查而不缓存。
    """
    if pipeline_name:
        return pipeline_name
    if not mode:
        return None
    resp = requests.get(
        f"{PLATFORM_API_URL}/api/v1/pipelines/creation-modes", timeout=10
    )
    resp.raise_for_status()
    for row in resp.json():
        if row.get("mode") == mode:
            return row.get("pipeline_name") or None
    raise ValueError(f"未知的创作模式: {mode}")
