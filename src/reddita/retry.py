"""Bounded retry timing for read-only Reddit requests."""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import random


RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


def request_spacing(*, extended: bool) -> float:
    return random.uniform(6.0, 8.0) if extended else random.uniform(1.0, 2.5)


def retry_delay(status: int, retry_after: str | None, attempt: int) -> float | None:
    if status not in RETRYABLE_STATUSES:
        return None
    if retry_after:
        value = retry_after.strip()
        if value.isdigit():
            return float(value)
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            pass
    if status == 429:
        lower = min(60.0, float(10 * (2 ** attempt)))
        return random.uniform(lower, min(60.0, lower * 1.5))
    return random.uniform(0.0, min(8.0, float(2 ** attempt)))
