"""Chapter role classification for the translation pipeline.

Roles are derived from acquisition metadata when available and from a small,
ordered set of title patterns otherwise.  The classifier is deliberately
separate from prompt-time heuristics: a chapter's role decides whether it is
translated, summarized and exported, so it must be inspectable and stable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import unicodedata
from pathlib import Path
from typing import Iterable, Mapping, Sequence

MAIN = "main"
EXTRA = "extra"
CHARACTER_SHEET = "character_sheet"
SETTING = "setting"
PREFACE = "preface"
AFTERWORD = "afterword"
UNKNOWN = "unknown"

ALL_ROLES = (MAIN, EXTRA, CHARACTER_SHEET, SETTING, PREFACE, AFTERWORD, UNKNOWN)
TRANSLATED_ROLES = (MAIN, EXTRA, CHARACTER_SHEET, SETTING, PREFACE, AFTERWORD)
SUMMARY_ROLES = (MAIN, EXTRA, PREFACE, AFTERWORD)

_EXTRA_MARKERS = ("番外", "外伝", "外傳")
_CHARACTER_MARKERS = ("登場人物", "人物一覧", "人物紹介", "人物表",
                      "キャラクター一覧", "キャラクター紹介")
_SETTING_MARKERS = ("設定資料", "設定紹介", "用語集", "用語一覧", "世界観",
                    "世界について")
_PREFACE_MARKERS = ("前書き", "序章", "プロローグ", "はじめに")
_AFTERWORD_MARKERS = ("後書き", "あとがき", "エピローグ", "終章", "おわりに")

# Ordered; the first match wins.  Patterns are applied to the title with any
# acquisition section prefix removed.  A marker-bearing pattern may occur after
# the prefix, so those use search rather than match.
_MAIN_PATTERNS: tuple[tuple[str, re.Pattern[str], bool], ...] = (
    ("ja_number_wa", re.compile(r"(?:^|[\s　])第?\s*(\d{1,4})\s*[話话]"), True),
    ("ja_number_chapter", re.compile(r"(?:^|[\s　])第\s*(\d{1,4})\s*[章節节]"), True),
    ("bare_number", re.compile(r"^\s*(\d{1,4})(?=$|[\s　.．:：、,，\-—–~～])"), False),
    ("episode", re.compile(r"^\s*(?:Episode|EP?)\s*[.\-:]?\s*(\d{1,4})\b",
                           re.IGNORECASE), False),
    ("letter_number", re.compile(r"^\s*[SYRKB]\s*(\d{1,4})(?=$|\s)"), False),
)

_CF = re.compile(r"[​-‏‪-‮⁠-⁤﻿]")


def normalize_title(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = _CF.sub("", text)
    return re.sub(r"[ \t　]+", " ", text).strip()


def _strip_section(title: str, section: str | None) -> str:
    if not section:
        return title
    section = normalize_title(section)
    if not section:
        return title
    for separator in (" ", "　", "：", ":", " - ", "—"):
        prefix = section + separator
        if title.startswith(prefix):
            return title[len(prefix):].strip()
    return title


@dataclass(frozen=True)
class ChapterRoleEntry:
    order: int
    title: str
    role: str
    subtype: str | None = None
    section: str | None = None
    role_source: str = "title_pattern"
    confidence: int = 2
    ordinal: int | None = None
    pattern_id: str | None = None
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "order": self.order,
            "title": self.title,
            "role": self.role,
            "subtype": self.subtype,
            "section": self.section,
            "role_source": self.role_source,
            "confidence": self.confidence,
            "ordinal": self.ordinal,
            "pattern_id": self.pattern_id,
            "warnings": list(self.warnings),
        }


@dataclass
class ChapterPlan:
    edition_id: str
    entries: dict[int, ChapterRoleEntry]
    main_ratio: float
    warnings: list[str] = field(default_factory=list)
    source: str = "derived"

    def role(self, order: int) -> str:
        entry = self.entries.get(order)
        return entry.role if entry else UNKNOWN

    def entry(self, order: int) -> ChapterRoleEntry | None:
        return self.entries.get(order)

    def is_main(self, order: int) -> bool:
        return self.role(order) == MAIN

    def is_translatable(self, order: int,
                        roles: Iterable[str] = TRANSLATED_ROLES) -> bool:
        return self.role(order) in set(roles)

    def translate_orders(self, start: int, end: int,
                         roles: Iterable[str] = TRANSLATED_ROLES,
                         exclude: Iterable[int] = ()) -> list[int]:
        allowed = set(roles)
        excluded = set(exclude)
        return [order for order in range(start, end)
                if order in self.entries
                and order not in excluded
                and self.entries[order].role in allowed]

    def summary_orders(self, start: int, end: int,
                       roles: Iterable[str] = SUMMARY_ROLES) -> list[int]:
        return self.translate_orders(start, end, roles)

    def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for entry in self.entries.values():
            result[entry.role] = result.get(entry.role, 0) + 1
        return result

    def to_dict(self) -> dict:
        return {
            "schema": "chapter-plan-v1",
            "edition_id": self.edition_id,
            "source": self.source,
            "main_ratio": self.main_ratio,
            "warnings": list(self.warnings),
            "counts": self.counts(),
            "entries": [self.entries[order].to_dict()
                        for order in sorted(self.entries)],
        }

    @classmethod
    def from_dict(cls, value: Mapping) -> "ChapterPlan":
        entries = {}
        for row in value.get("entries") or []:
            order = int(row["order"])
            entries[order] = ChapterRoleEntry(
                order=order,
                title=str(row.get("title", "")),
                role=str(row.get("role", UNKNOWN)),
                subtype=row.get("subtype"),
                section=row.get("section"),
                role_source=str(row.get("role_source", "manual")),
                confidence=int(row.get("confidence", 2)),
                ordinal=(int(row["ordinal"]) if row.get("ordinal") is not None
                         else None),
                pattern_id=row.get("pattern_id"),
                warnings=tuple(row.get("warnings") or ()),
            )
        return cls(
            edition_id=str(value.get("edition_id", "")),
            entries=entries,
            main_ratio=float(value.get("main_ratio", 0.0)),
            warnings=list(value.get("warnings") or []),
            source=str(value.get("source", "file")),
        )

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                          encoding="utf-8")
        return target


def _explicit_role(title: str, section: str | None) -> tuple[str, str | None, str] | None:
    """Return (role, subtype, source) for an explicit non-main marker."""
    for value, source in ((title, "title"), (section or "", "section")):
        if not value:
            continue
        if any(marker in value for marker in _EXTRA_MARKERS):
            return EXTRA, "side_story", source
        if any(marker in value for marker in _CHARACTER_MARKERS):
            return CHARACTER_SHEET, "character_sheet", source
        if any(marker in value for marker in _SETTING_MARKERS):
            return SETTING, "setting", source
        if any(marker in value for marker in _PREFACE_MARKERS):
            return PREFACE, "preface", source
        if any(marker in value for marker in _AFTERWORD_MARKERS):
            return AFTERWORD, "afterword", source
    return None


def _match_main(title: str, section: str | None) -> tuple[int, str] | None:
    stripped = _strip_section(title, section)
    for pattern_id, regex, search in _MAIN_PATTERNS:
        match = regex.search(stripped) if search else regex.match(stripped)
        if match:
            return int(match.group(1)), pattern_id
    return None


def classify_document(*, order: int, title: str,
                      section: str | None = None,
                      role_override: Mapping | None = None) -> ChapterRoleEntry:
    """Classify one chapter.

    Explicit acquisition/manual metadata wins; otherwise title markers, then
    the first matching main pattern.  Anything else is an extra, not main.
    """
    normalized = normalize_title(title)
    if role_override:
        role = str(role_override.get("role") or UNKNOWN)
        return ChapterRoleEntry(
            order=order, title=title, role=role,
            subtype=role_override.get("subtype"),
            section=section,
            role_source=str(role_override.get("role_source", "manual")),
            confidence=int(role_override.get("confidence", 1)),
            ordinal=role_override.get("ordinal"),
            pattern_id=role_override.get("pattern_id"),
            warnings=tuple(role_override.get("warnings") or ()),
        )

    explicit = _explicit_role(normalized, section)
    if explicit:
        role, subtype, source = explicit
        return ChapterRoleEntry(
            order=order, title=title, role=role, subtype=subtype,
            section=section, role_source=source, confidence=2,
            warnings=(),
        )

    matched = _match_main(normalized, section)
    if matched:
        ordinal, pattern_id = matched
        return ChapterRoleEntry(
            order=order, title=title, role=MAIN, section=section,
            role_source="title_pattern", confidence=2,
            ordinal=ordinal, pattern_id=pattern_id,
        )

    if not normalized:
        return ChapterRoleEntry(
            order=order, title=title, role=UNKNOWN, section=section,
            role_source="empty_title", confidence=0,
            warnings=("empty title",),
        )
    return ChapterRoleEntry(
        order=order, title=title, role=EXTRA, section=section,
        role_source="fallback", confidence=1,
    )


def _finish_plan(edition_id: str, entries: dict[int, ChapterRoleEntry],
                 source: str, *, main_ratio_floor: float = 0.90,
                 hard_floor: float = 0.75) -> ChapterPlan:
    counts = {}
    for entry in entries.values():
        counts[entry.role] = counts.get(entry.role, 0) + 1
    total = len(entries)
    main_count = counts.get(MAIN, 0)
    ratio = (main_count / total) if total else 0.0
    warnings: list[str] = []
    if total and ratio < hard_floor:
        warnings.append(
            f"main_ratio {ratio:.3f} < {hard_floor:.2f}; manual takeover required")
    elif total and ratio < main_ratio_floor:
        warnings.append(
            f"main_ratio {ratio:.3f} < {main_ratio_floor:.2f}; classification needs review")
    ordinals: dict[int, list[int]] = {}
    for entry in entries.values():
        if entry.ordinal is not None:
            ordinals.setdefault(entry.ordinal, []).append(entry.order)
    for ordinal, orders in sorted(ordinals.items()):
        if len(orders) > 1:
            warnings.append(f"duplicate main ordinal {ordinal}: orders {orders}")
    return ChapterPlan(edition_id=edition_id, entries=entries,
                       main_ratio=ratio, warnings=warnings, source=source)


def build_chapter_plan(edition_id: str, documents: Sequence[Mapping],
                       *, role_overrides: Mapping[int, Mapping] | None = None,
                       main_ratio_floor: float = 0.90,
                       hard_floor: float = 0.75) -> ChapterPlan:
    """Build a plan from archive documents or acquisition rows.

    Each document must expose ``edition_order`` and ``raw_title``; ``section``
    may be a top-level key or live in ``metadata``.
    """
    entries: dict[int, ChapterRoleEntry] = {}
    overrides = dict(role_overrides or {})
    for doc in documents:
        order = int(doc["edition_order"])
        title = str(doc.get("raw_title") or doc.get("title") or "")
        metadata = doc.get("metadata") or {}
        section = doc.get("section") or metadata.get("section")
        entries[order] = classify_document(
            order=order, title=title, section=section,
            role_override=overrides.get(order))
    return _finish_plan(edition_id, entries, "derived",
                        main_ratio_floor=main_ratio_floor,
                        hard_floor=hard_floor)


def build_chapter_plan_from_manifest(edition_id: str, manifest: Mapping,
                                     *, main_ratio_floor: float = 0.90,
                                     hard_floor: float = 0.75) -> ChapterPlan:
    """Build from a syosetu-fetch manifest.

    The manifest's ``source_ordinal`` is 1-based; the imported edition order is
    ``source_ordinal - 1``.  Callers should verify this against the edition.
    """
    entries: dict[int, ChapterRoleEntry] = {}
    for index, row in enumerate(manifest.get("chapters") or []):
        ordinal = int(row.get("source_ordinal") or index + 1)
        order = ordinal - 1
        title = str(row.get("title") or row.get("display_title") or "")
        section = row.get("section")
        entries[order] = classify_document(
            order=order, title=title, section=section)
    return _finish_plan(edition_id, entries, "manifest",
                        main_ratio_floor=main_ratio_floor,
                        hard_floor=hard_floor)


def load_chapter_plan(project, edition_id: str, *,
                      documents: Sequence[Mapping] | None = None,
                      manifest_path: str | Path | None = None,
                      main_ratio_floor: float = 0.90,
                      hard_floor: float = 0.75) -> ChapterPlan:
    """Load or derive a plan for an edition.

    Search order: explicit manifest, ``<project>/chapter_plan.json``, then
    archive documents.
    """
    if manifest_path:
        path = Path(manifest_path)
        if path.is_file():
            value = json.loads(path.read_text(encoding="utf-8"))
            if "chapters" in value:
                return build_chapter_plan_from_manifest(
                    edition_id, value, main_ratio_floor=main_ratio_floor,
                    hard_floor=hard_floor)
            return ChapterPlan.from_dict(value)
    project_path = getattr(project, "path", None)
    if project_path is not None:
        sidecar = Path(project_path) / "chapter_plan.json"
        if sidecar.is_file():
            value = json.loads(sidecar.read_text(encoding="utf-8"))
            plan = ChapterPlan.from_dict(value)
            if not plan.edition_id or plan.edition_id == edition_id:
                return plan
    if documents is None:
        documents = project._archive([edition_id]).documents(edition_id)
    return build_chapter_plan(edition_id, documents,
                              main_ratio_floor=main_ratio_floor,
                              hard_floor=hard_floor)
