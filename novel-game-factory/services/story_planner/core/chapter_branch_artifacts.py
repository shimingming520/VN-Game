"""Strict validation for completed ``chapter_branch`` database artifacts.

The public story-tree contract remains unchanged.  This module only decides
whether a completed redelivery is safe to skip; it never repairs or mutates
published data.
"""

from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
import hmac
import re
from typing import Any, Iterable, Mapping

from services.story_planner.core.chapter_branch_runtime import (
    CHAPTER_BRANCH_STEP_ORDER,
    PLAN_VERSION,
    validate_generated_content,
)
from services.story_planner.table.story_tree_to_db import (
    calculate_publication_fingerprint,
)
from services.story_planner.core.content_warnings import (
    validate_content_warnings_schema,
)


_SUCCESSFUL_NODE_STATUSES = frozenset({"completed", "omitted"})
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SUMMARY_FIELDS = ("outline", "background", "one_liner")
_WORLDVIEW_FIELDS = {
    "worldview": dict,
    "char_cards": str,
    "relationship_map": str,
    "world_rules": str,
    "narrative_arc": str,
}
_MISSING = object()


class ChapterBranchArtifactValidationError(RuntimeError):
    """Raised when a completed task does not own a complete valid artifact set."""

    def __init__(self, task_id: str, errors: Iterable[str]):
        self.task_id = str(task_id)
        self.errors = tuple(str(error) for error in errors if str(error))
        detail = "；".join(self.errors) or "未知产物错误"
        super().__init__(
            f"chapter_branch 已完成产物校验失败: task_id={self.task_id}，{detail}"
        )


@dataclass(frozen=True)
class CompletedChapterBranchArtifacts:
    """Validated identity of one authoritative completed publication."""

    node_ids: frozenset[str]
    fingerprint: str
    run_id: str
    run_started_at: str


