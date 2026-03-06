"""JSON output formatter."""
from __future__ import annotations

import json

from ..models import FinalItem, Suggestion, VoteResult


def format_json_report(
    request: str,
    agent_results: list[dict],
    suggestions: list[Suggestion],
    votes: list[VoteResult],
    final: list[FinalItem],
    total_in: int,
    total_out: int,
    total_cached: int,
    elapsed: float,
) -> str:
    """Return structured JSON output."""
    return json.dumps(
        {
            "request": request[:500],
            "agents": [
                {
                    "index": r["agent"],
                    "model": r["model"],
                    "suggestion_count": len(r["suggestions"]),
                    "error": r.get("error"),
                    "tokens_in": r.get("tokens_in", 0),
                    "tokens_out": r.get("tokens_out", 0),
                }
                for r in agent_results
            ],
            "suggestions": [
                {
                    "id": item.suggestion.id,
                    "title": item.suggestion.title,
                    "description": item.suggestion.description,
                    "category": item.suggestion.category,
                    "priority": item.suggestion.priority,
                    "source_agents": item.suggestion.source_agents,
                    "consensus_percent": item.agree_percent,
                    "avg_score": item.avg_score,
                    "agree_count": item.agree_count,
                    "total_voters": item.total_voters,
                    "votes": [
                        {
                            "agent_index": v.agent_index,
                            "agent_model": v.agent_model,
                            "agree": v.agree,
                            "score": v.score,
                            "reasoning": v.reasoning,
                        }
                        for v in item.votes
                    ],
                }
                for item in final
            ],
            "stats": {
                "total_in_tokens": total_in,
                "total_out_tokens": total_out,
                "total_cached_tokens": total_cached,
                "elapsed_seconds": round(elapsed, 1),
            },
        },
        ensure_ascii=False,
        indent=2,
    )
