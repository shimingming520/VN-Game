"""Validation and normalization for soft-branch variable effects."""

from __future__ import annotations

import ast
import math
import re
from typing import Any


DEFAULT_EFFECT_CAP = 8
MIN_EFFECT_CAP = 3
MAX_EFFECT_CAP = 15
RUNTIME_VALUE_MIN = 0
RUNTIME_VALUE_MAX = 100
DEFAULT_ENDING_PROGRESS_RATIO = 0.70
DEFAULT_ENDING_RATIO_THRESHOLD = 0.65
DEFAULT_ENDING_MARGIN_RATIO = 0.10
DEFAULT_MINIMUM_EFFECTIVE_CHOICES = 3
_EFFECT_PAIR_RE = re.compile(r"effects_a=(\{[^}]*\})\s+effects_b=(\{[^}]*\})")


def _clamp_runtime_value(value: int | float) -> int:
    return max(RUNTIME_VALUE_MIN, min(RUNTIME_VALUE_MAX, int(value)))


def extract_effect_pairs_from_markdown(
    content: str,
) -> tuple[list[tuple[dict[str, int], dict[str, int]]], list[str]]:
    """Parse every persisted effects marker using the same contract as game_builder."""
    pairs: list[tuple[dict[str, int], dict[str, int]]] = []
    issues: list[str] = []
    for index, match in enumerate(_EFFECT_PAIR_RE.finditer(content or ""), 1):
        try:
            effects_a = ast.literal_eval(match.group(1))
            effects_b = ast.literal_eval(match.group(2))
        except (SyntaxError, ValueError):
            issues.append(f"第 {index} 个 effects marker 无法解析")
            continue
        if not isinstance(effects_a, dict) or not isinstance(effects_b, dict):
            issues.append(f"第 {index} 个 effects marker 不是对象")
            continue
        pairs.append((effects_a, effects_b))
    return pairs, issues


def merge_effect_caps_with_observed(
    caps: dict[str, int] | None,
    effect_pairs: list[tuple[dict[str, int], dict[str, int]]] | None,
) -> dict[str, int]:
    """Preserve the minimum cap required by already persisted effects during resume."""
    merged = {
        str(key): max(1, int(value))
        for key, value in (caps or {}).items()
    }
    for effects_a, effects_b in effect_pairs or []:
        for effects in (effects_a or {}, effects_b or {}):
            for key, value in effects.items():
                try:
                    magnitude = abs(int(value))
                except (TypeError, ValueError):
                    continue
                merged[str(key)] = max(merged.get(str(key), 1), magnitude)
    return merged


def build_effect_key_schedule(
    variable_defs: list[dict] | None,
    total_branches: int,
) -> list[str]:
    """Return a stable round-robin schedule so every route axis gets narrative coverage."""
    keys = [
        str(item["key"])
        for item in variable_defs or []
        if isinstance(item, dict) and item.get("key")
    ]
    if not keys:
        return []
    return [keys[index % len(keys)] for index in range(max(int(total_branches or 0), 0))]


def calculate_actual_effect_reachability(
    variable_defs: list[dict] | None,
    effect_pairs: list[tuple[dict[str, int], dict[str, int]]] | None,
) -> dict[str, dict[str, Any]]:
    """Calculate each route's real maximum from the saved A/B choices."""
    result: dict[str, dict[str, Any]] = {}
    pairs = effect_pairs or []
    for variable in variable_defs or []:
        if not isinstance(variable, dict) or not variable.get("key"):
            continue
        key = str(variable["key"])
        initial = _clamp_runtime_value(variable.get("init", 50))
        threshold = int(variable.get("threshold", initial))
        gains = []
        for effects_a, effects_b in pairs:
            value_a = int((effects_a or {}).get(key, 0))
            value_b = int((effects_b or {}).get(key, 0))
            gains.append(max(0, value_a, value_b))
        max_value = _clamp_runtime_value(initial + sum(gains))
        result[key] = {
            "initial": initial,
            "threshold": threshold,
            "max_value": max_value,
            "positive_choices": sum(1 for gain in gains if gain > 0),
            "reachable": max_value >= threshold,
        }
    return result


