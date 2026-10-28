"""Hierarchical index primitives used by the translation architecture."""
from .artifacts import (Artifact, ArtifactKind, BlockStatus, ChapterStatus,
                        InputRef)
from .backfill import BackfillGap, BackfillPlan, BackfillRegion, plan_backfill
from .context_plan import (BatchPlan, BatchSelection, ContextDocument, ContextPlan,
                           CoverageSegment, NearContext, batch_near_reserve,
                           build_context_plan, choose_batch_size, near_context,
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
                       align_reference_by_titles, build_bilingual_html,
                       build_epub, build_review_report, collect_epub_chapters,
                       collect_planned_chapters, verify_epub)
from .pointers import (PointerAdvance, PointerEvent, PointerSet, block_identity,
                       block_key, contiguous_advance)
from .roles import (AFTERWORD, ALL_ROLES, CHARACTER_SHEET, ChapterPlan,
                    ChapterRoleEntry, EXTRA, MAIN, PREFACE, SETTING,
                    SUMMARY_ROLES, TRANSLATED_ROLES, UNKNOWN, build_chapter_plan,
                    build_chapter_plan_from_manifest, classify_document,
                    load_chapter_plan, normalize_title as normalize_chapter_title)
from .scheduler import Task, TaskStore, fair_select, run_once
from .snapshot import IndexSnapshot
from .style import (StyleProfile, extract_style_profile, normalize_punctuation,
                    render_main_title, strip_title_ordinal, style_violations)
from .workflow import TranslationWorkflow, GlossaryTerm, glossary_evidence

__all__ = [
    "Artifact", "ArtifactKind", "BlockStatus", "ChapterStatus", "InputRef",
    "BackfillPlan", "BackfillRegion", "BackfillGap", "plan_backfill",
    "BatchPlan", "BatchSelection", "ContextDocument", "ContextPlan",
    "CoverageSegment", "NearContext", "batch_near_reserve", "near_context",
    "build_context_plan", "choose_batch_size", "preflight_batch",
    "LevelConfig", "PointerAdvance", "PointerEvent", "PointerSet",
    "IndexSnapshot", "block_identity", "block_key", "contiguous_advance",
    "Task", "TaskStore", "fair_select", "run_once",
    "TranslationWorkflow", "GlossaryTerm", "glossary_evidence",
    "ChapterPlan", "ChapterRoleEntry", "MAIN", "EXTRA", "CHARACTER_SHEET",
    "SETTING", "PREFACE", "AFTERWORD", "UNKNOWN", "ALL_ROLES",
    "TRANSLATED_ROLES", "SUMMARY_ROLES", "classify_document",
    "build_chapter_plan", "build_chapter_plan_from_manifest",
    "load_chapter_plan", "normalize_chapter_title",
    "StyleProfile", "extract_style_profile", "normalize_punctuation",
    "render_main_title", "strip_title_ordinal", "style_violations",
    "TranslationRunner", "TranslationStore", "DraftChapter",
    "ReviewOutcome", "PipelineError", "ChapterBlocked",
    "align_reference_by_titles", "build_bilingual_html", "build_epub",
    "build_review_report", "collect_epub_chapters",
    "collect_planned_chapters", "verify_epub",
    "DEFAULT_ESTIMATE_FACTOR", "MaterializationDecision", "PlannedBlock",
    "ReferenceIndexPlan", "SingleChapterSplitRequired",
    "choose_materialization", "direct_estimate", "estimate_chapter_tokens",
    "plan_reference_blocks", "required_children_for_high_level",
    "rollup_estimate", "split_planned_block", "IndexMetrics",
]
