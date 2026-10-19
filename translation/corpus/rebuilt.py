"""Verify a fresh import against the independently audited cache reading order.

Rebinding a structural patch requires equality of every normalized paragraph,
not just a matching title, chapter key or start anchor.
"""
from __future__ import annotations

import copy
import json
from collections import Counter
from itertools import zip_longest
from pathlib import Path

from ..text import VERSION, strip_zero_width, zero_width_counts
from . import load_chapters
from ..storage import digest, file_digest, read_json, write_json, write_jsonl


def cleaned_row(row):
    return {"kind": row["kind"], "text": strip_zero_width(row["text"]).strip(),
            "ruby": [{k: strip_zero_width(v) for k, v in r.items()} for r in row["ruby"]]}


def verify_rebuilt_import(root: Path, base: Path, previous: Path, rebuild_folder: Path) -> dict:
    corpus, previous_corpus = base / "corpus", previous / "corpus"
    manifest = read_json(corpus / "corpus_manifest.json")
    rebuild = read_json(rebuild_folder / "manifest.json")
    if any(file_digest(root / d["path"]) != d["output_sha256"] for d in rebuild["documents"]):
        raise ValueError("Installed EPUB differs from validated rebuild")
    if any(file_digest(root / p) != h for p, h in rebuild["cache_inputs"].items()):
        raise ValueError("Raw cache changed after rebuild")
    if {(d["path"], d["sha256"]) for d in manifest["documents"]} != {(d["path"], d["output_sha256"]) for d in rebuild["documents"]}:
        raise ValueError("Imported documents differ from rebuilt EPUBs")
    previous_manifest = read_json(previous_corpus / "corpus_manifest.json")
    backups = {d["path"]: rebuild_folder / d["backup"] for d in rebuild["documents"]}
    if any(file_digest(backups[d["path"]]) != d["sha256"] for d in previous_manifest["documents"]):
        raise ValueError("Previous audit no longer has its exact original EPUB backups")
    old, new = load_chapters(previous_corpus), load_chapters(corpus)
    identity = lambda c: tuple(c[k] for k in ("chapter_id", "language", "order", "title", "kind", "match_key"))
    if [identity(c) for c in old] != [identity(c) for c in new]:
        raise ValueError("Chapter identity/order changed")
    controls, checked, empty = Counter(), 0, 0
    with (previous_corpus / "paragraphs.jsonl").open(encoding="utf-8") as a, (corpus / "paragraphs.jsonl").open(encoding="utf-8") as b:
        for left, right in zip_longest(a, b):
            if left is None or right is None:
                raise ValueError("Paragraph count changed")
            previous_row, row = json.loads(left), json.loads(right)
            if (previous_row["chapter_id"], previous_row["ordinal"]) != (row["chapter_id"], row["ordinal"]) or cleaned_row(previous_row) != cleaned_row(row):
                raise ValueError(f"Rebuilt paragraph disagrees with audited cache: {row['paragraph_id']}")
            controls.update(zero_width_counts(row["text"]))
            for ruby in row["ruby"]:
                for text in ruby.values():
                    controls.update(zero_width_counts(text))
            checked += 1
            empty += not bool(row["text"])
    if controls:
        raise ValueError(f"Rebuilt corpus still contains zero-width controls: {dict(controls)}")
    audit_id = digest({"rebuild": file_digest(rebuild_folder / "manifest.json"),
                       "previous_chapters": file_digest(previous_corpus / "chapters.jsonl"),
                       "previous_paragraphs": file_digest(previous_corpus / "paragraphs.jsonl"),
                       "output_fingerprint": manifest["corpus_fingerprint"], "cleanup": VERSION})
    for chapter in new:
        chapter["metadata"].setdefault("reference_integrity", {}).update({
            "order_status": "verified_cache_rebuild", "page_chain_complete": True,
            "content_status": "all_paragraphs_equal_to_cleaned_audited_cache",
            "semantic_status": "unreviewed", "gold_approved": False, "rebuild_audit": audit_id})
    write_jsonl(corpus / "chapters.jsonl", new)
    manifest.update({"reference_audit_fingerprint": audit_id, "rebuild_manifest": str((rebuild_folder / "manifest.json").relative_to(root)),
                     "cleanup": VERSION, "gold_approved": False})
    write_json(corpus / "corpus_manifest.json", manifest)
    result = {"chapter_count": len(new), "paragraphs_verified": checked, "empty_paragraphs_retained": empty,
              "zero_width_controls": {}, "all_rows_match_audited_cache": True, "original_epub_backups_verified": True,
              "cache_unchanged": True, "audit_id": audit_id, "semantic_gold_approved": False,
              "verified_chapters_sha256": file_digest(corpus / 'chapters.jsonl'),
              "verified_paragraphs_sha256": file_digest(corpus / 'paragraphs.jsonl'),
              "corpus_fingerprint": manifest['corpus_fingerprint']}
    write_json(base / "verification/rebuild.json", result)
    return result


def rebind_section_patch(previous: Path, base: Path, patch: dict) -> dict:
    old_corpus, new_corpus = previous / "corpus", base / "corpus"
    if patch["corpus_fingerprint"] != read_json(old_corpus / "corpus_manifest.json")["corpus_fingerprint"]:
        raise ValueError("Original section patch fingerprint is stale")
    verification = read_json(base / "verification/rebuild.json")
    if not verification["all_rows_match_audited_cache"]:
        raise ValueError("Complete rebuild verification required before patch rebinding")
    if verification['verified_chapters_sha256'] != file_digest(new_corpus / 'chapters.jsonl') or verification['verified_paragraphs_sha256'] != file_digest(new_corpus / 'paragraphs.jsonl'):
        raise ValueError('Verified corpus changed before section rebinding')
    new_chapters = {c["chapter_id"]: c for c in load_chapters(new_corpus)}
    wanted = {u["chapter_id"] for u in patch["updates"]}
    rows = []
    for folder in (old_corpus, new_corpus):
        by_id = {}
        with (folder / "paragraphs.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                row = json.loads(line)
                if row["chapter_id"] in wanted:
                    by_id[row["paragraph_id"]] = row
        rows.append(by_id)
    old_rows, new_rows = rows
    positions = {(r["chapter_id"], r["ordinal"]): r for r in new_rows.values()}
    def rebind(pid):
        old = old_rows[pid]
        new = positions[old["chapter_id"], old["ordinal"]]
        if cleaned_row(old) != cleaned_row(new):
            raise ValueError("Section row content changed")
        return new["paragraph_id"]
    rebound = copy.deepcopy(patch)
    for update in rebound["updates"]:
        chapter = new_chapters[update["chapter_id"]]
        update["revision_id"] = chapter["revision_id"]
        update["paragraph_ids"] = [rebind(p) for p in update["paragraph_ids"]]
        evidence = update["evidence"]
        evidence["start_anchor"] = strip_zero_width(evidence["start_anchor"]).strip()
        for key in ("retained_paragraph_ids", "duplicate_paragraph_ids"):
            if key in evidence:
                evidence[key] = [rebind(p) for p in evidence[key]]
    rebound["corpus_fingerprint"] = read_json(new_corpus / "corpus_manifest.json")["corpus_fingerprint"]
    rebound["review"]["rebound_from_patch"] = digest(patch)
    rebound["review"]["rebind_basis"] = "Every paragraph equals the cleaned, audited cache reading order; exact duplicate evidence revalidated on application."
    return rebound
