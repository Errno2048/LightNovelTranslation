"""Read-pool concurrency and explicit integrity verification."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from translation.narrative.schemas import NarrativeError
from translation.narrative.store import NarrativeStore


def test_concurrent_reads_share_a_read_pool(tmp_path):
    path = tmp_path / 'store.sqlite'
    store = NarrativeStore(path)
    try:
        ident = store.put('demo', {'value': 'ok'})
        assert store.get(ident) == {'value': 'ok'}

        def read_many(_):
            for _ in range(50):
                assert store.get(ident) == {'value': 'ok'}
                assert store.objects('demo')[0][0] == ident

        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(read_many, range(16)))
    finally:
        store.close()


def test_verify_detects_tampering_without_slowing_normal_reads(tmp_path):
    path = tmp_path / 'store.sqlite'
    store = NarrativeStore(path)
    try:
        ident = store.put('demo', {'value': 'ok'})
    finally:
        store.close()

    db = sqlite3.connect(path)
    db.execute("UPDATE objects SET payload=? WHERE id=?", (json.dumps({'value': 'bad'}), ident))
    db.commit()
    db.close()

    store = NarrativeStore(path)
    try:
        assert store.get(ident) == {'value': 'bad'}
        with pytest.raises(NarrativeError) as caught:
            store.get(ident, verify=True)
        assert caught.value.kind == 'stale_summary'
        checked, invalid = store.verify()
        assert checked == 1
        assert invalid == [ident]
    finally:
        store.close()
