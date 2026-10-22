"""Lazy reference-index planning with not_created planned boundaries."""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, replace
from typing import Iterable, Mapping

from .context_plan import ContextDocument, TokenEstimator
from .levels import LevelConfig
from .pointers import block_key

DEFAULT_ESTIMATE_FACTOR = 1.15
DEFAULT_PROMPT_OVERHEAD = 2048
DEFAULT_ROLLUP_CHILD_TOKENS = 2500


class SingleChapterSplitRequired(ValueError):
    """A single chapter does not fit; TranslationUnit split is required."""


@dataclass(frozen=True)
class PlannedBlock:
    block_id: str
    level: int
    start: int
    end: int
    source_ids: tuple[str, ...]
    estimated_tokens: int
    parent_id: str | None = None
    source_kind: str = "reference"
    status: str = "not_created"
    oversized: bool = False

    def __post_init__(self):
        if self.level < 1:
            raise ValueError("planned blocks start at level 1")
        if self.start < 0 or self.end <= self.start:
            raise ValueError("invalid planned range")
        if not self.source_ids:
            raise ValueError("planned block needs source ids")
        if self.status != "not_created":
            raise ValueError("planned blocks must use not_created")
        object.__setattr__(self, "source_ids", tuple(self.source_ids))


@dataclass(frozen=True)
class ReferenceIndexPlan:
    blocks: tuple[PlannedBlock, ...]
    levels: LevelConfig
    estimate_version: str
    estimate_factor: float = DEFAULT_ESTIMATE_FACTOR

    def by_level(self, level: int) -> tuple[PlannedBlock, ...]:
        return tuple(block for block in self.blocks if block.level == level)

    def get(self, block_id: str) -> PlannedBlock:
        for block in self.blocks:
            if block.block_id == block_id:
                return block
        raise KeyError(block_id)


@dataclass(frozen=True)
class MaterializationDecision:
    mode: str
    direct_tokens: int
    rollup_tokens: int
    capacity: int
    reason: str


def estimate_chapter_tokens(document: ContextDocument, estimator: TokenEstimator,
                            *, factor: float = DEFAULT_ESTIMATE_FACTOR) -> int:
    text = document.source_text
    if document.translation_text:
        text += "\n" + document.translation_text
    return int(math.ceil(estimator.count(text) * factor))


def direct_estimate(source_ids: Iterable[str],
                    documents: Mapping[str, ContextDocument],
                    estimator: TokenEstimator, *,
                    factor: float = DEFAULT_ESTIMATE_FACTOR,
                    prompt_overhead: int = DEFAULT_PROMPT_OVERHEAD) -> int:
    return prompt_overhead + sum(
        estimate_chapter_tokens(documents[source_id], estimator, factor=factor)
        for source_id in source_ids)


def rollup_estimate(children: Iterable[PlannedBlock], *,
                    factor: float = DEFAULT_ESTIMATE_FACTOR,
                    prompt_overhead: int = DEFAULT_PROMPT_OVERHEAD,
                    child_token_cap: int = DEFAULT_ROLLUP_CHILD_TOKENS) -> int:
    return prompt_overhead + sum(
        int(math.ceil(min(child.estimated_tokens, child_token_cap) * factor))
        for child in children)


def choose_materialization(direct_tokens: int, rollup_tokens: int,
                           capacity: int) -> MaterializationDecision:
    if direct_tokens <= capacity:
        return MaterializationDecision("direct", direct_tokens, rollup_tokens,
                                       capacity, "direct_fits")
    if rollup_tokens <= capacity:
        return MaterializationDecision("rollup", direct_tokens, rollup_tokens,
                                       capacity, "direct_overflow_rollup")
    return MaterializationDecision("split", direct_tokens, rollup_tokens,
                                   capacity, "both_overflow")


def _planned_id(level: int, start: int, end: int, source_ids: Iterable[str]) -> str:
    digest = hashlib.sha256("|".join(source_ids).encode("utf-8")).hexdigest()
    return block_key(level, start, end, digest)


def _group(documents: list[ContextDocument], level: int,
           estimator: TokenEstimator, max_children: int, capacity: int,
           factor: float, prompt_overhead: int) -> list[PlannedBlock]:
    groups = []
    current = []
    current_tokens = 0

    def flush():
        nonlocal current, current_tokens
        if not current:
            return
        source_ids = tuple(document.source_id for document in current)
        start, end = current[0].order, current[-1].order + 1
        groups.append(PlannedBlock(
            block_id=_planned_id(level, start, end, source_ids),
            level=level, start=start, end=end, source_ids=source_ids,
            estimated_tokens=current_tokens,
            oversized=current_tokens + prompt_overhead > capacity))
        current = []
        current_tokens = 0

    for document in documents:
        token = estimate_chapter_tokens(document, estimator, factor=factor)
        if current and (len(current) >= max_children or
                        current_tokens + token + prompt_overhead > capacity):
            flush()
        current.append(document)
        current_tokens += token
        if len(current) >= max_children:
            flush()
    flush()
    return groups


