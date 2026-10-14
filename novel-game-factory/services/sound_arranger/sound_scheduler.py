from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import re
import threading
import wave

import numpy as np


logger = logging.getLogger(__name__)

IMPORTANCE_RANK = {"critical": 0, "important": 1, "normal": 2}
SIMILARITY_THRESHOLDS = {
    "bgm": 0.85,
    "sfx": 0.75,
    "ambience": 0.85,
}
SOFT_TARGETS = {"bgm": 100, "sfx": 152, "ambience": 48}
RESOLUTION_SOURCES = (
    "existing_canonical",
    "global_cache_hit",
    "direct_reuse",
    "cluster_representative_generated",
    "cluster_member_copied",
)


class SoundScheduleMetrics:
    """Thread-safe counters for how scheduled sound targets were resolved."""

    def __init__(self):
        self._counts = {source: 0 for source in RESOLUTION_SOURCES}
        self._counts.update(
            {
                "global_cache_write_success": 0,
                "global_cache_write_failure": 0,
            }
        )
        self._lock = threading.Lock()

    def record_resolution(self, source: str, amount: int = 1) -> None:
        if source not in RESOLUTION_SOURCES:
            raise ValueError(f"unknown sound resolution source: {source}")
        with self._lock:
            self._counts[source] += int(amount)

    def record_cache_write(self, status: str, amount: int = 1) -> None:
        key = f"global_cache_write_{str(status).strip().lower()}"
        if key not in {
            "global_cache_write_success",
            "global_cache_write_failure",
        }:
            raise ValueError(f"unknown global cache write status: {status}")
        with self._lock:
            self._counts[key] += int(amount)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)


@dataclass
class ScheduledAudioTask:
    task_id: str
    node_id: str
    audio_type: str
    prompt: str
    duration: object
    base_name: str
    event_id: str
    generation_metadata: dict | None
    importance: str
    importance_reason: str
    node_order: int
    start_order: int
    beat_coverage: int
    prompt_vector: np.ndarray | None = None
    canonical_path: str | None = None

    @property
    def member_key(self) -> tuple[str, str]:
        return str(self.node_id), str(self.event_id)

    def as_legacy_tuple(self) -> tuple:
        return (
            self.task_id,
            self.node_id,
            self.audio_type,
            self.prompt,
            self.duration,
            self.base_name,
            self.event_id,
            self.generation_metadata,
        )


@dataclass
class AudioCluster:
    representative: ScheduledAudioTask
    members: list[ScheduledAudioTask]

    @property
    def importance(self) -> str:
        if not self.members:
            return normalize_scheduler_importance(
                self.representative.importance
            )
        return min(
            (
                normalize_scheduler_importance(member.importance)
                for member in self.members
            ),
            key=IMPORTANCE_RANK.__getitem__,
        )

    @property
    def member_count(self) -> int:
        return len(self.members)

    @property
    def covered_node_ids(self) -> set[str]:
        return {str(member.node_id) for member in self.members}

    @property
    def beat_coverage(self) -> int:
        return sum(max(int(member.beat_coverage), 1) for member in self.members)

    @property
    def first_node_order(self) -> int:
        members = self.members or [self.representative]
        return min(int(member.node_order) for member in members)

    @property
    def first_start_order(self) -> int:
        members = self.members or [self.representative]
        return min(int(member.start_order) for member in members)


@dataclass
class ReusableAudioSource:
    asset_id: str
    source_path: str
    compatibility_key: tuple
    prompt_vector: np.ndarray


@dataclass
class CoveragePhaseResult:
    covered_node_ids: set[str]
    remaining_bgm_clusters: list[AudioCluster]


def normalize_scheduler_importance(value: object) -> str:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in IMPORTANCE_RANK:
            return normalized
    return "normal"


def semantic_similarity_threshold(audio_type: object) -> float | None:
    """Return the configured reuse threshold for one sound track."""
    if not isinstance(audio_type, str):
        return None
    return SIMILARITY_THRESHOLDS.get(audio_type.strip().lower())


def is_valid_wav(path: str | Path) -> bool:
    try:
        resolved = Path(path)
        if not resolved.is_file() or resolved.stat().st_size <= 44:
            return False
        with wave.open(str(resolved), "rb") as wav_file:
            return (
                wav_file.getnframes() > 0
                and wav_file.getframerate() > 0
                and wav_file.getnchannels() > 0
                and wav_file.getsampwidth() > 0
            )
    except (OSError, EOFError, wave.Error, TypeError, ValueError):
        return False


