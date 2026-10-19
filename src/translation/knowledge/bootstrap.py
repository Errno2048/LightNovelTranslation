"""Create the small, explicitly reviewed P4 evidence catalog.

The bootstrap step deliberately separates machine-selected donor ranges from
human-reviewed evidence.  A donor is usable for few-shot or convention
extraction only when an explicit, revision-bound review manifest authorizes
that purpose.  Without such a manifest the catalog still records the proposed
range as a candidate, but it cannot enter K0 or a translation prompt.
"""
from __future__ import annotations

from pathlib import Path

from ..storage import digest, read_json, read_jsonl, write_jsonl
from .evidence import CorpusArchive, input_manifest, review_range
from .schemas import KnowledgeError, QueryPolicy


def _body_rows(archive: CorpusArchive, chapter_id: str, count: int = 2) -> list[dict]:
    archive.ensure_rows([chapter_id])
    rows = [r for r in archive.rows[chapter_id] if r.get("kind") == "body" and r.get("text", "").strip()]
    return rows[:count]


def _chapter_maps(archive: CorpusArchive) -> tuple[dict[str, dict], dict[str, dict]]:
    for language in ("ja", "zh"):
        keys = [c["match_key"] for c in archive.chapters.values()
                if c.get("language") == language and c.get("kind") == "narrative" and c.get("match_key")]
        if len(keys) != len(set(keys)):
            raise KnowledgeError("invalid_record", "ambiguous chapter match keys")
    sources = {c["match_key"]: c for c in archive.chapters.values()
               if c.get("language") == "ja" and c.get("kind") == "narrative" and c.get("match_key")}
    translations = {c["match_key"]: c for c in archive.chapters.values()
                    if c.get("language") == "zh" and c.get("kind") == "narrative" and c.get("match_key")}
    return sources, translations


def _review_specs(review_manifest: dict | None, archive: CorpusArchive) -> dict[tuple[str, str], dict]:
    if review_manifest is None:
        return {}
    if review_manifest.get("version") != "p4-donor-review-v1":
        raise KnowledgeError("invalid_record", "unsupported donor review manifest version")
    if review_manifest.get("corpus_fingerprint") != archive.fingerprint:
        raise KnowledgeError("stale_evidence", "donor review manifest is for another corpus")
    specs = {}
    for spec in review_manifest.get("entries", []):
        if not spec.get("target_key") or not spec.get("donor_key"):
            raise KnowledgeError("invalid_record", "donor review requires target and donor keys")
        key = (spec["target_key"], spec["donor_key"])
        if key in specs:
            raise KnowledgeError("invalid_record", "duplicate donor review entry")
        specs[key] = dict(spec)
    return specs


def _paragraph_ids(archive: CorpusArchive, chapter_id: str, spec: dict, side: str) -> list[str]:
    explicit = spec.get(f"{side}_paragraph_ids")
    if explicit is not None:
        if not isinstance(explicit, list) or not explicit:
            raise KnowledgeError("invalid_record", f"{side}_paragraph_ids must be nonempty")
        return list(explicit)
    count = spec.get(f"{side}_body_count", 2)
    if type(count) is not int or count <= 0 or count > 8:
        raise KnowledgeError("invalid_record", f"{side}_body_count must be between 1 and 8")
    return [r["paragraph_id"] for r in _body_rows(archive, chapter_id, count)]


