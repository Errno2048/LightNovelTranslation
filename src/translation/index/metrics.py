"""Debug metrics for lazy reference-index materialization."""
from __future__ import annotations

import json
import logging
import time
from collections import Counter
from pathlib import Path

LOGGER = logging.getLogger("translation.index")


class IndexMetrics:
    def __init__(self):
        self.counters = Counter()
        self.events = []

    def record(self, event: str, **fields):
        self.counters[event] += 1
        payload = {"event": event, **fields}
        self.events.append(payload)
        LOGGER.debug("index metric %s", payload)

    def as_dict(self) -> dict:
        return dict(self.counters)

    def dump(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for event in self.events:
                handle.write(json.dumps({"ts": time.time(), **event},
                                        ensure_ascii=False) + "\n")
        return len(self.events)

    def clear(self):
        self.counters.clear()
        self.events.clear()
