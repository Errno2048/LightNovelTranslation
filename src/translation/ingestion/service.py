"""Orchestration for deterministic generic input normalization."""

from __future__ import annotations

import shutil
import re
import zipfile
from xml.etree import ElementTree as ET
from pathlib import Path
from typing import Any

from ..storage import digest, file_digest, read_json, read_jsonl, write_json, write_jsonl
from .epub import extract_epub
from .export import export_edition
from .patches import patch_structure
from .schemas import InputSpec
from .structure import propose_structure as build_structure
from .txt import extract_txt
from .util import detect_format, issue
from .validation import validate_extraction, validate_structure


def _as_spec(spec: InputSpec | dict[str, Any]) -> InputSpec:
    return spec if isinstance(spec, InputSpec) else InputSpec.from_dict(spec)


def inspect_input(spec: InputSpec | dict[str, Any], *, base: Path | None = None) -> dict:
    spec = _as_spec(spec)
    base = (base or Path.cwd()).resolve()
    sources = []
    issues = []
    for source in spec.resolve(base):
        path = Path(source.path)
        row = source.to_dict()
        row["resolved_path"] = str(path)
        if not path.is_file():
            row["status"] = "missing"
            issues.append(issue("missing_source", "error", {"source_id": source.source_id}, str(path)))
            sources.append(row)
            continue
        fmt = detect_format(path, source.format)
        if fmt == "unsupported":
            row.update({"status": "unsupported", "format": fmt, "byte_count": path.stat().st_size,
                        "sha256": file_digest(path)})
            issues.append(issue("unsupported_format", "error", {"source_id": source.source_id},
                                "auto detection supports EPUB containers and TXT files"))
            sources.append(row)
            continue
        row.update({"status": "readable", "format": fmt, "byte_count": path.stat().st_size,
                    "sha256": file_digest(path)})
        sources.append(row)
    return {"schema": "import-inspection-v1", "input": spec.to_dict(),
            "sources": sources, "issues": issues,
            "status": "unsupported" if any(row.get("status") == "unsupported" for row in sources) else
                      "needs_resolution" if issues else "ready",
            "fingerprint": digest({"input": spec.to_dict(), "sources": sources, "issues": issues})}


