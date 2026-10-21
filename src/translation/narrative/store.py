"""Append-only content-addressed objects and durable request attempts in SQLite."""
import json
import sqlite3
import threading
import time
from pathlib import Path

from ..storage import digest, write_json
from .schemas import NarrativeError

OBJECT_CACHE_LIMIT = 16 * 1024 * 1024


class NarrativeStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._object_cache = {}
        self._object_cache_bytes = 0
        self._kind_versions = {}
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS objects (id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS attempts (seq INTEGER PRIMARY KEY AUTOINCREMENT,
            identity TEXT NOT NULL, kind TEXT NOT NULL, input_reserved INTEGER NOT NULL,
            output_reserved INTEGER NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL);
        """)
        self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()

    def put(self, kind, data):
        with self.lock:
            ident = kind + ":" + digest(data)
            encoded = json.dumps(data, ensure_ascii=False, sort_keys=True)
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO objects VALUES(?,?,?)", (ident, kind, encoded))
            self._kind_versions[kind] = self._kind_versions.get(kind, 0) + 1
            cached = self._object_cache.pop(kind, None)
            if cached is not None:
                self._object_cache_bytes -= cached[2]
            return ident

    def get(self, ident, kind=None):
        with self.lock:
            row = self.db.execute("SELECT kind,payload FROM objects WHERE id=?", (ident,)).fetchone()
            if not row or (kind and row[0] != kind):
                raise NarrativeError("coverage_gap", "missing immutable object: " + ident)
            data = json.loads(row[1])
            if ident != row[0] + ":" + digest(data):
                raise NarrativeError("stale_summary", "stored object failed hash validation")
            return data

    def objects(self, kind):
        with self.lock:
            version = self._kind_versions.get(kind, 0)
            cached = self._object_cache.get(kind)
            if cached is not None and cached[0] == version:
                return list(cached[1])
            rows = self.db.execute("SELECT id,payload FROM objects WHERE kind=? ORDER BY id",
                                   (kind,)).fetchall()
            result = []
            size = 0
            for ident, payload in rows:
                data = json.loads(payload)
                if ident != kind + ":" + digest(data):
                    raise NarrativeError("stale_summary", "stored object failed hash validation")
                result.append((ident, data))
                size += len(payload)
            if size <= OBJECT_CACHE_LIMIT:
                self._object_cache[kind] = (version, result, size)
                self._object_cache_bytes += size
                while (self._object_cache_bytes > OBJECT_CACHE_LIMIT and
                       len(self._object_cache) > 1):
                    oldest = next(iter(self._object_cache))
                    _, _, old_size = self._object_cache.pop(oldest)
                    self._object_cache_bytes -= old_size
            return result

    def kind_version(self, kind):
        with self.lock:
            return self._kind_versions.get(kind, 0)

    def reserve(self, identity, kind, input_tokens, output_tokens, limits, *, retry_failed=False):
        # Reserve before making any HTTP request. Crashes/unknown billing retain the reservation.
        with self.lock:
            return self._reserve_locked(identity, kind, input_tokens, output_tokens, limits,
                                        retry_failed=retry_failed)

    def _reserve_locked(self, identity, kind, input_tokens, output_tokens, limits, *, retry_failed=False):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            old = self.db.execute("SELECT status FROM attempts WHERE identity=? ORDER BY seq DESC LIMIT 1", (identity,)).fetchone()
            if old and (old[0] == "running" or not retry_failed):
                raise NarrativeError("request_already_attempted", "explicit retry of a completed/failed attempt required")
            requests, inputs, outputs = self.db.execute(
                "SELECT COUNT(*),COALESCE(SUM(input_reserved),0),COALESCE(SUM(output_reserved),0) FROM attempts").fetchone()
            kind_count = self.db.execute("SELECT COUNT(*) FROM attempts WHERE kind=?", (kind,)).fetchone()[0]
            kind_limit = limits.get(kind + "_requests", 0)
            request_limit = limits.get("requests", 0)
            if ((kind_limit and kind_count >= kind_limit) or
                    (request_limit and requests >= request_limit)):
                raise NarrativeError("budget_exceeded", "request ledger limit reached")
            input_limit = limits.get("input_tokens", 0)
            output_limit = limits.get("output_tokens", 0)
            if ((input_limit and inputs + input_tokens > input_limit) or
                    (output_limit and outputs + output_tokens > output_limit)):
                raise NarrativeError("budget_exceeded", "cumulative reserved token limit reached")
            cursor = self.db.execute("INSERT INTO attempts(identity,kind,input_reserved,output_reserved,status,payload) VALUES(?,?,?,?,?,?)",
                                     (identity, kind, input_tokens, output_tokens, "running", json.dumps({"started": time.time()})))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return cursor.lastrowid

    def finish(self, seq, status, result):
        with self.lock:
            with self.db:
                self.db.execute("UPDATE attempts SET status=?,payload=? WHERE seq=?",
                                (status, json.dumps(result, ensure_ascii=False, sort_keys=True), seq))

    def attempts(self):
        with self.lock:
            return [{"seq": r[0], "identity": r[1], "kind": r[2], "input_reserved": r[3],
                     "output_reserved": r[4], "status": r[5], **json.loads(r[6])}
                    for r in self.db.execute("SELECT * FROM attempts ORDER BY seq")]

    def export(self, path):
        with self.lock:
            data = {"version": "narrative-export-v1", "objects": [
                {"id": r[0], "kind": r[1], "data": self.get(r[0])}
                for r in self.db.execute("SELECT id,kind FROM objects ORDER BY id")]}
            data["fingerprint"] = digest(data)
            write_json(path, data)
            return data["fingerprint"]
