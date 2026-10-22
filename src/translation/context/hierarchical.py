"""Compile complete three-tier context with no hidden calls or silent truncation."""
from dataclasses import asdict
import json

from ..narrative.schemas import ContextSpec, NarrativeError
from ..narrative.service import dependency
from ..storage import digest
from ..workflow import SYSTEM

PROMPT_VERSION = "hierarchical-context"


def _validate_examples(view, examples):
    for example in examples:
        if example.get("status") not in {"reviewed_prefix", "reviewed_range"}:
            raise NarrativeError("quality_hold", "few-shot example lacks an explicit review status")
        if not example.get("review", {}).get("purposes", {}).get("few_shot"):
            raise NarrativeError("quality_hold", "few-shot use is not authorized by the review")
        evidence = example.get("review", {}).get("evidence", [])
        if len(evidence) != 2 or {e.get("language") for e in evidence} != {"ja", "zh"}:
            raise NarrativeError("quality_hold", "example needs both Japanese and Chinese evidence")
        if example["review"].get("decision") != "accepted":
            raise NarrativeError("quality_hold", "example review is not accepted")
        view.validate_source_dependencies(example.get("source_dependencies"), derived_bilingual=True)
        for e in evidence:
            chapter = view.archive.chapters[e["chapter_id"]]
            view.check(chapter, translation=chapter["language"] == "zh")
            from ..knowledge.schemas import QueryPolicy
            view.archive.validate_evidence(e, QueryPolicy("single_gap"))
            name = "source_text" if e["language"] == "ja" else "translation_text"
            if example.get(name) != e["text"]:
                raise NarrativeError("stale_summary", "example text differs from reviewed evidence")
        if len({e["source_id"] for e in evidence}) != 1:
            raise NarrativeError("invalid_record", "example evidence has different chapter mappings")
        if not {e["source_id"] for e in evidence} <= set(example["source_dependencies"]):
            raise NarrativeError("invalid_record", "example dependency closure omits evidence")


def validate_knowledge(view, knowledge):
    if knowledge.get("prompt_blocks") and knowledge.get("fingerprint") != digest(dict(knowledge, fingerprint="")):
        raise NarrativeError("stale_summary", "P4 knowledge bundle identity differs")
    records = {r["id"]: r for r in knowledge.get("records", [])}
    selected_ids = set()
    for item in knowledge.get("selected_evidence", []):
        selected_ids.add(item["item_id"])
        view.validate_source_dependencies(item.get("source_dependencies"), derived_bilingual=True)
        for e in item.get("evidence", []):
            c = view.archive.chapters[e["chapter_id"]]
            view.check(c, translation=c["language"] == "zh")
            from ..knowledge.schemas import QueryPolicy
            view.archive.validate_evidence(e, QueryPolicy("single_gap"))
        if not item.get("evidence") or not {e["source_id"] for e in item["evidence"]} <= set(item["source_dependencies"]):
            raise NarrativeError("invalid_record", "knowledge dependency closure omits evidence")
    if set(records) != selected_ids:
        raise NarrativeError("invalid_record", "knowledge records and selected evidence differ")
    for item in knowledge.get("selected_evidence", []):
        record = records[item["item_id"]]
        if record.get("status") not in {"accepted", "frozen"} or record.get("evidence") != item["evidence"] or record.get("source_dependencies") != item["source_dependencies"]:
            raise NarrativeError("quality_hold", "knowledge record lacks matching accepted provenance")
        for parent, revision in record.get("knowledge_dependency_revisions", {}).items():
            if parent not in records or records[parent].get("revision_id") != revision:
                raise NarrativeError("stale_summary", "knowledge parent revision missing from dependency closure")


