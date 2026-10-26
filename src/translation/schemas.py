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
    messages: list[dict[str, Any]]
    max_tokens: int = 8192
    temperature: float = 0.2
    task: str = "translation"
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    allow_truncated: bool = False

    def __post_init__(self):
        if self.max_tokens <= 0 or not self.messages:
            raise ValueError("positive output budget and messages required")
        for message in self.messages:
            role = message.get("role")
            if role not in {"system", "user", "assistant", "tool"}:
                raise ValueError("invalid message role")
            content = message.get("content")
            if role == "assistant":
                if content is not None and not isinstance(content, str):
                    raise ValueError("invalid assistant content")
                tool_calls = message.get("tool_calls")
                if tool_calls is not None and not isinstance(tool_calls, list):
                    raise ValueError("invalid assistant tool_calls")
                if content is None and not tool_calls:
                    raise ValueError("assistant message needs content or tool_calls")
            else:
                if not isinstance(content, str):
                    raise ValueError("invalid message content")
                if role == "tool" and not isinstance(message.get("tool_call_id"), str):
                    raise ValueError("tool message needs tool_call_id")


@dataclass
class GenerationResult:
    text: str
    finish_reason: str | None
    model: str | None
    request_id: str | None
    usage: dict[str, Any]
    elapsed_seconds: float
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


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

