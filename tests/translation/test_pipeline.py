from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import json
import re
import threading
import zipfile

import pytest

from lightnovel_translation import (GenerationResult, Limits, TranslationConfig,
                                    TranslationProject, WorkflowConfig)
from translation.index import (TranslationRunner, TranslationWorkflow, LevelConfig,
                               TranslationStore, build_epub, verify_epub)
from translation.providers import ProviderError


class ScriptedProvider:
    def __init__(self):
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        if request.task.startswith("translate_"):
            text = ("@@TITLE\ntext: 归来\n"
                    "@@TRANSLATION S001\ntext: 诺亚回到了村子。\n"
                    "@@TRANSLATION S002\ntext: 众人听他讲述旅途。\n@@END")
        elif request.task.startswith("review_"):
            text = ("@@SUMMARY\nreviewed_all: yes\n"
                    "semantic_coverage_complete: yes\nzero_findings: yes\n@@END")
        else:
            raise AssertionError("unexpected task " + request.task)
        return GenerationResult(text, "stop", "scripted", str(len(self.requests)), {}, 0)


class RevisionProvider:
    translate_title = "归来"
    revise_title = "归来"

    def __init__(self):
        self.requests = []
        self.reviews = 0

    def generate(self, request):
        self.requests.append(request)
        if request.task.startswith("translate_"):
            text = (f"@@TITLE\ntext: {self.translate_title}\n"
                    "@@TRANSLATION S001\ntext: 诺亚没有回到村子。\n"
                    "@@TRANSLATION S002\ntext: 众人听他讲述旅途。\n@@END")
        elif request.task.startswith("review_"):
            self.reviews += 1
            if self.reviews == 1:
                text = ("@@FINDING f1\ncategory: negation\nseverity: major\n"
                        "basis: source_meaning\nsource_ref: S001\ntarget_ref: T001\n"
                        "claim: 原文是肯定，草稿加入否定。\n"
                        "suggestion: 改为“诺亚回到了村子。”\nconfidence: high\n"
                        "@@SUMMARY\nreviewed_all: yes\n"
                        "semantic_coverage_complete: yes\nzero_findings: no\n@@END")
            else:
                text = ("@@SUMMARY\nreviewed_all: yes\n"
                        "semantic_coverage_complete: yes\nzero_findings: yes\n@@END")
        elif request.task.startswith("adjudicate_"):
            text = ("@@DECISION d1\nfinding: f1\ndecision: confirmed\n"
                    "reason: 原文明确为肯定，草稿加入否定。\n@@END")
        elif request.task.startswith("revise_"):
            text = (f"@@TITLE\ntext: {self.revise_title}\n"
                    "@@TRANSLATION S001\ntext: 诺亚回到了村子。\n"
                    "@@TRANSLATION S002\ntext: 众人听他讲述旅途。\n@@END")
        else:
            raise AssertionError("unexpected task " + request.task)
        return GenerationResult(text, "stop", "scripted", str(len(self.requests)), {}, 0)


class TitleRevisionProvider(RevisionProvider):
    translate_title = "第二章 出发"
    revise_title = "第二章 归来了"


def test_pipelined_mode_config_validation():
    from lightnovel_translation.errors import ConfigurationError
    assert WorkflowConfig().translation_mode == "batch"
    assert WorkflowConfig(translation_mode="pipelined").pipeline_review_backlog == 2
    assert WorkflowConfig(adjudication_mode="strict").adjudication_mode == "strict"
    with pytest.raises(ConfigurationError):
        WorkflowConfig(translation_mode="unknown")
    with pytest.raises(ConfigurationError):
        WorkflowConfig(pipeline_review_backlog=0)
    with pytest.raises(ConfigurationError):
        WorkflowConfig(pipeline_manual_backlog=0)
    with pytest.raises(ConfigurationError):
        WorkflowConfig(adjudication_mode="unknown")


