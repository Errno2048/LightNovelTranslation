"""Read local credentials without including them in serializable settings."""
from __future__ import annotations

import os
import runpy
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .budget import ModelCapacity, RoleBudget


@dataclass
class ModelConfig:
    model: str
    base_url: str
    api_key: str = field(repr=False)
    dimension: int | None = None
    timeout: float = 240
    retries: int = 2
    thinking: bool | str | None = False
    reasoning_effort: str | None = None
    capacity: ModelCapacity | None = None
    trust_env: bool = True

    def __post_init__(self):
        url = urlsplit(self.base_url)
        if url.scheme not in {"https", "http"} or not url.netloc or url.username or url.password or url.query or url.fragment:
            raise ValueError("invalid endpoint; credentials/query must not be in URL")
        if not self.model or self.timeout <= 0 or not 0 <= self.retries <= 5:
            raise ValueError("invalid model or timeout/retry configuration")
        if self.dimension is not None and self.dimension <= 0:
            raise ValueError("invalid embedding dimension")
        if type(self.trust_env) is not bool:
            raise ValueError('trust_env must be boolean')
        if self.thinking is not None and type(self.thinking) is not bool and self.thinking != "omit":
            raise ValueError("thinking must be true, false, or 'omit'")
        if self.reasoning_effort is not None and self.reasoning_effort not in {"low", "medium", "high", "max", "xhigh", "minimal", "none"}:
            raise ValueError("invalid reasoning effort")

    def public(self) -> dict:
        result = {"model": self.model, "base_url": self.base_url, "dimension": self.dimension,
                "timeout": self.timeout, "retries": self.retries, "thinking": self.thinking,
                "credential_alias": "configured-local-key"}
        # Preserve historical provider identities when this optional field is unset.
        if self.reasoning_effort is not None:
            result["reasoning_effort"] = self.reasoning_effort
        if self.capacity is not None:
            from dataclasses import asdict
            result["capacity"] = asdict(self.capacity)
        if not self.trust_env:
            result['transport']={'trust_env':False,'tls_verification':True}
        return result


@dataclass
class Settings:
    generation: ModelConfig
    embedding: ModelConfig
    roles: dict[str, RoleBudget] = field(default_factory=dict)
    index: dict = field(default_factory=dict)
    sequence: dict = field(default_factory=dict)

    def public(self) -> dict:
        from dataclasses import asdict
        result = {"generation": self.generation.public(), "embedding": self.embedding.public()}
        if self.roles:
            result["roles"] = {k: asdict(v) for k, v in self.roles.items()}
        if self.index: result["index"] = self.index
        if self.sequence: result["sequence"] = self.sequence
        return result


def load_settings(root: Path, overrides: Path | None = None) -> Settings:
    key_file = root / "config/apikey.py"
    local = runpy.run_path(str(key_file)) if key_file.exists() else {}
    explicit = tomllib.loads(overrides.read_text(encoding="utf-8")) if overrides else {}
    configs = {}
    for name, prefix in [("generation", "LLM"), ("embedding", "EMBEDDING")]:
        base = {"model": local.get(prefix + "_MODEL", ""), "base_url": local.get(prefix + "_BASE_URL", ""),
                "api_key": local.get(prefix + "_API_KEY", "")}
        if name == "embedding":
            base["dimension"] = local.get("EMBEDDING_DIM", 1024)
        env_endpoint = os.environ.get("NOVELFETCH_"+prefix+"_BASE_URL")
        if env_endpoint is not None and env_endpoint != base["base_url"] and "NOVELFETCH_"+prefix+"_API_KEY" not in os.environ:
            raise ValueError("environment endpoint change requires explicit API_KEY environment variable")
        for attr, suffix in [("model", "MODEL"), ("base_url", "BASE_URL"), ("api_key", "API_KEY")]:
            env = os.environ.get("NOVELFETCH_" + prefix + "_" + suffix)
            if env is not None:
                base[attr] = env
        dim = os.environ.get("NOVELFETCH_EMBEDDING_DIM")
        if name == "embedding" and dim:
            base["dimension"] = int(dim)
        section = explicit.get(name, {})
        allowed = {"model", "base_url", "api_key_env", "dimension", "timeout", "retries", "thinking", "reasoning_effort", "trust_env"}
        if set(section) - allowed:
            raise ValueError(f"unknown {name} configuration fields")
        # Changing endpoints requires an explicit credential identity, even when reusing a key.
        if "base_url" in section and section["base_url"] != base["base_url"] and "api_key_env" not in section:
            raise ValueError("endpoint override requires api_key_env to prevent credential misrouting")
        base.update({k: v for k, v in section.items() if k != "api_key_env"})
        if "api_key_env" in section:
            base["api_key"] = os.environ.get(section["api_key_env"], "")
        if name == "generation" and explicit.get("capacity"):
            base["capacity"] = ModelCapacity(**explicit["capacity"])
        configs[name] = ModelConfig(**base)
    roles = {k: RoleBudget(**v) for k, v in explicit.get("roles", {}).items()}
    if configs["generation"].capacity and any(v.requested_output_tokens > configs["generation"].capacity.max_output_tokens for v in roles.values()):
        raise ValueError("role output exceeds model capacity")
    return Settings(**configs, roles=roles, index=explicit.get("index", {}), sequence=explicit.get("sequence", {}))

