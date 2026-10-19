"""Deterministic helpers shared by generic import readers."""

from __future__ import annotations

import html
import re
import unicodedata
import zipfile
from pathlib import Path
from typing import Iterable

from ..storage import digest
from ..text import strip_zero_width, zero_width_counts


def normalize_text(value: str) -> str:
    value = strip_zero_width(value.replace("\r\n", "\n").replace("\r", "\n"))
    return re.sub(r"[ \t\f\v]+", " ", value).strip()


def normalize_title(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", html.unescape(value or ""))).strip()


def detect_format(path: Path, declared: str = "auto") -> str:
    if declared != "auto":
        return "txt" if declared == "text" else declared
    suffix = path.suffix.lower()
    if zipfile.is_zipfile(path):
        try:
            with zipfile.ZipFile(path) as archive:
                return "epub" if "META-INF/container.xml" in archive.namelist() else "unsupported"
        except (OSError, zipfile.BadZipFile):
            return "unsupported"
    if suffix in {".txt", ".text"}:
        return "txt"
    return "unsupported"


def stable_block_id(source_id: str, source_hash: str, resource_id: str,
                    source_order: int, block_index: int) -> str:
    return "B" + digest({"source_id": source_id, "source_hash": source_hash,
                          "resource_id": resource_id, "source_order": source_order,
                          "block_index": block_index})[:20]


def stable_entry_id(edition_id: str, source_ranges: Iterable[dict[str, object]]) -> str:
    return f"{edition_id}:entry:{digest(list(source_ranges))[:20]}"


def source_descriptor(source, path: Path, fmt: str, encoding: str | None,
                      source_hash: str) -> dict:
    return {
        "source_id": source.source_id,
        "path": source.path,
        "display_path": source.path,
        "format": fmt,
        "input_order": source.input_order,
        "volume_hint": source.volume_hint,
        "encoding": encoding,
        "sha256": source_hash,
        "byte_count": path.stat().st_size,
    }


def clean_text_with_report(value: str) -> tuple[str, dict[str, int]]:
    counts = zero_width_counts(value)
    return strip_zero_width(value), counts


def issue(code: str, severity: str, scope: dict, message: str,
          evidence: list[str] | None = None, **extra) -> dict:
    return {"issue_id": "I" + digest({"code": code, "scope": scope,
                                        "message": message})[:16],
            "code": code, "severity": severity, "scope": scope,
            "evidence_refs": evidence or [], "message": message,
            "resolution": None, **extra}
