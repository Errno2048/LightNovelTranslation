"""Generic EPUB/TXT import and structure normalization.

The package keeps source bytes and structural decisions separate from the
legacy site-specific corpus readers.  It deliberately produces reviewable
artifacts before anything is exported to the translation pipeline.
"""

from .schemas import InputSpec, SourceSpec
from .reference import ReferenceSetPlan, plan_reference_set
from .service import (
    apply_structure_patch,
    export_corpus,
    extract_sources,
    inspect_input,
    load_extraction,
    load_structure,
    normalize_input,
    propose_structure,
    read_source_range,
    validate_import,
)

__all__ = [
    "ReferenceSetPlan", "plan_reference_set",
    "apply_structure_patch",
    "normalize_input",
    "read_source_range",
    "InputSpec",
    "SourceSpec",
    "inspect_input",
    "extract_sources",
    "load_extraction",
    "propose_structure",
    "load_structure",
    "validate_import",
    "export_corpus",
]