def extract_sources(spec: InputSpec | dict[str, Any], output: Path, *, base: Path | None = None) -> dict:
    spec = _as_spec(spec)
    base = (base or Path.cwd()).resolve()
    resolved = spec.resolve(base)
    output = Path(output)
    blocks, source_rows, issues, navigation = [], [], [], []
    source_paths = {}
    for source in resolved:
        path = Path(source.path)
        if not path.is_file():
            raise FileNotFoundError(path)
        fmt = detect_format(path, source.format)
        if fmt == "epub":
            try:
                descriptor, rows, nav, local_issues = extract_epub(source, path)
            except (zipfile.BadZipFile, ET.ParseError, KeyError) as exc:
                raise ValueError(f"invalid EPUB source {source.source_id}: {type(exc).__name__}") from exc
            navigation.extend({**row, "source_id": source.source_id} for row in nav)
        elif fmt == "txt":
            descriptor, rows, local_issues = extract_txt(source, path, paragraph_policy=spec.paragraph_policy)
        else:  # pragma: no cover - detect_format currently closes the domain
            raise ValueError(f"unsupported source format: {fmt}")
        source_hash_before = file_digest(path)
        source_hash_from_extract = descriptor.get("sha256")
        if source_hash_before != source_hash_from_extract:
            raise RuntimeError(f"source changed while it was being extracted: {source.source_id}")
        original_source = next(row for row in spec.sources if row.source_id == source.source_id)
        descriptor["path"] = original_source.path
        descriptor["input_order"] = original_source.input_order
        descriptor["volume_hint"] = original_source.volume_hint
        descriptor["blob"] = f"blobs/{source_hash_before}"
        source_paths[source.source_id] = path
        source_rows.append(descriptor)
        blocks.extend(rows)
        issues.extend(local_issues)
    input_manifest = spec.to_dict()
    extraction_manifest = {"schema": "import-extraction-v1", "edition_id": spec.edition_id,
                           "language": spec.language, "profile": spec.profile,
                           "input": input_manifest, "sources": source_rows,
                           "block_count": len(blocks), "issue_count": len(issues),
                           "navigation_count": len(navigation),
                           "extraction_fingerprint": digest({"input": input_manifest,
                                                               "sources": source_rows,
                                                               "blocks": blocks,
                                                               "navigation": navigation,
                                                               "issues": issues})}
    existing_manifest = output / "manifest.json"
    if existing_manifest.exists():
        if read_json(existing_manifest) != extraction_manifest:
            raise ValueError("import output is immutable; choose a new directory for changed inputs")
        for name, expected in (("sources", source_rows), ("blocks", blocks),
                               ("issues", issues), ("navigation", navigation)):
            target = output / (name + ".jsonl")
            if not target.is_file() or read_jsonl(target) != expected:
                raise ValueError("existing import artifacts do not match the completion manifest")
        for descriptor in source_rows:
            blob = output / descriptor["blob"]
            if not blob.is_file() or file_digest(blob) != descriptor["sha256"]:
                raise ValueError("existing import source snapshot is missing or changed")
        return extraction_manifest
    if output.exists() and any(output.iterdir()):
        raise ValueError("import directory is non-empty without a completion manifest")
    output.mkdir(parents=True, exist_ok=True)
    for descriptor in source_rows:
        path = source_paths[descriptor["source_id"]]
        blob = output / descriptor["blob"]
        if not blob.exists():
            blob.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, blob)
        if file_digest(blob) != descriptor["sha256"] or file_digest(path) != descriptor["sha256"]:
            raise RuntimeError(f"source snapshot verification failed: {descriptor['source_id']}")
    write_json(output / "input-manifest.json", input_manifest)
    write_jsonl(output / "sources.jsonl", source_rows)
    write_jsonl(output / "blocks.jsonl", blocks)
    write_jsonl(output / "issues.jsonl", issues)
    write_jsonl(output / "navigation.jsonl", navigation)
    # Manifest is the completion marker and is written only after all artifacts.
    write_json(output / "manifest.json", extraction_manifest)
    return extraction_manifest


def load_extraction(folder: Path) -> dict:
    folder = Path(folder)
    manifest = read_json(folder / "manifest.json")
    return {"manifest": manifest, "sources": read_jsonl(folder / "sources.jsonl"),
            "blocks": read_jsonl(folder / "blocks.jsonl"),
            "issues": read_jsonl(folder / "issues.jsonl"),
            "navigation": read_jsonl(folder / "navigation.jsonl")}


def propose_structure(import_folder: Path) -> dict:
    import_folder = Path(import_folder)
    extraction = load_extraction(import_folder)
    structure = build_structure(extraction)
    _save_structure(import_folder, structure)
    return structure


def _save_structure(import_folder: Path, structure: dict) -> None:
    structure_dir = import_folder / "structures"
    structure_dir.mkdir(parents=True, exist_ok=True)
    path = structure_dir / (structure["fingerprint"] + ".json")
    if path.exists() and read_json(path) != structure:
        raise ValueError("immutable structure revision was changed")
    write_json(path, structure)
    write_json(import_folder / "structure.json", structure)


def load_structure(import_folder: Path, revision: str | None = None) -> dict:
    import_folder = Path(import_folder)
    if revision:
        if not re.fullmatch(r"[0-9a-f]{64}", revision):
            raise ValueError("revision must be a structure fingerprint")
        result = read_json(import_folder / "structures" / (revision + ".json"))
        if result.get("fingerprint") != revision:
            raise ValueError("structure revision does not match its filename")
        return result
    return read_json(import_folder / "structure.json")


def validate_import(import_folder: Path, revision: str | None = None) -> dict:
    import_folder = Path(import_folder)
    extraction = load_extraction(import_folder)
    structure = load_structure(import_folder, revision)
    extraction_result = validate_extraction(extraction, root=import_folder)
    structure_result = validate_structure(extraction, structure)
    issues = extraction_result["issues"] + structure_result["issues"]
    ok = extraction_result["ok"] and structure_result["ok"]
    result = {"schema": "import-validation-v1", "extraction": extraction_result,
              "structure": structure_result,
              "ok": ok, "issues": issues,
              "status": ("needs_resolution" if not ok else
                         "ready_with_warnings" if issues else "ready"),
              "fingerprint": digest({"extraction": extraction_result, "structure": structure_result})}
    write_json(import_folder / "validation.json", result)
    return result


