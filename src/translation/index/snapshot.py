"""Immutable view of the hierarchical index at a given point."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .artifacts import Artifact, ArtifactKind, BlockStatus
from .levels import LevelConfig
from .pointers import PointerSet


@dataclass(frozen=True)
class IndexSnapshot:
    snapshot_id: str
    generation: str
    levels: LevelConfig
    artifacts: Mapping[str, Artifact] = field(default_factory=dict)
    pointers: PointerSet = field(default_factory=PointerSet)
    frozen_at: float = 0.0

    def __post_init__(self):
        if not self.snapshot_id:
            raise ValueError("snapshot_id is required")
        artifacts = dict(self.artifacts)
        for artifact_id, artifact in artifacts.items():
            if artifact_id != artifact.artifact_id:
                raise ValueError("artifact map key must equal artifact_id")
            self.levels.validate_artifact_level(artifact.level,
                                                chapter=artifact.is_chapter)
            if artifact.is_block and artifact.parent_id:
                parent = artifacts.get(artifact.parent_id)
                if parent is None or not parent.is_block:
                    raise ValueError("block parent must be another block in the snapshot")
                if parent.level != artifact.level + 1:
                    raise ValueError("block parent must be exactly one level above")
                if not set(artifact.source_ids) <= set(parent.source_ids):
                    raise ValueError("child block source_ids must be covered by its parent")
            if (artifact.status == BlockStatus.ACCEPTED.value and
                    artifact.review_mode == "rebuild_only" and
                    "rebuild_only" not in artifact.quality_labels):
                raise ValueError("rebuild_only accepted blocks must carry the quality label")
        for level in self.pointers.materialized_through:
            self.levels.require_level(level)
        for level in self.pointers.accepted_through:
            self.levels.require_level(level)
        object.__setattr__(self, "artifacts", artifacts)

    def artifact(self, artifact_id: str) -> Artifact:
        try:
            return self.artifacts[artifact_id]
        except KeyError as exc:
            raise KeyError(f"unknown artifact: {artifact_id}") from exc

    def chapters(self):
        return [artifact for artifact in self.artifacts.values()
                if artifact.kind == ArtifactKind.CHAPTER.value]

    def blocks(self, level: int | None = None):
        return [artifact for artifact in self.artifacts.values()
                if artifact.kind == ArtifactKind.BLOCK.value and
                (level is None or artifact.level == level)]

    def accepted_chapters(self):
        return [artifact for artifact in self.chapters()
                if artifact.status == "accepted"]

    def accepted_blocks(self, level: int | None = None):
        return [artifact for artifact in self.blocks(level)
                if artifact.status == BlockStatus.ACCEPTED.value]

    def block_range(self, level: int) -> tuple[int, int] | None:
        """Return the covered source-id range only when the block is contiguous."""
        blocks = sorted(self.blocks(level), key=lambda item: item.source_ids[0])
        if not blocks:
            return None
        return blocks[0].source_ids[0], blocks[-1].source_ids[-1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "generation": self.generation,
            "frozen_at": self.frozen_at,
            "levels": {
                "max_children": list(self.levels.max_children),
                "context_num_blocks": list(self.levels.context_num_blocks),
            },
            "pointers": {
                "translated_through": self.pointers.translated_through,
                "boundary_through": self.pointers.boundary_through,
                "reviewed_through": self.pointers.reviewed_through,
                "materialized_through": dict(self.pointers.materialized_through),
                "accepted_through": dict(self.pointers.accepted_through),
            },
            "artifacts": [
                {
                    "artifact_id": artifact.artifact_id,
                    "kind": artifact.kind,
                    "level": artifact.level,
                    "parent_id": artifact.parent_id,
                    "source_ids": list(artifact.source_ids),
                    "status": artifact.status,
                    "generation": artifact.generation,
                    "revision": artifact.revision,
                    "review_id": artifact.review_id,
                    "review_mode": artifact.review_mode,
                    "quality_labels": list(artifact.quality_labels),
                }
                for artifact in self.artifacts.values()
            ],
        }
