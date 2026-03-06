"""Custom exception hierarchy for council."""
from __future__ import annotations


class CouncilError(Exception):
    """Base exception for all council errors."""


class ConfigError(CouncilError):
    """Configuration validation error."""


class SecurityError(CouncilError):
    """Security violation (path traversal, rate limit, etc.)."""


class LLMError(CouncilError):
    """LLM API call error."""


class LLMTimeoutError(LLMError):
    """LLM call timed out."""


class ParseError(CouncilError):
    """Failed to parse LLM response."""


class QuorumError(CouncilError):
    """Not enough agents responded successfully."""
