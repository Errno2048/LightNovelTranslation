"""Small, JSON-serializable result contracts."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ChapterInfo:
    id: str
    edition_id: str
    position: int  # Zero-based position among narrative chapters.
    edition_order: int
    title: str
    paragraph_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RunResult:
    state: str
    job_name: str
    run_id: str
    committed_count: int
    completed: tuple[str, ...]
    artifact_dir: str
    error: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.state in {"complete", "paused_at_limit"}

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "completed": list(self.completed), "ok": self.ok}
