"""Pure context planning over a hierarchical index snapshot.

The builder does not call models and does not know about prompts. It returns
ordered coverage segments plus token estimates so the caller can either render
messages or shrink the batch before spending any model budget.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Protocol, Sequence

from .artifacts import ArtifactKind, BlockStatus
from .snapshot import IndexSnapshot


class TokenEstimator(Protocol):
    def count(self, text: str) -> int:
        ...


@dataclass(frozen=True)
class ContextDocument:
    source_id: str
    order: int
    title: str
    source_text: str
    translation_text: str | None = None
    translation_artifact_id: str | None = None
    provisional: bool = False


@dataclass(frozen=True)
class CoverageSegment:
    representation: str
    level: int
    start: int
    end: int
    status: str
    text: str
    token_estimate: int
    artifact_id: str | None = None
    source_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContextPlan:
    target_id: str
    batch_ids: tuple[str, ...]
    segments: tuple[CoverageSegment, ...]
    total_tokens: int
    max_tokens: int
    warnings: tuple[str, ...] = ()

    @property
    def fits(self) -> bool:
        return self.total_tokens <= self.max_tokens

    @property
    def coverage(self) -> tuple[CoverageSegment, ...]:
        return self.segments

    def render_messages(self) -> list[dict[str, str]]:
        messages = []
        for segment in self.segments:
            if segment.representation == "summary":
                content = (f"【level {segment.level} 摘要 [{segment.start}:{segment.end}) "
                           f"{segment.status}】\n{segment.text}")
            elif segment.representation == "full":
                content = segment.text
            elif segment.representation == "batch_source":
                content = "【本批次前文日文（尚未翻译）】\n" + segment.text
            else:
                content = "【待翻译正文】\n" + segment.text
            messages.append({"role": "user", "content": content})
        return messages


@dataclass(frozen=True)
class BatchPlan:
    batch_ids: tuple[str, ...]
    plans: tuple[ContextPlan, ...]
    max_tokens: int
    fits: bool


@dataclass(frozen=True)
class BatchSelection:
    size: int
    batch_ids: tuple[str, ...]
    plans: tuple[ContextPlan, ...]
    fits: bool


def _value(status) -> str:
    return status.value if hasattr(status, "value") else str(status)


def _block_text(block) -> str:
    return str(block.content.get("summary") or block.content.get("text") or "")


def _block_status_allowed(block) -> bool:
    return _value(block.status) in {BlockStatus.ACCEPTED.value,
                                    BlockStatus.PROVISIONAL.value}


def _block_bounds(block, documents: Mapping[str, ContextDocument]):
    orders = [documents[source_id].order for source_id in block.source_ids
              if source_id in documents]
    if not orders:
        return None
    return min(orders), max(orders) + 1


def _block_rows(snapshot: IndexSnapshot, level: int,
                documents: Mapping[str, ContextDocument]):
    rows = []
    for block in snapshot.blocks(level):
        if not _block_status_allowed(block):
            continue
        bounds = _block_bounds(block, documents)
        if bounds is None:
            continue
        start, end = bounds
        rows.append((start, end, block))
    rows.sort(key=lambda item: (item[0], item[1]))
    return rows


def _accepted_by_source(snapshot: IndexSnapshot):
    result = {}
    for artifact in snapshot.accepted_chapters():
        for source_id in artifact.source_ids:
            result[source_id] = artifact
    return result


def _full_chapters(documents, batch_start, accepted, full_chapters):
    candidates = [doc for doc in documents.values()
                  if doc.order < batch_start and doc.source_id in accepted
                  and doc.translation_text][-full_chapters:]
    if not candidates:
        return [], batch_start, ()
    align = candidates[0].order
    return candidates, align, ()


def _align_full_start(snapshot, documents, align, batch_start, warnings):
    best = None
    for start, end, block in _block_rows(snapshot, 1, documents):
        if start <= align < end and end <= batch_start:
            if best is None or start < best:
                best = start
    if best is None or best > align:
        return align, warnings
    if best < align:
        warnings = warnings + ("full chapters aligned down to a level-1 block boundary",)
    return best, warnings


def _select_level(snapshot, documents, level, boundary, remaining_budget,
                  estimator, warnings):
    # A block that starts before the full-text boundary but ends inside it is
    # still useful: including it may overlap a few recent full chapters, but
    # omitting it would leave an uncovered gap between the summary and the
    # full-text window.
    rows = [row for row in _block_rows(snapshot, level, documents)
            if row[0] < boundary]
    if not rows:
        warnings = warnings + (f"no usable level-{level} blocks before {boundary}",)
        return [], boundary, warnings
    minimum = snapshot.levels.context_blocks_for(level)
    required = rows[-minimum:]
    selected = list(required)
    parent_id = required[0][2].parent_id if required else None
    if parent_id and snapshot.levels.enabled(level + 1):
        siblings = [row for row in rows if row[2].parent_id == parent_id]
        selected = siblings
    selected.sort(key=lambda item: (item[0], item[1]))
    required_ids = {row[2].artifact_id for row in selected}
    used = sum(estimator.count(_block_text(row[2])) for row in selected)
    if level == 1:
        # Level 1 keeps exactly the configured recent blocks (plus the
        # sibling completion above); deeper history belongs to level 2.
        new_boundary = min(row[0] for row in selected)
        return selected, new_boundary, warnings
    # Level 2 and above: fill the remaining budget with earlier parents so
    # the whole prefix stays covered when configured as "all".
    if used <= remaining_budget:
        for row in reversed([item for item in rows if item[2].artifact_id not in required_ids]):
            cost = estimator.count(_block_text(row[2]))
            if used + cost > remaining_budget:
                break
            selected.append(row)
            used += cost
            remaining_budget -= cost
    new_boundary = min(row[0] for row in selected)
    return selected, new_boundary, warnings


@dataclass(frozen=True)
class NearContext:
    """Per-target text that must stay adjacent to the request's tail.

    ``near_tokens`` feeds the history budget. Batch callers pass the largest
    value in the batch as ``selection_reserve_tokens`` so every request selects
    the same history blocks; otherwise the summary prefix changes with the
    target and no batch request can reuse another's cached prefix.
    """
    batch_start: int
    full_start: int
    full_docs: tuple[ContextDocument, ...]
    full_texts: tuple[str, ...]
    prefix_docs: tuple[ContextDocument, ...]
    prefix_text: str
    target_text: str
    near_tokens: int
    warnings: tuple[str, ...]


def near_context(snapshot: IndexSnapshot, documents: Mapping[str, ContextDocument],
                 target_id: str, batch_ids: Sequence[str], *,
                 estimator: TokenEstimator,
                 full_chapters: int = 5) -> NearContext:
    if target_id not in documents:
        raise ValueError("target is not present in documents")
    if not batch_ids or target_id not in batch_ids:
        raise ValueError("target must belong to the batch")
    batch_docs = sorted((documents[source_id] for source_id in batch_ids),
                        key=lambda doc: doc.order)
    batch_start = batch_docs[0].order
    target = documents[target_id]
    warnings: tuple[str, ...] = ()
    accepted = _accepted_by_source(snapshot)

    candidates, align, _ = _full_chapters(documents, batch_start, accepted,
                                          max(1, full_chapters))
    full_start, warnings = _align_full_start(snapshot, documents, align,
                                             batch_start, warnings)
    full_docs = tuple(doc for doc in sorted(documents.values(), key=lambda item: item.order)
                      if full_start <= doc.order < batch_start and
                      doc.source_id in accepted and doc.translation_text)

    def full_text(doc):
        label = "未审草稿" if doc.provisional else "已译章节"
        return f"【{label} {doc.title}】\n{doc.source_text}\n---\n{doc.translation_text}"

    near_texts = [full_text(doc) for doc in full_docs]
    prefix_docs = tuple(doc for doc in batch_docs if doc.order < target.order)
    prefix_text = "\n\n".join(f"【{doc.title}】\n{doc.source_text}" for doc in prefix_docs)
    target_text = f"【{target.title}】\n{target.source_text}"
    near_tokens = (sum(estimator.count(text) for text in near_texts) +
                   (estimator.count(prefix_text) if prefix_text else 0) +
                   estimator.count(target_text))
    return NearContext(batch_start=batch_start, full_start=full_start,
                       full_docs=full_docs, full_texts=tuple(near_texts),
                       prefix_docs=prefix_docs, prefix_text=prefix_text,
                       target_text=target_text, near_tokens=near_tokens,
                       warnings=warnings)


def batch_near_reserve(snapshot: IndexSnapshot,
                       documents: Mapping[str, ContextDocument],
                       batch_ids: Sequence[str], *,
                       estimator: TokenEstimator,
                       full_chapters: int = 5) -> int:
    """Worst-case near-context size across a batch, for a shared history budget."""
    return max(near_context(snapshot, documents, source_id, batch_ids,
                            estimator=estimator,
                            full_chapters=full_chapters).near_tokens
               for source_id in batch_ids)


def build_context_plan(snapshot: IndexSnapshot, documents: Mapping[str, ContextDocument],
                       target_id: str, batch_ids: Sequence[str], *,
                       estimator: TokenEstimator, max_tokens: int,
                       full_chapters: int = 5,
                       selection_reserve_tokens: int | None = None) -> ContextPlan:
    parts = near_context(snapshot, documents, target_id, batch_ids,
                         estimator=estimator, full_chapters=full_chapters)
    batch_start = parts.batch_start
    full_start = parts.full_start
    target = documents[target_id]
    warnings = parts.warnings
    full_docs = parts.full_docs
    near_texts = list(parts.full_texts)
    prefix_docs = parts.prefix_docs
    prefix_text = parts.prefix_text
    target_text = parts.target_text
    near_tokens = parts.near_tokens
    if selection_reserve_tokens is not None:
        near_tokens = max(near_tokens, selection_reserve_tokens)
    remaining = max(0, max_tokens - near_tokens)

    selected_by_level = {}
    boundary = full_start
    for level in range(1, snapshot.levels.max_level + 1):
        selected, boundary, warnings = _select_level(
            snapshot, documents, level, boundary, remaining, estimator, warnings)
        if not selected:
            # Lazy indexes may have no level-1 artifacts while a higher level
            # already covers the older range. Keep the boundary and try up.
            continue
        selected_by_level[level] = selected
        remaining = max(0, remaining - sum(estimator.count(_block_text(row[2]))
                                           for row in selected))

    segments = []
    for level in range(snapshot.levels.max_level, 0, -1):
        for start, end, block in selected_by_level.get(level, []):
            text = _block_text(block)
            segments.append(CoverageSegment(
                representation="summary", level=level, start=start, end=end,
                status=_value(block.status), text=text,
                token_estimate=estimator.count(text), artifact_id=block.artifact_id,
                source_ids=tuple(block.source_ids)))
    for doc, text in zip(full_docs, near_texts):
        segments.append(CoverageSegment(
            representation="full", level=0, start=doc.order, end=doc.order + 1,
            status="accepted", text=text, token_estimate=estimator.count(text),
            source_ids=(doc.source_id,)))
    if prefix_text:
        segments.append(CoverageSegment(
            representation="batch_source", level=0, start=batch_start,
            end=target.order, status="source", text=prefix_text,
            token_estimate=estimator.count(prefix_text),
            source_ids=tuple(doc.source_id for doc in prefix_docs)))
    segments.append(CoverageSegment(
        representation="target_source", level=0, start=target.order,
        end=target.order + 1, status="source", text=target_text,
        token_estimate=estimator.count(target_text),
        source_ids=(target.source_id,)))

    total = sum(segment.token_estimate for segment in segments)
    return ContextPlan(target_id=target_id, batch_ids=tuple(batch_ids),
                       segments=tuple(segments), total_tokens=total,
                       max_tokens=max_tokens, warnings=warnings)


def preflight_batch(snapshot: IndexSnapshot, documents: Mapping[str, ContextDocument],
                    batch_ids: Sequence[str], *, estimator: TokenEstimator,
                    max_tokens: int, full_chapters: int = 5) -> BatchPlan:
    plans = tuple(build_context_plan(snapshot, documents, source_id, batch_ids,
                                     estimator=estimator, max_tokens=max_tokens,
                                     full_chapters=full_chapters)
                  for source_id in batch_ids)
    return BatchPlan(batch_ids=tuple(batch_ids), plans=plans,
                     max_tokens=max(plan.total_tokens for plan in plans),
                     fits=all(plan.fits for plan in plans))


def choose_batch_size(snapshot: IndexSnapshot, documents: Mapping[str, ContextDocument],
                      ordered_ids: Sequence[str], *,
                      estimator: TokenEstimator, max_tokens: int,
                      requested_size: int, full_chapters: int = 5) -> BatchSelection:
    if requested_size < 1:
        raise ValueError("requested_size must be positive")
    limit = min(requested_size, len(ordered_ids))
    for size in range(limit, 0, -1):
        batch_ids = tuple(ordered_ids[:size])
        plan = preflight_batch(snapshot, documents, batch_ids, estimator=estimator,
                               max_tokens=max_tokens, full_chapters=full_chapters)
        if plan.fits:
            return BatchSelection(size=size, batch_ids=batch_ids,
                                  plans=plan.plans, fits=True)
    batch_ids = tuple(ordered_ids[:1])
    plan = preflight_batch(snapshot, documents, batch_ids, estimator=estimator,
                           max_tokens=max_tokens, full_chapters=full_chapters)
    return BatchSelection(size=1, batch_ids=batch_ids, plans=plan.plans, fits=plan.fits)
