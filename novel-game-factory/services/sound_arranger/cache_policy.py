from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)


CacheMode = Literal["reuse", "bypass"]

_SIGNATURE_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def normalize_duration_seconds(duration: object) -> int:
    """Normalize an audio duration to the provider-supported 1..190 range."""
    if isinstance(duration, bool):
        raise TypeError("duration must be a finite number, not bool")

    numeric_duration = float(duration)
    if not math.isfinite(numeric_duration):
        raise ValueError("duration must be finite")

    return max(1, min(int(numeric_duration), 190))


@dataclass(frozen=True)
class AudioCachePartition:
    cache_namespace: str
    model_alias: str
    audio_type: str
    prompt_version: str
    primary_instrument: str
    supporting_instruments: tuple[str, ...]
    bpm: int
    duration_seconds: int
    audio_format: str
    steps: int

    def __post_init__(self) -> None:
        normalized_supporting = tuple(
            sorted(
                {
                    str(item).strip()
                    for item in self.supporting_instruments
                    if str(item).strip()
                }
            )
        )
        object.__setattr__(
            self,
            "supporting_instruments",
            normalized_supporting,
        )

    def payload(self) -> dict[str, object]:
        return {
            "cache_namespace": self.cache_namespace,
            "model_alias": self.model_alias,
            "audio_type": self.audio_type,
            "prompt_version": self.prompt_version,
            "primary_instrument": self.primary_instrument,
            "supporting_instruments": self.supporting_instruments,
            "bpm": self.bpm,
            "duration_seconds": self.duration_seconds,
            "audio_format": self.audio_format,
            "steps": self.steps,
        }

    def signature(self) -> str:
        raw = json.dumps(
            self.payload(),
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class GenerationMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generation_signature: str = Field(pattern=r"^[0-9a-f]{64}$")
    cache_namespace: str = Field(min_length=1)
    model_alias: str = Field(min_length=1)
    audio_type: Literal["bgm", "sfx", "ambience"]
    prompt_version: str = Field(min_length=1)
    primary_instrument: str
    supporting_instruments: list[str]
    bpm: int = Field(ge=0)
    duration_seconds: int = Field(ge=1, le=190)
    audio_format: str = Field(min_length=1)
    steps: int = Field(ge=1)
    music_style: str
    emotion_target: str
    provider_url: str = Field(min_length=1)

    @field_validator("supporting_instruments")
    @classmethod
    def normalize_supporting(cls, values: list[str]) -> list[str]:
        return sorted({str(value).strip() for value in values if str(value).strip()})

    @model_validator(mode="after")
    def validate_audio_type_fields(self):
        if self.audio_type == "bgm":
            if not self.primary_instrument.strip():
                raise ValueError("BGM generation metadata requires primary_instrument")
            if self.bpm < 1:
                raise ValueError("BGM generation metadata requires positive bpm")
            return self

        has_musical_fields = any(
            (
                self.primary_instrument.strip(),
                self.supporting_instruments,
                self.music_style.strip(),
                self.emotion_target.strip(),
            )
        )
        if has_musical_fields or self.bpm != 0:
            raise ValueError(
                "SFX/Ambience metadata requires empty musical fields and bpm=0"
            )
        return self


def build_generation_metadata(
    *,
    cache_namespace: str,
    model_alias: str,
    audio_type: Literal["bgm", "sfx", "ambience"],
    prompt_version: str,
    primary_instrument: str,
    supporting_instruments: tuple[str, ...] | list[str],
    bpm: int,
    raw_duration: object,
    audio_format: str,
    steps: int,
    music_style: str,
    emotion_target: str,
    provider_url: str,
) -> GenerationMetadata:
    duration_seconds = normalize_duration_seconds(raw_duration)
    partition = AudioCachePartition(
        cache_namespace=cache_namespace,
        model_alias=model_alias,
        audio_type=audio_type,
        prompt_version=prompt_version,
        primary_instrument=primary_instrument,
        supporting_instruments=tuple(supporting_instruments),
        bpm=bpm,
        duration_seconds=duration_seconds,
        audio_format=audio_format,
        steps=steps,
    )

    return GenerationMetadata(
        generation_signature=partition.signature(),
        cache_namespace=partition.cache_namespace,
        model_alias=partition.model_alias,
        audio_type=audio_type,
        prompt_version=partition.prompt_version,
        primary_instrument=partition.primary_instrument,
        supporting_instruments=list(partition.supporting_instruments),
        bpm=partition.bpm,
        duration_seconds=partition.duration_seconds,
        audio_format=partition.audio_format,
        steps=partition.steps,
        music_style=music_style,
        emotion_target=emotion_target,
        provider_url=provider_url,
    )


def normalize_generation_metadata(raw_metadata: object) -> GenerationMetadata | None:
    if not isinstance(raw_metadata, dict):
        return None

    try:
        metadata = GenerationMetadata.model_validate(raw_metadata)
    except ValidationError:
        return None

    partition = AudioCachePartition(
        cache_namespace=metadata.cache_namespace,
        model_alias=metadata.model_alias,
        audio_type=metadata.audio_type,
        prompt_version=metadata.prompt_version,
        primary_instrument=metadata.primary_instrument,
        supporting_instruments=tuple(metadata.supporting_instruments),
        bpm=metadata.bpm,
        duration_seconds=metadata.duration_seconds,
        audio_format=metadata.audio_format,
        steps=metadata.steps,
    )
    if partition.signature() != metadata.generation_signature:
        return None
    return metadata


def resolve_effective_cache_mode(
    requested_cache_mode: CacheMode,
    raw_metadata: object,
) -> CacheMode:
    if requested_cache_mode != "reuse":
        return "bypass"
    if normalize_generation_metadata(raw_metadata) is None:
        return "bypass"
    return "reuse"


def build_asset_id(
    base_name: str,
    prompt: str,
    generation_signature: str,
) -> str:
    if not _SIGNATURE_PATTERN.fullmatch(generation_signature):
        raise ValueError("generation_signature must be a lowercase SHA-256 digest")

    prompt_hash = hashlib.md5(prompt.encode("utf-8")).hexdigest()[:6]
    suffix = f"_{prompt_hash}_{generation_signature[:8]}"
    max_base_length = 100 - len(suffix)
    truncated_base = str(base_name).strip()[:max_base_length].rstrip("_")
    if not truncated_base:
        truncated_base = "audio"[:max_base_length]
    return f"{truncated_base}{suffix}"