def _route_rule_satisfied(
    target_index: int,
    values: tuple[int, ...],
    positives: tuple[int, ...],
    effectives: tuple[int, ...],
    variable_defs: list[dict],
) -> bool:
    variable = variable_defs[target_index]
    if not bool(variable.get("route_enabled", True)):
        return False
    effective = effectives[target_index]
    if effective < int(
        variable.get("minimum_effective_choices", DEFAULT_MINIMUM_EFFECTIVE_CHOICES)
    ):
        return False
    tendency = positives[target_index] / effective if effective else 0.0
    competitor_tendencies = [
        positives[index] / effectives[index] if effectives[index] else 0.0
        for index in range(len(variable_defs))
        if index != target_index
    ]
    competitor_tendency = max(competitor_tendencies, default=0.0)
    return (
        values[target_index] >= int(variable.get("threshold", 50))
        and tendency >= float(
            variable.get("ratio_threshold", DEFAULT_ENDING_RATIO_THRESHOLD)
        )
        and tendency - competitor_tendency
        >= float(variable.get("margin_ratio", DEFAULT_ENDING_MARGIN_RATIO))
    )


def simulate_ending_route_reachability(
    variable_defs: list[dict] | None,
    effect_pairs: list[tuple[dict[str, int], dict[str, int]]] | None,
    *,
    beam_width: int = 256,
) -> dict[str, dict[str, Any]]:
    """Verify that each ending can be reached by one real sequence of A/B choices.

    The state transition mirrors both game builders: values are clamped to 0..100,
    while positive/effective counters only change when the selected option contains
    a non-zero effect for that variable.  A bounded beam keeps long novels tractable.
    """
    variables = [
        item
        for item in variable_defs or []
        if isinstance(item, dict) and item.get("key")
    ]
    keys = [str(item["key"]) for item in variables]
    pairs = effect_pairs or []
    initial_values = tuple(
        _clamp_runtime_value(item.get("init", 50)) for item in variables
    )
    initial_counts = tuple(0 for _ in variables)
    result: dict[str, dict[str, Any]] = {}

    for target_index, variable in enumerate(variables):
        key = keys[target_index]
        if not bool(variable.get("route_enabled", True)):
            result[key] = {"reachable": False, "reason": "route_disabled"}
            continue

        states = {(initial_values, initial_counts, initial_counts)}
        for effects_a, effects_b in pairs:
            next_states = set()
            for values, positives, effectives in states:
                for selected in (effects_a or {}, effects_b or {}):
                    next_values = list(values)
                    next_positives = list(positives)
                    next_effectives = list(effectives)
                    for index, effect_key in enumerate(keys):
                        try:
                            delta = int(selected.get(effect_key, 0) or 0)
                        except (TypeError, ValueError):
                            delta = 0
                        if delta == 0:
                            continue
                        next_values[index] = _clamp_runtime_value(
                            next_values[index] + delta
                        )
                        next_effectives[index] += 1
                        if delta > 0:
                            next_positives[index] += 1
                    next_states.add(
                        (
                            tuple(next_values),
                            tuple(next_positives),
                            tuple(next_effectives),
                        )
                    )

            if len(next_states) > max(2, int(beam_width)):
                def score(state: tuple) -> tuple[float, ...]:
                    values, positives, effectives = state
                    target_effective = effectives[target_index]
                    target_tendency = (
                        positives[target_index] / target_effective
                        if target_effective else 0.0
                    )
                    competitor = max(
                        (
                            positives[index] / effectives[index]
                            if effectives[index] else 0.0
                        )
                        for index in range(len(variables))
                        if index != target_index
                    ) if len(variables) > 1 else 0.0
                    return (
                        float(values[target_index]),
                        target_tendency - competitor,
                        target_tendency,
                        float(target_effective),
                    )

                states = set(
                    sorted(next_states, key=score, reverse=True)[: int(beam_width)]
                )
            else:
                states = next_states

        winning_state = next(
            (
                state
                for state in states
                if _route_rule_satisfied(
                    target_index,
                    state[0],
                    state[1],
                    state[2],
                    variables,
                )
            ),
            None,
        )
        if winning_state is None:
            result[key] = {
                "reachable": False,
                "reason": "normalized_route_rules",
                "explored_states": len(states),
            }
            continue
        values, positives, effectives = winning_state
        target_effective = effectives[target_index]
        target_tendency = positives[target_index] / target_effective
        result[key] = {
            "reachable": True,
            "reason": "reachable",
            "value": values[target_index],
            "positive": positives[target_index],
            "effective": target_effective,
            "tendency": target_tendency,
            "explored_states": len(states),
        }
    return result


