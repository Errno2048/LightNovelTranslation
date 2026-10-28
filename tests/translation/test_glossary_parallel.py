from __future__ import annotations

import threading
import time

from lightnovel_translation import (GenerationResult, TranslationConfig,
                                    TranslationProject, WorkflowConfig)
from translation.index import (LevelConfig, TranslationRunner,
                               TranslationStore, TranslationWorkflow)


class ParallelGlossaryProvider:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.tasks = []

    def generate(self, request):
        with self.lock:
            self.tasks.append(request.task)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.15)
            text = ("@@TERM\nsource: ノア\ntarget: 诺亚\nkind: person\n"
                    "confidence: 1\n@@END")
            return GenerationResult(text, "stop", "scripted",
                                    request.task, {}, 0)
        finally:
            with self.lock:
                self.active -= 1


def test_glossary_chunks_run_concurrently_and_write_on_main_thread(tmp_path):
    japanese = tmp_path / "ja.txt"
    chinese = tmp_path / "zh.txt"
    japanese.write_text(
        "第1話 帰還\n\nノアは村に帰った。\n\n"
        "第2話 出発\n\nノアは村を出た。\n\n"
        "第3話 到着\n\nノアは町に着いた。\n",
        encoding="utf-8")
    chinese.write_text(
        "第1章 归来\n\n诺亚回到了村子。\n\n"
        "第2章 出发\n\n诺亚离开了村子。\n\n"
        "第3章 到达\n\n诺亚抵达了城镇。\n",
        encoding="utf-8")
    provider = ParallelGlossaryProvider()
    config = TranslationConfig(
        workflow=WorkflowConfig(glossary_concurrency=2))
    with TranslationProject(tmp_path / "project", config,
                            provider=provider,
                            provider_id="glossary-parallel-v1") as project:
        project.import_book(japanese, edition_id="ja", language="ja",
                            paragraph_policy="blank_line")
        project.import_book(chinese, edition_id="zh", language="zh",
                            paragraph_policy="blank_line")
        flow = TranslationWorkflow(project, "ja", "zh",
                                   levels=LevelConfig((2, 2), (1, 1)),
                                   full_chapters=1)
        with TranslationStore(tmp_path / "translation.sqlite") as store:
            runner = TranslationRunner(
                project, flow, store, continuation_start=3, main_end=3,
                reference_texts={0: "诺亚回到了村子。",
                                 1: "诺亚离开了村子。",
                                 2: "诺亚抵达了城镇。"},
                reference_chunk=2)
            terms = runner.ensure_glossary()
            assert terms
            assert provider.max_active == 2
            complete = store.db.execute(
                "SELECT payload FROM events WHERE kind='glossary_complete'"
                " ORDER BY event_id DESC LIMIT 1").fetchone()
            assert complete is not None
            assert len(store.glossary()) >= 1
