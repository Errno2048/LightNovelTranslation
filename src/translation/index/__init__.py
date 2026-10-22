"""Hierarchical index primitives used by the translation architecture."""
from .artifacts import (Artifact, ArtifactKind, BlockStatus, ChapterStatus,
                        InputRef)
from .context_plan import (BatchPlan, BatchSelection, ContextDocument, ContextPlan,
                           CoverageSegment, build_context_plan, choose_batch_size,
                           preflight_batch)
from .lazy import (DEFAULT_ESTIMATE_FACTOR, MaterializationDecision, PlannedBlock,
                   ReferenceIndexPlan, SingleChapterSplitRequired,
                   choose_materialization, direct_estimate, estimate_chapter_tokens,
                   plan_reference_blocks, required_children_for_high_level,
                   rollup_estimate, split_planned_block)
from .levels import LevelConfig
from .metrics import IndexMetrics
from .pipeline import (ChapterBlocked, DraftChapter, TranslationRunner,
                       PipelineError, ReviewOutcome, TranslationStore,
                       align_reference_by_titles, build_epub,
                       collect_epub_chapters, verify_epub)
from .pointers import (PointerAdvance, PointerEvent, PointerSet, block_identity,
                       block_key, contiguous_advance)
from .scheduler import Task, TaskStore, fair_select, run_once
from .snapshot import IndexSnapshot
from .workflow import TranslationWorkflow, GlossaryTerm, glossary_evidence

__all__ = [
    "Artifact", "ArtifactKind", "BlockStatus", "ChapterStatus", "InputRef",
    "BatchPlan", "BatchSelection", "ContextDocument", "ContextPlan",
    "CoverageSegment", "build_context_plan", "choose_batch_size", "preflight_batch",
    "LevelConfig", "PointerAdvance", "PointerEvent", "PointerSet",
    "IndexSnapshot", "block_identity", "block_key", "contiguous_advance",
    "Task", "TaskStore", "fair_select", "run_once",
    "TranslationWorkflow", "GlossaryTerm", "glossary_evidence",
    "TranslationRunner", "TranslationStore", "DraftChapter",
    "ReviewOutcome", "PipelineError", "ChapterBlocked",
    "align_reference_by_titles", "build_epub", "collect_epub_chapters",
    "verify_epub",
    "DEFAULT_ESTIMATE_FACTOR", "MaterializationDecision", "PlannedBlock",
    "ReferenceIndexPlan", "SingleChapterSplitRequired",
    "choose_materialization", "direct_estimate", "estimate_chapter_tokens",
    "plan_reference_blocks", "required_children_for_high_level",
    "rollup_estimate", "split_planned_block", "IndexMetrics",
]
