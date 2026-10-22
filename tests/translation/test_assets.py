from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

from translation.index.assets import (AssetIndex, load_asset_index,
                                      sniff_media_type)
from translation.index.pipeline import (TranslationRunner, build_epub,
                                        verify_epub)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Z0YQAAAAASUVORK5CYII=")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
                    encoding="utf-8")


def test_media_type_sniffs_bytes_not_extension():
    assert sniff_media_type(PNG, "cover.png") == "image/png"
    assert sniff_media_type(b"\xff\xd8\xff\xe0JFIF", "cover.png") == "image/jpeg"


def test_prompt_rows_inject_inline_markers():
    runner = object.__new__(TranslationRunner)
    runner.source_by_order = {0: {"rows": [{"ordinal": 0, "text": "Hello world"}]}}
    runner.asset_index = AssetIndex(
        edition_id="book",
        chapters={0: {"inline": [{"asset_id": "img-a", "paragraph": 0,
                                  "offset": 6}], "block": []}},
    )
    rows = runner.prompt_rows(0)
    assert rows[0]["text"] == "Hello [[IMG:img-a]]world"
    assert runner.expected_markers(rows) == {"S001": ["img-a"]}


def test_inline_marker_validation():
    text = ("@@TITLE\ntext: 標題\n"
            "@@TRANSLATION S001\ntext: 你好[[IMG:img-a]]世界\n"
            "@@END\n")
    parsed = TranslationRunner.parse_translation(
        None, text, ["S001"], expected_markers={"S001": ["img-a"]})
    assert parsed["errors"] == []
    assert parsed["paragraphs"] == ["你好世界"]
    assert parsed["inline_assets"] == [
        {"paragraph": 0, "offset": 2, "asset_id": "img-a"}]

    missing = TranslationRunner.parse_translation(
        None, "@@TITLE\ntext: 標題\n@@TRANSLATION S001\ntext: 你好世界\n@@END\n",
        ["S001"], expected_markers={"S001": ["img-a"]})
    assert any(error.startswith("image_markers_mismatch:S001")
               for error in missing["errors"])


def test_asset_index_extracts_inline_block_and_cover(tmp_path):
    project_path = tmp_path / "project"
    corpus = project_path / "imports" / "book" / "corpus"
    extraction = project_path / "imports" / "book" / "extraction"
    epub_path = tmp_path / "source.epub"
    with zipfile.ZipFile(epub_path, "w") as archive:
        archive.writestr("OPS/page1.html", """<html><body>
<p>Hello <img src="images/a.png" alt="inline"/> world</p>
<p><img src="images/b.png" alt="block"/></p>
</body></html>""")
        archive.writestr("OPS/cover.html",
                         '<html><body><img src="images/cover.png" alt="cover"/></body></html>')
        archive.writestr("OPS/images/a.png", PNG + b"a")
        archive.writestr("OPS/images/b.png", PNG + b"b")
        archive.writestr("OPS/images/cover.png", PNG + b"c")
    entry = {
        "entry_id": "entry-0", "edition_id": "book", "edition_order": 0,
        "revision_id": "r", "raw_title": "Chapter",
        "source_ranges": [{"resource_id": "OPS/page1.html"}],
    }
    paragraph = {
        "entry_id": "entry-0", "edition_id": "book", "revision_id": "r",
        "paragraph_id": "p0", "ordinal": 0, "kind": "body",
        "raw_text": "Hello  world",
        "source_ranges": [{"resource_id": "OPS/page1.html"}],
    }
    _write_jsonl(corpus / "entries.jsonl", [entry])
    _write_jsonl(corpus / "paragraphs.jsonl", [paragraph])
    _write_jsonl(corpus / "provenance.jsonl", [])
    _write_jsonl(corpus / "issues.jsonl", [])
    (corpus / "manifest.json").write_text("{}", encoding="utf-8")
    _write_jsonl(extraction / "sources.jsonl", [{
        "format": "epub", "path": str(epub_path),
        "resources": [
            {"resource_id": "OPS/page1.html", "media_type": "application/xhtml+xml"},
            {"resource_id": "OPS/cover.html", "media_type": "application/xhtml+xml"},
        ],
    }])
    project = SimpleNamespace(
        path=project_path,
        _manifest=lambda: {"editions": {"book": {
            "corpus_folder": "imports/book/corpus"}}},
    )
    index = load_asset_index(project, "book", refresh=True)
    assert len(index.assets) == 3
    assert index.cover is not None
    chapter = index.chapter_assets(0)
    assert chapter["inline"] == [{
        "asset_id": chapter["inline"][0]["asset_id"],
        "paragraph": 0, "offset": 6,
    }]
    assert chapter["block"] and chapter["block"][0]["after_paragraph"] == 0


def test_build_epub_with_assets(tmp_path):
    assets = {
        "img-a": {"data": PNG + b"a", "media_type": "image/png", "alt": "A"},
        "img-b": {"data": PNG + b"b", "media_type": "image/png", "alt": "B"},
        "cover": {"data": PNG + b"c", "media_type": "image/png", "alt": "Cover"},
    }
    chapters = [
        {"title": "One", "paragraphs": ["Hello world"],
         "assets": {"inline": [{"asset_id": "img-a", "paragraph": 0, "offset": 6}],
                    "block": []}},
        {"title": "Two", "paragraphs": ["First", "Second"],
         "assets": {"inline": [],
                    "block": [{"asset_id": "img-b", "after_paragraph": 0}]}},
    ]
    output = tmp_path / "book.epub"
    build_epub(output=output, title="Book", chapters=chapters, language="zh-TW",
               assets=assets, cover_asset="cover")
    result = verify_epub(output)
    assert result["chapter_files"] == 2
    assert result["image_count"] == 3
    assert result["missing_images"] == []
    with zipfile.ZipFile(output) as archive:
        first = archive.read("OEBPS/text/chapter-0001.xhtml").decode("utf-8")
        second = archive.read("OEBPS/text/chapter-0002.xhtml").decode("utf-8")
        assert "Hello <img" in first and "/>world</p>" in first
        assert "First</p>" in second and 'class="image"' in second
        assert "Second</p>" in second
