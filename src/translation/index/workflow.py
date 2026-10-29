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
from .prompts import (SOURCE_RECORDS_RULES, source_message,
                      source_records_alias_map)
from .snapshot import IndexSnapshot


@dataclass(frozen=True)
class GlossaryTerm:
    source: str
    target: str
    kind: str = "other"
    confidence: int = 2
    aliases: tuple[str, ...] = ()
    locked: bool = False
    provisional: bool = False
    entity_id: int | None = None
    form_type: str = "other"


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
        # Level selection counts follow the workflow config: level 1 keeps
        # the most recent N blocks; level 2 includes every earlier parent
        # unless a numeric cap is configured.
        n_l1 = int(getattr(workflow, "context_num_level1", 3) or 3)
        n_l2_cfg = getattr(workflow, "context_num_level2", "all")
        n_l2 = n_l1 if n_l2_cfg == "all" else int(n_l2_cfg)
        self.levels = LevelConfig(self.levels.max_children,
                                  (n_l1, n_l2))
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
                 glossary: Iterable[GlossaryTerm] = (),
                 stable_glossary: Iterable[GlossaryTerm] = (),
                 unresolved: Iterable[str] = (),
                 threads: Mapping | None = None,
                 output_tokens: int = 16384,
                 output_profile: str = "plain",
                 source_rows: Iterable[Mapping] | None = None,
                 provisional_orders: Iterable[int] = (),
                 evidence_excerpt_chars: int = 120,
                 evidence_max_excerpts: int = 2,
                 evidence_max_chars: int = 12000,
                 style_profile=None,
                 ) -> tuple[list[dict], dict, object]:
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
        # The stable core is an append-only projection. Batch-only additions go
        # in a later message so a new term never moves the reusable prefix.
        stable_terms = list(stable_glossary)
        stable_sources = {term.source for term in stable_terms}
        batch_terms = [term for term in glossary
                       if term.source not in stable_sources]
        terms = stable_terms + batch_terms
        cards = [_entity_card(index, term) for index, term in enumerate(terms, 1)]
        stable_cards = cards[:len(stable_terms)]
        batch_cards = cards[len(stable_terms):]
        stable_workset = {
            "core": [{"id": "CORE-REF", "value": "参考译文只作术语/风格候选，日文是事实依据。"}],
            "extraction_gaps": ["参考索引为 lazy planned blocks；仅使用已物化摘要或全文。"],
            "authority": "candidate_unreviewed",
            "candidates": stable_cards,
        }
        batch_workset = {
            "scope": "batch_additions",
            "authority": "candidate_unreviewed",
            "candidates": batch_cards,
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
                "日文原文是事实依据。实体卡中 confidence=1、locked 或 provisional 的 "
                "target_form 必须使用；其余术语为候选。按源段记录协议输出，不要解释、不要代码块。")
        else:
            system = (
                "你是日译中轻小说翻译。参考资料（摘要、实体、线程、定向证据）均为未审候选；"
                "日文原文是事实依据。实体卡中 confidence=1、locked 或 provisional 的 "
                "target_form 必须使用；其余术语为候选。只输出正文，不输出标题、段落定位ID或解释。")
        # L0 first, then run-stable, then batch-stable, then chapter-specific.
        # Do not put a per-chapter value before a block with a longer lifetime.
        messages = [{"role": "system", "content": system}]
        if output_profile == "source-records":
            messages.append({"role": "user", "content": SOURCE_RECORDS_RULES})
        messages.extend([
            {"role": "user", "content":
             "【冻结约定与稳定候选；参考译文不是事实】\n" +
             json.dumps(stable_workset, ensure_ascii=False, separators=(",", ":"))},
            {"role": "user", "content":
             "【参考索引与近期全文；参考译文不是目标已提交译文】"},
        ])
        if style_profile is not None:
            messages.append({"role": "user", "content": style_profile.card()})
        rendered = plan.render_messages()
        stable_summaries = []
        provisional_summaries = []
        full_messages = []
        batch_sources = []
        for message, segment in zip(rendered, plan.segments):
            if segment.representation == "target_source":
                continue
            if segment.representation == "summary":
                target_list = (provisional_summaries
                               if segment.status == "provisional"
                               else stable_summaries)
                target_list.append(message)
            elif segment.representation == "full":
                full_messages.append(message)
            elif segment.representation == "batch_source":
                batch_sources.append(message)
        messages.extend(stable_summaries)
        if batch_cards:
            messages.append({"role": "user", "content":
                             "【本批次新增术语候选；未审】\n" +
                             json.dumps(batch_workset, ensure_ascii=False,
                                        separators=(",", ":"))})
        messages.extend(provisional_summaries)
        messages.extend(full_messages)
        messages.extend(batch_sources)
        messages.append({"role": "user", "content":
             "【本章未决、线程与选择元数据；未审候选】\n" +
             json.dumps(dynamic_workset, ensure_ascii=False, separators=(",", ":"))})
        near_orders = {segment.start for segment in plan.segments
                       if segment.representation == "full"}
        history_docs = [doc for doc in documents.values()
                        if doc.order < target_order and doc.translation_text]
        evidence = glossary_evidence(
            target.source_text, terms, history_docs,
            near_orders=near_orders,
            excerpt_chars=evidence_excerpt_chars,
            max_excerpts=evidence_max_excerpts,
            max_chars=evidence_max_chars)
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
                             source_records_alias_map(aliases)})
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
                  stable_glossary: Iterable[GlossaryTerm] = (),
                  unresolved: Iterable[str] = (), threads: Mapping | None = None,
                  output_tokens: int = 16384, output_profile: str = "plain",
                  source_rows: Iterable[Mapping] | None = None,
                  provisional_orders: Iterable[int] = (),
                  evidence_excerpt_chars: int = 120,
                  evidence_max_excerpts: int = 2,
                  evidence_max_chars: int = 12000,
                  style_profile=None):
        messages, report, plan = self.assemble(
            snapshot=snapshot, documents=documents, target_order=target_order,
            batch_orders=batch_orders, glossary=glossary,
            stable_glossary=stable_glossary,
            unresolved=unresolved, threads=threads, output_tokens=output_tokens,
            output_profile=output_profile, source_rows=source_rows,
            provisional_orders=provisional_orders,
            evidence_excerpt_chars=evidence_excerpt_chars,
            evidence_max_excerpts=evidence_max_excerpts,
            evidence_max_chars=evidence_max_chars,
            style_profile=style_profile)
        result = self.project.provider.generate(GenerationRequest(
            messages, max_tokens=output_tokens, temperature=0,
            task=f"translate_{target_order}"))
        return result, report, plan

    def assemble_backfill(self, *, target_order: int, target_title: str,
                          before_texts: Iterable[tuple[int, str, str]] = (),
                          after_texts: Iterable[tuple[int, str, str]] = (),
                          glossary: Iterable[GlossaryTerm] = (),
                          stable_glossary: Iterable[GlossaryTerm] = (),
                          reference_summary: str = "",
                          target_summaries: Iterable[str] = (),
                          unresolved: Iterable[str] = (),
                          threads: Mapping | None = None,
                          output_tokens: int = 16384,
                          source_rows: Iterable[Mapping] | None = None,
                          style_profile=None,
                          region_label: str = "") -> tuple[list[dict], dict]:
        """Build a cache-friendly backfill prompt.

        Layer A (system/glossary/summary/style) and Layer B (region before and
        after) are identical for every gap in a region.  Per-gap metadata and
        the target source are appended last.
        """
        stable_terms = list(stable_glossary)
        stable_sources = {term.source for term in stable_terms}
        batch_terms = [term for term in glossary
                       if term.source not in stable_sources]
        cards = [_entity_card(index, term)
                 for index, term in enumerate(stable_terms + batch_terms, 1)]
        stable_cards = cards[:len(stable_terms)]
        batch_cards = cards[len(stable_terms):]
        stable_workset = {
            "core": [{"id": "CORE-REF",
                      "value": "参考译文只作术语/风格候选，日文是事实依据。"}],
            "extraction_gaps": ["补译缺口没有参考译文；使用前后参考章节作为上下文。"],
            "authority": "candidate_unreviewed",
            "candidates": stable_cards,
        }
        system = (
            "你是日译中轻小说翻译。参考资料（摘要、实体、线程、定向证据）均为未审候选；"
            "日文原文是事实依据。实体卡中 confidence=1、locked 或 provisional 的 "
            "target_form 必须使用；其余术语为候选。按源段记录协议输出，不要解释、不要代码块。")
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": SOURCE_RECORDS_RULES},
            {"role": "user", "content":
             "【冻结约定与稳定候选；参考译文不是事实】\n" +
             json.dumps(stable_workset, ensure_ascii=False,
                        separators=(",", ":"))},
            {"role": "user", "content":
             "【补译区域上下文；位置来自日文源顺序】"},
        ]
        if style_profile is not None:
            messages.append({"role": "user", "content": style_profile.card()})
        if reference_summary:
            messages.append({"role": "user", "content":
                             "【全书参考摘要；未审候选】\n" + reference_summary})
        for summary in target_summaries:
            messages.append({"role": "user", "content":
                             "【已接受摘要；未审候选】\n" + str(summary)})
        if batch_cards:
            messages.append({"role": "user", "content":
                             "【本批次新增术语候选；未审】\n" +
                             json.dumps({"scope": "batch_additions",
                                         "candidates": batch_cards},
                                        ensure_ascii=False,
                                        separators=(",", ":"))})
        if region_label:
            messages.append({"role": "user", "content":
                             f"【补译区域】{region_label}"})
        for label, rows in (("缺口前文参考", before_texts),
                            ("缺口后文参考", after_texts)):
            for order, title, text in rows:
                messages.append({"role": "user", "content":
                                 f"【{label}；order {order}】{title}\n{text}"})
        dynamic = {
            "unresolved": list(unresolved),
            "threads": dict(threads or {"active": [], "resolved": [],
                                        "dormant_relevant": []}),
            "selection": {"target_order": target_order,
                          "mode": "backfill"},
        }
        messages.append({"role": "user", "content":
                         "【本章未决、线程与选择元数据；未审候选】\n" +
                         json.dumps(dynamic, ensure_ascii=False,
                                    separators=(",", ":"))})
        rows = list(source_rows or ())
        if not rows or any(not str(row.get("text", "")).strip() for row in rows):
            raise ValueError("source_rows must contain nonempty target paragraphs")
        aliases = [f"S{index:03d}" for index in range(1, len(rows) + 1)]
        messages.append({"role": "user", "content":
                         f"【当前目标 {target_title} 完整日文正文；只翻译本章】\n" +
                         "\n".join(f"[{alias}] {row['text']}"
                                   for alias, row in zip(aliases, rows))})
        messages.append({"role": "user", "content":
                         source_records_alias_map(aliases)})
        report = plan_request(messages, self.project.config.capacity,
                              RoleBudget(output_tokens), self.project.counter)
        return messages, report


