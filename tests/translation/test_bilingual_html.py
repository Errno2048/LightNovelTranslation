from __future__ import annotations

from translation.index import build_bilingual_html


def test_bilingual_html_puts_translation_before_original_and_escapes():
    document = build_bilingual_html(
        title="测试<预览>",
        chapters=[{
            "order": 202,
            "title": "危险<章>",
            "source_title": "危険な<章>",
            "status": "accepted",
            "source_paragraphs": ["原文<一>&", "原文二"],
            "translation_paragraphs": ["译文<一>&", "译文二"],
        }],
    )
    assert document.startswith("<!DOCTYPE html>")
    assert "<title>测试&lt;预览&gt;</title>" in document
    assert "译文&lt;一&gt;&amp;" in document
    assert "原文&lt;一&gt;&amp;" in document
    translation = document.index('class="translation"')
    original = document.index('class="original"')
    assert translation < original
    assert document.count('class="bilingual-pair"') == 2
    assert 'lang="zh-CN"' in document
    assert 'lang="ja"' in document
    assert 'href="#chapter-202"' in document
    assert 'id="chapter-202"' in document


def test_bilingual_html_warns_and_preserves_unpaired_paragraphs():
    document = build_bilingual_html(
        title="段落测试",
        chapters=[{
            "order": 7,
            "title": "章节",
            "status": "ready",
            "source_paragraphs": ["原文一", "原文二"],
            "translation_paragraphs": ["译文一"],
        }],
    )
    assert "段落数不一致：原文 2 段，译文 1 段" in document
    assert "（无译文）" in document
    assert 'data-source-count="2"' in document
    assert 'data-target-count="1"' in document
    assert document.count('class="bilingual-pair"') == 2


def test_bilingual_html_empty_document_has_a_clear_message():
    document = build_bilingual_html(title="空", chapters=[])
    assert "没有可导出的章节。" in document
    assert document.rstrip().endswith("</html>")


def test_bilingual_html_escapes_script_and_uses_source_title_fallback():
    document = build_bilingual_html(
        title="安全",
        chapters=[{
            "order": 1,
            "title": "安全译文",
            "source_title": "原文<script>alert(1)</script>",
            "source_paragraphs": [],
            "translation_paragraphs": ["译文"],
        }],
    )
    assert "<script>" not in document
    assert "&lt;script&gt;" in document
    assert "原文&lt;script&gt;alert(1)&lt;/script&gt;" in document
    assert "原文标题：" in document
