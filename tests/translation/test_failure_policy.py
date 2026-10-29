from __future__ import annotations

import re
import threading

import pytest

from lightnovel_translation import (GenerationResult, TranslationConfig,
                                    TranslationProject, WorkflowConfig)
from lightnovel_translation.errors import ConfigurationError
from translation.index import (LevelConfig, TranslationRunner,
                               TranslationStore, TranslationWorkflow)
from translation.providers import ProviderError


class FlakyProvider:
    def __init__(self, *, fail_once: bool = False):
        self.fail_once = fail_once
        self.fail_always = False
        self.failed = False
        self.lock = threading.Lock()
        self.requests = []

    def generate(self, request):
        with self.lock:
            self.requests.append(request)
            task = request.task
            should_fail = (self.fail_once and task == "translate_1"
                           and (self.fail_always or not self.failed))
            if should_fail:
                self.failed = True
        if should_fail:
            # network errors are now retried with bounded backoff; fail on
            # both attempts so the chapter really ends up blocked.
            raise ProviderError("network", "simulated persistent failure",
                                retryable=True)
        if task.startswith("batch_glossary_"):
            return GenerationResult("@@END", "stop", "scripted", task, {}, 0)
        if task.startswith("translate_"):
            content = "\n".join(message["content"]
                                for message in request.messages)
            aliases = list(dict.fromkeys(re.findall(r"\[(S\d{3})\]", content)))
            body = "\n".join(
                f"@@TRANSLATION {alias}\ntext: 诺亚回到了村子。"
                for alias in aliases)
            return GenerationResult(
                f"@@TITLE\ntext: 归来\n{body}\n@@END",
                "stop", "scripted", task, {}, 0)
        if task.startswith("review_"):
            return GenerationResult(
                "@@SUMMARY\nreviewed_all: yes\n"
                "semantic_coverage_complete: yes\nzero_findings: yes\n@@END",
                "stop", "scripted", task, {}, 0)
        raise AssertionError("unexpected task " + task)


def _project(tmp_path, provider):
    japanese = tmp_path / "ja.txt"
    chinese = tmp_path / "zh.txt"
    japanese.write_text(
        "第1話 帰還\n\nノアは村に帰った。\n\n皆は旅の話を聞いた。\n\n"
        "第2話 出発\n\nノアは村を出た。\n\n皆は見送った。\n\n"
        "第3話 到着\n\nノアは町に着いた。\n\n皆は迎えた。\n",
        encoding="utf-8")
    chinese.write_text(
        "第1章 归来\n\n诺亚回到了村子。\n\n众人听他讲述旅途。\n\n"
        "第2章 出发\n\n诺亚离开了村子。\n\n众人为他送行。\n\n"
        "第3章 到达\n\n诺亚抵达了城镇。\n\n众人迎接他。\n",
        encoding="utf-8")
    project = TranslationProject(
        tmp_path / "project", TranslationConfig(), provider=provider,
        provider_id="flaky-v1")
    project.import_book(japanese, edition_id="ja", language="ja",
                        paragraph_policy="blank_line")
    project.import_book(chinese, edition_id="zh", language="zh",
                        paragraph_policy="blank_line")
    flow = TranslationWorkflow(project, "ja", "zh",
                               levels=LevelConfig((2, 2), (1, 1)),
                               full_chapters=1)
    return project, flow


def test_failure_policy_continue_and_repair(tmp_path):
    provider = FlakyProvider(fail_once=True)
    provider.fail_always = True
    project, flow = _project(tmp_path, provider)
    try:
        with TranslationStore(tmp_path / "translation.sqlite") as store:
            runner = TranslationRunner(
                project, flow, store, continuation_start=1, main_end=3,
                reference_texts={0: "诺亚回到了村子。\n\n众人听他讲述旅途。"},
                require_reference_glossary=False)
            runner.failure_policy = "continue"
            result = runner.run_concurrent(
                start=1, end=3, translation_concurrency=2,
                review_concurrency=2, batch_size=2, update_summaries=False)
            assert result["errors"]
            assert result["errors"][0]["order"] == 1
            assert store.chapter(2)["status"] == "ready"
            provider.fail_always = False
            provider.failed = False
            repaired = runner.repair_pending()
            assert repaired["repaired"] == [1]
            assert store.chapter(1)["status"] == "accepted"
            assert store.chapter(2)["status"] == "accepted"
    finally:
        project.close()


def test_review_start_as_completed_accepts_window(tmp_path):
    provider = FlakyProvider()
    project, flow = _project(tmp_path, provider)
    try:
        with TranslationStore(tmp_path / "translation.sqlite") as store:
            runner = TranslationRunner(
                project, flow, store, continuation_start=1, main_end=3,
                reference_texts={0: "诺亚回到了村子。\n\n众人听他讲述旅途。"},
                require_reference_glossary=False)
            runner.failure_policy = "continue"
            runner.review_start = "as_completed"
            result = runner.run_concurrent(
                start=1, end=3, translation_concurrency=2,
                review_concurrency=2, batch_size=2, update_summaries=False)
            assert not result["errors"]
            assert store.chapter(1)["status"] == "accepted"
            assert store.chapter(2)["status"] == "accepted"
    finally:
        project.close()


def test_as_completed_requires_continue_policy():
    with pytest.raises(ConfigurationError):
        WorkflowConfig(failure_policy="stop_batch",
                       review_start="as_completed")
