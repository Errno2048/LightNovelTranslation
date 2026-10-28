"""Block materializer + DAG barriers for the hierarchical summary index.

Builds level 1 blocks from chapter prose (JA plus paired ZH when present)
and level 2 blocks direct-first with a level 1 + rollup fallback.  Blocks
persist in the TranslationStore as structured artifacts with
not_created/provisional/committed status.  Translation may consume
provisional blocks; review must ground on accepted facts while still seeing
the current level 1 provisional summary of its own block.
"""
from __future__ import annotations

import json
from typing import Iterable, Mapping, Sequence

from ..providers import ProviderError
from ..storage import digest
from .summary_index import (ChapterRef, SummaryBlock, coverage_gap,
                            plan_blocks, COMMITTED, NOT_CREATED, PROVISIONAL)
from .pipeline import PipelineError, TranslationRunner, TranslationStore

SUMMARY_SYSTEM = (
    "你为一本小说建立可追溯索引。仅依据本次提供的正文，不使用作品记忆。"
    "用中文概括；日文专名保留原文表记，不猜中文译名；中文对照仅作译名参考。"
    "读完整块，概括行动、参与者、起因和结果；保留叙事线与未知状态。"
    "概要以2500字为软目标；提取5至20个关键事件，最多64个实体。"
    "evidence_ref 只能填本次正文的段落标签。字段固定，最后独占一行@@END。\n"
    "@@OVERVIEW\ntext: 连贯概括\n"
    "@@EVENT e1\ntext: 事件\nevidence_ref: 段落标签\n"
    "@@ENTITY n1\nform: 名称\nkind: person|skill|place|organization|item|other\n"
    "description: 描述\nevidence_ref: 段落标签\n"
    "@@COVERAGE\nprocessed_all: yes\nomissions: 无\n@@END")

ROLLUP_SYSTEM = (
    "你是资料整理员。根据同一本书子块的摘要与实体/事件清单，生成父块的连贯概括。"
    "合并去重实体与事件，不引入子块之外的信息，不猜译名。"
    "概要以2500字为软目标。只输出概括正文，不要解释。")


def _paragraph_rows(chapters: Sequence[ChapterRef]) -> tuple[list[dict], dict]:
    rows = []
    refs = {}
    for chapter in chapters:
        paragraphs = [p for p in chapter.ja_text.split("\n\n") if p.strip()]
        for index, text in enumerate(paragraphs, 1):
            alias = f"J{chapter.order:04d}P{index:03d}"
            rows.append({"ref": alias, "ja": text,
                         "zh": None, "title": chapter.title, "order": chapter.order})
            refs[alias] = {"text": text}
        if chapter.zh_text:
            zh_paragraphs = [p for p in chapter.zh_text.split("\n\n") if p.strip()]
            for index, text in enumerate(zh_paragraphs[:len(paragraphs)], 1):
                alias = f"J{chapter.order:04d}P{index:03d}"
                for row in rows:
                    if row["ref"] == alias:
                        row["zh"] = text
    return rows, refs


def _parse_records(text: str) -> dict:
    from ..editions.protocol import flat
    parsed = flat(text, {"OVERVIEW", "EVENT", "ENTITY", "TEMPORAL", "KNOWLEDGE", "COVERAGE"})
    errors = list(parsed["errors"])
    overviews = [r for r in parsed["records"] if r["record"] == "OVERVIEW"]
    if len(overviews) != 1 or not overviews[0]["fields"].get("text"):
        errors.append("one_nonempty_overview_required")
        overview = ""
    else:
        overview = overviews[0]["fields"]["text"]
    coverages = [r for r in parsed["records"] if r["record"] == "COVERAGE"]
    coverage = coverages[0]["fields"] if coverages else {}
    if coverage.get("processed_all") not in {"yes", "no"}:
        errors.append("coverage_required")
    events, entities, temporal, knowledge = [], [], [], []
    for record in parsed["records"]:
        kind = record["record"]
        fields = record["fields"]
        if kind == "EVENT" and fields.get("text"):
            events.append(fields)
        elif kind == "ENTITY" and fields.get("form") and fields.get("kind"):
            entities.append(fields)
        elif kind == "TEMPORAL" and fields.get("text"):
            temporal.append(fields)
        elif kind == "KNOWLEDGE" and fields.get("text"):
            knowledge.append(fields)
    return {"overview": overview, "events": events, "entities": entities,
            "temporal_claims": temporal, "knowledge_claims": knowledge,
            "coverage": coverage, "errors": errors}