def deduplicate_canonical_targets(
    candidates: list[ScheduledAudioTask],
) -> tuple[
    list[ScheduledAudioTask],
    list[tuple[str, str, str, str]],
]:
    deduplicated: list[ScheduledAudioTask] = []
    duplicate_keys: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for candidate in candidates:
        key = (
            str(candidate.task_id),
            str(candidate.node_id),
            str(candidate.audio_type),
            str(candidate.event_id),
        )
        if key in seen:
            duplicate_keys.append(key)
            continue
        seen.add(key)
        deduplicated.append(candidate)
    return deduplicated, duplicate_keys


def _metadata_dict(candidate: ScheduledAudioTask) -> dict | None:
    metadata = candidate.generation_metadata
    if isinstance(metadata, dict):
        return metadata
    if hasattr(metadata, "model_dump"):
        dumped = metadata.model_dump()
        return dumped if isinstance(dumped, dict) else None
    return None


def compatibility_key(candidate: ScheduledAudioTask) -> tuple:
    metadata = _metadata_dict(candidate)
    if metadata is not None:
        if candidate.audio_type == "bgm":
            primary = metadata.get("primary_instrument")
            if isinstance(primary, str) and primary.strip():
                return "bgm", primary.strip().lower()
        elif candidate.audio_type in {"sfx", "ambience"}:
            signature = metadata.get("generation_signature")
            if isinstance(signature, str) and signature.strip():
                return candidate.audio_type, signature.strip()

    return (
        "unique",
        str(candidate.audio_type),
        str(candidate.task_id),
        str(candidate.node_id),
        str(candidate.event_id),
    )


def _cosine_similarity(left: np.ndarray, right: np.ndarray) -> float:
    left_array = np.asarray(left, dtype=float)
    right_array = np.asarray(right, dtype=float)
    if left_array.shape != right_array.shape:
        return -1.0
    denominator = np.linalg.norm(left_array) * np.linalg.norm(right_array)
    if denominator <= 0:
        return -1.0
    return float(np.dot(left_array, right_array) / denominator)


def assign_direct_sources(
    sources: list[ReusableAudioSource],
    candidates: list[ScheduledAudioTask],
    threshold: float | None = None,
) -> list[tuple[ReusableAudioSource, ScheduledAudioTask]]:
    assignments: list[tuple[ReusableAudioSource, ScheduledAudioTask]] = []
    frozen_sources = tuple(sources)
    for candidate in candidates:
        if candidate.prompt_vector is None:
            continue
        effective_threshold = (
            threshold
            if threshold is not None
            else semantic_similarity_threshold(candidate.audio_type)
        )
        if effective_threshold is None:
            logger.warning(
                "sound_scheduler_unknown_audio_type: event_id=%s audio_type=%s",
                candidate.event_id,
                candidate.audio_type,
            )
            continue
        candidate_key = compatibility_key(candidate)
        matches: list[tuple[float, ReusableAudioSource]] = []
        for source in frozen_sources:
            if source.compatibility_key != candidate_key:
                continue
            score = _cosine_similarity(
                source.prompt_vector,
                candidate.prompt_vector,
            )
            if score >= effective_threshold:
                matches.append((score, source))
        if not matches:
            continue
        matches.sort(
            key=lambda item: (
                -item[0],
                str(item[1].asset_id),
                str(item[1].source_path),
            )
        )
        assignments.append((matches[0][1], candidate))
    return assignments


def _candidate_order_key(candidate: ScheduledAudioTask) -> tuple:
    return (
        IMPORTANCE_RANK[normalize_scheduler_importance(candidate.importance)],
        -max(int(candidate.beat_coverage), 1),
        int(candidate.node_order),
        int(candidate.start_order),
        str(candidate.event_id),
        str(candidate.node_id),
    )


