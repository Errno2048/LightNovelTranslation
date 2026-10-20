"""Non-secret acquisition settings, independent of model readiness."""
from dataclasses import dataclass, fields
import json
import math
from pathlib import Path
import tomllib

from .errors import ConfigurationError


@dataclass(frozen=True)
class SyosetuConfig:
    concurrency: int = 2
    request_interval: float = 1.0

    def __post_init__(self):
        if type(self.concurrency) is not int or not 1 <= self.concurrency <= 8:
            raise ConfigurationError('syosetu.concurrency must be an integer between 1 and 8')
        if (type(self.request_interval) not in (int, float)
                or not math.isfinite(self.request_interval) or self.request_interval < 0):
            raise ConfigurationError('syosetu.request_interval must be a finite non-negative number')

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) - {f.name for f in fields(cls)}:
            raise ConfigurationError('invalid syosetu configuration fields')
        return cls(**value)

    @classmethod
    def load(cls, path: str | Path | None = None):
        # Parse only this section. No model initialization or key/environment lookup.
        from .user_config import user_config_path
        explicit = path is not None
        path = Path(path).expanduser().resolve() if explicit else user_config_path()
        if not explicit and not path.exists():
            return cls()
        try:
            text = path.read_text(encoding='utf-8-sig')
            if path.suffix.lower() == '.toml':
                value = tomllib.loads(text)
            elif path.suffix.lower() == '.json':
                value = json.loads(text)
            else:
                raise ConfigurationError('configuration file must be TOML or JSON')
            if not isinstance(value, dict) or value.get('schema_version', 1) != 1:
                raise ConfigurationError('unsupported acquisition configuration')
            return cls.from_dict(value.get('syosetu', {}))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError, json.JSONDecodeError):
            raise ConfigurationError('could not load acquisition configuration') from None
