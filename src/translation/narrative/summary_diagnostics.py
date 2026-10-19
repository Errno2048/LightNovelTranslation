"""Separate wire/schema, literal citations and semantic review for experiments."""
import re

from .bilingual import KINDS, reference_rows, validate_content


def schema_errors(content, config):
    citation = {"ref": str, "quote": str}
    evidence = {"pairing_id": str, "ja": citation, "zh": citation}
    rendering = {"source_form": str, "target_form": str, "usage_scope": str, "evidence": [evidence]}
    summary = {"text": str, "evidence": [citation]}
    entity = {"local_entity_id": str, "kind": str, "source_forms": [str],
              "description": summary, "renderings": [rendering]}
    schema = {"overview": summary, "details": [], "translation_metadata": {
        "entities": [entity], "fixed_expressions": [rendering], "conflicts": [str]},
        "unresolved": [str], "coverage_notes": [str]}
    errors = []

    def check(obj, spec, path):
        if isinstance(spec, dict):
            if not isinstance(obj, dict) or set(obj) != set(spec):
                errors.append(path + ": object fields mismatch")
            if isinstance(obj, dict):
                for k in set(obj) & set(spec):
                    check(obj[k], spec[k], path + "/" + k)
        elif isinstance(spec, list):
            if not isinstance(obj, list):
                errors.append(path + ": expected list")
            elif spec:
                for i, item in enumerate(obj):
                    check(item, spec[0], path + "/" + str(i))
            elif obj:
                errors.append(path + ": expected empty details")
        elif not isinstance(obj, spec):
            errors.append(path + ": expected " + spec.__name__)
    check(content, schema, "")
    if errors:
        return errors
    if not content["overview"]["text"].strip() or len(content["overview"]["text"].strip()) > config.overview_max_chars:
        errors.append("overview empty or exceeds length")
    if not content["overview"]["evidence"]:
        errors.append("overview evidence empty")
    meta = content["translation_metadata"]
    if len(meta["entities"]) > config.max_entities or len(meta["fixed_expressions"]) > config.max_fixed_expressions:
        errors.append("metadata count exceeds budget")
    ids = [e["local_entity_id"] for e in meta["entities"]]
    if len(ids) != len(set(ids)) or any(not i.strip() for i in ids):
        errors.append("duplicate/empty entity id")
    for e in meta["entities"]:
        if e["kind"] not in KINDS or not e["source_forms"] or len(set(e["source_forms"])) != len(e["source_forms"]):
            errors.append("invalid kind/forms: " + e["local_entity_id"])
        if e["description"]["text"].strip() and not e["description"]["evidence"]:
            errors.append("description evidence empty: " + e["local_entity_id"])
        for row in e["renderings"]:
            if row["source_form"] not in e["source_forms"]:
                errors.append("rendering form undeclared: " + e["local_entity_id"])
    for row in meta["fixed_expressions"] + [r for e in meta["entities"] for r in e["renderings"]]:
        if any(not row[k].strip() for k in ("source_form", "target_form", "usage_scope")) or not row["evidence"]:
            errors.append("empty rendering field/evidence")
    return errors


def diagnose(content, pairs, config):
    refs = reference_rows(pairs)
    citations = []

    def walk(obj, path=""):
        if isinstance(obj, dict):
            if set(obj) == {"ref", "quote"}:
                ref, quote = obj["ref"], obj["quote"]
                row = refs.get(ref) if isinstance(ref, str) else None
                prefix = ref.rsplit("@", 1)[0] if isinstance(ref, str) else ""
                alternatives = [r for r, v in refs.items() if r.rsplit("@", 1)[0] == prefix
                                and isinstance(quote, str) and quote and quote in v["text"]]
                if row and isinstance(quote, str) and quote and quote in row["text"]:
                    status = "exact_at_ref"
                elif len(alternatives) == 1:
                    status = "unique_same_chapter_relocation"
                elif alternatives:
                    status = "ambiguous_same_chapter_relocation"
                else:
                    status = "no_exact_same_chapter_match"
                citations.append({"path": path, "ref": ref, "quote": quote, "status": status,
                                  "alternatives": alternatives, "within_quote_limit": isinstance(quote, str) and len(quote) <= config.max_quote_chars})
            for key, val in obj.items():
                walk(val, path + "/" + key)
        elif isinstance(obj, list):
            for i, val in enumerate(obj):
                walk(val, path + "/" + str(i))
    walk(content)
    try:
        validate_content(content, pairs, [], config)
        error = None
    except (ValueError, KeyError, TypeError) as exc:
        error = str(exc)
    shape = schema_errors(content, config)
    return {"schema_ok": not shape, "schema_errors": shape, "strict_content_validation_ok": error is None,
            "strict_content_validation_error": error, "citation_counts": {s: sum(e["status"] == s for e in citations)
                for s in ("exact_at_ref", "unique_same_chapter_relocation", "ambiguous_same_chapter_relocation", "no_exact_same_chapter_match")},
            "citations": citations, "semantic_status": "requires_separate_review"}
