"""Run the production chapter_branch pipeline locally without database/Celery writes."""

from __future__ import annotations

import argparse
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any
from unittest.mock import patch

from common.usage_tracker import normalize_llm_usage


@dataclass
class LocalRecord:
    data: Any
    name: str = ""


@dataclass(frozen=True)
class LocalPublishResult:
    md_count: int
    orphan_md_deleted: int
    publication_fingerprint: str


class LocalUsageTracker:
    """Offline usage collector used by smoke runs without touching platform DB."""

    def __init__(self, output_dir: Path):
        self.output_dir = Path(output_dir)
        self._lock = threading.RLock()
        self._context: dict[str, str] | None = None
        self._calls: list[dict[str, Any]] = []
        report_path = self.output_dir / "usage_report.json"
        if report_path.exists():
            try:
                existing = json.loads(report_path.read_text(encoding="utf-8"))
                calls = existing.get("calls", []) if isinstance(existing, dict) else []
                if isinstance(calls, list):
                    self._calls.extend(call for call in calls if isinstance(call, dict))
            except (OSError, json.JSONDecodeError):
                # A damaged diagnostic report must not block resuming the novel.
                pass

    def set_context(
        self,
        task_id: str,
        service: str,
        *,
        force_new_run: bool = False,
        run_id: str | None = None,
    ) -> str:
        del force_new_run
        resolved_run_id = run_id or hashlib.sha256(
            f"{task_id}:{service}:{time.time_ns()}".encode("utf-8")
        ).hexdigest()[:24]
        with self._lock:
            self._context = {
                "task_id": task_id,
                "service": service,
                "run_id": resolved_run_id,
            }
        return resolved_run_id

    def clear_context(self, run_id: str | None = None) -> None:
        with self._lock:
            if run_id and self._context and self._context["run_id"] != run_id:
                return
            self._context = None

    def add_llm_usage(
        self,
        usage: Any,
        model: str | None = None,
        provider: str | None = None,
    ) -> bool:
        normalized = normalize_llm_usage(usage, provider=provider)
        with self._lock:
            if not self._context:
                return False
            self._calls.append(
                {
                    **self._context,
                    "model": model or "",
                    "provider": provider or "",
                    "prompt_tokens": normalized.input_tokens,
                    "completion_tokens": normalized.output_tokens,
                    "cached_input_tokens": normalized.cached_input_tokens,
                    "cache_write_tokens": normalized.cache_write_tokens,
                    "reasoning_tokens": normalized.reasoning_tokens,
                    "total_tokens": normalized.total_tokens,
                }
            )
        return True

    def add_llm(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        model: str | None = None,
        provider: str | None = None,
    ) -> bool:
        return self.add_llm_usage(
            {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
            model=model,
            provider=provider,
        )

    def flush(self, run_id: str | None = None) -> bool:
        del run_id
        with self._lock:
            calls = list(self._calls)
        totals = {
            key: sum(int(call.get(key, 0) or 0) for call in calls)
            for key in (
                "prompt_tokens",
                "completion_tokens",
                "cached_input_tokens",
                "cache_write_tokens",
                "reasoning_tokens",
                "total_tokens",
            )
        }
        totals["call_count"] = len(calls)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "usage_report.json").write_text(
            json.dumps({"totals": totals, "calls": calls}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return True


class LocalArtifactStore:
    """Filesystem-backed stand-in for chapter_branch persistence during smoke tests."""

    def __init__(self, output_dir: Path):
        self.output_dir = Path(output_dir)
        self.md_dir = self.output_dir / "generated_stories"
        self.latest_worldview: dict[str, Any] | None = None
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.md_dir.mkdir(parents=True, exist_ok=True)

    def save_md(self, task_id: str, name: str, content: str, overwrite: bool = True) -> LocalRecord:
        path = self.md_dir / f"{name}.md"
        if path.exists() and not overwrite:
            return LocalRecord(path.read_text(encoding="utf-8"), name=name)
        path.write_text(content, encoding="utf-8")
        return LocalRecord(content, name=name)

    def get_md(self, task_id: str, name: str) -> LocalRecord | None:
        path = self.md_dir / f"{name}.md"
        if not path.exists():
            return None
        return LocalRecord(path.read_text(encoding="utf-8"), name=name)

    def save_story_tree(
        self,
        task_id: str,
        json_data: dict,
        overwrite: bool = True,
    ) -> LocalRecord:
        path = self.output_dir / "story_tree.json"
        if path.exists() and not overwrite:
            return LocalRecord(json.loads(path.read_text(encoding="utf-8")))
        path.write_text(
            json.dumps(json_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return LocalRecord(json_data)

    def save_diagnostics(
        self,
        task_id: str,
        candidate_story_tree: dict[str, Any],
        quality_report: dict[str, Any],
    ) -> None:
        del task_id
        (self.output_dir / "candidate_story_tree.json").write_text(
            json.dumps(candidate_story_tree, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.output_dir / "quality_report.json").write_text(
            json.dumps(quality_report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def publish_story_tree_transactionally(
        self,
        task_id: str,
        md_contents: dict[str, str],
        json_data: dict[str, Any],
        expected_node_ids: set[str],
        *,
        run_id: str,
        run_started_at: str,
    ) -> LocalPublishResult:
        """Filesystem equivalent of the final atomic database publication."""
        expected = set(expected_node_ids or [])
        if set(md_contents) != expected:
            raise ValueError("smoke MD 集合与预期节点不一致")
        orphan_count = 0
        for path in self.md_dir.glob("*.md"):
            if path.stem not in expected:
                path.unlink()
                orphan_count += 1
        for name, content in md_contents.items():
            self.save_md(task_id, name, content, overwrite=True)

        fingerprint_source = json.dumps(
            {"md": md_contents, "json": json_data},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        fingerprint = hashlib.sha256(
            fingerprint_source.encode("utf-8")
        ).hexdigest()
        published = dict(json_data)
        published["publication"] = {
            "run_id": run_id,
            "run_started_at": run_started_at,
            "fingerprint": fingerprint,
        }
        self.save_story_tree(task_id, published, overwrite=True)
        return LocalPublishResult(
            md_count=len(md_contents),
            orphan_md_deleted=orphan_count,
            publication_fingerprint=fingerprint,
        )

    def save_plan(self, task_id: str, plan: dict) -> None:
        from services.story_planner.core.chapter_branch_runtime import (
            refresh_plan_topology_fingerprint,
        )
        refresh_plan_topology_fingerprint(plan)
        (self.output_dir / "chapter_branch_plan.json").write_text(
            json.dumps(plan, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load_plan(self, task_id: str) -> dict:
        path = self.output_dir / "chapter_branch_plan.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def save_summary(
        self,
        task_id: str,
        outline: str,
        background: str,
        one_liner: str,
        *,
        require_stage_ownership: bool = False,
    ) -> bool:
        # Production persistence accepts this fencing-related keyword. The
        # local filesystem adapter is already isolated from the database, so
        # there is no lease to acquire here; keep the signature compatible and
        # report a successful local write to the caller.
        del require_stage_ownership
        (self.output_dir / "summary.json").write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "outline": outline,
                    "background": background,
                    "one_liner": one_liner,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return True

    def load_summary(self, task_id: str):
        """Return a production-shaped summary row from the local artifact."""
        path = self.output_dir / "summary.json"
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))

        class _SummaryRow:
            status = 1
            data = {
                key: payload.get(key, "")
                for key in ("outline", "background", "one_liner")
            }

        del task_id
        return _SummaryRow()

    def save_worldview(self, task_id: str, novel_ctx: Any, art_style: str) -> None:
        self.latest_worldview = getattr(novel_ctx, "worldview", {})
        (self.output_dir / "worldview.json").write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "art_style": art_style,
                    "branch_type": getattr(novel_ctx, "branch_type", "default"),
                    "worldview": getattr(novel_ctx, "worldview", {}),
                    "chapter_summaries": getattr(novel_ctx, "chapter_summaries", []),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


def _safe_task_id(input_path: Path) -> str:
    stem = re.sub(r"[^a-zA-Z0-9_-]+", "_", input_path.stem).strip("_")
    return f"smoke_{stem or 'chapter_branch'}"


def run_smoke(
    input_path: Path,
    output_dir: Path,
    *,
    output_language: str = "zh",
    art_style: str = "ink_chinese",
    worldview_seed: Path | None = None,
) -> dict[str, Any]:
    """Execute the real pipeline and return an offline quality report."""
    input_path = Path(input_path).resolve()
    output_dir = Path(output_dir).resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"真实测试文本不存在: {input_path}")

    os.environ["STORY_PLANNER_SMOKE_MODE"] = "1"
    from services.story_planner import tasks
    from services.story_planner.core.chapter_branch_quality import evaluate_artifacts
    from services.story_planner.core.llm_config import load_llm_settings
    from services.story_planner.core import llm_client
    from services.story_planner.core.novel_text_encoding import read_novel_text
    from services.story_planner.core import novel_reader

    encoding_result = read_novel_text(input_path, convert_to_utf8=True)
    if encoding_result.converted:
        print(
            f"📝 已将 smoke 输入从 {encoding_result.source_encoding} "
            f"转换为无 BOM UTF-8: {input_path}"
        )

    store = LocalArtifactStore(output_dir)
    usage_tracker = LocalUsageTracker(output_dir)
    task_id = _safe_task_id(input_path)
    usage_run_id = usage_tracker.set_context(
        task_id, "story_planner", force_new_run=True
    )
    started = time.perf_counter()

    _analyze_patch = nullcontext()
    if worldview_seed:
        seed_path = Path(worldview_seed).resolve()
        seed_payload = json.loads(seed_path.read_text(encoding="utf-8"))
        seed_worldview = seed_payload.get("worldview", seed_payload)
        original_analyze_novel = novel_reader.analyze_novel

        def _seeded_analyze_novel(*args, **kwargs):
            kwargs["worldview_override"] = seed_worldview
            return original_analyze_novel(*args, **kwargs)

        _analyze_patch = patch.object(
            novel_reader,
            "analyze_novel",
            side_effect=_seeded_analyze_novel,
        )
        print(f"♻️  smoke 使用世界观种子: {seed_path}")

    def _config(_task_id: str) -> dict:
        return {
            "art_style": art_style,
            "output_language": output_language,
            "story_mode": "chapter_branch",
            "novel_filename": input_path.name,
        }

    class _LocalQueryResult:
        def __init__(self, row):
            self._row = row

        def first(self):
            return self._row

    class _LocalSession:
        """Minimal Session double for the final summary verification read."""

        def __init__(self, row):
            self._row = row

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            del exc_type, exc_value, traceback
            return False

        def exec(self, statement):
            del statement
            return _LocalQueryResult(self._row)

    try:
        with (
            patch.object(tasks, "_get_task_config", side_effect=_config),
        patch.object(tasks, "_save_novel_ctx_to_world_bible", side_effect=store.save_worldview),
        patch.object(tasks, "_load_chapter_branch_plan", side_effect=store.load_plan),
        patch.object(tasks, "_save_chapter_branch_plan", side_effect=store.save_plan),
        patch.object(tasks, "_save_summary", side_effect=store.save_summary),
        patch.object(tasks, "_trigger_downstream", return_value=None),
        patch.object(tasks, "_usage_tracker", usage_tracker),
        patch.object(llm_client, "_usage_tracker", usage_tracker),
        patch.object(
            tasks,
            "_save_chapter_branch_diagnostics",
            side_effect=store.save_diagnostics,
        ),
        patch.object(tasks, "_record_chapter_branch_heartbeat", return_value=True),
        # tasks.py imports these writers as module-level aliases. Patch the
        # aliases themselves; patching only story_tree_to_db would otherwise
        # leave real database writes reachable during a smoke run.
        patch.object(tasks, "save_story_md_to_db", side_effect=store.save_md),
        patch.object(
            tasks,
            "publish_story_tree_transactionally",
            side_effect=store.publish_story_tree_transactionally,
        ),
        patch.object(
            tasks,
            "Session",
            side_effect=lambda *_args, **_kwargs: _LocalSession(
                store.load_summary(task_id)
            ),
        ),
        patch(
            "services.story_planner.table.story_tree_to_db.save_story_md_to_db",
            side_effect=store.save_md,
        ),
        patch(
            "services.story_planner.table.story_tree_to_db.get_story_md_by_name",
            side_effect=store.get_md,
        ),
        patch(
            "services.story_planner.table.story_tree_to_db.get_story_md_by_name_strict",
            side_effect=store.get_md,
        ),
        patch(
            "services.story_planner.table.story_tree_to_db.publish_story_tree_transactionally",
            side_effect=store.publish_story_tree_transactionally,
        ),
            _analyze_patch,
        ):
            tasks._parse_chapter_branch_mode(
                input_path,
                novel_name=input_path.stem,
                game_project=task_id,
            )
    except Exception as exc:
        from services.story_planner.core.chapter_branch_runtime import mark_running_nodes_failed

        failed_plan = store.load_plan(task_id)
        if mark_running_nodes_failed(failed_plan, str(exc)):
            store.save_plan(task_id, failed_plan)
        raise
    finally:
        usage_tracker.flush(run_id=usage_run_id)
        usage_tracker.clear_context(usage_run_id)

    story_tree_path = output_dir / "story_tree.json"
    story_tree = json.loads(story_tree_path.read_text(encoding="utf-8"))
    md_contents = {
        path.stem: path.read_text(encoding="utf-8")
        for path in store.md_dir.glob("*.md")
    }
    report = evaluate_artifacts(
        story_tree,
        md_contents,
        worldview=getattr(store, "latest_worldview", None),
    )
    settings = load_llm_settings()
    report["run"] = {
        "task_id": task_id,
        "input_path": str(input_path),
        "input_bytes": input_path.stat().st_size,
        "source_encoding": encoding_result.source_encoding,
        "encoding_converted": encoding_result.converted,
        "output_dir": str(output_dir),
        "duration_seconds": round(time.perf_counter() - started, 3),
        "model": settings.model,
        "base_url": settings.base_url,
    }
    (output_dir / "quality_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(description="chapter_branch 真实数据本地冒烟测试")
    parser.add_argument(
        "input_path",
        nargs="?",
        default=r"C:\Users\12984\Desktop\黑莲花0-3.txt",
    )
    parser.add_argument("--output-dir")
    parser.add_argument("--language", default="zh")
    parser.add_argument("--art-style", default="ink_chinese")
    parser.add_argument("--worldview-seed")
    args = parser.parse_args()

    input_path = Path(args.input_path)
    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        output_dir = Path(__file__).resolve().parent / "smoke_output" / f"{input_path.stem}-{timestamp}"

    report = run_smoke(
        input_path,
        output_dir,
        output_language=args.language,
        art_style=args.art_style,
        worldview_seed=Path(args.worldview_seed) if args.worldview_seed else None,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("passed") else 2


if __name__ == "__main__":
    raise SystemExit(main())
