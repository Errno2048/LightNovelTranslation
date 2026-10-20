"""One rate/concurrency gate shared by metadata, chapter workers and images."""
from collections import Counter
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
import math
from threading import Condition
import time

from .errors import TranslationError

BACKOFF_SECONDS = 5.0
MAX_BACKOFF_SECONDS = 60.0


def retry_after_seconds(value):
    if value is None:
        return None
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except (ValueError, TypeError):
        try:
            # HTTP-date values have one-second precision.  Round upward so
            # scheduling latency cannot make us retry before the advertised
            # timestamp.
            return max(0.0, math.ceil(parsedate_to_datetime(value).timestamp() - time.time()))
        except (ValueError, TypeError, OverflowError):
            return None


class SourceRequests:
    def __init__(self, concurrency, interval, retries):
        self.concurrency = self.effective_concurrency = concurrency
        self.interval, self.retries = interval, retries
        self.condition = Condition()
        self.active = self.maximum = self.requests = self.cache_hits = self.retry_count = 0
        self.next_start = self.cooldown = 0.0
        self.stopped = False
        self.failure = None
        self.statuses = Counter()
        self.throttles = []

    @contextmanager
    def slot(self):
        with self.condition:
            while True:
                if self.stopped:
                    if self.failure:
                        error = self.failure
                        raise TranslationError(error.code, str(error), details=dict(error.details), retryable=error.retryable)
                    raise TranslationError('source_download_cancelled', 'Acquisition stopped after a failed request')
                delay = max(self.next_start, self.cooldown) - time.monotonic()
                if self.active < self.effective_concurrency and delay <= 0:
                    self.active += 1
                    self.requests += 1
                    self.maximum = max(self.maximum, self.active)
                    self.next_start = time.monotonic() + self.interval
                    break
                self.condition.wait(max(0.001, delay) if delay > 0 else None)
        try:
            yield
        finally:
            with self.condition:
                self.active -= 1
                self.condition.notify_all()

    def abort(self, error=None):
        with self.condition:
            if error is not None and self.failure is None:
                self.failure = error
            self.stopped = True
            self.condition.notify_all()

    def cached(self):
        with self.condition:
            self.cache_hits += 1

    def throttle(self, status, header, attempt):
        requested = retry_after_seconds(header)
        seconds = max(BACKOFF_SECONDS, requested if requested is not None else BACKOFF_SECONDS * 2**attempt)
        with self.condition:
            self.effective_concurrency = max(1, self.effective_concurrency // 2)
            self.cooldown = max(self.cooldown, time.monotonic() + seconds)
            self.throttles.append({'status': status, 'wait_seconds': seconds,
                                  'effective_concurrency': self.effective_concurrency})
            self.condition.notify_all()
        # Never retry earlier than Retry-After; return control for long waits.
        return seconds <= MAX_BACKOFF_SECONDS

    def request(self, original, fetcher, method, url, **kwargs):
        for attempt in range(self.retries + 1):
            try:
                with self.slot():
                    response = original(method, url, **kwargs)
                with self.condition:
                    self.statuses[getattr(fetcher, '_lnt_status', 200)] += 1
                return response
            except TranslationError:
                raise
            except Exception as exc:
                status = getattr(exc, 'status_code', None)
                with self.condition:
                    self.statuses[status if status is not None else 'network_error'] += 1
                may_wait = True
                if status in {429, 503}:
                    may_wait = self.throttle(status, getattr(fetcher, '_lnt_retry_after', None), attempt)
                from requests.exceptions import ConnectionError, Timeout
                transient = status in {429, 500, 502, 503, 504, 525} or isinstance(exc, (ConnectionError, Timeout))
                if transient and attempt < self.retries and may_wait:
                    if status not in {429, 503}:
                        with self.condition:
                            self.cooldown = max(self.cooldown, time.monotonic() + BACKOFF_SECONDS * 2**attempt)
                    with self.condition:
                        self.retry_count += 1
                    continue
                code = 'source_rate_limited' if status == 429 else 'source_request_failed'
                error = TranslationError(code, 'Source request failed; cached responses are retained',
                    details={'status': status, 'attempts': attempt + 1,
                             'exception_type': type(exc).__name__,
                             'retry_after_seconds': retry_after_seconds(getattr(fetcher, '_lnt_retry_after', None))},
                    retryable=transient)
                self.abort(error)
                raise error from None

    def summary(self):
        with self.condition:
            return {'configured_concurrency': self.concurrency, 'effective_concurrency': self.effective_concurrency,
                    'max_in_flight': self.maximum, 'request_interval': self.interval,
                    'network_requests': self.requests, 'cache_hits': self.cache_hits,
                    'retries': self.retry_count, 'statuses': {str(k): v for k, v in self.statuses.items()},
                    'throttle_events': list(self.throttles)}
