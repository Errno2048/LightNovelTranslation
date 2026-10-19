"""Admit navigational summaries with explicit missing-reference annotations."""
from collections import Counter

from .bilingual import KINDS, reference_rows
from .summary_protocol import VERSION, empty_content, parse_output

ADMISSION_VERSION = "summary-admission-v1"


def decode_summary(raw, pairs, config):
    parsed = parse_output(raw)
    issues = list(parsed.issues)
    quarantined = list(parsed.quarantined)
    references, components = [], []
    notes = list(parsed.reference_notes)
    refs = reference_rows(pairs)
    ja_text = "\n".join(p["source"]["text"] for p in pairs)
    zh_text = "\n".join(d["text"] for p in pairs for d in p["translation"])
    value = parsed.content
    content = empty_content()

    def issue(code, path, **details):
        issues.append({"code": code, "path": path, **details})

    def reject(reason, path, obj):
        quarantined.append({"reason": reason, "path": path, "value": obj})

    def note(obj, path):
        if isinstance(obj.get("reference_note"), str) and obj["reference_note"].strip():
            notes.append({"path": path, "text": obj["reference_note"]})

    def strings(obj, path):
        if isinstance(obj, str):
            issue("scalar_to_list", path)
            return [obj] if obj.strip() else []
        if obj is None:
            return []
        if not isinstance(obj, list):
            reject("expected_string_list", path, obj)
            return []
        result = []
        for i, item in enumerate(obj):
            if isinstance(item, str) and item.strip():
                result.append(item)
            elif item not in (None, ""):
                reject("non_string_list_item", path+"/"+str(i), item)
        return result

    def obj_fields(obj, allowed, path):
        if isinstance(obj, dict):
            for key in set(obj)-set(allowed):
                reject("unknown_field", path+"/"+key, obj[key])

    def citation(cite, language, path):
        obj_fields(cite, {"ref", "quote"}, path)
        ref, quote = (cite.get("ref"), cite.get("quote")) if isinstance(cite, dict) else (None, None)
        row = refs.get(ref) if isinstance(ref, str) else None
        reason, suggestions = None, []
        if not isinstance(quote, str) or not quote.strip() or not isinstance(ref, str) or not ref:
            reason = "missing_ref_or_quote"
        elif not row or row["language"] != language:
            reason = "unknown_ref_or_wrong_language"
        elif len(quote) > config.max_quote_chars:
            reason = "quote_exceeds_limit"
        elif quote not in row["text"]:
            reason = "quote_mismatch"
            prefix = ref.rsplit("@", 1)[0]
            suggestions = [r for r, v in refs.items() if r.rsplit("@", 1)[0] == prefix and quote in v["text"]]
        references.append({"path": path, "ref": ref, "quote": quote,
                           "status": "missing_reference" if reason else "matched",
                           "reason": reason, "suggested_refs": suggestions})
        return {"ref": ref, "quote": quote} if reason is None else None

    def evidence(obj, path, required=True):
        if isinstance(obj, dict):
            issue("single_evidence_object_to_list", path)
            obj = [obj]
        if not isinstance(obj, list):
            if obj is not None:
                reject("expected_evidence_list", path, obj)
            obj = []
        if not obj and required:
            references.append({"path": path, "ref": None, "quote": None, "status": "missing_reference",
                               "reason": "not_provided", "suggested_refs": []})
        return [valid for i, e in enumerate(obj) if (valid := citation(e, "ja", path+"/"+str(i))) is not None]

    def text_field(obj, key, path, default=""):
        val = obj.get(key, default)
        if not isinstance(val, str):
            reject("expected_text", path+"/"+key, val)
            return default
        return val

    if not isinstance(value, dict):
        value = {}
    if "overview" not in value and isinstance(value.get("summary"), (str, dict)):
        value = {**value, "overview": value["summary"]}
        issue("summary_alias_for_overview", "/overview")
    obj_fields(value, {"overview", "details", "translation_metadata", "unresolved", "coverage_notes", "summary"}, "")
    overview = value.get("overview", {})
    if isinstance(overview, str):
        overview = {"text": overview}
        issue("overview_string_to_object", "/overview")
    if not isinstance(overview, dict):
        reject("invalid_overview", "/overview", overview)
        overview = {}
    obj_fields(overview, {"text", "evidence", "reference_note"}, "/overview")
    note(overview, "/overview")
    content["overview"] = {"text": text_field(overview, "text", "/overview").strip(),
                           "evidence": evidence(overview.get("evidence"), "/overview/evidence")}
    if len(content["overview"]["text"]) > config.overview_max_chars:
        issue("overview_exceeds_requested_length", "/overview/text", actual=len(content["overview"]["text"]), limit=config.overview_max_chars)
    if value.get("details"):
        reject("details_not_requested_for_single_node", "/details", value["details"])
    for key in ("unresolved", "coverage_notes"):
        content[key] = strings(value.get(key), "/"+key)
    meta = value.get("translation_metadata", {})
    if not isinstance(meta, dict):
        reject("invalid_metadata", "/translation_metadata", meta)
        meta = {}
    obj_fields(meta, {"entities", "fixed_expressions", "conflicts"}, "/translation_metadata")
    content["translation_metadata"]["conflicts"] = strings(meta.get("conflicts"), "/translation_metadata/conflicts")

    def rendering(row, path, forms=None):
        if not isinstance(row, dict):
            reject("invalid_rendering", path, row)
            return None
        obj_fields(row, {"source_form", "target_form", "usage_scope", "evidence", "reference_note"}, path)
        clean = {k: text_field(row, k, path) for k in ("source_form", "target_form", "usage_scope")}
        if not clean["source_form"].strip() or not clean["target_form"].strip():
            reject("missing_rendering_forms", path, row)
            return None
        if clean["source_form"] not in ja_text or (forms is not None and clean["source_form"] not in forms):
            reject("unmatched_or_undeclared_rendering_source", path, row)
            return None
        if clean["target_form"] not in zh_text:
            reject("target_form_not_in_reference_chinese", path, row)
            return None
        note(row, path)
        if not clean["usage_scope"].strip():
            clean["usage_scope"] = "本节点；原输出未说明适用语境"
            issue("missing_usage_scope", path+"/usage_scope")
        clean["evidence"] = []
        groups = row.get("evidence", [])
        if isinstance(groups, dict):
            groups = [groups]
            issue("single_bilingual_evidence_to_list", path+"/evidence")
        if not isinstance(groups, list):
            reject("invalid_bilingual_evidence", path+"/evidence", groups)
            groups = []
        if not groups:
            references.append({"path": path+"/evidence", "ref": None, "quote": None,
                               "status": "missing_reference", "reason": "bilingual_evidence_not_provided", "suggested_refs": []})
        for i, group in enumerate(groups):
            loc = path+"/evidence/"+str(i)
            if not isinstance(group, dict):
                reject("invalid_bilingual_group", loc, group)
                group = {}
            obj_fields(group, {"pairing_id", "ja", "zh"}, loc)
            ja = citation(group.get("ja"), "ja", loc+"/ja")
            zh = citation(group.get("zh"), "zh", loc+"/zh")
            if not ja or not zh:
                continue
            jp, zp = refs[ja["ref"]]["pairing_id"], refs[zh["ref"]]["pairing_id"]
            pid = group.get("pairing_id")
            if not pid and jp == zp:
                pid = jp
                issue("derived_pairing_id", loc, pairing_id=pid)
            reason = "cross_pair_bilingual_evidence" if jp != zp or pid != jp else (
                "rendering_form_absent_from_quote" if clean["source_form"] not in ja["quote"] or clean["target_form"] not in zh["quote"] else None)
            if reason:
                issue(reason, loc)
                references.append({"path": loc, "ref": None, "quote": None, "status": "missing_reference",
                                   "reason": reason, "suggested_refs": []})
            else:
                clean["evidence"].append({"pairing_id": pid, "ja": ja, "zh": zh})
        return clean

    entities = meta.get("entities", [])
    if not isinstance(entities, list):
        reject("entities_not_a_list", "/translation_metadata/entities", entities)
        entities = []
    id_counts = Counter(e.get("local_entity_id") for e in entities if isinstance(e, dict) and
                        isinstance(e.get("local_entity_id"), str) and e["local_entity_id"].strip())
    for i, entity in enumerate(entities):
        original_path = "/translation_metadata/entities/"+str(i)
        if not isinstance(entity, dict):
            reject("entity_not_object", original_path, entity)
            continue
        obj_fields(entity, {"local_entity_id", "kind", "source_forms", "description", "renderings", "reference_note"}, original_path)
        ident = entity.get("local_entity_id")
        if isinstance(ident, str) and id_counts[ident] > 1:
            reject("ambiguous_duplicate_entity_id", original_path, entity)
            continue
        if not isinstance(ident, str) or not ident.strip():
            ident = f"auto_e{i+1}"
            while ident in id_counts:
                ident += "_"
            issue("generated_local_id", original_path, id=ident)
        forms = strings(entity.get("source_forms"), original_path+"/source_forms")
        if not forms or any(f not in ja_text for f in forms):
            reject("entity_form_absent_from_japanese", original_path, entity)
            continue
        if len(set(forms)) != len(forms):
            forms = list(dict.fromkeys(forms))
            issue("deduplicate_identical_source_form", original_path)
        path = "/translation_metadata/entities/"+str(len(content["translation_metadata"]["entities"]))
        note(entity, path)
        kind = entity.get("kind")
        if not isinstance(kind, str) or kind not in KINDS:
            issue("unknown_kind_preserved_as_annotation", path+"/kind", original_kind=kind, canonical_kind="other")
            kind = "other"
        desc = entity.get("description", {})
        if isinstance(desc, str):
            desc = {"text": desc}
            issue("description_string_to_object", path+"/description")
        if not isinstance(desc, dict):
            reject("invalid_description", original_path+"/description", desc)
            desc = {}
        obj_fields(desc, {"text", "evidence", "reference_note"}, path+"/description")
        note(desc, path+"/description")
        desc_text = text_field(desc, "text", path+"/description")
        clean = {"local_entity_id": ident, "kind": kind, "source_forms": forms,
                 "description": {"text": desc_text, "evidence": evidence(desc.get("evidence"), path+"/description/evidence", bool(desc_text.strip()))}, "renderings": []}
        rend = entity.get("renderings", [])
        if not isinstance(rend, list):
            reject("renderings_not_a_list", original_path+"/renderings", rend)
            rend = []
        for j, row in enumerate(rend):
            loc = path+"/renderings/"+str(len(clean["renderings"]))
            if (valid := rendering(row, loc, forms)) is not None:
                clean["renderings"].append(valid)
        content["translation_metadata"]["entities"].append(clean)
        components.append({"path": path, "local_entity_id": ident, "status": "reference_candidate_semantics_unreviewed"})
    fixed = meta.get("fixed_expressions", [])
    if not isinstance(fixed, list):
        reject("fixed_expressions_not_a_list", "/translation_metadata/fixed_expressions", fixed)
        fixed = []
    for row in fixed:
        loc = "/translation_metadata/fixed_expressions/"+str(len(content["translation_metadata"]["fixed_expressions"]))
        if (valid := rendering(row, loc)) is not None:
            content["translation_metadata"]["fixed_expressions"].append(valid)
    for key, maximum in (("entities", config.max_entities), ("fixed_expressions", config.max_fixed_expressions)):
        count = len(content["translation_metadata"][key])
        if count > maximum:
            issue("metadata_count_exceeds_requested_limit", "/translation_metadata/"+key, actual=count, limit=maximum)
    blocking = list(parsed.blocking)
    if not content["overview"]["text"]:
        blocking.append("missing_readable_overview")
    missing = [r for r in references if r["status"] == "missing_reference"]
    quality = {"version": VERSION, "admission_version": ADMISSION_VERSION, "output_format": parsed.output_format,
        "status": "needs_format_repair" if blocking else "usable_with_annotations" if issues or missing or quarantined or notes else "usable",
        "semantic_status": "unreviewed", "reference_status": "missing_reference" if missing or notes else "literal_matches_only",
        "missing_reference_count": len(missing), "references": references, "reference_notes": notes,
        "issues": issues, "quarantined": quarantined, "components": components, "blocking": list(dict.fromkeys(blocking))}
    return {"usable": not blocking, "content": content, "quality": quality}


def component_quality(quality, path):
    def inside(row):
        p = row.get("path", "")
        return p == path or p.startswith(path+"/")
    missing = [r for r in quality["references"] if inside(r) and r["status"] == "missing_reference"]
    notes = [r for r in quality["reference_notes"] if inside(r)]
    return {"reference_status": "missing_reference" if missing or notes else "literal_matches_only",
            "missing_reference_count": len(missing), "semantic_status": "unreviewed",
            "issues": [i for i in quality["issues"] if inside(i)],
            "reference_notes": notes}
