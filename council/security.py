"""Security: path validation, secret redaction, error sanitization, rate limiting."""
from __future__ import annotations

import fnmatch
import logging
import re
import time
from collections import deque
from pathlib import Path

from .constants import SENSITIVE_FILE_PATTERNS
from .exceptions import SecurityError

log = logging.getLogger("council.security")

# ── Secret redaction ────────────────────────────────────────────────

_SECRET_REGEXES = [
    re.compile(r"sk-[a-zA-Z0-9\-]{20,}"),                     # OpenAI-style
    re.compile(r"AKIA[0-9A-Z]{16}"),                           # AWS access key
    re.compile(r"ghp_[a-zA-Z0-9]{36}"),                        # GitHub PAT
    re.compile(r"gho_[a-zA-Z0-9]{36}"),                        # GitHub OAuth
    re.compile(r"xox[bprs]-[a-zA-Z0-9\-]{10,}"),              # Slack token
    re.compile(r"(?i)(?:password|secret|token|api_key|apikey)\s*[:=]\s*['\"]?([^\s'\"]{8,})"),
]

_denylist_keys: list[str] = []


def set_denylist_keys(keys: list[str]) -> None:
    """Populate denylist with actual API keys from config (for exact-match redaction)."""
    global _denylist_keys
    _denylist_keys = [k for k in keys if k and len(k) > 5]


def redact_secrets(text: str) -> str:
    """Remove sensitive data from text before sending to LLM."""
    for key in _denylist_keys:
        if key in text:
            text = text.replace(key, "[REDACTED]")
    for pattern in _SECRET_REGEXES:
        text = pattern.sub("[REDACTED]", text)
    return text


def sanitize_error(error: Exception) -> str:
    """Remove sensitive data from error messages before exposing to user."""
    msg = str(error)
    msg = re.sub(r"sk-[a-zA-Z0-9\-]{10,}", "[KEY_REDACTED]", msg)
    msg = re.sub(r"(api_key=)[^\s&]+", r"\1[REDACTED]", msg)
    msg = re.sub(r"(https?://)[^@\s]+@", r"\1[REDACTED]@", msg)
    for key in _denylist_keys:
        if key in msg:
            msg = msg.replace(key, "[REDACTED]")
    return msg


# ── Path validation ─────────────────────────────────────────────────

def is_sensitive_file(path: Path) -> bool:
    """Check if a file matches sensitive patterns (secrets, keys, etc.)."""
    name = path.name.lower()
    # .env.example is NOT sensitive (contains no real secrets)
    if name in (".env.example", ".env.sample", ".env.template"):
        return False
    for pattern in SENSITIVE_FILE_PATTERNS:
        if fnmatch.fnmatch(name, pattern):
            return True
    return False


def validate_path(
    fp: Path,
    allowed_roots: list[Path] | None = None,
) -> tuple[bool, str]:
    """Validate a resolved file path against security constraints.

    Returns (is_valid, reason_if_denied).
    """
    # Symlink escape check
    try:
        real = fp.resolve(strict=True) if fp.exists() else fp.resolve()
    except OSError as e:
        return False, f"Cannot resolve path: {e}"

    # Allowed roots check
    if allowed_roots:
        if not any(real == root or _is_under(real, root) for root in allowed_roots):
            return False, "Outside allowed roots"

    # Sensitive path block
    sensitive_dirs = {".ssh", ".gnupg", ".aws", ".config"}
    if any(part.lower() in sensitive_dirs for part in real.parts):
        return False, "Sensitive directory"

    return True, ""


def _is_under(path: Path, root: Path) -> bool:
    """Check if path is under root (works on Python 3.9+)."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# ── Rate limiter ────────────────────────────────────────────────────

class RateLimiter:
    """Simple token-bucket rate limiter based on timestamps."""

    def __init__(self, max_calls: int, window_seconds: float = 60.0):
        self.max_calls = max_calls
        self.window = window_seconds
        self._timestamps: deque[float] = deque()

    def check(self) -> bool:
        """Return True if call is allowed, False if rate-limited."""
        now = time.monotonic()
        while self._timestamps and now - self._timestamps[0] > self.window:
            self._timestamps.popleft()
        if len(self._timestamps) >= self.max_calls:
            return False
        self._timestamps.append(now)
        return True
