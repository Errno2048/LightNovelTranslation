"""Sequential translate/review/commit pipeline for the hierarchical index workflow.

The index package intentionally keeps translation, review and commit explicit.
This module wires those steps into a resumable chapter pipeline without
reintroducing the old ``translation.sequence`` engine.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Iterable, Mapping, Sequence
import uuid
import xml.etree.ElementTree as ET
import zipfile

from ..providers import ProviderError
from ..review.prompts import (AUDIT_ADJUDICATION_FEWSHOT, AUDIT_ADJUDICATION,
                              AUDIT_REVIEW_FEWSHOT, AUDIT_TASK)
from ..review.protocol import FIELDS, parse_records
from ..schemas import GenerationRequest
from ..storage import digest
from .artifacts import Artifact, InputRef
from .assets import AssetIndex, load_asset_index, sniff_media_type
from .context_plan import ContextDocument
from .prompts import source_records_rules
from .snapshot import IndexSnapshot
from .workflow import TranslationWorkflow, GlossaryTerm


class PipelineError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code, self.details = code, details or {}


class ChapterBlocked(PipelineError):
    pass


@dataclass(frozen=True)
class DraftChapter:
    order: int
    source_id: str
    source_hash: str
    ja_title: str
    title_zh: str
    text: str
    paragraphs: tuple[str, ...]
    version: int
    review: Mapping
    assets: Mapping = field(default_factory=dict)


@dataclass(frozen=True)
class ReviewOutcome:
    findings: tuple[dict, ...]
    unresolved: tuple[str, ...]
    coverage_complete: bool
    zero_findings: bool
    raw: str
    errors: tuple[str, ...] = ()
    term_checks: tuple[dict, ...] = ()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_IMAGE_MARKER = re.compile(r"\[\[IMG:([A-Za-z0-9_.:-]+)\]\]")

REVIEW_TOOLS = [
    {"type": "function", "function": {
        "name": "read_chapter",
        "description": ("读取当前审校章之前的某一章中文内容（优先官方参考译文，其次已接受译文；"
                        "没有中文时返回日文原文）。只能读取 order 小于当前章的内容。"),
        "parameters": {"type": "object", "properties": {
            "order": {"type": "integer", "description": "edition_order（0 基）；当前章会给出"},
            "include_source": {"type": "boolean",
                               "description": "同时返回日文原文（占用更多上下文）"}},
            "required": ["order"]},
    }},
    {"type": "function", "function": {
        "name": "lookup_term",
        "description": ("查询专名的规范译名、别名以及之前章节中的日文/中文实际用法。"
                        "返回官方参考优先，其次为已接受译文。"),
        "parameters": {"type": "object", "properties": {
            "term": {"type": "string", "description": "日文专名或已知中文写法"},
            "limit": {"type": "integer", "description": "最多返回多少个历史命中，默认 5"}},
            "required": ["term"]},
    }},
]


def _strip_markers(value: str) -> tuple[str, list[tuple[str, int]]]:
    parts: list[str] = []
    positions: list[tuple[str, int]] = []
    cursor = 0
    offset = 0
    for match in _IMAGE_MARKER.finditer(value):
        chunk = value[cursor:match.start()]
        parts.append(chunk)
        offset += len(chunk)
        positions.append((match.group(1), offset))
        cursor = match.end()
    parts.append(value[cursor:])
    return "".join(parts), positions


def _normalize_confidence(value) -> int:
    if isinstance(value, bool):
        return 2
    if isinstance(value, int):
        return value if value in {1, 2, 3} else 2
    text = str(value or "").strip().lower()
    return {"1": 1, "fixed": 1, "high": 1, "2": 2, "contextual": 2,
            "medium": 2, "3": 3, "candidate": 3, "low": 3}.get(text, 2)


def _normalize_aliases(value) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        values = []
    return sorted({str(item).strip() for item in values if str(item).strip()})


def _build_term_checks(source_rows: Sequence[Mapping],
                       target_paragraphs: Sequence[str],
                       glossary: Sequence[GlossaryTerm],
                       limit: int = 50) -> list[dict]:
    """One deterministic check per term, covering every missing occurrence."""
    checks = []
    for term in glossary:
        if int(term.confidence) > 2 or not term.source or not term.target:
            continue
        forms = [term.target, *term.aliases]
        occurrences = []
        occurrence_count = 0
        for index, source_row in enumerate(source_rows):
            if index >= len(target_paragraphs):
                break
            source_text = str(source_row.get("text", ""))
            if term.source not in source_text:
                continue
            target_text = str(target_paragraphs[index])
            if any(form and form in target_text for form in forms):
                continue
            occurrence_count += 1
            if len(occurrences) < 8:
                occurrences.append({
                    "source_ref": f"S{index + 1:03d}",
                    "target_ref": f"T{index + 1:03d}",
                    "source_text": source_text[:300],
                    "target_text": target_text[:300],
                })
        if not occurrences:
            continue
        checks.append({
            "id": f"tc{len(checks) + 1:03d}",
            "source_ref": occurrences[0]["source_ref"],
            "target_ref": occurrences[0]["target_ref"],
            "term": term.source,
            "canonical": term.target,
            "aliases": list(term.aliases),
            "kind": term.kind,
            "confidence": int(term.confidence),
            "locked": bool(term.locked),
            "status": "fixed_missing" if term.confidence == 1 else "needs_context",
            "occurrence_count": occurrence_count,
            "occurrences": occurrences,
        })
        if len(checks) >= limit:
            break
    return checks


def _clean_ref(value: str) -> str:
    return value.split("|", 1)[0].strip() if isinstance(value, str) else ""


def _numbered_title(title: str) -> bool:
    return bool(re.match(r"^\s*\d{1,4}(?!\d)", title or ""))


def _is_extra_title(title: str) -> bool:
    value = (title or "").strip()
    return (value.startswith("※") or value.startswith("＊") or
            any(marker in value for marker in (
                "人物紹介", "設定紹介", "後書き", "あとがき", "作者コメント",
                "番外編", "番外", "外伝")))


class TranslationStore:
    """SQLite content store with immutable versions and atomic acceptance."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS chapters (
                edition_order INTEGER PRIMARY KEY,
                source_id TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                ja_title TEXT NOT NULL,
                version INTEGER NOT NULL,
                title_zh TEXT NOT NULL,
                text TEXT NOT NULL,
                status TEXT NOT NULL,
                review TEXT NOT NULL DEFAULT '{}',
                assets TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS versions (
                version_id INTEGER PRIMARY KEY AUTOINCREMENT,
                edition_order INTEGER NOT NULL,
                version INTEGER NOT NULL,
                source_hash TEXT NOT NULL,
                ja_title TEXT NOT NULL,
                title_zh TEXT NOT NULL,
                text TEXT NOT NULL,
                status TEXT NOT NULL,
                review TEXT NOT NULL DEFAULT '{}',
                assets TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                UNIQUE(edition_order, version)
            );
            CREATE TABLE IF NOT EXISTS summaries (
                scope TEXT NOT NULL,
                start_order INTEGER NOT NULL,
                end_order INTEGER NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(scope, start_order, end_order)
            );
            CREATE TABLE IF NOT EXISTS glossary (
                source TEXT PRIMARY KEY,
                target TEXT NOT NULL,
                kind TEXT NOT NULL,
                confidence INTEGER NOT NULL DEFAULT 2,
                aliases TEXT NOT NULL DEFAULT '[]',
                locked INTEGER NOT NULL DEFAULT 0,
                evidence_count INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                edition_order INTEGER,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        for table in ("chapters", "versions"):
            columns = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
            if "assets" not in columns:
                self.db.execute(
                    f"ALTER TABLE {table} ADD COLUMN assets TEXT NOT NULL DEFAULT '{{}}'")
        glossary_columns = {row[1] for row in self.db.execute("PRAGMA table_info(glossary)")}
        for name, definition in (("confidence", "INTEGER NOT NULL DEFAULT 2"),
                                 ("aliases", "TEXT NOT NULL DEFAULT '[]'"),
                                 ("locked", "INTEGER NOT NULL DEFAULT 0")):
            if name not in glossary_columns:
                self.db.execute(f"ALTER TABLE glossary ADD COLUMN {name} {definition}")
        self.db.commit()

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def event(self, kind: str, *, order: int | None = None, **payload):
        self.db.execute(
            "INSERT INTO events(edition_order, kind, payload, created_at) VALUES(?,?,?,?)",
            (order, kind, json.dumps(payload, ensure_ascii=False), _now()))
        self.db.commit()

    def chapter(self, order: int) -> dict | None:
        row = self.db.execute("SELECT * FROM chapters WHERE edition_order=?",
                              (order,)).fetchone()
        if not row:
            return None
        value = dict(row)
        try:
            value["assets"] = json.loads(value.get("assets") or "{}")
        except ValueError:
            value["assets"] = {}
        return value

    def model_totals(self) -> dict:
        """Persistent model-attempt totals used for optional workflow limits."""
        requests = prompt = completion = 0
        for (payload,) in self.db.execute(
                "SELECT payload FROM events WHERE kind='model'"):
            requests += 1
            usage = json.loads(payload).get("usage") or {}
            prompt += usage.get("prompt_tokens") or usage.get("input_tokens") or 0
            completion += usage.get("completion_tokens") or usage.get("output_tokens") or 0
        return {"requests": requests, "input_tokens": prompt,
                "output_tokens": completion}

    def put_draft(self, *, order: int, source_id: str, source_hash: str,
                  ja_title: str, title_zh: str, text: str,
                  status: str = "draft", review: Mapping | None = None,
                  assets: Mapping | None = None) -> int:
        current = self.chapter(order)
        version = (current["version"] + 1) if current else 1
        review_text = json.dumps(review or {}, ensure_ascii=False)
        if assets is None:
            assets = (current or {}).get("assets") or {}
        assets_text = json.dumps(assets, ensure_ascii=False)
        with self.db:
            self.db.execute(
                "INSERT INTO versions(edition_order, version, source_hash, ja_title,"
                " title_zh, text, status, review, assets, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (order, version, source_hash, ja_title, title_zh, text,
                 status, review_text, assets_text, _now()))
            self.db.execute(
                "INSERT INTO chapters(edition_order, source_id, source_hash, ja_title,"
                " version, title_zh, text, status, review, assets, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(edition_order) DO UPDATE SET"
                " source_id=excluded.source_id, source_hash=excluded.source_hash,"
                " ja_title=excluded.ja_title, version=excluded.version,"
                " title_zh=excluded.title_zh, text=excluded.text,"
                " status=excluded.status, review=excluded.review,"
                " assets=excluded.assets, updated_at=excluded.updated_at",
                (order, source_id, source_hash, ja_title, version, title_zh,
                 text, status, review_text, assets_text, _now()))
        return version

    def set_status(self, order: int, status: str, review: Mapping | None = None):
        with self.db:
            self.db.execute(
                "UPDATE chapters SET status=?, review=?, updated_at=?"
                " WHERE edition_order=?",
                (status, json.dumps(review or {}, ensure_ascii=False), _now(), order))

    def accepted_orders(self) -> list[int]:
        return [row["edition_order"] for row in self.db.execute(
            "SELECT edition_order FROM chapters WHERE status='accepted'"
            " ORDER BY edition_order")]

    def translations(self) -> dict[int, str]:
        return {row["edition_order"]: row["text"] for row in self.db.execute(
            "SELECT edition_order, text FROM chapters WHERE status='accepted'")}

    def put_summary(self, scope: str, start: int, end: int, text: str):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO summaries(scope,start_order,end_order,text,created_at)"
                " VALUES(?,?,?,?,?)", (scope, start, end, text, _now()))

    def summary(self, scope: str, start: int, end: int) -> str | None:
        row = self.db.execute(
            "SELECT text FROM summaries WHERE scope=? AND start_order=? AND end_order=?",
            (scope, start, end)).fetchone()
        return row["text"] if row else None

    def summaries(self, scope: str) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT scope,start_order,end_order,text FROM summaries"
            " WHERE scope=? ORDER BY start_order", (scope,))]

    def put_glossary(self, rows: Iterable[Mapping]):
        now = _now()
        with self.db:
            for row in rows:
                source = str(row.get("source", "")).strip()
                target = str(row.get("target", "")).strip()
                if len(source) < 2 or not target:
                    continue
                kind = str(row.get("kind") or "other")
                confidence = _normalize_confidence(row.get("confidence"))
                aliases = _normalize_aliases(row.get("aliases"))
                locked = bool(row.get("locked", False))
                existing = self.db.execute(
                    "SELECT target,kind,confidence,aliases,locked,evidence_count"
                    " FROM glossary WHERE source=?", (source,)).fetchone()
                if existing is None:
                    self.db.execute(
                        "INSERT INTO glossary(source,target,kind,confidence,aliases,"
                        "locked,evidence_count,updated_at) VALUES(?,?,?,?,?,?,1,?)",
                        (source, target, kind, 1 if locked else confidence,
                         json.dumps(aliases, ensure_ascii=False), 1 if locked else 0, now))
                    continue
                old_aliases = []
                try:
                    old_aliases = [str(value) for value in
                                   json.loads(existing["aliases"] or "[]")]
                except (TypeError, ValueError):
                    old_aliases = []
                merged_aliases = set(old_aliases) | set(aliases)
                if existing["locked"]:
                    canonical = existing["target"]
                    confidence = 1
                    locked = True
                elif locked:
                    canonical = target
                    confidence = 1
                elif existing["confidence"] <= confidence:
                    canonical = existing["target"]
                    confidence = existing["confidence"]
                else:
                    canonical = target
                merged_aliases.add(existing["target"])
                merged_aliases.add(target)
                merged_aliases.discard(canonical)
                new_kind = existing["kind"]
                if new_kind == "other" and kind != "other":
                    new_kind = kind
                self.db.execute(
                    "UPDATE glossary SET target=?,kind=?,confidence=?,aliases=?,"
                    "locked=?,evidence_count=?,updated_at=? WHERE source=?",
                    (canonical, new_kind, confidence,
                     json.dumps(sorted(merged_aliases), ensure_ascii=False),
                     1 if locked else 0, existing["evidence_count"] + 1, now, source))

    def confirm_glossary_term(self, source: str, *, target: str | None = None,
                              aliases: Iterable[str] | None = None,
                              confidence: int = 1,
                              locked: bool = True) -> dict:
        """Explicitly confirm a term; this is the only path that creates a locked canonical."""
        source = str(source or "").strip()
        existing = self.db.execute(
            "SELECT target,kind,confidence,aliases,locked FROM glossary WHERE source=?",
            (source,)).fetchone()
        if existing is None:
            raise KeyError(f"unknown glossary term: {source}")
        target = str(target or existing["target"]).strip()
        if not target:
            raise ValueError("confirmed target must not be empty")
        if aliases is None:
            try:
                aliases = json.loads(existing["aliases"] or "[]")
            except ValueError:
                aliases = []
        clean_aliases = sorted({str(value).strip() for value in aliases
                                if str(value).strip() and str(value).strip() != target})
        level = _normalize_confidence(confidence)
        if locked:
            level = 1
        with self.db:
            self.db.execute(
                "UPDATE glossary SET target=?,confidence=?,aliases=?,locked=?,"
                "updated_at=? WHERE source=?",
                (target, level, json.dumps(clean_aliases, ensure_ascii=False),
                 1 if locked else 0, _now(), source))
        value = dict(self.db.execute(
            "SELECT source,target,kind,confidence,aliases,locked,evidence_count"
            " FROM glossary WHERE source=?", (source,)).fetchone())
        value["aliases"] = json.loads(value.get("aliases") or "[]")
        value["locked"] = bool(value.get("locked"))
        return value

    def glossary(self, limit: int = 200) -> list[dict]:
        # First-seen order is append-only: new terms do not move existing ones,
        # so a growing glossary keeps a reusable prompt prefix.
        result = []
        for row in self.db.execute(
                "SELECT source,target,kind,confidence,aliases,locked,evidence_count"
                " FROM glossary ORDER BY rowid LIMIT ?", (limit,)):
            value = dict(row)
            try:
                value["aliases"] = json.loads(value.get("aliases") or "[]")
            except ValueError:
                value["aliases"] = []
            value["locked"] = bool(value.get("locked"))
            result.append(value)
        return result

    def stats(self) -> dict:
        return {
            "accepted": len(self.accepted_orders()),
            "chapters": self.db.execute("SELECT COUNT(*) FROM chapters").fetchone()[0],
            "events": self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        }