def _sync_ending_rule_to_topology(
    topology: list[dict] | None,
    variable: dict,
) -> None:
    key = str(variable.get("key") or "")
    if not key:
        return
    for node in topology or []:
        if not isinstance(node, dict) or node.get("type") != "ending":
            continue
        if str(node.get("branch_variable") or "") != key:
            continue
        node["branch_threshold"] = int(variable["threshold"])
        node["branch_ratio_threshold"] = float(variable["ratio_threshold"])
        node["branch_margin_ratio"] = float(variable["margin_ratio"])
        node["effective_choice_count"] = int(variable["effective_choice_count"])
        node["minimum_effective_choices"] = int(
            variable["minimum_effective_choices"]
        )
        node["route_enabled"] = bool(variable["route_enabled"])


def _describe_ending_rule(variable: dict) -> str:
    key = str(variable.get("key") or "")
    if not bool(variable.get("route_enabled", True)):
        return f"{key} 数值结局已禁用，回退普通/原著结局"
    return (
        f">= {int(variable['threshold'])}，有效选择 >= "
        f"{int(variable['minimum_effective_choices'])}，正向倾向 >= "
        f"{float(variable['ratio_threshold']):.0%}，且倾向领先 >= "
        f"{float(variable['margin_ratio']):.0%} → {key} 结局"
    )


