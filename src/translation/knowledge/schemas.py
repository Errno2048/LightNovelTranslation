"""P4 contracts; story scope, disclosure position and revision are independent."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from ..storage import digest

VERSION = "knowledge-v1"


class KnowledgeError(ValueError):
    def __init__(self, kind: str, detail: str):
        self.kind = kind
        super().__init__(f"{kind}: {detail}")


@dataclass
class QueryPolicy:
    mode: str
    hidden_source_ids: list[str] = field(default_factory=list)
    reserved_source_ids: list[str] = field(default_factory=list)
    cutoff_order: int | None = None
    allowed_translation_source_ids: list[str] | None = None
    allow_future_source: bool = False

    def __post_init__(self):
        if self.mode not in {"single_gap", "block_gap", "sequential"}:
            raise KnowledgeError("visibility_denied", "invalid mode")
        if self.mode == "sequential" and self.cutoff_order is None:
            raise KnowledgeError("visibility_denied", "sequential cutoff required")
        if self.allow_future_source:
            raise KnowledgeError("unsupported_query", "future source lookahead is not implemented")

    def public(self):
        return asdict(self)

    @property
    def fingerprint(self):
        return digest(self.public())

    @classmethod
    def from_legacy(cls, policy, *, allowed_translation_source_ids=None):
        data = asdict(policy) if hasattr(policy, "__dataclass_fields__") else dict(policy)
        return cls(**data, allowed_translation_source_ids=allowed_translation_source_ids)


def validate_scope(scope: dict[str, Any]):
    allowed = {"source_start", "source_end", "speaker", "addressee"}
    if set(scope) - allowed:
        raise KnowledgeError("unsupported_query", "unknown convention scope fields")
    for name in ("source_start", "source_end"):
        if name in scope and (type(scope[name]) is not int or scope[name] < 0):
            raise KnowledgeError("invalid_record", "scope order must be nonnegative integer")
    if scope.get("source_end", float("inf")) < scope.get("source_start", 0):
        raise KnowledgeError("invalid_record", "reversed convention interval")


def validate_story_scope(scope: dict):
    """No inferred timestamps. Unknown endpoints never imply forever."""
    if set(scope) - {"kind", "start", "end", "end_status", "constraints"}:
        raise KnowledgeError("invalid_record", "unknown story scope fields")
    if scope.get("kind") not in {"unknown", "interval", "relative"}:
        raise KnowledgeError("invalid_record", "invalid story scope kind")
    if scope.get("end_status") not in {"unknown", "known", "observed_open"}:
        raise KnowledgeError("invalid_record", "explicit endpoint status required")
    if scope["end_status"] == "known" and scope.get("end") is None:
        raise KnowledgeError("invalid_record", "known end requires endpoint")
    if scope["end_status"] != "known" and scope.get("end") is not None:
        raise KnowledgeError("invalid_record", "unknown/open end cannot carry endpoint")
    if scope["kind"] == "interval" and scope.get("start") is None:
        raise KnowledgeError("invalid_record", "interval start required")
    for c in scope.get("constraints", []):
        if not isinstance(c, dict) or c.get("relation") not in {"before", "after", "during", "overlaps"} or not c.get("anchor"):
            raise KnowledgeError("invalid_record", "invalid relative time constraint")


def scope_applicability(scope: dict, order: int, *, speaker=None, addressee=None):
    validate_scope(scope)
    if order < scope.get("source_start", 0) or order > scope.get("source_end", float("inf")):
        return "not_applicable"
    for name, actual in (("speaker", speaker), ("addressee", addressee)):
        if scope.get(name) and actual is None:
            return "uncertain"
        if scope.get(name) and scope[name] != actual:
            return "not_applicable"
    return "applicable"


def validate_item(item: dict):
    if item.get("kind") not in {"convention", "entity", "assertion"}:
        raise KnowledgeError("invalid_record", "unsupported item kind")
    if not item.get("id") or not item.get("evidence_ids") or not isinstance(item.get("source_dependencies"), list):
        raise KnowledgeError("invalid_record", "item identity, evidence and dependencies required")
    if item["kind"] == "convention":
        if item.get("category") not in {"term", "name", "address", "style", "voice"}:
            raise KnowledgeError("invalid_record", "invalid convention category")
        if not isinstance(item.get("source_form"), str) or not item["source_form"].strip():
            raise KnowledgeError("invalid_record", "source form required")
        if not isinstance(item.get("target_form"), str) or not item["target_form"].strip():
            raise KnowledgeError("invalid_record", "target form required")
        if item.get("strength", "preferred") not in {"required", "preferred", "example"}:
            raise KnowledgeError("invalid_record", "invalid constraint strength")
        if not isinstance(item.get("variants", []), list) or any(not isinstance(x, str) or not x.strip() for x in item.get("variants", [])):
            raise KnowledgeError("invalid_record", "invalid variants")
        validate_scope(item.get("scope", {}))
    if item["kind"] == "assertion":
        validate_story_scope(item.get("story_scope", {}))
        if item.get("perspective") not in {"narration", "claim", "belief", "inference"}:
            raise KnowledgeError("invalid_record", "perspective required")
        if not all(item.get(x) for x in ("subject", "predicate", "object")):
            raise KnowledgeError("invalid_record", "assertion triple required")
    if item["kind"] == "entity" and not isinstance(item.get("aliases"), list):
        raise KnowledgeError("invalid_record", "entity aliases required")
