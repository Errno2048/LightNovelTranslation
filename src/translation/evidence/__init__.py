"""Shared evidence helpers retained by the fused index architecture."""
from .targeted import (build_targeted_evidence, message, normalize,
                       validate_history_rows)

__all__ = ["build_targeted_evidence", "message", "normalize", "validate_history_rows"]
