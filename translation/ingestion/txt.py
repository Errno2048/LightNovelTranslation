"""Strict text-file decoding and source-located line extraction."""

from __future__ import annotations

import codecs
import hashlib
from pathlib import Path

from ..text import strip_zero_width, zero_width_counts
from .util import stable_block_id


def decode_text(data: bytes, *, override: str | None = None) -> tuple[str, str, list[dict]]:
    candidates: list[str] = []
    if override:
        try:
            return data.decode(override, errors="strict"), override, []
        except (LookupError, UnicodeDecodeError) as exc:
            raise ValueError("declared text encoding cannot decode the input") from exc
    if data.startswith(codecs.BOM_UTF8):
        candidates.append("utf-8-sig")
    elif data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        candidates.append("utf-32")
    elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        candidates.append("utf-16")
    if candidates:
        codec = candidates[0]
        return data.decode(codec, errors="strict"), codec, []
    try:
        return data.decode("utf-8", errors="strict"), "utf-8", []
    except UnicodeDecodeError:
        pass
    candidates.extend(["cp932", "gb18030", "big5"])
    results: list[tuple[str, str]] = []
    seen = set()
    for codec in candidates:
        if codec.lower() in seen:
            continue
        seen.add(codec.lower())
        try:
            text = data.decode(codec, errors="strict")
        except (LookupError, UnicodeDecodeError):
            continue
        if "\ufffd" not in text:
            results.append((codec, text))
    if not results:
        raise ValueError("unable to decode text input without replacement characters")
    chosen_codec, chosen_text = results[0]
    alternatives = []
    for codec, text in results[1:]:
        if text != chosen_text:
            alternatives.append({"codec": codec, "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
    issues = []
    if alternatives and override is None and not data.startswith((codecs.BOM_UTF8, codecs.BOM_UTF16_LE,
                                                                   codecs.BOM_UTF16_BE, codecs.BOM_UTF32_LE,
                                                                   codecs.BOM_UTF32_BE)):
        issues.append({"code": "encoding_ambiguous", "chosen_codec": chosen_codec,
                       "severity": "error",
                       "alternative_decodings": alternatives})
    if chosen_codec.lower() not in {"utf-8", "utf-8-sig"}:
        issues.append({"code": "non_utf8_text", "codec": chosen_codec})
    return chosen_text, chosen_codec, issues


def _physical_lines(text: str):
    offset = 0
    for line_number, line in enumerate(text.splitlines(keepends=True), 1):
        body = line.rstrip("\r\n")
        start, end = offset, offset + len(body)
        yield line_number, start, end, body
        offset += len(line)
    if not text or (text and not text.endswith(("\n", "\r"))):
        return


def extract_txt(source, path: Path, *, paragraph_policy: str = "auto") -> tuple[dict, list[dict], list[dict]]:
    raw = path.read_bytes()
    source_hash = hashlib.sha256(raw).hexdigest()
    text, codec, issues = decode_text(raw, override=source.encoding_override)
    physical_lines = list(_physical_lines(text))
    has_blank_lines = any(not row[3].strip() for row in physical_lines)
    resolved_policy = paragraph_policy
    if paragraph_policy == "auto":
        # Blank lines alone do not prove that the intervening lines are wraps.
        # Keep physical lines until an explicit paragraph policy is chosen.
        resolved_policy = "line"
        issues.append({"code": "paragraph_policy_ambiguous", "severity": "warning",
                       "source_id": source.source_id,
                       "message": "physical lines were preserved; select blank_line or wrapped to merge them"})
    blocks = []
    removed_total = {}
    for index, (line, start, end, value) in enumerate(physical_lines):
        counts = zero_width_counts(value)
        for char, count in counts.items():
            removed_total[char] = removed_total.get(char, 0) + count
        cleaned = strip_zero_width(value)
        block_id = stable_block_id(source.source_id, source_hash, "text", index, index)
        blocks.append({
            "block_id": block_id, "source_id": source.source_id,
            "source_sha256": source_hash, "resource_id": "text",
            "source_order": index, "node_kind": "line" if cleaned.strip() else "separator",
            "raw_text": cleaned, "normalized_text": cleaned.strip(),
            "locator": {"resource_id": "text", "line_start": line,
                        "line_end": line, "char_start": start, "char_end": end},
            "ruby": [], "layout_hints": {"encoding": codec, "blank_line": not bool(cleaned.strip()),
                "paragraph_policy": resolved_policy,
                "zero_width_removed": counts},
        })
    if removed_total:
        issues.append({"code": "zero_width_removed", "counts": removed_total,
                       "source_id": source.source_id})
    if not any(row["raw_text"].strip() for row in blocks):
        issues.append({"code": "no_text_blocks", "source_id": source.source_id})
    return ({"source_id": source.source_id, "path": source.path, "format": "txt",
             "sha256": source_hash, "byte_count": len(raw), "encoding": codec,
             "line_count": max(0, len(text.splitlines())),
             "has_blank_lines": has_blank_lines,
             "paragraph_policy": resolved_policy}, blocks, issues)
