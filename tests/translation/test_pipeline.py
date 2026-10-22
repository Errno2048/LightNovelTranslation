from __future__ import annotations

from pathlib import Path
import re
import threading

from lightnovel_translation import GenerationResult, TranslationConfig, TranslationProject
from translation.index import (TranslationRunner, TranslationWorkflow, LevelConfig,
                               TranslationStore, build_epub, verify_epub)


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
    def __init__(self):
        self.requests = []
        self.reviews = 0

    def generate(self, request):
        self.requests.append(request)
        if request.task.startswith("translate_"):
            text = ("@@TITLE\ntext: 归来\n"
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
            text = ("@@TITLE\ntext: 归来\n"
                    "@@TRANSLATION S001\ntext: 诺亚回到了村子。\n"
                    "@@TRANSLATION S002\ntext: 众人听他讲述旅途。\n@@END")
        else:
            raise AssertionError("unexpected task " + request.task)
        return GenerationResult(text, "stop", "scripted", str(len(self.requests)), {}, 0)


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
            result = runner.run_order(1)
            assert result["status"] == "accepted"
            assert store.translations()[1] == "诺亚回到了村子。\n\n众人听他讲述旅途。"
            assert store.chapter(1)["title_zh"] == "归来"


class ConcurrentProvider:
    def __init__(self):
        self.requests = []
        self.lock = threading.Lock()

    def generate(self, request):
        with self.lock:
            self.requests.append(request)
        if request.task.startswith("batch_glossary_"):
            return GenerationResult("[]", "stop", "scripted", request.task, {}, 0)
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
