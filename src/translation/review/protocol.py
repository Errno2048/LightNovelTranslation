"""Small flat record protocol. Syntax recovery never supplies missing semantics."""
import re
import unicodedata

FIELDS = {
    "MAP": {"source", "target", "status"},
    "FINDING": {"category", "severity", "source_ref", "target_ref", "claim", "suggestion", "confidence", "needs_lookup", "evidence_ref"},
    "UNRESOLVED": {"text", "needs_lookup"},
    "TERMCHECK": {"check", "verdict", "reason", "source_ref", "target_ref"},
    "SUMMARY": {"reviewed_all", "semantic_coverage_complete", "zero_findings"},
    "DECISION": {"finding", "decision", "reason"},
    "PATCH": {"start", "end", "expected_text_hash", "findings", "replacement"},
    "SIGNAL": {"signal", "decision", "reason", "source_ref", "target_ref", "finding"},
}
ALIASES = {"source_ids": "source", "target_ids": "target", "source_paragraphs": "source",
           "target_paragraphs": "target", "replacement_text": "replacement"}


_CLOSED_ENUMS = {
    "category": {"omission", "addition", "negation", "role", "number", "term", "tense", "speaker", "style", "other"},
    "severity": {"critical", "major", "minor", "suggestion"},
    "basis": {"source_meaning", "observed_usage", "style_preference"},
    "confidence": {"high", "medium", "low"},
    "decision": {"confirmed", "dismissed", "unresolved"},
    "reviewed_all": {"yes"},
    "semantic_coverage_complete": {"yes", "no"},
    "zero_findings": {"yes", "no"},
    "status": {"mapped"},
    "verdict": {"ok", "violation", "uncertain"},
}
_ENUM_ALIASES = {
    "decision": {
        "确认": "confirmed", "確定": "confirmed", "已确认": "confirmed", "已確認": "confirmed",
        "驳回": "dismissed", "駁回": "dismissed", "否决": "dismissed", "否決": "dismissed",
        "未决": "unresolved", "未決": "unresolved", "不确定": "unresolved", "不確定": "unresolved",
    },
    "reviewed_all": {"是": "yes"},
    "semantic_coverage_complete": {"是": "yes", "否": "no"},
    "zero_findings": {"是": "yes", "否": "no"},
}


def _header(line, tolerant):
    candidate = unicodedata.normalize("NFKC", line) if tolerant else line
    name_pattern = r"[A-Za-z_]+" if tolerant else r"[A-Z_]+"
    match = re.fullmatch(r"@@(" + name_pattern + r")(?:\s+([^\s]+))?", candidate.strip())
    if not match:
        return None, candidate
    name, ident = match.groups()
    if tolerant:
        name = name.upper()
        ident = unicodedata.normalize("NFKC", ident) if ident else ident
    return (name, ident), candidate


def _field_line(line, tolerant):
    if tolerant:
        match = re.match(r"^([^:：]+?)\s*[:：]\s?(.*)$", line)
        if not match:
            return None
        key = unicodedata.normalize("NFKC", match[1]).strip().lower()
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", key):
            return None
        return key, match[2]
    match = re.match(r"^([a-z_]+)\s*[:：]\s?(.*)$", line)
    return (match[1], match[2]) if match else None


def _looks_like_field(line, tolerant):
    parsed = _field_line(line, tolerant)
    return bool(parsed and re.fullmatch(r"[a-z_][a-z0-9_]*", parsed[0]))


def _normalize_value(field, value, notes, line_number):
    """Normalize only protocol values whose domains are explicitly closed."""
    original = value.strip()
    if field in {"finding", "signal", "source", "target"}:
        normalized = unicodedata.normalize("NFKC", original)
        if normalized != original:
            notes.append("fullwidth_id:" + str(line_number))
            return normalized
        return value
    if field in {"source_ref", "target_ref", "evidence_ref"}:
        # Citation quotes are free text. Width-fold only the label before '|'.
        parts = re.split(r"[|｜]", original, maxsplit=1)
        label, separator, quote = (parts[0], True, parts[1]) if len(parts) == 2 else (original, False, "")
        folded = unicodedata.normalize("NFKC", label).strip()
        if separator and (folded != label.strip() or "｜" in original.split("|", 1)[0]):
            notes.append("fullwidth_reference_id:" + str(line_number))
            return folded + " | " + quote.strip()
        if not separator and folded != original:
            notes.append("fullwidth_reference_id:" + str(line_number))
            return folded
        return value
    if field in _CLOSED_ENUMS:
        folded = unicodedata.normalize("NFKC", original).lower()
        alias = _ENUM_ALIASES.get(field, {}).get(folded)
        if alias:
            notes.append("closed_enum_alias:" + str(line_number) + ":" + field)
            return alias
        if folded in _CLOSED_ENUMS[field]:
            if folded != original:
                notes.append("fullwidth_enum:" + str(line_number) + ":" + field)
                return folded
    return value


