"""Compile all coarse overviews plus explicitly materialized nearby refinements."""
from dataclasses import asdict
import json

from ..narrative.multilevel import (MultiLevelContextSpec, read_multilevel_index,
    select_nodes, read_node_summary, rollup_multilevel_metadata)
from ..narrative.schemas import NarrativeError
from ..narrative.service import dependency
from ..narrative.metadata import metadata_prompt
from ..storage import digest
from ..workflow import SYSTEM
from .hierarchical import _validate_examples, validate_knowledge


def compile_multilevel_context(view, index_id, summary_ids, *, spec=None, metadata=None,
                               metadata_core_forms=(), knowledge=None, examples=(), coverage_scope_reviews=(),
                               coverage_compression_reviews=()):
    spec = spec or MultiLevelContextSpec()
    summary_view = view.summary_view() if hasattr(view, "summary_view") else view
    if view.policy.target_id is None:
        raise NarrativeError("unsupported_policy", "multilevel translation requires a target")
    index = read_multilevel_index(view, index_id)
    required = select_nodes(view, index_id, n_child=spec.n_child, requested_child_ids=spec.requested_child_ids)
    missing = [n["node_id"] for n in required if n["node_id"] not in summary_ids]
    if missing:
        raise NarrativeError("coverage_gap", "missing required coarse/child summaries: " + ", ".join(missing))
    from ..narrative.scope_coverage import read_scope_review
    scope_reviews = {}
    if len(set(coverage_scope_reviews)) != len(coverage_scope_reviews):
        raise NarrativeError("invalid_record", "duplicate coverage scope review")
    for review_id in coverage_scope_reviews:
        review = read_scope_review(summary_view,index_id,review_id)
        if review['summary_id'] not in summary_ids.values() or review['summary_id'] in scope_reviews:
            raise NarrativeError("visibility_denied", "scope review is duplicated or belongs to an unselected summary")
        scope_reviews[review['summary_id']] = review
    from ..narrative.scope_coverage import read_compression_review
    compression_reviews = {}
    if len(set(coverage_compression_reviews)) != len(coverage_compression_reviews):
        raise NarrativeError('invalid_record','duplicate compression review')
    for review_id in coverage_compression_reviews:
        review=read_compression_review(summary_view,index_id,review_id)
        if review['summary_id'] not in summary_ids.values() or review['summary_id'] in compression_reviews:
            raise NarrativeError('visibility_denied','compression review is duplicated or belongs to an unselected summary')
        compression_reviews[review['summary_id']]=review
    # Also admit metadata-only refinements and reject unknown/out-of-view inputs.
    snapshot = rollup_multilevel_metadata(summary_view, index_id, summary_ids, persist=False)
    if metadata is not None and metadata != snapshot:
        raise NarrativeError("stale_summary", "parent metadata revision differs from exact node inputs")
    _validate_examples(view, examples)
    knowledge = knowledge or {"prompt_blocks": [], "selected_evidence": [], "fingerprint": None}
    validate_knowledge(view, knowledge)
    target = view.body(view.policy.target_id, task=True)
    prior = [c for c in view.sources if c["order"] < target["order"]]
    near_ids = [c["chapter_id"] for c in prior[-spec.n_chapter:]] if spec.n_chapter else []
    excluded = [cid for cid in near_ids if cid in view.policy.reserved_source_ids]
    near = view.read_chapters([cid for cid in near_ids if cid not in excluded])
    full = {p["source"]["chapter_id"] for p in near}
    blocks = [{"kind": "rules", "text": "一级为粗略前情，二级为已按需构建的详情。未构建的二级节点不表示没有事件。"
        "摘要及元信息语义未审查，可能有压缩、遗漏和错误；以目标原文和近章完整正文为准。"
        "只翻译最后的目标正文，不补写人物身份、关系或说话人；保留不确定性。"}]
    if any("quality" in view.store.get(sid, "multilevel-summary") for sid in summary_ids.values()):
        blocks[0]["text"] += ("missing_reference表示引文缺失或定位不符，不表示该段剧情不存在，也不代表已核实。"
            "需要核对时可按node_id和chapters定位二级索引或原文；当前上下文不提供自动获取工具。")
    if index["excluded_source_ids"]:
        blocks.append({"kind": "rules", "text": "资料按实验政策隐藏了部分前文章节，不推断这些缺口的剧情。"})
    if scope_reviews:
        blocks.append({"kind":"rules","text":"部分摘要的缺章提示仅指向政策隐藏的范围外章节，已核对范围；原提示仍留档，不推断隐藏剧情，也不代表摘要语义已确认。"})
    if compression_reviews:
        blocks.append({'kind':'rules','text':'部分提示仅说明粗概括未逐一展开战斗细节，原提示仍留档；这不代表摘要内容已获语义确认。'})
    for row in knowledge.get("prompt_blocks", []):
        blocks.append({"kind": "global_knowledge", "text": row["text"]})
    for example in examples:
        blocks.append({"kind": "few_shot", "text": "【日文】\n"+example["source_text"]+"\n【中文】\n"+example["translation_text"]})
    dependencies, selected, omitted = {}, [], []
    for node in required:
        sid = summary_ids[node["node_id"]]
        artifact = read_node_summary(summary_view, sid, node)
        reviewed = {n['note_index'] for n in scope_reviews.get(sid,{}).get('notices',[])}
        reviewed.update(n['note_index'] for n in compression_reviews.get(sid,{}).get('notices',[]))
        if any(i not in reviewed for i,_ in enumerate(artifact["content"]["coverage_notes"])):
            raise NarrativeError("coverage_gap", "node reports unresolved material coverage notes: " + node["node_id"])
        if node["level"] == 2 and set(node["chapter_ids"]) <= full:
            omitted.append({"node_id": node["node_id"], "reason": "replaced_by_complete_chapters"})
        else:
            label = {"node_id": node["node_id"], "level": node["level"],
                "chapters": [h["match_key"] for h in node["headers"]], "quality": artifact["review_status"]}
            if "quality" in artifact:
                q = artifact["quality"]
                label.update(reference_status=q["reference_status"], missing_reference_count=q["missing_reference_count"],
                    lookup={"parent_id": node["parent_id"], "chapter_range": [node["headers"][0]["match_key"], node["headers"][-1]["match_key"]]})
            blocks.append({"kind": "coarse_overview" if node["level"] == 1 else "child_detail", "id": sid,
                "text": json.dumps(label, ensure_ascii=False)+"\n"+artifact["content"]["overview"]["text"]})
    # Every metadata input is a dependency, even if it did not produce a quote.
    for parent in snapshot["parents"]:
        for item in parent["inputs"]:
            sid = item["summary_id"]
            artifact = view.store.get(sid, "multilevel-summary")
            selected.append({"block_id": item["node_id"], "summary_id": sid})
            for pair in artifact["pairs"]:
                dependencies[pair["source"]["chapter_id"]] = dependency(pair["source"])
                for segment in pair["translation"]:
                    body = view.body(segment["chapter_id"])
                    dependencies[body["chapter_id"]] = dependency(body)
    for pair in near:
        src = pair["source"]
        dependencies[src["chapter_id"]] = dependency(src)
        text = f"【前章 {src['match_key']} 完整日文】\n{src['text']}\n【中文状态：{pair['translation_status']}】"
        for segment in pair["translation"]:
            text += "\n【完整范围中文参考，语义待核验】\n"+segment["text"]
            body = view.body(segment["chapter_id"])
            dependencies[body["chapter_id"]] = dependency(body)
        blocks.append({"kind": "near_chapter", "id": src["chapter_id"], "text": text})
    match_text = "\n".join([target["text"]]+[p["source"]["text"] for p in near])
    entries = [e for p in snapshot["parents"] for e in p["entries"]]
    chosen = [e for e in entries if any(f in match_text or f in metadata_core_forms for f in e["source_forms"])]
    chosen_ids = {e["entry_id"] for e in chosen}
    workset = {"snapshot_id": snapshot["snapshot_id"], "selected": chosen,
        "omitted": [{"entry_id": e["entry_id"], "reason": "no_target_near_or_core_match"} for e in entries if e["entry_id"] not in chosen_ids],
        "fixed_expressions": [r for p in snapshot["parents"] for r in p["fixed_expressions"] if r["source_form"] in match_text],
        "rendering_variants": [], "conflicts": [r for p in snapshot["parents"] for r in p["conflicts"]]}
    blocks.append({"kind": "translation_metadata", "text": metadata_prompt(workset)})
    blocks.append({"kind": "target", "text": "请完整翻译以下本章正文。\n<source>\n"+target["text"]+"\n</source>"})
    messages = [{"role": "system", "content": SYSTEM}]+[
        {"role": "user", "content": f"【{b['kind']}】\n{b['text']}"} for b in blocks]
    count = view.counter.messages(messages)
    layers = {}
    for block in blocks:
        layers[block["kind"]] = layers.get(block["kind"], 0)+view.counter.count(block["text"])
    if count > spec.input_budget:
        raise NarrativeError("budget_exceeded", "multilevel context exceeds budget; explicitly revise windows, no silent truncation")
    dependencies[target["chapter_id"]] = dependency(target)
    result = {"version": "multilevel-context-v1", "source": target, "index_id": index_id,
        "policy": view.policy.public(), "requested": asdict(spec), "prompt_blocks": blocks, "messages": messages,
        "selected_summaries": selected, "omitted_ranges": omitted, "chapter_reviews": [r for p in near for r in p["review_ids"]],
        "translation_evidence": [s for p in near for s in p["translation"]], "dependencies": list(dependencies.values()),
        "knowledge": knowledge, "examples": list(examples), "multilevel_metadata": snapshot,
        "metadata_workset": workset, "metadata_core_forms": list(metadata_core_forms),
        "effective": {"coarse_nodes": len(index["roots"]), "child_nodes": sum(n["level"] == 2 for n in required),
            "near_chapters": [p["source"]["chapter_id"] for p in near], "excluded_near_positions": excluded, "coverage": "all_required_coarse_and_child_nodes"},
        "token_accounting": {"estimated_input": count, "method": view.counter.identity, "input_budget": spec.input_budget,
            "context_limit": spec.context_limit, "reserved_output": spec.max_output_tokens, "safety_tokens": spec.safety_tokens, "layers": layers}}
    if coverage_scope_reviews:
        result['coverage_scope_reviews'] = list(coverage_scope_reviews)
    if coverage_compression_reviews:
        result['coverage_compression_reviews'] = list(coverage_compression_reviews)
    result["fingerprint"] = digest(result)
    return result
