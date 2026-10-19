"""Audit cached chapter boundaries and materialize a separate order-repair branch."""
from __future__ import annotations

import copy
import html
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from . import classify, extract_paragraphs, load_chapters, normal_title
from .linovelib import PROFILE, SCRIPT_SHA256, page_info, replace_pua, restore_order
from ..storage import atomic_text, digest, file_digest, read_json, read_jsonl, write_json, write_jsonl

AUDIT_VERSION = "reference-order-audit-v2"


def _page_address(book: int, chapter: int, page: int) -> str:
    return f"/novel/{book}/{chapter}{'_' + str(page) if page > 1 else ''}.html"


def _clean_ads(content):
    for node in content.select("div.dag, div.cgo, div.google-auto-placed, ins.adsbygoogle"):
        node.decompose()


def _cached_pages(root: Path):
    groups, issues, hashes = defaultdict(list), [], {}
    for path in sorted((root / "temp").glob("linovelib_*.html")):
        raw = path.read_bytes()
        soup = BeautifulSoup(replace_pua(raw.decode("utf-8-sig")), "html.parser")
        content = soup.find(id="TextContent")
        if content is None:
            continue  # Catalog and volume pages do not use the chapter protocol.
        locator = str(path.relative_to(root))
        hashes[locator] = digest(raw)
        try:
            info = page_info(soup)
            expected_name = "linovelib_https___www.linovelib.com" + _page_address(info["book_id"], info["chapter_id"], info["page"]).replace("/", "_")
            if path.name != expected_name:
                raise ValueError("cached filename disagrees with chapter/page metadata")
            _clean_ads(content)
            before = str(content)
            mapping = restore_order(content, info["chapter_id"], profile=info["profile"])
            page = {**info, **mapping, "locator": locator, "sha256": hashes[locator],
                    "before_markup": before, "after_markup": str(content)}
            groups[normal_title(info["title"])].append(page)
        except ValueError as exc:
            issues.append({"locator": locator, "status": "quarantined", "reason": str(exc)})
    return groups, issues, hashes


def validate_page_chain(pages: list[dict]) -> list[dict]:
    pages = sorted(pages, key=lambda x: x["page"])
    identities = {(p["book_id"], p["chapter_id"]) for p in pages}
    if len(identities) != 1 or [p["page"] for p in pages] != list(range(1, len(pages) + 1)):
        raise ValueError("ambiguous identity, missing page, or duplicate page")
    book, chapter = next(iter(identities))
    for i, page in enumerate(pages):
        if i > 0 and urlsplit(page["previous"]).path != _page_address(book, chapter, i):
            raise ValueError("broken previous-page link")
        next_path = urlsplit(page["next"]).path
        if i + 1 < len(pages):
            if next_path != _page_address(book, chapter, i + 2):
                raise ValueError("broken next-page link")
        elif re.fullmatch(rf"/novel/{book}/{chapter}(?:_\d+)?\.html", next_path):
            raise ValueError("last cached page still points inside this chapter; incomplete chain")
    return pages


def _row_content(row: dict) -> dict:
    return {k: row[k] for k in ("text", "kind", "ruby")}