def parse_records(text, allowed, *, allow_intermediate_end=True, extra_fields=None, allow_inline_end=False,
                  syntax_profile="legacy", allow_preamble=False, field_schema=None, implicit_fields=None):
    if syntax_profile not in {"legacy", "tolerant-records"}:
        raise ValueError("unknown record syntax profile")
    tolerant = syntax_profile == "tolerant-records"
    schema = FIELDS if field_schema is None else field_schema
    records, errors, current, key, ended = [], [], None, None, False
    lines = text.replace("\r\n", "\n").splitlines()
    notes = []
    if allow_inline_end and 'DECISION' in allowed:
        nonempty = [i for i,line in enumerate(lines) if line.strip()]
        if nonempty:
            last = nonempty[-1]
            # Recover only the final delimiter attached to a reason field.
            # Quoted markers, other fields, interior delimiters and following
            # prose remain invalid; no claim or reason text is synthesized.
            end_pattern = r'[ \t]+[@＠]{2}[EＥ][NＮ][DＤ][ \t]*$' if tolerant else r'[ \t]+@@END[ \t]*$'
            marker = re.search(end_pattern, lines[last], re.IGNORECASE if tolerant else 0)
            prefix = lines[last][:marker.start()].rstrip() if marker else ""
            parsed_reason = _field_line(prefix.strip(), tolerant) if marker else None
            if parsed_reason and ALIASES.get(parsed_reason[0], parsed_reason[0]) == 'reason' and parsed_reason[1].strip():
                lines[last:last+1] = [prefix,'@@END']
                notes.append('terminal_inline_end_after_reason')
            else:
                marker_line = unicodedata.normalize("NFKC", lines[last]).strip() if tolerant else lines[last].strip()
                marker_only = marker_line != "@@END" and re.fullmatch(r'(?:最后\s*)?@@END[。．.!！]?[ \t]*', marker_line)
                if marker_only:
                    # Models sometimes wrap the required delimiter in a short
                    # Chinese lead-in (for example ``最后@@END。``). Recover only
                    # this final, delimiter-only line; never strip prose or an
                    # interior marker and never invent a missing reason.
                    lines[last] = '@@END'
                    notes.append('terminal_inline_end_after_marker')
    if tolerant:
        nonempty = [i for i, line in enumerate(lines) if line.strip()]
        if nonempty:
            last = nonempty[-1]
            marker_line = unicodedata.normalize("NFKC", lines[last]).strip()
            if marker_line != "@@END" and re.fullmatch(r'(?:最后\s*)?@@END[.!。！]?[ \t]*', marker_line):
                lines[last] = "@@END"
                notes.append("terminal_marker_variant")
    ignored_preamble = set()
    preamble_text = None
    if allow_preamble:
        first_record = next((i for i, raw in enumerate(lines) if _header(raw.strip(), tolerant)[0]), None)
        if first_record is not None:
            preamble = [i for i, raw in enumerate(lines[:first_record]) if raw.strip() and not raw.strip().startswith("```")]
            # Permit a single leading explanation block only. Structured-looking
            # lines or record markers remain errors instead of being discarded.
            if preamble and all(not _looks_like_field(lines[i].strip(), tolerant) and "@@" not in lines[i] and "＠＠" not in lines[i]
                                for i in preamble):
                ignored_preamble.update(preamble)
                preamble_text = "\n".join(lines[i] for i in preamble).strip()
                notes.append("ignored_preamble_before_first_record")
    terminal = [i for i, line in enumerate(lines) if line.strip() and not line.strip().startswith("```")]
    # A real response placed END before SUMMARY and another END after it. Treat
    # only exact intermediate delimiters as separators when a final END exists.
    final_end = None
    if terminal:
        last_header, _ = _header(lines[terminal[-1]].strip(), tolerant)
        if last_header and last_header[0] == "END":
            final_end = terminal[-1]
    for line_number, raw in enumerate(lines):
        if line_number in ignored_preamble:
            continue
        line = raw.strip()
        if line.startswith("```"):
            continue
        parsed_header, _ = _header(line, tolerant)
        if parsed_header:
            name, ident = parsed_header
            if tolerant and line != "@@"+name+(" "+ident if ident else ""):
                notes.append("record_header_normalized_at_line:"+str(line_number+1))
            if ended:
                errors.append("record_after_end")
            if name == "END":
                if allow_intermediate_end and final_end is not None and line_number < final_end:
                    notes.append("intermediate_end_at_line:"+str(line_number+1))
                    current, key = None, None
                    continue
                ended, current, key = True, None, None
                continue
            if name not in allowed:
                errors.append("unknown_record:"+name)
                current, key = None, None
                continue
            current = {"record": name, "id": ident or str(len(records)+1), "fields": {}}
            records.append(current)
            key = None
            continue
        if not line:
            if current and key:
                current["fields"][key] += "\n"
            continue
        if ended:
            errors.append("text_after_end")
            continue
        if current is None:
            errors.append("text_outside_record")
            continue
        parsed_field = _field_line(line, tolerant)
        if parsed_field:
            field_name, field_value = parsed_field
            field = ALIASES.get(field_name, field_name)
            if tolerant and not re.match(r"^"+re.escape(field_name)+r"\s*:", line):
                notes.append("field_syntax_normalized_at_line:"+str(line_number+1))
            if field not in schema[current["record"]] | set((extra_fields or {}).get(current["record"], ())):
                if key and key == (implicit_fields or {}).get(current["record"]):
                    current["fields"][key] += "\n"+raw
                    notes.append("literal_prose_field:"+field)
                else:
                    errors.append("unknown_field:"+field)
                    key = None
            elif field in current["fields"]:
                errors.append("duplicate_field:"+field)
                key = None
            else:
                key = field
                current["fields"][key] = _normalize_value(field, field_value, notes, line_number+1) if tolerant else field_value
        elif key:
            current["fields"][key] += "\n"+raw
        elif current["record"] in (implicit_fields or {}):
            field = implicit_fields[current["record"]]
            if field not in schema[current["record"]] or field in current["fields"]:
                errors.append("invalid_implicit_field")
            else:
                key = field
                current["fields"][key] = raw
                notes.append("implicit_field:"+current["record"]+":"+key)
        else:
            errors.append("orphan_line")
    ids = [(r["record"], r["id"]) for r in records]
    if len(ids) != len(set(ids)):
        errors.append("duplicate_record_id")
    for record in records:
        record["fields"] = {k: v.strip() for k, v in record["fields"].items()}
    if not ended:
        errors.append("missing_end")
    if not records:
        errors.append("empty_records")
    value = {"records": records, "errors": errors, "complete": not errors}
    if notes: value["notes"] = notes
    if preamble_text: value["leading_prose"] = preamble_text
    return value


