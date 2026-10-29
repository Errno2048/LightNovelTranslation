from __future__ import annotations

import json

from lightnovel_translation import (GenerationResult, TranslationConfig,
                                    TranslationProject)
from translation.index import (LevelConfig, TranslationRunner,
                               TranslationStore, TranslationWorkflow)


class StaleProvider:
    """clean translate/review; second review reports a major finding."""

    def __init__(self):
        self.requests = []
        self.reviews = 0

    def generate(self, request):
        self.requests.append(request.task)
        task = request.task
        if task.startswith("batch_glossary_") or task.startswith("glossary_"):
            return GenerationResult("@@END", "stop", "scripted", task, {}, 0)
        if task.startswith("translate_"):
            import re
            target_msg = next(m["content"] for m in request.messages
                              if m["content"].startswith("【当前目标"))
            aliases = list(dict.fromkeys(
                re.findall(r"\[(S\d{3})\]", target_msg)))
            body = "\n".join(
                f"@@TRANSLATION {a}\ntext: 诺亚回到了村子。"
                for a in aliases)
            return GenerationResult(
                f"@@TITLE\ntext: 归来\n{body}\n@@END",
                "stop", "scripted", task, {}, 0)
        if task.startswith("review_"):
            self.reviews += 1
            if self.reviews == 1:
                return GenerationResult(
                    "@@SUMMARY\nreviewed_all: yes\n"
                    "semantic_coverage_complete: yes\nzero_findings: yes\n@@END",
                    "stop", "scripted", task, {}, 0)
            return GenerationResult(
                "@@FINDING f1\ncategory: negation\nseverity: major\n"
                "basis: source_meaning\nsource_ref: S001\ntarget_ref: T001\n"
                "claim: 术语修改后该译法不再成立。\n"
                "suggestion: 改为正确译名。\nconfidence: high\n"
                "@@SUMMARY\nreviewed_all: yes\n"
                "semantic_coverage_complete: yes\nzero_findings: no\n@@END",
                "stop", "scripted", task, {}, 0)
        if task.startswith("adjudicate_"):
            return GenerationResult(
                "@@DECISION d1\nfinding: f1\ndecision: confirmed\n"
                "reason: 术语已更新。\n@@END",
                "stop", "scripted", task, {}, 0)
        if task.startswith(("revise_", "index_")):
            return GenerationResult("@@END", "stop", "scripted", task, {}, 0)
        raise AssertionError("unexpected task " + task)


def _project(tmp_path, provider):
    ja = tmp_path / "ja.txt"
    zh = tmp_path / "zh.txt"
    ja.write_text("第1話 帰還\n\nノアは村に帰った。\n\n"
                  "第2話 出発\n\nノアは村を出た。\n", encoding="utf-8")
    zh.write_text("第1章 归来\n\n诺亚回到了村子。\n\n", encoding="utf-8")
    project = TranslationProject(tmp_path / "project", TranslationConfig(),
                                 provider=provider, provider_id="stale-v1")
    project.import_book(ja, edition_id="ja", language="ja",
                        paragraph_policy="blank_line")
    project.import_book(zh, edition_id="zh", language="zh",
                        paragraph_policy="blank_line")
    flow = TranslationWorkflow(project, "ja", "zh",
                               levels=LevelConfig((2, 2), (1, 1)),
                               full_chapters=1)
    return project, flow


def test_stale_review_pass_promotes_major_to_manual(tmp_path):
    provider = StaleProvider()
    project, flow = _project(tmp_path, provider)
    try:
        with TranslationStore(tmp_path / "translation.sqlite") as store:
            runner = TranslationRunner(
                project, flow, store, continuation_start=1, main_end=2,
                reference_texts={0: "诺亚回到了村子。"},
                require_reference_glossary=False)
            runner.failure_policy = "continue"
            result = runner.run_concurrent(
                start=1, end=2, batch_size=1, translation_concurrency=1,
                review_concurrency=1, update_summaries=False)
            assert result["errors"] == []
            assert store.chapter(1)["status"] == "accepted"
            # Simulate a term revision marker hitting chapter 1.
            store.event("term_revision_required", order=1,
                        target="诺亚", reason="test")
            outcome = runner.stale_review_pass()
            assert 1 in outcome["manual"], outcome
            row = store.chapter(1)
            assert row["status"] == "manual_review"
            review = json.loads(row["review"])
            assert review["stale_review"]["causes"] == [
                "term_revision_required"]
    finally:
        project.close()


def test_stale_review_skips_unchanged_chapters(tmp_path):
    provider = StaleProvider()
    project, flow = _project(tmp_path, provider)
    try:
        with TranslationStore(tmp_path / "translation.sqlite") as store:
            runner = TranslationRunner(
                project, flow, store, continuation_start=1, main_end=2,
                reference_texts={0: "诺亚回到了村子。"},
                require_reference_glossary=False)
            runner.failure_policy = "continue"
            runner.run_concurrent(start=1, end=2, batch_size=1,
                                  translation_concurrency=1,
                                  review_concurrency=1,
                                  update_summaries=False)
            reviews_before = provider.reviews
            outcome = runner.stale_review_pass()
            assert outcome["reviewed"] == [] and outcome["manual"] == []
            assert provider.reviews == reviews_before
    finally:
        project.close()