def restore_chapter(chapter: dict, old_rows: list[dict], pages: list[dict]) -> tuple[dict, list[dict], dict]:
    """Require exact cache-to-EPUB fidelity, then move existing records only."""
    pages = validate_page_chain(pages)
    raw_rows, _ = extract_paragraphs("<html><body>" + "".join(p["before_markup"] for p in pages) + "</body></html>", "zh")
    if [_row_content(r) for r in raw_rows] != [_row_content(r) for r in old_rows]:
        raise ValueError("cached paragraphs do not exactly reproduce the imported EPUB; no repair applied")
    permutation, provenance, offset = [], [], 0
    for page in pages:
        permutation.extend(offset + i for i in page["output_to_input"])
        provenance.extend({"cache_locator": page["locator"], "input_page_paragraph": i,
                           "page": page["page"], "cache_sha256": page["sha256"]} for i in page["output_to_input"])
        offset += len(page["output_to_input"])
    if sorted(permutation) != list(range(len(old_rows))):
        raise ValueError("restoration must be a bijection of all chapter paragraphs")
    revision = digest({"profile": PROFILE, "input_revision": chapter["revision_id"],
                       "pages": [(p["locator"], p["sha256"]) for p in pages], "output_to_input": permutation})
    rows = []
    for ordinal, (original, origin) in enumerate(zip(permutation, provenance)):
        row = copy.deepcopy(old_rows[original])
        row.update({"paragraph_id": f"{chapter['chapter_id']}@{revision[:16]}:p{ordinal:04d}",
                    "revision_id": revision, "ordinal": ordinal,
                    "provenance": {**origin, "previous_paragraph_id": old_rows[original]["paragraph_id"],
                                   "previous_ordinal": original, "previous_revision_id": chapter["revision_id"]}})
        rows.append(row)
    if Counter(digest(_row_content(r)) for r in rows) != Counter(digest(_row_content(r)) for r in old_rows):
        raise ValueError("paragraph content changed during order restoration")
    result = copy.deepcopy(chapter)
    text = "\n\n".join(r["text"] for r in rows if r["kind"] == "body")
    moved = sum(a != b for a, b in enumerate(permutation))
    result.update({"revision_id": revision, "text": text, "char_count": len(text),
                   "paragraph_ids": [r["paragraph_id"] for r in rows],
                   "source_url": "https://www.linovelib.com" + _page_address(pages[0]["book_id"], pages[0]["chapter_id"], 1)})
    quality = {"order_status": "restored_from_site_protocol" if moved else "verified_site_order_unchanged",
               "content_status": "exact_paragraph_multiset_preserved", "semantic_status": "unreviewed",
               "profile": PROFILE, "input_revision_id": chapter["revision_id"],
               "page_chain_complete": True, "moved_paragraphs": moved, "gold_approved": False}
    result["metadata"]["reference_integrity"] = quality
    result["metadata"]["raw_cache_sources"] = [{k: p[k] for k in ("locator", "sha256", "page")} for p in pages]
    result["metadata"]["used_raw_cache"] = True
    audit = {"chapter_id": chapter["chapter_id"], "match_key": chapter["match_key"], "title": chapter["title"],
             "kind": chapter["kind"], "input_revision_id": chapter["revision_id"], "output_revision_id": revision,
             "status": quality["order_status"], "paragraph_count": len(rows), "moved_paragraphs": moved,
             "output_to_input": permutation, "page_count": len(pages), "all_paragraphs_preserved": True}
    return result, rows, audit


def _comparison(chapters, corrected, old_rows, new_rows, audits, output, sample_keys):
    sources = {c["match_key"]: c for c in chapters if c["language"] == "ja" and c["kind"] == "narrative"}
    records = {a["chapter_id"]: a for a in audits}
    sections = ["<p>按网站脚本还原的译文；仅验证段落顺序与内容守恒，尚未批准为逐段语义金标准。段号均从 0 开始。零宽字符保留。</p>"]
    for c in chapters:
        cid = c["chapter_id"]
        if c["language"] != "zh" or c["match_key"] not in sample_keys or cid not in corrected:
            continue
        source = sources.get(c["match_key"], {"text": "（无对应原文）"})
        sections.append(f"<h2>{html.escape(c['title'])}</h2><p>移动 {records[cid]['moved_paragraphs']} / {len(old_rows[cid])} 段</p><div class='columns'>")
        sections.append(f"<section><h3>日文原文</h3><pre>{html.escape(source['text'])}</pre></section>")
        for label, rows in [("原 EPUB 顺序", old_rows[cid]), ("还原后的阅读顺序", new_rows[cid])]:
            sections.append(f"<section><h3>{label}</h3>")
            for row in rows:
                previous = row.get("provenance", {}).get("previous_ordinal", row["ordinal"])
                sections.append(f"<p><small>{row['ordinal']} ← 原 {previous}</small><br>{html.escape(row['text'])}</p>")
            sections.append("</section>")
        sections.append("</div>")
    atomic_text(output / "comparison.html", "<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>译文顺序修复核查</title><style>body{font-family:system-ui;margin:24px} .columns{display:flex;gap:24px} section{flex:1;min-width:0} pre{white-space:pre-wrap;font-family:inherit} p,pre{line-height:1.7} small{color:#777} h2{border-top:1px solid #aaa;padding-top:20px}</style><body>" + "".join(sections) + "</body></html>")