def test_runner_accepts_a_clean_review(tmp_path: Path):
    japanese = tmp_path / "ja.txt"
    chinese = tmp_path / "zh.txt"
    japanese.write_text("第1話 帰還\n\nノアは村に帰った。\n\n皆は旅の話を聞いた。\n\n"
                        "第2話 出発\n\nノアは村を出た。\n\n皆は見送った。\n",
                        encoding="utf-8")
    chinese.write_text("第1章 归来\n\n诺亚回到了村子。\n\n众人听他讲述旅途。\n\n"
                       "第2章 出发\n\n诺亚离开了村子。\n\n众人为他送行。\n",
                       encoding="utf-8")
    provider = ScriptedProvider()
    with TranslationProject(tmp_path / "project",
                            TranslationConfig(), provider=provider,
                            provider_id="scripted-v1") as project:
        project.import_book(japanese, edition_id="ja", language="ja",
                            paragraph_policy="blank_line")
        project.import_book(chinese, edition_id="zh", language="zh",
                            paragraph_policy="blank_line")
        flow = TranslationWorkflow(project, "ja", "zh",
                             levels=LevelConfig((2, 2), (1, 1)), full_chapters=1)
        with TranslationStore(tmp_path / "translation.sqlite") as store:
            runner = TranslationRunner(
                project, flow, store, continuation_start=1, main_end=2,
                reference_texts={0: "诺亚回到了村子。\n\n众人听他讲述旅途。"})
            assert runner.translation_tokens == project.config.workflow.translation_tokens
            assert runner.review_tokens == project.config.workflow.review_tokens
            assert runner.adjudication_tokens == project.config.workflow.adjudication_tokens
            assert runner.summary_tokens == project.config.workflow.summary_tokens
            assert runner.glossary_tokens == project.config.workflow.glossary_tokens
            result = runner.run_order(1)
            assert result["status"] == "accepted"
            assert store.translations()[1] == "诺亚回到了村子。\n\n众人听他讲述旅途。"
            assert store.chapter(1)["title_zh"] == "第2章 归来"

            translate = next(request for request in provider.requests
                             if request.task == "translate_1")
            contents = [message["content"] for message in translate.messages]
            rules = next(index for index, content in enumerate(contents)
                         if content.startswith("【译文输出协议 source-records】"))
            target = next(index for index, content in enumerate(contents)
                          if content.startswith("【当前目标"))
            aliases = next(index for index, content in enumerate(contents)
                           if content.startswith("【本次合法源段ID"))
            assert rules < target < aliases

            review = next(request for request in provider.requests
                          if request.task == "review_1")
            contents = [message["content"] for message in review.messages]
            protocol = next(index for index, content in enumerate(contents)
                            if content.startswith("请逐段核对"))
            evidence = next(index for index, content in enumerate(contents)
                            if content.startswith("【审查证据"))
            assert protocol < evidence


class ConcurrentProvider:
    def __init__(self):
        self.requests = []
        self.lock = threading.Lock()

    def generate(self, request):
        with self.lock:
            self.requests.append(request)
        if request.task.startswith("batch_glossary_"):
            return GenerationResult("[]", "stop", "scripted", request.task, {}, 0)
        if request.task.startswith("glossary_"):
            return GenerationResult(
                "@@TERM\nsource: ノア\ntarget: 诺亚\nkind: person\n"
                "confidence: 1\n@@END", "stop", "scripted", request.task, {}, 0)
        if request.task.startswith("translate_"):
            content = "\n".join(message["content"] for message in request.messages)
            aliases = list(dict.fromkeys(re.findall(r"\[(S\d{3})\]", content)))
            body = "\n".join(f"@@TRANSLATION {alias}\ntext: 译文{alias}。"
                             for alias in aliases)
            return GenerationResult(f"@@TITLE\ntext: 归来\n{body}\n@@END",
                                    "stop", "scripted", request.task, {}, 0)
        if request.task.startswith("review_"):
            return GenerationResult(
                "@@SUMMARY\nreviewed_all: yes\n"
                "semantic_coverage_complete: yes\nzero_findings: yes\n@@END",
                "stop", "scripted", request.task, {}, 0)
        raise AssertionError("unexpected task " + request.task)


class PipelinedProvider:
    def __init__(self):
        self.requests = []
        self.lock = threading.Lock()

    def generate(self, request):
        with self.lock:
            self.requests.append(request)
        task = request.task
        if task.startswith("batch_glossary_"):
            target = "诺阿" if task == "batch_glossary_3_3" else "诺亚"
            text = (f"@@TERM\nsource: ノア\ntarget: {target}\n"
                    "kind: person\nconfidence: 3\n@@END\n")
            return GenerationResult(text, "stop", "scripted", task, {}, 0)
        if task.startswith("glossary_"):
            return GenerationResult(
                "@@TERM\nsource: ノア\ntarget: 诺亚\nkind: person\n"
                "confidence: 1\n@@END", "stop", "scripted", task, {}, 0)
        if task.startswith("provisional_summary_"):
            return GenerationResult("未审摘要：诺亚继续旅行。", "stop",
                                    "scripted", task, {}, 0)
        if task.startswith("translate_"):
            content = "\n".join(message["content"]
                                for message in request.messages)
            match = re.search(
                r'"name":\["ノア"\].*?"target_form":\["([^"]+)"\].*?'
                r'"provisional":true', content, re.S)
            if match:
                target = match.group(1)
            elif task == "translate_3":
                target = "诺阿"
            else:
                target = "诺亚"
            aliases = list(dict.fromkeys(re.findall(r"\[(S\d{3})\]", content)))
            body = "\n".join(
                f"@@TRANSLATION {alias}\ntext: {target}段落{index + 1}。"
                for index, alias in enumerate(aliases))
            return GenerationResult(
                f"@@TITLE\ntext: 归来\n{body}\n@@END", "stop",
                "scripted", task, {}, 0)
        if task.startswith("review_"):
            return GenerationResult(
                "@@SUMMARY\nreviewed_all: yes\n"
                "semantic_coverage_complete: yes\nzero_findings: yes\n@@END",
                "stop", "scripted", task, {}, 0)
        raise AssertionError("unexpected task " + task)


