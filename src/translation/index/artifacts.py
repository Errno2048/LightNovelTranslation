"""Chapter and block artifacts shared by the hierarchical index."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class ArtifactKind(str, Enum):
    CHAPTER = "chapter"
    BLOCK = "block"


class ChapterStatus(str, Enum):
    PLANNED = "planned"
    TRANSLATED = "translated"
    REVIEWING = "reviewing"
    ACCEPTED = "accepted"
    REVISION_REQUIRED = "revision_required"
    BLOCKED = "blocked"


class BlockStatus(str, Enum):
    PLANNED = "planned"
    PROVISIONAL = "provisional"
    REBUILD_REQUIRED = "rebuild_required"
    REVIEWING = "reviewing"
    ACCEPTED = "accepted"
    STALE = "stale"
    FAILED = "failed"


CHAPTER_STATUSES = {status.value for status in ChapterStatus}
BLOCK_STATUSES = {status.value for status in BlockStatus}
REVIEW_MODES = {"rebuild_only", "model"}


def _value(value):
    return value.value if isinstance(value, Enum) else str(value)


@dataclass(frozen=True)
class InputRef:
    source_id: str
    version_id: str
    text_hash: str = ""

    def __post_init__(self):
        if not self.source_id or not self.version_id:
            raise ValueError("input source_id and version_id are required")


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    kind: ArtifactKind | str
    level: int
    source_ids: tuple[str, ...]
    status: ChapterStatus | BlockStatus | str
    inputs: tuple[InputRef, ...] = ()
    parent_id: str | None = None
    content: Mapping[str, Any] = field(default_factory=dict)
    generation: str = ""
    revision: int = 0
    supersedes: str | None = None
    review_id: str | None = None
    review_mode: str | None = None
    quality_labels: tuple[str, ...] = ()

    def __post_init__(self):
        kind = _value(self.kind)
        status = _value(self.status)
        if kind not in {item.value for item in ArtifactKind}:
            raise ValueError("unknown artifact kind")
        allowed = CHAPTER_STATUSES if kind == ArtifactKind.CHAPTER.value else BLOCK_STATUSES
        if status not in allowed:
            raise ValueError(f"status {status!r} is invalid for {kind}")
        if not self.artifact_id:
            raise ValueError("artifact_id is required")
        if type(self.level) is not int or self.level < 0:
            raise ValueError("level must be a non-negative integer")
        source_ids = tuple(self.source_ids)
        inputs = tuple(self.inputs)
        if not source_ids:
            raise ValueError("source_ids cannot be empty")
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("source_ids must be unique")
        quality_labels = tuple(self.quality_labels)
        if self.review_mode is not None and self.review_mode not in REVIEW_MODES:
            raise ValueError("unknown review_mode")
        if kind == ArtifactKind.CHAPTER.value:
            if self.level != 0:
                raise ValueError("chapter artifacts must use level 0")
            if self.parent_id is not None:
                raise ValueError("chapter artifacts cannot have a parent block")
        else:
            if self.level < 1:
                raise ValueError("block artifacts must use level >= 1")
        if status == ChapterStatus.ACCEPTED.value and kind == ArtifactKind.CHAPTER.value:
            if not inputs:
                raise ValueError("accepted chapter must reference its accepted input")
        if kind == ArtifactKind.BLOCK.value:
            if status == BlockStatus.ACCEPTED.value:
                if not inputs:
                    raise ValueError("accepted block must record its inputs")
                if self.review_mode != "rebuild_only" and not self.review_id:
                    raise ValueError("accepted block requires review_id unless rebuild_only")
            if status == BlockStatus.PROVISIONAL.value and self.review_id is not None:
                raise ValueError("provisional block cannot have a review_id")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "source_ids", source_ids)
        object.__setattr__(self, "inputs", inputs)
        object.__setattr__(self, "quality_labels", quality_labels)

    @property
    def is_chapter(self) -> bool:
        return self.kind == ArtifactKind.CHAPTER.value

    @property
    def is_block(self) -> bool:
        return self.kind == ArtifactKind.BLOCK.value