def compile_hierarchical_context(view, index_id, summaries, *, spec=None, knowledge=None, examples=(),
                                 translation_metadata=None, metadata_core_forms=()):
    spec = spec or ContextSpec()
    if view.policy.target_id is None:
        raise NarrativeError("unsupported_policy", "translation requires a target-bound view")
    summary_view = view.summary_view() if hasattr(view, "summary_view") else view
    index = summary_view.list_blocks(index_id)
    if spec.include_summaries:
        from ..narrative.summaries import check_boundaries
        check_boundaries(view, index)
    target = view.body(view.policy.target_id, task=True)
    _validate_examples(view, examples)
    # Distance counts reading positions, including reserved/missing translations; no backfilling.
    preceding = [c for c in view.sources if c["order"] < target["order"]]
    requested_ids = [c["chapter_id"] for c in preceding[-spec.n_chapter:]] if spec.n_chapter else []
    near_chapters, excluded = [], []
    for cid in requested_ids:
        if cid in view.policy.reserved_source_ids:
            excluded.append({"chapter_id": cid, "reason": "reserved_source"})
        else:
            near_chapters.extend(view.read_chapters([cid]))
    full_ids = {p["source"]["chapter_id"] for p in near_chapters}
    blocks, deps, selected, omitted = [], [], [], []
    blocks.append({"kind": "rules", "text": "以下资料仅用于理解前情与保持译法。摘要是有出处但可能有遗漏的导航，不是逐字翻译依据。"
                   "保留原文的不确定性，不根据前情补写身份解释或替无标记对白指定说话人。仅翻译最后的目标正文。"})
    if index["policy_exclusions"]:
        blocks.append({"kind": "rules", "text": "前情资料中有按实验政策隐藏的章节，覆盖仅限允许范围，不表示剧情连续完整。不要推断缺口中的事件。"})
    knowledge = knowledge or {"prompt_blocks": [], "selected_evidence": [], "fingerprint": None}
    validate_knowledge(view, knowledge)
    for block in knowledge.get("prompt_blocks", []):
        if block["kind"] in {"rules", "conventions", "style", "entity"}:
            blocks.append({"kind": "global_knowledge", "text": block["text"]})
    for example in examples:
        blocks.append({"kind": "few_shot", "id": example.get("id"),
                       "text": "【原文片段】\n" + example["source_text"] + "\n【参考译文片段】\n" + example["translation_text"]})
    near_block_ids = {b["block_id"] for b in index["blocks"][-spec.n_block:]} if spec.n_block else set()
    if spec.include_summaries:
        reviews = {}
        for rid, review in view.store.objects("summary-review"):
            reviews.setdefault(review["summary_id"], []).append((rid, review))
        for block in index["blocks"]:
            sid = summaries.get(block["block_id"])
            if not sid:
                raise NarrativeError("coverage_gap", "missing exact block/prefix summary: " + block["block_id"])
            summary = summary_view.read_summary(sid, block["chapter_ids"])
            coverage_review_ids = []
            if summary.get("schema_version") == "bilingual-block" and summary["content"].get("coverage_notes"):
                note_reviews=[(rid,r) for rid,r in view.store.objects("summary-coverage-review") if r.get("summary_id")==sid]
                if (not note_reviews or any(r.get("decision") != "non_material_compression_or_reference_warning" or
                        r.get("notes_hash") != digest(summary["content"]["coverage_notes"]) or
                        r.get("policy_fingerprint") != view.policy.fingerprint or not r.get("method") or not r.get("reason")
                        for _,r in note_reviews)):
                    raise NarrativeError("coverage_gap", "v2 summary reports incomplete coverage; explicitly replan or review non-material notes")
                coverage_review_ids=[rid for rid,_ in note_reviews]
            if summary.get("block_id") != block["block_id"]:
                raise NarrativeError("visibility_denied", "summary was generated for another block")
            if summary["ranges"] != block["ranges"]:
                raise NarrativeError("stale_summary", "summary detail partition differs from index")
            checks = reviews.get(sid, [])
            if any(r["decision"] != "accepted" for _, r in checks):
                raise NarrativeError("quality_hold", "summary has a rejected/unresolved review")
            if spec.require_reviewed_summaries and not checks:
                raise NarrativeError("quality_hold", "summary needs semantic review")
            tier = "near_block" if block["block_id"] in near_block_ids else "far_block"
            label = {"block_id": block["block_id"], "chapters": [view.archive.chapters[c]["match_key"] for c in block["chapter_ids"]],
                     "quality": "reviewed" if checks else summary["review_status"]}
            blocks.append({"kind": tier + "_overview", "id": sid, "text": json.dumps(label, ensure_ascii=False) +
                           "\n" + summary["content"]["overview"]["text"]})
            if tier == "near_block":
                for r, detail in zip(block["ranges"], summary["content"]["details"]):
                    if set(r["chapter_ids"]) <= full_ids:
                        omitted.append({"range_id": r["range_id"], "reason": "replaced_by_complete_source", "chapter_ids": r["chapter_ids"]})
                    else:
                        blocks.append({"kind": "near_block_detail", "id": r["range_id"],
                                       "text": json.dumps(r, ensure_ascii=False) + "\n" + detail["text"]})
            deps.extend(summary["dependencies"])
            selected.append({"summary_id": sid, "block_id": block["block_id"], "tier": tier,
                             "chapter_ids": block["chapter_ids"], "review_ids": [r for r, _ in checks]})
            if coverage_review_ids:
                selected[-1]["coverage_review_ids"]=coverage_review_ids
    translation_pids = set()
    for pair in near_chapters:
        src = pair["source"]
        deps.append(dependency(src))
        text = f"【前文章节 {src['match_key']} 日文完整正文】\n{src['text']}\n【中文状态：{pair['translation_status']}】"
        for segment in pair["translation"]:
            keys = {(segment["chapter_id"], p) for p in segment["paragraph_ids"]}
            if keys & translation_pids:
                raise NarrativeError("conflict", "overlapping complete translation samples")
            translation_pids.update(keys)
            text += f"\n【完整中文参考 {segment['match_key']}；范围已核对，语义待核验】\n{segment['text']}"
        blocks.append({"kind": "near_chapter", "id": src["chapter_id"], "text": text})
    metadata_workset = None
    if translation_metadata is not None:
        from ..narrative.metadata import metadata_prompt, query_translation_metadata
        if {x["summary_id"] for x in translation_metadata["inputs"]} != {x["summary_id"] for x in selected}:
            raise NarrativeError("visibility_denied", "metadata inputs must match selected summaries")
        metadata_workset = query_translation_metadata(view, translation_metadata,
            "\n".join([target["text"]] + [p["source"]["text"] for p in near_chapters]), core_forms=metadata_core_forms)
        blocks.append({"kind": "translation_metadata", "id": translation_metadata["snapshot_id"],
                       "text": metadata_prompt(metadata_workset)})
    target_block = {"kind": "target", "id": target["chapter_id"],
                    "text": "请完整翻译以下本章正文。\n<source>\n" + target["text"] + "\n</source>"}
    blocks.append(target_block)
    messages = [{"role": "system", "content": SYSTEM}] + [
        {"role": "user", "content": f"【{b['kind']}】\n{b['text']}"} for b in blocks]
    count = view.counter.messages(messages)
    accounting = {"estimated_input": count, "method": view.counter.identity, "input_budget": spec.input_budget,
                  "context_limit": spec.context_limit, "reserved_output": spec.max_output_tokens,
                  "safety_tokens": spec.safety_tokens, "layers": {}}
    for b in blocks:
        accounting["layers"][b["kind"]] = accounting["layers"].get(b["kind"], 0) + view.counter.count(b["text"])
    if count > spec.input_budget:
        raise NarrativeError("budget_exceeded", json.dumps({"accounting": accounting, "requested": asdict(spec),
                             "suggestions": ["explicitly change N or summary budgets; never silently drop history"]}, ensure_ascii=False))
    result = {"version": PROMPT_VERSION, "source": target, "prompt_blocks": blocks, "messages": messages,
              "policy": view.policy.public(), "index_id": index_id, "requested": asdict(spec),
              "effective": {"near_chapters": [p["source"]["chapter_id"] for p in near_chapters],
                            "n_chapter": len(near_chapters), "n_block": len(near_block_ids) if spec.include_summaries else 0,
                            "bilingual_chapters": sum(bool(p["translation"]) for p in near_chapters),
                            "near_translation_status": [{"chapter_id": p["source"]["chapter_id"], "status": p["translation_status"]} for p in near_chapters],
                            "excluded_near_positions": excluded, "policy_exclusions": index["policy_exclusions"]},
              "selected_summaries": selected, "omitted_ranges": omitted, "token_accounting": accounting,
              "dependencies": deps + [dependency(target)], "knowledge": knowledge,
              "chapter_reviews": [r for p in near_chapters for r in p["review_ids"]],
              "translation_evidence": [s for p in near_chapters for s in p["translation"]],
              "examples": list(examples)}
    result["example_dependencies"] = [x.get("source_dependencies", []) for x in examples]
    if translation_metadata is not None:
        result.update(version="hierarchical-context", translation_metadata=translation_metadata,
                      metadata_workset=metadata_workset, metadata_core_forms=list(metadata_core_forms))
    result["fingerprint"] = digest(result)
    return result


def explain_context(bundle):
    if bundle["fingerprint"] != digest({k: v for k, v in bundle.items() if k != "fingerprint"}):
        raise NarrativeError("stale_summary", "context bundle changed")
    return {k: bundle[k] for k in ("fingerprint", "policy", "requested", "effective", "selected_summaries",
                                  "omitted_ranges", "token_accounting", "chapter_reviews")}
