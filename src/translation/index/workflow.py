"""Hierarchical translation workflow: lazy reference index, ContextPlan and evidence-bound prompts.

This is the supported surface for the new architecture. Review/commit are
intentionally not hidden here; callers should keep those steps explicit or use
the legacy branch until the new review lifecycle is wired.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from ..budget import RoleBudget, plan_request
from ..schemas import GenerationRequest
from .artifacts import Artifact, InputRef
from .context_plan import (ContextDocument, batch_near_reserve,
                           build_context_plan)
from .lazy import plan_reference_blocks
from .levels import LevelConfig
from .prompts import source_message, source_records_rules
from .snapshot import IndexSnapshot


@dataclass(frozen=True)
class GlossaryTerm:
    source: str
    target: str
    kind: str = "other"


@dataclass
class TranslationWorkflow:
    project: object
    source_edition: str
    reference_edition: str
    levels: LevelConfig = field(default_factory=lambda: LevelConfig((10, 10), (3, 1)))
    estimate_factor: float = 1.15
    prompt_overhead: int = 2048
    full_chapters: int | None = None
    reference_summary_tokens: int | None = None
    reference_map: Mapping[int, int] | None = None

    def __post_init__(self):
        workflow = self.project.config.workflow
        if self.full_chapters is None:
            self.full_chapters = workflow.full_chapters
        if self.reference_summary_tokens is None:
            self.reference_summary_tokens = workflow.summary_tokens
        self.archive = self.project._archive([self.source_edition, self.reference_edition])
        self._source_docs = self.archive.documents(self.source_edition)
        self._reference_docs = self.archive.documents(self.reference_edition)
        self._reference_by_order = {doc["edition_order"]: doc
                                    for doc in self._reference_docs}
        self._reference_by_source_order = (
            {source_order: self._reference_by_order[reference_order]
             for source_order, reference_order in self.reference_map.items()
             if reference_order in self._reference_by_order}
            if self.reference_map is not None else self._reference_by_order)

    def reference_for(self, order: int):
        return self._reference_by_source_order.get(order)

    @property
    def capacity(self) -> int:
        return self.project.config.capacity.context_window_tokens

    def documents(self, *, start: int = 0, end: int | None = None,
                  with_reference: bool = True,
                  translations: Mapping[int, str] | None = None) -> dict[int, ContextDocument]:
        documents = {}
        for doc in self._source_docs:
            order = doc["edition_order"]
            if order < start or (end is not None and order >= end):
                continue
            reference = self._reference_by_source_order.get(order)
            translation = (translations or {}).get(order)
            if translation is None and with_reference and reference:
                translation = reference["text"]
            documents[order] = ContextDocument(
                source_id=doc["entry_id"], order=order,
                title=doc["raw_title"], source_text=doc["text"],
                translation_text=translation)
        return documents

    def reference_documents(self) -> dict[int, ContextDocument]:
        return {doc["edition_order"]: ContextDocument(
            source_id=doc["entry_id"], order=doc["edition_order"],
            title=doc["raw_title"], source_text=doc["text"])
            for doc in self._reference_docs}

    def reference_plan(self):
        return plan_reference_blocks(
            self.reference_documents(), self.levels, _ByteCounter(),
            capacity=self.capacity, factor=self.estimate_factor,
            prompt_overhead=self.prompt_overhead,
            estimate_version=f"reference:{self.reference_edition}")

    def summarize_reference(self, *, start: int = 0, end: int,
                            label: str = "reference") -> tuple[str, object]:
        text = "\n\n".join(
            f"### {doc['raw_title']}\n{doc['text']}"
            for doc in self._reference_docs
            if start <= doc["edition_order"] < end)
        messages = [
            {"role": "system", "content":
             "你是轻小说资料整理员。只输出中文摘要，覆盖人物、地点、事件、专有名词和未决线索；"
             "不得编造原文没有的内容。输出一段连续摘要，不要解释。"},
            {"role": "user", "content": f"【{label}】\n{text}"},
        ]
        result = self.project.provider.generate(GenerationRequest(
            messages, max_tokens=self.reference_summary_tokens,
            temperature=0, task="reference_summary"))
        return result.text, result

    def make_snapshot(self, summary: str, *, summary_start: int, summary_end: int,
                      full_start: int, full_end: int,
                      documents: Mapping[int, ContextDocument]) -> IndexSnapshot:
        source_ids = tuple(documents[order].source_id
                           for order in range(summary_start, summary_end)
                           if order in documents)
        block = Artifact(
            artifact_id="reference-summary", kind="block", level=2,
            source_ids=source_ids, status="accepted",
            inputs=(InputRef("reference", "lazy-plan", "summary"),),
            content={"summary": summary}, review_mode="rebuild_only",
            quality_labels=("rebuild_only", "reference"))
        artifacts = {block.artifact_id: block}
        for order in range(full_start, full_end):
            doc = documents.get(order)
            if doc is None:
                continue
            artifact = Artifact(
                artifact_id=f"reference-chapter-{order}", kind="chapter", level=0,
                source_ids=(doc.source_id,), status="accepted",
                inputs=(InputRef(doc.source_id, "reference", str(order)),),
                quality_labels=("reference",))
            artifacts[artifact.artifact_id] = artifact
        return IndexSnapshot(snapshot_id="translation-workflow", generation="hierarchical",
                             levels=self.levels, artifacts=artifacts)

    def assemble(self, *, snapshot: IndexSnapshot, documents: Mapping[int, ContextDocument],
                 target_order: int, batch_orders: Iterable[int],
                 glossary: Iterable[GlossaryTerm] = (), unresolved: Iterable[str] = (),
                 threads: Mapping | None = None,
                 output_tokens: int = 16384,
                 output_profile: str = "plain",
                 source_rows: Iterable[Mapping] | None = None,
                 provisional_orders: Iterable[int] = ()) -> tuple[list[dict], dict, object]:
        if output_profile not in {"plain", "source-records"}:
            raise ValueError("unknown output profile")
        batch_orders = tuple(batch_orders)
        target = documents[target_order]
        batch_ids = [documents[order].source_id for order in batch_orders]
        by_source = {doc.source_id: doc for doc in documents.values()}
        # Every chapter in a batch must select the same history blocks, or the
        # first differing summary invalidates the rest of the cached prefix.
        reserve = (batch_near_reserve(snapshot, by_source, batch_ids,
                                      estimator=_ByteCounter(),
                                      full_chapters=self.full_chapters)
                   if len(batch_ids) > 1 else None)
        plan = build_context_plan(snapshot, by_source, target.source_id, batch_ids,
                                  estimator=_ByteCounter(),
                                  max_tokens=self.capacity - output_tokens,
                                  full_chapters=self.full_chapters,
                                  selection_reserve_tokens=reserve)
        # Keep the caller's stable order. Re-sorting a growing glossary breaks
        # the cached prefix at the first newly inserted term.
        terms = list(glossary)
        cards = [_entity_card(index, term) for index, term in enumerate(terms, 1)]
        # Keep the cacheable prefix free of per-chapter values. The glossary and
        # fixed rules go first; target/batch selection and thread state go after
        # the reference context.
        stable_workset = {
            "core": [{"id": "CORE-REF", "value": "参考译文只作术语/风格候选，日文是事实依据。"}],
            "candidates": cards,
            "extraction_gaps": ["参考索引为 lazy planned blocks；仅使用已物化摘要或全文。"],
            "authority": "candidate_unreviewed",
        }
        dynamic_workset = {
            "unresolved": list(unresolved),
            "threads": dict(threads or {"active": [], "resolved": [], "dormant_relevant": []}),
            "selection": {"target_order": target_order,
                          "recent_chapters": self.full_chapters,
                          "batch_orders": list(batch_orders),
                          "provisional_target_orders": list(provisional_orders)},
        }
        if output_profile == "source-records":
            system = (
                "你是日译中轻小说翻译。参考资料（摘要、实体、线程、定向证据）均为未审候选；"
                "日文原文是事实依据。术语优先使用候选 target_form。按源段记录协议输出，"
                "不要解释、不要代码块。")
        else:
            system = (
                "你是日译中轻小说翻译。参考资料（摘要、实体、线程、定向证据）均为未审候选；"
                "日文原文是事实依据。术语优先使用候选 target_form。只输出正文，不输出标题、"
                "段落定位ID或解释。")
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content":
             "【冻结约定与稳定候选；参考译文不是事实】\n" +
             json.dumps(stable_workset, ensure_ascii=False, separators=(",", ":"))},
            {"role": "user", "content":
             "【参考索引与近期全文；参考译文不是目标已提交译文】"},
        ]
        rendered = plan.render_messages()
        messages.extend(message for message, segment in zip(rendered, plan.segments)
                        if segment.representation != "target_source")
        messages.append({"role": "user", "content":
             "【本章未决、线程与选择元数据；未审候选】\n" +
             json.dumps(dynamic_workset, ensure_ascii=False, separators=(",", ":"))})
        evidence = glossary_evidence(
            target.source_text, terms,
            [doc.translation_text for doc in documents.values()
             if doc.order < target_order and doc.translation_text])
        if evidence:
            messages.append({"role": "user", "content":
                             "【本章定向词条与局部证据；未审候选】\n" +
                             json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))})
        if output_profile == "source-records":
            rows = list(source_rows or ())
            if not rows or any(not str(row.get("text", "")).strip() for row in rows):
                raise ValueError("source_rows must contain the target chapter's nonempty paragraphs")
            aliases = [f"S{index:03d}" for index in range(1, len(rows) + 1)]
            messages.append({"role": "user", "content":
                             f"【当前目标 {target.title} 完整日文正文；只翻译本章】\n" +
                             "\n".join(f"[{alias}] {row['text']}"
                                       for alias, row in zip(aliases, rows))})
            messages.append({"role": "user", "content":
                             source_records_rules(aliases)})
        else:
            source = {"chapter_id": target.source_id, "match_key": target.title,
                      "title": target.title, "text": target.source_text,
                      "order": target.order, "language": "ja"}
            messages.append(source_message(source, list(unresolved), reference_profile="plain"))
            messages.append({"role": "user", "content":
                             "只输出最终中文译文正文；不要标题、不要段落ID、不要解释。"})
        report = plan_request(messages, self.project.config.capacity,
                              RoleBudget(output_tokens), self.project.counter)
        if not report["fits"]:
            # Optional evidence is the first thing to drop; mandatory history stays.
            messages = [message for message in messages
                        if "本章定向词条" not in message.get("content", "")]
            report = plan_request(messages, self.project.config.capacity,
                                  RoleBudget(output_tokens), self.project.counter)
        return messages, report, plan

    def translate(self, *, snapshot: IndexSnapshot,
                  documents: Mapping[int, ContextDocument], target_order: int,
                  batch_orders: Iterable[int], glossary: Iterable[GlossaryTerm] = (),
                  unresolved: Iterable[str] = (), threads: Mapping | None = None,
                  output_tokens: int = 16384, output_profile: str = "plain",
                  source_rows: Iterable[Mapping] | None = None,
                  provisional_orders: Iterable[int] = ()):
        messages, report, plan = self.assemble(
            snapshot=snapshot, documents=documents, target_order=target_order,
            batch_orders=batch_orders, glossary=glossary, unresolved=unresolved,
            threads=threads, output_tokens=output_tokens,
            output_profile=output_profile, source_rows=source_rows,
            provisional_orders=provisional_orders)
        result = self.project.provider.generate(GenerationRequest(
            messages, max_tokens=output_tokens, temperature=0,
            task=f"translate_{target_order}"))
        return result, report, plan


class _ByteCounter:
    def count(self, text):
        return len(text.encode("utf-8"))


def _entity_card(index: int, term: GlossaryTerm) -> dict:
    return {
        "id": f"E{index:02d}", "type": "ENTITY",
        "fields": {"name": [term.source], "kind": [term.kind],
                   "description": [f"参考译名候选：{term.target}"],
                   "target_form": [term.target], "ambiguity": []},
        "authority": "candidate_unreviewed",
        "semantic_status": "candidate_unreviewed",
        "prompt_projection": "entity-card-v1",
    }


def glossary_evidence(target_text: str, glossary: Iterable[GlossaryTerm],
                      reference_texts: Iterable[str] = ()) -> dict | None:
    terms = []
    for term in glossary:
        if term.source not in target_text:
            continue
        occurrences = [index for index, line in enumerate(target_text.splitlines(), 1)
                       if term.source in line]
        hits = [text for text in reference_texts if term.target in text]
        terms.append({"source_form": term.source,
                      "candidate_cards": [{"description": [f"参考译名候选：{term.target}"],
                                          "target_forms": [term.target],
                                          "semantic_status": "candidate_unreviewed"}],
                      "occurrences": occurrences[:8],
                      "historical_match_count": len(hits),
                      "historical_excerpts": hits[:2]})
    if not terms:
        return None
    return {"schema": "targeted-evidence-v1",
            "selection": "glossary_and_literal_reference_hits",
            "authority": "candidate_unreviewed", "terms": terms,
            "note": "检索不到不等于作品没有相关信息；日文原文是事实依据。"}