class TranslationRunner:
    """Translate, review, and commit chapters with resumable SQLite state."""

    def __init__(self, project, flow: TranslationWorkflow, store: TranslationStore, *,
                 continuation_start: int, main_end: int,
                 glossary: Iterable[GlossaryTerm] = (),
                 reference_texts: Mapping[int, str] | None = None,
                 history_chapters: int | None = None,
                 summary_every: int | None = None,
                 summary_lag: int | None = None,
                 max_revisions: int | None = None,
                 translation_tokens: int | None = None,
                 review_tokens: int | None = None,
                 adjudication_tokens: int | None = None,
                 summary_tokens: int | None = None,
                 glossary_tokens: int | None = None,
                 reference_chunk: int | None = None):
        self.project = project
        self.flow = flow
        self.store = store
        self.continuation_start = continuation_start
        self.main_end = main_end
        self.glossary = list(glossary)
        workflow = project.config.workflow
        self.history_chapters = (workflow.history_chapters
                                 if history_chapters is None else history_chapters)
        self.summary_every = (workflow.summary_every
                              if summary_every is None else summary_every)
        self.summary_lag = (workflow.summary_lag
                            if summary_lag is None else summary_lag)
        self.max_revisions = (workflow.max_revisions
                              if max_revisions is None else max_revisions)
        self.translation_tokens = (workflow.translation_tokens
                                   if translation_tokens is None else translation_tokens)
        self.review_tokens = (workflow.review_tokens
                              if review_tokens is None else review_tokens)
        self.adjudication_tokens = (workflow.adjudication_tokens
                                    if adjudication_tokens is None else adjudication_tokens)
        self.summary_tokens = (workflow.summary_tokens
                               if summary_tokens is None else summary_tokens)
        self.glossary_tokens = (workflow.glossary_tokens
                                if glossary_tokens is None else glossary_tokens)
        self.reference_chunk = (workflow.reference_chunk
                                if reference_chunk is None else reference_chunk)
        self.max_term_checks = getattr(workflow, "max_term_checks", 50)
        self.lookup_chapters = getattr(workflow, "lookup_chapters", 8)
        self.lookup_terms = getattr(workflow, "lookup_terms", 50)
        self.max_tool_rounds = getattr(workflow, "max_tool_rounds", 4)
        self.source_by_order = {doc["edition_order"]: doc
                                for doc in flow._source_docs}
        source_edition = getattr(flow, "source_edition", "")
        try:
            self.asset_index = (load_asset_index(project, source_edition)
                                if source_edition else AssetIndex.empty(""))
        except Exception:
            self.asset_index = AssetIndex.empty(source_edition)
        self.reference_texts = dict(reference_texts or {
            order: doc["text"]
            for order, doc in flow._reference_by_source_order.items()})
        summaries = store.summaries("target")
        self._last_summary_end = max(
            (row["end_order"] for row in summaries), default=continuation_start)

    # -- source and context -------------------------------------------------
    def source(self, order: int) -> dict:
        try:
            return self.source_by_order[order]
        except KeyError as exc:
            raise PipelineError("unknown_order", f"no source chapter at order {order}") from exc

    def source_rows(self, order: int) -> list[dict]:
        return list(self.source(order).get("rows") or [])

    def prompt_rows(self, order: int) -> list[dict]:
        rows = [dict(row) for row in self.source_rows(order)]
        inline = self.asset_index.chapter_assets(order).get("inline") or []
        by_paragraph: dict[int, list[dict]] = {}
        for item in inline:
            by_paragraph.setdefault(int(item.get("paragraph", 0)), []).append(item)
        for row in rows:
            placements = by_paragraph.get(int(row.get("ordinal", 0)))
            if not placements:
                continue
            text = row.get("text", "")
            for item in sorted(placements, key=lambda value: int(value.get("offset", 0)),
                               reverse=True):
                offset = max(0, min(int(item.get("offset", 0)), len(text)))
                text = text[:offset] + f"[[IMG:{item['asset_id']}]]" + text[offset:]
            row["text"] = text
        return rows

    def expected_markers(self, rows: Iterable[Mapping]) -> dict[str, list[str]]:
        return {f"S{index:03d}": [match.group(1) for match in
                                  _IMAGE_MARKER.finditer(str(row.get("text", "")))]
                for index, row in enumerate(rows, 1)}

    def _check_limits(self):
        limits = self.project.config.limits
        if not (limits.max_requests or limits.max_input_tokens or limits.max_output_tokens):
            return
        totals = self.store.model_totals()
        reasons = []
        if limits.max_requests and totals["requests"] >= limits.max_requests:
            reasons.append("max_requests")
        if limits.max_input_tokens and totals["input_tokens"] >= limits.max_input_tokens:
            reasons.append("max_input_tokens")
        if limits.max_output_tokens and totals["output_tokens"] >= limits.max_output_tokens:
            reasons.append("max_output_tokens")
        if reasons:
            raise PipelineError(
                "budget", "persistent model limits reached",
                details={"limits": {"max_requests": limits.max_requests,
                                    "max_input_tokens": limits.max_input_tokens,
                                    "max_output_tokens": limits.max_output_tokens},
                         "totals": totals, "reasons": reasons})

    def documents(self, end: int,
                  provisional: Mapping[int, str] | None = None) -> dict[int, ContextDocument]:
        translations = self.store.translations()
        provisional = dict(provisional or {})
        translations.update(provisional)
        documents = {}
        for order, doc in self.source_by_order.items():
            if order >= end:
                continue
            translation = translations.get(order)
            if translation is None:
                translation = self.reference_texts.get(order)
            documents[order] = ContextDocument(
                source_id=doc["entry_id"], order=order,
                title=doc["raw_title"], source_text=doc["text"],
                translation_text=translation,
                provisional=order in provisional)
        return documents

    def snapshot(self, order: int, documents: Mapping[int, ContextDocument],
                 provisional_orders: Iterable[int] = ()) -> IndexSnapshot:
        artifacts = {}
        reference_summary = self.store.summary("reference", 0, self.continuation_start)
        if reference_summary:
            ids = tuple(self.source(level).get("entry_id")
                        for level in range(0, self.continuation_start)
                        if level in self.source_by_order)
            artifacts["reference-summary"] = Artifact(
                artifact_id="reference-summary", kind="block", level=2,
                source_ids=ids, status="accepted",
                inputs=(InputRef("reference", "reference-summary"),),
                content={"summary": reference_summary},
                review_mode="rebuild_only",
                quality_labels=("rebuild_only", "reference"))
        for row in self.store.summaries("target"):
            start, end = row["start_order"], row["end_order"]
            ids = tuple(self.source(level).get("entry_id")
                        for level in range(start, end) if level in self.source_by_order)
            if not ids:
                continue
            artifact_id = f"target-summary-{start}-{end}"
            artifacts[artifact_id] = Artifact(
                artifact_id=artifact_id, kind="block", level=2,
                source_ids=ids, status="accepted",
                inputs=(InputRef("target", f"target-summary-{start}-{end}"),),
                content={"summary": row["text"]},
                review_mode="rebuild_only",
                quality_labels=("rebuild_only", "target_summary"))
        for level in sorted(self.reference_texts):
            if level >= self.continuation_start or level not in self.source_by_order:
                continue
            artifact_id = f"reference-chapter-{level}"
            artifacts[artifact_id] = Artifact(
                artifact_id=artifact_id, kind="chapter", level=0,
                source_ids=(self.source(level)["entry_id"],), status="accepted",
                inputs=(InputRef(self.source(level)["entry_id"], "reference", str(level)),),
                quality_labels=("reference",))
        for level in self.store.accepted_orders():
            if level not in self.source_by_order:
                continue
            artifact_id = f"target-chapter-{level}"
            artifacts[artifact_id] = Artifact(
                artifact_id=artifact_id, kind="chapter", level=0,
                source_ids=(self.source(level)["entry_id"],), status="accepted",
                inputs=(InputRef(self.source(level)["entry_id"], "target", str(level)),),
                quality_labels=("target_accepted",))
        for level in provisional_orders:
            if level not in self.source_by_order:
                continue
            artifact_id = f"provisional-chapter-{level}"
            artifacts[artifact_id] = Artifact(
                artifact_id=artifact_id, kind="chapter", level=0,
                source_ids=(self.source(level)["entry_id"],), status="accepted",
                inputs=(InputRef(self.source(level)["entry_id"], "provisional",
                                 str(level)),),
                quality_labels=("provisional_target",))
        return IndexSnapshot(snapshot_id=f"run-{order}", generation="hierarchical",
                             levels=self.flow.levels, artifacts=artifacts)

    def _target_style_hint(self) -> str:
        for order in sorted(self.reference_texts):
            text = str(self.reference_texts.get(order) or "").strip()
            if text:
                return text[:160]
        return ""

    def _glossary_cards(self, order: int | None = None) -> list[GlossaryTerm]:
        if self.glossary:
            return list(self.glossary)
        # Batch translation keeps a stable broad list. Single-chapter reviews
        # additionally filter to terms that occur in that chapter, plus fixed
        # terms, so newly confirmed entries are not hidden by the limit.
        terms = [GlossaryTerm(row["source"], row["target"], row["kind"],
                              int(row.get("confidence", 2)),
                              tuple(row.get("aliases") or ()),
                              bool(row.get("locked")))
                 for row in self.store.glossary(limit=1000)]
        if order is None:
            return terms
        text = self.source(order).get("text", "")
        return [term for term in terms
                if term.source in text or int(term.confidence) <= 1]

    def pending_glossary_confirmation(self, orders: Iterable[int]) -> list[dict]:
        orders = [int(order) for order in orders]
        target_text = "\n".join(self.source(order).get("text", "") for order in orders
                                if order in self.source_by_order)
        reference_text = "\n".join(
            self.source(order).get("text", "")
            for order in range(0, self.continuation_start)
            if order in self.source_by_order)
        accepted_text = "\n".join(
            row.get("text", "") for row in
            (self.store.chapter(order) for order in self.store.accepted_orders())
            if row and row.get("status") == "accepted")
        pending = []
        for row in self.store.glossary(limit=1000):
            if int(row.get("confidence", 3)) != 3:
                continue
            source = row["source"]
            if source not in target_text:
                continue
            pending.append({
                "source": source, "target": row["target"],
                "kind": row["kind"], "aliases": row.get("aliases") or [],
                "reference_hits": reference_text.count(source),
                "accepted_hits": accepted_text.count(source),
            })
        return pending

    def confirm_glossary(self, decisions: Mapping[str, Mapping]) -> list[dict]:
        confirmed = []
        for source, decision in decisions.items():
            if not isinstance(decision, Mapping):
                raise ValueError("each glossary decision must be an object")
            row = self.store.confirm_glossary_term(
                source,
                target=decision.get("target"),
                aliases=decision.get("aliases"),
                confidence=int(decision.get("confidence", 1)),
                locked=bool(decision.get("locked", True)))
            self.store.event("glossary_confirmed", source=source,
                             target=row["target"], aliases=row["aliases"],
                             confidence=row["confidence"], locked=row["locked"])
            confirmed.append(row)
        return confirmed

    def _history_pairs(self, order: int,
                       provisional: Mapping[int, str] | None = None) -> list[dict]:
        provisional = dict(provisional or {})
        accepted = [value for value in self.store.accepted_orders() if value < order]
        candidates = sorted(set(accepted) | set(provisional))
        pairs = []
        for level in candidates[-self.history_chapters:]:
            if level in provisional:
                pairs.append(self._pair(level, provisional[level],
                                        authority="provisional_target"))
            else:
                pairs.append(self._pair(level, self.store.chapter(level)["text"],
                                        authority="accepted_target"))
        if len(pairs) < self.history_chapters:
            reference = [value for value in sorted(self.reference_texts)
                         if value < self.continuation_start]
            for level in reference[-(self.history_chapters - len(pairs)):]:
                pairs.append(self._pair(level, self.reference_texts[level],
                                        authority="official_reference"))
        return pairs

    def _pair(self, order: int, translation: str, *, authority: str) -> dict:
        source_rows = self.source_rows(order)
        target_rows = [line for line in translation.split("\n\n") if line.strip()]
        return {
            "chapter": order,
            "authority": authority,
            "source": [{"ref": f"R{order:04d}.{index:03d}", "text": row["text"]}
                       for index, row in enumerate(source_rows, 1)],
            "translation": [{"ref": f"L{order:04d}.{index:03d}", "text": text}
                            for index, text in enumerate(target_rows, 1)],
        }

    # -- model calls --------------------------------------------------------
    def _generate(self, messages: list[dict], max_tokens: int, task: str,
                  *, attempts: int | None = None, log_event: bool = True,
                  usage_sink: list | None = None,
                  tools: list[dict] | None = None,
                  tool_choice: str | dict | None = None):
        if attempts is None:
            attempts = self.project.config.limits.max_attempts
        request = GenerationRequest(messages, max_tokens=max_tokens,
                                    temperature=0, task=task,
                                    tools=tools, tool_choice=tool_choice)
        last = None
        for attempt in range(attempts):
            started = time.monotonic()
            try:
                result = self.project.provider.generate(request)
                record = {"task": task, "success": True, "attempt": attempt + 1,
                          "usage": result.usage,
                          "elapsed": result.elapsed_seconds,
                          "request_id": result.request_id,
                          "max_tokens": request.max_tokens,
                          "tool_calls": len(result.tool_calls)}
                if log_event:
                    self.store.event("model", **record)
                if usage_sink is not None:
                    usage_sink.append(record)
                return result
            except ProviderError as exc:
                last = exc
                details = exc.details if isinstance(exc.details, dict) else {}
                record = {"task": task, "success": False, "attempt": attempt + 1,
                          "error": exc.kind, "elapsed": round(time.monotonic() - started, 3),
                          "request_id": details.get("request_id"),
                          "max_tokens": request.max_tokens,
                          "usage": details.get("usage") if isinstance(details.get("usage"), dict) else None}
                if log_event:
                    self.store.event("model", **record)
                if usage_sink is not None:
                    usage_sink.append(record)
                # Only retry failures whose remote completion state is known and
                # non-billable (or a completed truncated/empty response). Timeout,
                # network and gateway 5xx/524 states are ambiguous and are not
                # silently resent.
                if exc.kind == "truncated" and attempt + 1 < attempts:
                    capacity = self.project.config.capacity.max_output_tokens
                    if request.max_tokens < capacity:
                        request = GenerationRequest(
                            messages,
                            max_tokens=min(capacity, request.max_tokens * 2),
                            temperature=0, task=task,
                            tools=tools, tool_choice=tool_choice)
                        continue
                if exc.kind in {"empty", "finish_reason"} and attempt + 1 < attempts:
                    time.sleep(1 + attempt)
                    continue
                if exc.kind == "rate_limit" and attempt + 1 < attempts:
                    time.sleep(min(2 ** attempt, 20))
                    continue
                raise
        raise last

    # -- translation --------------------------------------------------------
    def _translate_draft(self, order: int, *, provisional: Mapping[int, str] | None = None,
                         documents: Mapping[int, ContextDocument] | None = None,
                         snapshot: IndexSnapshot | None = None,
                         batch_orders: Iterable[int] | None = None,
                         glossary: Iterable[GlossaryTerm] | None = None,
                         unresolved_prior: Iterable[str] | None = None,
                         log_event: bool = False,
                         usage_sink: list | None = None) -> DraftChapter:
        source = self.source(order)
        rows = self.source_rows(order)
        prompt_rows = self.prompt_rows(order)
        expected_markers = self.expected_markers(prompt_rows)
        docs = (documents if documents is not None
                else self.documents(order + 1, provisional=provisional))
        provisional_orders = tuple((provisional or {}).keys())
        snapshot = (snapshot if snapshot is not None
                    else self.snapshot(order, docs,
                                       provisional_orders=provisional_orders))
        batch = tuple(batch_orders or (order,))
        if order not in batch:
            batch = (order,) + batch
        terms = (list(glossary) if glossary is not None
                 else self._glossary_cards())
        unresolved = (list(unresolved_prior) if unresolved_prior is not None
                      else self._unresolved())
        aliases = [f"S{index:03d}" for index in range(1, len(rows) + 1)]
        messages, report, plan = self.flow.assemble(
            snapshot=snapshot, documents=docs, target_order=order,
            batch_orders=batch, glossary=terms,
            unresolved=unresolved, threads=None,
            output_tokens=self.translation_tokens,
            output_profile="source-records", source_rows=prompt_rows,
            provisional_orders=provisional_orders)
        if not report["fits"]:
            raise PipelineError("context_budget", "translation request exceeds capacity",
                                details={"order": order, "report": report})
        parsed = None
        raw = ""
        for attempt in range(3):
            result = self._generate(messages, self.translation_tokens,
                                    f"translate_{order}",
                                    log_event=log_event, usage_sink=usage_sink)
            raw = result.text
            parsed = self.parse_translation(raw, aliases,
                                            expected_markers=expected_markers)
            if not parsed["errors"]:
                break
            messages = messages[:-1] + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content":
                 "上次输出未通过源段布局校验。依据同一原文重新输出本章全部标题和"
                 "TRANSLATION记录，不能只补一段。错误：" + "; ".join(parsed["errors"]) +
                 "\n" + source_records_rules(aliases)}]
        if parsed is None or parsed["errors"]:
            raise PipelineError("translation_protocol",
                                "translation output is incomplete",
                                details={"order": order, "errors": parsed["errors"],
                                         "text": raw[-4000:]})
        source_assets = self.asset_index.chapter_assets(order)
        assets = {
            "source_edition": getattr(self.flow, "source_edition", ""),
            "inline": parsed["inline_assets"],
            "block": source_assets.get("block", []),
        }
        asset_ids = {item["asset_id"] for item in assets["inline"]}
        asset_ids.update(item["asset_id"] for item in assets["block"])
        assets["asset_ids"] = sorted(asset_ids)
        return DraftChapter(order=order, source_id=source["entry_id"],
                            source_hash=source["text_hash"],
                            ja_title=source["raw_title"],
                            title_zh=parsed["title"], text=parsed["text"],
                            paragraphs=tuple(parsed["paragraphs"]),
                            version=0, review={"protocol": parsed["profile"]},
                            assets=assets)

    def translate_order(self, order: int) -> DraftChapter:
        draft = self._translate_draft(order, log_event=True)
        version = self.store.put_draft(
            order=order, source_id=draft.source_id,
            source_hash=draft.source_hash, ja_title=draft.ja_title,
            title_zh=draft.title_zh, text=draft.text,
            status="draft", review=draft.review, assets=draft.assets)
        self.store.event("translate", order=order, version=version,
                         usage=None, paragraphs=len(draft.paragraphs))
        return replace(draft, version=version)

    def parse_translation(self, text: str, aliases: Sequence[str], *,
                          expected_markers: Mapping[str, Sequence[str]] | None = None) -> dict:
        parsed = parse_records(
            text, {"TITLE", "TRANSLATION"},
            field_schema={"TITLE": {"text"}, "TRANSLATION": {"text"}},
            syntax_profile="tolerant-records",
            implicit_fields={"TITLE": "text", "TRANSLATION": "text"},
            allow_intermediate_end=False)
        errors = list(parsed["errors"])
        titles = [row for row in parsed["records"] if row["record"] == "TITLE"]
        translations = [row for row in parsed["records"] if row["record"] == "TRANSLATION"]
        if len(titles) != 1:
            errors.append(f"expected_one_title:{len(titles)}")
        title = titles[0]["fields"].get("text", "").strip() if titles else ""
        if not title:
            errors.append("empty_title")
        actual = [row["id"] for row in translations]
        if actual != list(aliases):
            missing = [alias for alias in aliases if alias not in actual]
            unknown = [alias for alias in actual if alias not in aliases]
            if missing:
                errors.append("missing_source_ids:" + ",".join(missing))
            if unknown:
                errors.append("unknown_source_ids:" + ",".join(unknown))
            if not missing and not unknown:
                errors.append("source_ids_must_be_an_ordered_partition")
        paragraphs = []
        inline_assets = []
        expected_map = expected_markers or {}
        for index, row in enumerate(translations):
            body = row["fields"].get("text", "").strip()
            if not body:
                errors.append("empty_translation:" + row["id"])
            normalized = " ".join(line.strip() for line in body.splitlines()
                                  if line.strip())
            markers = [match.group(1) for match in _IMAGE_MARKER.finditer(normalized)]
            expected = list(expected_map.get(row["id"], []))
            if expected_markers is not None and markers != expected:
                errors.append(
                    "image_markers_mismatch:" + row["id"] +
                    ":expected=" + ",".join(expected) +
                    ":got=" + ",".join(markers))
            clean, positions = _strip_markers(normalized)
            paragraphs.append(clean)
            for asset_id, offset in positions:
                inline_assets.append({"paragraph": index, "offset": offset,
                                      "asset_id": asset_id})
        return {"profile": "source-records", "errors": errors,
                "title": title, "paragraphs": paragraphs,
                "text": "\n\n".join(paragraphs),
                "inline_assets": inline_assets,
                "semantic_status": "not_verified"}

    # -- review -------------------------------------------------------------
    def _unresolved(self) -> list[str]:
        values = []
        for order in self.store.accepted_orders()[-3:]:
            row = self.store.chapter(order)
            for item in (json.loads(row["review"] or "{}").get("unresolved") or []):
                if item not in values:
                    values.append(item)
        return values[-8:]

    def _budget_view(self, budget: dict) -> dict:
        used_chapters = sorted(budget["used_chapters"])
        used_terms = sorted(budget["used_terms"])
        return {
            "max_chapters": budget["max_chapters"],
            "max_terms": budget["max_terms"],
            "used_chapters": used_chapters,
            "used_terms": used_terms,
            "remaining_chapters": budget["max_chapters"] - len(used_chapters),
            "remaining_terms": budget["max_terms"] - len(used_terms),
        }

    def _prior_target_text(self, order: int,
                           accepted_texts: Mapping[int, str] | None = None
                           ) -> tuple[str | None, str]:
        if order < self.continuation_start:
            value = self.reference_texts.get(order)
            return value, "zh-reference" if value else "ja-source"
        if accepted_texts is not None:
            value = accepted_texts.get(order)
            return (value, "zh") if value is not None else (None, "ja-source")
        row = self.store.chapter(order)
        if row and row.get("status") == "accepted":
            return row.get("text"), "zh"
        return None, "ja-source"

    def _read_prior_chapter(self, order: int, current_order: int, budget: dict,
                            include_source: bool = False,
                            accepted_texts: Mapping[int, str] | None = None) -> dict:
        if type(order) is not int or order < 0 or order >= current_order:
            raise ValueError("only prior edition_order values can be read")
        if order not in self.source_by_order:
            raise ValueError("unknown edition_order")
        if order not in budget["used_chapters"]:
            if len(budget["used_chapters"]) >= budget["max_chapters"]:
                raise ValueError("chapter lookup budget exhausted")
            budget["used_chapters"].add(order)
        source = self.source(order)
        text, language = self._prior_target_text(order, accepted_texts)
        if text is None:
            text = source.get("text", "")
        limit = 12000
        truncated = len(text) > limit
        result = {"order": order, "title": source.get("raw_title", ""),
                  "language": language, "text": text[:limit],
                  "truncated": truncated, "budget": self._budget_view(budget)}
        if include_source and language != "ja-source":
            source_text = source.get("text", "")
            result["source_text"] = source_text[:6000]
            result["source_truncated"] = len(source_text) > 6000
        return result

    def _lookup_term(self, term: str, current_order: int, budget: dict,
                     limit: int = 5,
                     glossary_rows: Iterable[Mapping] | None = None,
                     accepted_texts: Mapping[int, str] | None = None) -> dict:
        term = str(term or "").strip()
        if not term:
            raise ValueError("term must not be empty")
        if term not in budget["used_terms"]:
            if len(budget["used_terms"]) >= budget["max_terms"]:
                raise ValueError("term lookup budget exhausted")
            budget["used_terms"].add(term)
        limit = max(1, min(int(limit or 5), 10))
        rows = (glossary_rows if glossary_rows is not None
                else self.store.glossary(limit=1000))
        glossary = []
        for row in rows:
            aliases = [str(value) for value in (row.get("aliases") or [])]
            if term not in {row["source"], row["target"], *aliases}:
                continue
            glossary.append({"source": row["source"], "target": row["target"],
                             "kind": row["kind"], "confidence": row.get("confidence", 2),
                             "aliases": aliases, "locked": bool(row.get("locked"))})
        reference_orders = list(range(0, min(self.continuation_start, current_order)))
        if accepted_texts is not None:
            accepted_orders = sorted(
                order for order in accepted_texts
                if self.continuation_start <= order < current_order)
        else:
            accepted_orders = [order for order in self.store.accepted_orders()
                               if self.continuation_start <= order < current_order]
        hits = []
        for group in (reference_orders, list(reversed(accepted_orders))):
            for order in group:
                if len(hits) >= limit:
                    break
                source_text = str(self.source(order).get("text", ""))
                target_text, language = self._prior_target_text(order, accepted_texts)
                if term not in source_text and (not target_text or term not in target_text):
                    continue
                source_paragraph = next(
                    (paragraph for paragraph in source_text.split("\n\n")
                     if term in paragraph), "")
                target_paragraph = ""
                if target_text:
                    target_paragraph = next(
                        (paragraph for paragraph in target_text.split("\n\n")
                         if term in paragraph), "")
                if not target_paragraph and target_text:
                    target_paragraph = target_text[:1000]
                hits.append({
                    "order": order,
                    "title": self.source(order).get("raw_title", ""),
                    "language": language,
                    "source": source_paragraph[:500],
                    "target": target_paragraph[:1000],
                })
            if len(hits) >= limit:
                break
        return {"term": term, "glossary": glossary, "hits": hits,
                "budget": self._budget_view(budget)}

    def _execute_review_tool(self, name: str, arguments: str, current_order: int,
                             budget: dict,
                             tool_context: Mapping | None = None) -> dict:
        safe_args = {}
        accepted_texts = (tool_context.get("accepted_texts")
                          if tool_context is not None else None)
        glossary_rows = (tool_context.get("glossary")
                         if tool_context is not None else None)
        try:
            args = json.loads(arguments or "{}")
            if not isinstance(args, dict):
                raise ValueError("tool arguments must be an object")
            safe_args = {key: args.get(key) for key in
                         ("order", "term", "limit", "include_source")
                         if key in args}
            if name == "read_chapter":
                result = self._read_prior_chapter(
                    args.get("order"), current_order, budget,
                    include_source=bool(args.get("include_source", False)),
                    accepted_texts=accepted_texts)
            elif name == "lookup_term":
                result = self._lookup_term(args.get("term", ""), current_order,
                                           budget, limit=args.get("limit", 5),
                                           glossary_rows=glossary_rows,
                                           accepted_texts=accepted_texts)
            else:
                result = {"error": "unknown tool", "name": name,
                          "budget": self._budget_view(budget)}
        except (ValueError, TypeError, KeyError) as exc:
            result = {"error": str(exc), "budget": self._budget_view(budget)}
        record = {
            "order": current_order, "tool": name, "arguments": safe_args,
            "budget": self._budget_view(budget), "error": result.get("error"),
            "hit_count": len(result.get("hits") or ()) if name == "lookup_term" else None}
        if (tool_context is not None and
                tool_context.get("buffer_events", True)):
            tool_context.setdefault("events", []).append(record)
        else:
            self.store.event("review_tool", **record)
        return result

    def _review_draft(self, draft: DraftChapter, *, history=None, glossary=None,
                      unresolved_prior=None, log_event: bool = False,
                      usage_sink: list | None = None,
                      tool_context: dict | None = None) -> ReviewOutcome:
        rows = self.source_rows(draft.order)
        source = [{"ref": f"S{index:03d}", "text": row["text"]}
                  for index, row in enumerate(rows, 1)]
        target = [{"ref": f"T{index:03d}", "text": text}
                  for index, text in enumerate(draft.paragraphs, 1)]
        source_by_ref = {row["ref"]: row["text"] for row in source}
        target_by_ref = {row["ref"]: row["text"] for row in target}
        history = list(history) if history is not None else self._history_pairs(draft.order)
        glossary = list(glossary) if glossary is not None else \
            self._glossary_cards(draft.order)
        unresolved = (list(unresolved_prior) if unresolved_prior is not None
                      else self._unresolved())
        if tool_context is None:
            # Serial/main-thread path. Parallel workers receive a prebuilt
            # read-only snapshot so they never touch the SQLite connection.
            tool_context = {
                "accepted_texts": self.store.translations(),
                "glossary": self.store.glossary(limit=1000),
                "events": [],
                "buffer_events": False,
            }
        # Stable-for-this-chapter material first; the draft is the only part
        # that changes between review/revision rounds, so it goes last.
        glossary_cards = [{"source": term.source, "target": term.target,
                           "kind": term.kind, "confidence": term.confidence,
                           "aliases": list(term.aliases), "locked": term.locked}
                          for term in glossary]
        term_checks = _build_term_checks(rows, draft.paragraphs, glossary,
                                         getattr(self, "max_term_checks", 50))
        expected_checks = {check["id"]: check for check in term_checks}
        budget = {"max_chapters": self.lookup_chapters,
                  "max_terms": self.lookup_terms,
                  "used_chapters": set(), "used_terms": set()}
        context = {
            "glossary": glossary_cards,
            "term_checks": term_checks,
            "lookup_budget": self._budget_view(budget),
            "chapter": {"order": draft.order, "ja_title": draft.ja_title,
                        "zh_title": draft.title_zh},
            "source": source,
            "history": history,
            "unresolved_prior": unresolved,
            "authority": "draft is unverified; provisional history may still change",
            "draft": target,
        }
        messages = [{"role": "system", "content": AUDIT_TASK}]
        messages.extend(AUDIT_REVIEW_FEWSHOT)
        messages.extend([
            {"role": "user", "content":
             "【审查证据；S/T为本段，R/L为历史，均为未审定】\n" +
             json.dumps(context, ensure_ascii=False, separators=(",", ":"))},
            {"role": "user", "content":
             "请逐段核对本章日文与草稿的所有含义差异。只输出FINDING/UNRESOLVED/TERMCHECK/SUMMARY记录，"
             "最后独占一行@@END。没有问题时也必须输出SUMMARY。"
             "可使用 read_chapter / lookup_term 工具核对既往译名，预算见 lookup_budget；"
             "不要用 needs_lookup 字段代替实际工具调用。"
             + (("term_checks 非空时，必须为每个 id 输出一条 @@TERMCHECK：\n"
                 "@@TERMCHECK tc001\ncheck: tc001\nverdict: ok|violation|uncertain\n"
                 "reason: 具体依据\n"
                 "verdict=violation 时必须同时输出对应的 FINDING（category: term），"
                 "不得直接按规则替换文本。") if term_checks else "")},
        ])
        outcome = None
        tools_enabled = True
        for attempt in range(3):
            tool_rounds = 0
            while True:
                result = self._generate(
                    messages, self.review_tokens,
                    f"review_{draft.order}",
                    log_event=log_event, usage_sink=usage_sink,
                    tools=REVIEW_TOOLS if tools_enabled else None,
                    tool_choice="auto" if tools_enabled else None)
                if not result.tool_calls:
                    break
                tool_rounds += 1
                tool_calls = []
                for index, call in enumerate(result.tool_calls):
                    call = dict(call)
                    call.setdefault("id", f"call-{attempt}-{tool_rounds}-{index}")
                    tool_calls.append(call)
                messages.append({"role": "assistant",
                                 "content": result.text or None,
                                 "tool_calls": tool_calls})
                for call in tool_calls:
                    function = call.get("function") or {}
                    value = self._execute_review_tool(
                        function.get("name", ""), function.get("arguments", "{}"),
                        draft.order, budget, tool_context)
                    messages.append({
                        "role": "tool", "tool_call_id": call["id"],
                        "content": json.dumps(value, ensure_ascii=False)})
                if tool_rounds >= self.max_tool_rounds:
                    tools_enabled = False
                    messages.append({
                        "role": "user",
                        "content": "工具调用轮次已用尽。请立即基于已有证据输出最终审校记录，"
                                   "不要再调用工具。"})
                    result = self._generate(messages, self.review_tokens,
                                            f"review_{draft.order}",
                                            log_event=log_event,
                                            usage_sink=usage_sink)
                    break
            outcome = self.parse_review(result.text, source_by_ref, target_by_ref,
                                        expected_checks=expected_checks)
            if not outcome.errors:
                break
            messages.extend([
                {"role": "assistant", "content": result.text},
                {"role": "user", "content":
                 "上次审校输出未通过协议校验。错误：" + "; ".join(outcome.errors) +
                 "\n请重新输出完整审校记录；只能引用已提供的S/T/R/L标签，"
                 "最后逐项输出 TERMCHECK，最后独占一行@@END。"},
            ])
        if outcome is None or outcome.errors:
            raise ChapterBlocked("review_protocol", "review output is invalid",
                                 details={"order": draft.order,
                                          "errors": outcome.errors if outcome else []})
        return outcome

    def review_order(self, draft: DraftChapter) -> ReviewOutcome:
        return self._review_draft(draft, log_event=True)

    def parse_review(self, text: str, source_by_ref: Mapping[str, str],
                     target_by_ref: Mapping[str, str], *,
                     expected_checks: Mapping[str, Mapping] | None = None) -> ReviewOutcome:
        parsed = parse_records(
            text, {"FINDING", "UNRESOLVED", "TERMCHECK", "SUMMARY"},
            field_schema={**FIELDS, "FINDING": FIELDS["FINDING"] | {"basis"}},
            syntax_profile="tolerant-records", allow_inline_end=True,
            allow_preamble=True)
        errors = list(parsed["errors"])
        summaries = [row for row in parsed["records"] if row["record"] == "SUMMARY"]
        if len(summaries) != 1:
            errors.append(f"expected_one_summary:{len(summaries)}")
        summary = summaries[0]["fields"] if summaries else {}
        if summary and summary.get("reviewed_all") != "yes":
            errors.append("reviewed_all_not_yes")
        coverage = summary.get("semantic_coverage_complete")
        if coverage not in {"yes", "no"}:
            errors.append("invalid_semantic_coverage")
        zero = summary.get("zero_findings")
        if zero not in {"yes", "no"}:
            errors.append("invalid_zero_findings")
        findings = []
        for row in parsed["records"]:
            if row["record"] != "FINDING":
                continue
            fields = row["fields"]
            source_ref = _clean_ref(fields.get("source_ref", ""))
            target_ref = _clean_ref(fields.get("target_ref", ""))
            if source_ref not in source_by_ref:
                errors.append("unknown_source_ref:" + (source_ref or "empty"))
            if target_ref not in target_by_ref:
                errors.append("unknown_target_ref:" + (target_ref or "empty"))
            for key in ("category", "severity", "claim", "suggestion", "confidence"):
                if not fields.get(key):
                    errors.append(f"missing_{key}:{row['id']}")
            if fields.get("category") not in {
                    "omission", "addition", "negation", "role", "number",
                    "term", "tense", "speaker", "style", "other"}:
                errors.append(f"invalid_category:{row['id']}")
            if fields.get("severity") not in {
                    "critical", "major", "minor", "suggestion"}:
                errors.append(f"invalid_severity:{row['id']}")
            if fields.get("confidence") not in {"high", "medium", "low"}:
                errors.append(f"invalid_confidence:{row['id']}")
            findings.append({
                "id": row["id"], **fields,
                "source_ref": source_ref, "target_ref": target_ref,
                "source_text": source_by_ref.get(source_ref, ""),
                "target_text": target_by_ref.get(target_ref, ""),
            })
        if zero == "yes" and findings:
            errors.append("zero_findings_mismatch")
        if zero == "no" and not findings:
            errors.append("zero_findings_mismatch")
        unresolved = [row["fields"].get("text", "").strip()
                      for row in parsed["records"]
                      if row["record"] == "UNRESOLVED" and
                      row["fields"].get("text", "").strip()]
        expected = dict(expected_checks or {})
        seen_checks = set()
        term_checks = []
        for row in parsed["records"]:
            if row["record"] != "TERMCHECK":
                continue
            fields = row["fields"]
            check_id = str(fields.get("check") or row["id"]).strip()
            if check_id not in expected:
                errors.append("unknown_termcheck:" + (check_id or "empty"))
                continue
            if check_id in seen_checks:
                errors.append("duplicate_termcheck:" + check_id)
                continue
            seen_checks.add(check_id)
            verdict = fields.get("verdict")
            if verdict not in {"ok", "violation", "uncertain"}:
                errors.append("invalid_termcheck_verdict:" + check_id)
            if not str(fields.get("reason", "")).strip():
                errors.append("missing_termcheck_reason:" + check_id)
            expected_row = expected[check_id]
            occurrences = expected_row.get("occurrences") or [{
                "source_ref": expected_row.get("source_ref", ""),
                "target_ref": expected_row.get("target_ref", ""),
            }]
            allowed_refs = {(row.get("source_ref"), row.get("target_ref"))
                            for row in occurrences}
            source_ref = _clean_ref(fields.get("source_ref", "")) or \
                expected_row.get("source_ref", "")
            target_ref = _clean_ref(fields.get("target_ref", "")) or \
                expected_row.get("target_ref", "")
            if (source_ref, target_ref) not in allowed_refs:
                errors.append("termcheck_ref_mismatch:" + check_id)
            if verdict == "violation" and not any(
                    finding.get("category") == "term" and
                    (finding.get("source_ref"), finding.get("target_ref")) in allowed_refs
                    for finding in findings):
                errors.append("termcheck_violation_requires_finding:" + check_id)
            term_checks.append({
                "id": check_id, "verdict": verdict,
                "reason": fields.get("reason", ""),
                "source_ref": source_ref, "target_ref": target_ref,
                "term": expected_row.get("term", ""),
                "canonical": expected_row.get("canonical", ""),
                "aliases": list(expected_row.get("aliases") or []),
                "confidence": expected_row.get("confidence"),
                "locked": bool(expected_row.get("locked")),
                "status": expected_row.get("status", ""),
                "occurrence_count": expected_row.get("occurrence_count", 0),
                "occurrences": list(expected_row.get("occurrences") or []),
            })
        missing_checks = sorted(set(expected) - seen_checks)
        if missing_checks:
            errors.append("missing_termchecks:" + ",".join(missing_checks))
        return ReviewOutcome(findings=tuple(findings),
                             unresolved=tuple(unresolved),
                             coverage_complete=coverage == "yes",
                             zero_findings=zero == "yes", raw=text,
                             errors=tuple(errors),
                             term_checks=tuple(term_checks))

    # -- adjudication and revision -----------------------------------------
    def adjudicate(self, draft: DraftChapter, review: ReviewOutcome, *,
                   log_event: bool = True,
                   usage_sink: list | None = None) -> dict:
        if not review.findings:
            return {}
        findings = []
        for finding in review.findings:
            findings.append({key: finding.get(key) for key in (
                "id", "category", "severity", "basis", "source_ref", "target_ref",
                "claim", "suggestion", "confidence", "source_text", "target_text")})
        term_checks = [{
            "id": check.get("id"), "term": check.get("term"),
            "canonical": check.get("canonical"),
            "aliases": check.get("aliases") or [],
            "status": check.get("status"), "confidence": check.get("confidence"),
            "locked": check.get("locked"),
            "occurrence_count": check.get("occurrence_count"),
            "verdict": check.get("verdict"),
            "source_ref": check.get("source_ref"),
            "target_ref": check.get("target_ref"),
            "occurrences": check.get("occurrences") or [],
        } for check in review.term_checks]
        messages = [{"role": "system", "content": AUDIT_ADJUDICATION}]
        messages.extend(AUDIT_ADJUDICATION_FEWSHOT)
        messages.extend([
            {"role": "user", "content":
             "【候选审校意见与绑定原段；逐项裁决】\n" +
             json.dumps(findings, ensure_ascii=False, separators=(",", ":")) +
             "\n【程序术语检查；fixed_missing 的 canonical 为已确认规范译名】\n" +
             json.dumps(term_checks, ensure_ascii=False, separators=(",", ":"))},
            {"role": "user", "content":
             "每个finding恰好输出一条DECISION；最后输出@@END。"
             "不要新增finding、重写正文。"},
        ])
        expected = {finding["id"] for finding in review.findings}
        for attempt in range(3):
            result = self._generate(messages, self.adjudication_tokens,
                                    f"adjudicate_{draft.order}",
                                    log_event=log_event, usage_sink=usage_sink)
            parsed = parse_records(result.text, {"DECISION"},
                                   syntax_profile="tolerant-records",
                                   allow_inline_end=True)
            errors = list(parsed["errors"])
            decisions = {}
            for row in parsed["records"]:
                fields = row["fields"]
                finding_id = fields.get("finding") or row["id"]
                if finding_id not in expected:
                    errors.append("unknown_finding:" + finding_id)
                    continue
                if fields.get("decision") not in {"confirmed", "dismissed", "unresolved"}:
                    errors.append("invalid_decision:" + finding_id)
                    continue
                if not fields.get("reason", "").strip():
                    errors.append("missing_reason:" + finding_id)
                decisions[finding_id] = {"decision": fields["decision"],
                                         "reason": fields.get("reason", "")}
            missing = expected - set(decisions)
            if missing:
                errors.append("missing_decisions:" + ",".join(sorted(missing)))
            if not errors:
                break
            messages.extend([
                {"role": "assistant", "content": result.text},
                {"role": "user", "content":
                 "上次裁决未通过校验。错误：" + "; ".join(errors) +
                 "\n请为每个finding重新输出完整DECISION记录，最后@@END。"},
            ])
        if errors:
            raise ChapterBlocked("adjudication_protocol",
                                 "adjudication output is invalid",
                                 details={"order": draft.order, "errors": errors})
        return decisions

    def _revise_draft(self, draft: DraftChapter, review: ReviewOutcome,
                      decisions: Mapping[str, Mapping], *,
                      log_event: bool = False,
                      usage_sink: list | None = None) -> DraftChapter:
        confirmed = [finding for finding in review.findings
                     if decisions.get(finding["id"], {}).get("decision") == "confirmed"]
        if not confirmed:
            return draft
        rows = self.prompt_rows(draft.order)
        expected_markers = self.expected_markers(rows)
        source = [{"ref": f"S{index:03d}", "text": row["text"]}
                  for index, row in enumerate(rows, 1)]
        target_paragraphs = list(draft.paragraphs)
        inline = (draft.assets or {}).get("inline") or []
        by_paragraph: dict[int, list[dict]] = {}
        for item in inline:
            by_paragraph.setdefault(int(item.get("paragraph", 0)), []).append(item)
        for paragraph_index, text in enumerate(target_paragraphs):
            for item in sorted(by_paragraph.get(paragraph_index, []),
                               key=lambda value: int(value.get("offset", 0)),
                               reverse=True):
                offset = max(0, min(int(item.get("offset", 0)), len(text)))
                text = text[:offset] + f"[[IMG:{item['asset_id']}]]" + text[offset:]
            target_paragraphs[paragraph_index] = text
        target = [{"ref": f"T{index:03d}", "text": text}
                  for index, text in enumerate(target_paragraphs, 1)]
        evidence = {
            "chapter": {"order": draft.order, "ja_title": draft.ja_title},
            "source": source,
            "term_checks": [
                {key: check.get(key) for key in (
                    "id", "term", "canonical", "aliases", "status", "confidence",
                    "locked", "occurrence_count", "verdict")}
                for check in review.term_checks
                if check.get("verdict") == "violation"],
            "confirmed_findings": [
                {**{key: finding.get(key) for key in (
                    "id", "category", "severity", "source_ref", "target_ref",
                    "claim", "suggestion", "confidence", "source_text", "target_text")},
                 "adjudication_reason": decisions[finding["id"]]["reason"]}
                for finding in confirmed],
            "draft": target,
        }
        aliases = [row["ref"] for row in source]
        messages = [
            {"role": "system", "content":
             "你是日译中轻小说翻译。只修复已确认的含义差异，保留其他正确内容、"
             "事实、语气和段落。按源段记录协议输出完整本章；不要解释。"},
            {"role": "user", "content":
             "【原文、当前草稿与已确认问题】\n" +
             json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))},
            {"role": "user", "content": source_records_rules(aliases)},
        ]
        parsed = None
        raw = ""
        for attempt in range(3):
            result = self._generate(messages, self.translation_tokens,
                                    f"revise_{draft.order}",
                                    log_event=log_event, usage_sink=usage_sink)
            raw = result.text
            parsed = self.parse_translation(raw, aliases,
                                            expected_markers=expected_markers)
            if not parsed["errors"]:
                break
            messages = messages[:-1] + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content":
                 "上次修订输出未通过源段校验。错误：" + "; ".join(parsed["errors"]) +
                 "\n" + source_records_rules(aliases)}]
        if parsed is None or parsed["errors"]:
            raise ChapterBlocked("revision_protocol", "revision output is incomplete",
                                 details={"order": draft.order,
                                          "errors": parsed["errors"] if parsed else []})
        assets = dict(draft.assets or {})
        assets["source_edition"] = getattr(self.flow, "source_edition", "")
        assets["inline"] = parsed["inline_assets"]
        if not assets.get("block"):
            assets["block"] = self.asset_index.chapter_assets(draft.order).get("block", [])
        asset_ids = {item["asset_id"] for item in assets["inline"]}
        asset_ids.update(item["asset_id"] for item in assets["block"])
        assets["asset_ids"] = sorted(asset_ids)
        return DraftChapter(order=draft.order, source_id=draft.source_id,
                            source_hash=draft.source_hash, ja_title=draft.ja_title,
                            title_zh=parsed["title"], text=parsed["text"],
                            paragraphs=tuple(parsed["paragraphs"]),
                            version=0,
                            review={"protocol": parsed["profile"],
                                    "revision_from": draft.version},
                            assets=assets)

    def revise(self, draft: DraftChapter, review: ReviewOutcome,
               decisions: Mapping[str, Mapping]) -> DraftChapter:
        confirmed = [finding for finding in review.findings
                     if decisions.get(finding["id"], {}).get("decision") == "confirmed"]
        revised = self._revise_draft(draft, review, decisions, log_event=True)
        version = self.store.put_draft(
            order=revised.order, source_id=revised.source_id,
            source_hash=revised.source_hash, ja_title=revised.ja_title,
            title_zh=revised.title_zh, text=revised.text,
            status="draft", review=revised.review, assets=revised.assets)
        self.store.event("revise", order=revised.order, version=version,
                         confirmed=[finding["id"] for finding in confirmed])
        return replace(revised, version=version)

    # -- orchestration ------------------------------------------------------
    def run_order(self, order: int) -> dict:
        current = self.store.chapter(order)
        if current and current["status"] == "accepted":
            return {"order": order, "status": "accepted", "reused": True}
        self._check_limits()
        if current and current["status"] == "blocked":
            self.store.set_status(order, "draft")
        draft = self.translate_order(order)
        review = self.review_order(draft)
        if review.errors:
            raise ChapterBlocked("review_protocol", "review output is invalid",
                                 details={"order": order, "errors": review.errors})
        if not review.coverage_complete:
            # A second, explicit full-coverage request; no semantic repair yet.
            self.store.event("review_incomplete", order=order)
            raise ChapterBlocked("review_incomplete",
                                 "review did not declare semantic coverage",
                                 details={"order": order})
        decisions = self.adjudicate(draft, review)
        confirmed = {finding["id"]: finding for finding in review.findings
                     if decisions.get(finding["id"], {}).get("decision") == "confirmed"}
        major = self._revision_findings(confirmed, review)
        rounds = 0
        while major and rounds < self.max_revisions:
            draft = self.revise(draft, review, decisions)
            review = self.review_order(draft)
            if review.errors:
                raise ChapterBlocked("review_protocol", "review output is invalid",
                                     details={"order": order,
                                              "errors": review.errors})
            decisions = self.adjudicate(draft, review)
            confirmed = {finding["id"]: finding for finding in review.findings
                         if decisions.get(finding["id"], {}).get("decision") == "confirmed"}
            major = self._revision_findings(confirmed, review)
            rounds += 1
        review_data = {
            "profile": "chapter-review",
            "rounds": rounds,
            "findings": review.findings,
            "decisions": decisions,
            "unresolved": review.unresolved,
            "term_checks": review.term_checks,
            "semantic_coverage_complete": review.coverage_complete,
            "accepted_with_major": sorted(major),
            "text_hash": digest(draft.text),
        }
        if major:
            self.store.set_status(order, "blocked", review_data)
            self.store.event("blocked", order=order,
                             findings=sorted(major))
            raise ChapterBlocked("unresolved_major",
                                 "major findings remain after the revision budget",
                                 details={"order": order, "findings": sorted(major)})
        self.store.set_status(order, "accepted", review_data)
        self.store.event("accept", order=order, version=draft.version,
                         rounds=rounds, findings=len(review.findings))
        return {"order": order, "status": "accepted", "rounds": rounds,
                "findings": len(review.findings)}

    def run(self, start: int | None = None, end: int | None = None,
            *, update_summaries: bool = True) -> dict:
        start = self.continuation_start if start is None else start
        end = self.main_end if end is None else end
        results = []
        for order in range(max(start, self.continuation_start), end):
            if order not in self.source_by_order:
                continue
            if _is_extra_title(self.source(order)["raw_title"]):
                continue
            result = self.run_order(order)
            results.append(result)
            if update_summaries:
                self.update_target_summary()
        return {"start": start, "end": end, "chapters": len(results),
                "results": results, "stats": self.store.stats()}

    def _revision_findings(self, confirmed: Mapping[str, Mapping],
                           review: ReviewOutcome) -> dict[str, Mapping]:
        fixed_refs = set()
        for check in review.term_checks:
            if (check.get("verdict") != "violation" or
                    check.get("status") != "fixed_missing"):
                continue
            occurrences = check.get("occurrences") or ()
            if occurrences:
                fixed_refs.update(
                    (item.get("source_ref"), item.get("target_ref"))
                    for item in occurrences)
            else:
                fixed_refs.add((check.get("source_ref"), check.get("target_ref")))
        result = {}
        for ident, finding in confirmed.items():
            refs = (finding.get("source_ref"), finding.get("target_ref"))
            if (finding.get("severity") in {"critical", "major"} or
                    (finding.get("category") == "term" and refs in fixed_refs)):
                result[ident] = finding
        return result

    def _process_draft(self, draft: DraftChapter, *, history, glossary,
                       unresolved_prior, max_revisions: int,
                       tool_context: dict | None = None) -> dict:
        usages = []
        try:
            return self._process_draft_inner(
                draft, history=history, glossary=glossary,
                unresolved_prior=unresolved_prior,
                max_revisions=max_revisions, usages=usages,
                tool_context=tool_context)
        except Exception as exc:
            return {"status": "error", "draft": draft, "review": None,
                    "decisions": {}, "rounds": 0, "usages": usages,
                    "tool_events": (tool_context or {}).get("events", []),
                    "message": str(exc)}

    def _process_draft_inner(self, draft: DraftChapter, *, history, glossary,
                             unresolved_prior, max_revisions: int,
                             usages: list,
                             tool_context: dict | None = None) -> dict:
        """Pure worker: model calls only, no SQLite or mutable runner state."""
        if tool_context is None:
            tool_context = {
                "accepted_texts": {},
                "glossary": [{
                    "source": term.source, "target": term.target,
                    "kind": term.kind, "confidence": term.confidence,
                    "aliases": list(term.aliases), "locked": term.locked,
                } for term in glossary],
                "events": [],
                "buffer_events": True,
            }
        review = self._review_draft(
            draft, history=history, glossary=glossary,
            unresolved_prior=unresolved_prior, log_event=False,
            usage_sink=usages, tool_context=tool_context)
        if review.errors:
            return {"status": "error", "draft": draft, "review": review,
                    "decisions": {}, "rounds": 0, "usages": usages,
                    "tool_events": tool_context.get("events", [])}
        if not review.coverage_complete:
            return {"status": "incomplete", "draft": draft, "review": review,
                    "decisions": {}, "rounds": 0, "usages": usages,
                    "tool_events": tool_context.get("events", [])}
        decisions = self.adjudicate(draft, review, log_event=False,
                                    usage_sink=usages)
        confirmed = {finding["id"]: finding for finding in review.findings
                     if decisions.get(finding["id"], {}).get("decision") == "confirmed"}
        major = self._revision_findings(confirmed, review)
        rounds = 0
        while major and rounds < max_revisions:
            draft = self._revise_draft(draft, review, decisions, log_event=False,
                                       usage_sink=usages)
            review = self._review_draft(
                draft, history=history, glossary=glossary,
                unresolved_prior=unresolved_prior, log_event=False,
                usage_sink=usages, tool_context=tool_context)
            if review.errors:
                return {"status": "error", "draft": draft, "review": review,
                        "decisions": {}, "rounds": rounds, "usages": usages,
                        "tool_events": tool_context.get("events", [])}
            decisions = self.adjudicate(draft, review, log_event=False,
                                        usage_sink=usages)
            confirmed = {finding["id"]: finding for finding in review.findings
                         if decisions.get(finding["id"], {}).get("decision") == "confirmed"}
            major = self._revision_findings(confirmed, review)
            rounds += 1
        return {"status": "blocked" if major else "accepted",
                "draft": draft, "review": review, "decisions": decisions,
                "rounds": rounds, "usages": usages,
                "tool_events": tool_context.get("events", []),
                "major": sorted(major)}

    def _review_data(self, result: Mapping) -> dict:
        review = result["review"]
        return {
            "profile": "chapter-review",
            "rounds": result["rounds"],
            "findings": review.findings,
            "decisions": result["decisions"],
            "unresolved": review.unresolved,
            "term_checks": review.term_checks,
            "semantic_coverage_complete": review.coverage_complete,
            "accepted_with_major": result.get("major", []),
            "text_hash": digest(result["draft"].text),
        }

    def run_concurrent(self, start: int | None = None, end: int | None = None,
                       *, translation_concurrency: int | None = None,
                       review_concurrency: int | None = None,
                       batch_size: int | None = None,
                       update_summaries: bool = True) -> dict:
        """Translate a batch with source-only intra-batch context, in parallel.

        Within one batch, chapter N sees the accepted prefix plus the Japanese
        source text of preceding chapters in the same batch, not their
        translations. A shared batch glossary is extracted first, so all
        workers can run concurrently without a translation dependency chain.

        The last chapter is translated first and alone: its prompt contains
        every earlier chapter's prefix, so finishing it warms provider-side
        prefix caches for the rest of the batch. Reviews/adjudication/revisions
        then run concurrently; acceptance is written in order by the main
        thread.
        """
        workflow = self.project.config.workflow
        translation_concurrency = (workflow.translation_concurrency
                                   if translation_concurrency is None
                                   else translation_concurrency)
        review_concurrency = (workflow.review_concurrency
                              if review_concurrency is None else review_concurrency)
        batch_size = workflow.batch_size if batch_size is None else batch_size
        if translation_concurrency < 1 or review_concurrency < 1 or batch_size < 1:
            raise ValueError("concurrency and batch_size must be positive")
        start = self.continuation_start if start is None else start
        end = self.main_end if end is None else end
        accepted = set(self.store.accepted_orders())
        orders = [order for order in range(max(start, self.continuation_start), end)
                  if order in self.source_by_order
                  and not _is_extra_title(self.source(order)["raw_title"])
                  and order not in accepted]
        results = []
        index = 0
        with ThreadPoolExecutor(max_workers=max(1, translation_concurrency)) as trans_pool, \
                ThreadPoolExecutor(max_workers=max(1, review_concurrency)) as review_pool:
            while index < len(orders):
                self._check_limits()
                batch = orders[index:index + batch_size]
                documents = self.documents(max(batch) + 1)
                snapshot = self.snapshot(batch[0], documents)
                glossary = self.ensure_batch_glossary(batch)
                unresolved = self._unresolved()
                # Read-only snapshot for review tools. Workers must not use
                # the store connection, which belongs to the main thread.
                tool_snapshot = {
                    "accepted_texts": self.store.translations(),
                    "glossary": self.store.glossary(limit=1000),
                }
                translation_jobs = {}

                def submit_translation(order):
                    usages = []
                    return (trans_pool.submit(
                        self._translate_draft, order,
                        documents=documents, snapshot=snapshot,
                        batch_orders=batch, glossary=glossary,
                        unresolved_prior=unresolved, log_event=False,
                        usage_sink=usages), usages)

                # The last chapter's prompt contains every earlier chapter's
                # prefix. Finishing it first warms the provider's prefix cache
                # for the whole batch; fan-out afterwards is nearly all hits.
                if len(batch) > 1:
                    warm = submit_translation(batch[-1])
                    translation_jobs[batch[-1]] = warm
                    try:
                        warm[0].result()
                    except Exception:
                        pass
                for order in batch:
                    if order not in translation_jobs:
                        translation_jobs[order] = submit_translation(order)
                drafts, translation_error = [], None
                for order in batch:
                    job, usages = translation_jobs[order]
                    try:
                        draft = job.result()
                    except Exception as exc:
                        for usage in usages:
                            self.store.event("model", **usage)
                        if translation_error is None:
                            translation_error = (order, type(exc).__name__, str(exc))
                        continue
                    for usage in usages:
                        self.store.event("model", **usage)
                    version = self.store.put_draft(
                        order=order, source_id=draft.source_id,
                        source_hash=draft.source_hash, ja_title=draft.ja_title,
                        title_zh=draft.title_zh, text=draft.text,
                        status="draft", review=draft.review, assets=draft.assets)
                    draft = replace(draft, version=version)
                    self.store.event("translate", order=order, version=version,
                                     paragraphs=len(draft.paragraphs))
                    drafts.append(draft)
                if translation_error:
                    order, reason, message = translation_error
                    row = self.store.chapter(order)
                    if row:
                        self.store.set_status(order, "blocked",
                                              {"error": reason, "message": message})
                    self.store.event("blocked", order=order,
                                     reason="translation_" + reason)
                    raise ChapterBlocked(reason, f"chapter {order} translation failed",
                                         details={"order": order, "batch": batch})
                provisional = {draft.order: draft.text for draft in drafts}
                review_jobs = []
                for draft in drafts:
                    history = self._history_pairs(
                        draft.order,
                        provisional={o: t for o, t in provisional.items()
                                     if o < draft.order})
                    tool_context = {**tool_snapshot, "events": [],
                                    "buffer_events": True}
                    review_jobs.append((draft, review_pool.submit(
                        self._process_draft, draft, history=history,
                        glossary=glossary, unresolved_prior=unresolved,
                        max_revisions=self.max_revisions,
                        tool_context=tool_context)))
                blocked = None
                for draft, job in review_jobs:
                    try:
                        result = job.result()
                    except Exception as exc:
                        result = {"status": "error", "draft": draft,
                                  "usages": [], "message": str(exc),
                                  "review": None, "decisions": {}, "rounds": 0}
                    for event in result.get("tool_events") or []:
                        self.store.event("review_tool", **event)
                    for usage in result.get("usages", []):
                        self.store.event("model", **usage)
                    status = result["status"]
                    if status == "accepted":
                        review_data = self._review_data(result)
                        final = result["draft"]
                        if final.text == draft.text:
                            self.store.set_status(final.order, "accepted",
                                                  review_data)
                            version = draft.version
                        else:
                            version = self.store.put_draft(
                                order=final.order, source_id=final.source_id,
                                source_hash=final.source_hash,
                                ja_title=final.ja_title,
                                title_zh=final.title_zh,
                                text=final.text, status="accepted",
                                review=review_data, assets=final.assets)
                        self.store.event("accept", order=final.order,
                                         version=version, rounds=result["rounds"],
                                         findings=len(result["review"].findings))
                        results.append({"order": final.order, "status": "accepted",
                                        "rounds": result["rounds"],
                                        "findings": len(result["review"].findings)})
                    else:
                        review_data = (result.get("review") and
                                       self._review_data(result)) or {
                                           "error": status,
                                           "message": result.get("message", "")}
                        final = result["draft"]
                        self.store.put_draft(
                            order=final.order, source_id=final.source_id,
                            source_hash=final.source_hash,
                            ja_title=final.ja_title, title_zh=final.title_zh,
                            text=final.text, status="blocked",
                            review=review_data, assets=final.assets)
                        self.store.event("blocked", order=final.order,
                                         reason=status)
                        blocked = (final.order, status)
                        break
                if update_summaries:
                    self.update_target_summary()
                if blocked:
                    order, reason = blocked
                    raise ChapterBlocked(
                        reason, f"chapter {order} did not pass review",
                        details={"order": order, "batch": batch})
                index += len(batch)
        return {"start": start, "end": end, "chapters": len(results),
                "results": results, "stats": self.store.stats()}

    def contiguous_accepted_end(self) -> int:
        accepted = set(self.store.accepted_orders())
        order = self.continuation_start
        while order < self.main_end:
            source = self.source_by_order.get(order)
            if source is None or _is_extra_title(source["raw_title"]):
                order += 1
                continue
            if order in accepted:
                order += 1
                continue
            return order
        return self.main_end

    def update_target_summary(self, *, force: bool = False) -> str | None:
        end = self.contiguous_accepted_end() - self.summary_lag
        if end <= self._last_summary_end:
            return None
        if end - self._last_summary_end < self.summary_every and not force:
            return None
        start, stop = self._last_summary_end, end
        prior = [row for row in self.store.summaries("target")
                 if row["end_order"] == start]
        previous = prior[-1]["text"] if prior else ""
        pairs = []
        for order in range(start, stop):
            source = self.source_by_order.get(order)
            if source is None or _is_extra_title(source["raw_title"]):
                continue
            row = self.store.chapter(order)
            if not row or row["status"] != "accepted":
                return None
            pairs.append(f"### {row['ja_title']}\n{source['text']}\n---\n{row['text']}")
        if not pairs:
            return None
        self._check_limits()
        prompt = (
            "你是轻小说资料整理员。下面是此前累计摘要（仅用于保持连续性）和新增章节的"
            "日文原文与已审中文译文。请输出新增部分的中文摘要，覆盖人物、地点、事件、"
            "专有名词、关系变化和未决线索；不得编造原文没有的内容。只输出连续摘要。\n\n"
            f"【此前摘要】\n{previous or '（无）'}\n\n【新增章节】\n" + "\n\n".join(pairs))
        result = self._generate([
            {"role": "system", "content": "你是严谨的轻小说资料整理员，只输出中文摘要。"},
            {"role": "user", "content": prompt},
        ], self.summary_tokens, f"target_summary_{start}_{stop}")
        self.store.put_summary("target", start, stop, result.text)
        self._last_summary_end = stop
        self.store.event("summary", kind_detail="target", start=start, end=stop)
        return result.text

    def ensure_reference_summary(self, *, force: bool = False) -> str | None:
        existing = self.store.summary("reference", 0, self.continuation_start)
        if existing and not force:
            return existing
        previous = ""
        cursor = 0
        if not force:
            done = [row for row in self.store.summaries("reference-progress")
                    if row["start_order"] == 0]
            if done:
                latest = max(done, key=lambda row: row["end_order"])
                previous, cursor = latest["text"], latest["end_order"]
        chunks = list(range(cursor, self.continuation_start, self.reference_chunk))
        for start in chunks:
            self._check_limits()
            stop = min(start + self.reference_chunk, self.continuation_start)
            blocks = []
            for order in range(start, stop):
                text = self.reference_texts.get(order)
                source = self.source_by_order.get(order)
                if text and source:
                    blocks.append(f"### {source['raw_title']}\n{text}")
            if not blocks:
                continue
            prompt = (
                "你是轻小说资料整理员。此前累计摘要只用于保持连续性；下面新增的官方中文"
                "参考章节。请输出截至这些新增章节的更新累计摘要，覆盖人物、地点、事件、"
                "专有名词、关系和未决线索；不得编造。只输出连续摘要。\n\n"
                f"【此前摘要】\n{previous or '（无）'}\n\n【新增章节】\n" +
                "\n\n".join(blocks))
            result = self._generate([
                {"role": "system", "content": "你是严谨的轻小说资料整理员，只输出中文摘要。"},
                {"role": "user", "content": prompt},
            ], self.summary_tokens, f"reference_summary_{start}_{stop}")
            previous = result.text
            self.store.put_summary("reference-progress", 0, stop, previous)
            self.store.event("summary_chunk", start=start, end=stop,
                             chars=len(previous))
        if previous:
            self.store.put_summary("reference", 0, self.continuation_start, previous)
            self.store.event("summary", kind_detail="reference", start=0,
                             end=self.continuation_start)
        return previous or None

    def ensure_glossary(self, *, force: bool = False) -> list[dict]:
        complete = self.store.db.execute(
            "SELECT 1 FROM events WHERE kind='glossary_complete' LIMIT 1").fetchone()
        if complete and not force:
            return self.store.glossary()
        done = set()
        if not force:
            for (payload,) in self.store.db.execute(
                    "SELECT payload FROM events WHERE kind='glossary_chunk'"):
                value = json.loads(payload)
                done.add((value.get("start"), value.get("end")))
        chunk = self.reference_chunk
        expected = {(start, min(start + chunk, self.continuation_start))
                    for start in range(0, self.continuation_start, chunk)}
        for start in range(0, self.continuation_start, chunk):
            self._check_limits()
            stop = min(start + chunk, self.continuation_start)
            if (start, stop) in done:
                continue
            pairs = []
            for order in range(start, stop):
                source = self.source_by_order.get(order)
                text = self.reference_texts.get(order)
                if source and text:
                    pairs.append(
                        f"### JA {source['raw_title']}\n{source['text']}\n"
                        f"### ZH {text}")
            if not pairs:
                continue
            style_hint = self._target_style_hint()
            prompt = _reference_glossary_prompt(pairs, style_hint)
            try:
                result = self._generate([
                    {"role": "system", "content": "你是严谨的日中对照术语整理员。"},
                    {"role": "user", "content": prompt},
                ], self.glossary_tokens, f"glossary_{start}_{stop}")
                rows = _parse_json_array(result.text)
                self.store.put_glossary(rows)
                self.store.event("glossary_chunk", start=start, end=stop,
                                 terms=len(rows))
                try:
                    focused = self._generate([
                        {"role": "system", "content": "你是严谨的日中对照术语整理员。"},
                        {"role": "user", "content":
                         _reference_focused_prompt(pairs, style_hint)},
                    ], self.glossary_tokens, f"glossary_focus_{start}_{stop}")
                    focused_rows = _parse_json_array(focused.text)
                    self.store.put_glossary(focused_rows)
                    self.store.event("glossary_focus_chunk", start=start,
                                     end=stop, terms=len(focused_rows))
                except (PipelineError, ProviderError, ValueError):
                    self.store.event("glossary_focus_failed", start=start,
                                     end=stop)
                done.add((start, stop))
            except (PipelineError, ProviderError, ValueError):
                self.store.event("glossary_failed", start=start, end=stop)
        if expected <= done:
            self.store.event("glossary_complete", terms=len(self.store.glossary()))
        else:
            self.store.event("glossary_incomplete",
                             missing=sorted(expected - done))
        return self.store.glossary()

    def ensure_batch_glossary(self, batch_orders: Sequence[int]) -> list[GlossaryTerm]:
        """Extract one shared term table for a concurrently translated batch.

        The extraction sees the batch's Japanese source and the existing
        glossary, so all workers use the same target forms even though they do
        not see each other's translations.
        """
        orders = [order for order in batch_orders
                  if order in self.source_by_order
                  and not _is_extra_title(self.source(order)["raw_title"])]
        if not orders:
            return list(self.glossary)
        text = "\n\n".join(
            f"### {self.source(order)['raw_title']}\n{self.source(order)['text']}"
            for order in orders)
        existing = self.store.glossary(limit=1000)
        relevant = [row for row in existing
                    if str(row.get("source", "")).strip() in text]
        self._check_limits()
        rows = []
        prompt = _batch_glossary_prompt(text, relevant, self._target_style_hint())
        try:
            result = self._generate([
                {"role": "system", "content": "你是严谨的日中轻小说术语整理员。"},
                {"role": "user", "content": prompt},
            ], self.glossary_tokens, f"batch_glossary_{orders[0]}_{orders[-1]}")
            parsed_rows = _parse_json_array(result.text)
            deduped = {}
            for row in parsed_rows:
                source = str(row.get("source", "")).strip()
                target = str(row.get("target", "")).strip()
                if source and target and source not in deduped:
                    deduped[source] = {**row, "source": source, "target": target}
            rows = [deduped[source] for source in sorted(deduped)]
            # A term never observed in the aligned reference cannot be a fixed
            # canonical form; keep the first continuation sighting a candidate.
            reference_source = "\n".join(
                self.source(order).get("text", "")
                for order in range(0, self.continuation_start)
                if order in self.source_by_order)
            for row in rows:
                if row["source"] not in reference_source:
                    row["confidence"] = 3
            self.store.put_glossary(rows)
            self.store.event("batch_glossary", start=orders[0], end=orders[-1] + 1,
                             terms=len(rows))
        except (PipelineError, ProviderError, ValueError):
            self.store.event("batch_glossary_failed", start=orders[0],
                             end=orders[-1] + 1)
        merged = {}
        # Preserve the existing first-seen order and append new terms; sorting
        # a growing table would move old entries and break cached prefixes.
        for row in relevant:
            source = str(row.get("source", "")).strip()
            target = str(row.get("target", "")).strip()
            if source and target:
                merged[source] = GlossaryTerm(
                    source, target, str(row.get("kind") or "other"),
                    int(row.get("confidence", 2)),
                    tuple(row.get("aliases") or ()),
                    bool(row.get("locked")))
        for row in rows:
            source = row["source"]
            if source in merged:
                continue
            merged[source] = GlossaryTerm(
                source, row["target"], str(row.get("kind") or "other"),
                _normalize_confidence(row.get("confidence")),
                tuple(_normalize_aliases(row.get("aliases"))),
                bool(row.get("locked", False)))
        return list(merged.values())


