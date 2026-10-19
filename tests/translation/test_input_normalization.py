from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from translation.editions import EditionArchive
from translation.storage import file_digest, read_jsonl, write_jsonl
from translation.ingestion import (
    apply_structure_patch,
    export_corpus,
    extract_sources,
    inspect_input,
    load_extraction,
    load_structure,
    normalize_input,
    propose_structure,
    read_source_range,
    validate_import,
)
from translation.ingestion.structure import looks_like_heading


def write_epub(path: Path, resources: dict[str, str], *, nav: str | None = None,
               title: str = "Test Book"):
    nav = nav or ""
    manifest = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
    ]
    spine = []
    for index, name in enumerate(resources):
        manifest.append(f'<item id="r{index}" href="{name}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="r{index}"/>')
    package = (
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{title}</dc:title></metadata>'
        f'<manifest>{"".join(manifest)}</manifest><spine>{"".join(spine)}</spine></package>'
    )
    container = (
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles></container>'
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", package)
        archive.writestr("OEBPS/nav.xhtml", nav)
        for name, body in resources.items():
            archive.writestr("OEBPS/" + name, body)


def manifest_for(path: Path, *, language="ja", edition_id="fixture", paragraph_policy="line"):
    return {
        "schema": "novel-import-input-v1",
        "edition_id": edition_id,
        "language": language,
        "edition_label": "fixture",
        "sources": [{"source_id": "s0", "path": str(path), "format": "auto", "input_order": 0}],
        "profile": "generic-v1",
        "paragraph_policy": paragraph_policy,
    }


def test_epub_navigation_splits_multiple_chapters_in_one_resource(tmp_path):
    nav = (
        '<html xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol>'
        '<li><a href="book.xhtml#c1">第一章 开始</a></li>'
        '<li><a href="book.xhtml#c2">第二章 继续</a></li>'
        '</ol></nav></body></html>'
    )
    book = (
        '<html><head><title>Book metadata title</title></head><body>'
        '<h1 id="c1">第一章 开始</h1><p>甲段落。</p>'
        '<h1 id="c2">第二章 继续</h1><p>乙段落。</p>'
        '</body></html>'
    )
    path = tmp_path / "book.epub"
    write_epub(path, {"book.xhtml": book}, nav=nav)
    spec = manifest_for(path)
    assert inspect_input(spec)["status"] == "ready"
    folder = tmp_path / "import"
    extract_sources(spec, folder)
    structure = propose_structure(folder)
    assert len(structure["entries"]) == 2
    assert [row["raw_title"] for row in structure["entries"]] == ["第一章 开始", "第二章 继续"]
    validation = validate_import(folder)
    assert validation["ok"]
    archive_folder = tmp_path / "export"
    exported = export_corpus(folder, archive_folder)
    archive = EditionArchive.from_folder(archive_folder)
    docs = archive.documents("fixture")
    assert exported["entry_count"] == 2 and len(docs) == 2
    assert [doc["text"] for doc in docs] == ["甲段落。", "乙段落。"]


def test_epub_chapter_can_continue_across_resources(tmp_path):
    first = '<html><body><h1>第一章</h1><p>第一半。</p></body></html>'
    second = '<html><head><title>ignored</title></head><body><p>第二半。</p></body></html>'
    path = tmp_path / "split.epub"
    write_epub(path, {"a.xhtml": first, "b.xhtml": second})
    folder = tmp_path / "import"
    extract_sources(manifest_for(path), folder)
    structure = propose_structure(folder)
    assert len(structure["entries"]) == 1
    assert structure["entries"][0]["boundary_kind"] == "chapter"
    assert any(row["code"] == "resource_continuation_ambiguous" for row in structure["issues"])
    assert validate_import(folder)["ok"]
    archive_folder = tmp_path / "export"
    export_corpus(folder, archive_folder)
    doc = EditionArchive.from_folder(archive_folder).documents("fixture")[0]
    assert doc["text"] == "第一半。\n\n第二半。"


def test_txt_preserves_source_lines_and_zero_width_diagnostics(tmp_path):
    path = tmp_path / "book.txt"
    path.write_text("第一章\n甲\u200b乙\n\n第二章\n丙\n", encoding="utf-8")
    folder = tmp_path / "import"
    extract_sources(manifest_for(path), folder)
    extraction = load_extraction(folder)
    assert [row["raw_text"] for row in extraction["blocks"] if row["raw_text"]] == ["第一章", "甲乙", "第二章", "丙"]
    assert any(row["code"] == "zero_width_removed" for row in extraction["issues"])
    structure = propose_structure(folder)
    assert len(structure["entries"]) == 2
    validation = validate_import(folder)
    assert validation["ok"]


def test_missing_title_keeps_body_as_unresolved_entry(tmp_path):
    path = tmp_path / "untitled.epub"
    write_epub(path, {"body.xhtml": '<html><body><div>正文一。</div><div>正文二。</div></body></html>'}, nav="")
    folder = tmp_path / "import"
    extract_sources(manifest_for(path), folder)
    structure = propose_structure(folder)
    entry = structure["entries"][0]
    assert entry["raw_title"] == ""
    assert entry["title_source"] == "generated_label"
    assert entry["boundary_kind"] == "unresolved"
    assert validate_import(folder)["status"] == "ready_with_warnings"


def test_chapter_heading_patterns_cover_japanese_fullwidth_and_special_titles():
    for title in ("第３章　新しい朝", "第十話", "第2巻", "序章", "終章", "幕間", "番外編"):
        assert looks_like_heading(title)
    assert not looks_like_heading("彼は第3章の話をした")


def test_navigation_parent_with_children_does_not_duplicate_chapter_body(tmp_path):
    nav = (
        '<html xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol>'
        '<li><a href="book.xhtml#volume">第一巻</a><ol>'
        '<li><a href="book.xhtml#c1">第一章</a></li>'
        '<li><a href="book.xhtml#c2">第二章</a></li>'
        '</ol></li></ol></nav></body></html>'
    )
    book = (
        '<html><body><section id="volume">'
        '<h1 id="c1">第一章</h1><p>甲の本文。</p>'
        '<h1 id="c2">第二章</h1><p>乙の本文。</p>'
        '</section></body></html>'
    )
    path = tmp_path / "nested-nav.epub"
    write_epub(path, {"book.xhtml": book}, nav=nav)
    folder = tmp_path / "import"
    extract_sources(manifest_for(path), folder)
    structure = propose_structure(folder)
    assert [row["raw_title"] for row in structure["entries"]] == ["第一章", "第二章"]
    assert any(row["code"] == "navigation_parent_deferred" for row in structure["issues"])
    assert validate_import(folder)["ok"]
    output = tmp_path / "export"
    export_corpus(folder, output)
    docs = EditionArchive.from_folder(output).documents("fixture")
    assert [row["text"] for row in docs] == ["甲の本文。", "乙の本文。"]


def test_txt_paragraph_policies_merge_only_declared_line_groups(tmp_path):
    source = "第一章\n甲行\n乙行\n\n丙行\n丁行\n"
    expected = {
        "line": ["甲行", "乙行", "丙行", "丁行"],
        "blank_line": ["甲行\n乙行", "丙行\n丁行"],
        "wrapped": ["甲行乙行", "丙行丁行"],
    }
    for policy, paragraphs in expected.items():
        path = tmp_path / f"{policy}.txt"
        path.write_text(source, encoding="utf-8")
        folder = tmp_path / f"import-{policy}"
        extract_sources(manifest_for(path, edition_id=policy, paragraph_policy=policy), folder)
        propose_structure(folder)
        assert validate_import(folder)["ok"]
        output = tmp_path / f"export-{policy}"
        export_corpus(folder, output)
        doc = EditionArchive.from_folder(output).documents(policy)[0]
        assert [row["text"] for row in doc["rows"]] == paragraphs
        exported = read_jsonl(output / "paragraphs.jsonl")
        for paragraph in exported:
            ranges = paragraph["source_ranges"]
            assert ranges
            if len(ranges) > 1:
                assert [row["locator"]["line_start"] for row in ranges] == list(
                    range(ranges[0]["locator"]["line_start"],
                          ranges[0]["locator"]["line_start"] + len(ranges))
                )


def test_epub_direct_text_br_ruby_table_and_list_are_exported_once(tmp_path):
    book = (
        '<html><body><h1 id="c1">第一章</h1>'
        '<div>直接文本<br/>续行<p>本文<ruby>蜘蛛<rt>くも</rt></ruby>结束。</p></div>'
        '<table><tr><th>技能</th><th>值</th></tr><tr><td>火</td><td>四</td></tr></table>'
        '<ul><li>清单项目</li></ul></body></html>'
    )
    path = tmp_path / "layout.epub"
    write_epub(path, {"book.xhtml": book})
    folder = tmp_path / "import"
    extract_sources(manifest_for(path), folder)
    extraction = load_extraction(folder)
    assert extraction["sources"][0]["resources"][0]["text_coverage"]["complete"]
    assert any(row["ruby"] == [{"base": "蜘蛛", "reading": "くも"}] for row in extraction["blocks"])
    structure = propose_structure(folder)
    assert validate_import(folder)["ok"]
    output = tmp_path / "export"
    export_corpus(folder, output)
    doc = EditionArchive.from_folder(output).documents("fixture")[0]
    assert "直接文本\n续行" in doc["text"]
    assert "本文蜘蛛结束。" in doc["text"]
    assert "技能\t值" in doc["text"]
    assert "火\t四" in doc["text"]
    assert "清单项目" in doc["text"]
    assert "くも" not in doc["text"]
    provenance = read_jsonl(output / "provenance.jsonl")
    assert all(row["block_ids"] and row["source_ranges"] for row in provenance)


def test_body_numbering_and_empty_navigation_anchor_do_not_lose_first_paragraph(tmp_path):
    nav = (
        '<html xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol>'
        '<li><a href="book.xhtml#start">幕間の話</a></li>'
        '<li><a href="book.xhtml#chapter2">第二章</a></li>'
        '</ol></nav></body></html>'
    )
    book = (
        '<html><body><a id="start"></a>'
        '<p>彼は第1章ではなく、2、3分も続けていた。</p>'
        '<p>第一部隊から魔力弾が飛んできた。</p>'
        '<p>第1話は各所で無料公開中。</p>'
        '<h1 id="chapter2">第二章</h1><p>次の章の本文。</p>'
        '</body></html>'
    )
    path = tmp_path / "body-numbers.epub"
    write_epub(path, {"book.xhtml": book}, nav=nav)
    folder = tmp_path / "import"
    extract_sources(manifest_for(path), folder)
    structure = propose_structure(folder)
    assert len(structure["entries"]) == 2
    assert structure["entries"][0]["title_block_id"] is None
    assert validate_import(folder)["ok"]
    output = tmp_path / "export"
    export_corpus(folder, output)
    docs = EditionArchive.from_folder(output).documents("fixture")
    assert docs[0]["text"] == "彼は第1章ではなく、2、3分も続けていた。\n\n第一部隊から魔力弾が飛んできた。\n\n第1話は各所で無料公開中。"
    assert docs[1]["text"] == "次の章の本文。"


def test_headingless_xhtml_does_not_import_head_metadata_as_body(tmp_path):
    path = tmp_path / "fragment.epub"
    write_epub(path, {"body.xhtml": (
        '<html><head><title>metadata only</title></head><div><div>甲。</div><div>乙。</div></div></html>'
    )}, nav="")
    folder = tmp_path / "import"
    extract_sources(manifest_for(path), folder)
    extraction = load_extraction(folder)
    assert [row["raw_text"] for row in extraction["blocks"]] == ["甲。", "乙。"]
    assert extraction["sources"][0]["resources"][0]["text_coverage"]["complete"]


def test_auto_txt_policy_preserves_physical_lines_and_reports_ambiguity(tmp_path):
    path = tmp_path / "ambiguous.txt"
    path.write_text("第一章\n甲行\n乙行\n\n第二章\n丙行\n", encoding="utf-8")
    folder = tmp_path / "import"
    extract_sources(manifest_for(path, paragraph_policy="auto"), folder)
    extraction = load_extraction(folder)
    assert [row["raw_text"] for row in extraction["blocks"] if row["raw_text"]] == [
        "第一章", "甲行", "乙行", "第二章", "丙行"
    ]
    source = extraction["sources"][0]
    assert source["paragraph_policy"] == "line" and source["has_blank_lines"]
    assert any(row["code"] == "paragraph_policy_ambiguous" for row in extraction["issues"])


def test_wrapped_txt_does_not_join_dialogue_list_or_scene_separator(tmp_path):
    path = tmp_path / "wrapped.txt"
    path.write_text("第1章\n这是一段\n继续叙述\n\n「新的对白\n对白续行\n1. 技能一\n2. 技能二\n***\n结尾。\n",
                    encoding="utf-8")
    folder = tmp_path / "import"
    extract_sources(manifest_for(path, paragraph_policy="wrapped"), folder)
    propose_structure(folder)
    output = tmp_path / "export"
    export_corpus(folder, output)
    rows = EditionArchive.from_folder(output).documents("fixture")[0]["rows"]
    texts = [row["text"] for row in rows]
    assert texts[0:2] == ["这是一段继续叙述", "「新的对白"]
    assert texts[2:] == ["对白续行", "1. 技能一", "2. 技能二", "***", "结尾。"]


@pytest.mark.parametrize("codec", ["utf-8-sig", "utf-16", "cp932", "gb18030"])
def test_txt_encoding_bom_or_explicit_override_round_trips(codec, tmp_path):
    text = "第一章\n本文。\n"
    path = tmp_path / f"{codec}.txt"
    path.write_bytes(text.encode(codec))
    source = {"source_id": "s0", "path": str(path), "format": "txt", "input_order": 0}
    if codec in {"cp932", "gb18030"}:
        source["encoding_override"] = codec
    spec = {**manifest_for(path), "sources": [source]}
    folder = tmp_path / f"import-{codec}"
    extract_sources(spec, folder)
    extraction = load_extraction(folder)
    assert "本文。" in [row["raw_text"] for row in extraction["blocks"]]
    propose_structure(folder)
    assert validate_import(folder)["ok"]


def test_ambiguous_text_encoding_blocks_export_until_resolved(tmp_path):
    path = tmp_path / "ambiguous-encoding.txt"
    path.write_bytes(b"\xa4\xa2\n")
    folder = tmp_path / "import"
    extract_sources(manifest_for(path), folder)
    propose_structure(folder)
    validation = validate_import(folder)
    assert not validation["ok"]
    assert validation["status"] == "needs_resolution"
    assert any(row["code"] == "encoding_ambiguous" for row in validation["issues"])
    with pytest.raises(ValueError, match="invalid"):
        export_corpus(folder, tmp_path / "must-not-export")


def test_source_snapshot_and_extraction_fingerprint_detect_tampering(tmp_path):
    path = tmp_path / "book.txt"
    path.write_text("第一章\n正文。\n", encoding="utf-8")
    source_hash = file_digest(path)
    folder = tmp_path / "import"
    extract_sources(manifest_for(path), folder)
    assert file_digest(path) == source_hash
    blocks = read_jsonl(folder / "blocks.jsonl")
    blocks[-1]["raw_text"] = "被篡改。"
    write_jsonl(folder / "blocks.jsonl", blocks)
    propose_structure(folder)
    validation = validate_import(folder)
    assert not validation["ok"]
    assert any(row["code"] == "extraction_fingerprint_mismatch" for row in validation["issues"])


def test_export_completion_manifest_is_checked_and_idempotent(tmp_path):
    path = tmp_path / "book.txt"
    path.write_text("第一章\n正文。\n", encoding="utf-8")
    folder = tmp_path / "import"
    extract_sources(manifest_for(path), folder)
    propose_structure(folder)
    output = tmp_path / "export"
    first = export_corpus(folder, output)
    assert export_corpus(folder, output) == first
    assert EditionArchive.from_folder(output).documents("fixture")
    paragraphs = read_jsonl(output / "paragraphs.jsonl")
    write_jsonl(output / "paragraphs.jsonl", paragraphs[:-1])
    with pytest.raises(ValueError, match="fingerprint|count mismatch"):
        EditionArchive.from_folder(output)


def test_patch_confirms_weak_txt_headings_and_preserves_old_revision(tmp_path):
    path = tmp_path / "weak.txt"
    path.write_text("1. Beginning\n甲。\n2. Next\n乙。\n", encoding="utf-8")
    folder = tmp_path / "import"
    extract_sources(manifest_for(path), folder)
    base = propose_structure(folder)
    assert len(base["entries"]) == 1
    extraction = load_extraction(folder)
    first, second = extraction["blocks"][0]["block_id"], extraction["blocks"][2]["block_id"]
    excerpt = read_source_range(folder, [second], context=1)
    assert [row["raw_text"] for row in excerpt["blocks"]] == ["甲。", "2. Next", "乙。"]
    patch = {"schema": "import-structure-patch-v1", "base_structure_revision": base["fingerprint"],
             "operations": [{"op": "set_boundary", "at": {"block_id": key},
                             "title_ref": {"block_id": key}, "reason": "fixture chapter title"}
                            for key in (first, second)]}
    result = apply_structure_patch(folder, patch)
    assert len(result["entries"]) == 2
    assert result["fingerprint"] != base["fingerprint"]
    assert load_structure(folder, base["fingerprint"]) == base
    assert apply_structure_patch(folder, patch) == result
    assert validate_import(folder)["ok"]
    output = tmp_path / "export"
    export_corpus(folder, output)
    assert [doc["text"] for doc in EditionArchive.from_folder(output).documents("fixture")] == ["甲。", "乙。"]


def test_patch_rejects_stale_replacement_and_illegal_anchor_without_writes(tmp_path):
    path = tmp_path / "book.txt"
    path.write_text("第一章\n正文。\n", encoding="utf-8")
    folder = tmp_path / "import"
    extract_sources(manifest_for(path), folder)
    base = propose_structure(folder)
    first = base["entries"][0]["block_ids"][0]
    common = {"schema": "import-structure-patch-v1", "base_structure_revision": base["fingerprint"]}
    invalid = [
        {**common, "base_structure_revision": "0" * 64, "operations": [{"op": "remove_boundary", "at": {"block_id": first}, "reason": "bad"}]},
        {**common, "operations": [{"op": "select_title", "at": {"block_id": first}, "title_ref": {"block_id": first}, "replacement_text": "invented", "reason": "bad"}]},
        {**common, "operations": [{"op": "set_boundary", "at": {"block_id": "missing"}, "reason": "bad"}]},
    ]
    for patch in invalid:
        with pytest.raises(ValueError):
            apply_structure_patch(folder, patch)
        assert load_structure(folder) == base


def test_merge_patch_and_paragraph_policy_patch_rebuild_without_changing_text(tmp_path):
    path = tmp_path / "book.txt"
    path.write_text("第一章\n甲。\n第二章\n乙。\n丙。\n", encoding="utf-8")
    folder = tmp_path / "import"
    extract_sources(manifest_for(path), folder)
    base = propose_structure(folder)
    patch = {"schema": "import-structure-patch-v1", "base_structure_revision": base["fingerprint"],
             "operations": [{"op": "remove_boundary", "at": {"block_id": base["entries"][1]["block_ids"][0]}, "reason": "fixture merged chapter"},
                            {"op": "set_paragraph_policy", "entry_ids": [base["entries"][0]["entry_id"]], "policy": "blank_line", "reason": "explicit paragraph choice"}]}
    merged = apply_structure_patch(folder, patch)
    assert len(merged["entries"]) == 1
    output = tmp_path / "export"
    export_corpus(folder, output)
    doc = EditionArchive.from_folder(output).documents("fixture")[0]
    # Removing a boundary does not remove the former title from the source.
    assert doc["text"] == "甲。\n第二章\n乙。\n丙。"


def test_volume_hints_drive_new_index_profile_without_changing_legacy_plan(tmp_path):
    from translation.budget import ModelCapacity
    from translation.editions import EditionIndexSpec, plan_edition_index
    from translation.narrative import TokenCounter

    paths = [tmp_path / f"volume{index}.txt" for index in range(2)]
    for path in paths:
        path.write_text("第一章\n本文。\n", encoding="utf-8")
    spec = manifest_for(paths[0])
    spec["sources"] = [{"source_id": f"s{index}", "path": str(path), "input_order": index,
                        "volume_hint": f"Volume {index + 1}"} for index, path in enumerate(paths)]
    result = normalize_input(spec, tmp_path / "run")
    archive = EditionArchive.from_folder(result["corpus_folder"])
    docs = archive.documents("fixture")
    assert len({doc["document_id"] for doc in docs}) == 1
    assert len({doc["volume_id"] for doc in docs}) == 2
    plan = plan_edition_index(archive, "fixture", ModelCapacity(65536, 8192), TokenCounter(),
                             EditionIndexSpec(parent_max_chapters=100, child_max_chapters=10))
    assert len(plan["parents"]) == 2
    assert plan["identity"]["boundary_profile"] == "logical-volume-v1"


def test_unsupported_pdf_and_arbitrary_zip_are_not_decoded_as_txt(tmp_path):
    pdf = tmp_path / "book.pdf"
    pdf.write_bytes(b"%PDF-1.7\n")
    unrelated = tmp_path / "archive.zip"
    with zipfile.ZipFile(unrelated, "w") as archive:
        archive.writestr("notes.txt", "notes")
    for path in (pdf, unrelated):
        assert inspect_input(manifest_for(path))["status"] == "unsupported"
        with pytest.raises(ValueError, match="unsupported"):
            extract_sources(manifest_for(path), tmp_path / path.stem)


def test_raw_titles_keep_width_and_spacing_separate_from_matching_key(tmp_path):
    title = "第３章　同じ  朝"
    path = tmp_path / "raw-title.epub"
    nav = f'<nav><ol><li><a href="book.xhtml#c">{title}</a></li></ol></nav>'
    write_epub(path, {"book.xhtml": f'<html><body><h1 id="c">{title}</h1><p>本文。</p></body></html>'}, nav=nav)
    result = normalize_input(manifest_for(path), tmp_path / "run")
    assert EditionArchive.from_folder(result["corpus_folder"]).documents("fixture")[0]["raw_title"] == title


def test_navigation_order_conflict_is_blocked_instead_of_reordering_text(tmp_path):
    path = tmp_path / "reordered.epub"
    nav = ('<nav><ol><li><a href="book.xhtml#b">第二章</a></li>'
           '<li><a href="book.xhtml#a">第一章</a></li></ol></nav>')
    write_epub(path, {"book.xhtml": '<body><h1 id="a">第一章</h1><p>甲。</p><h1 id="b">第二章</h1><p>乙。</p></body>'}, nav=nav)
    result = normalize_input(manifest_for(path), tmp_path / "run")
    assert not result["ok"] and result["status"] == "needs_resolution"
    assert any(row["code"] == "navigation_order_conflict" for row in result["issues"])
    assert "corpus_folder" not in result


def test_percent_encoded_resource_href_and_fragment_resolve(tmp_path):
    from translation.ingestion.epub import _decode, _fragment, _resolve

    assert _resolve("OPS/nav.xhtml", "book%20file.xhtml#c%201") == "OPS/book file.xhtml"
    assert _fragment("book%20file.xhtml#c%201") == "c 1"
    assert _resolve("OPS/nav.xhtml", "#local") == "OPS/nav.xhtml"
    markup = '<?xml version="1.0" encoding="cp932"?><html><body>本文。</body></html>'
    assert _decode(markup.encode("cp932"))[0] == markup
    with pytest.raises(ValueError):
        _decode(b'<?xml version="1.0" encoding="utf-8"?><p>\xff</p>')


def test_same_input_in_separate_output_folders_has_identical_ids_and_export(tmp_path):
    path = tmp_path / "replay.txt"
    path.write_text("第一章\n甲。\n\n第二章\n乙。\n", encoding="utf-8")
    spec = manifest_for(path)
    a = normalize_input(spec, tmp_path / "a")
    b = normalize_input(spec, tmp_path / "b")
    assert a["export"] == b["export"]
    assert read_jsonl(Path(a["corpus_folder"]) / "entries.jsonl") == read_jsonl(Path(b["corpus_folder"]) / "entries.jsonl")
    assert normalize_input(spec, tmp_path / "a")["export"] == a["export"]


def test_missing_source_blob_blocks_export_even_when_structure_is_valid(tmp_path):
    path = tmp_path / "snapshot.txt"
    path.write_text("第一章\n正文。\n", encoding="utf-8")
    folder = tmp_path / "import"
    extract_sources(manifest_for(path), folder)
    propose_structure(folder)
    extraction = load_extraction(folder)
    (folder / extraction["sources"][0]["blob"]).unlink()
    assert not validate_import(folder)["ok"]
    with pytest.raises(ValueError, match="invalid"):
        export_corpus(folder, tmp_path / "output")


def test_cli_run_uses_manifest_relative_paths_without_a_model(tmp_path, capsys):
    import json
    from translation.cli import main
    from translation.storage import write_json

    path = tmp_path / "book.txt"
    path.write_text("第一章\n正文。\n", encoding="utf-8")
    spec = manifest_for(Path("book.txt"))
    write_json(tmp_path / "input.json", spec)
    output = tmp_path / "run"
    assert main(["import", "run", "--manifest", str(tmp_path / "input.json"), "--output", str(output)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]
    assert EditionArchive.from_folder(output / "corpus").documents("fixture")[0]["text"] == "正文。"


def test_cli_staged_import_accepts_directory_paths_in_run_argument(tmp_path, capsys):
    import json
    from translation.cli import main
    from translation.storage import write_json

    path = tmp_path / "book.txt"
    path.write_text("第一章\n甲。\n第二章\n乙。\n", encoding="utf-8")
    write_json(tmp_path / "input.json", manifest_for(path))
    folder, output = tmp_path / "nested" / "extraction", tmp_path / "nested" / "corpus"
    assert main(["import", "extract", "--manifest", str(tmp_path / "input.json"), "--output", str(folder)]) == 0
    capsys.readouterr()
    assert main(["import", "plan", "--run", str(folder)]) == 0
    plan = json.loads(capsys.readouterr().out)
    block = load_structure(folder)["entries"][0]["block_ids"][0]
    assert main(["import", "read", "--run", str(folder), "--block-id", block]) == 0
    assert json.loads(capsys.readouterr().out)["blocks"][0]["block_id"] == block
    assert main(["import", "validate", "--run", str(folder)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]
    assert main(["import", "export", "--run", str(folder), "--output", str(output)]) == 0
    assert json.loads(capsys.readouterr().out)["entry_count"] == 2
    assert len(EditionArchive.from_folder(output).documents("fixture")) == 2