class _ByteCounter:
    def count(self, text):
        return len(text.encode("utf-8"))


def _term_authority(term: GlossaryTerm) -> str:
    if term.locked:
        return "locked_canonical"
    if term.provisional:
        return "provisional_canonical"
    if term.confidence == 1:
        return "fixed_canonical"
    return "candidate_unreviewed"


def _entity_card(index: int, term: GlossaryTerm) -> dict:
    authority = _term_authority(term)
    return {
        "id": f"E{index:02d}", "type": "ENTITY",
        "fields": {"name": [term.source], "kind": [term.kind],
                   "description": [f"参考译名候选：{term.target}"],
                   "target_form": [term.target],
                   "confidence": [term.confidence],
                   "aliases": list(term.aliases),
                   "locked": term.locked,
                   "provisional": term.provisional,
                   "entity_id": term.entity_id,
                   "form_type": term.form_type,
                   "ambiguity": []},
        "authority": authority,
        "semantic_status": authority,
        "prompt_projection": "entity-card-v1",
    }


def _doc_value(doc, name, default=None):
    if isinstance(doc, Mapping):
        return doc.get(name, default)
    return getattr(doc, name, default)


def _excerpt(text: str, index: int, length: int, radius: int) -> str:
    start = max(0, index - radius)
    end = min(len(text), index + length + radius)
    value = text[start:end].replace("\r", " ").replace("\n", " ").strip()
    if start > 0:
        value = "…" + value
    if end < len(text):
        value = value + "…"
    return value