def test_run_mode_dispatches_by_configuration():
    runner = object.__new__(TranslationRunner)
    runner.translation_mode = "pipelined"
    runner.run_pipelined = lambda **kwargs: {"mode": "pipelined"}
    runner.run_concurrent = lambda **kwargs: {"mode": "batch"}
    assert runner.run_mode()["mode"] == "pipelined"
    assert runner.run_mode(mode="batch")["mode"] == "batch"


class StrictProvider:
    def __init__(self):
        self.requests = []
        self.lock = threading.Lock()

    def _translation(self, request):
        content = "\n".join(message["content"]
                            for message in request.messages)
        aliases = list(dict.fromkeys(
            re.findall(r"\[(S\d{3})\]", content) +
            ["S" + value for value in re.findall(r"\bS(\d{3})\b", content)]))
        body = "\n".join(f"@@TRANSLATION {alias}\ntext: 诺亚没有回到村子。"
                         for alias in aliases)
        return f"@@TITLE\ntext: 归来\n{body}\n@@END"

    def generate(self, request):
        with self.lock:
            self.requests.append(request)
        task = request.task
        if task.startswith("batch_glossary_"):
            return GenerationResult("@@END", "stop", "scripted", task, {}, 0)
        if task.startswith("glossary_"):
            return GenerationResult(
                "@@TERM\nsource: ノア\ntarget: 诺亚\nkind: person\n"
                "confidence: 1\n@@END", "stop", "scripted", task, {}, 0)
        if task.startswith("provisional_summary_"):
            return GenerationResult("未审摘要。", "stop", "scripted", task, {}, 0)
        if task.startswith("translate_"):
            return GenerationResult(self._translation(request), "stop",
                                    "scripted", task, {}, 0)
        if task.startswith("revise_"):
            return GenerationResult(self._translation(request), "stop",
                                    "scripted", task, {}, 0)
        if task == "review_1":
            return GenerationResult(
                "@@FINDING f1\ncategory: negation\nseverity: major\n"
                "basis: source_meaning\nsource_ref: S001\ntarget_ref: T001\n"
                "claim: 原文是肯定，草稿加入否定。\n"
                "suggestion: 改为“诺亚回到了村子。”\nconfidence: high\n"
                "@@SUMMARY\nreviewed_all: yes\n"
                "semantic_coverage_complete: yes\nzero_findings: no\n@@END",
                "stop", "scripted", task, {}, 0)
        if task.startswith("review_"):
            return GenerationResult(
                "@@SUMMARY\nreviewed_all: yes\n"
                "semantic_coverage_complete: yes\nzero_findings: yes\n@@END",
                "stop", "scripted", task, {}, 0)
        if task.startswith("adjudicate_"):
            return GenerationResult(
                "@@DECISION f1\nfinding: f1\ndecision: confirmed\n"
                "reason: 原文明确为肯定，草稿加入否定。\n@@END",
                "stop", "scripted", task, {}, 0)
        raise AssertionError("unexpected task " + task)


