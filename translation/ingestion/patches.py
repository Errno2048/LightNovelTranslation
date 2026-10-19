"""Revision-bound, block-level structural edits; source text is never editable."""

from __future__ import annotations

from copy import deepcopy

from ..storage import digest
from .util import normalize_title, stable_entry_id
from .validation import validate_structure


def _ranges(block_ids: list[str], blocks: dict) -> list[dict]:
    return [{"source_id": blocks[key]["source_id"],
             "source_sha256": blocks[key]["source_sha256"],
             "resource_id": blocks[key]["resource_id"], "block_id": key,
             "locator": deepcopy(blocks[key]["locator"])} for key in block_ids]


def patch_structure(extraction: dict, base: dict, patch: dict) -> dict:
    if patch.get("schema") != "import-structure-patch-v1":
        raise ValueError("unknown structure patch schema")
    if patch.get("base_structure_revision") != base.get("fingerprint"):
        raise ValueError("stale structure patch")
    if not validate_structure(extraction, base)["ok"]:
        raise ValueError("cannot patch an invalid base structure")
    operations = patch.get("operations")
    if not isinstance(operations, list) or not operations or len(operations) > 100:
        raise ValueError("patch must contain between 1 and 100 operations")
    if set(patch) - {"schema", "base_structure_revision", "operations", "decision_by"}:
        raise ValueError("unknown patch fields; source text cannot be edited")
    decision_by = patch.get("decision_by", "human")
    if decision_by not in {"human", "agent", "rule"}:
        raise ValueError("invalid structural decision author")
    structure = deepcopy(base)
    entries = structure["entries"]
    blocks = {row["block_id"]: row for row in extraction["blocks"]}
    source_formats = {row["source_id"]: row["format"] for row in extraction["sources"]}
    patch_id = "P" + digest(patch)[:20]

    def containing(block_id):
        if block_id not in blocks:
            raise ValueError("unknown boundary block")
        return next((index for index, entry in enumerate(entries) if block_id in entry["block_ids"]), None)

    def title(entry, reference):
        if not isinstance(reference, dict) or set(reference) != {"block_id"}:
            raise ValueError("title_ref must select a complete source block")
        block_id = reference["block_id"]
        if block_id not in entry["block_ids"] or not blocks[block_id]["raw_text"].strip():
            raise ValueError("title reference is empty or outside entry")
        entry.update(raw_title=blocks[block_id]["raw_text"].strip(), title_source="body_heading",
                     title_block_id=block_id)

    def targets(operation):
        wanted = operation.get("entry_ids")
        if not isinstance(wanted, list) or not wanted or len(wanted) != len(set(wanted)):
            raise ValueError("entry_ids must be a non-empty unique list")
        rows = [entry for entry in entries if entry["entry_id"] in wanted]
        if len(rows) != len(wanted):
            raise ValueError("unknown patch entry ID")
        positions = [entries.index(entry) for entry in rows]
        if positions != list(range(min(positions), max(positions) + 1)):
            raise ValueError("patch range must contain consecutive entries")
        return rows

    for operation in operations:
        if not isinstance(operation, dict) or not isinstance(operation.get("reason"), str) or not operation["reason"].strip():
            raise ValueError("each patch operation needs a reason")
        op = operation.get("op")
        common = {"op", "reason", "evidence_refs"}
        fields = {
            "set_boundary": {"at", "title_ref"}, "remove_boundary": {"at"},
            "select_title": {"at", "title_ref"}, "classify_range": {"entry_ids", "kind"},
            "set_paragraph_policy": {"entry_ids", "policy"},
            "set_volume": {"entry_ids", "volume_label"},
        }
        if op not in fields or set(operation) - common - fields[op]:
            raise ValueError("unsupported structural operation or fields")
        touched = []
        if op in {"set_boundary", "remove_boundary", "select_title"}:
            at = operation.get("at")
            if not isinstance(at, dict) or set(at) != {"block_id"}:
                raise ValueError("boundary must select a complete source block")
            block_id = at["block_id"]
            index = containing(block_id)
            if index is None:
                raise ValueError("boundary block has no owner")
            entry = entries[index]
            position = entry["block_ids"].index(block_id)
            if op == "set_boundary":
                if position:
                    second = deepcopy(entry)
                    second["entry_id"] = "pending:" + patch_id + ":" + str(len(entries))
                    second["block_ids"], entry["block_ids"] = entry["block_ids"][position:], entry["block_ids"][:position]
                    second.update(raw_title="", title_block_id=None, title_source="generated_label")
                    # A title moved into the second range cannot belong to the first.
                    if entry.get("title_block_id") not in entry["block_ids"]:
                        entry.update(raw_title="", title_block_id=None, title_source="generated_label",
                                     boundary_kind="unresolved")
                    entries.insert(index + 1, second)
                    entry = second
                if "title_ref" in operation:
                    title(entry, operation["title_ref"])
                entry["boundary_kind"] = "chapter" if entry.get("raw_title") else "unresolved"
                touched = [entry]
            elif op == "remove_boundary":
                if position or index == 0:
                    raise ValueError("remove_boundary must target an existing non-initial entry start")
                previous = entries[index - 1]
                previous["block_ids"].extend(entry["block_ids"])
                entries.pop(index)
                touched = [previous]
            else:
                title(entry, operation.get("title_ref"))
                touched = [entry]
        else:
            touched = targets(operation)
            if op == "classify_range":
                kind = operation.get("kind")
                if kind not in {"narrative", "narrative_side", "reference", "toc", "preface", "afterword", "volume_intro", "translator_note"}:
                    raise ValueError("unsupported entry classification")
                for entry in touched:
                    entry["kind"] = kind
            elif op == "set_paragraph_policy":
                policy = operation.get("policy")
                if policy not in {"line", "blank_line", "wrapped"}:
                    raise ValueError("choose an explicit paragraph policy")
                if any(source_formats[blocks[key]["source_id"]] != "txt"
                       for entry in touched for key in entry["block_ids"]):
                    raise ValueError("TXT paragraph policies cannot be applied to EPUB blocks")
                for entry in touched:
                    entry.setdefault("metadata", {})["paragraph_policy"] = policy
            elif op == "set_volume":
                label = operation.get("volume_label")
                if not isinstance(label, str) or not normalize_title(label):
                    raise ValueError("volume_label is required")
                volume_id = extraction["manifest"]["edition_id"] + ":volume:" + digest(label)[:16]
                for entry in touched:
                    entry["volume_id"] = volume_id
                    entry.setdefault("metadata", {})["volume_label"] = label
                    entry["metadata"]["volume_source"] = "structural_patch"
        for entry in touched:
            entry.setdefault("metadata", {}).update(decision_by=decision_by, patch_id=patch_id,
                                                    decision_state="confirmed")

    for order, entry in enumerate(entries):
        source_ranges = _ranges(entry["block_ids"], blocks)
        entry_id = stable_entry_id(extraction["manifest"]["edition_id"], source_ranges)
        entry.update(entry_id=entry_id, logical_entry_id=entry_id, reading_order=order,
                     source_ranges=source_ranges,
                     display_title=entry.get("raw_title") or f"Unnamed reading entry {order + 1}")
    structure.update(parent_structure_revision=base["fingerprint"], patch_id=patch_id)
    structure["issues"] = [row for row in structure.get("issues", []) if row["code"] != "unresolved_structure"]
    unresolved = [row["entry_id"] for row in entries if row["boundary_kind"] == "unresolved"]
    if unresolved:
        structure["issues"].append({"code": "unresolved_structure", "severity": "warning",
                                    "scope": {"entry_ids": unresolved}})
    structure["status"] = "ready_with_warnings" if structure["issues"] else "ready"
    structure["fingerprint"] = digest({key: value for key, value in structure.items() if key != "fingerprint"})
    result = validate_structure(extraction, structure)
    if not result["ok"]:
        raise ValueError("structural patch violates coverage, title ownership or reading order")
    return structure