def build_evidence_catalog(archive: CorpusArchive, splits: dict, *, target_keys: list[str],
                           donor_keys: dict[str, list[str]], destination: Path,
                           review_manifest: dict | None = None) -> dict:
    sources, translations = _chapter_maps(archive)
    specs = _review_specs(review_manifest, archive)
    if not target_keys or len(target_keys) != len(set(target_keys)):
        raise KnowledgeError("invalid_record", "unique target keys required")
    if set(donor_keys) != set(target_keys):
        raise KnowledgeError("invalid_record", "donor allowlist must cover exactly the target keys")
    if any(not isinstance(v, list) or not v or len(v) != len(set(v)) for v in donor_keys.values()):
        raise KnowledgeError("invalid_record", "nonempty unique donor lists required")
    if set(specs) - {(t, d) for t, ds in donor_keys.items() for d in ds}:
        raise KnowledgeError("invalid_record", "review outside donor allowlist")
    missing_targets = [key for key in target_keys if key not in sources]
    if missing_targets:
        raise KnowledgeError("not_covered", f"unknown target chapter: {missing_targets[0]}")
    all_donors = sorted({key for values in donor_keys.values() for key in values})
    missing_donors = [key for key in all_donors if key not in sources or key not in translations]
    if missing_donors:
        raise KnowledgeError("invalid_record", f"donor lacks a parallel source/translation: {missing_donors[0]}")
    donor_ids = [sources[key]["chapter_id"] for key in all_donors]
    reserved = list(splits.get("validation", [])) + list(splits.get("blind_test", []))
    entries, reviews = [], []
    for target_key in target_keys:
        target = sources[target_key]
        if target_key in all_donors:
            raise KnowledgeError("visibility_denied", "target chapter cannot be a donor")
        hidden = [target["chapter_id"]]
        policy = QueryPolicy("single_gap", hidden_source_ids=hidden, reserved_source_ids=reserved,
                             allowed_translation_source_ids=donor_ids)
        for donor_key in donor_keys[target_key]:
            if donor_key == target_key:
                raise KnowledgeError("visibility_denied", "target chapter cannot be a donor")
            source = sources[donor_key]
            translation = translations[donor_key]
            spec = specs.get((target_key, donor_key), {})
            if spec.get("source_revision_id") and spec["source_revision_id"] != source["revision_id"]:
                raise KnowledgeError("stale_evidence", f"source donor revision changed: {donor_key}")
            if spec.get("translation_revision_id") and spec["translation_revision_id"] != translation["revision_id"]:
                raise KnowledgeError("stale_evidence", f"translation donor revision changed: {donor_key}")
            source_ids = _paragraph_ids(archive, source["chapter_id"], spec, "source")
            translation_ids = _paragraph_ids(archive, translation["chapter_id"], spec, "translation")
            decision = spec.get("decision", "unresolved")
            if decision == "accepted" and not all(spec.get(k) for k in
                    ("source_revision_id", "translation_revision_id", "source_paragraph_ids",
                     "translation_paragraph_ids", "method", "reason")):
                raise KnowledgeError("invalid_record", "accepted donor requires explicit revisions, ranges and review method/reason")
            if decision not in {"accepted", "rejected", "partial", "unresolved"}:
                raise KnowledgeError("invalid_record", "invalid donor review decision")
            purposes = spec.get("purposes", {})
            method = spec.get("method", "bootstrap_unreviewed")
            reason = spec.get("reason", "Donor range requires explicit semantic review before use.")
            if decision == "accepted" and not purposes.get("correspondence"):
                raise KnowledgeError("invalid_record", "accepted donor must authorize correspondence")
            patch = {
                "corpus_fingerprint": archive.fingerprint,
                "source": {"chapter_id": source["chapter_id"], "revision_id": source["revision_id"], "paragraph_ids": source_ids},
                "translation": {"chapter_id": translation["chapter_id"], "revision_id": translation["revision_id"], "paragraph_ids": translation_ids},
                "decision": decision, "method": method, "reason": reason,
                "purposes": purposes,
            }
            review = review_range(archive, policy, patch)
            source_evidence, translation_evidence = review["evidence"]
            usable = decision == "accepted" and purposes.get("correspondence") and purposes.get("few_shot")
            entry = {
                "id": "example:" + digest({"target": target_key, "donor": donor_key, "review": review["review_id"]})[:24],
                "source_chapter_id": source["chapter_id"], "translation_chapter_id": translation["chapter_id"],
                "source_revision_id": source["revision_id"], "translation_revision_id": translation["revision_id"],
                "source_order": source["order"], "source_dependencies": [source["chapter_id"]],
                "source_text": source_evidence["text"], "translation_text": translation_evidence["text"],
                "source_evidence_ids": [source_evidence["id"]], "translation_evidence_ids": [translation_evidence["id"]],
                "evidence_ids": [source_evidence["id"], translation_evidence["id"]],
                "review": review, "status": "reviewed_prefix" if usable else "candidate", "target_keys": [target_key],
                "source_paragraph_ids": source_ids, "translation_paragraph_ids": translation_ids,
                "quality": spec.get("quality", "reviewed_range_not_semantic_gold" if usable else "unreviewed_candidate"),
            }
            entries.append(entry)
            reviews.append(review)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and read_jsonl(destination) != entries:
        raise KnowledgeError("conflict", "evidence catalog is frozen; choose a new destination")
    write_jsonl(destination, entries)
    return {"version": "p4-evidence-catalog-v2", "entries": len(entries),
            "target_keys": target_keys, "donor_keys": donor_keys,
            "corpus_fingerprint": archive.fingerprint,
            "review_manifest_fingerprint": digest(review_manifest) if review_manifest is not None else None,
            "review_ids": [x["review_id"] for x in reviews],
            "usable_entries": sum(x["status"] == "reviewed_prefix" for x in entries),
            "candidate_entries": sum(x["status"] == "candidate" for x in entries),
            "fingerprint": digest(entries)}


def bootstrap_p4(corpus: Path, output: Path, splits_path: Path, *, target_keys: list[str],
                donor_keys: dict[str, list[str]], review_manifest_path: Path | None = None) -> dict:
    archive = CorpusArchive(corpus)
    splits = read_json(splits_path)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    manifest = input_manifest(archive, splits, output / "input_manifest.json")
    review_manifest = read_json(review_manifest_path) if review_manifest_path else None
    catalog = build_evidence_catalog(archive, splits, target_keys=target_keys, donor_keys=donor_keys,
                                     destination=output / "evidence_catalog.jsonl",
                                     review_manifest=review_manifest)
    return {"input_manifest": manifest, "catalog": catalog}
