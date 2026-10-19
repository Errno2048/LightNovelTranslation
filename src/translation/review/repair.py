"""One bounded, lossless transcription fallback. No category/claim/quote repairs."""
import json

from .protocol import parse_records, FIELDS


def decode_records(session, version_id, raw, allowed, *, extra_fields=None, syntax_profile="legacy", allow_preamble=False):
    text = raw["generation"]["text"]
    parsed = parse_records(text, allowed, allow_intermediate_end=True, extra_fields=extra_fields,
                           syntax_profile=syntax_profile, allow_preamble=allow_preamble)
    if parsed["complete"]:
        return parsed, None
    # Only a complete set of readable records missing its terminal delimiter is
    # eligible. Ambiguous keys, unknown content and substantive omissions fail closed.
    if parsed["errors"] != ["missing_end"] or not parsed["records"]:
        return parsed, None
    if "SUMMARY" in allowed and not any(r["record"] == "SUMMARY" and r["fields"].get("reviewed_all") == "yes" for r in parsed["records"]):
        return parsed, None
    if session.limits.get("repair_requests", 0) == 0:
        return parsed, None
    # No originals, references, diagnoses or new facts are passed to this role.
    messages = [{"role": "system", "content": "你是格式转录器，不是审校员。逐字保留所有记录ID、字段及值，只补齐末尾@@END。不得修复引文、语义、分类或建议。"},
                {"role": "user", "content": "允许记录及字段："+json.dumps({k: sorted(FIELDS[k] | set((extra_fields or {}).get(k, ()))) for k in sorted(allowed)}, ensure_ascii=False)+"\n【待转录原输出】\n"+text}]
    repair_budget=getattr(session,'role_budgets',{}).get('repair')
    fixed = session.call(version_id, "repair", messages,
                         max_tokens=repair_budget.requested_output_tokens if repair_budget else 16384)
    decoded = parse_records(fixed["generation"]["text"], allowed, extra_fields=extra_fields,
                            syntax_profile=syntax_profile, allow_preamble=allow_preamble)
    accepted = (decoded["complete"] and decoded["records"] == parsed["records"]
                and decoded.get("leading_prose") == parsed.get("leading_prose"))
    rid = session.view.store.put("review-format-repair", {"version_id": version_id, "original_generation": raw["id"],
        "repair_generation": fixed["id"], "accepted": accepted, "fidelity": "exact_record_ids_fields_and_values"})
    return (decoded if accepted else parsed), rid


def read_records(view, version_id, generation_id, repair_id, allowed, *, allow_intermediate_end=True,
                 extra_fields=None, syntax_profile="legacy", allow_preamble=False):
    from ..narrative.schemas import NarrativeError
    raw = view.store.get(generation_id, "review-generation")
    if raw["version_id"] != version_id or raw["generation"]["finish_reason"] != "stop":
        raise NarrativeError("invalid_record", "wrong/incomplete raw response")
    parsed = parse_records(raw["generation"]["text"], allowed, allow_intermediate_end=allow_intermediate_end,
                           extra_fields=extra_fields, syntax_profile=syntax_profile, allow_preamble=allow_preamble)
    if repair_id:
        repair = view.store.get(repair_id, "review-format-repair")
        if repair["version_id"] != version_id or repair["original_generation"] != generation_id:
            raise NarrativeError("invalid_record", "format repair provenance mismatch")
        fixed = view.store.get(repair["repair_generation"], "review-generation")
        if fixed["version_id"] != version_id or fixed["kind"] != "repair" or fixed["generation"]["finish_reason"] != "stop":
            raise NarrativeError("invalid_record", "repair response belongs to another request")
        decoded = parse_records(fixed["generation"]["text"], allowed, extra_fields=extra_fields,
                                syntax_profile=syntax_profile, allow_preamble=allow_preamble)
        if repair["accepted"]:
            if (not decoded["complete"] or decoded["records"] != parsed["records"]
                    or decoded.get("leading_prose") != parsed.get("leading_prose")):
                raise NarrativeError("invalid_record", "repair changed fields or values")
            parsed = decoded
    return parsed
