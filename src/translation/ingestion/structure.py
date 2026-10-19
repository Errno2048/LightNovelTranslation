"""Deterministic chapter and paragraph structure proposals."""

from __future__ import annotations

import re
from collections import defaultdict

from ..storage import digest
from .util import normalize_title, stable_entry_id


# Keep the construction ASCII-safe on Windows and make the rules explicit.
_CJK_NUMERALS = "\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u767e\u5343\u4e07\u96f6\u3007"
_CHAPTER_UNITS = "\u7ae0\u8a71\u8bdd\u56de\u7bc0\u8282\u5dfb\u5377\u7de8\u7bc7\u90e8\u5e55"
_SPECIAL_HEADINGS = (
    "\u5e8f\u7ae0", "\u7d42\u7ae0", "\u7d42\u8a71", "\u7d42\u5e55", "\u5e8f\u5e55",
    "\u9591\u8a71", "\u9593\u8a71", "\u5e55\u9593", "\u756a\u5916", "\u756a\u5916\u7de8",
    "\u4f59\u8a71", "\u5f8c\u65e5\u8ac7", "\u5f8c\u65e5\u8b5a", "\u30d7\u30ed\u30ed\u30fc\u30b0",
    "\u30a8\u30d4\u30ed\u30fc\u30b0",
    "\u7ec8\u7ae0", "\u7ec8\u8bdd", "\u95f4\u7ae0", "\u5e55\u95f4", "\u540e\u65e5\u8c08",
)
_HEADING_PATTERNS = (
    re.compile(r"^\u7b2c\s*[0-9\uff10-\uff19" + re.escape(_CJK_NUMERALS) +
               r"]+\s*[" + re.escape(_CHAPTER_UNITS) +
               r"](?=$|[\s:\uff1a\u300c\u300e\u3010\uff08(\uff0e.\u3001-])", re.I),
    re.compile(r"^(?:" + "|".join(re.escape(value) for value in _SPECIAL_HEADINGS) +
               r")(?=$|[\s\u3000:\uff1a\u300c\u300e\u3010\uff08(\uff0e.\u3001-])", re.I),
    re.compile(r"^(?:chapter|ch\.?|part)\s*[0-9ivxlc]+\b", re.I),
    re.compile(r"^(?:S|Y|R|K|B)\s*[0-9]+(?:\s|$)", re.I),
)
_NUMBERED_LINE = re.compile(
    r"^(?:\d{1,4}[.\u3001:\uff1a\- ]|[\uff10-\uff19]{1,4}[.\u3001:\uff1a\- ]|["
    + re.escape(_CJK_NUMERALS) + r"]+[.\u3001])"
)
_REFERENCE_TITLE = re.compile(
    r"^(?:0+\s*)?(?:\u30ad\u30e3\u30e9\u30af\u30bf\u30fc\u4e00\u89a7|"
    r"\u4eba\u7269\u4e00\u89a7|\u4eba\u7269\u7d39\u4ecb|\u30b9\u30ad\u30eb\u4e00\u89a7|"
    r"\u7528\u8a9e\u96c6|\u7528\u8a9e\u4e00\u89a7|\u8a2d\u5b9a\u8cc7\u6599|"
    r"\u76ee\u6b21|\u4f5c\u54c1\u95a2\u9023)(?=$|[\s&\uff06])"
)
_PREFACE_TITLE = re.compile(r"^(?:\u5e8f\u7ae0|\u30d7\u30ed\u30ed\u30fc\u30b0|\u306f\u3058\u3081\u306b|\u524d\u66f8\u304d)")
_AFTERWORD_TITLE = re.compile(r"^(?:\u7d42\u7ae0|\u7d42\u8a71|\u30a8\u30d4\u30ed\u30fc\u30b0|\u3042\u3068\u304c\u304d|\u5f8c\u66f8\u304d)")


def _entry_kind(title: str, metadata_title: str = "") -> str:
    value = normalize_title(title)
    normalized_book = normalize_title(metadata_title)
    if value and normalized_book and value == normalized_book:
        return "volume_intro"
    if _REFERENCE_TITLE.search(value):
        return "reference"
    if _PREFACE_TITLE.search(value):
        return "narrative_preface"
    if _AFTERWORD_TITLE.search(value):
        return "narrative_afterword"
    if value and re.match(r"^(?:S|Y|R|K|B)\s*[0-9]+(?:\s|$)", value, re.I):
        return "narrative_side"
    return "narrative"


