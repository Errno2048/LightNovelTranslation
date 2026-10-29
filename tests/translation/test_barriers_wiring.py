from __future__ import annotations

import pytest

from lightnovel_translation import (GenerationResult, TranslationConfig,
                                    TranslationProject, WorkflowConfig)
from lightnovel_translation.errors import ConfigurationError
from translation.index import (LevelConfig, TranslationRunner,
                               TranslationStore, TranslationWorkflow)
from translation.index.pipeline import PipelineError
from test_summary_index import IndexProvider, _chapters, _spec


class IndexTranslateProvider(IndexProvider):
    def generate(self, request):
        task = request.task
        if task.startswith("translate_"):
            return GenerationResult(
                "@@TITLE\ntext: 归来\n"
                "@@TRANSLATION S001\ntext: 诺亚回到了村子。\n@@END",
                "stop", "scripted", task, {}, 0)
        if task.startswith(("review_", "adjudicate_", "revise_")):
            return GenerationResult(
                "@@SUMMARY\nreviewed_all: yes\n"
                "semantic_coverage_complete: yes\nzero_findings: yes\n@@END",
                "stop", "scripted", task, {}, 0)
        return super().generate(request)


def test_execution_policy_config_validation():
    assert WorkflowConfig().execution_policy == "concurrent"
    with pytest.raises(ConfigurationError):
        WorkflowConfig(execution_policy="strict", failure_policy="continue")
    with pytest.raises(ConfigurationError):
        WorkflowConfig(execution_policy="unknown")


def test_barrier_blocks_when_history_gap_cannot_close(tmp_path):
    """With built blocks but a hole in committed coverage, strict mode must
    refuse to translate (summary_barrier), not silently proceed."""
    provider = IndexTranslateProvider()
    project, flow = _runner_with_many_chapters(tmp_path, provider)
    try:
        with TranslationStore(tmp_path / "translation.sqlite") as store:
            runner = TranslationRunner(
                project, flow, store, continuation_start=1, main_end=3,
                reference_texts={0: "艾维离开了村子。"},
                require_reference_glossary=False)
            runner.failure_policy = "continue"
            runner.review_start = "prefix_as_completed"
            runner.execution_policy = "strict"
            index = runner._summary_index()
            from translation.index.summary_index import NOT_CREATED
            for block in sorted(index.blocks.values(),
                                key=lambda b: (b.start, b.level)):
                if block.level == 1 and block.start >= 20:
                    continue  # leave later blocks unbuilt => gap
                if block.status == NOT_CREATED and block.level == 1:
                    index.build_level1(block, committed=True)
            with pytest.raises(PipelineError) as caught:
                runner.run_concurrent(start=40, end=41, batch_size=1,
                                      translation_concurrency=1,
                                      review_concurrency=1,
                                      update_summaries=False)
            assert caught.value.code == "summary_barrier"
    finally:
        project.close()


def _runner_with_many_chapters(tmp_path, provider):
    """A project whose web-ja edition has 50 short chapters."""
    ja = tmp_path / "ja.txt"
    zh = tmp_path / "zh.txt"
    body = "".join(
        f"第{i}話 序\n\nアイビーは村を出た。\n\n森で商人に出会う。\n\n"
        for i in range(1, 51))
    ja.write_text(body, encoding="utf-8")
    zh.write_text("第1章 序\n\n艾维离开了村子。\n\n", encoding="utf-8")
    project = TranslationProject(tmp_path / "project", TranslationConfig(),
                                 provider=provider,
                                 provider_id="barrier-v1")
    project.import_book(ja, edition_id="ja", language="ja",
                        paragraph_policy="blank_line")
    project.import_book(zh, edition_id="zh", language="zh",
                        paragraph_policy="blank_line")
    flow = TranslationWorkflow(project, "ja", "zh",
                               levels=LevelConfig((2, 2), (1, 1)),
                               full_chapters=1)
    return project, flow