def test_strict_mode_holds_review_but_keeps_translating(tmp_path: Path):
    japanese = tmp_path / "ja.txt"
    chinese = tmp_path / "zh.txt"
    japanese.write_text(
        "第1話 序\n\nノアは村に帰った。\n\n皆は旅の話を聞いた。\n\n"
        "第2話 到着\n\nノアは町に着いた。\n\n皆は迎えた。\n\n"
        "第3話 再会\n\nノアは友に会った。\n\n皆は喜んだ。\n\n"
        "第4話 出発\n\nノアは港へ向かった。\n\n皆は見送った。\n",
        encoding="utf-8")
    chinese.write_text(
        "第1章 序\n\n诺亚回到了村子。\n\n众人听他讲述旅途。\n\n"
        "第2章 到达\n\n诺亚抵达了城镇。\n\n众人迎接他。\n\n"
        "第3章 再会\n\n诺亚见到了朋友。\n\n众人十分高兴。\n\n"
        "第4章 出发\n\n诺亚前往港口。\n\n众人为他送行。\n",
        encoding="utf-8")
    provider = StrictProvider()
    config = TranslationConfig(
        workflow=WorkflowConfig(adjudication_mode="strict"))
    with TranslationProject(tmp_path / "project", config, provider=provider,
                            provider_id="strict-v1") as project:
        project.import_book(japanese, edition_id="ja", language="ja",
                            paragraph_policy="blank_line")
        project.import_book(chinese, edition_id="zh", language="zh",
                            paragraph_policy="blank_line")
        flow = TranslationWorkflow(project, "ja", "zh",
                                   levels=LevelConfig((2, 2), (1, 1)),
                                   full_chapters=1)
        with TranslationStore(tmp_path / "translation.sqlite") as store:
            runner = TranslationRunner(
                project, flow, store, continuation_start=1, main_end=4,
                reference_texts={0: "诺亚回到了村子。\n\n众人听他讲述旅途。"},
                translation_tokens=512, review_tokens=512,
                adjudication_tokens=512, max_revisions=1)
            result = runner.run_pipelined(
                translation_concurrency=1, review_concurrency=1,
                batch_size=1, pipeline_review_backlog=2,
                pipeline_manual_backlog=2, update_summaries=False)
            assert result["manual_review"] == 1
            assert result["ready"] == 2
            assert result["accepted"] == 0
            assert runner.manual_review_queue()[0]["order"] == 1
            tasks = [request.task for request in provider.requests]
            assert "translate_2" in tasks and "translate_3" in tasks
            assert store.chapter(1)["status"] == "manual_review"
            assert store.chapter(2)["status"] == "ready"
            assert store.chapter(3)["status"] == "ready"
            runner.resolve_manual_review(
                1, decision="accept", notes="人工复核接受")
            assert set(store.translations()) == {1, 2, 3}
            review = json.loads(store.chapter(1)["review"])
            assert review["manual_override"] is True
            assert runner.manual_review_queue() == []


def test_pipelined_runner_uses_prior_drafts_and_provisional_terms(tmp_path: Path):
    japanese = tmp_path / "ja.txt"
    chinese = tmp_path / "zh.txt"
    japanese.write_text(
        "第1話 序\n\n旅人は村を出た。\n\n道は長かった。\n\n"
        "第2話 到着\n\nノアは町に着いた。\n\n皆は迎えた。\n\n"
        "第3話 再会\n\nノアは友に会った。\n\n皆は喜んだ。\n\n"
        "第4話 出発\n\nノアは港へ向かった。\n\n皆は見送った。\n",
        encoding="utf-8")
    chinese.write_text(
        "第1章 序\n\n旅人离开了村子。\n\n道路很漫长。\n\n"
        "第2章 到达\n\n诺亚抵达了城镇。\n\n众人迎接他。\n\n"
        "第3章 再会\n\n诺亚见到了朋友。\n\n众人十分高兴。\n\n"
        "第4章 出发\n\n诺亚前往港口。\n\n众人为他送行。\n",
        encoding="utf-8")
    provider = PipelinedProvider()
    with TranslationProject(tmp_path / "project",
                            TranslationConfig(), provider=provider,
                            provider_id="pipelined-v1") as project:
        project.import_book(japanese, edition_id="ja", language="ja",
                            paragraph_policy="blank_line")
        project.import_book(chinese, edition_id="zh", language="zh",
                            paragraph_policy="blank_line")
        flow = TranslationWorkflow(project, "ja", "zh",
                                   levels=LevelConfig((2, 2), (1, 1)),
                                   full_chapters=2)
        with TranslationStore(tmp_path / "translation.sqlite") as store:
            runner = TranslationRunner(
                project, flow, store, continuation_start=1, main_end=4,
                reference_texts={0: "旅人离开了村子。\n\n道路很漫长。"},
                require_reference_glossary=False,
                translation_tokens=512, review_tokens=512,
                adjudication_tokens=512)
            result = runner.run_pipelined(
                translation_concurrency=1, review_concurrency=1,
                batch_size=2, pipeline_review_backlog=2,
                update_summaries=False)
            assert result["mode"] == "pipelined"
            assert result["chapters"] == 3
            translations = store.translations()
            assert set(translations) == {1, 2, 3}
            assert all("诺亚" in text for text in translations.values())
            assert all("诺阿" not in text for text in translations.values())
            term = next(row for row in store.glossary(limit=1000)
                        if row["source"] == "ノア")
            assert term["target"] == "诺亚"
            assert term["confidence"] == 1
            assert term["provisional"] is True
            assert store.summaries("target-provisional")
            assert len([request for request in provider.requests
                        if request.task.startswith("provisional_summary_")]) >= 1
            translated = {request.task: "\n".join(
                message["content"] for message in request.messages)
                for request in provider.requests
                if request.task.startswith("translate_")}
            assert '"provisional":true' in translated["translate_3"]
            assert "未审草稿" in translated["translate_3"]
            events = [json.loads(row[0]) for row in store.db.execute(
                "SELECT payload FROM events WHERE kind='pipeline_batch'")]
            assert len(events) == 2


