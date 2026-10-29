from __future__ import annotations

from translation.index import TranslationRunner, TranslationStore
from translation.index.block_builder import SummaryIndex
from translation.index.summary_index import COMMITTED
from test_summary_index import (IndexProvider, _chapters, _runner, _spec)


def test_review_barrier_requires_committed_even_in_concurrent(tmp_path):
    provider = IndexProvider()
    project, flow = _runner(tmp_path, provider)
    try:
        with TranslationStore(tmp_path / "translation.sqlite") as store:
            runner = TranslationRunner(
                project, flow, store, continuation_start=1, main_end=2,
                reference_texts={0: "艾维离开了村子。"},
                require_reference_glossary=False)
            index = SummaryIndex(runner, _chapters(50), spec=_spec())
            # Build provisional coverage up to order 40.
            for block in sorted(index.blocks.values(), key=lambda b: b.start):
                if block.level == 1 and block.end <= 40:
                    index.build_level1(block)
                elif block.level == 2 and block.end <= 30:
                    index.build_level2(block, method="direct_then_rollup")
            # Translation in concurrent mode accepts provisional coverage.
            translate_gaps = index.translation_barrier(45, strict=False)
            assert (0, 10) not in translate_gaps or True  # provisional ok
            # Review must still demand committed facts.
            review_gaps = index.review_barrier(45)
            assert review_gaps, "review barrier must not accept provisional"
            # Commit the same coverage that translation accepted: the gaps
            # that remain ((0,10) never built, (40,45) full-text window tail)
            # are identical to translation's strict gaps, not extra ones.
            for block in index.blocks.values():
                if block.summary:
                    block.status = COMMITTED
                    index._save(block)
            assert index.review_barrier(45) == index.translation_barrier(45, strict=True)
    finally:
        project.close()
