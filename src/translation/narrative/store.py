"""Append-only content-addressed objects and durable request attempts in SQLite.

Reads use a small pool of query-only connections so immutable object loads do not
serialize behind the single writer. Hashes are verified when objects are written;
``verify`` is the explicit integrity path for reads.
"""
import json
import queue
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path

from ..storage import digest, write_json
from .schemas import NarrativeError

OBJECT_CACHE_LIMIT = 96 * 1024 * 1024
GET_CACHE_LIMIT = 64 * 1024 * 1024
READ_POOL_SIZE = 8
READ_CACHE_SIZE_KB = 4096


class NarrativeStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._cache_lock = threading.RLock()
        self._object_cache = {}
        self._object_cache_bytes = 0
        self._get_cache = OrderedDict()
        self._get_cache_bytes = 0
        self._kind_versions = {}
        self._read_pool = queue.LifoQueue()
        self._read_all = []
        self._read_count = 0
        self._read_guard = threading.Lock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS objects (id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS attempts (seq INTEGER PRIMARY KEY AUTOINCREMENT,
            identity TEXT NOT NULL, kind TEXT NOT NULL, input_reserved INTEGER NOT NULL,
            output_reserved INTEGER NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS idx_objects_kind ON objects(kind, id);
          CREATE INDEX IF NOT EXISTS idx_attempts_identity ON attempts(identity);
          CREATE INDEX IF NOT EXISTS idx_review_request_logical
            ON objects(json_extract(payload,'$.logical_task')) WHERE kind='review-request';
        """)
        self.db.commit()

    def _new_read_connection(self):
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA cache_size=-%d" % READ_CACHE_SIZE_KB)
        return conn

    def _writer_owned(self):
        checker = getattr(self.lock, '_is_owned', None)
        return bool(checker and checker())

    @contextmanager
    def _reader(self):
        # A caller inside a write transaction must see its own uncommitted rows.
        if self._writer_owned():
            yield self.db
            return
        try:
            conn = self._read_pool.get_nowait()
        except queue.Empty:
            conn = None
            with self._read_guard:
                if self._read_count < READ_POOL_SIZE:
                    conn = self._new_read_connection()
                    self._read_all.append(conn)
                    self._read_count += 1
            if conn is None:
                conn = self._read_pool.get()
        try:
            yield conn
        finally:
            self._read_pool.put(conn)

    def close(self):
        with self.lock:
            with self._read_guard:
                for conn in self._read_all:
                    try:
                        conn.close()
                    except sqlite3.Error:
                        pass
                self._read_all.clear()
                self._read_count = 0
            while True:
                try:
                    self._read_pool.get_nowait().close()
                except queue.Empty:
                    break
                except sqlite3.Error:
                    pass
            self.db.close()

    def put(self, kind, data):
        with self.lock:
            ident = kind + ":" + digest(data)
            encoded = json.dumps(data, ensure_ascii=False, sort_keys=True)
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO objects VALUES(?,?,?)", (ident, kind, encoded))
            with self._cache_lock:
                self._kind_versions[kind] = self._kind_versions.get(kind, 0) + 1
                cached = self._object_cache.pop(kind, None)
                if cached is not None:
                    self._object_cache_bytes -= cached[2]
            return ident

    def get(self, ident, kind=None, *, verify=False):
        if not verify:
            with self._cache_lock:
                cached = self._get_cache.get(ident)
                if cached is not None:
                    self._get_cache.move_to_end(ident)
                    return cached[0]
        with self._reader() as conn:
            row = conn.execute("SELECT kind,payload FROM objects WHERE id=?", (ident,)).fetchone()
        if not row or (kind and row[0] != kind):
            raise NarrativeError("coverage_gap", "missing immutable object: " + ident)
        data = json.loads(row[1])
        if verify and ident != row[0] + ":" + digest(data):
            raise NarrativeError("stale_summary", "stored object failed hash validation")
        if not verify:
            size = len(row[1])
            with self._cache_lock:
                if ident not in self._get_cache:
                    self._get_cache[ident] = (data, size)
                    self._get_cache_bytes += size
                    while self._get_cache_bytes > GET_CACHE_LIMIT:
                        _, (_, old_size) = self._get_cache.popitem(last=False)
                        self._get_cache_bytes -= old_size
        return data

    def objects(self, kind, *, verify=False):
        with self._cache_lock:
            version = self._kind_versions.get(kind, 0)
            cached = self._object_cache.get(kind)
            if cached is not None and cached[0] == version and not verify:
                return list(cached[1])
        with self._reader() as conn:
            rows = conn.execute("SELECT id,payload FROM objects WHERE kind=? ORDER BY id",
                                (kind,)).fetchall()
        result = []
        size = 0
        for ident, payload in rows:
            data = json.loads(payload)
            if verify and ident != kind + ":" + digest(data):
                raise NarrativeError("stale_summary", "stored object failed hash validation")
            result.append((ident, data))
            size += len(payload)
        if verify:
            return result
        with self._cache_lock:
            if self._kind_versions.get(kind, 0) == version:
                self._object_cache[kind] = (version, result, size)
                self._object_cache_bytes += size
                while (self._object_cache_bytes > OBJECT_CACHE_LIMIT and
                       len(self._object_cache) > 1):
                    oldest = next(iter(self._object_cache))
                    _, _, old_size = self._object_cache.pop(oldest)
                    self._object_cache_bytes -= old_size
        return result

    def kind_version(self, kind):
        with self._cache_lock:
            return self._kind_versions.get(kind, 0)

    def count(self, kind):
        with self._reader() as conn:
            return conn.execute("SELECT COUNT(*) FROM objects WHERE kind=?", (kind,)).fetchone()[0]

    def count_where(self, kind, field, value):
        with self._reader() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM objects WHERE kind=? AND json_extract(payload,'$." + field + "')=?",
                (kind, value)).fetchone()[0]

    def verify(self):
        """Explicit integrity pass; returns (checked, invalid_ids)."""
        checked = 0
        invalid = []
        for ident, kind in self.db.execute("SELECT id,kind FROM objects ORDER BY id").fetchall():
            try:
                self.get(ident, kind, verify=True)
            except NarrativeError:
                invalid.append(ident)
            checked += 1
        return checked, invalid

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
        with self._reader() as conn:
            rows = conn.execute("SELECT * FROM attempts ORDER BY seq").fetchall()
        return [{"seq": r[0], "identity": r[1], "kind": r[2], "input_reserved": r[3],
                 "output_reserved": r[4], "status": r[5], **json.loads(r[6])}
                for r in rows]

    def export(self, path):
        with self.lock:
            data = {"version": "narrative-export-v1", "objects": [
                {"id": r[0], "kind": r[1], "data": self.get(r[0], verify=True)}
                for r in self.db.execute("SELECT id,kind FROM objects ORDER BY id")]}
            data["fingerprint"] = digest(data)
            write_json(path, data)
            return data["fingerprint"]
