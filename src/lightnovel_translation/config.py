"""Repository-independent TOML/JSON configuration. No Python files are executed."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
import json
import os
from pathlib import Path
import tomllib
from typing import Any
from urllib.parse import urlsplit

from translation.budget import ModelCapacity, RoleBudget
from translation.config import ModelConfig as _EngineModelConfig
from translation.editions.index import EditionIndexSpec
from translation.config_spec import SequenceSpec, default_roles

from .errors import ConfigurationError
from .acquisition_config import SyosetuConfig


@dataclass(frozen=True)
class WorkflowConfig:
    """Defaults for the resumable, batch-concurrent translation workflow."""
    translation_concurrency: int = 3
    review_concurrency: int = 4
    glossary_concurrency: int = 2
    translation_mode: str = "batch"
    pipeline_review_backlog: int = 2
    pipeline_manual_backlog: int = 2
    adjudication_mode: str = "normal"
    batch_size: int = 30
    full_chapters: int = 5
    history_chapters: int = 3
    summary_every: int = 5
    summary_lag: int = 0
    reference_chunk: int = 16
    max_revisions: int = 2
    translation_tokens: int = 65536
    review_tokens: int = 65536
    adjudication_tokens: int = 32768
    summary_tokens: int = 16384
    glossary_tokens: int = 16384
    glossary_terms_per_chapter: int = 5
    glossary_terms_base: int = 40
    glossary_max_continuations: int = 4
    prompt_glossary_limit: int = 512
    evidence_excerpt_chars: int = 120
    evidence_max_excerpts: int = 2
    evidence_max_chars: int = 12000
    lookup_chapters: int = 8
    lookup_terms: int = 50
    max_term_checks: int = 50
    max_tool_rounds: int = 4
    translate_extras: bool = True
    translate_reference_pages: bool = True
    summary_extras: bool = True
    main_ratio_floor: float = 0.90
    require_reference_glossary: bool = True
    auto_reference_glossary: bool = True
    strict_term_conflicts: bool = True
    style_profile: bool = True
    bilingual_mode: str = "auto"
    backfill: str = "ask"
    failure_policy: str = "stop_batch"
    review_start: str = "barrier"

    def __post_init__(self):
        for name in ('translation_concurrency', 'review_concurrency',
                     'glossary_concurrency',
                     'pipeline_review_backlog', 'pipeline_manual_backlog',
                     'batch_size', 'full_chapters', 'summary_every',
                     'reference_chunk', 'translation_tokens', 'review_tokens',
                     'adjudication_tokens', 'summary_tokens', 'glossary_tokens',
                     'glossary_terms_per_chapter', 'evidence_excerpt_chars',
                     'evidence_max_excerpts', 'evidence_max_chars',
                     'lookup_chapters', 'lookup_terms', 'max_tool_rounds'):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ConfigurationError(f'workflow.{name} must be a positive integer')
        if self.translation_mode not in {'batch', 'pipelined'}:
            raise ConfigurationError(
                "workflow.translation_mode must be 'batch' or 'pipelined'")
        if self.adjudication_mode not in {'normal', 'strict'}:
            raise ConfigurationError(
                "workflow.adjudication_mode must be 'normal' or 'strict'")
        if type(self.max_term_checks) is not int or self.max_term_checks < 0:
            raise ConfigurationError('workflow.max_term_checks must be a nonnegative integer')
        if (type(self.prompt_glossary_limit) is not int or
                self.prompt_glossary_limit < 0):
            raise ConfigurationError(
                'workflow.prompt_glossary_limit must be a nonnegative integer')
        if type(self.glossary_terms_base) is not int or self.glossary_terms_base < 0:
            raise ConfigurationError('workflow.glossary_terms_base must be a nonnegative integer')
        if (type(self.glossary_max_continuations) is not int or
                self.glossary_max_continuations < 0):
            raise ConfigurationError('workflow.glossary_max_continuations must be a nonnegative integer')
        if type(self.history_chapters) is not int or self.history_chapters < 0:
            raise ConfigurationError('workflow.history_chapters must be a nonnegative integer')
        if type(self.summary_lag) is not int or self.summary_lag < 0:
            raise ConfigurationError('workflow.summary_lag must be a nonnegative integer')
        if type(self.max_revisions) is not int or not 0 <= self.max_revisions <= 8:
            raise ConfigurationError('workflow.max_revisions must be between 0 and 8')
        for name in ('translate_extras', 'translate_reference_pages',
                     'summary_extras', 'require_reference_glossary',
                     'auto_reference_glossary', 'strict_term_conflicts',
                     'style_profile'):
            if type(getattr(self, name)) is not bool:
                raise ConfigurationError(f'workflow.{name} must be boolean')
        if (type(self.main_ratio_floor) not in (int, float) or
                not 0 <= float(self.main_ratio_floor) <= 1):
            raise ConfigurationError(
                'workflow.main_ratio_floor must be between 0 and 1')
        if self.bilingual_mode not in {'auto', 'pairs', 'blocks'}:
            raise ConfigurationError(
                "workflow.bilingual_mode must be 'auto', 'pairs' or 'blocks'")
        if self.backfill not in {'ask', 'all', 'extras', 'none'}:
            raise ConfigurationError(
                "workflow.backfill must be 'ask', 'all', 'extras' or 'none'")
        if self.failure_policy not in {'stop_batch', 'continue'}:
            raise ConfigurationError(
                "workflow.failure_policy must be 'stop_batch' or 'continue'")
        if self.review_start not in {'barrier', 'as_completed', 'prefix_as_completed'}:
            raise ConfigurationError(
                "workflow.review_start must be 'barrier', 'as_completed' or "
                "'prefix_as_completed'")
        if self.review_start in {'as_completed', 'prefix_as_completed'} and self.failure_policy != 'continue':
            raise ConfigurationError(
                "workflow.review_start='as_completed' or 'prefix_as_completed' "
                "requires failure_policy='continue'")


@dataclass(frozen=True)
class ReferenceConfig:
    """Optional prose budgets; None derives a limit from the model capacity."""
    packet_token_budget: int | None = None
    style_max_documents: int = 3
    style_excerpt_paragraphs: int = 48
    terminology_max_terms: int = 16
    terminology_input_tokens: int | None = None
    terminology_output_tokens: int | None = None
    terminology_max_documents: int = 12
    terminology_radius: int = 2
    discover_terms: bool = True

    def __post_init__(self):
        for key, value in asdict(self).items():
            if key == 'discover_terms':
                if type(value) is not bool:
                    raise ConfigurationError('references.discover_terms must be boolean')
            elif value is None and key in {'packet_token_budget', 'terminology_input_tokens', 'terminology_output_tokens'}:
                continue
            elif type(value) is not int or value < (0 if key == 'terminology_radius' else 1):
                raise ConfigurationError('references.' + key + ' must be a positive integer')

    def budgets(self, capacity, roles):
        def available(output):
            limit = min(capacity.context_window_tokens-output-capacity.safety_tokens,
                        capacity.max_input_tokens or capacity.context_window_tokens)
            return max(1, int(limit/capacity.token_inflation_factor))
        packet = available(max(v.requested_output_tokens for v in roles.values()))
        term_output = min(self.terminology_output_tokens or roles['indexer'].requested_output_tokens,
                          capacity.max_output_tokens)
        term_input = available(term_output)
        return {'packet_capacity_tokens': packet,
                'packet_token_budget': min(self.packet_token_budget or packet, packet),
                'terminology_input_tokens': min(self.terminology_input_tokens or max(1, term_input//4), term_input),
                'terminology_output_tokens': term_output}


@dataclass
class ModelConfig(_EngineModelConfig):
    """OpenAI-compatible model settings; retries belong to the durable engine."""
    retries: int = 0
    thinking: bool | str | None = "omit"

    def __post_init__(self) -> None:
        if not isinstance(self.api_key, str):
            raise ConfigurationError("model.api_key must be a string")
        super().__post_init__()


@dataclass(frozen=True)
class Limits:
    """Durable limits, separately applied to each index and translation job."""
    retry_max_wait_seconds: float = 8.0
    max_requests: int = 0
    max_input_tokens: int = 0
    max_output_tokens: int = 0
    max_attempts: int = 3

    def __post_init__(self) -> None:
        integer_fields = {k: v for k, v in asdict(self).items()
                          if k != 'retry_max_wait_seconds'}
        if any(type(v) is not int or v < 0 for v in integer_fields.values()):
            raise ConfigurationError("limits must be nonnegative integers (0 means unlimited)")
        if not 1 <= self.max_attempts <= 3:
            raise ConfigurationError("max_attempts must be between 1 and 3")
        if type(self.retry_max_wait_seconds) not in (int, float) or self.retry_max_wait_seconds <= 0:
            raise ConfigurationError("limits.retry_max_wait_seconds must be positive")


def _known(value: dict, names: set[str], section: str) -> dict:
    if not isinstance(value, dict):
        raise ConfigurationError(section + " must be an object/table")
    unknown = set(value) - names
    if unknown:
        raise ConfigurationError(section + ": unknown fields: " + ", ".join(sorted(unknown)))
    return dict(value)


@dataclass(frozen=True)
class TranslationConfig:
    """All advanced engine settings, with conservative new-run defaults.

    Capacity is a caller-declared limit, never a claim about a model's abilities.
    Without a model the project supports local operations and provider injection.
    """
    model: ModelConfig | None = None
    capacity: ModelCapacity = field(default_factory=lambda: ModelCapacity(1048576, 131072))
    roles: dict[str, RoleBudget] = field(default_factory=dict)
    index: EditionIndexSpec = field(default_factory=EditionIndexSpec)
    sequence: dict[str, Any] = field(default_factory=dict)
    limits: Limits = field(default_factory=Limits)
    references: ReferenceConfig = field(default_factory=ReferenceConfig)
    tokenizer_path: Path | None = None
    syosetu: SyosetuConfig = field(default_factory=SyosetuConfig)
    workflow: WorkflowConfig = field(default_factory=WorkflowConfig)

    def __post_init__(self) -> None:
        if not isinstance(self.syosetu, SyosetuConfig):
            raise ConfigurationError('syosetu must be a SyosetuConfig instance')
        if not isinstance(self.references, ReferenceConfig):
            raise ConfigurationError('references must be a ReferenceConfig instance')
        if not isinstance(self.workflow, WorkflowConfig):
            raise ConfigurationError('workflow must be a WorkflowConfig instance')
        for name in ('translation_tokens', 'review_tokens', 'adjudication_tokens',
                     'summary_tokens', 'glossary_tokens'):
            if getattr(self.workflow, name) > self.capacity.max_output_tokens:
                raise ConfigurationError(
                    f'workflow.{name} exceeds capacity.max_output_tokens')
        if self.model and self.model.retries != 0:
            raise ConfigurationError("model.retries must be 0; use limits.max_attempts for durable retries")
        if self.model and self.model.capacity and self.model.capacity != self.capacity:
            raise ConfigurationError("model.capacity and capacity differ")
        if set(self.roles) - set(default_roles()):
            raise ConfigurationError("unknown role names: " + ", ".join(sorted(set(self.roles)-set(default_roles()))))
        if any(not isinstance(v, RoleBudget) for v in self.roles.values()):
            raise ConfigurationError("roles values must be RoleBudget instances")
        self.sequence_spec()  # Validate every option before creating a project.

    def role_budgets(self) -> dict[str, RoleBudget]:
        budgets = {k: RoleBudget(min(v.requested_output_tokens, self.capacity.max_output_tokens))
                   for k, v in default_roles().items()}
        budgets.update(self.roles)
        for auxiliary, main in {"mapping": "reviewer", "repair": "reviewer", "revision": "translator"}.items():
            if auxiliary not in self.roles:
                budgets[auxiliary] = budgets[main]
        return budgets

    def sequence_spec(self) -> SequenceSpec:
        controlled = {"capacity", "roles", *asdict(self.limits).keys()}
        allowed = {f.name for f in fields(SequenceSpec)} - controlled
        values = _known(self.sequence, allowed, "sequence")
        # Source-bound records avoid silently accepting merged/missing paragraphs.
        values = {"output_profile": "source-records",
                  "parent_max_chapters": self.index.parent_max_chapters,
                  "child_max_chapters": self.index.child_max_chapters,
                  "index_layout": self.index.layout,
                  "parent_min_chapters": self.index.parent_min_chapters,
                  "expected_l2_count": self.index.expected_l2_count,
                  "l2_prepartition_min_chapters": self.index.l2_prepartition_min_chapters,
                  "parent_build_mode": self.index.parent_build_mode,
                  "entity_catalog_threshold": self.index.entity_catalog_threshold,
                  "core_entity_limit": self.index.core_entity_limit,
                  "child_min_content_tokens": self.index.child_min_content_tokens,
                  **values}
        # Accept profile names written by earlier releases while keeping the
        # current workflow vocabulary canonical.
        aliases = {
            "output_profile": {"plain-v1": "plain",
                               "source-records-v1": "source-records"},
            "review_profile": {"audit-v5": "chapter-review",
                               "audit-v6": "chapter-review"},
            "reference_profile": {"legacy": "plain", "stable-v1": "stable"},
            "mapping_profile": {"audit-v2": "coarse", "coarse-v1": "coarse"},
            "index_layout": {"legacy-v1": "plain", "token-v2": "token"},
        }
        for key, mapping in aliases.items():
            if key in values:
                values[key] = mapping.get(values[key], values[key])
        try:
            sequence_limits = {k: v for k, v in asdict(self.limits).items()
                               if k != 'retry_max_wait_seconds'}
            return SequenceSpec(self.capacity, roles=self.role_budgets(), **sequence_limits, **values)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(str(exc)) from exc

    def public(self) -> dict[str, Any]:
        """Redacted diagnostic identity, deliberately not a credential backup."""
        return {"schema_version": 1, "model": self.model.public() if self.model else None,
                "capacity": asdict(self.capacity),
                "roles": {k: asdict(v) for k, v in self.role_budgets().items()},
                "index": asdict(self.index), "sequence": asdict(self.sequence_spec()),
                "workflow": asdict(self.workflow),
                "limits": asdict(self.limits),
                "references": asdict(self.references),
                "syosetu": asdict(self.syosetu),
                "reference_budgets": self.references.budgets(self.capacity, self.role_budgets()),
                "tokenizer_path": str(self.tokenizer_path) if self.tokenizer_path else None}

    def generation_issues(self) -> list[str]:
        """Return missing field names only, without exposing credential values."""
        if self.model is None:
            return ["model"]
        issues = []
        if self.model.model in {"YOUR_MODEL", "your-model", "fill-in-model-name"}:
            issues.append("model.model")
        host = urlsplit(self.model.base_url).hostname or ""
        if host == "invalid" or host.endswith(".invalid"):
            issues.append("model.base_url")
        if not self.model.api_key.strip() or self.model.api_key in {"YOUR_API_KEY", "FILL_LOCALLY"}:
            issues.append("model.api_key")
        return issues

    @classmethod
    def from_user_config(cls, directory: str | Path | None = None) -> TranslationConfig:
        """Load ~/.lightnovel_translation/config.toml, without displaying it."""
        from .user_config import user_config_path
        return cls.from_file(user_config_path(directory))

    @classmethod
    def load_default(cls) -> TranslationConfig:
        """Use the user's reusable file when present; otherwise allow local-only work."""
        from .user_config import user_config_path
        path = user_config_path()
        return cls.from_file(path) if path.is_file() else cls()

    @classmethod
    def from_dict(cls, value: dict[str, Any], *, base: Path | None = None) -> TranslationConfig:
        value = _known(value, {f.name for f in fields(cls)} | {"schema_version"}, "configuration")
        if value.pop("schema_version", 1) != 1:
            raise ConfigurationError("unsupported configuration schema_version")
        try:
            capacity = ModelCapacity(**value.get("capacity", {
                "context_window_tokens": 1048576, "max_output_tokens": 131072}))
            raw_model = value.get("model")
            model = None
            if raw_model is not None:
                raw_model = _known(raw_model, {f.name for f in fields(ModelConfig)} - {"capacity"} | {"api_key_env"}, "model")
                key_env = raw_model.pop("api_key_env", "LIGHTNOVEL_TRANSLATION_API_KEY")
                if not isinstance(key_env, str) or not key_env.strip():
                    raise ConfigurationError("model.api_key_env must name an environment variable")
                if not raw_model.get("api_key"):
                    raw_model["api_key"] = os.environ.get(key_env, "")
                # Omit provider-specific thinking parameters by default.
                raw_model.setdefault("thinking", "omit")
                # Transport retries would be invisible to the durable request ledger.
                raw_model.setdefault("retries", 0)
                if raw_model["retries"] != 0:
                    raise ConfigurationError("model.retries must be 0; use limits.max_attempts for durable retries")
                model = ModelConfig(**raw_model)
            tokenizer = value.get("tokenizer_path")
            if tokenizer is not None:
                tokenizer = Path(tokenizer)
                if not tokenizer.is_absolute():
                    tokenizer = (base or Path.cwd()) / tokenizer
                tokenizer = tokenizer.resolve()
            raw_sequence = value.get("sequence", {})
            raw_workflow = value.get("workflow")
            if raw_workflow is None:
                raw_workflow = {}
            else:
                raw_workflow = _known(raw_workflow,
                                      {f.name for f in fields(WorkflowConfig)},
                                      "workflow")
            # Legacy sequence files still supply concurrency controls that
            # existed before the dedicated workflow section. Other old fields
            # do not override the new workflow defaults.
            for key in ("translation_concurrency", "review_concurrency"):
                if key not in raw_workflow and key in raw_sequence:
                    raw_workflow[key] = raw_sequence[key]
            workflow = WorkflowConfig(**raw_workflow)
            return cls(model=model, capacity=capacity,
                       roles={k: RoleBudget(**v) for k, v in value.get("roles", {}).items()},
                       index=EditionIndexSpec(**value.get("index", {})),
                       sequence=raw_sequence, workflow=workflow,
                       limits=Limits(**value.get("limits", {})),
                       references=ReferenceConfig(**value.get('references', {})),
                       syosetu=SyosetuConfig.from_dict(value.get('syosetu', {})),
                       tokenizer_path=tokenizer)
        except ConfigurationError:
            raise
        except (TypeError, ValueError, AttributeError) as exc:
            raise ConfigurationError("invalid translation configuration: " + str(exc)) from exc

    @classmethod
    def from_file(cls, path: str | Path) -> TranslationConfig:
        path = Path(path).expanduser().resolve()
        try:
            if path.suffix.lower() == ".toml":
                value = tomllib.loads(path.read_text(encoding="utf-8-sig"))
            elif path.suffix.lower() == ".json":
                value = json.loads(path.read_text(encoding="utf-8-sig"))
            else:
                raise ConfigurationError("configuration file must be TOML or JSON")
            return cls.from_dict(value, base=path.parent)
        except (tomllib.TOMLDecodeError, json.JSONDecodeError):
            # Parser exception objects may retain the entire credential document.
            raise ConfigurationError("invalid configuration syntax") from None
