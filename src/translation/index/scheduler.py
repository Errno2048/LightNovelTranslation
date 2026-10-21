"""Persistent task DAG, fair stage selection, and monotonic pointers."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping


@dataclass(frozen=True)
class Task:
    task_id: str
    kind: str
    stage: str
    priority: int = 0
    idempotency_key: str | None = None
    payload: Mapping | None = None


class TaskStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY, kind TEXT NOT NULL, stage TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL,
            idempotency_key TEXT UNIQUE);
          CREATE TABLE IF NOT EXISTS task_deps (
            task_id TEXT NOT NULL, depends_on TEXT NOT NULL,
            PRIMARY KEY(task_id, depends_on));
          CREATE TABLE IF NOT EXISTS outbox (
            event_id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS pointers (
            name TEXT NOT NULL, level INTEGER NOT NULL, value INTEGER NOT NULL,
            PRIMARY KEY(name, level));
        """)
        self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()

    def add(self, task: Task, *, depends_on: Iterable[str] = ()) -> str:
        with self.lock:
            if task.idempotency_key:
                row = self.db.execute(
                    "SELECT task_id FROM tasks WHERE idempotency_key=?",
                    (task.idempotency_key,)).fetchone()
                if row:
                    return row[0]
            with self.db:
                self.db.execute(
                    "INSERT INTO tasks(task_id,kind,stage,priority,state,payload,idempotency_key) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (task.task_id, task.kind, task.stage, task.priority, "pending",
                     json.dumps(task.payload or {}, ensure_ascii=False, sort_keys=True),
                     task.idempotency_key))
                for dependency in depends_on:
                    self.db.execute(
                        "INSERT OR IGNORE INTO task_deps(task_id,depends_on) VALUES(?,?)",
                        (task.task_id, dependency))
            return task.task_id

    def ready(self, *, stages: Iterable[str] | None = None) -> list[dict]:
        with self.lock:
            query = (
                "SELECT t.task_id,t.kind,t.stage,t.priority,t.attempts,t.payload "
                "FROM tasks t WHERE t.state='pending' AND NOT EXISTS ("
                " SELECT 1 FROM task_deps d JOIN tasks p ON p.task_id=d.depends_on"
                " WHERE d.task_id=t.task_id AND p.state!='done')")
            parameters = []
            if stages is not None:
                stages = tuple(stages)
                query += " AND t.stage IN (" + ",".join("?" for _ in stages) + ")"
                parameters.extend(stages)
            query += " ORDER BY t.priority DESC,t.task_id"
            rows = self.db.execute(query, parameters).fetchall()
        return [{"task_id": row[0], "kind": row[1], "stage": row[2],
                 "priority": row[3], "attempts": row[4],
                 "payload": json.loads(row[5])} for row in rows]

    def claim(self, task_id: str) -> bool:
        with self.lock:
            with self.db:
                cursor = self.db.execute(
                    "UPDATE tasks SET state='running' WHERE task_id=? AND state='pending'",
                    (task_id,))
            return cursor.rowcount == 1

    def complete(self, task_id: str):
        with self.lock:
            with self.db:
                self.db.execute("UPDATE tasks SET state='done' WHERE task_id=?",
                                (task_id,))

    def fail(self, task_id: str, *, retry: bool = False):
        with self.lock:
            with self.db:
                if retry:
                    self.db.execute(
                        "UPDATE tasks SET state='pending',attempts=attempts+1 WHERE task_id=?",
                        (task_id,))
                else:
                    self.db.execute(
                        "UPDATE tasks SET state='failed',attempts=attempts+1 WHERE task_id=?",
                        (task_id,))

    def block(self, task_id: str):
        with self.lock:
            with self.db:
                self.db.execute("UPDATE tasks SET state='blocked' WHERE task_id=?",
                                (task_id,))

    def recover_running(self, *, retry: bool = True) -> int:
        with self.lock:
            with self.db:
                if retry:
                    cursor = self.db.execute(
                        "UPDATE tasks SET state='pending',attempts=attempts+1 "
                        "WHERE state='running'")
                else:
                    cursor = self.db.execute(
                        "UPDATE tasks SET state='blocked' WHERE state='running'")
            return cursor.rowcount

    def pointer(self, name: str, level: int = 0) -> int:
        with self.lock:
            row = self.db.execute(
                "SELECT value FROM pointers WHERE name=? AND level=?",
                (name, level)).fetchone()
            return row[0] if row else -1

    def advance_pointer(self, name: str, value: int, *, level: int = 0,
                        events: Iterable[dict] = ()) -> tuple[int, int]:
        """Monotonically update a pointer and append idempotent outbox events."""
        if type(value) is not int or value < -1:
            raise ValueError("pointer must be an integer >= -1")
        if type(level) is not int or level < 0:
            raise ValueError("level must be a non-negative integer")
        inserted = 0
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                row = self.db.execute(
                    "SELECT value FROM pointers WHERE name=? AND level=?",
                    (name, level)).fetchone()
                current = row[0] if row else -1
                if value <= current:
                    self.db.rollback()
                    return current, 0
                self.db.execute(
                    "INSERT INTO pointers(name,level,value) VALUES(?,?,?) "
                    "ON CONFLICT(name,level) DO UPDATE SET value=excluded.value",
                    (name, level, value))
                for event in events:
                    encoded = json.dumps(event, ensure_ascii=False, sort_keys=True)
                    event_id = "event:" + hashlib.sha256(
                        encoded.encode("utf-8")).hexdigest()
                    cursor = self.db.execute(
                        "INSERT OR IGNORE INTO outbox(event_id,kind,payload) VALUES(?,?,?)",
                        (event_id, str(event.get("kind", "pointer")), encoded))
                    inserted += cursor.rowcount
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise
        return value, inserted

    def pending_events(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT event_id,kind,payload FROM outbox ORDER BY event_id").fetchall()
        return [{"event_id": row[0], "kind": row[1], "payload": json.loads(row[2])}
                for row in rows]

    def ack_event(self, event_id: str):
        with self.lock:
            with self.db:
                self.db.execute("DELETE FROM outbox WHERE event_id=?", (event_id,))


def fair_select(tasks: Iterable[dict], quotas: Mapping[str, float],
                limit: int) -> list[dict]:
    """Allocate a cycle by stage weights, then by priority within each stage."""
    if limit < 1:
        return []
    tasks = list(tasks)
    if not tasks:
        return []
    by_stage = {}
    for task in tasks:
        by_stage.setdefault(task["stage"], []).append(task)
    for values in by_stage.values():
        values.sort(key=lambda item: (-item.get("priority", 0), item["task_id"]))
    weights = {stage: max(0.0, float(quotas.get(stage, 1.0)))
               for stage in by_stage}
    total_weight = sum(weights.values())
    if total_weight <= 0:
        weights = {stage: 1.0 for stage in by_stage}
        total_weight = float(len(by_stage))
    exact = {stage: limit * weight / total_weight
             for stage, weight in weights.items()}
    counts = {stage: min(len(by_stage[stage]), int(math.floor(value)))
              for stage, value in exact.items()}
    remaining = limit - sum(counts.values())
    while remaining > 0:
        candidates = [stage for stage in by_stage
                      if counts[stage] < len(by_stage[stage])]
        if not candidates:
            break
        candidates.sort(key=lambda stage: (
            -(exact[stage] - counts[stage]), stage))
        counts[candidates[0]] += 1
        remaining -= 1
    selected = []
    for stage, count in counts.items():
        selected.extend(by_stage[stage][:count])
    selected.sort(key=lambda item: (-item.get("priority", 0), item["task_id"]))
    return selected[:limit]


def run_once(store: TaskStore, executors: Mapping[str, Callable[[dict], None]],
             quotas: Mapping[str, float], *, limit: int = 32) -> dict:
    ready = store.ready(stages=tuple(executors))
    selected = fair_select(ready, quotas, limit)
    completed = []
    failed = []
    for task in selected:
        if not store.claim(task["task_id"]):
            continue
        try:
            executor = executors[task["stage"]]
            executor(task)
            store.complete(task["task_id"])
            completed.append(task["task_id"])
        except Exception:
            store.fail(task["task_id"])
            failed.append(task["task_id"])
    return {"completed": completed, "failed": failed}
