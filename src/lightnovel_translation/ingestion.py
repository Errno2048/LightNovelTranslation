"""Deterministic normalization tools for applications and future agent adapters.

All operations here are local and never execute an LLM. Patch/export retain the
original immutable source and provenance guarantees of the normalization engine.
"""
from translation.ingestion import (
    InputSpec, SourceSpec, inspect_input, extract_sources, load_extraction,
    propose_structure, load_structure, read_source_range, apply_structure_patch,
    validate_import, export_corpus, normalize_input,
    ReferenceSetPlan, plan_reference_set,
)

__all__ = ["InputSpec", "SourceSpec", "inspect_input", "extract_sources", "load_extraction",
           "propose_structure", "load_structure", "read_source_range", "apply_structure_patch",
           "validate_import", "export_corpus", "normalize_input",
           "ReferenceSetPlan", "plan_reference_set"]
