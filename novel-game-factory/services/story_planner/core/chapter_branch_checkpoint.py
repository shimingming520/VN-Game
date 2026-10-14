"""Fail-closed persistence for the resumable ``chapter_branch`` plan."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable, Mapping

from sqlmodel import Session, select

from services.story_planner.table.models import ChapterBranchPlan, engine


class ChapterBranchPlanReadError(RuntimeError):
    """Raised when checkpoint storage cannot be read authoritatively."""


class ChapterBranchPlanWriteError(RuntimeError):
    """Raised when checkpoint storage cannot persist an update."""


class StalePlannerExecutionError(ChapterBranchPlanWriteError):
    """Raised when an old execution attempts to overwrite a newer checkpoint."""


def load_chapter_branch_plan(task_id: str, *, db_engine=engine) -> dict[str, Any]:
    """Return an existing plan; return empty only when the row truly does not exist."""

    try:
        with Session(db_engine) as session:
            existing = session.exec(
                select(ChapterBranchPlan).where(ChapterBranchPlan.task_id == task_id)
            ).first()
    except Exception as exc:
        raise ChapterBranchPlanReadError(
            f"读取 chapter_branch_plan 失败: task_id={task_id}, error={exc}"
        ) from exc
    if existing is None:
        return {}
    if not isinstance(existing.topology_json, dict):
        raise ChapterBranchPlanReadError(
            f"chapter_branch_plan 已损坏: task_id={task_id}, topology_json 不是对象"
        )
    plan = deepcopy(existing.topology_json)
    if plan and str(plan.get("task_id") or task_id) != str(task_id):
        raise ChapterBranchPlanReadError(
            f"chapter_branch_plan 已损坏: task_id={task_id}, 内部 task_id 不匹配"
        )
    return plan


def save_chapter_branch_plan(
    task_id: str,
    plan: Mapping[str, Any],
    *,
    expected_run_id: str,
    allow_run_takeover: bool = False,
    db_engine=engine,
    ownership_check: Callable[[], Any] | None = None,
) -> None:
    """Persist one checkpoint with a transaction-local execution CAS.

    ``expected_run_id`` is the run observed by the caller.  A new execution may
    replace it only when ``allow_run_takeover`` is explicitly true.  Once that
    transaction commits, the previous execution can no longer save.
    """

    if not isinstance(plan, Mapping):
        raise ChapterBranchPlanWriteError("chapter_branch_plan 必须是对象")
    plan_data = deepcopy(dict(plan))
    if str(plan_data.get("task_id") or "") != str(task_id):
        raise ChapterBranchPlanWriteError(
            f"chapter_branch_plan.task_id 不匹配: expected={task_id!r}"
        )
    incoming_run_id = str(plan_data.get("run_id") or "")
    expected = str(expected_run_id or "")

    with Session(db_engine) as session:
        try:
            statement = select(ChapterBranchPlan).where(
                ChapterBranchPlan.task_id == task_id
            )
            if session.connection().dialect.name == "postgresql":
                statement = statement.with_for_update()
            existing = session.exec(statement).first()
            if ownership_check is not None:
                ownership_check()
            if existing is None:
                session.add(
                    ChapterBranchPlan(task_id=task_id, topology_json=plan_data)
                )
                session.commit()
                return
            if not isinstance(existing.topology_json, dict):
                raise ChapterBranchPlanWriteError(
                    f"chapter_branch_plan 已损坏，拒绝覆盖: task_id={task_id}"
                )

            current_run_id = str(existing.topology_json.get("run_id") or "")
            if current_run_id != expected:
                raise StalePlannerExecutionError(
                    f"拒绝旧 execution 保存断点: task_id={task_id}, "
                    f"expected_run_id={expected!r}, current_run_id={current_run_id!r}"
                )
            if incoming_run_id != current_run_id and not allow_run_takeover:
                raise StalePlannerExecutionError(
                    f"execution 接管未声明: task_id={task_id}, "
                    f"current_run_id={current_run_id!r}, incoming_run_id={incoming_run_id!r}"
                )

            existing.topology_json = plan_data
            session.add(existing)
            session.commit()
        except (ChapterBranchPlanWriteError, StalePlannerExecutionError):
            session.rollback()
            raise
        except Exception as exc:
            session.rollback()
            raise ChapterBranchPlanWriteError(
                f"chapter_branch 断点计划保存失败: task_id={task_id}, error={exc}"
            ) from exc
