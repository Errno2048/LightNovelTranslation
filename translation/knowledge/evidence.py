"""Revision-bound corpus access and explicit parallel-range review."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from ..storage import digest, file_digest, read_json, write_json
from .schemas import KnowledgeError


class CorpusArchive:
    def __init__(self, corpus: Path):
        self.corpus = Path(corpus)
        self.manifest = read_json(corpus / "corpus_manifest.json")
        self.chapters = {}
        with (corpus / "chapters.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                row = json.loads(line)
                self.chapters[row["chapter_id"]] = row
        self.sources = {c["match_key"]: c for c in self.chapters.values() if c["language"] == "ja" and c.get("match_key")}
        self.rows = {}
        self.input_identity = {name: file_digest(corpus / name) for name in ("chapters.jsonl", "paragraphs.jsonl", "corpus_manifest.json")}
        self.fingerprint = digest(self.input_identity)

    def canonical(self, chapter):
        if chapter["language"] == "ja":
            return chapter["chapter_id"]
        source = self.sources.get(chapter.get("match_key"))
        return source["chapter_id"] if source else None

    def check_allowed(self, chapter, policy, *, reference=False):
        cid = self.canonical(chapter)
        if cid is None or cid in policy.reserved_source_ids:
            raise KnowledgeError("visibility_denied", "unmapped or reserved source")
        if chapter["language"] == "zh":
            if cid in policy.hidden_source_ids or (policy.allowed_translation_source_ids is not None and cid not in policy.allowed_translation_source_ids):
                raise KnowledgeError("visibility_denied", "translation is hidden or outside donor allowlist")
        if policy.mode == "sequential" and self.sources[chapter["match_key"]]["order"] > policy.cutoff_order:
            # The caller may explicitly supply only the current target original.
            if not (chapter["language"] == "ja" and cid in policy.hidden_source_ids):
                raise KnowledgeError("visibility_denied", "beyond sequential cutoff")
        integrity = chapter.get("metadata", {}).get("reference_integrity", {})
        if integrity.get("order_status") == "quarantined" or integrity.get("parallel_body_eligible") is False:
            raise KnowledgeError("quality_hold", "chapter has unresolved integrity hold")
        if reference and chapter["language"] == "zh" and integrity.get("evaluation_eligible") is False:
            raise KnowledgeError("quality_hold", "reference is not evaluation eligible")

    def ensure_rows(self, chapter_ids):
        missing = set(chapter_ids) - self.rows.keys()
        if not missing:
            return
        found = defaultdict(list)
        missing = set(missing)
        with (self.corpus / "paragraphs.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                # Avoid parsing all 282k rows when only a few chapters are needed.
                row = json.loads(line)
                if row["chapter_id"] in missing:
                    found[row["chapter_id"]].append(row)
        for cid in missing:
            c = self.chapters[cid]
            rows = found[cid]
            if [p["paragraph_id"] for p in rows] != c["paragraph_ids"] or any(p["revision_id"] != c["revision_id"] for p in rows):
                raise KnowledgeError("stale_evidence", "paragraph revision/order mismatch")
            self.rows[cid] = rows

    def evidence(self, chapter_id, paragraph_ids, policy, *, purpose="search"):
        c = self.chapters.get(chapter_id)
        if c is None:
            raise KnowledgeError("not_covered", "unknown evidence chapter")
        self.check_allowed(c, policy)
        self.ensure_rows([chapter_id])
        lookup = {p["paragraph_id"]: p for p in self.rows[chapter_id]}
        if not paragraph_ids or len(set(paragraph_ids)) != len(paragraph_ids) or any(p not in lookup for p in paragraph_ids):
            raise KnowledgeError("stale_evidence", "invalid paragraph IDs")
        selected = [lookup[p] for p in paragraph_ids]
        if [p["ordinal"] for p in selected] != sorted(p["ordinal"] for p in selected):
            raise KnowledgeError("invalid_record", "unordered range")
        if any(p["kind"] != "body" or p.get("section", {}).get("semantic_status") == "known_numeric_conflict" for p in selected):
            raise KnowledgeError("quality_hold", "nonparallel or conflicting section")
        if any(not p.get("text", "").strip() for p in selected):
            raise KnowledgeError("quality_hold", "empty paragraphs are not evidence")
        data = {"chapter_id": chapter_id, "revision_id": c["revision_id"], "paragraph_ids": paragraph_ids,
                "language": c["language"], "source_id": self.canonical(c), "source_order": self.sources[c["match_key"]]["order"],
                "text": "\n".join(p["text"] for p in selected), "ordinals": [p["ordinal"] for p in selected],
                "purpose": purpose, "source_dependencies": [self.canonical(c)], "corpus_fingerprint": self.fingerprint}
        data["text_hash"] = digest(data["text"])
        data["id"] = "evidence:" + digest({k: data[k] for k in ("chapter_id", "revision_id", "paragraph_ids", "text_hash")})[:24]
        return data

    def validate_evidence(self, e, policy):
        if e.get("corpus_fingerprint") != self.fingerprint:
            raise KnowledgeError("stale_evidence", "corpus identity changed")
        actual = self.evidence(e["chapter_id"], e["paragraph_ids"], policy, purpose=e.get("purpose", "search"))
        for k in ("id", "revision_id", "text_hash", "text", "source_id", "source_dependencies"):
            if e.get(k) != actual[k]:
                raise KnowledgeError("stale_evidence", f"invalid evidence {k}")
        return actual


def review_range(archive, policy, patch):
    """Accept only explicit revision-bound decisions, never scores."""
    if patch.get("corpus_fingerprint") != archive.fingerprint:
        raise KnowledgeError("stale_evidence", "review corpus changed")
    if patch.get("decision") not in {"accepted", "rejected", "partial", "unresolved"}:
        raise KnowledgeError("invalid_record", "invalid review decision")
    if not patch.get("method") or not patch.get("reason"):
        raise KnowledgeError("invalid_record", "review method/reason required")
    sides = [archive.evidence(patch[name]["chapter_id"], patch[name]["paragraph_ids"], policy, purpose="parallel_review") for name in ("source", "translation")]
    if sides[0]["language"] != "ja" or sides[1]["language"] != "zh" or sides[0]["source_id"] != sides[1]["source_id"]:
        raise KnowledgeError("invalid_record", "parallel identity mismatch")
    for side, name in zip(sides, ("source", "translation")):
        if patch[name].get("revision_id") != side["revision_id"]:
            raise KnowledgeError("stale_evidence", "review revision changed")
    purposes = patch.get("purposes", {})
    if set(purposes) - {"correspondence", "convention", "few_shot", "evaluation"} or any(type(v) is not bool for v in purposes.values()):
        raise KnowledgeError("invalid_record", "invalid review purposes")
    if any(purposes.values()) and patch["decision"] != "accepted":
        raise KnowledgeError("invalid_record", "partial/rejected ranges cannot authorize uses; submit accepted subranges")
    if purposes.get("convention") or purposes.get("few_shot"):
        if not purposes.get("correspondence"):
            raise KnowledgeError("invalid_record", "correspondence review required")
    result = {**patch, "evidence": sides, "policy_fingerprint": policy.fingerprint}
    result["review_id"] = "review:" + digest(result)[:24]
    return result


def input_manifest(archive, splits, destination):
    result = {"version": "p4-input-v1", "corpus_fingerprint": archive.fingerprint,
              "corpus_declared_fingerprint": archive.manifest["corpus_fingerprint"],
              "files": archive.input_identity, "split_fingerprint": splits["fingerprint"]}
    if destination.exists() and read_json(destination) != result:
        raise KnowledgeError("stale_evidence", "input manifest changed; choose new branch")
    write_json(destination, result)
    return result
