"""P4.1 contracts. Source visibility and translation visibility are distinct."""
from dataclasses import asdict, dataclass, field

from ..knowledge.schemas import KnowledgeError
from ..storage import digest

VERSION = "narrative-v1"


class NarrativeError(KnowledgeError):
    pass


@dataclass
class NarrativePolicy:
    target_id: str | None = None
    cutoff_order: int | None = None
    reserved_source_ids: list[str] = field(default_factory=list)
    hidden_translation_source_ids: list[str] = field(default_factory=list)
    allowed_translation_source_ids: list[str] | None = None
    after_chapters: int = 0
    after_blocks: int = 0

    def __post_init__(self):
        if (self.target_id is None) != (self.cutoff_order is None):
            raise NarrativeError("unsupported_policy", "target and preceding cutoff must be specified together")
        if self.after_chapters or self.after_blocks:
            raise NarrativeError("unsupported_policy", "future reference context is not implemented")
        if self.cutoff_order is not None and type(self.cutoff_order) is not int:
            raise NarrativeError("unsupported_policy", "cutoff must be an integer")
        for name in ("reserved_source_ids", "hidden_translation_source_ids", "allowed_translation_source_ids"):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
                    raise NarrativeError("unsupported_policy", "invalid source allow/deny list")
                setattr(self, name, sorted(set(value)))

    def public(self):
        return asdict(self)

    @property
    def fingerprint(self):
        return digest(self.public())


@dataclass
class BlockConfig:
    min_chapters: int = 20
    max_chapters: int = 40
    max_input_tokens: int = 150000
    detail_chapters: int = 4

    def __post_init__(self):
        if any(type(x) is not int or x <= 0 for x in asdict(self).values()) or self.min_chapters > self.max_chapters:
            raise NarrativeError("invalid_record", "invalid block bounds")


@dataclass
class ContextSpec:
    n_chapter: int = 3
    n_block: int = 2
    context_limit: int = 128000
    max_output_tokens: int = 8192
    safety_tokens: int = 2048
    include_summaries: bool = True
    require_reviewed_summaries: bool = False

    def __post_init__(self):
        if any(type(x) is not int or x < 0 for x in (self.n_chapter, self.n_block, self.safety_tokens)):
            raise NarrativeError("invalid_record", "nonnegative integer windows required")
        if self.max_output_tokens <= 0 or self.input_budget <= 0:
            raise NarrativeError("budget_exceeded", "output reserve leaves no input budget")

    @property
    def input_budget(self):
        return self.context_limit - self.max_output_tokens - self.safety_tokens
