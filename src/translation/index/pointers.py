"""Monotonic pointers and deterministic block identities."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Iterable, Mapping


SCALAR_POINTERS = ("translated_through", "boundary_through", "reviewed_through")


@dataclass(frozen=True)
class PointerSet:
    """Positions are chapter indices; -1 means "nothing completed yet"."""

    translated_through: int = -1
    boundary_through: int = -1
    reviewed_through: int = -1
    materialized_through: Mapping[int, int] = field(default_factory=dict)
    accepted_through: Mapping[int, int] = field(default_factory=dict)

    def __post_init__(self):
        for name in SCALAR_POINTERS:
            value = getattr(self, name)
            if type(value) is not int or value < -1:
                raise ValueError(f"{name} must be an integer >= -1")
        for name in ("materialized_through", "accepted_through"):
            values = dict(getattr(self, name))
            for level, value in values.items():
                if type(level) is not int or level < 1:
                    raise ValueError("pointer levels must be positive integers")
                if type(value) is not int or value < -1:
                    raise ValueError("pointer positions must be integers >= -1")
            object.__setattr__(self, name, values)

    def materialized(self, level: int) -> int:
        return self.materialized_through.get(level, -1)

    def accepted(self, level: int) -> int:
        return self.accepted_through.get(level, -1)

    def with_scalar(self, name: str, value: int) -> "PointerSet":
        if name not in SCALAR_POINTERS:
            raise ValueError("unknown scalar pointer")
        if type(value) is not int or value < -1:
            raise ValueError("pointer must be an integer >= -1")
        if value < getattr(self, name):
            raise ValueError("pointers are monotonic")
        return replace(self, **{name: value})

    def with_materialized(self, level: int, value: int) -> "PointerSet":
        return self._with_level("materialized_through", level, value)

    def with_accepted(self, level: int, value: int) -> "PointerSet":
        return self._with_level("accepted_through", level, value)

    def _with_level(self, name: str, level: int, value: int) -> "PointerSet":
        if type(level) is not int or level < 1:
            raise ValueError("level must be a positive integer")
        if type(value) is not int or value < -1:
            raise ValueError("pointer must be an integer >= -1")
        values = dict(getattr(self, name))
        if value < values.get(level, -1):
            raise ValueError("pointers are monotonic")
        values[level] = value
        return replace(self, **{name: values})


@dataclass(frozen=True)
class PointerEvent:
    pointer: str
    level: int | None
    previous: int
    current: int
    reason: str


@dataclass(frozen=True)
class PointerAdvance:
    value: int
    events: tuple[PointerEvent, ...]


def contiguous_advance(current: int, completed: Iterable[int], *,
                       blocked_at: int | None = None,
                       pointer: str = "translated_through",
                       level: int | None = None) -> PointerAdvance:
    """Advance over contiguous completed positions, stopping at the first gap."""
    if type(current) is not int or current < -1:
        raise ValueError("current must be an integer >= -1")
    completed = set(completed)
    value = current
    events = []
    while True:
        candidate = value + 1
        if candidate not in completed:
            break
        if blocked_at is not None and candidate >= blocked_at:
            break
        previous = value
        value = candidate
        events.append(PointerEvent(pointer, level, previous, value, "contiguous_completed"))
    return PointerAdvance(value, tuple(events))


def block_identity(level: int, start: int, end: int, inputs_hash: str) -> tuple:
    if type(level) is not int or level < 1:
        raise ValueError("level must be a positive integer")
    if type(start) is not int or type(end) is not int or start < 0 or end <= start:
        raise ValueError("block range must satisfy 0 <= start < end")
    if not inputs_hash:
        raise ValueError("inputs_hash is required")
    return level, start, end, inputs_hash


def block_key(level: int, start: int, end: int, inputs_hash: str) -> str:
    level, start, end, inputs_hash = block_identity(level, start, end, inputs_hash)
    return f"L{level}:{start:06d}-{end:06d}:{inputs_hash[:16]}"
