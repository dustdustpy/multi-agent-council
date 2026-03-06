"""Pydantic configuration models with env var resolution and mtime caching."""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, field_validator, model_validator

from .constants import (
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_MAX_CONCURRENT,
    DEFAULT_RATE_LIMIT,
    DEFAULT_TIMEOUT,
)
from .exceptions import ConfigError

log = logging.getLogger("council.config")

# ── Env var resolution ──────────────────────────────────────────────
_ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")

# Known model-name prefixes for format-mismatch warnings
_KNOWN_OPENAI_PREFIXES = ("gpt-", "o1-", "o3-", "o4-")
_KNOWN_GOOGLE_PREFIXES = ("gemini-",)


def resolve_env_vars(value: str) -> str:
    """Resolve ``${ENV_VAR}`` patterns.  Literal values pass through unchanged."""
    def _replace(m: re.Match) -> str:
        var = m.group(1)
        env_val = os.environ.get(var)
        if env_val is None:
            raise ConfigError(
                f"Environment variable '{var}' not set "
                f"(referenced in config as ${{{var}}})"
            )
        return env_val
    return _ENV_PATTERN.sub(_replace, value)


# ── Pydantic models ────────────────────────────────────────────────

class MemberConfig(BaseModel):
    model: str
    format: str  # "anthropic" | "openai"
    base_url: str
    api_key: str
    context_window: int = DEFAULT_CONTEXT_WINDOW
    max_output: int = 4096

    @field_validator("format")
    @classmethod
    def validate_format(cls, v: str) -> str:
        v = v.lower()
        if v not in ("anthropic", "openai"):
            raise ValueError(f"Unsupported API format: {v!r}")
        return v

    def resolved_api_key(self) -> str:
        return resolve_env_vars(self.api_key)

    def resolved_base_url(self) -> str:
        return resolve_env_vars(self.base_url)


class CouncilConfig(BaseModel):
    members: list[MemberConfig]
    synthesizer: MemberConfig

    @field_validator("members")
    @classmethod
    def validate_members(cls, v: list[MemberConfig]) -> list[MemberConfig]:
        if not v:
            raise ValueError("Council must have at least 1 member")
        return v

    @model_validator(mode="after")
    def warn_format_mismatch(self) -> "CouncilConfig":
        for i, m in enumerate(self.members):
            _check_format_hint(m.model, m.format, f"Member #{i+1}")
        _check_format_hint(
            self.synthesizer.model, self.synthesizer.format, "Synthesizer"
        )
        return self


class PromptsConfig(BaseModel):
    system_prompt: str = (
        "You are a Senior Expert Reviewer. You evaluate ALL aspects equally: "
        "Architecture, Code Quality, Security, Performance, UX/DX, Feasibility, "
        "and Maintainability. Be thorough, specific, and actionable."
    )
    explore_template: str = ""  # empty = use built-in
    synthesize_template: str = ""
    vote_template: str = ""


class SettingsConfig(BaseModel):
    timeout_seconds: int = DEFAULT_TIMEOUT
    max_concurrent: int = DEFAULT_MAX_CONCURRENT
    allowed_roots: list[str] = []
    show_thinking: bool = False
    debug: bool = False
    min_quorum: int = 1
    rate_limit_per_minute: int = DEFAULT_RATE_LIMIT
    quick_council_size: int = 3

    @field_validator("timeout_seconds")
    @classmethod
    def validate_timeout(cls, v: int) -> int:
        if v < 10:
            raise ValueError("timeout_seconds must be >= 10")
        return v

    @field_validator("min_quorum")
    @classmethod
    def validate_quorum(cls, v: int) -> int:
        if v < 1:
            raise ValueError("min_quorum must be >= 1")
        return v


class AppConfig(BaseModel):
    council: CouncilConfig
    prompts: PromptsConfig = PromptsConfig()
    settings: SettingsConfig = SettingsConfig()


# ── Config loading with mtime cache ────────────────────────────────

_cached_config: Optional[AppConfig] = None
_cached_mtime: float = 0.0


def _config_path() -> Path:
    env = os.environ.get("COUNCIL_CONFIG_PATH")
    if env:
        return Path(env)
    return Path(__file__).parent.parent / "council_config.json"


def load_config(force_reload: bool = False) -> AppConfig:
    """Load and validate config.  Uses mtime-based cache to avoid re-reading."""
    global _cached_config, _cached_mtime

    path = _config_path()
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")

    mtime = path.stat().st_mtime
    if not force_reload and _cached_config is not None and mtime == _cached_mtime:
        return _cached_config

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    # Provide defaults for optional top-level keys
    data.setdefault("prompts", {})
    data.setdefault("settings", {})

    try:
        config = AppConfig(**data)
    except Exception as e:
        raise ConfigError(f"Config validation failed: {e}") from e

    _cached_config = config
    _cached_mtime = mtime
    log.info(
        "Config loaded: %d members, synthesizer=%s",
        len(config.council.members),
        config.council.synthesizer.model,
    )
    return config


# ── Helpers ─────────────────────────────────────────────────────────

def _check_format_hint(model: str, declared: str, label: str) -> None:
    """Warn if model name suggests a different API format (may be proxied)."""
    ml = model.lower()
    if declared == "anthropic":
        if any(ml.startswith(p) for p in _KNOWN_OPENAI_PREFIXES + _KNOWN_GOOGLE_PREFIXES):
            log.warning(
                "%s: model %r looks non-Anthropic but format is 'anthropic'. "
                "This is OK if using a proxy that translates the protocol.",
                label, model,
            )