def test_concurrent_runner_accepts_a_window(tmp_path: Path):
    japanese = tmp_path / "ja.txt"
    chinese = tmp_path / "zh.txt"
    japanese.write_text("第1話 帰還\n\nノアは村に帰った。\n\n皆は旅の話を聞いた。\n\n"
                        "第2話 出発\n\nノアは村を出た。\n\n皆は見送った。\n\n"
                        "第3話 到着\n\nノアは町に着いた。\n\n皆は迎えた。\n",
                        encoding="utf-8")
    chinese.write_text("第1章 归来\n\n诺亚回到了村子。\n\n众人听他讲述旅途。\n\n"
                       "第2章 出发\n\n诺亚离开了村子。\n\n众人为他送行。\n\n"
                       "第3章 到达\n\n诺亚抵达了城镇。\n\n众人迎接他。\n",
                       encoding="utf-8")
    provider = ConcurrentProvider()
    with TranslationProject(tmp_path / "project",
                            TranslationConfig(), provider=provider,
                            provider_id="concurrent-v1") as project:
        project.import_book(japanese, edition_id="ja", language="ja",
                            paragraph_policy="blank_line")
        project.import_book(chinese, edition_id="zh", language="zh",
                            paragraph_policy="blank_line")
        flow = TranslationWorkflow(project, "ja", "zh",
                             levels=LevelConfig((2, 2), (1, 1)), full_chapters=2)
        with TranslationStore(tmp_path / "translation.sqlite") as store:
            runner = TranslationRunner(
                project, flow, store, continuation_start=1, main_end=3,
                reference_texts={0: "诺亚回到了村子。\n\n众人听他讲述旅途。"},
                require_reference_glossary=False,
                translation_tokens=512, review_tokens=512,
                adjudication_tokens=512)
            result = runner.run_concurrent(
                translation_concurrency=2, review_concurrency=2, batch_size=2,
                update_summaries=False)
            assert result["chapters"] == 2
            assert set(store.translations()) == {1, 2}
            translation = [request for request in provider.requests
                           if request.task == "translate_2"]
            assert translation
            text = "\n".join(message["content"] for message in translation[0].messages)
            assert "本批次前文日文" in text
            assert "第1話 帰還" in text
            assert '"provisional_target_orders":[]' in text


def test_glossary_order_is_append_only(tmp_path: Path):
    with TranslationStore(tmp_path / "translation.sqlite") as store:
        store.put_glossary([
            {"source": "アイ", "target": "爱", "kind": "person"},
            {"source": "イオ", "target": "依", "kind": "person"},
        ])
        store.put_glossary([{"source": "ウル", "target": "乌", "kind": "place"}])
        store.put_glossary([{"source": "アイ", "target": "爱", "kind": "person"}])
        assert [row["source"] for row in store.glossary()] == ["アイ", "イオ", "ウル"]


