"""Markdown report formatter with cost estimation."""
from __future__ import annotations

from ..constants import MODEL_PRICING, PRIORITY_ICONS
from ..models import FinalItem, Suggestion, VoteResult
from ..utils import escape_md_cell


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate cost in USD based on model pricing."""
    pricing = MODEL_PRICING.get(model, MODEL_PRICING["_default"])
    return (
        input_tokens * pricing["input"] + output_tokens * pricing["output"]
    ) / 1_000_000


def format_report(
    request: str,
    agent_results: list[dict],
    suggestions: list[Suggestion],
    votes: list[VoteResult],
    final: list[FinalItem],
    total_in: int,
    total_out: int,
    total_cached: int,
    elapsed: float,
    show_thinking: bool = False,
) -> str:
    lines: list[str] = ["# Council Expert Report\n"]

    # Request
    lines.append("## Request")
    if len(request) > 500:
        lines.append(request[:500] + f"\n... [truncated, {len(request)} chars total]")
    else:
        lines.append(request)
    lines.append("")

    # Agent exploration summary
    lines.append("## Step 1: Agent Analysis")
    for r in agent_results:
        n = len(r["suggestions"])
        status = f"{n} suggestions" if not r.get("error") else f"ERROR: {r['error']}"
        lines.append(f"- **Agent #{r['agent']+1}** (`{r['model']}`): {status}")
        if show_thinking and r.get("thinking"):
            lines.append(
                f"  <details><summary>Thinking</summary>\n\n"
                f"  {r['thinking'][:500]}\n  </details>"
            )
    lines.append("")

    # Deduplicated suggestions
    if suggestions:
        lines.append(
            f"## Step 2: Suggestions ({len(suggestions)} items, deduplicated)"
        )
        for s in suggestions:
            lines.append(f"### #{s.id}: {s.title}")
            lines.append(f"**Category:** {s.category} | **Priority:** {s.priority}")
            if s.source_agents:
                lines.append(f"**Source agents:** {s.source_agents}")
            lines.append(s.description)
            lines.append("")

    # Vote results table
    if final:
        lines.append("## Vote Results — Ranked by Consensus & Priority\n")
        lines.append(
            "| # | Suggestion | Priority | Consensus | Avg Score | Vote Details |"
        )
        lines.append(
            "|---|-----------|----------|-----------|-----------|--------------|"
        )
        for item in final:
            s = item.suggestion
            vote_details = " / ".join(
                f"{'✓' if v.agree else '✗'}{v.score}" for v in item.votes
            )
            icon = PRIORITY_ICONS.get(s.priority, "⚪")
            lines.append(
                f"| {s.id} "
                f"| {escape_md_cell(s.title)} "
                f"| {icon} {s.priority} "
                f"| **{item.agree_percent:.0f}%** ({item.agree_count}/{item.total_voters}) "
                f"| **{item.avg_score}/10** "
                f"| {escape_md_cell(vote_details)} |"
            )
        lines.append("")

        # Detailed breakdown
        lines.append("## Detailed Breakdown\n")
        for item in final:
            s = item.suggestion
            icon = PRIORITY_ICONS.get(s.priority, "⚪")
            lines.append(f"### #{s.id}: {s.title} — {icon} {s.priority}")
            lines.append(
                f"**Consensus:** {item.agree_percent:.0f}% "
                f"({item.agree_count}/{item.total_voters}) | "
                f"**Score:** {item.avg_score}/10\n"
            )
            lines.append(s.description)
            lines.append("\n**Votes:**")
            for v in item.votes:
                icon_v = "✓" if v.agree else "✗"
                lines.append(
                    f"- {icon_v} Agent #{v.agent_index+1} "
                    f"(`{v.agent_model}`): {v.score}/10 — {v.reasoning}"
                )
            lines.append("")

    # Stats footer
    lines.append("---")
    cache_info = f" ({total_cached:,} cached)" if total_cached else ""
    lines.append(
        f"*Tokens: {total_in:,} in{cache_info} / {total_out:,} out | "
        f"Time: {elapsed:.1f}s*"
    )

    return "\n".join(lines)