def _reference_glossary_prompt(pairs: Sequence[str], style_hint: str = "") -> str:
    return (
        "下面是同一部Web小说相邻章节的日文原文和官方中文译文。请提取在两版中"
        "稳定对应的专名：人名、地名、组织、称号、职务、种族/族裔、物品、技能。"
        "不要提取普通名词、整句或只在单侧出现的词。只输出JSON数组，不要解释或代码块：\n"
        '[{"source":"クリシェ","target":"庫莉茜耶","kind":"person","confidence":1,"aliases":[]}]'
        "\nkind只能是person、place、organization、title、role、race、item、skill、other。"
        "\nconfidence只能是：1=已固定译名（参考译文明确且稳定，必须沿用）；"
        "2=需要根据语境判断（参考有候选，但可能随语境变化，可在aliases给出允许变体）；"
        "3=仅候选（首次出现或证据不足，后续确认）。没有参考中文支持时不得使用1。"
        "\ntarget必须与参考中文正文的字形一致（参考为繁体时不得输出简体）；"
        "同一源词有多个中文写法时 confidence=2 并把其它写法放入 aliases。"
        "\naliases是同一对象可接受的其它中文写法数组；没有就写[]。最多40项。"
        + (f"\n【目标字形参考】{style_hint}" if style_hint else "")
        + "\n\n" + "\n\n".join(pairs))


