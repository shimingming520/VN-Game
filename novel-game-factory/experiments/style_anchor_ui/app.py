"""Standalone FastAPI UI for validating user-approved style anchors."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from common.task_models import ImageModel
from common.visual_style import (
    ASSET_CHARACTER,
    ASSET_SCENE,
    REMOVE_CHARACTER_BACKGROUND_KEY,
    STYLE_MODE_CUSTOM,
    STYLE_MODE_LLM,
    VisualStyleConfigError,
    file_sha256,
    load_visual_style_config,
    resolve_asset_style_plan,
    save_visual_style_config,
    style_anchor_path,
)
from common.style_anchor_generation import (
    AnchorGenerationError,
    generate_anchor,
    normalize_uploaded_anchor,
)
from experiments.style_anchor_ui.reuse_pipeline import (
    ReusePipelineError,
    create_derived_visual_task,
    get_visual_asset_debug,
    list_visual_assets,
    preflight_source_task,
    reuse_feature_enabled,
    start_visual_only_pipeline,
    visual_asset_path,
)


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
GATEWAY_URL = os.getenv("STYLE_UI_GATEWAY_URL", "http://127.0.0.1:8000").rstrip("/")
REQUEST_TIMEOUT = float(os.getenv("STYLE_UI_GATEWAY_TIMEOUT", "120"))
_CONFIG_LOCK = threading.RLock()

app = FastAPI(title="Style Anchor Experiment", version="0.1.0")


class StyleConfigRequest(BaseModel):
    mode: str = STYLE_MODE_LLM
    character_global_style: str = ""
    scene_global_style: str = ""
    remove_character_background: bool = True


class GenerateAnchorRequest(BaseModel):
    image_model: str = ImageModel.DEFAULT


class ConfirmPipelineRequest(BaseModel):
    project_name: str
    outline: Optional[dict] = None
    source_task_id: Optional[str] = None


class ReusePreflightRequest(BaseModel):
    source_task_id: str


class ReuseCreateRequest(BaseModel):
    source_task_id: str
    character_global_style: str = ""
    scene_global_style: str = ""
    remove_character_background: bool = False
    image_model: str = ImageModel.DEFAULT
    project_name: Optional[str] = None


def _kind_or_400(asset_kind: str) -> str:
    kind = str(asset_kind or "").strip().lower()
    if kind not in {ASSET_CHARACTER, ASSET_SCENE}:
        raise HTTPException(status_code=400, detail="asset_kind 只支持 character / scene")
    return kind


def _anchor_url(task_id: str, kind: str, sha256: str = "") -> str:
    stamp = sha256[:12] if sha256 else str(int(time.time()))
    return f"/api/tasks/{task_id}/anchors/{kind}/image?v={stamp}"


def _public_config(task_id: str, config: Dict[str, Any]) -> Dict[str, Any]:
    result = {"task_id": task_id, **config}
    for kind in (ASSET_CHARACTER, ASSET_SCENE):
        section = dict(result[kind])
        path = style_anchor_path(task_id, kind)
        section["exists"] = path.exists() and path.stat().st_size > 0
        section["preview_url"] = (
            _anchor_url(task_id, kind, section.get("sha256", ""))
            if section["exists"]
            else None
        )
        result[kind] = section
    result["ready_to_start"] = _ready_to_start(task_id, config)
    return result


def _ready_to_start(task_id: str, config: Dict[str, Any]) -> bool:
    if config.get("mode") == STYLE_MODE_LLM:
        return True
    try:
        resolve_asset_style_plan(task_id, ASSET_CHARACTER, fetch_remote=False)
        resolve_asset_style_plan(task_id, ASSET_SCENE, fetch_remote=False)
        return True
    except VisualStyleConfigError:
        return False


async def _gateway_json(
    method: str,
    path: str,
    *,
    json_body: Optional[Dict[str, Any]] = None,
    data: Optional[Dict[str, Any]] = None,
    files: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.request(
                method,
                f"{GATEWAY_URL}{path}",
                json=json_body,
                data=data,
                files=files,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"项目 API Gateway 连接失败: {exc}") from exc
    if not response.is_success:
        try:
            detail = response.json().get("detail", response.text)
        except Exception:
            detail = response.text
        raise HTTPException(status_code=response.status_code, detail=detail)
    if not response.content:
        return {}
    return response.json()


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/debug")
def debug_page():
    return FileResponse(STATIC_DIR / "debug.html")


@app.get("/health")
def health():
    return {
        "status": "healthy",
        "gateway_url": GATEWAY_URL,
        "reuse_feature_enabled": reuse_feature_enabled(),
    }


@app.post("/api/reuse/preflight")
def reuse_preflight(req: ReusePreflightRequest):
    try:
        return preflight_source_task(req.source_task_id)
    except ReusePipelineError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"复用预检失败: {exc}") from exc


@app.post("/api/reuse/create")
def reuse_create(req: ReuseCreateRequest):
    try:
        result = create_derived_visual_task(
            req.source_task_id,
            character_global_style=req.character_global_style,
            scene_global_style=req.scene_global_style,
            remove_character_background=req.remove_character_background,
            image_model=req.image_model,
            project_name=req.project_name,
        )
        task_id = result["derived_task_id"]
        config = load_visual_style_config(task_id, fetch_remote=False)
        return {
            "success": True,
            **result,
            "style": _public_config(task_id, config),
        }
    except ReusePipelineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except VisualStyleConfigError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"创建派生实验任务失败: {exc}") from exc


@app.post("/api/upload")
async def upload_story(
    novel: UploadFile = File(...),
    user_id: Optional[str] = Form(default=None),
    user_name: Optional[str] = Form(default=None),
    output_language: str = Form(default="zh-CN"),
    story_mode: str = Form(default="original"),
    gameplay: str = Form(default="interactive_story"),
    text_model: str = Form(default="gpt-5.6-sol"),
    image_model: str = Form(default=ImageModel.DEFAULT),
    audio_model: str = Form(default="speech-2.8-hd"),
    resolution: str = Form(default="1080p"),
    mode: Optional[str] = Form(default=None),
    pipeline_name: Optional[str] = Form(default=None),
):
    content = await novel.read()
    data = {
        "output_language": output_language,
        # 仅满足现有 Gateway/游戏 UI 主题契约，不参与新的生图风格控制。
        "art_style": "ink_chinese",
        "story_mode": story_mode,
        "gameplay": gameplay,
        "text_model": text_model,
        "image_model": image_model,
        "audio_model": audio_model,
        "resolution": resolution,
    }
    if user_id:
        data["user_id"] = user_id
    if user_name:
        data["user_name"] = user_name
    if mode:
        data["mode"] = mode
    if pipeline_name:
        data["pipeline_name"] = pipeline_name
    return await _gateway_json(
        "POST",
        "/api/v1/story/upload",
        data=data,
        files={
            "novel": (
                novel.filename or "novel.txt",
                content,
                novel.content_type or "text/plain",
            )
        },
    )


@app.post("/api/tasks/{task_id}/config")
async def proxy_story_config(task_id: str, body: Dict[str, Any]):
    return await _gateway_json(
        "POST",
        f"/api/v1/story/{task_id}/config",
        json_body=body,
    )


@app.get("/api/tasks/{task_id}/style")
def get_style(task_id: str):
    try:
        config = load_visual_style_config(task_id, fetch_remote=False)
        return _public_config(task_id, config)
    except VisualStyleConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/tasks/{task_id}/style")
def set_style(task_id: str, req: StyleConfigRequest):
    mode = req.mode.strip().lower()
    if mode not in {STYLE_MODE_LLM, STYLE_MODE_CUSTOM}:
        raise HTTPException(status_code=400, detail="mode 只支持 llm / custom")

    with _CONFIG_LOCK:
        try:
            config = load_visual_style_config(task_id, fetch_remote=False)
            config["mode"] = mode
            config[REMOVE_CHARACTER_BACKGROUND_KEY] = bool(
                req.remove_character_background
            )
            incoming = {
                ASSET_CHARACTER: req.character_global_style.strip(),
                ASSET_SCENE: req.scene_global_style.strip(),
            }
            for kind, style in incoming.items():
                section = config[kind]
                old_style = str(section.get("global_style") or "").strip()
                if style != old_style:
                    section["global_style"] = style
                    section["confirmed"] = False
                    # 生成图与生成时的风格文本绑定；上传图本身仍可保留，
                    # 但修改辅助文本后也必须重新确认。
                    section["stale"] = section.get("source") == "generated"
            save_visual_style_config(task_id, config, mirror=False)
            return _public_config(task_id, config)
        except VisualStyleConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/tasks/{task_id}/anchors/{asset_kind}/generate")
def generate_style_anchor(task_id: str, asset_kind: str, req: GenerateAnchorRequest):
    kind = _kind_or_400(asset_kind)
    with _CONFIG_LOCK:
        try:
            config = load_visual_style_config(task_id, fetch_remote=False)
            if config.get("mode") != STYLE_MODE_CUSTOM:
                raise HTTPException(status_code=409, detail="请先切换到 custom 模式")
            section = config[kind]
            target = style_anchor_path(task_id, kind)
            metadata, prompt = generate_anchor(
                task_id,
                kind,
                section.get("global_style", ""),
                req.image_model,
                target,
            )
            section.update(
                source="generated",
                confirmed=False,
                stale=False,
                sha256=metadata["sha256"],
                model=metadata["model"],
                prompt=prompt,
            )
            save_visual_style_config(task_id, config, mirror=False)
            return {
                "success": True,
                "asset_kind": kind,
                "preview_url": _anchor_url(task_id, kind, metadata["sha256"]),
                **metadata,
                "prompt": prompt,
            }
        except HTTPException:
            raise
        except (AnchorGenerationError, VisualStyleConfigError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Anchor 生成失败: {exc}") from exc


@app.post("/api/tasks/{task_id}/anchors/{asset_kind}/upload")
async def upload_style_anchor(
    task_id: str,
    asset_kind: str,
    image: UploadFile = File(...),
):
    kind = _kind_or_400(asset_kind)
    content = await image.read()
    with _CONFIG_LOCK:
        try:
            config = load_visual_style_config(task_id, fetch_remote=False)
            if config.get("mode") != STYLE_MODE_CUSTOM:
                raise HTTPException(status_code=409, detail="请先切换到 custom 模式")
            target = style_anchor_path(task_id, kind)
            metadata = normalize_uploaded_anchor(content, target)
            config[kind].update(
                source="uploaded",
                confirmed=False,
                stale=False,
                sha256=metadata["sha256"],
                model="uploaded",
                prompt="",
            )
            save_visual_style_config(task_id, config, mirror=False)
            return {
                "success": True,
                "asset_kind": kind,
                "preview_url": _anchor_url(task_id, kind, metadata["sha256"]),
                **metadata,
            }
        except HTTPException:
            raise
        except (AnchorGenerationError, VisualStyleConfigError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/tasks/{task_id}/anchors/{asset_kind}/confirm")
def confirm_style_anchor(task_id: str, asset_kind: str):
    kind = _kind_or_400(asset_kind)
    with _CONFIG_LOCK:
        try:
            config = load_visual_style_config(task_id, fetch_remote=False)
            if config.get("mode") != STYLE_MODE_CUSTOM:
                raise HTTPException(status_code=409, detail="当前不是 custom 模式")
            section = config[kind]
            target = style_anchor_path(task_id, kind)
            if not target.exists() or target.stat().st_size <= 0:
                raise HTTPException(status_code=404, detail="Anchor 图片不存在")
            if section.get("stale"):
                raise HTTPException(
                    status_code=409,
                    detail="风格文本已变更，请重新生成 Anchor 后再确认",
                )
            section["sha256"] = file_sha256(target)
            section["confirmed"] = True
            save_visual_style_config(task_id, config, mirror=False)
            return _public_config(task_id, config)
        except HTTPException:
            raise
        except VisualStyleConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/tasks/{task_id}/anchors/{asset_kind}/image")
def get_anchor_image(task_id: str, asset_kind: str):
    kind = _kind_or_400(asset_kind)
    target = style_anchor_path(task_id, kind)
    if not target.exists() or target.stat().st_size <= 0:
        raise HTTPException(status_code=404, detail="Anchor 图片不存在")
    return FileResponse(target, media_type="image/png")


@app.post("/api/tasks/{task_id}/start")
async def start_pipeline(task_id: str, req: ConfirmPipelineRequest):
    try:
        config = load_visual_style_config(task_id, fetch_remote=False)
        if config.get("mode") == STYLE_MODE_CUSTOM:
            # 两类资产必须各自通过确认、stale、文件和 sha256 校验。
            resolve_asset_style_plan(task_id, ASSET_CHARACTER, fetch_remote=False)
            resolve_asset_style_plan(task_id, ASSET_SCENE, fetch_remote=False)
    except VisualStyleConfigError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    body = req.model_dump(exclude_none=True)
    return await _gateway_json(
        "POST",
        f"/api/v1/story/{task_id}/confirm",
        json_body=body,
    )


@app.post("/api/tasks/{task_id}/start-visual-only")
def start_reused_visual_pipeline(task_id: str):
    try:
        return start_visual_only_pipeline(task_id)
    except VisualStyleConfigError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ReusePipelineError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"visual-only Pipeline 启动失败: {exc}") from exc


@app.get("/api/tasks/{task_id}/visual-assets")
def get_visual_assets(task_id: str):
    try:
        return list_visual_assets(task_id)
    except ReusePipelineError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取美术资产失败: {exc}") from exc


@app.get("/api/tasks/{task_id}/visual-assets/{category}/{filename}/debug")
def get_visual_asset_debug_info(task_id: str, category: str, filename: str):
    try:
        return get_visual_asset_debug(task_id, category, filename)
    except ReusePipelineError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取生图调试信息失败: {exc}") from exc


@app.get("/api/tasks/{task_id}/visual-assets/{category}/{filename}")
def get_visual_asset_image(task_id: str, category: str, filename: str):
    try:
        target = visual_asset_path(task_id, category, filename)
    except ReusePipelineError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    media_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(target.suffix.lower(), "application/octet-stream")
    return FileResponse(target, media_type=media_type)