def read_source_range(import_folder: Path, block_ids: list[str], *, context: int = 2,
                      max_blocks: int = 64, max_chars: int = 20000) -> dict:
    """Read a bounded window of frozen blocks without model calls or rebuilding."""
    if not block_ids or type(context) is not int or not 0 <= context <= 16:
        raise ValueError("supply block IDs and context between 0 and 16")
    if type(max_blocks) is not int or not 1 <= max_blocks <= 256:
        raise ValueError("max_blocks must be between 1 and 256")
    extraction = load_extraction(import_folder)
    lookup = {row["block_id"]: index for index, row in enumerate(extraction["blocks"])}
    if any(block_id not in lookup for block_id in block_ids):
        raise ValueError("unknown source block")
    selected = set()
    for block_id in block_ids:
        index = lookup[block_id]
        selected.update(range(max(0, index - context), min(len(lookup), index + context + 1)))
    if len(selected) > max_blocks:
        raise ValueError("requested source range exceeds max_blocks")
    if sum(len(extraction["blocks"][index]["raw_text"]) for index in selected) > max_chars:
        raise ValueError("requested source range exceeds max_chars")
    return {"schema": "import-source-range-v1",
            "extraction_fingerprint": extraction["manifest"]["extraction_fingerprint"],
            "requested_block_ids": block_ids,
            "blocks": [extraction["blocks"][index] for index in sorted(selected)]}


def apply_structure_patch(import_folder: Path, patch: dict) -> dict:
    import_folder = Path(import_folder)
    extraction = load_extraction(import_folder)
    if not validate_extraction(extraction, root=import_folder)["ok"]:
        raise ValueError("resolve extraction errors before structural patching")
    base = load_structure(import_folder)
    patch_path = import_folder / "patches" / (digest(patch) + ".json")
    if patch_path.exists():
        saved = read_json(patch_path)
        if saved["patch"] == patch and saved["result_revision"] == base["fingerprint"]:
            return base
        raise ValueError("stale structure patch replay")
    if (base.get("patch_id") == "P" + digest(patch)[:20] and
            base.get("parent_structure_revision") == patch.get("base_structure_revision")):
        write_json(patch_path, {"patch": patch, "result_revision": base["fingerprint"]})
        return base
    structure = patch_structure(extraction, base, patch)
    # Save immutable result before advancing the current revision; a failed
    # operation never writes a partly modified structural snapshot.
    _save_structure(import_folder, structure)
    write_json(patch_path, {"patch": patch, "result_revision": structure["fingerprint"]})
    return structure


def normalize_input(spec: InputSpec | dict[str, Any], output: Path, *, base: Path | None = None) -> dict:
    """Run the deterministic minimum pipeline, exporting only validated inputs."""
    output = Path(output)
    extraction_folder = output / "extraction"
    extract_sources(spec, extraction_folder, base=base)
    # Replays retain an explicitly patched head rather than resetting it.
    if not (extraction_folder / "structure.json").exists():
        propose_structure(extraction_folder)
    validation = validate_import(extraction_folder)
    result = {"schema": "import-run-v1", "status": validation["status"],
              "ok": validation["ok"], "import_folder": str(extraction_folder),
              "structure_fingerprint": validation["structure"]["structure_fingerprint"],
              "issues": validation["issues"]}
    if validation["ok"]:
        result["export"] = export_corpus(extraction_folder, output / "corpus")
        result["corpus_folder"] = str(output / "corpus")
    write_json(output / "result.json", result)
    return result


def export_corpus(import_folder: Path, output: Path, revision: str | None = None) -> dict:
    import_folder = Path(import_folder)
    extraction = load_extraction(import_folder)
    structure = load_structure(import_folder, revision)
    validation = validate_import(import_folder, revision)
    return export_edition(extraction, structure, validation, Path(output))