def test_concurrent_runner_warms_the_last_chapter_first(tmp_path: Path):
    japanese = tmp_path / "ja.txt"
    chinese = tmp_path / "zh.txt"
    japanese.write_text(
        "第1話 帰還\n\nノアは村に帰った。\n\n皆は旅の話を聞いた。\n\n"
        "第2話 出発\n\nノアは村を出た。\n\n皆は見送った。\n\n"
        "第3話 到着\n\nノアは町に着いた。\n\n皆は迎えた。\n\n"
        "第4話 再会\n\nノアは友に会った。\n\n皆は喜んだ。\n",
        encoding="utf-8")
    chinese.write_text(
        "第1章 归来\n\n诺亚回到了村子。\n\n众人听他讲述旅途。\n\n"
        "第2章 出发\n\n诺亚离开了村子。\n\n众人为他送行。\n\n"
        "第3章 到达\n\n诺亚抵达了城镇。\n\n众人迎接他。\n\n"
        "第4章 再会\n\n诺亚见到了朋友。\n\n众人十分高兴。\n",
        encoding="utf-8")
    provider = ConcurrentProvider()
    with TranslationProject(tmp_path / "project",
                            TranslationConfig(), provider=provider,
                            provider_id="warmup-v1") as project:
        project.import_book(japanese, edition_id="ja", language="ja",
                            paragraph_policy="blank_line")
        project.import_book(chinese, edition_id="zh", language="zh",
                            paragraph_policy="blank_line")
        flow = TranslationWorkflow(project, "ja", "zh",
                                   levels=LevelConfig((2, 2), (1, 1)),
                                   full_chapters=2)
        with TranslationStore(tmp_path / "translation.sqlite") as store:
            runner = TranslationRunner(
                project, flow, store, continuation_start=1, main_end=4,
                reference_texts={0: "诺亚回到了村子。\n\n众人听他讲述旅途。"},
                require_reference_glossary=False,
                translation_tokens=512, review_tokens=512,
                adjudication_tokens=512)
            result = runner.run_concurrent(
                translation_concurrency=1, review_concurrency=1, batch_size=3,
                update_summaries=False)
            assert result["chapters"] == 3
            tasks = [request.task for request in provider.requests
                     if request.task.startswith("translate_")]
            assert tasks == ["translate_3", "translate_1", "translate_2"]
            rendered = {request.task: "\n".join(message["content"]
                                                for message in request.messages)
                        for request in provider.requests
                        if request.task.startswith("translate_")}
            # Each chapter's shared prefix must be a byte prefix of the next
            # chapter's prompt; this is what makes the cache warm-up effective.
            for earlier, later in (("translate_1", "translate_2"),
                                   ("translate_2", "translate_3")):
                prefix = rendered[earlier].split("【本章未决")[0]
                assert prefix
                assert rendered[later].startswith(prefix)


def test_runner_revises_a_confirmed_major_finding(tmp_path: Path):
    japanese = tmp_path / "ja.txt"
    chinese = tmp_path / "zh.txt"
    japanese.write_text("第1話 帰還\n\nノアは村に帰った。\n\n皆は旅の話を聞いた。\n\n"
                        "第2話 出発\n\nノアは村を出た。\n\n皆は見送った。\n",
                        encoding="utf-8")
    chinese.write_text("第1章 归来\n\n诺亚回到了村子。\n\n众人听他讲述旅途。\n\n"
                       "第2章 出发\n\n诺亚离开了村子。\n\n众人为他送行。\n",
                       encoding="utf-8")
    provider = RevisionProvider()
    with TranslationProject(tmp_path / "project",
                            TranslationConfig(), provider=provider,
                            provider_id="revision-v1") as project:
        project.import_book(japanese, edition_id="ja", language="ja",
                            paragraph_policy="blank_line")
        project.import_book(chinese, edition_id="zh", language="zh",
                            paragraph_policy="blank_line")
        flow = TranslationWorkflow(project, "ja", "zh",
                             levels=LevelConfig((2, 2), (1, 1)), full_chapters=1)
        with TranslationStore(tmp_path / "translation.sqlite") as store:
            runner = TranslationRunner(
                project, flow, store, continuation_start=1, main_end=2,
                reference_texts={0: "诺亚回到了村子。\n\n众人听他讲述旅途。"},
                translation_tokens=512, review_tokens=512,
                adjudication_tokens=512, max_revisions=1)
            result = runner.run_order(1)
            assert result["status"] == "accepted"
            assert result["rounds"] == 1
            assert store.translations()[1] == "诺亚回到了村子。\n\n众人听他讲述旅途。"
            assert store.chapter(1)["version"] == 2


