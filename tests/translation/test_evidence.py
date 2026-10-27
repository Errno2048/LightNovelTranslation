from __future__ import annotations

from pathlib import Path

from translation.index import (ContextDocument, GlossaryTerm,
                               TranslationRunner, TranslationStore,
                               glossary_evidence)


def _doc(order: int, source: str, target: str) -> ContextDocument:
    return ContextDocument(
        source_id=f"s{order}", order=order, title=f"第{order}章",
        source_text=source, translation_text=target)


def test_evidence_is_source_aligned_and_bounded():
    term = GlossaryTerm("勇魚", "勇鱼", "item", confidence=1, locked=True)
    history = [
        _doc(1, "勇魚が現れた。", "前文。" + ("勇鱼" * 20) + "后文。"),
        _doc(2, "別の話。", "这里也有勇鱼，但源文没有该词。"),
        _doc(3, "勇魚が泳ぐ。", "勇鱼在游。"),
    ]
    evidence = glossary_evidence(
        "勇魚が現れた。", [term], history, near_orders={3},
        excerpt_chars=20, max_excerpts=1, max_chars=1000)
    assert evidence is not None
    assert evidence["schema"] == "targeted-evidence-v2"
    assert evidence["selection"] == "source_aligned_bounded_snippets"
    assert len(evidence["terms"]) == 1
    entry = evidence["terms"][0]
    assert entry["historical_match_count"] == 1
    assert len(entry["historical_excerpts"]) == 1
    excerpt = entry["historical_excerpts"][0]
    assert excerpt["order"] == 1
    assert "勇鱼" in excerpt["text"]
    assert len(excerpt["text"]) < 80
    assert excerpt["text"] != history[0].translation_text


def test_evidence_alias_and_near_context_are_handled():
    term = GlossaryTerm("カッツォ", "鰹", "person", confidence=1, locked=True,
                        aliases=("鰹鱼花",))
    history = [
        _doc(1, "カッツォが来た。", "鰹鱼花来了。"),
        _doc(2, "カッツォが来た。", "鰹来了。"),
    ]
    evidence = glossary_evidence(
        "カッツォ", [term], history, near_orders={2},
        excerpt_chars=10, max_excerpts=2, max_chars=1000)
    assert evidence is not None
    entry = evidence["terms"][0]
    assert entry["historical_match_count"] == 1
    assert entry["historical_excerpts"][0]["order"] == 1


def test_evidence_global_cap_preserves_fixed_terms():
    terms = [
        GlossaryTerm("普通语", "普通语", "other", confidence=3),
        GlossaryTerm("固定名", "固定名", "person", confidence=1, locked=True),
        GlossaryTerm("另一个", "另一个", "item", confidence=2),
    ]
    target = "普通语 固定名 另一个"
    history = [
        _doc(1, "普通语 固定名 另一个",
             "普通语..." * 20 + "固定名..." * 20 + "另一个..." * 20),
    ]
    evidence = glossary_evidence(
        target, terms, history, excerpt_chars=8, max_excerpts=1,
        max_chars=25)
    assert evidence is not None
    assert evidence["truncated"] is True
    assert evidence["omitted_terms"] >= 1
    selected = {entry["source_form"] for entry in evidence["terms"]}
    assert "固定名" in selected


def test_evidence_returns_none_without_matching_history():
    term = GlossaryTerm("勇魚", "勇鱼")
    assert glossary_evidence("勇魚", [term], []) is None
    assert glossary_evidence("别的", [term],
                             [_doc(1, "勇魚", "勇鱼")]) is None


def test_stable_glossary_projection_is_append_only(tmp_path: Path):
    store = TranslationStore(tmp_path / "terms.sqlite")
    runner = object.__new__(TranslationRunner)
    runner.store = store
    runner.prompt_glossary_limit = 2
    try:
        store.put_glossary([
            {"source": "甲語", "target": "甲语", "kind": "person",
             "confidence": 1},
            {"source": "乙語", "target": "乙语", "kind": "place",
             "confidence": 1},
        ])
        first = runner._stable_glossary_cards()
        store.put_glossary([
            {"source": "丙語", "target": "丙语", "kind": "item",
             "confidence": 1},
        ])
        second = runner._stable_glossary_cards()
        assert [term.source for term in first] == ["甲語", "乙語"]
        assert [term.source for term in second] == ["甲語", "乙語"]
    finally:
        store.close()
