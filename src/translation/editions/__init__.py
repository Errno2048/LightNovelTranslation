"""Independent edition indexes and purpose-scoped translation references."""
from .archive import EditionArchive, classify_reference_mode, parallel_documents
from .index import EditionIndexSpec, plan_edition_index
from .service import EditionPipeline
from .sequence import CrossEditionSequenceBridge, build_sequence_inputs, validate_reference_packet

__all__ = ['EditionArchive', 'EditionIndexSpec', 'EditionPipeline',
           'classify_reference_mode', 'parallel_documents', 'plan_edition_index',
           'CrossEditionSequenceBridge', 'build_sequence_inputs', 'validate_reference_packet']