def test_revision_keeps_the_seeded_title_pattern(tmp_path: Path):
    japanese = tmp_path / "ja.txt"
    chinese = tmp_path / "zh.txt"
    japanese.write_text("第1話 帰還\n\nノアは村に帰った。\n\n皆は旅の話を聞いた。\n\n"
                        "第2話 出発\n\nノアは村を出た。\n\n皆は見送った。\n",
                        encoding="utf-8")
    chinese.write_text("第1章 归来\n\n诺亚回到了村子。\n\n众人听他讲述旅途。\n\n"
                       "第2章 出发\n\n诺亚离开了村子。\n\n众人为他送行。\n",
                       encoding="utf-8")
    provider = TitleRevisionProvider()
    with TranslationProject(tmp_path / "project",
                            TranslationConfig(), provider=provider,
                            provider_id="title-revision-v1") as project:
        project.import_book(japanese, edition_id="ja", language="ja",
                            paragraph_policy="blank_line")
        project.import_book(chinese, edition_id="zh", language="zh",
                            paragraph_policy="blank_line")
        flow = TranslationWorkflow(project, "ja", "zh",
                                   levels=LevelConfig((2, 2), (1, 1)),
                                   full_chapters=1)
        with TranslationStore(tmp_path / "translation.sqlite") as store:
            runner = TranslationRunner(
                project, flow, store, continuation_start=1, main_end=2,
                reference_texts={0: "诺亚回到了村子。\n\n众人听他讲述旅途。"},
                translation_tokens=512, review_tokens=512,
                adjudication_tokens=512, max_revisions=1)
            result = runner.run_order(1)
            assert result["status"] == "accepted"
            assert result["rounds"] == 1
            assert store.chapter(1)["title_zh"] == "第2章 出发"


def test_build_and_verify_epub(tmp_path: Path):
    output = tmp_path / "book.epub"
    build_epub(output=output, title="测试书", chapters=[
        {"title": "第一章", "paragraphs": ["第一段足够长的正文。", "第二段。"]},
        {"title": "第二章", "paragraphs": ["第三段。"]},
    ])
    info = verify_epub(output)
    assert info["title"] == "测试书"
    assert info["chapter_files"] == 2
    assert info["spine_items"] == 2
    assert info["total_chars"] > 0


def test_build_bilingual_epub_renders_translation_then_original(tmp_path: Path):
    output = tmp_path / "bilingual.epub"
    build_epub(
        output=output, title="双语测试", bilingual=True,
        chapters=[{
            "title": "第一章",
            "source_title": "第一章 原文",
            "paragraphs": ["译文一。", "译文二。"],
            "source_paragraphs": ["原文一。", "原文二。"],
        }])
    info = verify_epub(output)
    assert info["chapter_files"] == 1
    with zipfile.ZipFile(output) as archive:
        chapter = archive.read("OEBPS/text/chapter-0001.xhtml").decode("utf-8")
        style = archive.read("OEBPS/style.css").decode("utf-8")
    translation = chapter.index('class="bl-translation"')
    original = chapter.index('class="bl-original"')
    assert translation < original
    assert "译文一。" in chapter and "原文一。" in chapter
    assert 'lang="ja"' in chapter
    assert "原文标题：第一章 原文" in chapter
    assert ".bl-pair" in style and ".bl-original" in style


def test_build_bilingual_epub_marks_unpaired_paragraphs(tmp_path: Path):
    output = tmp_path / "bilingual-mismatch.epub"
    build_epub(
        output=output, title="双语测试", bilingual=True,
        chapters=[{
            "title": "第一章",
            "paragraphs": ["译文一。"],
            "source_paragraphs": ["原文一。", "原文二。"],
        }])
    with zipfile.ZipFile(output) as archive:
        chapter = archive.read("OEBPS/text/chapter-0001.xhtml").decode("utf-8")
    assert "段落数不一致：原文 2 段，译文 1 段" in chapter
    assert "（无译文）" in chapter
    assert chapter.count('class="bl-pair"') == 2


def test_collect_epub_skips_extra_and_missing_orders(tmp_path: Path):
    from translation.index import collect_epub_chapters
    japanese = tmp_path / "ja.txt"
    japanese.write_text(
        "第1話 始まり\n\n最初の段落。\n\n次の段落。\n\n"
        "番外編 おまけ\n\n番外の段落。\n\n"
        "第3話 続き\n\n三番目の段落。\n\n"
        "第4話 終わり\n\n最後の段落。\n", encoding="utf-8")
    with TranslationProject(tmp_path / "project", TranslationConfig()) as project:
        project.import_book(japanese, edition_id="ja", language="ja",
                            paragraph_policy="blank_line")
        flow = TranslationWorkflow(project, "ja", "ja",
                             levels=LevelConfig((2, 2), (1, 1)), full_chapters=1)
        with TranslationStore(tmp_path / "translation.sqlite") as store:
            runner = TranslationRunner(
                project, flow, store, continuation_start=0, main_end=4,
                reference_texts={})
            for order in (0, 2, 3):
                source = runner.source(order)
                store.put_draft(
                    order=order, source_id=source["entry_id"],
                    source_hash=source["text_hash"],
                    ja_title=source["raw_title"], title_zh=source["raw_title"],
                    text="译文。", status="accepted", review={})
            chapters = collect_epub_chapters(
                runner=runner, reference_chapters=[], end=4)
            assert [row["order"] for row in chapters] == [0, 2, 3]
            bilingual = collect_epub_chapters(
                runner=runner, reference_chapters=[], end=4, bilingual=True)
            assert bilingual[0]["source_title"] == "第1話 始まり"
            assert bilingual[0]["source_paragraphs"] == [
                "最初の段落。", "次の段落。"]
            assert bilingual[1]["source_paragraphs"] == ["三番目の段落。"]


