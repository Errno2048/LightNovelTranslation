"""Small explicit contracts. Artifacts remain portable JSON."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

SCHEMA_VERSION = "1"


@dataclass
class Paragraph:
    paragraph_id: str
    chapter_id: str
    revision_id: str
    ordinal: int
    kind: str
    text: str
    original_locator: str
    ruby: list[dict[str, str]] = field(default_factory=list)


@dataclass
class Chapter:
    chapter_id: str
    revision_id: str
    language: str
    source: str
    order: int
    title: str
    kind: str
    match_key: str | None
    locator: str
    source_url: str | None
    text: str
    paragraph_ids: list[str]
    char_count: int
    source_hash: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.language not in {"ja", "zh"} or self.order < 0:
            raise ValueError("invalid chapter language/order")
        if len(self.paragraph_ids) != len(set(self.paragraph_ids)):
            raise ValueError("duplicate paragraph ids")
        if self.char_count != len(self.text):
            raise ValueError("chapter character count mismatch")


@dataclass
class GenerationRequest:
    messages: list[dict[str, str]]
    max_tokens: int = 8192
    temperature: float = 0.2
    task: str = "translation"

    def __post_init__(self):
        if self.max_tokens <= 0 or not self.messages:
            raise ValueError("positive output budget and messages required")
        if any(m.get("role") not in {"system", "user", "assistant"} or not isinstance(m.get("content"), str) for m in self.messages):
            raise ValueError("invalid message")


@dataclass
class GenerationResult:
    text: str
    finish_reason: str | None
    model: str | None
    request_id: str | None
    usage: dict[str, Any]
    elapsed_seconds: float


@dataclass
class VisibilityPolicy:
    mode: Literal["single_gap", "block_gap", "sequential"]
    hidden_source_ids: list[str]
    reserved_source_ids: list[str] = field(default_factory=list)
    cutoff_order: int | None = None
    # Reserved for sequential raw-source lookahead. Gap examples are two-sided by mode.
    allow_future_source: bool = False

    def __post_init__(self):
        if self.mode not in {"single_gap", "block_gap", "sequential"}:
            raise ValueError("invalid visibility mode")
        if self.mode == "sequential" and self.cutoff_order is None:
            raise ValueError("sequential mode requires a cutoff")
        if self.allow_future_source:
            raise ValueError("sequential raw-source lookahead is not implemented in P3")


def record(value) -> dict:
    return {"schema_version": SCHEMA_VERSION, **asdict(value)}