def _reference_focused_prompt(pairs: Sequence[str], style_hint: str = "") -> str:
    return (
        "下面是同一部Web小说相邻章节的日文原文和官方中文译文。只提取在两版中稳定对应的："
        "职务、称号、组织职位、种族/族裔、组织/团体称谓。即使不是专有名词，只要反复出现并稳定对应也要提取；"
        "忽略人名、地名、物品、技能和普通叙述。只输出JSON数组，不要解释或代码块：\n"
        '[{"source":"組合長","target":"公會長","kind":"role","confidence":2,"aliases":["會長"]}]'
        "\nkind只能是title、role、race、organization、other。"
        "\nconfidence只能是：1=两版稳定且无歧义；2=同一源词在不同语境有多个中文写法，必须在aliases列出；"
        "3=首次出现或证据不足。没有参考中文支持时不得使用1。"
        "\ntarget必须与参考中文正文的字形一致（参考为繁体时不得输出简体）。最多60项。"
        + (f"\n【目标字形参考】{style_hint}" if style_hint else "")
        + "\n\n" + "\n\n".join(pairs))


def _batch_glossary_prompt(text: str, relevant: Sequence[Mapping],
                           style_hint: str = "") -> str:
    return (
        "下面是本批次相邻章节的日文原文和已有术语表。请补充本批次出现的专名："
        "人名、地名、组织、称号、职务、种族/族裔、物品、技能。已有术语必须沿用已有规范译名，"
        "不要改写；只输出需要新增或确认的项，不要输出普通名词或整句。只输出JSON数组，不要解释"
        "或代码块：\n"
        '[{"source":"クリシェ","target":"庫莉茜耶","kind":"person","confidence":1,"aliases":[]}]\n'
        "kind只能是person、place、organization、title、role、race、item、skill、other。"
        "confidence只能是：1=已固定译名；2=需要根据语境判断；3=仅候选。"
        "没有参考中文支持的首次出现必须使用3，不得标1。"
        "target必须与已有术语表和项目参考译文的字形一致（参考为繁体时不得输出简体）。"
        "aliases是允许的其它写法数组。最多80项。"
        + (f"\n【目标字形参考】{style_hint}" if style_hint else "")
        + "\n\n【已有术语表】\n"
        + json.dumps(list(relevant), ensure_ascii=False, separators=(",", ":"))
        + "\n\n【本批次原文】\n" + text)


