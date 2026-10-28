"""Hierarchical summary index: level 1 blocks roll up into level 2 parents.

Level 1 groups chapters (default 10); level 2 groups level 1 blocks
(default 10 children, ~100 chapters).  Blocks carry
``not_created``/``provisional``/``committed`` status; translation may use
provisional blocks while review must be grounded in accepted facts.
Planning reuses EditionIndexSpec semantics: group by token budget first,
fall back from a direct level 2 build to level 1 + rollup.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Iterable, Mapping, Sequence

from ..editions.index import EditionIndexSpec
from ..budget import RoleBudget, plan_request
from ..storage import digest

NOT_CREATED = "not_created"
PROVISIONAL = "provisional"
COMMITTED = "committed"


def _tokens(text: str) -> int:
    # Byte-based estimate matches the runner's _ByteCounter convention.
    return len(text.encode("utf-8"))


@dataclass(frozen=True)
class ChapterRef:
    order: int
    title: str
    ja_text: str
    zh_text: str | None

    @property
    def tokens(self) -> int:
        value = self.title + "\n" + self.ja_text
        if self.zh_text:
            value += "\n" + self.zh_text
        return _tokens(value) + 32


@dataclass
class SummaryBlock:
    block_id: str
    level: int
    start: int
    end: int            # exclusive
    children: tuple[str, ...]
    status: str = NOT_CREATED
    build_method: str | None = None
    summary: str = ""
    content: dict | None = None
    generation: int = 0
    paired: bool = True

    def to_row(self) -> dict:
        return {
            "block_id": self.block_id,
            "level": self.level,
            "start": self.start,
            "end": self.end,
            "children": json.dumps(list(self.children)),
            "status": self.status,
            "build_method": self.build_method,
            "summary": self.summary,
            "content": json.dumps(self.content or {}, ensure_ascii=False),
            "generation": self.generation,
            "paired": 1 if self.paired else 0,
        }


def plan_blocks(chapters: Sequence[ChapterRef], spec: EditionIndexSpec,
                capacity, counter, *, level1_capacity: int) -> dict[str, SummaryBlock]:
    """Plan level 1 then level 2 boundaries by token budget.

    ``level1_capacity`` is the input budget available for one level 1
    summary request (context window minus output/safety/reserves).
    """
    blocks: dict[str, SummaryBlock] = {}

    def make_id(level: int, start: int, end: int, children: tuple[str, ...]) -> str:
        payload = json.dumps(
            {"level": level, "start": start, "end": end, "children": list(children)},
            separators=(",", ":"))
        return "B" + digest(payload)[:20]

    level1_groups: list[list[ChapterRef]] = []
    current: list[ChapterRef] = []
    current_tokens = 0
    for chapter in chapters:
        if current and (len(current) >= spec.child_max_chapters or
                        current_tokens + chapter.tokens > level1_capacity):
            level1_groups.append(current)
            current, current_tokens = [], 0
        current.append(chapter)
        current_tokens += chapter.tokens
    if current:
        level1_groups.append(current)

    for members in level1_groups:
        children = tuple(f"C{m.order}" for m in members)
        block_id = make_id(1, members[0].order, members[-1].order + 1, children)
        blocks[block_id] = SummaryBlock(
            block_id=block_id, level=1,
            start=members[0].order, end=members[-1].order + 1,
            children=children)

    level1_ids = [bid for bid, block in blocks.items() if block.level == 1]
    parent_members: list[tuple[str, ...]] = []
    current = []
    current_span = 0
    for block_id in level1_ids:
        block = blocks[block_id]
        span = block.end - block.start
        if current and (len(current) >= spec.expected_l2_count or
                        current_span + span > spec.parent_max_chapters):
            parent_members.append(tuple(current))
            current, current_span = [], 0
        current.append(block_id)
        current_span += span
    if current:
        parent_members.append(tuple(current))

    for members in parent_members:
        first = blocks[members[0]]
        last = blocks[members[-1]]
        block_id = make_id(2, first.start, last.end, members)
        blocks[block_id] = SummaryBlock(
            block_id=block_id, level=2, start=first.start, end=last.end,
            children=members)
    return blocks


def coverage_gap(blocks: Mapping[str, SummaryBlock], start: int, end: int) -> list[tuple[int, int]]:
    """Return uncovered [start, end) ranges given committed-or-provisional blocks."""
    ranges = sorted(
        (block.start, block.end) for block in blocks.values()
        if block.status in {PROVISIONAL, COMMITTED}
        and block.start < end and block.end > start)
    gaps: list[tuple[int, int]] = []
    cursor = start
    for block_start, block_end in ranges:
        if block_start > cursor:
            gaps.append((cursor, min(block_start, end)))
        cursor = max(cursor, block_end)
        if cursor >= end:
            break
    if cursor < end:
        gaps.append((cursor, end))
    return [gap for gap in gaps if gap[0] < gap[1]]
