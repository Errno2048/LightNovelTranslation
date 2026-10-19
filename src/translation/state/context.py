"""Compile the reviewed/candidate P6 workset into an immutable P6 context bundle."""
from __future__ import annotations

import json

from ..storage import digest
from .schemas import P6Error


def compile_state_context(view, base_context, overlay_id, *, max_tokens=8000):
    if type(max_tokens) is not int or max_tokens <= 0:
        raise P6Error("budget_exceeded", "positive P6 token budget required")
    if base_context.get("policy") != view.policy_public:
        raise P6Error("visibility_denied", "base translation context uses another narrative policy")
    if base_context.get("source", {}).get("chapter_id") != view.target_id:
        raise P6Error("visibility_denied", "base context target differs from P6 target")
    overlay = view.store.read_overlay(overlay_id)
    if overlay.get("policy") != view.view_fingerprint:
        raise P6Error("visibility_denied", "analysis overlay belongs to another view")
    for dep in overlay["dependencies"]:
        view.narrative.validate_dependency(dep, task=dep["chapter_id"] == view.target_id)
    records = overlay["records"]
    confirmed = [r for r in records if r.get("review_status") == "reviewed"]
    candidates = [r for r in records if r.get("review_status", "candidate") == "candidate"]
    usages = [r for r in confirmed if r.get("type") == "USAGE"]
    facts = [r for r in confirmed if r.get("type") in {"SCENE", "EVENT", "STATE", "PERSPECTIVE"}]
    active = confirmed + candidates
    dialogue = [r for r in active if r.get("type") == "UTTERANCE"]
    unresolved = [r for r in active if r.get("type") == "NEED"]
    other_candidates = [r for r in candidates if r.get("type") not in {"UTTERANCE", "NEED"}]
    blocks = [{"kind": "p6_rules", "text": "以下P6工作集只在所列证据和故事时点内有效。已审记录仍不能覆盖目标日文；候选未审，不作为剧情事实。不同回忆/假设分支不自动合并。对白多候选须保留，不添加无依据的人名或性别。"}]
    def add(kind, rows):
        if rows:
            blocks.append({"kind": kind, "text": json.dumps(rows, ensure_ascii=False, sort_keys=True)})
    add("p6_reviewed_state", facts)
    add("p6_observed_usage", usages)
    add("p6_dialogue_candidates", dialogue)
    add("p6_unreviewed_candidates", other_candidates)
    add("p6_unresolved", unresolved)
    messages = list(base_context["messages"])
    target_at = next((i for i, b in enumerate(base_context["prompt_blocks"]) if b.get("kind") == "target"), len(base_context["prompt_blocks"]) - 1)
    # Each bundle's user messages mirror prompt_blocks after the system message.
    insert_at = max(1, min(target_at + 1, len(messages)))
    p6_messages = [{"role": "user", "content": f"【{b['kind']}】\n{b['text']}"} for b in blocks]
    messages[insert_at:insert_at] = p6_messages
    count = view.narrative.counter.messages(messages)
    added = sum(view.narrative.counter.count(b["text"]) for b in blocks)
    if added > max_tokens:
        raise P6Error("budget_exceeded", "P6 bundle exceeds additive token budget; explicit workset selection required")
    if count > base_context["requested"].get("context_limit", 1048576) - base_context["requested"].get("max_output_tokens", 8192) - base_context["requested"].get("safety_tokens", 2048):
        raise P6Error("budget_exceeded", "complete P5+P6 context exceeds model input budget")
    data = {"version": "p6-state-context-v1", "base_version": base_context["version"],
        "base_fingerprint": base_context["fingerprint"], "overlay_id": overlay_id,
        "overlay_fingerprint": digest(overlay), "policy": view.policy_public,
        "state_view_fingerprint": view.view_fingerprint, "prompt_blocks": blocks, "messages": messages,
        "dependencies": overlay["dependencies"], "target_source_hash": base_context["source"]["text_hash"],
        "token_accounting": {"base_input": base_context.get("token_accounting",{}).get("estimated_input"),
            "p6_added": added, "compiled_input": count, "p6_soft_budget": max_tokens,
            "method": view.narrative.counter.identity}, "coverage": overlay.get("coverage", "analysis_window_only"),
        "candidate_count": len(candidates), "reviewed_count": len(confirmed), "omitted": overlay.get("omitted", [])}
    data["fingerprint"] = digest(data)
    return data


def validate_state_context(view, bundle, base_context):
    if bundle.get("version") != "p6-state-context-v1" or bundle.get("base_fingerprint") != base_context.get("fingerprint"):
        raise P6Error("stale_evidence", "P6 context base changed")
    rebuilt = compile_state_context(view, base_context, bundle["overlay_id"],
                                   max_tokens=bundle["token_accounting"]["p6_soft_budget"])
    if rebuilt != bundle:
        raise P6Error("stale_evidence", "P6 context changed since it was frozen")
    for dep in bundle["dependencies"]:
        view.narrative.validate_dependency(dep, task=dep["chapter_id"] == view.target_id)
    return True