def _parse_json_array(text: str) -> list[dict]:
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```[a-zA-Z]*\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    try:
        data = json.loads(value)
    except ValueError:
        match = re.search(r"\[[\s\S]*\]", value)
        if not match:
            raise ValueError("no JSON array in response")
        data = json.loads(match.group(0))
    if not isinstance(data, list):
        raise ValueError("expected a JSON array")
    return [row for row in data if isinstance(row, dict)
            and isinstance(row.get("source"), str)
            and isinstance(row.get("target"), str)]


# -- reference alignment ---------------------------------------------------
def align_reference_by_titles(runner: TranslationRunner,
                              chinese_entries: Sequence[dict],
                              source_entries: Sequence[dict]) -> dict[int, int]:
    """Ask a model to map official Chinese chapters to Japanese web episodes.

    The official edition may split one web episode into two chapters, so the
    result is a source-order -> reference-order map (many Chinese chapters can
    point at one Japanese episode; the first is used for context).
    """
    zh = [{"order": index,
           "title": entry.get("raw_title", entry.get("title", ""))}
          for index, entry in enumerate(chinese_entries)]
    ja = [{"order": entry.get("edition_order", entry.get("order")),
           "title": entry.get("raw_title", entry.get("title", "")),
           "extra": _is_extra_title(entry.get("raw_title", entry.get("title", "")))}
          for entry in source_entries]
    prompt = (
        "下面是同一部作品的官方中文版主線章节目录和日文Web版话目录。中文版可能把"
        "一话拆成上下两章、合并章节或加入未编号的番外；日文版带extra=true的是设定/"
        "人物介绍等非正文页面，不要映射到这些页面。中文版章节编号与日文话编号不同，"
        "不能按编号直接对齐；例如中文『暗灰色的战场』对应日文『鈍色の戦場』。"
        "请只建立主線正文的对应关系，允许一个日文话对应多个中文章节。"
        "只输出JSON对象，不要解释或代码块：\n"
        '{"pairs":[{"zh":0,"ja":0,"confidence":"high"}],"continuation_ja":185}'
        "\nzh/ja使用列表中的order。confidence为high/medium/low。continuation_ja是"
        "最后一个中文主線章节所对应日文话之后的下一话order（若最后一章对应多个候选，"
        "用最有把握的）。\n\n【中文目录】\n" +
        json.dumps(zh, ensure_ascii=False, separators=(",", ":")) +
        "\n\n【日文目录】\n" +
        json.dumps(ja, ensure_ascii=False, separators=(",", ":")))
    result = runner._generate([
        {"role": "system", "content": "你是严谨的日中版本目录对齐员。"},
        {"role": "user", "content": prompt},
    ], 32768, "reference_alignment")
    value = result.text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```[a-zA-Z]*\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    match = re.search(r"\{[\s\S]*\}", value)
    data = json.loads(match.group(0) if match else value)
    pairs = data.get("pairs") if isinstance(data, dict) else None
    if not isinstance(pairs, list):
        raise PipelineError("alignment_failed", "alignment response has no pairs")
    known_ja = {entry.get("edition_order", entry.get("order")) for entry in source_entries}
    extra_ja = {row["order"] for row in ja if row["extra"]}
    known_zh = {entry["order"] for entry in zh}
    cleaned, mapping = [], {}
    for row in pairs:
        if not isinstance(row, dict):
            continue
        zh_order, ja_order = row.get("zh"), row.get("ja")
        if zh_order in known_zh and ja_order in known_ja and ja_order not in extra_ja:
            cleaned.append({"zh": zh_order, "ja": ja_order,
                            "confidence": row.get("confidence", "medium")})
            mapping.setdefault(ja_order, zh_order)
    if not mapping:
        raise PipelineError("alignment_failed", "alignment produced no valid pairs")
    continuation = data.get("continuation_ja") if isinstance(data, dict) else None
    if type(continuation) is not int or continuation not in known_ja:
        continuation = max(mapping) + 1
    return {"pairs": cleaned, "mapping": mapping, "continuation_ja": continuation}


