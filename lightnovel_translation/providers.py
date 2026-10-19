"""Provider injection contract. Caller-owned providers are never closed by the project."""
from translation.providers import TextGenerator, ProviderError
from translation.schemas import GenerationRequest, GenerationResult

__all__ = ["TextGenerator", "ProviderError", "GenerationRequest", "GenerationResult"]
