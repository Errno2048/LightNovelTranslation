"""Lossless flat-record parsing with exact source citation binding."""
import json
from .schemas import P6Error, SCHEMA_VERSION, validate_record

TYPES = {"SCENE", "EVENT", "STATE", "PERSPECTIVE", "USAGE", "UTTERANCE", "NEED", "SUMMARY"}


def parse_flat_records(text: str, allowed_refs: dict[str, str]):
    records, errors, current, current_count = [], [], None, 0
    for line_no, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if line.startswith("@@"):
            marker = line[2:].strip()
            if marker == "END":
                if current is None:
                    errors.append({"line": line_no, "kind": "unexpected_end_marker"})
                elif current_count == 0:
                    errors.append({"line": line_no, "kind": "empty_record_block", "type": current})
                current = None
                current_count = 0
            elif marker in TYPES:
                if current is not None:
                    errors.append({"line": line_no, "kind": "missing_end_marker", "type": current})
                current = marker
                current_count = 0
            else:
                errors.append({"line": line_no, "kind": "unknown_record_marker"})
                current = None
            continue
        if current is None:
            errors.append({"line": line_no, "kind": "content_outside_record"})
            continue
        # One JSON object per record; no enclosing array/bracket matching needed.
        try:
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise ValueError()
            row = validate_record({"schema": SCHEMA_VERSION, "type": current, **payload}, allowed_refs=allowed_refs)
            records.append(row)
            current_count += 1
        except (ValueError, P6Error) as exc:
            errors.append({"line": line_no, "kind": getattr(exc, "kind", "malformed_json"), "detail": str(exc)})
    if current is not None:
        errors.append({"kind": "unterminated_record_block", "type": current})
    return {"records": records, "errors": errors,
            "complete": bool(records) and current is None and current_count == 0 and not errors}


def format_record(kind: str, payload: dict) -> str:
    if kind not in TYPES:
        raise P6Error("invalid_record", "unknown flat record kind")
    return "@@" + kind + "\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n@@END"
