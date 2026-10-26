from __future__ import annotations

import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from translation.index.pipeline import (ReviewOutcome, TranslationRunner,
                                        TranslationStore, _build_term_checks)
from translation.index.workflow import GlossaryTerm
from translation.providers import HTTPProvider
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
    payload = captured["messages"][-2]["content"]
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