# -- EPUB output -----------------------------------------------------------
_ASSET_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif",
                     "image/webp": "webp", "image/svg+xml": "svg"}


def _asset_filename(asset_id: str, media_type: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", asset_id).strip("-") or "asset"
    return safe + "." + _ASSET_EXTENSIONS.get(media_type, "bin")


def _render_image(href: str, alt: str) -> str:
    return (f'<img src="{html.escape(href, quote=True)}"'
            f' alt="{html.escape(alt or "", quote=True)}"/>')


def _render_paragraph(text: str, placements: Sequence[Mapping],
                      asset_files: Mapping[str, Mapping]) -> str:
    if not placements:
        return f"<p>{html.escape(text)}</p>"
    pieces: list[str] = []
    cursor = 0
    for item in sorted(placements, key=lambda value: int(value.get("offset", 0))):
        offset = max(0, min(int(item.get("offset", 0)), len(text)))
        if offset < cursor:
            offset = cursor
        pieces.append(html.escape(text[cursor:offset]))
        asset = asset_files[item["asset_id"]]
        pieces.append(_render_image("../images/" + asset["filename"],
                                    asset.get("alt", "")))
        cursor = offset
    pieces.append(html.escape(text[cursor:]))
    return "<p>" + "".join(pieces) + "</p>"


def _render_block_image(asset_id: str, asset_files: Mapping[str, Mapping]) -> str:
    asset = asset_files[asset_id]
    return ('<p class="image">' +
            _render_image("../images/" + asset["filename"], asset.get("alt", "")) +
            "</p>")


def build_epub(*, output: str | Path, title: str, chapters: Sequence[dict],
               language: str = "zh-CN",
               assets: Mapping[str, Mapping] | None = None,
               cover_asset: str | None = None) -> Path:
    """Write an EPUB 3 from ordered chapter rows, optionally with image assets.

    ``assets`` maps asset_id to ``{data: bytes, media_type: str, alt: str}``.
    Chapter assets use ``inline`` (paragraph/offset) and ``block``
    (before_paragraph/after_paragraph) placements.
    """
    output = Path(output)
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    identifier = "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, title))
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    asset_source = dict(assets or {})
    used_assets = {item["asset_id"] for chapter in chapters
                   for item in ((chapter.get("assets") or {}).get("inline") or [])}
    used_assets.update(item["asset_id"] for chapter in chapters
                       for item in ((chapter.get("assets") or {}).get("block") or []))
    if cover_asset:
        used_assets.add(cover_asset)
    missing = sorted(asset_id for asset_id in used_assets if asset_id not in asset_source)
    if missing:
        raise ValueError("missing asset data: " + ", ".join(missing))
    asset_files: dict[str, dict] = {}
    asset_manifest: list[str] = []
    binary_files: dict[str, bytes] = {}
    for asset_id in sorted(used_assets):
        info = asset_source[asset_id]
        media_type = info.get("media_type") or "application/octet-stream"
        filename = _asset_filename(asset_id, media_type)
        asset_files[asset_id] = {"filename": filename, "alt": info.get("alt", "")}
        binary_files["OEBPS/images/" + filename] = info["data"]
        manifest_id = "cover-image" if asset_id == cover_asset else "asset-" + re.sub(
            r"[^A-Za-z0-9_.-]+", "-", asset_id)
        properties = ' properties="cover-image"' if asset_id == cover_asset else ""
        asset_manifest.append(
            f'<item id="{manifest_id}" href="images/{filename}" '
            f'media-type="{media_type}"{properties}/>')

    manifest, spine, nav = [], [], []
    files: dict[str, str | bytes] = {}
    for index, chapter in enumerate(chapters, 1):
        name = f"chapter-{index:04d}.xhtml"
        chapter_title = html.escape(chapter["title"] or f"第{index}章")
        chapter_assets = chapter.get("assets") or {}
        inline_by_paragraph: dict[int, list[Mapping]] = {}
        for item in chapter_assets.get("inline") or []:
            inline_by_paragraph.setdefault(int(item.get("paragraph", 0)), []).append(item)
        before: dict[int, list[str]] = {}
        after: dict[int, list[str]] = {}
        for item in chapter_assets.get("block") or []:
            rendered = _render_block_image(item["asset_id"], asset_files)
            if "before_paragraph" in item:
                before.setdefault(int(item["before_paragraph"]), []).append(rendered)
            else:
                after.setdefault(int(item.get("after_paragraph", -1)), []).append(rendered)
        parts: list[str] = list(after.get(-1, []))
        paragraph_index = 0
        for text in chapter["paragraphs"]:
            if not text.strip():
                continue
            parts.extend(before.get(paragraph_index, []))
            parts.append(_render_paragraph(text,
                                           inline_by_paragraph.get(paragraph_index, []),
                                           asset_files))
            parts.extend(after.get(paragraph_index, []))
            paragraph_index += 1
        for extra in sorted(index for index in after if index >= paragraph_index):
            parts.extend(after[extra])
        body = "\n".join(parts)
        files[f"OEBPS/text/{name}"] = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{language}">
<head><meta charset="utf-8"/><title>{chapter_title}</title>
<link rel="stylesheet" type="text/css" href="../style.css"/></head>
<body><h1>{chapter_title}</h1>
{body}
</body></html>"""
        manifest.append(f'<item id="c{index:04d}" href="text/{name}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="c{index:04d}"/>')
        nav.append(f'<li><a href="text/{name}">{chapter_title}</a></li>')
    toc_title = "目錄" if language.lower().replace("_", "-") in {"zh-tw", "zh-hant"} else "目录"
    files["OEBPS/nav.xhtml"] = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="{language}">
<head><meta charset="utf-8"/><title>{toc_title}</title></head>
<body><nav epub:type="toc" id="toc"><h1>{toc_title}</h1><ol>
{''.join(nav)}
</ol></nav></body></html>"""
    files["OEBPS/style.css"] = ("body{font-family:serif;line-height:1.8;margin:1.2em;}"
                                "h1{font-size:1.4em;margin:1.2em 0;}"
                                "p{text-indent:2em;margin:0.35em 0;}"
                                "p.image{text-align:center;text-indent:0;}"
                                "p.image img{max-width:100%;height:auto;}")
    cover_meta = ('<meta name="cover" content="cover-image"/>'
                  if cover_asset else "")
    files["OEBPS/content.opf"] = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="bookid">{identifier}</dc:identifier>
<dc:title>{html.escape(title)}</dc:title>
<dc:language>{language}</dc:language>
<meta property="dcterms:modified">{modified}</meta>
{cover_meta}
</metadata>
<manifest>
<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
<item id="css" href="style.css" media-type="text/css"/>
{''.join(asset_manifest)}
{''.join(manifest)}
</manifest>
<spine>
{''.join(spine)}
</spine>
</package>"""
    files["META-INF/container.xml"] = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip",
                         compress_type=zipfile.ZIP_STORED)
        for name in sorted(files):
            archive.writestr(name, files[name], compress_type=zipfile.ZIP_DEFLATED)
        for name, data in sorted(binary_files.items()):
            archive.writestr(name, data, compress_type=zipfile.ZIP_DEFLATED)
    return output


def verify_epub(path: str | Path) -> dict:
    """Structural check for an EPUB produced by :func:`build_epub`."""
    path = Path(path)
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if not names or names[0] != "mimetype":
            raise ValueError("mimetype must be the first ZIP entry")
        if archive.read("mimetype") != b"application/epub+zip":
            raise ValueError("invalid EPUB mimetype")
        container = ET.fromstring(archive.read("META-INF/container.xml"))
        rootfile = container.find(
            ".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile")
        if rootfile is None:
            raise ValueError("container has no rootfile")
        opf_path = rootfile.attrib["full-path"]
        opf = ET.fromstring(archive.read(opf_path))
        ns = {"opf": "http://www.idpf.org/2007/opf",
              "dc": "http://purl.org/dc/elements/1.1/"}
        title = opf.find(".//dc:title", ns)
        items = {item.attrib["id"]: item.attrib
                 for item in opf.findall(".//opf:manifest/opf:item", ns)}
        manifest = {key: item["href"] for key, item in items.items()}
        spine = [item.attrib["idref"]
                 for item in opf.findall(".//opf:spine/opf:itemref", ns)]
        if not spine or any(item not in manifest for item in spine):
            raise ValueError("invalid manifest/spine")
        base = opf_path.rsplit("/", 1)[0]
        total_chars, empty = 0, []
        image_files, missing_images, image_mismatches = [], [], []
        for item in spine:
            href = manifest[item]
            if not href.endswith((".xhtml", ".html")):
                continue
            name = f"{base}/{href}" if base else href
            body = ET.fromstring(archive.read(name).decode("utf-8"))
            text = "".join(body.itertext()).strip()
            total_chars += len(text)
            if len(text) < 20:
                empty.append(name)
        for key, item in items.items():
            if not (item.get("media-type") or "").startswith("image/"):
                continue
            name = f"{base}/{item['href']}" if base else item["href"]
            image_files.append(name)
            if name not in names or not archive.read(name):
                missing_images.append(name)
                continue
            detected = sniff_media_type(archive.read(name), name)
            if detected != item.get("media-type"):
                image_mismatches.append({"file": name,
                                         "declared": item.get("media-type"),
                                         "detected": detected})
        return {"title": title.text if title is not None else None,
                "zip_entries": len(names), "spine_items": len(spine),
                "chapter_files": sum(1 for item in spine
                                     if manifest[item].endswith((".xhtml", ".html"))),
                "total_chars": total_chars, "empty_files": empty,
                "image_count": len(image_files), "image_files": image_files,
                "missing_images": missing_images,
                "image_mismatches": image_mismatches,
                "size": path.stat().st_size}


def collect_epub_chapters(*, runner: TranslationRunner,
                          reference_chapters: Sequence[dict],
                          end: int | None = None,
                          reference_assets: Mapping[int, Mapping] | None = None) -> list[dict]:
    """Collect the main line in reading order, preferring accepted target text."""
    end = runner.main_end if end is None else end
    reference_assets = reference_assets or {}
    chapters = []
    for row in reference_chapters:
        chapter = dict(row)
        chapter.setdefault("assets", reference_assets.get(chapter.get("order")) or
                           {"inline": [], "block": []})
        chapters.append(chapter)
    for order in range(runner.continuation_start, end):
        source = runner.source_by_order.get(order)
        if source is None or _is_extra_title(source["raw_title"]):
            continue
        row = runner.store.chapter(order)
        if not row or row["status"] != "accepted":
            raise PipelineError("missing_accepted",
                                f"chapter {order} is not accepted")
        chapters.append({"title": row["title_zh"],
                         "paragraphs": row["text"].split("\n\n"),
                         "order": order, "source": "target",
                         "assets": row.get("assets") or {"inline": [], "block": []}})
    return chapters