def bind_exact_header_ids(parsed, expected):
    """Fill an absent association only from an exact known header ID.

    Generic sequential headings such as d1 cannot identify f1. An explicit
    conflicting association remains untouched for downstream validation.
    """
    notes = []
    for record in parsed["records"]:
        field = {"DECISION": "finding", "SIGNAL": "signal"}.get(record["record"])
        if field and field not in record["fields"] and record["id"] in expected.get(record["record"], set()):
            record["fields"][field] = record["id"]
            notes.append("bind_exact_header_id:"+record["record"]+":"+record["id"])
    return notes


def header_association_errors(parsed):
    """A generic record number is safe only with an explicit association.

    d1/s1 identify output records, not input candidates. A complete finding or
    signal field supplies the association; an ID-like conflicting header does
    not. Missing associations are checked by the consuming role.
    """
    errors = []
    for record in parsed["records"]:
        field = {"DECISION": "finding", "SIGNAL": "signal"}.get(record["record"])
        association = record["fields"].get(field) if field else None
        generic = r"d[1-9][0-9]*" if record["record"] == "DECISION" else r"s[1-9][0-9]*"
        if association and record["id"] != association and not re.fullmatch(generic, record["id"]):
            errors.append("record_header_association_mismatch:"+record["record"]+":"+record["id"])
    return errors


def ids(text):
    return [x for x in re.split(r"[,，\s]+", text.strip()) if x]


def locate(ref, segments, *, require_quote=False):
    if not isinstance(ref, str):
        return None
    if "|" in ref:
        alias, quote = (x.strip() for x in ref.split("|", 1))
    else:
        alias, quote = ref.strip(), None
    if require_quote and not quote:
        return None
    matches = [s for s in segments if alias in {s["alias"], s["id"]}]
    if len(matches) != 1 or quote is not None and (not quote or quote not in matches[0]["text"]):
        return None
    return {"paragraph_id": matches[0]["id"], "alias": matches[0]["alias"], "quote": quote}