def looks_like_heading(text: str, *, node_kind: str = "", tag: str = "") -> bool:
    value = normalize_title(text)
    if not value or len(value) > 180:
        return False
    if tag == "h1":
        return True
    if any(pattern.search(value) for pattern in _HEADING_PATTERNS):
        return True
    # A short all-caps/numbered line is useful for TXT, but avoid treating
    # ordinary short dialogue as a heading.
    if len(value) <= 80 and (tag in {"h2", "h3"} or _NUMBERED_LINE.match(value)):
        return True
    return False


def _title_for(blocks: list[dict], title_block_id: str | None, nav_label: str | None) -> tuple[str, str]:
    if nav_label and normalize_title(nav_label):
        return nav_label.strip(), "navigation"
    if title_block_id:
        block = next((row for row in blocks if row["block_id"] == title_block_id), None)
        if block:
            return block["raw_text"].strip(), "body_heading"
    return "", "unknown"


def _candidate_boundaries(blocks: list[dict], navigation: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return block-level boundary candidates and issues.

    A resource transition without a new heading is treated as a possible
    continuation.  This permits split chapters while retaining an explicit
    issue for cases that need human/agent confirmation.
    """
    by_resource = defaultdict(list)
    for row in blocks:
        by_resource[(row["source_id"], row["resource_id"])].append(row)
    positions = {row["block_id"]: i for i, row in enumerate(blocks)}
    first_by_resource = {key: next(row["block_id"] for row in rows if row["raw_text"].strip())
                         for key, rows in by_resource.items() if any(row["raw_text"].strip() for row in rows)}
    anchor_targets = {}
    for row in blocks:
        locator = row.get("locator", {})
        resource = (row["source_id"], row["resource_id"])
        anchors = set(locator.get("anchor_chain", [])) | set(locator.get("anchors", []))
        if locator.get("anchor"):
            anchors.add(locator["anchor"])
        for anchor in anchors:
            anchor_targets.setdefault((*resource, anchor), row["block_id"])
    nav_by_block = defaultdict(list)
    nav_parent_blocks = set()
    issues = []
    nav_positions = []
    for nav in navigation:
        if nav.get("has_children"):
            key = (nav.get("source_id"), nav.get("href"), nav.get("fragment"))
            parent_block = anchor_targets.get(key) if key[2] else first_by_resource.get((key[0], key[1]))
            if parent_block:
                nav_parent_blocks.add(parent_block)
            issues.append({"code": "navigation_parent_deferred", "severity": "warning",
                           "scope": {"source_id": nav.get("source_id"), "resource_id": nav.get("href")},
                           "label": nav.get("label", "")})
            continue
        key = (nav.get("source_id"), nav.get("href"), nav.get("fragment"))
        block_id = anchor_targets.get(key) if nav.get("fragment") else first_by_resource.get((key[0], key[1]))
        if block_id:
            nav_by_block[block_id].append(nav)
            nav_positions.append(positions[block_id])
        else:
            issues.append({"code": "navigation_target_missing", "severity": "warning",
                           "scope": {"source_id": key[0], "resource_id": key[1]},
                           "fragment": key[2]})
    if nav_positions != sorted(nav_positions):
        issues.append({"code": "navigation_order_conflict", "severity": "error",
                       "message": "navigation targets disagree with the frozen spine/block reading order"})
    navigation_resources = {(row["source_id"], row["resource_id"])
                            for row in blocks if row["block_id"] in nav_by_block}
    candidates, seen = [], set()
    for row in blocks:
        text = row.get("raw_text", "")
        locator = row.get("locator", {})
        heading = looks_like_heading(text, node_kind=row.get("node_kind", ""), tag=locator.get("tag", ""))
        explicit_heading = any(pattern.search(normalize_title(text)) for pattern in _HEADING_PATTERNS)
        if row["block_id"] in nav_parent_blocks and not explicit_heading:
            heading = False
        nav_rows = nav_by_block.get(row["block_id"], [])
        if nav_rows and row.get("node_kind") != "heading":
            # Navigation commonly points at the first body paragraph rather
            # than a visible title. Do not remove that paragraph from the body.
            heading = any(normalize_title(text) == normalize_title(nav.get("label", ""))
                          for nav in nav_rows)
        # Numbered prose, skill lists and countdowns are not chapter evidence.
        # In a navigated resource, infer additional boundaries only from actual
        # heading elements. Weak textual candidates stay available for review.
        if row.get("node_kind") in {"list_item", "table_row", "table"}:
            heading = False
        has_navigation = (row["source_id"], row["resource_id"]) in navigation_resources
        navigated_prose = has_navigation and row.get("node_kind") != "heading" and not nav_rows
        if heading or nav_rows:
            identity = row["block_id"]
            if identity in seen:
                continue
            seen.add(identity)
            labels = list(dict.fromkeys(r.get("label", "") for r in nav_rows if r.get("label")))
            if len(labels) > 1:
                issues.append({"code": "navigation_label_conflict", "severity": "warning",
                               "scope": {"block_id": identity}, "labels": labels})
            if heading and labels and all(normalize_title(label) != normalize_title(text) for label in labels):
                issues.append({"code": "navigation_body_title_conflict", "severity": "warning",
                               "scope": {"block_id": identity}, "labels": labels,
                               "body_title": normalize_title(text)})
            strong_heading = (explicit_heading or locator.get("tag") == "h1") and not navigated_prose
            candidates.append({
                "candidate_id": "C" + digest({"block_id": identity, "labels": labels})[:16],
                "kind": "chapter",
                "start_anchor": {"block_id": identity, "resource_id": row["resource_id"],
                                 "block_index": row["locator"].get("block_index")},
                "title_candidates": ([{"text": labels[0], "source": "navigation"}] if labels else []) +
                                    ([{"text": text.strip(), "source": "body_heading"}] if heading else []),
                "evidence": ([{"type": "navigation", "ref": row["block_id"]}] if nav_rows else []) +
                            ([{"type": "heading", "ref": row["block_id"]}] if heading else []),
                "conflicts": [],
                "decision_state": "accepted" if nav_rows or strong_heading else "candidate",
            })
    resources = list(by_resource)
    for resource in resources:
        first = by_resource[resource][0]
        if resource != next(iter(by_resource)) and first.get("block_id") not in {r["start_anchor"]["block_id"] for r in candidates}:
            issues.append({"code": "resource_continuation_ambiguous", "severity": "warning",
                           "scope": {"source_id": resource[0], "resource_id": resource[1]},
                           "message": "resource starts without a chapter heading; treated as continuation when possible"})
    return sorted(candidates, key=lambda row: positions[row["start_anchor"]["block_id"]]), issues


def propose_structure(extraction: dict) -> dict:
    blocks = list(extraction.get("blocks", []))
    navigation = list(extraction.get("navigation", []))
    if not blocks:
        structure = {"schema": "import-structure-v1", "rules_version": "generic-structure-v1",
                     "edition_id": extraction["manifest"]["edition_id"],
                     "extraction_fingerprint": extraction["manifest"]["extraction_fingerprint"],
                     "status": "needs_resolution", "entries": [], "candidates": [],
                     "issues": [{"code": "no_blocks", "severity": "error"}]}
        structure["fingerprint"] = digest(structure)
        return structure
    all_candidates, issues = _candidate_boundaries(blocks, navigation)
    candidates = [row for row in all_candidates if row["decision_state"] == "accepted"]
    block_index = {row["block_id"]: i for i, row in enumerate(blocks)}
    # A source with no recognizable heading still becomes one unresolved entry
    # so its text is available for later review instead of being discarded.
    candidate_blocks = {row["start_anchor"]["block_id"] for row in candidates}
    first_by_source = {}
    for row in blocks:
        first_by_source.setdefault(row["source_id"], row)
    for row in first_by_source.values():
        if row["block_id"] not in candidate_blocks:
            source_candidates = [candidate for candidate in candidates
                                 if blocks[block_index[candidate["start_anchor"]["block_id"]]]["source_id"] == row["source_id"]]
            first_candidate = min(source_candidates, key=lambda item: block_index[item["start_anchor"]["block_id"]]) if source_candidates else None
            if first_candidate is not None:
                start = block_index[row["block_id"]]
                end = block_index[first_candidate["start_anchor"]["block_id"]]
                if not any(block["raw_text"].strip() for block in blocks[start:end]):
                    first_candidate["start_anchor"] = {"block_id": row["block_id"], "resource_id": row["resource_id"],
                                                       "block_index": row["locator"].get("block_index")}
                    continue
            candidates.append({
                "candidate_id": "C" + digest({"block_id": row["block_id"], "kind": "source_start"})[:16],
                "kind": "chapter",
                "start_anchor": {"block_id": row["block_id"], "resource_id": row["resource_id"],
                                 "block_index": row["locator"].get("block_index")},
                "title_candidates": [],
                "evidence": [{"type": "source_start", "ref": row["block_id"]}],
                "conflicts": [],
                "decision_state": "unresolved",
            })
    # Every input source has a start boundary, even if it has no title.
    unique = {row["start_anchor"]["block_id"]: row for row in candidates}
    candidates = sorted(unique.values(), key=lambda row: block_index[row["start_anchor"]["block_id"]])
    entries = []
    source_by_id = {row["source_id"]: row for row in extraction.get("sources", [])}
    for position, candidate in enumerate(candidates):
        start = block_index[candidate["start_anchor"]["block_id"]]
        end = block_index[candidates[position + 1]["start_anchor"]["block_id"]] if position + 1 < len(candidates) else len(blocks)
        assigned = blocks[start:end]
        if not assigned:
            continue
        title_candidates = candidate.get("title_candidates", [])
        nav_title = next((row["text"] for row in title_candidates if row["source"] == "navigation"), None)
        heading_block = next((row for row in assigned if any(e["ref"] == row["block_id"] and e["type"] == "heading"
                                                              for e in candidate.get("evidence", []))), None)
        raw_title, title_source = _title_for(assigned, heading_block["block_id"] if heading_block else None, nav_title)
        source_ranges = [{"source_id": row["source_id"], "source_sha256": row["source_sha256"],
                          "resource_id": row["resource_id"], "block_id": row["block_id"],
                          "locator": row["locator"]}
                         for row in assigned]
        unresolved = candidate["decision_state"] != "accepted" or not raw_title
        source_metadata = source_by_id.get(assigned[0]["source_id"], {})
        kind = _entry_kind(raw_title, source_metadata.get("metadata_title", ""))
        volume_label = source_metadata.get("volume_hint")
        volume_id = (extraction["manifest"]["edition_id"] + ":volume:" + digest(volume_label)[:16]
                     if volume_label else None)
        entries.append({
            "entry_id": stable_entry_id(extraction["manifest"]["edition_id"], source_ranges),
            "logical_entry_id": stable_entry_id(extraction["manifest"]["edition_id"], source_ranges),
            "raw_title": raw_title,
            "display_title": raw_title or f"未命名阅读条目 {position + 1}",
            "title_source": title_source if raw_title else "generated_label",
            "kind": kind,
            "boundary_kind": "unresolved" if unresolved else "chapter",
            "reading_order": position,
            "source_ranges": source_ranges,
            "block_ids": [row["block_id"] for row in assigned],
            "title_block_id": heading_block["block_id"] if heading_block else None,
            "volume_id": volume_id,
            "metadata": {"candidate_id": candidate["candidate_id"],
                         "volume_label": volume_label, "volume_source": "input_hint" if volume_label else "unknown",
                         "decision_state": candidate["decision_state"]},
        })
    if any(entry["boundary_kind"] == "unresolved" for entry in entries):
        issues.append({"code": "unresolved_structure", "severity": "warning",
                       "scope": {"entry_ids": [e["entry_id"] for e in entries if e["boundary_kind"] == "unresolved"],
                                 "edition_id": extraction["manifest"]["edition_id"]},
                       "message": "one or more entries have no reliable original chapter title/boundary"})
    deferred = [row for row in all_candidates if row["decision_state"] != "accepted"]
    if deferred:
        issues.append({"code": "weak_heading_candidates_deferred", "severity": "warning",
                       "scope": {"candidate_ids": [row["candidate_id"] for row in deferred]},
                       "message": "weak numbered lines are retained in body until a boundary is confirmed"})
    structure = {"schema": "import-structure-v1", "rules_version": "generic-structure-v1",
                 "edition_id": extraction["manifest"]["edition_id"],
                 "extraction_fingerprint": extraction["manifest"]["extraction_fingerprint"],
                 "entries": entries, "candidates": candidates + deferred, "issues": issues,
                 "status": "needs_resolution" if any(row.get("severity") == "error" for row in issues)
                           else "ready_with_warnings" if issues else "ready"}
    structure["fingerprint"] = digest({k: v for k, v in structure.items() if k != "fingerprint"})
    return structure
