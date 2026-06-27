"""Backoff policy (DB-free, deterministic-testable)."""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class Backoff:
    base_seconds: float = 5.0
    factor: float = 2.0
    max_seconds: float = 3600.0
    jitter: bool = True  # full jitter spreads retry storms

    def compute(self, attempt: int, rng: random.Random | None = None) -> float:
        """Seconds to wait before retry `attempt` (1-based)."""
        attempt = max(1, attempt)
        ceiling = min(self.max_seconds, self.base_seconds * (self.factor ** (attempt - 1)))
        if not self.jitter:
            return ceiling
        r = rng or random
        return r.uniform(0.0, ceiling)


@dataclass(frozen=True)
class RetryOutcome:
    dead_letter: bool
    retry_count: int
    delay_seconds: float | None  # None when dead-lettered


def on_failure(
    retry_count: int,
    max_retries: int,
    backoff: Backoff,
    rng: random.Random | None = None,
) -> RetryOutcome:
    """Pure decision for a failed delivery: retry-with-backoff or dead-letter."""
    rc = retry_count + 1
    if rc >= max_retries:
        return RetryOutcome(dead_letter=True, retry_count=rc, delay_seconds=None)
    return RetryOutcome(dead_letter=False, retry_count=rc, delay_seconds=backoff.compute(rc, rng))
