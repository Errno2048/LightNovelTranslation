"""Mechanical checks for extraction and structural snapshots."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from ..storage import digest, file_digest
from .util import stable_entry_id


def validate_extraction(extraction: dict, *, root: Path | None = None) -> dict:
    blocks = list(extraction.get("blocks", []))
    issues = []
    manifest = extraction.get("manifest", {})
    if (any(manifest.get(key) != manifest.get("input", {}).get(key) for key in ("edition_id", "language", "profile")) or
            manifest.get("block_count") != len(blocks) or
            manifest.get("sources") != extraction.get("sources", []) or
            manifest.get("issue_count") != len(extraction.get("issues", [])) or
            manifest.get("navigation_count") != len(extraction.get("navigation", []))):
        issues.append({"code": "extraction_manifest_mismatch", "severity": "error"})
    actual_fingerprint = digest({
        "input": manifest.get("input"), "sources": extraction.get("sources", []),
        "blocks": blocks, "navigation": extraction.get("navigation", []),
        "issues": extraction.get("issues", []),
    })
    if manifest.get("extraction_fingerprint") != actual_fingerprint:
        issues.append({"code": "extraction_fingerprint_mismatch", "severity": "error"})
    source_by_id = {row["source_id"]: row for row in extraction.get("sources", [])}
    if len(source_by_id) != len(extraction.get("sources", [])):
        issues.append({"code": "duplicate_source_id", "severity": "error"})
    for source in source_by_id.values():
        if root is not None:
            blob = (root / source.get("blob", "")).resolve()
            if not blob.is_relative_to(root.resolve()) or not blob.is_file():
                issues.append({"code": "source_snapshot_missing", "severity": "error",
                               "scope": {"source_id": source["source_id"]}})
            elif file_digest(blob) != source.get("sha256"):
                issues.append({"code": "source_snapshot_changed", "severity": "error",
                               "scope": {"source_id": source["source_id"]}})
    ids = [row.get("block_id") for row in blocks]
    if len(ids) != len(set(ids)):
        issues.append({"code": "duplicate_block_id", "severity": "error"})
    if any(not row.get("source_id") or not row.get("resource_id") for row in blocks):
        issues.append({"code": "block_missing_identity", "severity": "error"})
    if any(row.get("source_id") not in source_by_id or
           row.get("source_sha256") != source_by_id.get(row.get("source_id"), {}).get("sha256")
           for row in blocks):
        issues.append({"code": "block_source_mismatch", "severity": "error"})
    orders = Counter((row.get("source_id"), row.get("resource_id"),
                      row.get("locator", {}).get("block_index", row.get("locator", {}).get("line_start")))
                     for row in blocks)
    for key, count in orders.items():
        if count > 1:
            issues.append({"code": "duplicate_source_order", "severity": "error", "scope": {"resource": key}})
    for row in extraction.get("issues", []):
        severity = row.get("severity") or ("error" if row.get("code") in {
            "missing_resource", "no_text_blocks", "decode_error", "resource_read_error"} else "warning")
        issues.append({"code": row.get("code", "extraction_warning"), "severity": severity,
                       "scope": {k: row[k] for k in ("source_id", "resource_id") if k in row},
                       "message": row.get("message", row.get("code", "extraction warning"))})
    result = {"schema": "import-validation-v1", "scope": "extraction",
              "ok": not any(row.get("severity") == "error" for row in issues),
              "issues": issues, "block_count": len(blocks),
              "extraction_fingerprint": extraction.get("manifest", {}).get("extraction_fingerprint")}
    result["fingerprint"] = digest(result)
    return result


def validate_structure(extraction: dict, structure: dict) -> dict:
    blocks = {row["block_id"]: row for row in extraction.get("blocks", [])}
    entries = structure.get("entries", [])
    issues = list(structure.get("issues", []))
    assigned: list[str] = []
    entry_ids = [row.get("entry_id") for row in entries]
    if len(entry_ids) != len(set(entry_ids)):
        issues.append({"code": "duplicate_entry_id", "severity": "error"})
    if [row.get("reading_order") for row in entries] != list(range(len(entries))):
        issues.append({"code": "invalid_entry_order", "severity": "error"})
    for entry in entries:
        if not entry.get("entry_id") or not entry.get("block_ids"):
            issues.append({"code": "empty_entry", "severity": "error",
                           "scope": {"entry_id": entry.get("entry_id")}})
        for block_id in entry.get("block_ids", []):
            if block_id not in blocks:
                issues.append({"code": "unknown_block_reference", "severity": "error",
                               "scope": {"entry_id": entry.get("entry_id"), "block_id": block_id}})
            assigned.append(block_id)
        title_id = entry.get("title_block_id")
        if title_id and title_id not in entry.get("block_ids", []):
            issues.append({"code": "title_outside_entry", "severity": "error",
                           "scope": {"entry_id": entry.get("entry_id"), "block_id": title_id}})
        known_ids = [key for key in entry.get("block_ids", []) if key in blocks]
        expected_ranges = [{"source_id": blocks[key]["source_id"], "source_sha256": blocks[key]["source_sha256"],
                            "resource_id": blocks[key]["resource_id"], "block_id": key,
                            "locator": blocks[key]["locator"]} for key in known_ids]
        if entry.get("source_ranges") != expected_ranges:
            issues.append({"code": "entry_source_range_mismatch", "severity": "error",
                           "scope": {"entry_id": entry.get("entry_id")}})
        if entry.get("entry_id") != stable_entry_id(extraction["manifest"]["edition_id"], expected_ranges):
            issues.append({"code": "entry_identity_mismatch", "severity": "error"})
    duplicates = [key for key, count in Counter(assigned).items() if count > 1]
    if duplicates:
        issues.append({"code": "overlapping_entries", "severity": "error", "scope": {"block_ids": duplicates}})
    missing = sorted(set(blocks) - set(assigned))
    if missing:
        issues.append({"code": "unassigned_blocks", "severity": "error",
                       "scope": {"block_ids": missing}})
    if assigned != list(blocks):
        issues.append({"code": "source_order_changed", "severity": "error"})
    if structure.get("extraction_fingerprint") != extraction.get("manifest", {}).get("extraction_fingerprint"):
        issues.append({"code": "stale_structure", "severity": "error"})
    if structure.get("fingerprint") != digest({key: value for key, value in structure.items()
                                               if key != "fingerprint"}):
        issues.append({"code": "structure_fingerprint_mismatch", "severity": "error"})
    errors = [row for row in issues if row.get("severity") == "error"]
    warnings = [row for row in issues if row.get("severity") != "error"]
    result = {"schema": "import-validation-v1", "scope": "structure", "ok": not errors,
              "status": "ready" if not errors and not warnings else "ready_with_warnings" if not errors else "needs_resolution",
              "issues": issues, "error_count": len(errors), "warning_count": len(warnings),
              "entry_count": len(entries), "block_count": len(blocks),
              "assigned_block_count": len(set(assigned)),
              "structure_fingerprint": structure.get("fingerprint")}
    result["fingerprint"] = digest(result)
    return result
