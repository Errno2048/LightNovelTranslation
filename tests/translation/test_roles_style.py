from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path

from translation.index import (CHARACTER_SHEET, EXTRA, MAIN, SETTING,
                               ChapterPlan, ChapterRoleEntry,
                               build_chapter_plan_from_manifest,
                               collect_planned_chapters)
from translation.index.assets import has_image_resources
from translation.index.style import (extract_style_profile,
                                     normalize_punctuation,
                                     render_main_title, strip_title_ordinal,
                                     style_violations)


def test_role_classification_uses_sections_and_title_patterns():
    manifest = {"chapters": [
        {"source_ordinal": 1, "title": "世界について", "section": "序"},
        {"source_ordinal": 2, "title": "1話 始まり", "section": "序"},
        {"source_ordinal": 3, "title": "オトルワ町まで　2話　旅", "section": "オトルワ町まで"},
        {"source_ordinal": 4, "title": "番外編 休日", "section": None},
        {"source_ordinal": 5, "title": "【登場人物 一覧表】", "section": None},
        {"source_ordinal": 6, "title": "オグト隊長さんへ", "section": "番外編 手紙"},
        {"source_ordinal": 7, "title": "400 タイトル", "section": None},
    ]}
    plan = build_chapter_plan_from_manifest("ja", manifest)
    assert plan.role(0) == SETTING
    assert plan.role(1) == MAIN and plan.entry(1).ordinal == 1
    assert plan.role(2) == MAIN and plan.entry(2).ordinal == 2
    assert plan.role(3) == EXTRA
    assert plan.role(4) == CHARACTER_SHEET
    assert plan.role(5) == EXTRA
    assert plan.role(6) == MAIN and plan.entry(6).ordinal == 400
    assert plan.warnings


def test_style_profile_extracts_titles_quotes_and_punctuation():
    profile = extract_style_profile(
        ["「你好。」\n\n世界很安静。", "「再见！」"],
        ["1话 开始", "2话 继续"])
    assert profile.title_marker == "话"
    assert profile.title_has_prefix is False
    assert profile.dialogue_open == "「"
    assert profile.fullwidth is True
    assert strip_title_ordinal("第400话 被吃掉了？", profile) == "被吃掉了？"
    assert strip_title_ordinal("第二章 出发", profile) == "出发"
    assert render_main_title(400, "被吃掉了？", profile) == "400话 被吃掉了？"
    value, changes = normalize_punctuation("“好!”", profile)
    assert value == "「好！」"
    assert "dialogue_quotes" in changes and "ascii_punctuation" in changes
    assert style_violations("“坏”", profile) == ["dialogue_quotes"]


def test_asset_fast_path_detects_absence_of_images():
    root = Path(tempfile.mkdtemp())
    empty = root / "empty.epub"
    with zipfile.ZipFile(empty, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("OEBPS/text/chapter.xhtml", "<html><body>text</body></html>")
    assert has_image_resources(empty) is False
    with_image = root / "image.epub"
    with zipfile.ZipFile(with_image, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("OEBPS/images/a.png", b"\x89PNG\r\n\x1a\n")
    assert has_image_resources(with_image) is True


class _FakeStore:
    def __init__(self, chapters):
        self._chapters = chapters

    def chapter(self, order):
        return self._chapters.get(order)

    def accepted_orders(self):
        return [order for order, row in self._chapters.items()
                if row["status"] == "accepted"]


class _FakeRunner:
    def __init__(self):
        self.source_by_order = {
            order: {"raw_title": f"{order + 1}話", "rows": [{"text": f"原文{order}"}]}
            for order in range(4)
        }
        self.chapter_plan = ChapterPlan(
            edition_id="ja",
            entries={
                0: ChapterRoleEntry(0, "1話", MAIN, ordinal=1),
                1: ChapterRoleEntry(1, "2話", MAIN, ordinal=2),
                2: ChapterRoleEntry(2, "番外", EXTRA),
                3: ChapterRoleEntry(3, "4話", MAIN, ordinal=4),
            },
            main_ratio=0.75)
        self.translate_roles = {MAIN, EXTRA}
        self.continuation_start = 2
        self.main_end = 4
        self.reference_texts = {0: "参考0", 2: "参考2"}
        self.store = _FakeStore({
            1: {"status": "accepted", "title_zh": "译文1", "text": "译1",
                "assets": "{}"},
            3: {"status": "accepted", "title_zh": "译文3", "text": "译3",
                "assets": "{}"},
        })

    def source(self, order):
        return self.source_by_order[order]

    def source_rows(self, order):
        return self.source_by_order[order]["rows"]


def test_planned_collector_interleaves_reference_and_target():
    runner = _FakeRunner()
    reference = [
        {"order": 0, "title": "参考0", "paragraphs": ["参0"]},
        {"order": 2, "title": "参考2", "paragraphs": ["参2"]},
    ]
    chapters = collect_planned_chapters(
        runner=runner, reference_chapters=reference, end=4, bilingual=True)
    assert [row["order"] for row in chapters] == [0, 1, 2, 3]
    assert [row["source"] for row in chapters] == [
        "reference", "target", "reference", "target"]
    assert [row["bilingual_mode"] for row in chapters] == [
        "blocks", "pairs", "blocks", "pairs"]
    assert all(row["source_paragraphs"] for row in chapters)
