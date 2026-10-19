"""Versioned P6 records; uncertain model output is never a confirmed fact."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from ..storage import digest

SCHEMA_VERSION = "p6-state-v1"
RECORD_TYPES = {"SCENE", "EVENT", "STATE", "PERSPECTIVE", "USAGE", "UTTERANCE", "NEED", "SUMMARY"}
REVIEW_STATES = {"candidate", "reviewed", "rejected", "retracted"}
PERSPECTIVES = {"world", "claim", "belief", "suspicion", "explicit_unknown", "learned"}
CHANNELS = {"speech", "inner_thought", "telepathy", "reported_speech", "quoted_speech", "unknown"}


class P6Error(ValueError):
    def __init__(self, kind: str, detail: str):
        self.kind = kind
        super().__init__(f"{kind}: {detail}")


def record_id(kind: str, value: dict) -> str:
    stable = {k: v for k, v in value.items() if k not in {"record_id", "chapter_id", "disclosure_order", "review_status", "review"}}
    return f"p6-{kind.lower()}:{digest(stable)[:24]}"


def validate_record(value: dict, *, allowed_refs: dict[str, str] | None = None) -> dict:
    """Validate shape, scope, policy-bound citations, and exact cited text."""
    if not isinstance(value, dict) or value.get("type") not in RECORD_TYPES:
        raise P6Error("invalid_record", "unknown or malformed record type")
    if value.get("schema") != SCHEMA_VERSION:
        raise P6Error("invalid_record", "unsupported schema version")
    ref = value.get("evidence_ref")
    quote = value.get("quote")
    if not isinstance(ref, str) or not isinstance(quote, str) or not quote.strip():
        raise P6Error("invalid_record", "each record requires one exact evidence ref and quote")
    if allowed_refs is not None:
        if ref not in allowed_refs:
            raise P6Error("visibility_denied", "record cites evidence outside this analysis input")
        if quote not in allowed_refs[ref]:
            raise P6Error("stale_evidence", "citation quote is not an exact substring of cited paragraph")
    if value["type"] in {"STATE", "EVENT", "PERSPECTIVE"}:
        branch = value.get("branch", "main")
        if not isinstance(branch, str) or not branch:
            raise P6Error("invalid_record", "story branch required")
        if value.get("perspective", "world") not in PERSPECTIVES:
            raise P6Error("invalid_record", "unsupported epistemic perspective")
    if value["type"] == "STATE":
        if not all(isinstance(value.get(k), str) and value[k].strip() for k in ("subject", "predicate", "object")):
            raise P6Error("invalid_record", "state subject, predicate and object required")
        if value.get("end_status", "unknown") not in {"unknown", "known", "observed_open"}:
            raise P6Error("invalid_record", "invalid state endpoint status")
        if value.get("end_status") == "known" and not value.get("valid_to"):
            raise P6Error("invalid_record", "known state end requires valid_to")
        if value.get("end_status") != "known" and value.get("valid_to") is not None:
            raise P6Error("invalid_record", "unknown/open state cannot claim valid_to")
        if value.get("predicate") not in {"location", "presence", "membership", "relation", "possession", "capability"}:
            raise P6Error("unsupported_predicate", "state predicate is outside the registered P6 set")
        if value.get("polarity", "affirmed") not in {"affirmed", "negated"}:
            raise P6Error("invalid_record", "state polarity must be affirmed or negated")
    if value["type"] == "PERSPECTIVE":
        if not all(isinstance(value.get(k), str) and value[k].strip() for k in ("person", "proposition")):
            raise P6Error("invalid_record", "perspective person and proposition required")
    if value["type"] == "EVENT":
        if not isinstance(value.get("event"), str) or not value["event"].strip():
            raise P6Error("invalid_record", "event description required")
        if value.get("relation") in {"before", "after", "same", "during", "overlaps"} and not all(value.get(k) for k in ("left_anchor", "right_anchor")):
            raise P6Error("invalid_record", "temporal relation requires both anchors")
    if value["type"] == "USAGE":
        if not all(isinstance(value.get(k), str) and value[k].strip() for k in ("source_form", "target_form", "translation_ref", "translation_quote")):
            raise P6Error("invalid_record", "observed usage needs source/target forms and paired exact Chinese evidence")
    if value["type"] == "UTTERANCE":
        if value.get("channel", "unknown") not in CHANNELS:
            raise P6Error("invalid_record", "unsupported utterance channel")
        if not isinstance(value.get("speaker_candidates", []), list):
            raise P6Error("invalid_record", "speaker candidates must remain a list")
        for candidate in value.get("speaker_candidates", []):
            if not isinstance(candidate, dict) or not candidate.get("entity") or candidate.get("status") not in {"explicit", "inferred", "ambiguous", "unsupported"}:
                raise P6Error("invalid_record", "malformed dialogue attribution candidate")
    return {**value, "record_id": record_id(value["type"], value)}


@dataclass(frozen=True)
class StoryTime:
    """Story-time anchor, deliberately separate from disclosure chapter."""
    anchor_id: str
    branch: str = "main"
    disclosure_order: int | None = None
    explicit_date: str | None = None

    def __post_init__(self):
        if not self.anchor_id or not self.branch:
            raise P6Error("invalid_record", "anchor and branch required")
        if self.disclosure_order is not None and (type(self.disclosure_order) is not int or self.disclosure_order < 0):
            raise P6Error("invalid_record", "disclosure order must be a nonnegative integer")


@dataclass
class StateBundle:
    version: str = SCHEMA_VERSION
    facts: list[dict] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)
    observed_usages: list[dict] = field(default_factory=list)
    utterances: list[dict] = field(default_factory=list)
    unresolved: list[dict] = field(default_factory=list)
    dependencies: list[dict] = field(default_factory=list)
    coverage: str = "unknown"
    omitted: list[dict] = field(default_factory=list)

    def fingerprint(self):
        return digest(asdict(self))
