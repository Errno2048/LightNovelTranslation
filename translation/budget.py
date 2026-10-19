"""Versioned capacity decisions. Cached tokens still occupy the input window."""
from dataclasses import asdict, dataclass
import math
import json

from .narrative.schemas import NarrativeError
from .storage import digest


@dataclass(frozen=True)
class ModelCapacity:
    context_window_tokens: int
    max_output_tokens: int
    max_input_tokens: int | None = None
    token_inflation_factor: float = 1.05
    safety_tokens: int = 2048

    def __post_init__(self):
        for value in (self.context_window_tokens, self.max_output_tokens):
            if type(value) is not int or value <= 0:
                raise ValueError("capacity must contain positive integer limits")
        if self.max_input_tokens is not None and (type(self.max_input_tokens) is not int or self.max_input_tokens <= 0):
            raise ValueError("max_input_tokens must be positive")
        if not math.isfinite(self.token_inflation_factor) or self.token_inflation_factor < 1:
            raise ValueError("inflation factor must be finite and at least one")
        if type(self.safety_tokens) is not int or self.safety_tokens < 0:
            raise ValueError("safety_tokens must be nonnegative")


@dataclass(frozen=True)
class RoleBudget:
    requested_output_tokens: int = 16384
    reasoning_reserve_tokens: int = 0

    def __post_init__(self):
        if type(self.requested_output_tokens) is not int or self.requested_output_tokens <= 0:
            raise ValueError("requested output must be a positive integer")
        if type(self.reasoning_reserve_tokens) is not int or not 0 <= self.reasoning_reserve_tokens < self.requested_output_tokens:
            raise ValueError("reasoning reserve must fit within requested output")


def calibration_profile(provider, counter, role):
    return digest({"provider": provider, "tokenizer": counter.identity,
                   "protocol": "p7-messages-v1", "role": role})


def plan_request(messages, model_capacity, role_budget, counter, *, calibration=None,
                 profile=None, tools=None):
    """Estimate the final messages once, and apply the prior exactly once."""
    capacity, role = model_capacity, role_budget
    estimate = counter.messages(messages)
    # TokenCounter's historical API counts content and a chat allowance. Also
    # account for role/tool/name metadata and explicit tool schemas when present.
    extra = [{k: v for k, v in m.items() if k not in {"role", "content"}} for m in messages]
    if any(extra):
        estimate += counter.count(json.dumps(extra, ensure_ascii=False, sort_keys=True))
    if tools:
        estimate += counter.count(json.dumps(tools, ensure_ascii=False, sort_keys=True))
    factor = max(capacity.token_inflation_factor, (calibration or {}).get("factor", 1))
    safe = math.ceil(estimate * factor)
    input_limit = min(capacity.context_window_tokens - role.requested_output_tokens - capacity.safety_tokens,
                      capacity.max_input_tokens or capacity.context_window_tokens)
    reasons = []
    if role.requested_output_tokens > capacity.max_output_tokens:
        reasons.append("output_limit")
    if safe > input_limit:
        reasons.append("context_limit")
    return {"schema": "request-budget-v1", "capacity": asdict(capacity), "role": asdict(role),
            "counter": counter.identity, "profile": profile, "estimated_input_tokens": estimate,
            "safe_input_tokens": safe, "factor": factor, "input_limit": input_limit,
            "total_reserved_tokens": safe + role.requested_output_tokens + capacity.safety_tokens,
            "remaining_input_tokens": input_limit-safe, "fits": not reasons, "reasons": reasons}


def require_budget(report):
    if not report["fits"]:
        raise NarrativeError("budget_exceeded", "request capacity exceeded: " + ",".join(report["reasons"]))
    return report


def observe_usage(report, usage, previous=None):
    """Return a monotone calibration revision; missing usage is not a sample."""
    actual = usage.get("prompt_tokens", usage.get("input_tokens")) if isinstance(usage, dict) else None
    prior = report["capacity"]["token_inflation_factor"]
    factor = max(prior, report["factor"], (previous or {}).get("factor", prior))
    sample = type(actual) is int and actual > 0 and report["estimated_input_tokens"] > 0
    if sample:
        factor = max(factor, actual / report["estimated_input_tokens"] * 1.02)
    return {"schema": "budget-calibration-v1", "profile": report["profile"], "factor": factor,
            "comparable_usage": sample, "actual_input_tokens": actual if sample else None,
            "estimated_input_tokens": report["estimated_input_tokens"], "usage": usage,
            "previous": digest(previous) if previous else None}


def read_calibration(store, profile):
    rows = [x for _, x in store.objects("budget-calibration") if x["profile"] == profile]
    return max(rows, key=lambda x: x["factor"], default=None)