def audit_references(root: Path, base: Path, *, branch: Path | None = None,
                     sample_keys=("main:3", "main:775", "main:1233")) -> dict:
    if branch is not None and branch.exists() and any(branch.iterdir()):
        raise ValueError("repair branch already exists; choose a new branch name")
    output, corpus = base / "order_audit/v2", base / "corpus"
    manifest = read_json(corpus / "corpus_manifest.json")
    input_files = {str((corpus / name).relative_to(root)): file_digest(corpus / name)
                   for name in ("corpus_manifest.json", "chapters.jsonl", "paragraphs.jsonl")}
    if any(file_digest(root / d["path"]) != d["sha256"] for d in manifest["documents"]):
        raise ValueError("original EPUB changed since corpus import; ingest a new branch first")
    input_files.update({d["path"]: d["sha256"] for d in manifest["documents"]})
    # Also ensure original Japanese caches remain the same as P1's inputs.
    input_files.update(manifest.get("raw_cache_documents", {}))
    chapters = load_chapters(corpus)
    old_rows = defaultdict(list)
    with (corpus / "paragraphs.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if row["chapter_id"].startswith("zh:"):
                old_rows[row["chapter_id"]].append(row)
    groups, page_issues, hashes = _cached_pages(root)
    input_files.update(hashes)
    # Site headings sometimes contain censorship placeholders absent in the
    # catalog/EPUB title. A unique chapter key may find a candidate, but exact
    # reproduction of every input paragraph is still mandatory before repair.
    by_key = defaultdict(list)
    for title, pages in groups.items():
        kind, key = classify(title, "linovelib", "", "")
        if kind == "narrative" and key:
            by_key[key].append(pages)
    corrected, updated_rows, audits, consumed = {}, {}, [], set()
    for chapter in chapters:
        if chapter["language"] != "zh":
            continue
        cid = chapter["chapter_id"]
        pages = groups.get(normal_title(chapter["title"]), [])
        method = "exact_title_and_exact_body"
        if not pages and chapter["match_key"] and len(by_key[chapter["match_key"]]) == 1:
            pages = by_key[chapter["match_key"]][0]
            method = "unique_sequence_key_and_exact_body"
        if not pages and chapter["kind"] != "narrative":
            continue
        try:
            if not pages:
                raise ValueError("matching cached chapter not found")
            result, rows, audit = restore_chapter(chapter, old_rows[cid], pages)
            audit["cache_identity_method"] = method
            audit["cache_titles"] = sorted({p["title"] for p in pages})
            corrected[cid], updated_rows[cid] = result, rows
            consumed.update(p["locator"] for p in pages)
        except ValueError as exc:
            audit = {"chapter_id": cid, "match_key": chapter["match_key"], "kind": chapter["kind"],
                     "title": chapter["title"], "status": "quarantined", "reason": str(exc)}
        audits.append(audit)
    fingerprint = digest({"audit_version": AUDIT_VERSION, "profile": PROFILE, "input_corpus": manifest["corpus_fingerprint"], "inputs": input_files})
    if (output / "audit_manifest.json").exists() and read_json(output / "audit_manifest.json")["fingerprint"] != fingerprint:
        raise ValueError("audit inputs changed; preserve existing evidence and use another book branch")
    page_records = [{k: v for k, v in page.items() if not k.endswith("_markup")}
                    for pages in groups.values() for page in pages]
    summary = {"version": AUDIT_VERSION, "profile": PROFILE, "fingerprint": fingerprint,
               "input_corpus_fingerprint": manifest["corpus_fingerprint"], "observed_site_script_sha256": SCRIPT_SHA256,
               "cached_chapter_pages": len(hashes), "decoded_pages": len(page_records),
               "page_issues": page_issues, "chapter_statuses": dict(Counter(a["status"] for a in audits)),
               "narrative_chapter_statuses": dict(Counter(a["status"] for a in audits if a["kind"] == "narrative")),
               "moved_paragraphs": sum(a.get("moved_paragraphs", 0) for a in audits),
               "unconsumed_cache_pages": sorted(set(hashes) - consumed),
               "input_files": input_files, "original_inputs_unchanged": False,
               "limitations": "Order fidelity is not semantic alignment or gold approval. Zero-width text is preserved. Existing reviews and examples are not inherited."}
    # This is the commit barrier: no derived corpus is published if any input changed.
    if any(file_digest(root / name) != expected for name, expected in input_files.items()):
        raise ValueError("original input changed during reference audit")
    summary["original_inputs_unchanged"] = True
    write_jsonl(output / "page_audit.jsonl", page_records)
    write_jsonl(output / "chapter_audit.jsonl", audits)
    write_json(output / "audit_manifest.json", summary)
    _comparison(chapters, corrected, old_rows, updated_rows, audits, output, sample_keys)
    if branch is not None:
        _materialize(corpus, manifest, chapters, corrected, updated_rows, audits, summary, branch)
        summary["repair_branch"] = str(branch.relative_to(root))
    _write_report(chapters, corrected, audits, summary, output)
    return {k: v for k, v in summary.items() if k != "input_files"}


def _materialize(corpus, manifest, chapters, corrected, updated_rows, audits, audit_manifest, branch,
                 *, profile=PROFILE, issue_type="reference_order_audit", inherit_all_issues=False):
    from ..alignment import rules
    quarantined = {a["chapter_id"] for a in audits if a["status"] == "quarantined"}
    results = []
    for original in chapters:
        c = copy.deepcopy(corrected.get(original["chapter_id"], original))
        if c["chapter_id"] in quarantined:
            c["metadata"]["reference_integrity"] = {"order_status": "quarantined", "gold_approved": False}
        results.append(c)
    def paragraphs():
        emitted = set()
        with (corpus / "paragraphs.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                row = json.loads(line)
                cid = row["chapter_id"]
                if cid not in updated_rows:
                    yield row
                elif cid not in emitted:
                    yield from updated_rows[cid]
                    emitted.add(cid)
    target = branch / "corpus"
    write_jsonl(target / "chapters.jsonl", results)
    write_jsonl(target / "paragraphs.jsonl", paragraphs())
    # Encoding and invisible-character issues remain applicable. Old paragraph
    # positions or sequence warnings must not be copied as if freshly checked.
    issues = [{"type": issue_type, "chapter_id": a["chapter_id"], "status": a["status"]} for a in audits]
    issues.extend({**i, "inherited_from_input": True} for i in read_jsonl(corpus / "corpus_issues.jsonl")
                  if inherit_all_issues or i["type"] in {"encoding_artifact", "invisible_characters", "short_body", "unclassified"})
    write_jsonl(target / "corpus_issues.jsonl", issues)
    derived = {**manifest, "parser_version": manifest["parser_version"] + "+" + profile,
               "parent_corpus_fingerprint": manifest["corpus_fingerprint"],
               "corpus_fingerprint": digest([(c["chapter_id"], c["revision_id"]) for c in results]),
               "reference_audit_fingerprint": audit_manifest["fingerprint"], "issues": len(issues),
               "repair_input_documents": audit_manifest["input_files"],
               "reviews_inherited": False, "gold_approved": False}
    write_json(target / "corpus_manifest.json", derived)
    rules(results, branch / "alignment")
    write_jsonl(branch / "alignment/example_catalog.jsonl", [])
    split_path = corpus.parent / "evaluations/splits.json"
    if split_path.exists():
        write_json(branch / "evaluations/splits.json", read_json(split_path))
    atomic_text(target / "ingest_report.md", f"# Reference repair branch\n\nParent: {manifest['corpus_fingerprint']}\n\nProfile: {profile}\n\nOriginal files are unchanged. All chapter correspondences require new review; semantic gold is not approved.\n")


def _write_report(chapters, corrected, audits, summary, output):
    ja = {c["match_key"]: c for c in chapters if c["language"] == "ja" and c["kind"] == "narrative"}
    zh = {c["match_key"]: corrected.get(c["chapter_id"], c) for c in chapters if c["language"] == "zh" and c["kind"] == "narrative"}
    pairs = set(ja) & set(zh)
    outliers = [{"key": k, "ratio": round(len(zh[k]["text"]) / max(1, len(ja[k]["text"])), 3)}
                for k in sorted(pairs) if not .3 <= len(zh[k]["text"]) / max(1, len(ja[k]["text"])) <= 1.5]
    alignment = {"sequence_key_pairs": len(pairs), "source_only_keys": sorted(set(ja) - set(zh)),
                 "target_only_keys": sorted(set(zh) - set(ja)), "length_ratio_outliers": outliers,
                 "limitation": "Sequence-key identity checks do not prove every bilingual paragraph or revision matches."}
    write_json(output / "chapter_boundary_checks.json", alignment)
    lines = ["# 参考译文顺序核查", "", f"协议：{PROFILE}。", "",
             "原因：网页 HTML 经过段落置换，阅读脚本按章节 ID 还原；原爬虫遗漏了这一步。每个 HTTP 分页单独处理，前 20 个非空直接子段落固定。", "",
             f"缓存章节分页：{summary['cached_chapter_pages']}；成功解析：{summary['decoded_pages']}。",
             f"中文正文状态：{summary['narrative_chapter_statuses']}。",
             f"移动段落总数：{summary['moved_paragraphs']}；原输入哈希保持不变：{summary['original_inputs_unchanged']}。", "",
             "每章修复前必须精确重现 EPUB 的全部段落，验证分页链完整和段落双射，修复后校验段落内容多重集合不变。", "",
             f"章序键对应：{len(pairs)}；仅原文：{len(alignment['source_only_keys'])}；仅译文：{len(alignment['target_only_keys'])}；长度比异常：{len(outliers)}。", "",
             "零宽字符全部保留，是否出现零宽字符不参与决定是否重排。修复状态只证明网站阅读顺序；未证明语义、版本一致，也未批准为评测金标准。", "",
             "修复分支重新生成候选章节映射，不继承旧审查、旧示例、向量缓存、实验输入或生成任务；固定的源章节划分保持不变。", "",
             "查看 comparison.html、chapter_audit.jsonl、page_audit.jsonl 和 chapter_boundary_checks.json。"]
    atomic_text(output / "report.md", "\n".join(lines) + "\n")