class FailingProvider:
    def __init__(self, failures):
        self.failures = list(failures)
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        failure = self.failures.pop(0) if self.failures else None
        if failure is not None:
            raise failure
        return GenerationResult("OK", "stop", "fake", str(self.calls),
                                {"prompt_tokens": 10, "completion_tokens": 5}, 0)


def _failure_runner(tmp_path: Path, provider, *, max_attempts=3):
    config = TranslationConfig(
        limits=Limits(max_attempts=max_attempts),
        workflow=WorkflowConfig(summary_tokens=2048, glossary_tokens=1024))
    project = SimpleNamespace(config=config, provider=provider)
    flow = SimpleNamespace(_source_docs=[],
                           _reference_by_source_order={},
                           full_chapters=5)
    store = TranslationStore(tmp_path / "translation.sqlite")
    runner = TranslationRunner(project, flow, store,
                               continuation_start=0, main_end=0)
    return runner, store


def test_timeout_retries_with_bounded_backoff(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("translation.index.pipeline.time.sleep", lambda _: None)
    provider = FailingProvider([ProviderError("timeout", retryable=True)])
    runner, store = _failure_runner(tmp_path, provider, max_attempts=2)
    result = runner._generate([{"role": "user", "content": "x"}], 64, "probe")
    assert result.text == "OK" and provider.calls == 2
    events = [json.loads(row[0]) for row in store.db.execute(
        "SELECT payload FROM events WHERE kind='model'")]
    assert [event["success"] for event in events] == [False, True]
    assert events[0]["error"] == "timeout"
    store.close()


def test_timeout_not_retried_beyond_attempt_budget(tmp_path: Path):
    provider = FailingProvider([ProviderError("timeout", retryable=True),
                                ProviderError("timeout", retryable=True),
                                ProviderError("timeout", retryable=True)])
    runner, store = _failure_runner(tmp_path, provider)
    with pytest.raises(ProviderError):
        runner._generate([{"role": "user", "content": "x"}], 64, "probe")
    assert provider.calls == 3
    store.close()


def test_server_error_retries_with_bounded_backoff(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("translation.index.pipeline.time.sleep", lambda _: None)
    provider = FailingProvider([ProviderError("server", "HTTP 502", retryable=True)])
    runner, store = _failure_runner(tmp_path, provider, max_attempts=2)
    result = runner._generate([{"role": "user", "content": "x"}], 64, "probe")
    assert result.text == "OK" and provider.calls == 2
    store.close()


def test_rate_limit_retries_with_configured_attempts(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("translation.index.pipeline.time.sleep", lambda _: None)
    provider = FailingProvider([ProviderError("rate_limit", retryable=True)])
    runner, store = _failure_runner(tmp_path, provider, max_attempts=2)
    result = runner._generate([{"role": "user", "content": "x"}], 64, "probe")
    assert result.text == "OK" and provider.calls == 2
    events = [json.loads(row[0]) for row in store.db.execute(
        "SELECT payload FROM events WHERE kind='model'")]
    assert [event["success"] for event in events] == [False, True]
    store.close()


def test_workflow_budgets_are_config_driven(tmp_path: Path):
    provider = FailingProvider([])
    runner, store = _failure_runner(tmp_path, provider)
    assert runner.summary_tokens == 2048
    assert runner.glossary_tokens == 1024
    store.close()


def test_persistent_request_limit_is_checked(tmp_path: Path):
    from translation.index import PipelineError
    config = TranslationConfig(limits=Limits(max_requests=1))
    project = SimpleNamespace(config=config, provider=FailingProvider([]))
    flow = SimpleNamespace(_source_docs=[], _reference_by_source_order={},
                           full_chapters=5)
    with TranslationStore(tmp_path / "translation.sqlite") as store:
        runner = TranslationRunner(project, flow, store,
                                   continuation_start=0, main_end=0)
        store.event("model", task="probe", success=True,
                    usage={"prompt_tokens": 10, "completion_tokens": 5})
        with pytest.raises(PipelineError) as caught:
            runner._check_limits()
        assert caught.value.code == "budget"
