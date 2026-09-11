"""Shared throttling helpers for Brightpearl sync operations."""
from __future__ import annotations

import random
import time
from typing import Optional, Tuple

from .common import LogCallback, log
from .settings import get_settings


def parse_int_header(headers: dict, key: str, default: int = 0) -> int:
    try:
        return int(headers.get(key, default))
    except (TypeError, ValueError):
        return default


def throttle_decision(
    requests_remaining: int,
    throttle_ms: int,
    *,
    threshold: Optional[int] = None,
) -> Tuple[int, str]:
    """Return sleep_ms + reason for Brightpearl throttling."""
    if threshold is None:
        threshold = get_settings().throttle_threshold
    if requests_remaining < threshold and throttle_ms > 0:
        return throttle_ms, "low request budget (header throttle)"
    return 0, "no throttle sleep needed"


def sleep_with_log(
    ms: int,
    log_callback: LogCallback = None,
    *,
    cancel_token=None,
    reason: Optional[str] = None,
    log_file: Optional[str] = None,
) -> None:
    """Sleep with jitter and logging, respecting cancellation."""
    jitter = int(ms * (0.85 + 0.30 * random.random())) if ms > 0 else 0
    used = jitter if ms > 250 else ms
    message = f"⏳ Sleeping for {used} ms"
    if reason:
        message += f" ({reason})"
    log(message + "...", log_callback=log_callback, log_file=log_file)

    step = 200
    waited = 0
    while waited < used:
        if cancel_token and cancel_token.is_set():
            log("⏹️ Sleep interrupted by cancel.", log_callback=log_callback, log_file=log_file)
            return
        time.sleep(min(step, used - waited) / 1000)
        waited += step
