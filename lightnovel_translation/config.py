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
from translation.sequence.schemas import SequenceSpec, default_roles

from .errors import ConfigurationError


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
    max_requests: int = 300
    max_input_tokens: int = 20_000_000
    max_output_tokens: int = 3_000_000
    max_attempts: int = 3

    def __post_init__(self) -> None:
        if any(type(v) is not int or v <= 0 for v in asdict(self).values()):
            raise ConfigurationError("limits must be positive integers")
        if self.max_attempts > 3:
            raise ConfigurationError("max_attempts must be between 1 and 3")


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
    capacity: ModelCapacity = field(default_factory=lambda: ModelCapacity(131072, 16384))
    roles: dict[str, RoleBudget] = field(default_factory=dict)
    index: EditionIndexSpec = field(default_factory=EditionIndexSpec)
    sequence: dict[str, Any] = field(default_factory=dict)
    limits: Limits = field(default_factory=Limits)
    tokenizer_path: Path | None = None

    def __post_init__(self) -> None:
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
        controlled = {"capacity", "roles", *asdict(self.limits)}
        allowed = {f.name for f in fields(SequenceSpec)} - controlled
        values = _known(self.sequence, allowed, "sequence")
        # Source-bound records avoid silently accepting merged/missing paragraphs.
        values = {"output_profile": "source-records-v1",
                  "parent_max_chapters": self.index.parent_max_chapters,
                  "child_max_chapters": self.index.child_max_chapters,
                  "child_min_content_tokens": self.index.child_min_content_tokens,
                  **values}
        try:
            return SequenceSpec(self.capacity, roles=self.role_budgets(), **asdict(self.limits), **values)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(str(exc)) from exc

    def public(self) -> dict[str, Any]:
        """Redacted diagnostic identity, deliberately not a credential backup."""
        return {"schema_version": 1, "model": self.model.public() if self.model else None,
                "capacity": asdict(self.capacity),
                "roles": {k: asdict(v) for k, v in self.role_budgets().items()},
                "index": asdict(self.index), "sequence": asdict(self.sequence_spec()),
                "limits": asdict(self.limits),
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
            capacity = ModelCapacity(**value.get("capacity", {"context_window_tokens": 131072,
                                                             "max_output_tokens": 16384}))
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
            return cls(model=model, capacity=capacity,
                       roles={k: RoleBudget(**v) for k, v in value.get("roles", {}).items()},
                       index=EditionIndexSpec(**value.get("index", {})),
                       sequence=value.get("sequence", {}), limits=Limits(**value.get("limits", {})),
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
