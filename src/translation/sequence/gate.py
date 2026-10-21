"""Shared call/token gate for translation, review, and materialization."""
import threading
from contextlib import contextmanager


class ModelGate:
    def __init__(self, *, max_calls=60, max_tokens=8_000_000):
        self.max_calls = max(1, int(max_calls))
        self.max_tokens = max(1, int(max_tokens))
        self.calls = 0
        self.tokens = 0
        self.condition = threading.Condition()

    @contextmanager
    def slot(self, tokens):
        tokens = max(1, min(int(tokens), self.max_tokens))
        with self.condition:
            while (self.calls >= self.max_calls or
                   self.tokens + tokens > self.max_tokens):
                self.condition.wait()
            self.calls += 1
            self.tokens += tokens
        try:
            yield
        finally:
            with self.condition:
                self.calls -= 1
                self.tokens -= tokens
                self.condition.notify_all()


class GatedProvider:
    """Provider proxy; the original provider is shared by all callers."""

    def __init__(self, provider, gate):
        self.provider = provider
        self.gate = gate

    def generate(self, request):
        tokens = sum(len(message.get('content', '').encode('utf-8'))
                     for message in request.messages)
        tokens += int(getattr(request, 'max_tokens', 0) or 0)
        with self.gate.slot(tokens):
            return self.provider.generate(request)

    def __getattr__(self, name):
        return getattr(self.provider, name)