def plan_reference_blocks(documents: Mapping[str, ContextDocument],
                          levels: LevelConfig, estimator: TokenEstimator, *,
                          capacity: int,
                          factor: float = DEFAULT_ESTIMATE_FACTOR,
                          prompt_overhead: int = DEFAULT_PROMPT_OVERHEAD,
                          estimate_version: str = "reference-plan") -> ReferenceIndexPlan:
    ordered = sorted(documents.values(), key=lambda document: document.order)
    if not ordered:
        return ReferenceIndexPlan((), levels, estimate_version, factor)
    if capacity <= 0:
        raise ValueError("capacity must be positive")
    children = _group(ordered, 1, estimator, levels.max_children_for(1),
                      capacity, factor, prompt_overhead)
    all_blocks = list(children)
    for level in range(2, levels.max_level + 1):
        groups = []
        current = []
        current_tokens = 0
        max_children = levels.max_children_for(level)

        def flush():
            nonlocal current, current_tokens
            if not current:
                return
            source_ids = tuple(source_id for child in current
                               for source_id in child.source_ids)
            start = min(child.start for child in current)
            end = max(child.end for child in current)
            parent_id = _planned_id(level, start, end, source_ids)
            parent = PlannedBlock(
                block_id=parent_id, level=level, start=start, end=end,
                source_ids=source_ids, estimated_tokens=current_tokens,
                oversized=current_tokens + prompt_overhead > capacity)
            groups.append(parent)
            for child in current:
                all_blocks[all_blocks.index(child)] = replace(
                    child, parent_id=parent_id)
            current = []
            current_tokens = 0

        for child in children:
            if current and (len(current) >= max_children or
                            current_tokens + child.estimated_tokens + prompt_overhead > capacity):
                flush()
            current.append(child)
            current_tokens += child.estimated_tokens
            if len(current) >= max_children:
                flush()
        flush()
        all_blocks.extend(groups)
        children = groups
    return ReferenceIndexPlan(tuple(all_blocks), levels, estimate_version, factor)


def split_planned_block(block: PlannedBlock,
                        documents: Mapping[str, ContextDocument],
                        estimator: TokenEstimator, *,
                        capacity: int,
                        factor: float = DEFAULT_ESTIMATE_FACTOR,
                        prompt_overhead: int = DEFAULT_PROMPT_OVERHEAD) -> tuple[PlannedBlock, ...]:
    if block.estimated_tokens + prompt_overhead <= capacity:
        return (block,)
    ids = sorted(block.source_ids,
                 key=lambda source_id: documents[source_id].order)
    if len(ids) == 1:
        raise SingleChapterSplitRequired(block.block_id)
    weights = [estimate_chapter_tokens(documents[source_id], estimator, factor=factor)
               for source_id in ids]
    total = sum(weights)
    target = total / 2
    running = 0
    split = 1
    for index, weight in enumerate(weights[:-1], start=1):
        running += weight
        if running >= target:
            split = index
            break
    halves = (ids[:split], ids[split:])
    result = []
    for half in halves:
        if not half:
            continue
        orders = [documents[source_id].order for source_id in half]
        source_ids = tuple(half)
        start, end = min(orders), max(orders) + 1
        tokens = sum(estimate_chapter_tokens(documents[source_id], estimator,
                                             factor=factor) for source_id in half)
        result.append(PlannedBlock(
            block_id=_planned_id(block.level, start, end, source_ids),
            level=block.level, start=start, end=end, source_ids=source_ids,
            estimated_tokens=tokens, parent_id=block.parent_id,
            source_kind=block.source_kind,
            oversized=tokens + prompt_overhead > capacity))
    return tuple(result)


def required_children_for_high_level(plan: ReferenceIndexPlan,
                                     block_id: str) -> tuple[PlannedBlock, ...]:
    """Return all low-level descendants that must be created before a rollup."""
    parent = plan.get(block_id)
    if parent.level <= 1:
        return (parent,)
    children = [block for block in plan.blocks
                if block.parent_id == block_id and block.level == parent.level - 1]
    if not children:
        return ()
    result = []
    for child in children:
        result.extend(required_children_for_high_level(plan, child.block_id))
    return tuple(result)
