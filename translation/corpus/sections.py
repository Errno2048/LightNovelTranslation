"""Explicit, revision-bound structural exclusions; no paragraph text is deleted."""
from __future__ import annotations

import copy
import json
import re
from collections import defaultdict
from pathlib import Path

from . import load_chapters
from .reference_audit import _materialize
from ..storage import digest, file_digest, read_json, write_json, write_jsonl

VERSION = "reference-sections-v1"


def normalized(text: str) -> str:
    return re.sub(r"[\s\u200b\u200c\u200d\ufeff]+", "", text)


def apply_section_patch(root: Path, base: Path, patch: dict, branch: Path) -> dict:
    if branch.resolve() == base.resolve() or (branch.exists() and any(branch.iterdir())):
        raise ValueError("section repair needs a new, separate branch")
    corpus = base / "corpus"
    manifest = read_json(corpus / "corpus_manifest.json")
    if patch.get("version") != VERSION or patch.get("corpus_fingerprint") != manifest["corpus_fingerprint"]:
        raise ValueError("section patch is unsupported or stale")
    if not patch.get("review", {}).get("reviewer") or not patch.get("updates"):
        raise ValueError("explicit structural review and updates required")
    chapters = load_chapters(corpus)
    index = {c["chapter_id"]: c for c in chapters}
    updates = defaultdict(list)
    assigned = set()
    for update in patch["updates"]:
        chapter = index.get(update["chapter_id"])
        if not chapter or chapter["language"] != "zh" or update["revision_id"] != chapter["revision_id"]:
            raise ValueError("unknown or stale section chapter")
        if update["new_kind"] not in {"unpaired_appendix", "duplicate_content", "afterword", "source_site_control"}:
            raise ValueError("unsupported section classification")
        positions = [chapter["paragraph_ids"].index(pid) for pid in update["paragraph_ids"]]
        if not positions or positions != list(range(positions[0], positions[-1] + 1)):
            raise ValueError("section must be a nonempty contiguous paragraph range")
        if assigned.intersection(update["paragraph_ids"]):
            raise ValueError("overlapping section patches")
        assigned.update(update["paragraph_ids"])
        if not update.get("reason") or not update.get("evidence"):
            raise ValueError("section evidence required")
        dependencies = update.get("source_dependencies", [])
        if len(dependencies) != len(set(dependencies)) or any(cid not in index or index[cid]["language"] != "ja" for cid in dependencies):
            raise ValueError("invalid section source dependencies")
        if update["new_kind"] == "afterword" and not dependencies:
            raise ValueError("translated afterword needs explicit source dependencies")
        updates[chapter["chapter_id"]].append(update)
    rows = defaultdict(list)
    with (corpus / "paragraphs.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if row["chapter_id"] in updates:
                rows[row["chapter_id"]].append(row)
    patch_id = digest(patch)
    corrected, audits = {}, []
    for cid, changes in updates.items():
        chapter, chapter_rows = index[cid], rows[cid]
        row_index = {r["paragraph_id"]: r for r in chapter_rows}
        if [r["paragraph_id"] for r in chapter_rows] != chapter["paragraph_ids"]:
            raise ValueError("chapter paragraph references/order do not match the corpus")
        revision = digest({"input_revision": chapter["revision_id"], "patch": patch_id, "updates": changes})
        for update in changes:
            selected = [row_index[pid] for pid in update["paragraph_ids"]]
            if any(r["kind"] != "body" for r in selected):
                raise ValueError("only body ranges can receive an initial section classification")
            if selected[0]["text"] != update["evidence"].get("start_anchor"):
                raise ValueError("section anchor text changed")
            if update['new_kind'] == 'source_site_control':
                from bs4 import BeautifulSoup
                evidence = update['evidence']
                cached = (root / evidence['cache_locator']).resolve()
                if not cached.is_relative_to((root / 'temp').resolve()) or file_digest(cached) != evidence['cache_sha256']:
                    raise ValueError('Invalid site-control cache evidence')
                content = BeautifulSoup(cached.read_text(encoding='utf-8-sig'), 'html.parser').find(id='TextContent')
                paragraph = content.find_all('p', recursive=False)[evidence['input_page_paragraph']]
                icon = paragraph.find('img', src='https://masiro.moe/static/image/common/fav.gif', alt='收藏')
                if len(selected) != 1 or icon is None or normalized(paragraph.get_text()) != '收藏' or normalized(selected[0]['text']) != '收藏':
                    raise ValueError('Site-control evidence includes unrecognized content')
            if update["new_kind"] == "duplicate_content":
                evidence = update["evidence"]
                kept = evidence.get("retained_paragraph_ids", [])
                duplicate = evidence.get("duplicate_paragraph_ids", [])
                if set(kept) & set(update["paragraph_ids"]) or not set(duplicate) <= set(update["paragraph_ids"]):
                    raise ValueError("invalid retained/duplicate paragraph ownership")
                if len(set(kept)) != len(kept) or len(set(duplicate)) != len(duplicate):
                    raise ValueError("duplicate evidence contains repeated paragraph identities")
                if kept != [r["paragraph_id"] for r in chapter_rows if r["paragraph_id"] in set(kept)] or duplicate != [r["paragraph_id"] for r in selected if r["paragraph_id"] in set(duplicate)]:
                    raise ValueError("duplicate evidence must preserve paragraph order")
                number = re.fullmatch(r"main:(\d+)", chapter.get("match_key", ""))
                if not number or not re.match(rf"^第{number[1]}[话話章]\s", evidence["start_anchor"]):
                    raise ValueError("duplicate block needs its repeated chapter-heading anchor")
                first = [normalized(row_index[p]["text"]) for p in kept]
                second = [normalized(row_index[p]["text"]) for p in duplicate]
                if not first or first != second or sum(map(len, first)) < 500:
                    raise ValueError("duplicate content is not an exact normalized paragraph sequence")
                excluded = set(update["paragraph_ids"]) - set(duplicate)
                for pid in excluded:
                    text = row_index[pid]["text"]
                    # Only an explicitly anchored repeated chapter heading or
                    # an invisible-only paragraph may accompany the duplicate.
                    if normalized(text) and text != evidence["start_anchor"]:
                        raise ValueError("duplicate exclusion contains unmatched content")
            for row in selected:
                row["kind"] = update["new_kind"]
                row["section"] = {"patch_id": patch_id, "reason": update["reason"],
                                  "source_dependencies": update.get("source_dependencies", []),
                                  "semantic_status": update.get("semantic_status", "unreviewed"),
                                  "parallel_body_eligible": False}
        for row in chapter_rows:
            previous_id = row["paragraph_id"]
            row.update({"paragraph_id": f"{cid}@{revision[:16]}:p{row['ordinal']:04d}", "revision_id": revision})
            row.setdefault("provenance", {})["section_input_paragraph_id"] = previous_id
        result = copy.deepcopy(chapter)
        text = "\n\n".join(r["text"] for r in chapter_rows if r["kind"] == "body")
        result.update({"revision_id": revision, "text": text, "char_count": len(text),
                       "paragraph_ids": [r["paragraph_id"] for r in chapter_rows]})
        result["metadata"]["reference_integrity"].update({"body_view_status": "reviewed_structural_exclusions",
                                                         "section_patch_id": patch_id, "gold_approved": False})
        corrected[cid] = result
        audits.append({"chapter_id": cid, "status": "reviewed_structural_exclusions", "old_revision": chapter["revision_id"],
                       "new_revision": revision, "body_characters_before": len(chapter["text"]), "body_characters_after": len(text)})
    input_files = dict(manifest.get("repair_input_documents", {}))
    for update in patch['updates']:
        if update['new_kind'] == 'source_site_control':
            input_files[update['evidence']['cache_locator']] = update['evidence']['cache_sha256']
    input_files.update({str((corpus / name).relative_to(root)): file_digest(corpus / name)
                        for name in ("chapters.jsonl", "paragraphs.jsonl", "corpus_manifest.json")})
    if any(file_digest(root / name) != sha for name, sha in input_files.items()):
        raise ValueError("section patch input files changed")
    audit_manifest = {"fingerprint": patch_id, "input_files": input_files}
    _materialize(corpus, manifest, chapters, corrected, rows, audits, audit_manifest, branch,
                 profile=VERSION, issue_type="reference_section_review", inherit_all_issues=True)
    write_json(branch / "corpus/section_patch.json", patch)
    write_jsonl(branch / "corpus/section_review.jsonl", audits)
    return {"branch": str(branch.relative_to(root)), "chapters_reclassified": len(corrected), "section_ranges": len(patch["updates"]),
            "paragraph_text_preserved": True, "patch_id": patch_id, "gold_approved": False}