def calibrate_ending_rules(
    variable_defs: list[dict] | None,
    topology: list[dict] | None,
    effect_pairs: list[tuple[dict[str, int], dict[str, int]]] | None,
    *,
    progress_ratio: float = DEFAULT_ENDING_PROGRESS_RATIO,
    ratio_threshold: float = DEFAULT_ENDING_RATIO_THRESHOLD,
    margin_ratio: float = DEFAULT_ENDING_MARGIN_RATIO,
    minimum_effective_choices: int = DEFAULT_MINIMUM_EFFECTIVE_CHOICES,
) -> list[str]:
    """Calibrate every ending once, then validate it against real A/B paths.

    Short stories use their observed reachable span; long stories naturally cap at
    100 and therefore receive a higher raw threshold.  If normalized rules remain
    impossible after conservative relaxation, only that route is disabled.
    """
    variables = [
        item
        for item in variable_defs or []
        if isinstance(item, dict) and item.get("key")
    ]
    pairs = effect_pairs or []
    reachability = calculate_actual_effect_reachability(variables, pairs)
    issues: list[str] = []
    minimum_choices = max(1, int(minimum_effective_choices))
    progress = max(0.0, min(1.0, float(progress_ratio)))

    for variable in variables:
        key = str(variable["key"])
        route = reachability[key]
        initial = int(route["initial"])
        max_value = int(route["max_value"])
        coverage = sum(
            1
            for effects_a, effects_b in pairs
            if int((effects_a or {}).get(key, 0) or 0) != 0
            or int((effects_b or {}).get(key, 0) or 0) != 0
        )
        positive_coverage = int(route.get("positive_choices", 0))
        span = max(0, max_value - initial)
        threshold = min(max_value, initial + math.ceil(span * progress))
        route_enabled = (
            coverage >= minimum_choices
            and positive_coverage >= minimum_choices
            and span > 0
        )
        variable.update(
            {
                "threshold": threshold,
                "max_reachable_value": max_value,
                "ratio_threshold": float(ratio_threshold),
                "margin_ratio": float(margin_ratio),
                "effective_choice_count": coverage,
                "minimum_effective_choices": minimum_choices,
                "route_enabled": route_enabled,
            }
        )
        variable["condition"] = _describe_ending_rule(variable)
        if not route_enabled:
            issues.append(
                f"变量 {key} 有效选择不足（有效 {coverage}，正向覆盖 "
                f"{positive_coverage}，最低 {minimum_choices}），已禁用数值结局"
            )
        _sync_ending_rule_to_topology(topology, variable)

    simulation = simulate_ending_route_reachability(variables, pairs)
    for variable in variables:
        key = str(variable["key"])
        if not variable.get("route_enabled") or simulation[key]["reachable"]:
            continue

        original = (
            float(variable["ratio_threshold"]),
            float(variable["margin_ratio"]),
            int(variable["threshold"]),
        )
        relaxed_candidates = [
            (0.60, float(variable["margin_ratio"]), int(variable["threshold"])),
            (0.60, 0.06, int(variable["threshold"])),
            (
                0.60,
                0.06,
                min(
                    int(variable["max_reachable_value"]),
                    _clamp_runtime_value(
                        int(variable.get("init", 50))
                        + math.ceil(
                            (
                                int(variable["max_reachable_value"])
                                - _clamp_runtime_value(variable.get("init", 50))
                            )
                            * 0.60
                        )
                    ),
                ),
            ),
        ]
        for candidate_ratio, candidate_margin, candidate_threshold in relaxed_candidates:
            variable["ratio_threshold"] = candidate_ratio
            variable["margin_ratio"] = candidate_margin
            variable["threshold"] = candidate_threshold
            candidate = simulate_ending_route_reachability(variables, pairs).get(key, {})
            if candidate.get("reachable"):
                issues.append(
                    f"变量 {key} 规则从 ratio={original[0]:.2f}, "
                    f"margin={original[1]:.2f}, threshold={original[2]} "
                    f"校准为 ratio={candidate_ratio:.2f}, "
                    f"margin={candidate_margin:.2f}, threshold={candidate_threshold}"
                )
                break
        else:
            variable["route_enabled"] = False
            issues.append(f"变量 {key} 不存在满足完整规则的真实选择路径，已禁用数值结局")
        variable["condition"] = _describe_ending_rule(variable)
        _sync_ending_rule_to_topology(topology, variable)

    return issues


def calibrate_thresholds_to_actual_effects(
    variable_defs: list[dict] | None,
    topology: list[dict] | None,
    effect_pairs: list[tuple[dict[str, int], dict[str, int]]] | None,
) -> list[str]:
    """Lower over-optimistic thresholds to real maxima, but never unlock uncovered axes."""
    reachability = calculate_actual_effect_reachability(variable_defs, effect_pairs)
    issues: list[str] = []
    topology_items = topology or []
    for variable in variable_defs or []:
        if not isinstance(variable, dict) or not variable.get("key"):
            continue
        key = str(variable["key"])
        route = reachability.get(key, {})
        if int(route.get("positive_choices", 0)) <= 0:
            issues.append(f"变量 {key} 没有任何正向 effects，禁止自动解锁结局")
            continue
        threshold = int(variable.get("threshold", route.get("initial", 50)))
        max_value = int(route.get("max_value", threshold))
        calibrated = min(threshold, max_value)
        if calibrated == threshold:
            continue
        variable["threshold"] = calibrated
        variable["condition"] = f">= {calibrated} → {key} 结局"
        for node in topology_items:
            if isinstance(node, dict) and node.get("branch_variable") == key:
                node["branch_threshold"] = calibrated
        issues.append(f"变量 {key} 阈值已按真实 effects 从 {threshold} 校准为 {calibrated}")
    return issues


