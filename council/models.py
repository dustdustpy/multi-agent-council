"""Data models for the council pipeline."""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class LLMResponse:
    content: str
    thinking: str
    model: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int = 0  # Anthropic prompt caching


@dataclass
class Suggestion:
    """One improvement suggestion (deduplicated by synthesizer)."""
    id: int
    title: str
    description: str
    category: str       # architecture, performance, security, quality, ux, etc.
    priority: str       # critical, high, medium, low
    source_agents: list[int] = field(default_factory=list)


@dataclass
class VoteResult:
    """One agent's vote on one suggestion."""
    agent_index: int
    agent_model: str
    suggestion_id: int
    score: int          # 1-10
    agree: bool
    reasoning: str


@dataclass
class FinalItem:
    """One item in the final ranked list."""
    suggestion: Suggestion
    avg_score: float
    agree_count: int
    total_voters: int
    agree_percent: float
    votes: list[VoteResult]
