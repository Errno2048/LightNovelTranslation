"""Deterministic, policy-bound aggregation; equal names remain candidate groups."""
import json

from ..storage import digest
from .schemas import NarrativeError


def merge_book_metadata(view, summary_ids, *, description_reviews=(), persist=True):
    index = []
    entries, expressions, conflicts = [], [], []
    artifacts = {}
    for sid in summary_ids:
        artifact = view.store.get(sid, "summary")
        artifact = view.read_summary(sid, artifact["chapter_ids"])
        artifacts[sid] = artifact
        if artifact.get("schema_version") != "bilingual-block":
            raise NarrativeError("coverage_gap", "v1 summary has no bilingual metadata")
        index.append({"summary_id": sid, "chapter_ids": artifact["chapter_ids"]})
        meta = artifact["content"]["translation_metadata"]
        for local in meta["entities"]:
            entry = {**local, "summary_id": sid, "block_id": artifact["block_id"],
                     "source_order": max(p["source"]["order"] for p in artifact["pairs"]),
                     "status": "observed", "review_status": "semantics_unreviewed", "origin": "reference"}
            entry["entry_id"] = "block-entity:" + digest({"summary_id": sid, "local_entity_id": local["local_entity_id"]})
            entries.append(entry)
        expressions.extend({**r, "summary_id": sid, "status": "observed", "review_status": "semantics_unreviewed"}
                           for r in meta["fixed_expressions"])
        conflicts.extend({"summary_id": sid, "text": text} for text in meta["conflicts"])
    if len(set(summary_ids)) != len(summary_ids):
        raise NarrativeError("invalid_record", "duplicate metadata input")
    applied = set()
    for review in description_reviews:
        key = (review.get("summary_id"), review.get("local_entity_id"))
        matching = [e for e in entries if (e["summary_id"], e["local_entity_id"]) == key]
        if key in applied or len(matching) != 1 or review.get("decision") != "reject_description" or not review.get("method") or not review.get("reason"):
            raise NarrativeError("invalid_record", "explicit unambiguous description rejection required")
        from .bilingual import reference_rows
        refs = reference_rows(artifacts[key[0]]["pairs"])
        if not review.get("evidence"):
            raise NarrativeError("invalid_record", "description review needs source evidence")
        for cite in review["evidence"]:
            row = refs.get(cite.get("ref"))
            if not row or row["language"] != "ja" or not cite.get("quote") or cite["quote"] not in row["text"]:
                raise NarrativeError("invalid_record", "invalid description review evidence")
        entry = matching[0]
        entry["rejected_description"] = entry["description"]
        entry["description"] = {"text": "", "evidence": []}
        entry["description_review"] = review
        applied.add(key)
    # Name matching is a retrieval aid, never an asserted same_as relationship.
    groups = {}
    for entry in entries:
        for form in entry["source_forms"]:
            groups.setdefault((entry["kind"], form), []).append(entry["entry_id"])
    candidates = [{"kind": k[0], "source_form": k[1], "entry_ids": sorted(v), "identity": "unresolved_candidate_group"}
                  for k, v in sorted(groups.items())]
    variants = []
    for group in candidates:
        relevant = [e for e in entries if e["entry_id"] in group["entry_ids"]]
        values = sorted({r["target_form"] for e in relevant for r in e["renderings"] if r["source_form"] == group["source_form"]})
        if len(values) > 1:
            variants.append({**group, "target_forms": values, "status": "potential_variant_requires_context"})
    result = {"version": "book-translation-metadata-v1", "policy": view.policy.public(), "inputs": index,
              "entries": entries, "fixed_expressions": expressions, "conflicts": conflicts,
              "candidate_groups": candidates, "rendering_variants": variants,
              "identity_links": [], "description_reviews": list(description_reviews),
              "review_status": "structural_with_selected_description_rejections_not_full_semantic_review"}
    result["fingerprint"] = digest(result)
    result["snapshot_id"] = view.store.put("book-metadata", result) if persist else "book-metadata:" + digest(result)
    return result


def validate_metadata(view, snapshot):
    expected = merge_book_metadata(view, [x["summary_id"] for x in snapshot["inputs"]],
                                   description_reviews=snapshot.get("description_reviews", []), persist=False)
    if expected != snapshot:
        raise NarrativeError("stale_summary", "metadata no longer matches policy or block evidence")


def query_translation_metadata(view, snapshot, text, *, core_forms=()):
    validate_metadata(view, snapshot)
    selected, omitted = [], []
    for entry in snapshot["entries"]:
        matched = [f for f in entry["source_forms"] if f in text or f in core_forms]
        if matched:
            selected.append({**entry, "matched_forms": matched,
                             "selection": "configured_core" if set(matched) & set(core_forms) else "source_form_match"})
        else:
            omitted.append({"entry_id": entry["entry_id"], "reason": "no_core_or_current_context_match"})
    expressions = [r for r in snapshot["fixed_expressions"] if r["source_form"] in text]
    chosen = {e["entry_id"] for e in selected}
    variants = [r for r in snapshot["rendering_variants"] if chosen & set(r["entry_ids"])]
    return {"snapshot_id": snapshot["snapshot_id"], "selected": selected, "omitted": omitted,
            "fixed_expressions": expressions, "rendering_variants": variants,
            "conflicts": snapshot["conflicts"], "core_forms": list(core_forms)}


def metadata_prompt(workset):
    rows = []
    for e in workset["selected"]:
        rows.append({"id": e["entry_id"], "kind": e["kind"], "source_forms": e["source_forms"],
                     "description": e["description"]["text"] or "描述待补",
                     "observed_renderings": [{"source": r["source_form"], "target": r["target_form"],
                                              "scope": r["usage_scope"],
                                              **({"reference_status": r["quality"]["reference_status"]} if "quality" in r else {})}
                                             for r in e["renderings"]],
                     "status": e["status"], "source_block": e["block_id"],
                     **({"support": {k: e["quality"][k] for k in ("reference_status", "missing_reference_count", "semantic_status")}}
                        if "quality" in e else {})})
    payload = {"entities": rows, "fixed_expressions": [{**{k: r[k] for k in ("source_form", "target_form", "usage_scope", "status")},
                **({"reference_status": r["quality"]["reference_status"]} if "quality" in r else {})}
               for r in workset["fixed_expressions"]], "rendering_variants": workset["rendering_variants"],
               "unresolved": workset["conflicts"]}
    return ("以下是从允许双语参考中观察到的译法及作品描述，语义尚未独立审查。无冲突且语境适用时优先沿用；"
            "冲突时按本章与邻近参考判断，不按多数票取名。相同表面名的多张卡不自动代表同一实体。"
            "同卡内多个日文形式也只是候选分组，不保证互为别名；逐条使用译法，不将分组描述套给所有词。"
            "用法中的‘全篇’是抽取器的未审查判断，证据实际只覆盖所示来源范围。"
            "role是职位/称谓，不能当人名。描述只辅助理解，不授权补写身份、关系或说话人。\n" +
            json.dumps(payload, ensure_ascii=False, sort_keys=True))