def apply_normalized_ending_rules(
    variable_defs: list[dict] | None,
    topology: list[dict] | None,
    effect_pairs: list[tuple[dict[str, int], dict[str, int]]] | None,
    *,
    minimum_value: int = 70,
    ratio_threshold: float = 0.60,
    margin: int = 8,
) -> None:
    """Scale ending difficulty by actual coverage instead of raw reachability only."""
    pairs = effect_pairs or []
    coverage: dict[str, int] = {}
    for variable in variable_defs or []:
        if not isinstance(variable, dict) or not variable.get("key"):
            continue
        key = str(variable["key"])
        coverage[key] = sum(
            1
            for effects_a, effects_b in pairs
            if int((effects_a or {}).get(key, 0) or 0) != 0
            or int((effects_b or {}).get(key, 0) or 0) != 0
        )
        variable["threshold"] = max(int(variable.get("threshold", 50)), minimum_value)
        variable["ratio_threshold"] = float(ratio_threshold)
        variable["margin"] = int(margin)
        variable["effective_choice_count"] = coverage[key]
        variable["condition"] = (
            f">= {variable['threshold']} 且正向倾向 >= {ratio_threshold:.0%} "
            f"且领先其他变量 >= {margin} → {key} 结局"
        )
    for node in topology or []:
        if not isinstance(node, dict) or node.get("type") != "ending":
            continue
        key = str(node.get("branch_variable") or "")
        if not key or key not in coverage:
            continue
        variable = next(
            item for item in variable_defs or []
            if isinstance(item, dict) and str(item.get("key")) == key
        )
        node["branch_threshold"] = variable["threshold"]
        node["branch_ratio_threshold"] = float(ratio_threshold)
        node["branch_margin"] = int(margin)
        node["effective_choice_count"] = coverage[key]


def calculate_effect_caps(variable_defs: list[dict] | None, total_branches: int) -> dict[str, int]:
    """Calculate a bounded per-branch delta for every declared variable."""
    branch_count = max(int(total_branches or 0), 1)
    caps: dict[str, int] = {}
    for variable in variable_defs or []:
        if not isinstance(variable, dict) or not variable.get("key"):
            continue
        key = str(variable["key"])
        try:
            initial = int(variable.get("init", 50))
            threshold = variable.get("threshold")
            if threshold is None:
                raw_cap = DEFAULT_EFFECT_CAP
            else:
                distance = abs(int(threshold) - initial)
                # 向上取整，避免阈值刚好因 round 向下舍入而变成不可达。
                raw_cap = math.ceil(distance / (branch_count * 0.65))
        except (TypeError, ValueError):
            raw_cap = DEFAULT_EFFECT_CAP
        caps[key] = max(MIN_EFFECT_CAP, min(MAX_EFFECT_CAP, raw_cap))
    return caps


def validate_threshold_reachability(
    variable_defs: list[dict] | None,
    total_branches: int,
    caps: dict[str, int],
    expected_direction_rate: float = 0.65,
) -> tuple[bool, list[str]]:
    """Check that a normal, consistently directed playthrough can reach each threshold."""
    branch_count = max(int(total_branches or 0), 0)
    direction_rate = max(0.0, min(1.0, float(expected_direction_rate)))
    issues: list[str] = []
    for variable in variable_defs or []:
        if not isinstance(variable, dict) or not variable.get("key"):
            continue
        key = str(variable["key"])
        threshold = variable.get("threshold")
        if threshold is None:
            continue
        try:
            initial = int(variable.get("init", 50))
            required = abs(int(threshold) - initial)
            reachable = branch_count * int(caps.get(key, DEFAULT_EFFECT_CAP)) * direction_rate
        except (TypeError, ValueError):
            issues.append(f"变量 {key} 的初始值或阈值不是整数")
            continue
        if required > reachable:
            issues.append(
                f"变量 {key} 阈值不可达：需要变化 {required}，正常路径最多约 {reachable:.1f}"
            )
    return not issues, issues