def _summary_messages(rows: Sequence[Mapping], *, paired: bool) -> list[dict]:
    documents = []
    for row in rows:
        item = {"ref": row["ref"], "ja": row["ja"]}
        if paired and row.get("zh"):
            item["zh"] = row["zh"]
        documents.append(item)
    payload = json.dumps({"documents": documents},
                         ensure_ascii=False, separators=(",", ":"))
    return [{"role": "system", "content": SUMMARY_SYSTEM},
            {"role": "user",
             "content": payload + "\n正文结束。只对本块提取，并以@@COVERAGE和@@END结束。"}]


class _DeferredSave:
    """Collects worker-side block updates; the main thread flushes them."""

    def __init__(self):
        self.pending: list[SummaryBlock] = []

    def save(self, block: SummaryBlock) -> None:
        self.pending.append(block)


class SummaryIndex:
    """Persistent hierarchical index bound to one TranslationStore."""

    def __init__(self, runner: TranslationRunner, chapters: Sequence[ChapterRef], *,
                 spec=None):
        import threading
        self.runner = runner
        self.store = runner.store
        self.chapters = {chapter.order: chapter for chapter in chapters}
        self.spec = spec or runner.project.config.index
        capacity = runner.project.config.capacity
        self.level1_capacity = (capacity.context_window_tokens
                                - runner.summary_tokens
                                - capacity.safety_tokens - 8192)
        self.blocks = plan_blocks(chapters, self.spec, None, None,
                                  level1_capacity=self.level1_capacity)
        self._tls = threading.local()
        self._deferred_events: list[dict] = []
        self._ensure_table()

    @property
    def _saver(self):
        return getattr(self._tls, "saver", None)

    @_saver.setter
    def _saver(self, value):
        self._tls.saver = value

    def _ensure_table(self) -> None:
        with self.store.db:
            self.store.db.execute(
                "CREATE TABLE IF NOT EXISTS summary_blocks ("
                "block_id TEXT PRIMARY KEY, level INTEGER, start_order INTEGER,"
                "end_order INTEGER, children TEXT, status TEXT,"
                "build_method TEXT, summary TEXT, content TEXT,"
                "generation INTEGER, paired INTEGER, updated_at TEXT)")
            for block in self.blocks.values():
                row = block.to_row()
                existing = self.store.db.execute(
                    "SELECT status,summary,content,generation FROM summary_blocks"
                    " WHERE block_id=?", (block.block_id,)).fetchone()
                if existing is None:
                    self.store.db.execute(
                        "INSERT INTO summary_blocks(block_id,level,start_order,"
                        "end_order,children,status,build_method,summary,content,"
                        "generation,paired,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
                        (row["block_id"], row["level"], row["start"], row["end"],
                         row["children"], row["status"], row["build_method"],
                         row["summary"], row["content"], row["generation"],
                         row["paired"]))
                else:
                    block.status = existing["status"]
                    block.summary = existing["summary"] or ""
                    block.content = json.loads(existing["content"] or "{}")
                    block.generation = existing["generation"]

    def _save(self, block: SummaryBlock) -> None:
        if self._saver is not None:
            self._saver.save(block)
            return
        self._flush_block(block)

    def _flush_block(self, block: SummaryBlock) -> None:
        row = block.to_row()
        with self.store.db:
            self.store.db.execute(
                "INSERT OR REPLACE INTO summary_blocks(block_id,level,start_order,"
                "end_order,children,status,build_method,summary,content,generation,"
                "paired,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
                (row["block_id"], row["level"], row["start"], row["end"],
                 row["children"], row["status"], row["build_method"],
                 row["summary"], row["content"], row["generation"], row["paired"]))

    def _event(self, **payload) -> None:
        if self._saver is not None:
            self._deferred_events.append(payload)
            return
        self.store.event("index_block", **payload)

    def flush(self) -> None:
        """Persist deferred block saves/events; call from the main thread."""
        if self._saver is None:
            return
        for block in self._saver.pending:
            self._flush_block(block)
        for payload in self._deferred_events:
            self.store.event("index_block", **payload)
        self._saver.pending.clear()
        self._deferred_events.clear()

    def _block_for(self, order: int, level: int) -> SummaryBlock | None:
        for block in self.blocks.values():
            if block.level == level and block.start <= order < block.end:
                return block
        return None

    def _children_chapters(self, block: SummaryBlock) -> list[ChapterRef]:
        orders = sorted(int(child[1:]) for child in block.children)
        return [self.chapters[order] for order in orders]

    def _generate(self, messages: list[dict], task: str, output_tokens: int) -> str:
        usages: list = []
        result = self.runner._generate(messages, output_tokens, task,
                                       log_event=False, usage_sink=usages)
        for usage in usages:
            self.store.event("model", **usage)
        return result.text

    def build_level1(self, order_or_block, *, committed: bool = False) -> dict:
        block = (order_or_block if isinstance(order_or_block, SummaryBlock)
                 else self._block_for(order_or_block, 1))
        if block is None:
            raise PipelineError("no_block", f"no level 1 block covers {order_or_block}")
        chapters = self._children_chapters(block)
        rows, _refs = _paragraph_rows(chapters)
        paired = all(c.zh_text is not None for c in chapters)
        messages = _summary_messages(rows, paired=paired)
        text = self._generate(messages, f"index_l1_{block.start}_{block.end}",
                              self.runner.summary_tokens)
        parsed = _parse_records(text)
        if parsed["errors"]:
            raise PipelineError("summary_protocol",
                                "level 1 summary output is invalid",
                                details={"block": block.block_id,
                                         "errors": parsed["errors"]})
        block.summary = parsed["overview"]
        block.content = parsed
        block.build_method = "direct"
        block.generation += 1
        block.status = COMMITTED if committed else PROVISIONAL
        self._save(block)
        self._event(order=None, start=block.start, end=block.end,
                    level=1, status=block.status,
                    method="direct", paired=paired,
                    events=len(parsed["events"]),
                    entities=len(parsed["entities"]))
        return {"block_id": block.block_id, "status": block.status,
                "overview_chars": len(block.summary)}

    def build_level2(self, parent: SummaryBlock, *, method: str | None = None) -> dict:
        method = method or self.spec.parent_build_mode
        chapters = [self.chapters[int(child[1:])]
                    for l1_id in parent.children
                    for child in self.blocks[l1_id].children]
        direct = _paragraph_rows(chapters)
        paired = all(c.zh_text is not None for c in chapters)
        if method in {"direct", "direct_then_rollup"}:
            try:
                messages = _summary_messages(direct[0], paired=paired)
                text = self._generate(messages,
                                      f"index_l2_{parent.start}_{parent.end}",
                                      self.runner.summary_tokens)
                parsed = _parse_records(text)
                if parsed["errors"]:
                    raise PipelineError("summary_protocol",
                                        "level 2 summary output is invalid",
                                        details={"block": parent.block_id,
                                                 "errors": parsed["errors"]})
                parent.summary = parsed["overview"]
                parent.content = parsed
                parent.build_method = "direct"
                parent.generation += 1
                parent.status = PROVISIONAL
                self._save(parent)
                self._event(start=parent.start, end=parent.end,
                            level=2, status=PROVISIONAL, method="direct",
                            paired=paired, events=len(parsed["events"]),
                            entities=len(parsed["entities"]))
                return {"block_id": parent.block_id, "status": PROVISIONAL,
                        "method": "direct"}
            except PipelineError as exc:
                if exc.code != "context_budget":
                    raise
                if method == "direct":
                    raise
        # rollup fallback
        for l1_id in parent.children:
            child = self.blocks[l1_id]
            if child.status == NOT_CREATED:
                self.build_level1(child)
        payload = {"children": []}
        for l1_id in parent.children:
            child = self.blocks[l1_id]
            payload["children"].append({
                "node_id": l1_id, "overview": child.summary,
                "events": child.content.get("events", []),
                "entities": child.content.get("entities", [])})
        messages = [{"role": "system", "content": ROLLUP_SYSTEM},
                    {"role": "user", "content": json.dumps(
                        payload, ensure_ascii=False, separators=(",", ":"))}]
        text = self._generate(messages, f"index_l2_rollup_{parent.start}_{parent.end}",
                              self.runner.summary_tokens)
        parsed = _parse_records("@@OVERVIEW\ntext: " + text + "\n"
                                "@@COVERAGE\nprocessed_all: yes\nomissions: 无\n@@END")
        parent.summary = parsed["overview"]
        parent.content = parsed
        parent.build_method = "child_summary_rollup"
        parent.generation += 1
        parent.status = PROVISIONAL
        self._save(parent)
        self._event(start=parent.start, end=parent.end,
                    level=2, status=PROVISIONAL,
                    method="child_summary_rollup", paired=paired,
                    events=len(parsed["events"]),
                    entities=len(parsed["entities"]))
        return {"block_id": parent.block_id, "status": PROVISIONAL,
                "method": "child_summary_rollup"}

    def commit_block(self, block: SummaryBlock, *, accepted_orders: set[int]) -> dict:
        """Commit a built block once every chapter it covers is accepted."""
        missing = [int(child[1:]) for child in block.children
                   if int(child[1:]) not in accepted_orders]
        if missing:
            return {"block_id": block.block_id, "status": block.status,
                    "missing_accepted": missing}
        if not block.summary:
            return {"block_id": block.block_id, "status": block.status,
                    "error": "not_built"}
        block.status = COMMITTED
        self._save(block)
        self.store.event("index_block_commit", start=block.start, end=block.end,
                         level=block.level)
        return {"block_id": block.block_id, "status": COMMITTED}

    def context_blocks(self, target_order: int, *, num_level1: int = 3,
                       allowed: Iterable[str] = (PROVISIONAL, COMMITTED),
                       full_start: int | None = None) -> dict:
        """Select summary blocks for a translation/review context prefix.

        The full-text window is aligned down to a level 1 boundary, and any
        level 2 parent overlapping the level 1 window still contributes:
        chapters it covers outside the level 1 selection are only available
        through that parent's summary.
        """
        allowed = set(allowed)
        lower = full_start if full_start is not None else target_order
        boundary = None
        for block in sorted((b for b in self.blocks.values() if b.level == 1),
                            key=lambda b: b.start, reverse=True):
            if block.end <= lower:
                boundary = block.end
                break
        if boundary is not None and lower > boundary:
            lower = boundary
        level1 = sorted((b for b in self.blocks.values()
                         if b.level == 1 and b.end <= lower
                         and b.status in allowed),
                        key=lambda b: b.start)[-num_level1:]
        level1_start = level1[0].start if level1 else lower
        level2 = sorted((b for b in self.blocks.values()
                         if b.level == 2 and b.end <= level1_start
                         and b.status in allowed),
                        key=lambda b: b.start)
        overlapping = [b for b in self.blocks.values()
                       if b.level == 2 and b.status in allowed
                       and b.start < level1_start < b.end]
        return {"level1": level1, "level2": level2,
                "level2_overlapping": overlapping,
                "full_start": lower,
                "level1_ranges": [(b.start, b.end) for b in level1],
                "gaps": coverage_gap(
                    {b.block_id: b for b in level1 + level2 + overlapping},
                    0, target_order)}

    def translation_barrier(self, target_order: int, *, strict: bool) -> list[tuple[int, int]]:
        """Return gaps that must be filled before translating target_order."""
        allowed = (COMMITTED,) if strict else (PROVISIONAL, COMMITTED)
        full_start = max(0, target_order - self.runner.history_chapters)
        selection = self.context_blocks(target_order, allowed=allowed,
                                        full_start=full_start)
        return selection["gaps"]

    def review_barrier(self, target_order: int, *, strict: bool) -> list[tuple[int, int]]:
        """Gap check for review. Currently identical to translation_barrier:
        with strict=False it also accepts PROVISIONAL coverage, although the
        design intent is that review grounds on accepted (COMMITTED) facts and
        may only read the current block's provisional summary. Reconcile this
        before wiring the index into the runner (TODO)."""
        return self.translation_barrier(target_order, strict=strict)
