"""Per-endpoint circuit breaker to avoid repeatedly calling failed endpoints."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..constants import CIRCUIT_FAILURE_THRESHOLD, CIRCUIT_RECOVERY_TIMEOUT


@dataclass
class _CircuitState:
    failures: int = 0
    last_failure: float = 0.0
    is_open: bool = False


class CircuitBreaker:
    """Simple circuit breaker: CLOSED -> OPEN -> HALF_OPEN -> CLOSED."""

    def __init__(
        self,
        failure_threshold: int = CIRCUIT_FAILURE_THRESHOLD,
        recovery_timeout: float = CIRCUIT_RECOVERY_TIMEOUT,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._states: dict[str, _CircuitState] = {}

    def _get(self, key: str) -> _CircuitState:
        if key not in self._states:
            self._states[key] = _CircuitState()
        return self._states[key]

    def can_call(self, endpoint: str) -> bool:
        state = self._get(endpoint)
        if not state.is_open:
            return True
        # Half-open: allow one try after recovery timeout
        if time.monotonic() - state.last_failure > self.recovery_timeout:
            state.is_open = False
            return True
        return False

    def record_success(self, endpoint: str) -> None:
        state = self._get(endpoint)
        state.failures = 0
        state.is_open = False

    def record_failure(self, endpoint: str) -> None:
        state = self._get(endpoint)
        state.failures += 1
        state.last_failure = time.monotonic()
        if state.failures >= self.failure_threshold:
            state.is_open = True
