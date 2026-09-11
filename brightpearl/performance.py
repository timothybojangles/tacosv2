"""Shared performance tracking helpers."""
from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Lock
from typing import Callable, Optional


PerformanceCallback = Optional[Callable[[float, float], None]]


@dataclass
class _PerformanceState:
    last_timestamp: Optional[float] = None


_state = _PerformanceState()
_callback: PerformanceCallback = None
_lock = Lock()


def set_performance_callback(callback: PerformanceCallback) -> None:
    """Register a callback to receive (rate_per_sec, timestamp)."""
    global _callback
    _callback = callback


def record_api_update(record_count: int, *, timestamp: Optional[float] = None) -> None:
    """Record an API update count and emit a rate update when possible."""
    if record_count <= 0:
        return
    now = timestamp or time.monotonic()
    with _lock:
        last_timestamp = _state.last_timestamp
        _state.last_timestamp = now

    if last_timestamp is None:
        return
    delta = max(now - last_timestamp, 0.000001)
    rate_per_sec = record_count / delta
    if _callback:
        _callback(rate_per_sec, now)


def estimate_record_count(payload: object, *, fallback: int = 1) -> int:
    """Estimate record count from a payload structure."""
    if payload is None:
        return fallback
    if isinstance(payload, (list, tuple, set)):
        return max(len(payload), fallback)
    if isinstance(payload, dict):
        for key in (
            "results",
            "rows",
            "items",
            "products",
            "corrections",
            "orders",
            "orderRows",
            "contactIds",
        ):
            value = payload.get(key)
            if isinstance(value, (list, tuple)):
                return max(len(value), fallback)
        return fallback
    return fallback