def _coerce_effects(
    effects: Any,
    valid_keys: set[str],
    caps: dict[str, int],
    label: str,
    issues: list[str],
) -> dict[str, int]:
    if not isinstance(effects, dict):
        issues.append(f"{label} 不是对象")
        return {}

    clean: dict[str, int] = {}
    for raw_key, raw_value in effects.items():
        key = str(raw_key)
        if key not in valid_keys:
            issues.append(f"{label} 包含非法 key: {key}")
            continue
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            issues.append(f"{label}.{key} 不是数值")
            continue
        if value == 0:
            issues.append(f"{label}.{key} 不能为 0")
            continue
        cap = max(1, int(caps.get(key, DEFAULT_EFFECT_CAP)))
        clean[key] = max(-cap, min(cap, value))
    return clean


def _dominant_key(effects: dict[str, int], key_order: list[str]) -> str | None:
    if not effects:
        return None
    order = {key: index for index, key in enumerate(key_order)}
    return min(effects, key=lambda key: (-abs(effects[key]), order.get(key, len(order))))


def sanitize_effect_pair(
    effects_a: Any,
    effects_b: Any,
    variable_defs: list[dict] | None,
    caps: dict[str, int],
    fallback_key: str | None = None,
    required_positive_key: str | None = None,
    allow_romance_mirror: bool = False,
) -> tuple[dict[str, int], dict[str, int], list[str]]:
    """Return a safe A/B pair with one shared, oppositely directed main axis."""
    key_order = [
        str(item["key"])
        for item in variable_defs or []
        if isinstance(item, dict) and item.get("key")
    ]
    valid_keys = set(key_order)
    issues: list[str] = []
    clean_a = _coerce_effects(effects_a, valid_keys, caps, "effects_a", issues)
    clean_b = _coerce_effects(effects_b, valid_keys, caps, "effects_b", issues)

    selected_fallback = fallback_key if fallback_key in valid_keys else None
    if selected_fallback is None:
        selected_fallback = key_order[0] if key_order else None

    def fallback(reason: str) -> tuple[dict[str, int], dict[str, int], list[str]]:
        issues.append(reason)
        if selected_fallback is None:
            return {}, {}, issues
        cap = max(1, int(caps.get(selected_fallback, DEFAULT_EFFECT_CAP)))
        return {selected_fallback: cap}, {selected_fallback: -cap}, issues

    if not clean_a or not clean_b:
        return fallback("effects 缺少有效的非零数值")
    if clean_a == clean_b:
        return fallback("effects_a 与 effects_b 构成假选择")

    dominant_a = _dominant_key(clean_a, key_order)
    dominant_b = _dominant_key(clean_b, key_order)
    if dominant_a != dominant_b:
        romance_mirror = (
            allow_romance_mirror
            and str(dominant_a or "").startswith("affection_")
            and str(dominant_b or "").startswith("affection_")
            and clean_a.get(dominant_a, 0) > 0
            and clean_b.get(dominant_b, 0) > 0
            and clean_b.get(dominant_a, 0) <= 0
            and clean_a.get(dominant_b, 0) <= 0
        )
        if not romance_mirror:
            return fallback("A/B 主导变量不一致")
    elif clean_a[dominant_a] * clean_b[dominant_b] >= 0:
        return fallback("同一主导变量必须是相反方向")

    if required_positive_key in valid_keys:
        required = str(required_positive_key)
        if max(clean_a.get(required, 0), clean_b.get(required, 0)) <= 0:
            main_strength = min(abs(clean_a[dominant_a]), abs(clean_b[dominant_b]))
            secondary_cap = min(
                max(1, int(caps.get(required, DEFAULT_EFFECT_CAP))),
                max(1, main_strength - 1),
            )
            clean_a[required] = -secondary_cap
            clean_b[required] = secondary_cap
            issues.append(f"已补齐目标变量 {required} 的正向 effects 覆盖")

    return clean_a, clean_b, issues


