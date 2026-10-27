from __future__ import annotations

import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from translation.index.pipeline import (ReviewOutcome, TranslationRunner,
                                        TranslationStore, _build_term_checks,
                                        _batch_glossary_prompt,
                                        _reference_focused_prompt,
                                        _reference_glossary_prompt,
                                        parse_term_records)
from translation.index.workflow import GlossaryTerm
from translation.providers import HTTPProvider, ProviderError
from translation.schemas import GenerationRequest
from lightnovel_translation.config import WorkflowConfig


class _Store:
    def __init__(self):
        self.rows = [{
            "source": "ラオブベーア", "target": "殘暴熊", "kind": "race",
            "confidence": 1, "aliases": [], "locked": True,
        }]

    def glossary(self, limit=200):
        return list(self.rows)

    def chapter(self, order):
        return None

    def accepted_orders(self):
        return []

    def event(self, *args, **kwargs):
        return None


class _Response:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class _Client:
    def __init__(self):
        self.payload = None

    def post(self, url, headers, json):
        self.payload = json
        return _Response({
            "id": "r1", "model": "mock",
            "choices": [{"finish_reason": "tool_calls", "message": {
                "content": None,
                "tool_calls": [{"id": "c1", "type": "function", "function": {
                    "name": "lookup_term", "arguments": '{"term":"x"}'}}],
            }}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })


def test_workflow_lookup_defaults():
    workflow = WorkflowConfig()
    assert workflow.lookup_chapters == 8
    assert workflow.lookup_terms == 50
    assert workflow.max_term_checks == 50


def test_term_checks_find_missing_canonical_forms():
    rows = [{"text": "ラオブベーアとエルフ"}, {"text": "組合長"}]
    target = ["殘暴熊與精靈", "組合長"]
    terms = [
        GlossaryTerm("ラオブベーア", "殘暴熊", "race", confidence=1),
        GlossaryTerm("エルフ", "艾爾芙", "race", confidence=1,
                     aliases=("艾爾芙族",)),
        GlossaryTerm("組合長", "公會長", "role", confidence=2),
    ]
    checks = _build_term_checks(rows, target, terms)
    assert [check["term"] for check in checks] == ["エルフ", "組合長"]
    assert checks[0]["status"] == "fixed_missing"
    assert checks[1]["status"] == "needs_context"
    assert checks[0]["source_ref"] == "S001"
    assert checks[1]["target_ref"] == "T002"


def test_termcheck_protocol_requires_coverage_and_finding():
    expected = {"tc001": {"source_ref": "S001", "target_ref": "T001",
                          "term": "ラオブベーア", "canonical": "殘暴熊",
                          "status": "fixed_missing"}}
    text = """@@TERMCHECK tc001
check: tc001
verdict: violation
reason: 译文用了音译而不是规范译名
@@FINDING f1
category: term
severity: major
basis: source_meaning
source_ref: S001
target_ref: T001
claim: 规范译名殘暴熊未出现
suggestion: 改为殘暴熊
confidence: high
@@SUMMARY
reviewed_all: yes
semantic_coverage_complete: yes
zero_findings: no
@@END
"""
    outcome = TranslationRunner.parse_review(
        None, text, {"S001": "ラオブベーア"}, {"T001": "拉奧布貝亞"},
        expected_checks=expected)
    assert not outcome.errors
    assert outcome.term_checks[0]["verdict"] == "violation"
    assert outcome.findings[0]["category"] == "term"

    missing = TranslationRunner.parse_review(
        None, "@@SUMMARY\nreviewed_all: yes\nsemantic_coverage_complete: yes\n"
              "zero_findings: yes\n@@END\n",
        {"S001": "x"}, {"T001": "x"}, expected_checks=expected)
    assert any(error.startswith("missing_termchecks:") for error in missing.errors)


def test_glossary_confidence_and_locked_merge():
    path = Path(tempfile.mkdtemp()) / "terms.sqlite"
    with TranslationStore(path) as store:
        store.put_glossary([{"source": "エルフ", "target": "艾爾芙族",
                             "kind": "race", "confidence": 1,
                             "aliases": ["艾爾芙"]}])
        row = store.glossary()[0]
        assert row["target"] == "艾爾芙族"
        assert row["confidence"] == 1
        assert set(row["aliases"]) == {"艾爾芙"}
        store.put_glossary([{"source": "エルフ", "target": "精靈",
                             "kind": "race", "confidence": 3}])
        row = store.glossary()[0]
        assert row["target"] == "艾爾芙族"
        assert "精靈" in row["aliases"]
        store.put_glossary([{"source": "ロック", "target": "固定",
                             "confidence": 1, "locked": True}])
        store.put_glossary([{"source": "ロック", "target": "変更",
                             "confidence": 1, "locked": True}])
        locked = next(row for row in store.glossary() if row["source"] == "ロック")
        assert locked["target"] == "固定"


def test_glossary_count_is_not_limited_by_default_page():
    path = Path(tempfile.mkdtemp()) / "count.sqlite"
    with TranslationStore(path) as store:
        store.put_glossary([
            {"source": f"語{i:03d}", "target": f"词{i:03d}",
             "kind": "other", "confidence": 3}
            for i in range(205)])
        assert store.glossary_count() == 205
        assert len(store.glossary()) == 200
        assert len(store.glossary(limit=1_000_000)) == 205


def test_provisional_glossary_is_mandatory_and_wins_merges():
    path = Path(tempfile.mkdtemp()) / "provisional.sqlite"
    with TranslationStore(path) as store:
        store.put_glossary([{"source": "ノア", "target": "诺亚",
                             "kind": "person", "confidence": 3}])
        promoted = store.promote_provisional_terms(["ノア"])
        assert promoted and promoted[0]["target"] == "诺亚"
        row = store.glossary()[0]
        assert row["target"] == "诺亚"
        assert row["confidence"] == 1
        assert row["provisional"] is True
        assert row["locked"] is False
        store.put_glossary([{"source": "ノア", "target": "诺阿",
                             "kind": "person", "confidence": 3}])
        row = store.glossary()[0]
        assert row["target"] == "诺亚"
        assert row["provisional"] is True
        term = GlossaryTerm("ノア", "诺亚", "person", 1, (), False, True)
        checks = _build_term_checks([{"text": "ノアが来た"}],
                                    ["诺阿来了"], [term])
        assert checks[0]["canonical"] == "诺亚"
        assert checks[0]["status"] == "fixed_missing"
        assert checks[0]["provisional"] is True


def test_review_tools_read_provisional_drafts():
    runner = object.__new__(TranslationRunner)
    runner.continuation_start = 1
    runner.source_by_order = {
        0: {"raw_title": "ch0", "text": "旅人は村を出た。"},
        1: {"raw_title": "ch1", "text": "ノアが来た。"},
    }
    runner.reference_texts = {0: "旅人离开了村子。"}
    context = {"accepted_texts": {}, "draft_texts": {1: "诺亚来了。"},
               "glossary": [], "events": [], "buffer_events": True}
    budget = {"max_chapters": 2, "max_terms": 5,
              "used_chapters": set(), "used_terms": set()}
    chapter = runner._execute_review_tool(
        "read_chapter", json.dumps({"order": 1}), 2, budget, context)
    assert chapter["language"] == "zh-provisional"
    assert chapter["authority"] == "provisional_draft"
    assert chapter["text"] == "诺亚来了。"
    lookup = runner._execute_review_tool(
        "lookup_term", json.dumps({"term": "诺亚"}), 2, budget, context)
    assert lookup["hits"][0]["authority"] == "provisional_draft"


def test_full_and_short_forms_share_entity_with_distinct_targets():
    path = Path(tempfile.mkdtemp()) / "entity.sqlite"
    with TranslationStore(path) as store:
        store.put_glossary([{"source": "天津気 刹那", "target": "天津气刹那",
                             "kind": "person", "confidence": 1,
                             "locked": True}])
        store.put_glossary([{"source": "セツナ", "target": "刹那",
                             "kind": "person", "confidence": 3,
                             "form_type": "short"}])
        entity = store.term_entity("天津気 刹那")
        assert entity is not None
        linked = store.link_term_form(
            "セツナ", entity["entity_id"], form_type="short", target="刹那")
        assert linked["entity_id"] == entity["entity_id"]
        forms = store.term_entity_forms(entity["entity_id"])
        assert {form["source"] for form in forms} == {"天津気 刹那", "セツナ"}
        targets = {form["source"]: form["target"] for form in forms}
        assert targets == {"天津気 刹那": "天津气刹那", "セツナ": "刹那"}


def test_target_conflict_blocks_provisional_promotion():
    path = Path(tempfile.mkdtemp()) / "conflict.sqlite"
    with TranslationStore(path) as store:
        store.put_glossary([{"source": "天津気 刹那", "target": "天津气刹那",
                             "kind": "person", "confidence": 1,
                             "locked": True}])
        store.put_glossary([{"source": "セツナ", "target": "天津气刹那",
                             "kind": "person", "confidence": 3,
                             "aliases": ["刹那"]}])
        assert store.promote_provisional_terms(["セツナ"]) == []
        row = next(row for row in store.glossary()
                   if row["source"] == "セツナ")
        assert row["confidence"] == 3
        assert row["provisional"] is False
        events = [json.loads(row[0]) for row in store.db.execute(
            "SELECT payload FROM events WHERE kind='term_target_conflict'")]
        assert events and events[0]["source"] == "セツナ"


def test_merge_entities_preserves_each_form_target():
    path = Path(tempfile.mkdtemp()) / "merge.sqlite"
    with TranslationStore(path) as store:
        store.put_glossary([{"source": "天津気 刹那", "target": "天津气刹那",
                             "kind": "person", "confidence": 1},
                            {"source": "セツナ", "target": "刹那",
                             "kind": "person", "confidence": 1}])
        merged = store.merge_term_entities("天津気 刹那", "セツナ")
        assert merged is not None
        forms = {form["source"]: form for form in merged["forms"]}
        assert set(forms) == {"天津気 刹那", "セツナ"}
        assert forms["天津気 刹那"]["target"] == "天津气刹那"
        assert forms["セツナ"]["target"] == "刹那"
        assert forms["天津気 刹那"]["entity_id"] == forms["セツナ"]["entity_id"]


def test_lookup_term_returns_entity_forms():
    path = Path(tempfile.mkdtemp()) / "lookup.sqlite"
    with TranslationStore(path) as store:
        store.put_glossary([{"source": "天津気 刹那", "target": "天津气刹那",
                             "kind": "person", "confidence": 1}])
        store.put_glossary([{"source": "セツナ", "target": "刹那",
                             "kind": "person", "confidence": 1}])
        entity = store.term_entity("天津気 刹那")
        store.link_term_form("セツナ", entity["entity_id"],
                             form_type="short", target="刹那")
        runner = object.__new__(TranslationRunner)
        runner.store = store
        runner.continuation_start = 1
        runner.reference_texts = {}
        runner.source_by_order = {0: {"raw_title": "ch0", "text": "セツナ"}}
        context = {"accepted_texts": {}, "draft_texts": {},
                   "glossary": store.glossary(limit=1000),
                   "term_conflicts": [], "events": [], "buffer_events": True}
        budget = {"max_chapters": 2, "max_terms": 5,
                  "used_chapters": set(), "used_terms": set()}
        result = runner._execute_review_tool(
            "lookup_term", json.dumps({"term": "セツナ"}), 1, budget, context)
        forms = result["glossary"][0]["entity_forms"]
        assert {form["source"] for form in forms} == {"天津気 刹那", "セツナ"}
        assert {form["target"] for form in forms} == {"天津气刹那", "刹那"}


def test_confirm_glossary_locks_candidate():
    path = Path(tempfile.mkdtemp()) / "confirm.sqlite"
    with TranslationStore(path) as store:
        store.put_glossary([{"source": "新語", "target": "候选",
                             "kind": "skill", "confidence": 3}])
        row = store.confirm_glossary_term(
            "新語", target="正式译名", aliases=["候选"], confidence=1,
            locked=True)
        assert row["target"] == "正式译名"
        assert row["confidence"] == 1
        assert row["locked"] is True
        assert row["aliases"] == ["候选"]
        store.put_glossary([{"source": "新語", "target": "另一译名",
                             "confidence": 1}])
        row = store.glossary()[0]
        assert row["target"] == "正式译名"
        assert row["locked"] is True


def test_review_tools_share_budget_and_find_reference_usage():
    runner = object.__new__(TranslationRunner)
    runner.continuation_start = 2
    runner.source_by_order = {
        0: {"raw_title": "ch0", "text": "ラオブベーアがいる。\n\n二段落"},
        1: {"raw_title": "ch1", "text": "別の章"},
    }
    runner.reference_texts = {0: "殘暴熊出現了。\n\n第二段"}
    runner.store = _Store()
    budget = {"max_chapters": 1, "max_terms": 1,
              "used_chapters": set(), "used_terms": set()}
    chapter = runner._read_prior_chapter(0, 2, budget)
    assert chapter["language"] == "zh-reference"
    assert chapter["budget"]["remaining_chapters"] == 0
    blocked = runner._execute_review_tool(
        "read_chapter", '{"order": 1}', 2, budget)
    assert "budget exhausted" in blocked["error"]

    budget = {"max_chapters": 2, "max_terms": 1,
              "used_chapters": set(), "used_terms": set()}
    result = runner._execute_review_tool(
        "lookup_term", '{"term":"ラオブベーア"}', 2, budget)
    assert result["glossary"][0]["target"] == "殘暴熊"
    assert result["hits"][0]["target"].startswith("殘暴熊")
    assert result["budget"]["remaining_terms"] == 0


def test_adjudication_receives_term_canonical_authority():
    runner = object.__new__(TranslationRunner)
    runner.adjudication_tokens = 100
    finding = {"id": "f1", "category": "term", "severity": "major",
               "basis": "source_meaning", "source_ref": "S001",
               "target_ref": "T001", "claim": "译名断裂",
               "suggestion": "改为维哲卫门", "confidence": "high"}
    check = {"id": "tc001", "term": "ウェザエモン", "canonical": "维哲卫门",
             "aliases": [], "status": "fixed_missing", "confidence": 1,
             "locked": True, "occurrence_count": 2, "verdict": "violation",
             "source_ref": "S001", "target_ref": "T001",
             "occurrences": []}
    review = ReviewOutcome(findings=(finding,), unresolved=(),
                           coverage_complete=True, zero_findings=False,
                           raw="", term_checks=(check,))
    captured = {}

    class _Result:
        text = ("@@DECISION f1\nfinding: f1\ndecision: confirmed\n"
                "reason: 词表规范译名缺失。\n@@END\n")

    def _generate(messages, max_tokens, task, **kwargs):
        captured["messages"] = messages
        return _Result()

    runner._generate = _generate
    decisions = runner.adjudicate(SimpleNamespace(order=1), review,
                                  log_event=False)
    assert decisions["f1"]["decision"] == "confirmed"
    contents = [message["content"] for message in captured["messages"]]
    payload = next(content for content in contents
                   if content.startswith("【候选审校意见"))
    instruction = next(index for index, content in enumerate(contents)
                       if content.startswith("每个finding"))
    evidence = next(index for index, content in enumerate(contents)
                    if content.startswith("【候选审校意见"))
    assert instruction < evidence
    assert "ウェザエモン" in payload
    assert "维哲卫门" in payload
    assert "fixed_missing" in payload


def test_fixed_term_violation_forces_revision_even_if_minor():
    runner = object.__new__(TranslationRunner)
    check = {"id": "tc001", "verdict": "violation", "status": "fixed_missing",
             "term": "ドラゴンフライ", "canonical": "DragonFly",
             "occurrences": [{"source_ref": "S001", "target_ref": "T001"}]}
    review = ReviewOutcome(findings=(), unresolved=(), coverage_complete=True,
                           zero_findings=False, raw="", term_checks=(check,))
    confirmed = {
        "f1": {"id": "f1", "category": "term", "severity": "minor",
               "source_ref": "S001", "target_ref": "T001"},
        "f2": {"id": "f2", "category": "other", "severity": "minor",
               "source_ref": "S002", "target_ref": "T002"},
        "f3": {"id": "f3", "category": "other", "severity": "major",
               "source_ref": "S003", "target_ref": "T003"},
    }
    result = runner._revision_findings(confirmed, review)
    assert set(result) == {"f1", "f3"}


def test_review_tools_use_worker_safe_snapshot():
    path = Path(tempfile.mkdtemp()) / "tools.sqlite"
    store = TranslationStore(path)
    store.put_glossary([{"source": "ラオブベーア", "target": "殘暴熊",
                         "kind": "race", "confidence": 1, "locked": True}])
    runner = object.__new__(TranslationRunner)
    runner.store = store
    runner.continuation_start = 2
    runner.source_by_order = {
        0: {"raw_title": "ch0", "text": "ラオブベーアがいる。"},
        1: {"raw_title": "ch1", "text": "別の章"},
    }
    runner.reference_texts = {0: "殘暴熊出現了。"}
    context = {"accepted_texts": {}, "glossary": store.glossary(limit=1000),
               "events": []}
    budget = {"max_chapters": 8, "max_terms": 50,
              "used_chapters": set(), "used_terms": set()}
    with ThreadPoolExecutor(max_workers=1) as pool:
        lookup = pool.submit(
            runner._execute_review_tool, "lookup_term",
            json.dumps({"term": "ラオブベーア"}), 2, budget, context).result()
        chapter = pool.submit(
            runner._execute_review_tool, "read_chapter",
            json.dumps({"order": 0}), 2, budget, context).result()
    assert lookup["glossary"][0]["target"] == "殘暴熊"
    assert lookup["hits"][0]["target"] == "殘暴熊出現了。"
    assert chapter["language"] == "zh-reference"
    assert [event["tool"] for event in context["events"]] == [
        "lookup_term", "read_chapter"]
    assert store.db.execute(
        "SELECT COUNT(*) FROM events WHERE kind='review_tool'").fetchone()[0] == 0
    serial = {**context, "events": [], "buffer_events": False}
    runner._execute_review_tool("lookup_term",
                                json.dumps({"term": "ラオブベーア"}),
                                2, budget, serial)
    assert serial["events"] == []
    assert store.db.execute(
        "SELECT COUNT(*) FROM events WHERE kind='review_tool'").fetchone()[0] == 1
    store.close()


def test_parse_term_records_complete_and_incomplete_tail():
    complete = """@@TERM
source: クリシェ
target: 庫莉茜耶
kind: person
confidence: 1
aliases: 庫莉茜雅|庫莉茜耶
@@TERM
source: ケイオースキューブ
target: 浑沌方块
kind: item
confidence: 2
@@END
"""
    parsed = parse_term_records(complete)
    assert parsed.complete is True
    assert [row["source"] for row in parsed.records] == [
        "クリシェ", "ケイオースキューブ"]
    assert parsed.records[0]["aliases"] == ["庫莉茜雅"]
    assert parsed.records[1]["aliases"] == []

    truncated = """@@TERM
source: カースドプリズン
target: 呪狱
kind: person
confidence: 1
@@TERM
source: メグ
target: Meg
"""
    parsed = parse_term_records(truncated)
    assert parsed.complete is False
    assert [row["source"] for row in parsed.records] == ["カースドプリズン"]
    assert "incomplete_tail" in parsed.errors
    assert "カースドプリズン" in parsed.trimmed
    assert "メグ" not in parsed.trimmed

    missing_end = """@@TERM
source: A
target: 甲
kind: other
confidence: 3
"""
    parsed = parse_term_records(missing_end)
    assert parsed.complete is False
    assert [row["source"] for row in parsed.records] == ["A"]
    assert "missing_end" in parsed.errors

    fullwidth = """@@TERM
source： A
target： 甲
kind： role
confidence： 1
aliases： B|C
@@END
"""
    parsed = parse_term_records(fullwidth)
    assert parsed.records[0]["target"] == "甲"
    assert parsed.records[0]["aliases"] == ["B", "C"]

    invalid = """@@TERM
source: A
target: 甲
kind: creature
confidence: 9
@@END
"""
    parsed = parse_term_records(invalid)
    assert parsed.records[0]["kind"] == "other"
    assert parsed.records[0]["confidence"] == 2
    assert any(error.startswith("invalid_kind:") for error in parsed.errors)
    assert any(error.startswith("invalid_confidence:") for error in parsed.errors)

    prefixed = """@@TERMCHECK tc001
@@TERM
source: A
target: 甲
kind: person
confidence: 1
@@END
"""
    parsed = parse_term_records(prefixed)
    assert [row["source"] for row in parsed.records] == ["A"]


def test_glossary_cap_is_linear():
    runner = object.__new__(TranslationRunner)
    runner.glossary_terms_per_chapter = 5
    runner.glossary_terms_base = 40
    assert runner._glossary_cap(16) == 120
    assert runner._glossary_cap(5) == 65
    assert runner._glossary_cap(0) == 40


def test_extract_terms_resumes_after_truncation():
    runner = object.__new__(TranslationRunner)
    runner.glossary_tokens = 100
    runner.glossary_max_continuations = 4
    calls = []
    script = [
        SimpleNamespace(
            text=("@@TERM\nsource: A\ntarget: 甲\nkind: person\nconfidence: 1\n"
                  "@@TERM\nsource: B\ntarget: 乙\n"),
            finish_reason="length"),
        SimpleNamespace(
            text=("@@TERM\nsource: B\ntarget: 乙\nkind: item\nconfidence: 2\n"
                  "@@END\n"),
            finish_reason="stop"),
    ]

    def _generate(messages, max_tokens, task, **kwargs):
        calls.append([dict(message) for message in messages])
        assert kwargs.get("allow_truncated") is True
        return script.pop(0)

    runner._generate = _generate
    rows, stats = runner._extract_terms(
        system="system", prompt="prompt", task="glossary", max_terms=10)
    assert [row["source"] for row in rows] == ["A", "B"]
    assert stats["rounds"] == 1
    assert stats["truncated"] is True
    assert stats["complete"] is True
    assert len(calls) == 2
    assert "@@TERM" in calls[1][2]["content"]
    assert "上一次输出" in calls[1][3]["content"]


def test_extract_terms_retries_after_no_complete_records():
    runner = object.__new__(TranslationRunner)
    runner.glossary_tokens = 100
    runner.glossary_max_continuations = 2
    calls = []
    script = [
        SimpleNamespace(text="reasoning only", finish_reason="length"),
        SimpleNamespace(
            text="@@TERM\nsource: A\ntarget: 甲\nkind: person\nconfidence: 1\n"
                 "@@END\n", finish_reason="stop"),
    ]

    def _generate(messages, max_tokens, task, **kwargs):
        calls.append([dict(message) for message in messages])
        return script.pop(0)

    runner._generate = _generate
    rows, stats = runner._extract_terms(
        system="system", prompt="prompt", task="glossary", max_terms=10)
    assert [row["source"] for row in rows] == ["A"]
    assert stats["rounds"] == 1
    assert stats["truncated"] is True
    assert stats["complete"] is True
    assert "第一条记录" in calls[1][-1]["content"]


def test_extract_terms_stops_without_progress_or_at_cap():
    runner = object.__new__(TranslationRunner)
    runner.glossary_tokens = 100
    runner.glossary_max_continuations = 4
    repeated = SimpleNamespace(
        text="@@TERM\nsource: A\ntarget: 甲\nkind: person\nconfidence: 1\n",
        finish_reason="length")
    runner._generate = lambda *args, **kwargs: repeated
    rows, stats = runner._extract_terms(
        system="system", prompt="prompt", task="glossary", max_terms=10)
    assert [row["source"] for row in rows] == ["A"]
    assert stats["no_progress"] is True
    assert stats["rounds"] == 1

    calls = []
    runner._generate = lambda *args, **kwargs: (
        calls.append(1) or SimpleNamespace(
            text="@@TERM\nsource: A\ntarget: 甲\nkind: person\nconfidence: 1\n",
            finish_reason="length"))
    rows, stats = runner._extract_terms(
        system="system", prompt="prompt", task="glossary", max_terms=1)
    assert stats["cap_reached"] is True
    assert len(calls) == 1


def test_glossary_prompts_use_records_and_linear_cap():
    general = _reference_glossary_prompt(["pair"], max_terms=120)
    assert "@@TERM" in general
    assert "@@END" in general
    assert "最多 120 项" in general
    assert "aliases 用 | 分隔" in general
    focused = _reference_focused_prompt(["pair"], max_terms=65)
    assert "最多 65 项" in focused
    batch = _batch_glossary_prompt("text", [], max_terms=55)
    assert "最多 55 项" in batch
    assert "@@TERM" in batch


class _LengthClient:
    def post(self, url, headers, json):
        return _Response({
            "id": "r1", "model": "mock",
            "choices": [{"finish_reason": "length", "message": {
                "content": "@@TERM\nsource: A"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })


def test_http_provider_returns_partial_on_length():
    from translation.config import ModelConfig
    provider = HTTPProvider(ModelConfig(model="mock", base_url="http://127.0.0.1:1",
                                        api_key="x", retries=0),
                            client=_LengthClient())
    result = provider.generate(GenerationRequest(
        [{"role": "user", "content": "extract"}], allow_truncated=True))
    assert result.finish_reason == "length"
    assert result.text.startswith("@@TERM")
    with pytest.raises(ProviderError):
        provider.generate(GenerationRequest(
            [{"role": "user", "content": "extract"}]))


def test_http_provider_returns_tool_calls():
    client = _Client()
    from translation.config import ModelConfig
    provider = HTTPProvider(ModelConfig(model="mock", base_url="http://127.0.0.1:1",
                                        api_key="x", retries=0), client=client)
    result = provider.generate(GenerationRequest(
        [{"role": "user", "content": "lookup"}],
        tools=[{"type": "function", "function": {"name": "lookup_term"}}],
        tool_choice="auto"))
    assert result.tool_calls[0]["id"] == "c1"
    assert client.payload["tools"][0]["function"]["name"] == "lookup_term"
    assert client.payload["tool_choice"] == "auto"
