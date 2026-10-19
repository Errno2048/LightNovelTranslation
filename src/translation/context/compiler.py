"""Compile reproducible, policy-bound P4 worksets without model calls."""
from __future__ import annotations
import json
from dataclasses import asdict, dataclass
from typing import Any
from ..storage import digest


@dataclass
class ContextBundle:
    prompt_blocks: list[dict[str, str]]
    selected_evidence: list[dict]
    knowledge_revision: str
    unresolved_requirements: list[str]
    omitted_candidates: list[dict]
    token_accounting: dict[str, Any]
    dependency_manifest: dict[str, Any]
    fingerprint: str

    def public(self):
        return asdict(self)


def compile_context(task, view, *, token_budget=60000, max_examples=2, surrounding=1):
    from ..workflow import build_messages
    if token_budget <= 0 or not task.get("source", {}).get("text"):
        raise ValueError("positive budget and complete source required")
    source = task["source"]
    examples = task.get("examples", [])
    if len(examples) > max_examples:
        raise ValueError("B/C require identical few-shot sets; increase max_examples")
    blocked = set(view.policy.hidden_source_ids + view.policy.reserved_source_ids)
    for example in examples:
        if example.get("status") != "reviewed_prefix" or set(example.get("source_dependencies", [])) & blocked:
            raise ValueError("example is not reviewed/visible")
        if example.get("review", {}).get("purposes") is not None and not example["review"]["purposes"].get("few_shot"):
            raise ValueError("example does not authorize few-shot use")
        for e in example.get("review", {}).get("evidence", []):
            view.kb.archive.validate_evidence(e, view.policy)
    conventions = view.get_conventions(source["text"])
    conflicts = {c["source_form"] for c in conventions["conflicts"]}
    unresolved = ["convention_conflict"] if conflicts else []
    if conventions["unknown"]:
        unresolved.append("no_matching_convention")
    blocks = [{"kind": "rules", "text": "仅将下列记录用于译名和表达一致性。条件不明的称谓保持候选，不能据此指定说话人；冲突和未知保持未决。"}]
    chosen, omitted, optional = [], [], []
    def include(item_id, block, required=False):
        explained = view.explain(item_id)
        blocks.append(block)
        chosen.append({"item_id": item_id, "revision_id": explained["item"]["revision_id"],
                       "evidence": explained["evidence"], "source_dependencies": explained["item"]["source_dependencies"]})
        if not required:
            optional.append((block, chosen[-1]))
    for c in sorted(conventions["conventions"], key=lambda x: x["item_id"]):
        if c["source_form"] in conflicts:
            omitted.append({"id": c["item_id"], "reason": "unresolved_conflict"})
            continue
        if c["applicability"] == "uncertain":
            unresolved.append("conditional_convention:" + c["item_id"])
        include(c["item_id"], {"kind": "conventions", "text": json.dumps(c, ensure_ascii=False)}, c["strength"] == "required")
    for e in view.resolve_mentions(source["text"])["candidates"]:
        include(e["entity_id"], {"kind": "entity", "text": json.dumps(e, ensure_ascii=False)})
    def estimate():
        provisional = dict(task, context_bundle={"prompt_blocks": blocks})
        return sum(len(m["content"].encode()) + 32 for m in build_messages(provisional, "C"))
    count = estimate()
    while count > token_budget and optional:
        block, evidence = optional.pop()
        blocks.remove(block)
        chosen.remove(evidence)
        omitted.append({"id": evidence["item_id"], "reason": "context_budget"})
        count = estimate()
    if count > token_budget:
        raise ValueError("budget_exceeded: complete source, identical few-shot and required conventions exceed budget")
    dependencies = {"view_fingerprint": view.fingerprint, "source_id": source.get("chapter_id"),
                    "source_revision_id": source.get("revision_id"), "policy": view.policy.public(),
                    "knowledge_items": [{"id": e["item_id"], "revision_id": e["revision_id"]} for e in chosen],
                    "source_dependencies": sorted({d for e in chosen + examples for d in e.get("source_dependencies", [])}),
                    "example_ids": [e.get("id", e.get("source_chapter_id")) for e in examples]}
    result = ContextBundle(blocks, chosen, view.commit_id, sorted(set(unresolved)), omitted,
                           {"estimated": count, "method": "utf8_byte_upper_bound_with_message_overhead",
                            "budget": token_budget, "includes_complete_prompt": True}, dependencies, "")
    result.fingerprint = digest(result.public())
    view.kb.record_context(result)
    return result