def _record_value(record: Any, field: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        return record.get(field, default)
    return getattr(record, field, default)


def _story_tree_validation_view(
    story_tree_data: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Return one locale view plus the serialized payload used for fingerprints.

    ``story_tree.json`` is now stored as a locale-keyed bundle (for example
    ``en-US``/``ja-JP``), while ``publication`` remains a top-level fencing
    record added by the transactional publisher.  Validation rules operate on
    one complete locale snapshot, but the publication fingerprint must be
    calculated over the exact bundle payload.
    """
    payload = dict(story_tree_data) if isinstance(story_tree_data, Mapping) else {}
    if isinstance(payload.get("nodes"), dict):
        return payload, payload, False

    candidates = [
        (str(language), value)
        for language, value in payload.items()
        if isinstance(value, Mapping) and isinstance(value.get("nodes"), dict)
    ]
    if not candidates:
        return payload, payload, False

    selected = next(
        (value for language, value in candidates if language == "en-US"),
        candidates[0][1],
    )
    return dict(deepcopy(selected)), payload, True


def _normalize_md_artifacts(
    md_artifacts: Iterable[Any],
) -> tuple[dict[str, str], set[str]]:
    contents: dict[str, str] = {}
    duplicates: set[str] = set()
    for artifact in md_artifacts or []:
        if isinstance(artifact, tuple) and len(artifact) == 2:
            name, content = artifact
        else:
            name = _record_value(artifact, "name", "")
            content = _record_value(artifact, "data", "")
        normalized_name = str(name or "").strip()
        if not normalized_name:
            continue
        if normalized_name in contents:
            duplicates.add(normalized_name)
            continue
        contents[normalized_name] = str(content or "")
    return contents, duplicates


def _content_warning_category_codes(tree: Mapping[str, Any]) -> frozenset[str]:
    """Return machine-readable warning category codes from one locale tree."""

    warnings = tree.get("content_warnings")
    if not isinstance(warnings, Mapping):
        return frozenset()
    work = warnings.get("work")
    if not isinstance(work, Mapping):
        return frozenset()
    return frozenset(
        str(item.get("code") or "").strip()
        for item in (work.get("categories") or [])
        if isinstance(item, Mapping) and str(item.get("code") or "").strip()
    )


def _content_warning_machine_projection(tree: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return locale-independent warning fields for bundle consistency checks.

    Localized copy is deliberately excluded.  All policy and evidence fields
    remain shared across locales so a player cannot bypass an acknowledgement
    gate merely by switching languages.
    """

    warnings = tree.get("content_warnings")
    if not isinstance(warnings, Mapping):
        return None
    work = warnings.get("work")
    if not isinstance(work, Mapping):
        return None
    categories = []
    for item in work.get("categories") or []:
        if not isinstance(item, Mapping):
            continue
        categories.append(
            {
                "code": str(item.get("code") or ""),
                "presence": str(item.get("presence") or ""),
                "severity": str(item.get("severity") or ""),
                "confidence": item.get("confidence"),
                "evidence_chapters": list(item.get("evidence_chapters") or []),
            }
        )
    categories.sort(key=lambda item: item["code"])
    policy = work.get("display_policy")
    detector = work.get("detector")
    return {
        "schema_version": warnings.get("schema_version"),
        "work": {
            "status": work.get("status"),
            "has_sensitive_content": work.get("has_sensitive_content"),
            "severity": work.get("severity"),
            "categories": categories,
            "display_policy": dict(policy) if isinstance(policy, Mapping) else policy,
            "detector": dict(detector) if isinstance(detector, Mapping) else detector,
        },
        "chapters": deepcopy(dict(warnings.get("chapters") or {}))
        if isinstance(warnings.get("chapters"), Mapping)
        else warnings.get("chapters"),
    }


def _validate_content_warning_locales(
    *,
    default_tree: Mapping[str, Any],
    serialized_payload: Mapping[str, Any],
    is_language_bundle: bool,
) -> list[str]:
    """Validate optional work warnings while keeping legacy trees readable.

    The field is optional for historical publications.  Once a publication
    contains it, however, every locale snapshot must carry a valid copy and the
    machine category codes must remain identical across locales.  Localized
    labels are intentionally not compared because they are expected to differ.
    """

    default_has_field = "content_warnings" in default_tree
    if not is_language_bundle:
        if not default_has_field:
            return []
        return [
            f"content_warnings: {issue}"
            for issue in validate_content_warnings_schema(
                default_tree.get("content_warnings"),
                require_localized=True,
            )
        ]

    locale_items = [
        (str(language), value)
        for language, value in serialized_payload.items()
        if language != "publication" and isinstance(value, Mapping)
    ]
    if not locale_items:
        return []
    present_locales = [
        language for language, value in locale_items if "content_warnings" in value
    ]
    # Older bundles may omit the optional field entirely.  A partially upgraded
    # bundle is unsafe because the player would see different policy metadata.
    if not present_locales:
        return []
    errors: list[str] = []
    if len(present_locales) != len(locale_items):
        missing = sorted(
            language for language, value in locale_items if "content_warnings" not in value
        )
        errors.append(
            "content_warnings: locale 缺失字段: " + ", ".join(missing)
        )

    reference_codes: frozenset[str] | None = None
    reference_machine: dict[str, Any] | None = None
    reference_language = ""
    for language, value in locale_items:
        if "content_warnings" not in value:
            continue
        issues = validate_content_warnings_schema(
            value.get("content_warnings"),
            require_localized=True,
        )
        errors.extend(
            f"content_warnings[{language}]: {issue}" for issue in issues
        )
        codes = _content_warning_category_codes(value)
        if reference_codes is None:
            reference_codes = codes
            reference_language = language
        elif codes != reference_codes:
            errors.append(
                "content_warnings: locale category code 不一致: "
                f"{reference_language}={sorted(reference_codes)}, "
                f"{language}={sorted(codes)}"
            )
        machine = _content_warning_machine_projection(value)
        if reference_machine is None:
            reference_machine = machine
        elif machine != reference_machine:
            errors.append(
                "content_warnings: locale 机器字段不一致: "
                f"参考语言={reference_language}，当前语言={language}"
            )
    return errors


def _locale_node_structure_signature(
    node_id: str,
    node: Mapping[str, Any],
) -> dict[str, Any]:
    """Return locale-invariant routing fields for one node.

    Title/summary and choice labels are intentionally excluded: they are the
    display strings translated per locale.  Missing fields stay distinguishable
    from explicit nulls so a partially upgraded bundle is diagnosed.
    """
    signature: dict[str, Any] = {}
    for field in ("id", "depth", "parent_id", "is_original"):
        signature[field] = node[field] if field in node else _MISSING
    children = node.get("children", _MISSING)
    if children is _MISSING:
        signature["children.child_id"] = _MISSING
    elif isinstance(children, list):
        signature["children.child_id"] = tuple(
            str(item.get("child_id") or "") if isinstance(item, Mapping) else ""
            for item in children
        )
    else:
        signature["children.child_id"] = ("__invalid_children__",)
    return signature


def _validate_locale_node_structure(
    reference_nodes: Mapping[str, Any],
    locale_nodes: Mapping[str, Any],
    *,
    message_prefix: str,
) -> list[str]:
    """Compare locale-invariant node structure while allowing display text translation."""
    errors: list[str] = []
    reference_signatures = {
        str(node_id): _locale_node_structure_signature(node_id, node)
        for node_id, node in reference_nodes.items()
        if isinstance(node, Mapping)
    }
    for node_id, expected in reference_signatures.items():
        current_node = locale_nodes.get(node_id)
        if not isinstance(current_node, Mapping):
            continue
        current = _locale_node_structure_signature(node_id, current_node)
        for field, expected_value in expected.items():
            current_value = current.get(field)
            if expected_value is _MISSING and current_value is _MISSING:
                continue
            if expected_value != current_value:
                errors.append(
                    f"{message_prefix}节点 {node_id} 的 {field} 与默认语言不一致"
                )
    return errors


def validate_story_tree_language_bundle(
    story_tree_data: Mapping[str, Any] | None,
) -> list[str]:
    """Validate publication metadata and routing consistency for every locale.

    This lightweight check is intended for the point immediately after the
    localized bundle is built and before publication. It deliberately returns
    diagnostics instead of raising so the caller can decide whether to repair,
    downgrade, or reject the release. Legacy single-locale trees remain
    supported.
    """

    tree, serialized_payload, is_language_bundle = _story_tree_validation_view(
        story_tree_data
    )
    if not tree:
        return ["story_tree language bundle 为空"]
    errors = _validate_content_warning_locales(
        default_tree=tree,
        serialized_payload=serialized_payload,
        is_language_bundle=is_language_bundle,
    )
    if is_language_bundle:
        expected_nodes = tree.get("nodes")
        expected_nodes = expected_nodes if isinstance(expected_nodes, dict) else {}
        expected_node_ids = set(expected_nodes)
        for language, locale_tree in serialized_payload.items():
            if language == "publication":
                continue
            if not isinstance(locale_tree, Mapping):
                errors.append(f"language bundle locale {language} 不是 JSON object")
                continue
            for metadata_field in ("book_title", "book_summary"):
                metadata_value = locale_tree.get(metadata_field)
                if not isinstance(metadata_value, str) or not metadata_value.strip():
                    errors.append(
                        f"language bundle locale {language} 缺少有效 {metadata_field}"
                    )
            locale_nodes = locale_tree.get("nodes")
            if not isinstance(locale_nodes, dict) or not locale_nodes:
                errors.append(f"language bundle locale {language} 缺少有效 nodes")
                continue
            if set(locale_nodes) != expected_node_ids:
                errors.append(f"language bundle locale {language} nodes 与默认语言不一致")
                continue
            errors.extend(
                _validate_locale_node_structure(
                    expected_nodes,
                    locale_nodes,
                    message_prefix=f"language bundle locale {language} ",
                )
            )
    return errors


def validate_completed_chapter_branch_artifacts(
    *,
    task_id: str,
    story_tree_data: Mapping[str, Any] | None,
    md_artifacts: Iterable[Any],
    plan: Mapping[str, Any] | None,
    summary: Any,
    worldview: Any,
    require_summary: bool = True,
) -> CompletedChapterBranchArtifacts:
    """Validate every authoritative artifact before accepting a completed skip."""

    errors: list[str] = []
    tree, serialized_payload, is_language_bundle = _story_tree_validation_view(
        story_tree_data
    )
    if not tree:
        errors.append("story_tree_json 缺失或不是对象")
    errors.extend(
        _validate_content_warning_locales(
            default_tree=tree,
            serialized_payload=serialized_payload,
            is_language_bundle=is_language_bundle,
        )
    )
    if tree and tree.get("story_mode") != "chapter_branch":
        errors.append("story_tree_json.story_mode 不是 chapter_branch")

    nodes = tree.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        errors.append("story_tree_json.nodes 缺失或不是非空对象")
        nodes = {}
    if is_language_bundle:
        expected_locale_nodes = set(nodes)
        for language, locale_tree in serialized_payload.items():
            if language == "publication":
                continue
            if not isinstance(locale_tree, Mapping):
                errors.append(f"语言版本 {language} 不是 JSON object")
                continue
            for metadata_field in ("book_title", "book_summary"):
                metadata_value = locale_tree.get(metadata_field)
                if not isinstance(metadata_value, str) or not metadata_value.strip():
                    errors.append(
                        f"语言版本 {language} 缺少有效 {metadata_field}"
                    )
            locale_nodes = locale_tree.get("nodes")
            if not isinstance(locale_nodes, dict) or not locale_nodes:
                errors.append(f"语言版本 {language} 的 nodes 缺失或为空")
                continue
            if set(locale_nodes) != expected_locale_nodes:
                errors.append(
                    f"语言版本 {language} 的 nodes 与默认语言不一致"
                )
                continue
            errors.extend(
                _validate_locale_node_structure(
                    nodes,
                    locale_nodes,
                    message_prefix=f"语言版本 {language} ",
                )
            )
    root_id = str(tree.get("root_id") or "")
    if not root_id or root_id not in nodes:
        errors.append("story_tree_json.root_id 不存在于 nodes")

    expected_node_ids: set[str] = set()
    for node_id, node in nodes.items():
        normalized_id = str(node_id or "").strip()
        if not normalized_id or not isinstance(node, dict):
            errors.append(f"节点 {node_id!r} 的 ID 或结构无效")
            continue
        expected_node_ids.add(normalized_id)
        declared_id = str(node.get("id") or "").strip()
        if declared_id != normalized_id:
            errors.append(f"节点 '{normalized_id}' 的 id 与节点键不一致")
        expected_content_file = f"{normalized_id}.md"
        if str(node.get("content_file") or "").strip() != expected_content_file:
            errors.append(
                f"节点 '{normalized_id}' 的 content_file 必须为 {expected_content_file}"
            )
        children = node.get("children")
        if not isinstance(children, list):
            errors.append(f"节点 '{normalized_id}' 的 children 不是列表")
            continue
        for child in children:
            child_id = str(child.get("child_id") or "") if isinstance(child, dict) else ""
            if child_id not in nodes:
                errors.append(
                    f"节点 '{normalized_id}' 指向不存在的 child_id '{child_id}'"
                )

    md_contents, duplicate_md_ids = _normalize_md_artifacts(md_artifacts)
    actual_node_ids = set(md_contents)
    missing_md_ids = sorted(expected_node_ids - actual_node_ids)
    extra_md_ids = sorted(actual_node_ids - expected_node_ids)
    if missing_md_ids:
        errors.append(f"缺少 MD: {missing_md_ids}")
    if extra_md_ids:
        errors.append(f"存在额外 MD: {extra_md_ids}")
    if duplicate_md_ids:
        errors.append(f"存在重复 MD: {sorted(duplicate_md_ids)}")
    for node_id in sorted(expected_node_ids & actual_node_ids):
        valid, reason = validate_generated_content(md_contents[node_id], min_chars=1)
        if (
            not valid
            and str(md_contents[node_id]).rstrip().endswith(".")
            and "正文末句缺少完整结束标点" in reason
        ):
            valid, reason = True, ""
        if not valid:
            errors.append(f"节点 '{node_id}' 正文无效: {reason}")

    quality_gate = tree.get("quality_gate")
    if not isinstance(quality_gate, dict) or quality_gate.get("passed") is not True:
        errors.append("发布质量门禁未明确通过")

    publication = (
        serialized_payload.get("publication")
        if is_language_bundle
        else tree.get("publication")
    )
    if not isinstance(publication, dict):
        publication = {}
        errors.append("publication 缺失或不是对象")
    run_id = str(publication.get("run_id") or "").strip()
    run_started_at = str(publication.get("run_started_at") or "").strip()
    stored_fingerprint = str(publication.get("fingerprint") or "").strip().lower()
    if not run_id or not run_started_at:
        errors.append("publication.run_id/run_started_at 缺失")
    if not _SHA256_PATTERN.fullmatch(stored_fingerprint):
        errors.append("publication.fingerprint 不是有效 SHA256")
    if expected_node_ids == actual_node_ids and not duplicate_md_ids:
        fingerprint_tree = dict(serialized_payload if is_language_bundle else tree)
        fingerprint_tree.pop("publication", None)
        calculated_fingerprint = calculate_publication_fingerprint(
            md_contents,
            fingerprint_tree,
        )
        if stored_fingerprint and not hmac.compare_digest(
            stored_fingerprint, calculated_fingerprint
        ):
            errors.append("发布指纹与当前 JSON/MD 内容不一致")
    else:
        calculated_fingerprint = ""

    plan_data = dict(plan) if isinstance(plan, Mapping) else {}
    if not plan_data:
        errors.append("chapter_branch 断点计划缺失或损坏")
    elif int(plan_data.get("version", 0) or 0) not in {5, PLAN_VERSION}:
        errors.append(
            f"chapter_branch 断点计划版本不受支持: {plan_data.get('version')!r}"
        )
    if plan_data and str(plan_data.get("task_id") or "") != str(task_id):
        errors.append("chapter_branch 断点计划 task_id 不匹配")
    plan_nodes = plan_data.get("nodes") if plan_data else None
    if not isinstance(plan_nodes, dict) or not plan_nodes:
        errors.append("chapter_branch 断点计划 nodes 缺失")
    else:
        incomplete = sorted(
            str(node_id)
            for node_id, entry in plan_nodes.items()
            if not isinstance(entry, dict)
            or entry.get("status") not in _SUCCESSFUL_NODE_STATUSES
        )
        if incomplete:
            errors.append(f"chapter_branch 断点计划仍有未成功节点: {incomplete}")
        completed_real_ids = {
            str(entry.get("real_id") or "")
            for entry in plan_nodes.values()
            if isinstance(entry, dict) and entry.get("status") == "completed"
        }
        completed_real_ids.discard("")
        if completed_real_ids != expected_node_ids:
            errors.append(
                "chapter_branch 断点计划 completed 节点与发布节点集合不一致"
            )

    plan_steps = plan_data.get("steps") if plan_data else None
    if int(plan_data.get("version", 0) or 0) == PLAN_VERSION and (
        not isinstance(plan_steps, dict) or not plan_steps
    ):
        errors.append("chapter_branch 内部步骤状态缺失")
    elif isinstance(plan_steps, dict) and plan_steps:
        required_steps = list(CHAPTER_BRANCH_STEP_ORDER)
        if not require_summary:
            required_steps = required_steps[: required_steps.index("summary_publish")]
        incomplete_steps = [
            step_name
            for step_name in required_steps
            if not isinstance(plan_steps.get(step_name), dict)
            or plan_steps[step_name].get("status") != "completed"
            or not str(plan_steps[step_name].get("output_digest") or "").strip()
        ]
        if incomplete_steps:
            errors.append(f"chapter_branch 内部步骤未完成: {incomplete_steps}")
        story_publish_step = plan_steps.get("story_publish", {})
        if (
            isinstance(story_publish_step, dict)
            and story_publish_step.get("status") == "completed"
            and calculated_fingerprint
            and str(story_publish_step.get("output_digest") or "")
            != calculated_fingerprint
        ):
            errors.append("chapter_branch story_publish 步骤摘要与发布指纹不一致")

    if require_summary:
        summary_status = _record_value(summary, "status")
        summary_data = _record_value(summary, "data", {})
        if summary_status != 1 or not isinstance(summary_data, dict):
            errors.append("story_summary 未处于 ready 状态")
        else:
            missing_summary_fields = [
                field
                for field in _SUMMARY_FIELDS
                if not str(summary_data.get(field) or "").strip()
            ]
            if missing_summary_fields:
                errors.append(f"story_summary 必填字段为空: {missing_summary_fields}")

    worldview_status = _record_value(worldview, "status")
    worldview_data = _record_value(worldview, "data", {})
    if worldview_status != 1 or not isinstance(worldview_data, dict):
        errors.append("world_bible 缺失、未 confirmed 或数据无效")
    else:
        invalid_worldview_fields = [
            field
            for field, expected_type in _WORLDVIEW_FIELDS.items()
            if field not in worldview_data
            or not isinstance(worldview_data.get(field), expected_type)
        ]
        if invalid_worldview_fields or not worldview_data.get("worldview"):
            errors.append(
                "world_bible 结构无效: "
                f"{invalid_worldview_fields or ['worldview']}"
            )

    if errors:
        raise ChapterBranchArtifactValidationError(task_id, errors)
    return CompletedChapterBranchArtifacts(
        node_ids=frozenset(expected_node_ids),
        fingerprint=calculated_fingerprint,
        run_id=run_id,
        run_started_at=run_started_at,
    )