def glossary_evidence(target_text: str, glossary: Iterable[GlossaryTerm],
                      history=(), *, near_orders: Iterable[int] = (),
                      excerpt_chars: int = 120, max_excerpts: int = 2,
                      max_chars: int = 12000) -> dict | None:
    """Return bounded, source-aligned usage snippets for target terms.

    Full historical chapters made this block dominate the prompt and still
    matched the wrong entity whenever the target string was a common substring.
    A term now needs both its Japanese source and one of its target/alias forms
    in the same historical chapter; only a short window around that usage is
    emitted. Documents already present as recent full context are skipped.
    """
    if max_chars <= 0 or max_excerpts <= 0:
        return None
    near = {int(order) for order in near_orders}
    prepared = []
    for term_index, term in enumerate(glossary):
        if not term.source or not term.target:
            continue
        if term.source not in target_text:
            continue
        forms = list(dict.fromkeys(
            form for form in (term.target, *term.aliases) if form))
        if not forms:
            continue
        candidates = []
        for doc in history:
            order = _doc_value(doc, "order")
            if order is None or int(order) in near:
                continue
            source_text = _doc_value(doc, "source_text") or ""
            if term.source not in source_text:
                continue
            translation = _doc_value(doc, "translation_text") or ""
            if not translation:
                continue
            found = None
            for form in forms:
                index = translation.find(form)
                if index >= 0:
                    found = (form, index)
                    break
            if found is None:
                continue
            candidates.append({
                "order": int(order),
                "title": str(_doc_value(doc, "title") or ""),
                "form": found[0],
                "text": _excerpt(translation, found[1], len(found[0]),
                                 max(0, int(excerpt_chars))),
            })
        if not candidates:
            continue
        # Prefer recent usage, then keep a stable glossary-order output.
        candidates.sort(key=lambda item: -item["order"])
        excerpts = []
        seen = set()
        for candidate in candidates:
            if candidate["text"] in seen:
                continue
            seen.add(candidate["text"])
            excerpts.append(candidate)
            if len(excerpts) >= max_excerpts:
                break
        if not excerpts:
            continue
        canonical = (term.locked or term.provisional or
                     int(term.confidence) == 1)
        kind = str(term.kind or "other")
        priority = (0 if canonical else 1,
                    0 if kind not in {"other"} else 1,
                    term_index)
        prepared.append((priority, term_index, term,
                         len(candidates), excerpts))
    if not prepared:
        return None

    prepared.sort(key=lambda item: item[0])
    selected = {}
    used = 0
    omitted_terms = 0
    truncated = False
    for _, term_index, term, match_count, excerpts in prepared:
        first = excerpts[0]
        cost = len(first["text"])
        if used + cost > max_chars:
            omitted_terms += 1
            truncated = True
            continue
        # Keep one excerpt for every selected term before spending bytes on a
        # second usage; this avoids losing a whole term to the global cap.
        selected[term_index] = {
            "source_form": term.source,
            "target": term.target,
            "confidence": int(term.confidence),
            "locked": bool(term.locked),
            "provisional": bool(term.provisional),
            "historical_match_count": match_count,
            "historical_excerpts": [{
                "order": first["order"], "text": first["text"]}],
        }
        used += cost
    for _, term_index, _, _, excerpts in prepared:
        entry = selected.get(term_index)
        if entry is None:
            continue
        for extra in excerpts[1:]:
            cost = len(extra["text"])
            if used + cost > max_chars:
                truncated = True
                break
            entry["historical_excerpts"].append({
                "order": extra["order"], "text": extra["text"]})
            used += cost
    if not selected:
        return None
    terms = [selected[index] for index in sorted(selected)]
    return {
        "schema": "targeted-evidence-v2",
        "selection": "source_aligned_bounded_snippets",
        "authority": "candidate_unreviewed",
        "excerpt_chars": int(excerpt_chars),
        "max_chars": int(max_chars),
        "truncated": truncated,
        "omitted_terms": omitted_terms,
        "terms": terms,
        "note": "证据为抽样局部片段；未列出的术语不等于无历史用法，日文原文是事实依据。",
    }