def cluster_candidates(
    candidates: list[ScheduledAudioTask],
    threshold: float | None = None,
) -> list[AudioCluster]:
    grouped: dict[tuple, list[ScheduledAudioTask]] = {}
    for candidate in sorted(candidates, key=_candidate_order_key):
        grouped.setdefault(compatibility_key(candidate), []).append(candidate)

    clusters: list[AudioCluster] = []
    for members in grouped.values():
        group_clusters: list[AudioCluster] = []
        for candidate in members:
            matches: list[tuple[float, AudioCluster]] = []
            effective_threshold = (
                threshold
                if threshold is not None
                else semantic_similarity_threshold(candidate.audio_type)
            )
            if candidate.prompt_vector is not None and effective_threshold is not None:
                for cluster in group_clusters:
                    representative_vector = cluster.representative.prompt_vector
                    if representative_vector is None:
                        continue
                    score = _cosine_similarity(
                        representative_vector,
                        candidate.prompt_vector,
                    )
                    if score >= effective_threshold:
                        matches.append((score, cluster))
            if not matches:
                group_clusters.append(
                    AudioCluster(
                        representative=candidate,
                        members=[candidate],
                    )
                )
                continue
            matches.sort(
                key=lambda item: (
                    -item[0],
                    _candidate_order_key(item[1].representative),
                )
            )
            matches[0][1].members.append(candidate)
        clusters.extend(group_clusters)
    return clusters


def _natural_sort_key(value: object) -> tuple:
    parts = re.split(r"(\d+)", str(value))
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.lower())
        for part in parts
        if part != ""
    )


def resolve_node_order(sound_data: dict) -> dict[str, int]:
    node_ids = [str(key) for key in sound_data if key != "_meta"]
    metadata = sound_data.get("_meta")
    configured = metadata.get("node_order") if isinstance(metadata, dict) else None
    if isinstance(configured, list):
        ordered: list[str] = []
        for node_id in configured:
            normalized = str(node_id)
            if normalized in node_ids and normalized not in ordered:
                ordered.append(normalized)
        missing = [node_id for node_id in node_ids if node_id not in ordered]
        if missing:
            logger.warning(
                "missing_sound_node_order: metadata omitted nodes=%s",
                missing,
            )
            ordered.extend(sorted(missing, key=_natural_sort_key))
        return {node_id: index for index, node_id in enumerate(ordered)}

    logger.warning("missing_sound_node_order: falling back to natural order")
    ordered = sorted(node_ids, key=_natural_sort_key)
    return {node_id: index for index, node_id in enumerate(ordered)}


def select_bgm_coverage_wave(
    clusters: list[AudioCluster],
    uncovered_nodes: set[str],
    max_workers: int,
) -> list[AudioCluster]:
    eligible = [
        cluster
        for cluster in clusters
        if cluster.representative.audio_type == "bgm"
        and cluster.covered_node_ids.intersection(uncovered_nodes)
    ]
    eligible.sort(
        key=lambda cluster: (
            -len(cluster.covered_node_ids.intersection(uncovered_nodes)),
            IMPORTANCE_RANK[cluster.importance],
            -cluster.beat_coverage,
            cluster.first_node_order,
            cluster.first_start_order,
            str(cluster.representative.event_id),
        )
    )

    selected: list[AudioCluster] = []
    claimed_nodes: set[str] = set()
    for cluster in eligible:
        covered = cluster.covered_node_ids.intersection(uncovered_nodes)
        if not covered or covered.intersection(claimed_nodes):
            continue
        selected.append(cluster)
        claimed_nodes.update(covered)
        if len(selected) >= max(0, int(max_workers)):
            break
    return selected


def order_competition_clusters(
    clusters: list[AudioCluster],
    success_counts: dict[str, int],
) -> list[AudioCluster]:
    def priority_group(audio_type: str, importance: str) -> int:
        if audio_type == "bgm":
            return IMPORTANCE_RANK[importance]
        if audio_type == "sfx":
            return 3 if importance == "critical" else 5
        if audio_type == "ambience":
            return 4 if importance in {"critical", "important"} else 6
        return 7

    def competition_key(cluster: AudioCluster) -> tuple:
        audio_type = cluster.representative.audio_type
        importance = cluster.importance
        target = SOFT_TARGETS.get(audio_type, 1)
        budget_ratio = int(success_counts.get(audio_type, 0)) / target
        return (
            priority_group(audio_type, importance),
            IMPORTANCE_RANK[importance],
            -cluster.member_count,
            -cluster.beat_coverage,
            budget_ratio,
            cluster.first_node_order,
            cluster.first_start_order,
            str(cluster.representative.event_id),
        )

    return sorted(clusters, key=competition_key)