def sanitize_romance_effect_pair(
    effects_a: Any,
    effects_b: Any,
    variable_defs: list[dict] | None,
    caps: dict[str, int],
    fallback_key: str | None = None,
    required_positive_key: str | None = None,
) -> tuple[dict[str, int], dict[str, int], list[str]]:
    """Normalize romance effects without imposing the generic personality-axis contract.

    Romance choices may mirror two different affection axes, or may represent
    a positive/negative decision on the same route.  The generic validator's
    single-dominant-axis rule is therefore too strict for this mode.  This
    contract still removes unknown keys, clamps values, rejects empty pairs,
    and guarantees opposite directions when both sides target one axis.
    """
    key_order = [
        str(item["key"])
        for item in variable_defs or []
        if isinstance(item, dict) and item.get("key")
    ]
    valid_keys = set(key_order)
    issues: list[str] = []
    clean_a = _coerce_effects(effects_a, valid_keys, caps, "effects_a", issues)
    clean_b = _coerce_effects(effects_b, valid_keys, caps, "effects_b", issues)
    selected_fallback = fallback_key if fallback_key in valid_keys else (key_order[0] if key_order else None)

    def fallback(reason: str) -> tuple[dict[str, int], dict[str, int], list[str]]:
        issues.append(reason)
        if selected_fallback is None:
            return {}, {}, issues
        cap = max(1, int(caps.get(selected_fallback, DEFAULT_EFFECT_CAP)))
        return {selected_fallback: cap}, {selected_fallback: -cap}, issues

    if not clean_a or not clean_b:
        return fallback("恋爱 effects 缺少有效的非零数值")
    if clean_a == clean_b:
        return fallback("恋爱 effects_a 与 effects_b 构成假选择")

    affection_keys = {key for key in key_order if key.startswith("affection_")}
    dominant_a = _dominant_key(clean_a, key_order)
    dominant_b = _dominant_key(clean_b, key_order)
    if dominant_a == dominant_b and clean_a[dominant_a] * clean_b[dominant_b] >= 0:
        # Same route is a genuine A/B choice only when the affection direction
        # changes.  Keep the stronger side and mirror its magnitude.
        cap = max(1, int(caps.get(dominant_a, DEFAULT_EFFECT_CAP)))
        strength = min(cap, max(abs(clean_a[dominant_a]), abs(clean_b[dominant_b]), 1))
        clean_b[dominant_b] = -strength if clean_a[dominant_a] > 0 else strength
        issues.append("恋爱同路线 effects 已规范化为相反方向")
    elif dominant_a != dominant_b and not (
        dominant_a in affection_keys and dominant_b in affection_keys
    ):
        return fallback("恋爱 effects 只能在 affection 路线之间镜像")

    if required_positive_key in valid_keys:
        required = str(required_positive_key)
        if max(clean_a.get(required, 0), clean_b.get(required, 0)) <= 0:
            # Coverage scheduling is a soft requirement for romance; add a
            # small secondary trace rather than replacing the player's route.
            strength = max(1, min(int(caps.get(required, DEFAULT_EFFECT_CAP)), 2))
            clean_a[required] = -strength
            clean_b[required] = strength
            issues.append(f"已补齐恋爱目标变量 {required} 的正向 effects 覆盖")

    return clean_a, clean_b, issues
