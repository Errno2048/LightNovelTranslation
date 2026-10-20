from concurrent.futures import ThreadPoolExecutor
from email.utils import formatdate
from threading import Barrier, Lock, Thread
import time

import pytest

from lightnovel_translation import ConfigurationError, SyosetuConfig, TranslationError
from lightnovel_translation._source_requests import SourceRequests, retry_after_seconds


def test_new_source_settings_preserve_existing_config_positional_arguments(tmp_path):
    from lightnovel_translation import TranslationConfig
    original = TranslationConfig()
    tokenizer = tmp_path / 'tokenizer.json'
    loaded = TranslationConfig(original.model, original.capacity, original.roles, original.index,
                               original.sequence, original.limits, original.references, tokenizer)
    assert loaded.tokenizer_path == tokenizer and loaded.syosetu == SyosetuConfig()


@pytest.mark.parametrize('interval', [-1, float('inf'), float('nan'), True, '1'])
def test_bad_intervals_are_rejected(interval):
    with pytest.raises(ConfigurationError):
        SyosetuConfig(request_interval=interval)


def test_request_interval_is_global_not_per_thread():
    requests = SourceRequests(4, .03, 0)
    starts, lock, barrier = [], Lock(), Barrier(4)
    def run(_):
        barrier.wait(timeout=5)
        with requests.slot():
            with lock:
                starts.append(time.monotonic())
            time.sleep(.06)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(run, range(4)))
    assert all(b-a >= .025 for a,b in zip(starts, starts[1:]))
    assert 1 < requests.summary()['max_in_flight'] <= 4


def test_retry_after_date_and_long_wait_fail_without_early_retry():
    assert retry_after_seconds('3') == 3
    assert 29 <= retry_after_seconds(formatdate(time.time()+30, usegmt=True)) <= 30
    assert retry_after_seconds('nonsense') is None
    assert retry_after_seconds('NaN') is None
    assert retry_after_seconds('Infinity') is None
    HTTPErrorFFF = pytest.importorskip('fanficfare.exceptions').HTTPErrorFFF
    requests = SourceRequests(4, 0, 3)
    class Fetcher:
        _lnt_retry_after = '3600'
    calls = []
    def original(method, url):
        calls.append(url)
        raise HTTPErrorFFF(url, 429, 'rate limited')
    with pytest.raises(TranslationError) as caught:
        requests.request(original, Fetcher(), 'GET', 'https://example.invalid/')
    assert len(calls) == 1 and caught.value.details['retry_after_seconds'] == 3600
    assert requests.summary()['effective_concurrency'] == 2


def test_forbidden_response_is_not_retried():
    HTTPErrorFFF = pytest.importorskip('fanficfare.exceptions').HTTPErrorFFF
    requests = SourceRequests(4, 0, 3)
    def original(method, url):
        raise HTTPErrorFFF(url, 403, 'forbidden')
    with pytest.raises(TranslationError) as caught:
        requests.request(original, object(), 'GET', 'https://example.invalid/')
    assert caught.value.details['status'] == 403
    assert not caught.value.retryable and requests.summary()['network_requests'] == 1


def test_real_fetcher_retry_after_hook_and_snapshot_cache(tmp_path, monkeypatch):
    RequestsFetcher = pytest.importorskip('fanficfare.fetchers.fetcher_requests').RequestsFetcher
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from lightnovel_translation.syosetu import _ResponseSnapshot
    import lightnovel_translation._source_requests as limits
    monkeypatch.setattr(limits, 'BACKOFF_SECONDS', .01)
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(time.monotonic())
            self.send_response(429 if len(calls) == 1 else 200)
            self.send_header('Retry-After', '0')
            self.end_headers()
            self.wfile.write(b'chapter')
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    values = {'max_request_retries': '0', 'connect_timeout': '2', 'user_agent': 'test'}
    fetcher = RequestsFetcher(lambda name, default=None: values.get(name, default), lambda *args: [])
    fetcher.get_requests_session().trust_env = False
    requests = SourceRequests(2, 0, 1)
    snapshot = _ResponseSnapshot(tmp_path / 'responses', offline=False, requests=requests)
    snapshot.install(fetcher)
    try:
        url = f'http://127.0.0.1:{server.server_port}/chapter'
        assert fetcher.do_request('GET', url).content == b'chapter'
        assert fetcher.do_request('GET', url).fromcache
        assert len(calls) == 2 and calls[1] - calls[0] >= .01
        stats = requests.summary()
        assert stats['retries'] == 1 and stats['statuses'] == {'429': 1, '200': 1}
        assert stats['effective_concurrency'] == 1 and stats['cache_hits'] == 1
        assert not snapshot.failures
    finally:
        fetcher.get_requests_session().close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
