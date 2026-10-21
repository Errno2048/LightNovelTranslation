"""Generic level configuration for the hierarchical translation index."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LevelConfig:
    """Levels are contiguous and 1-based (index 0 is level 1).

    ``max_children`` defines which levels exist. A missing level 3 simply
    means level 3 is disabled; it does not mean "unlimited".
    ``context_num_blocks`` may be shorter; missing entries fall back to the
    highest configured value.
    """

    max_children: tuple[int, ...]
    context_num_blocks: tuple[int, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "max_children", tuple(self.max_children))
        object.__setattr__(self, "context_num_blocks", tuple(self.context_num_blocks))
        if not self.max_children:
            raise ValueError("at least level 1 must be configured")
        if any(type(value) is not int or value <= 0 for value in self.max_children):
            raise ValueError("max_children values must be positive integers")
        if self.context_num_blocks:
            if len(self.context_num_blocks) > len(self.max_children):
                raise ValueError("context_num_blocks cannot configure disabled levels")
            if any(type(value) is not int or value <= 0
                   for value in self.context_num_blocks):
                raise ValueError("context_num_blocks values must be positive integers")

    @property
    def max_level(self) -> int:
        return len(self.max_children)

    def enabled(self, level: int) -> bool:
        return 1 <= level <= self.max_level

    def require_level(self, level: int) -> int:
        if not self.enabled(level):
            raise ValueError(f"level {level} is not configured")
        return level

    def max_children_for(self, level: int) -> int:
        return self.max_children[self.require_level(level) - 1]

    def context_blocks_for(self, level: int) -> int:
        self.require_level(level)
        if not self.context_num_blocks:
            return 1
        if level <= len(self.context_num_blocks):
            return self.context_num_blocks[level - 1]
        return self.context_num_blocks[-1]

    def validate_artifact_level(self, level: int, *, chapter: bool) -> None:
        if chapter:
            if level != 0:
                raise ValueError("chapter artifacts must use level 0")
            return
        self.require_level(level)
