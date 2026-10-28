"""Backfill planning for chapters inside the reference region.

Backfill gaps are translated with a stable book prefix plus a region-stable
before/after context.  Positions always come from source order; the model only
sees them, it never decides them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Iterable, Mapping


@dataclass(frozen=True)
class BackfillGap:
    order: int
    role: str
    title: str

    def to_dict(self) -> dict:
        return {"order": self.order, "role": self.role, "title": self.title}


@dataclass(frozen=True)
class BackfillRegion:
    region_id: int
    start: int
    end: int
    gaps: tuple[BackfillGap, ...]
    before_orders: tuple[int, ...] = ()
    after_orders: tuple[int, ...] = ()
    prefix_hash: str = ""

    def to_dict(self) -> dict:
        return {
            "region_id": self.region_id,
            "start": self.start,
            "end": self.end,
            "gaps": [gap.to_dict() for gap in self.gaps],
            "before_orders": list(self.before_orders),
            "after_orders": list(self.after_orders),
            "prefix_hash": self.prefix_hash,
        }


@dataclass
class BackfillPlan:
    regions: tuple[BackfillRegion, ...]
    report: dict = field(default_factory=dict)

    @property
    def gaps(self) -> tuple[BackfillGap, ...]:
        return tuple(gap for region in self.regions for gap in region.gaps)

    def to_dict(self) -> dict:
        return {"gaps": [gap.to_dict() for gap in self.gaps],
                "regions": [region.to_dict() for region in self.regions],
                "report": self.report}


def _context_orders(runner, region_start: int, region_end: int,
                    *, before_count: int, after_count: int) -> tuple[tuple[int, ...], tuple[int, ...]]:
    accepted = set(runner.store.accepted_orders())
    references = set(runner.reference_texts)
    available = [order for order in sorted(runner.source_by_order)
                 if order in accepted or order in references]
    before = [order for order in available if order < region_start][-before_count:]
    after = [order for order in available if order >= region_end][:after_count]
    return tuple(before), tuple(after)


def _prefix_hash(region_start: int, region_end: int,
                 before: Iterable[int], after: Iterable[int]) -> str:
    payload = json.dumps({
        "start": region_start, "end": region_end,
        "before": list(before), "after": list(after),
    }, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def plan_backfill(runner, *, start: int = 0, end: int | None = None,
                  region_span: int = 50,
                  before_count: int = 2,
                  after_count: int = 2,
                  roles: Iterable[str] | None = None) -> BackfillPlan:
    end = runner.main_end if end is None else end
    allowed = set(roles or runner.translate_roles)
    accepted = set(runner.store.accepted_orders())
    gaps = []
    for order in range(max(0, start), end):
        if order not in runner.source_by_order:
            continue
        if not runner.chapter_plan.is_translatable(order, allowed):
            continue
        if order in accepted or order in runner.reference_texts:
            continue
        gaps.append(BackfillGap(
            order=order, role=runner.chapter_plan.role(order),
            title=runner.source(order).get("raw_title", "")))
    regions = []
    current: list[BackfillGap] = []
    for gap in gaps:
        if current and gap.order - current[-1].order > region_span:
            regions.append(current)
            current = []
        current.append(gap)
    if current:
        regions.append(current)
    result = []
    for index, group in enumerate(regions, 1):
        region_start = group[0].order
        region_end = group[-1].order + 1
        before, after = _context_orders(
            runner, region_start, region_end,
            before_count=before_count, after_count=after_count)
        result.append(BackfillRegion(
            region_id=index, start=region_start, end=region_end,
            gaps=tuple(group), before_orders=before, after_orders=after,
            prefix_hash=_prefix_hash(region_start, region_end, before, after)))
    counts: dict[str, int] = {}
    for gap in gaps:
        counts[gap.role] = counts.get(gap.role, 0) + 1
    report = {
        "gap_count": len(gaps),
        "region_count": len(result),
        "roles": counts,
        "orders": [gap.order for gap in gaps],
        "regions": [{
            "region_id": region.region_id,
            "start": region.start,
            "end": region.end,
            "gaps": len(region.gaps),
            "before_orders": list(region.before_orders),
            "after_orders": list(region.after_orders),
        } for region in result],
    }
    return BackfillPlan(regions=tuple(result), report=report)
