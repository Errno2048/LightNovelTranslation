"""Export a validated generic structure to the existing edition archive."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re

from ..storage import atomic_text, digest, read_json, read_jsonl, write_json, write_jsonl
from .structure import looks_like_heading


_WRAPPED_HARD_BOUNDARY = re.compile(
    r"^(?:[\u300c\u300e\u201c\u2018\u300e\u3008\uff08(]|"
    r"[\-\*\u30fb\u25cf\u25cb\u25a0\u25c6\u25c7\u25a1]|"
    r"\d{1,4}[.)\u3001\uff0e]|[\uff10-\uff19]{1,4}[.)\u3001\uff0e])"
)
_SCENE_SEPARATOR = re.compile(r"^[\s*\uff0a=\uff1d\-\uff0d_\uff3f\u25c7\u25c6\u25cb\u25cf\u25a1]{3,}$")


def _source_range(block: dict) -> dict:
    return {
        "source_id": block["source_id"],
        "source_sha256": block.get("source_sha256"),
        "resource_id": block["resource_id"],
        "source_order": block.get("source_order"),
        "block_id": block["block_id"],
        "locator": deepcopy(block.get("locator", {})),
    }


def _join_wrapped(lines: list[str]) -> str:
    """Join explicitly declared hard-wrapped TXT lines conservatively."""
    result = lines[0]
    for line in lines[1:]:
        left = result[-1:] if result else ""
        right = line[:1]
        if left and right and left.isascii() and right.isascii() and left.isalnum() and right.isalnum():
            result += " " + line
        else:
            result += line
    return result


def _is_hard_paragraph_boundary(text: str) -> bool:
    value = text.strip()
    return (not value or looks_like_heading(value) or
            bool(_WRAPPED_HARD_BOUNDARY.match(value)) or bool(_SCENE_SEPARATOR.fullmatch(value)))


def _paragraph_rows(body_blocks: list[dict], source_by_id: dict[str, dict]) -> list[dict]:
    """Apply the frozen TXT paragraph policy while retaining every line locator."""
    result: list[dict] = []
    pending: list[dict] = []
    pending_key: tuple[str, str] | None = None
    pending_policy = ""

    def flush():
        nonlocal pending, pending_key, pending_policy
        if not pending:
            return
        texts = [row["raw_text"] for row in pending]
        if pending_policy == "wrapped":
            text = _join_wrapped(texts)
        elif pending_policy == "blank_line":
            text = "\n".join(texts)
        else:
            text = texts[0]
        result.append({"text": text, "block_ids": [row["block_id"] for row in pending],
                       "source_ranges": [_source_range(row) for row in pending],
                       "transform_ids": ([f"txt-{pending_policy}-paragraph-v1"]
                                         if pending_policy != "line" else [])})
        pending, pending_key, pending_policy = [], None, ""

    for block in body_blocks:
        text = block.get("raw_text", "")
        if not text.strip():
            flush()
            continue
        source = source_by_id.get(block["source_id"], {})
        policy = block.get("layout_hints", {}).get("paragraph_policy")
        if source.get("format") != "txt" or policy not in {"blank_line", "wrapped"}:
            flush()
            result.append({"text": text, "block_ids": [block["block_id"]],
                           "source_ranges": [_source_range(block)]})
            continue
        key = (block["source_id"], block["resource_id"])
        if pending and (key != pending_key or policy != pending_policy):
            flush()
        if policy == "wrapped" and pending and _is_hard_paragraph_boundary(text):
            flush()
        pending_key, pending_policy = key, policy
        pending.append(block)
        if policy == "wrapped" and _is_hard_paragraph_boundary(text):
            flush()
    flush()
    return result


def _nonspace(value: str) -> str:
    return re.sub(r"\s+", "", value)


def export_edition(extraction: dict, structure: dict, validation: dict, output: Path) -> dict:
    if (not validation.get("ok") or
            not validation.get("extraction", {}).get("ok", True) or
            not validation.get("structure", {}).get("ok", True)):
        raise ValueError("cannot export an invalid import structure")
    blocks = {row["block_id"]: row for row in extraction.get("blocks", [])}
    source_by_id = {row["source_id"]: row for row in extraction.get("sources", [])}
    edition_id = extraction["manifest"]["edition_id"]
    language = extraction["manifest"]["language"]
    document_id = f"{edition_id}:import"
    entries, paragraphs, provenance = [], [], []
    for entry in structure["entries"]:
        body_blocks = [blocks[block_id] for block_id in entry["block_ids"]
                       if block_id != entry.get("title_block_id")]
        policy_override = entry.get("metadata", {}).get("paragraph_policy")
        if policy_override:
            body_blocks = [{**row, "layout_hints": {**row.get("layout_hints", {}),
                                                   "paragraph_policy": policy_override}}
                           for row in body_blocks]
        rows = _paragraph_rows(body_blocks, source_by_id)
        source_text = _nonspace("".join(row["raw_text"] for row in body_blocks))
        exported_text = _nonspace("".join(row["text"] for row in rows))
        if source_text != exported_text:
            raise ValueError("paragraph normalization changed non-whitespace source text")
        revision = digest({"entry": entry, "rows": rows, "parser": "edition-export-v1"})
        paragraph_ids = []
        for ordinal, row in enumerate(rows):
            paragraph_id = f"{entry['entry_id']}@{revision[:16]}:p{ordinal:04d}"
            paragraph_ids.append(paragraph_id)
            paragraphs.append({
                "schema_version": "editions-v1",
                "edition_id": edition_id,
                "document_id": document_id,
                "entry_id": entry["entry_id"],
                "paragraph_id": paragraph_id,
                "revision_id": revision,
                "ordinal": ordinal,
                "kind": "body",
                "raw_text": row["text"],
                "source_ranges": row["source_ranges"],
                "transform_ids": row.get("transform_ids", []),
                "ruby": [item for block_id in row["block_ids"] for item in blocks[block_id].get("ruby", [])],
                "asset_refs": [item for block_id in row["block_ids"] for item in blocks[block_id].get("asset_refs", [])],
            })
            provenance.append({"paragraph_id": paragraph_id, "block_ids": row["block_ids"],
                               "source_ranges": row["source_ranges"]})
        entries.append({
            "schema_version": "editions-v1",
            "edition_id": edition_id,
            "document_id": document_id,
            "entry_id": entry["entry_id"],
            "revision_id": revision,
            "edition_order": entry["reading_order"],
            "native_order": entry["reading_order"],
            "logical_entry_order": entry["reading_order"],
            "volume_id": entry.get("volume_id"),
            "raw_title": entry.get("raw_title", ""),
            "display_title": entry.get("display_title", ""),
            "title_source": entry.get("title_source", "unknown"),
            "language": language,
            "kind": entry.get("kind", "narrative"),
            "boundary_kind": entry.get("boundary_kind", "unresolved"),
            "paragraph_ids": paragraph_ids,
            "title_block_id": entry.get("title_block_id"),
            "title_source_ranges": ([_source_range(blocks[entry["title_block_id"]])]
                                    if entry.get("title_block_id") else []),
            "source_ranges": entry.get("source_ranges", []),
            "metadata": {**entry.get("metadata", {}),
                         "index_boundary_profile": "logical-volume-v1",
                         "logical_entry_id": entry.get("logical_entry_id"),
                         "import_revision": structure["fingerprint"],
                         "quality_issue_codes": sorted({row["code"] for row in validation.get("issues", [])})},
        })
        if entry.get("title_block_id"):
            title_block = blocks[entry["title_block_id"]]
            provenance.append({"role": "chapter_title", "entry_id": entry["entry_id"],
                               "block_ids": [title_block["block_id"]],
                               "source_ranges": [_source_range(title_block)]})
        separators = [row for row in body_blocks if not row["raw_text"].strip()]
        if separators:
            provenance.append({"role": "layout_separator", "entry_id": entry["entry_id"],
                               "block_ids": [row["block_id"] for row in separators],
                               "source_ranges": [_source_range(row) for row in separators]})
    manifest = {"schema": "editions-export-v1", "status": "complete",
                "edition_id": edition_id, "language": language, "document_id": document_id,
                "entry_count": len(entries), "paragraph_count": len(paragraphs),
                "source_extraction_fingerprint": extraction["manifest"]["extraction_fingerprint"],
                "structure_fingerprint": structure["fingerprint"],
                "validation_fingerprint": validation["fingerprint"],
                "view_fingerprint": digest({"entries": entries, "paragraphs": paragraphs}),
                "source_nonspace_chars": sum(len(_nonspace(row["raw_text"])) for row in blocks.values()),
                "body_nonspace_chars": sum(len(_nonspace(row["raw_text"])) for row in paragraphs),
                "provenance_fingerprint": digest(provenance),
                "quality_status": validation.get("status", "ready_with_warnings"),
                "issue_count": len(validation.get("issues", [])),
                "issues_fingerprint": digest(validation.get("issues", [])),
                "semantic_status": "unreviewed", "evaluation_eligible": False}
    output = Path(output)
    manifest_path = output / "manifest.json"
    if manifest_path.exists():
        existing = read_json(manifest_path)
        if existing == manifest:
            for name, expected in (("entries", entries), ("paragraphs", paragraphs),
                                   ("provenance", provenance), ("issues", validation.get("issues", []))):
                target = output / (name + ".jsonl")
                if not target.is_file() or read_jsonl(target) != expected:
                    raise ValueError("existing edition export artifacts are missing or changed")
            return manifest
        raise ValueError("edition export is immutable; choose a new directory for changed inputs")
    if output.exists() and any(output.iterdir()):
        raise ValueError("edition export directory is non-empty without a completion manifest")
    output.mkdir(parents=True, exist_ok=True)
    write_jsonl(output / "entries.jsonl", entries)
    write_jsonl(output / "paragraphs.jsonl", paragraphs)
    write_jsonl(output / "provenance.jsonl", provenance)
    write_jsonl(output / "issues.jsonl", validation.get("issues", []))
    lines = ["# Generic edition export", "", f"Edition: `{edition_id}`", f"Entries: {len(entries)}",
             f"Body paragraphs: {len(paragraphs)}", "", "The export preserves source ranges and is semantically unreviewed."]
    atomic_text(output / "report.md", "\n".join(lines) + "\n")
    # The manifest is the completion marker and is committed last.
    write_json(output / "manifest.json", manifest)
    return manifest
