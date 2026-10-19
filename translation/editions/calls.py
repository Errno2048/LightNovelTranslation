"""Durable request capture and bounded retries, including concurrent index builds."""
from dataclasses import asdict
from pathlib import Path
from threading import Lock

from ..budget import RoleBudget, plan_request, require_budget
from ..providers import ProviderError
from ..schemas import GenerationRequest
from ..storage import digest, read_json, write_json
from .prompts import PROTOCOL


class EditionCaller:
    def __init__(self, provider, counter, capacity, folder, *, max_requests=80,
                 max_input_tokens=2000000, max_output_tokens=800000, max_attempts=3):
        if not 1 <= max_attempts <= 3:
            raise ValueError('max_attempts must be 1..3')
        self.provider, self.counter, self.capacity = provider, counter, capacity
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.max_requests, self.max_attempts = max_requests, max_attempts
        self.max_input_tokens, self.max_output_tokens = max_input_tokens, max_output_tokens
        self.new_requests, self.cached_requests = 0, 0
        self.reserved_input, self.reserved_output = 0, 0
        self.lock = Lock()
        self.key_locks = {}
        self.provider_identity = provider.config.public() if hasattr(provider, 'config') else {'model': 'mock'}
        # Budgets apply to the durable experiment, including failed and pending
        # sends, not just the latest CLI invocation.
        previous = {}
        for path in self.folder.glob('*.json'):
            saved = read_json(path)
            if 'identity' in saved:
                previous[digest(saved['identity'])] = saved['identity']
        self.previous_requests = len(previous)
        self.reserved_input = sum(v['budget']['safe_input_tokens'] for v in previous.values())
        self.reserved_output = sum(v['request']['max_tokens'] for v in previous.values())

    def call(self, task, messages, output_tokens):
        budget = require_budget(plan_request(messages, self.capacity, RoleBudget(output_tokens), self.counter))
        request = GenerationRequest(messages=messages, max_tokens=output_tokens, temperature=0, task=task)
        identity = {'protocol': PROTOCOL, 'task': task, 'request': asdict(request),
                    'provider': self.provider_identity, 'budget': budget}
        key = digest(identity)
        with self.lock:
            key_lock = self.key_locks.setdefault(key, Lock())
        with key_lock:
            path = self.folder / (key + '.json')
            if path.exists():
                saved = read_json(path)
                if saved['identity'] != identity or saved['fingerprint'] != digest(saved['generation']):
                    raise ValueError('cached generation integrity mismatch')
                with self.lock:
                    self.cached_requests += 1
                return {'id': key, **saved, 'cache_hit': True}
            failed = self.folder / (key + '.failure.json')
            if failed.exists():
                failure = read_json(failed)
                raise ProviderError(failure['kind'], 'persisted failed request; retry has a distinct task attempt',
                                    details=failure.get('details', {}))
            # Persist reservations before sending. A crash must not silently resend.
            pending = self.folder / (key + '.pending.json')
            if pending.exists():
                raise ProviderError('pending_request', 'remote completion unknown; inspect before resending')
            with self.lock:
                if self.previous_requests + self.new_requests >= self.max_requests or self.reserved_input + budget['safe_input_tokens'] > self.max_input_tokens or self.reserved_output + output_tokens > self.max_output_tokens:
                    raise ProviderError('budget', 'independent edition run request/token limit reached')
                self.new_requests += 1
                self.reserved_input += budget['safe_input_tokens']
                self.reserved_output += output_tokens
                write_json(pending, {'identity': identity})
            try:
                generation = asdict(self.provider.generate(request))
            except ProviderError as exc:
                write_json(failed, {'identity': identity, 'kind': exc.kind, 'details': exc.details})
                pending.unlink(missing_ok=True)
                raise
            saved = {'identity': identity, 'generation': generation, 'fingerprint': digest(generation)}
            write_json(path, saved)
            pending.unlink(missing_ok=True)
            return {'id': key, **saved, 'cache_hit': False}

    def structured(self, task, messages, output_tokens, validator):
        conversation = list(messages)
        attempts = []
        # Parser upgrades can recover an already-paid response even when its
        # repair prompt listed the former parser's errors. Reuse only a saved
        # dialogue with the exact original task/input/provider prefix.
        saved_dialogues = []
        for path in self.folder.glob('*.json'):
            if path.name.endswith(('.pending.json', '.failure.json')):
                continue
            saved = read_json(path)
            identity = saved['identity']
            if (identity['task'].startswith(task + ':attempt:') and identity['provider'] == self.provider_identity and
                identity['request']['max_tokens'] == output_tokens and
                identity['request']['messages'][:len(messages)] == messages):
                if digest(identity) != path.stem or digest(saved['generation']) != saved['fingerprint']:
                    raise ValueError('saved dialogue integrity mismatch')
                saved_dialogues.append((identity['task'], path.stem, saved))
        for _, ident, saved in sorted(saved_dialogues):
            parsed = validator(saved['generation']['text'])
            attempts.append({'generation_id': ident, 'errors': parsed.get('errors', []),
                             'cache_hit': True, 'replayed_saved_dialogue': True})
            if parsed['complete']:
                with self.lock:
                    self.cached_requests += len(attempts)
                return {'status': 'complete', 'result': parsed, 'generation_id': ident, 'attempts': attempts}
        for attempt in range(self.max_attempts):
            try:
                raw = self.call(task + ':attempt:' + str(attempt+1), conversation, output_tokens)
            except ProviderError as exc:
                attempts.append({'error': exc.kind, 'details': exc.details})
                if exc.kind not in {'timeout', 'network', 'server', 'rate_limit', 'truncated', 'empty', 'invalid_json'}:
                    raise
                if attempt + 1 == self.max_attempts:
                    break
                continue
            parsed = validator(raw['generation']['text'])
            attempts.append({'generation_id': raw['id'], 'errors': parsed.get('errors', []),
                             'cache_hit': raw['cache_hit']})
            if parsed['complete']:
                return {'status': 'complete', 'result': parsed, 'generation_id': raw['id'], 'attempts': attempts}
            conversation += [{'role': 'assistant', 'content': raw['generation']['text']},
                {'role': 'user', 'content': '上次输出存在以下可检查的格式/引用ID问题，请根据同一资料重新输出完整结果，不编造缺失语义：\n' + str(parsed['errors'])}]
        return {'status': 'failed', 'attempts': attempts, 'result': parsed if 'parsed' in locals() else None}
