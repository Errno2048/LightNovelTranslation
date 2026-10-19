"""Small serializable contracts for generic input normalization."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


SCHEMA = "novel-import-input-v1"


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    path: str
    format: str = "auto"
    input_order: int = 0
    volume_hint: str | None = None
    encoding_override: str | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SourceSpec":
        if not isinstance(value, dict):
            raise ValueError("source must be an object")
        source_id = str(value.get("source_id", "")).strip()
        path = str(value.get("path", "")).strip()
        if not source_id or not path:
            raise ValueError("source_id and path are required")
        order = value.get("input_order", 0)
        if type(order) is not int or order < 0:
            raise ValueError("input_order must be a non-negative integer")
        fmt = str(value.get("format", "auto")).lower().strip()
        if fmt not in {"auto", "epub", "txt", "text"}:
            raise ValueError(f"unsupported source format: {fmt}")
        for name in ("volume_hint", "encoding_override"):
            if value.get(name) is not None and (not isinstance(value[name], str) or not value[name].strip()):
                raise ValueError(name + " must be a non-empty string when supplied")
        return cls(source_id, path, fmt, order,
                   value.get("volume_hint"), value.get("encoding_override"))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class InputSpec:
    edition_id: str
    language: str
    edition_label: str
    sources: tuple[SourceSpec, ...]
    profile: str = "generic-v1"
    version_relation: str | None = None
    encoding_policy: str = "strict-auto"
    paragraph_policy: str = "auto"
    cleanup_profile: str = "zero-width-v1"
    schema: str = SCHEMA

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "InputSpec":
        if not isinstance(value, dict):
            raise ValueError("input manifest must be an object")
        schema = value.get("schema", SCHEMA)
        if schema != SCHEMA:
            raise ValueError(f"unsupported input schema: {schema}")
        edition_id = str(value.get("edition_id", "")).strip()
        language = str(value.get("language", "")).strip().lower()
        label = str(value.get("edition_label", edition_id)).strip()
        if not edition_id or language not in {"ja", "zh"}:
            raise ValueError("edition_id and a supported language are required")
        raw_sources = value.get("sources")
        if not isinstance(raw_sources, list) or not raw_sources:
            raise ValueError("sources must be a non-empty list")
        sources = tuple(sorted((SourceSpec.from_dict(row) for row in raw_sources),
                               key=lambda row: (row.input_order, row.source_id)))
        if len({row.source_id for row in sources}) != len(sources):
            raise ValueError("duplicate source_id")
        if len({row.input_order for row in sources}) != len(sources):
            raise ValueError("duplicate input_order")
        paragraph_policy = str(value.get("paragraph_policy", "auto"))
        if paragraph_policy not in {"auto", "blank_line", "line", "wrapped"}:
            raise ValueError("unknown paragraph_policy")
        if value.get("profile", "generic-v1") != "generic-v1":
            raise ValueError("unknown import profile")
        if value.get("encoding_policy", "strict-auto") != "strict-auto":
            raise ValueError("unknown encoding_policy")
        if value.get("cleanup_profile", "zero-width-v1") != "zero-width-v1":
            raise ValueError("unknown cleanup_profile")
        return cls(edition_id, language, label, sources,
                   str(value.get("profile", "generic-v1")),
                   value.get("version_relation"),
                   str(value.get("encoding_policy", "strict-auto")),
                   paragraph_policy,
                   str(value.get("cleanup_profile", "zero-width-v1")),
                   schema)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["sources"] = [source.to_dict() for source in self.sources]
        return value

    def resolve(self, base: Path) -> tuple[SourceSpec, ...]:
        """Return source specs with paths resolved for reading.

        The resolved absolute path is an execution detail and is not used as
        the content identity written to artifacts.
        """
        resolved = []
        for source in self.sources:
            path = Path(source.path)
            if not path.is_absolute():
                path = (base / path).resolve()
            resolved.append(SourceSpec(source.source_id, str(path), source.format,
                                       source.input_order, source.volume_hint,
                                       source.encoding_override))
        return tuple(resolved)


@dataclass(frozen=True)
class Extraction:
    manifest: dict[str, Any]
    sources: tuple[dict[str, Any], ...]
    blocks: tuple[dict[str, Any], ...]
    issues: tuple[dict[str, Any], ...]
    navigation: tuple[dict[str, Any], ...] = field(default_factory=tuple)

