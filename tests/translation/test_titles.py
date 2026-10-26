from __future__ import annotations

import tempfile
from pathlib import Path

from translation.index.pipeline import DraftChapter, TranslationRunner, TranslationStore
from translation.index.titles import (format_chinese_number, parse_target_template,
                                     parse_title_pattern, render_title_template,
                                     title_pattern_key)


def _draft(order: int, source: str, target: str) -> DraftChapter:
    return DraftChapter(
        order=order, source_id=f"s{order}", source_hash=f"h{order}",
        ja_title=source, title_zh=target, text="正文。",
        paragraphs=("正文。",), version=0, review={})


def test_title_parser_and_number_format():
    first = parse_title_pattern("あなたの為のオーケストラ 其の一")
    last = parse_title_pattern("あなたの為のオーケストラ 其の二十二")
    assert first is not None and last is not None
    assert first.ordinal == 1
    assert last.ordinal == 22
    assert title_pattern_key(first) == title_pattern_key(last)
    half = parse_title_pattern("倶に天を戴いて 其の九半")
    assert half is not None and half.ordinal == 9 and half.suffix.startswith("半")
    assert format_chinese_number(1) == "一"
    assert format_chinese_number(10) == "十"
    assert format_chinese_number(22) == "二十二"
    assert parse_title_pattern("ただの固有タイトル") is None


def test_target_template_and_render():
    target = parse_target_template("为您独奏的管弦乐团 其二十二", 22)
    assert target is not None
    assert target.style == "chinese"
    assert target.template == "为您独奏的管弦乐团 其{n}"
    assert render_title_template(target.template, 9, target.style) == \
        "为您独奏的管弦乐团 其九"
    arabic = parse_target_template("为您独奏的管弦乐团 其22", 22)
    assert arabic is not None and arabic.style == "arabic"
    assert render_title_template(arabic.template, 3, arabic.style) == \
        "为您独奏的管弦乐团 其3"


def test_title_pattern_seed_and_normalize():
    store = TranslationStore(Path(tempfile.mkdtemp()) / "titles.sqlite")
    runner = object.__new__(TranslationRunner)
    runner.store = store
    try:
        drafts = [
            _draft(1, "あなたの為のオーケストラ 其の一", "为您独奏的管弦乐团 其一"),
            _draft(2, "あなたの為のオーケストラ 其の二", "为你独奏的管弦乐 其2"),
        ]
        runner._seed_title_patterns(drafts)
        first = runner._apply_title_normalization(drafts[0])
        second = runner._apply_title_normalization(drafts[1])
        assert first.title_zh == "为您独奏的管弦乐团 其一"
        assert second.title_zh == "为您独奏的管弦乐团 其二"
        pattern = store.term_form(
            title_pattern_key(parse_title_pattern(drafts[0].ja_title)))
        assert pattern is not None
        assert pattern["form_type"] == "title_pattern"
        assert pattern["provisional"] is True
    finally:
        store.close()
