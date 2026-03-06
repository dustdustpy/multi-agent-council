"""MCP tool definitions — council, council_test, council_health, council_history."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP

from .config import AppConfig, MemberConfig, load_config
from .constants import DEFAULT_RATE_LIMIT, MAX_FILE_PATHS, MAX_REQUEST_LENGTH
from .engine import CouncilEngine
from .exceptions import CouncilError, QuorumError
from .file_reader import read_paths_async
from .formatters.json_fmt import format_json_report
from .formatters.markdown import format_report
from .history import HistoryStore
from .llm.factory import LLMClientFactory
from .logging_config import new_correlation_id, setup_logging
from .project_indexer import ProjectIndex, build_context_for_tier, index_project
from .security import RateLimiter, sanitize_error, set_denylist_keys

log = logging.getLogger("council.tools")

# ── Module-level shared state ────────────────────────────────────
setup_logging()

mcp_server = FastMCP(name="Council Review")
_client_factory = LLMClientFactory()
_rate_limiter = RateLimiter(max_calls=DEFAULT_RATE_LIMIT)

# Project index cache (#6): keyed by (root_path, max_mtime)
_index_cache: dict[str, tuple[float, ProjectIndex]] = {}


def _get_cached_index(root: Path) -> ProjectIndex | None:
    """Return cached index if no files changed (mtime-based)."""
    key = str(root.resolve())
    if key not in _index_cache:
        return None
    cached_mtime, cached_index = _index_cache[key]
    # Check max mtime of project files
    try:
        current_mtime = max(
            f.stat().st_mtime for f in root.rglob("*")
            if f.is_file() and not any(p in str(f) for p in (".git", "node_modules", "__pycache__"))
        )
    except (ValueError, OSError):
        return None
    if current_mtime <= cached_mtime:
        log.info("Using cached project index for %s", root.name)
        return cached_index
    return None


def _cache_index(root: Path, idx: ProjectIndex) -> None:
    """Cache project index with current max mtime."""
    key = str(root.resolve())
    try:
        current_mtime = max(
            f.stat().st_mtime for f in root.rglob("*")
            if f.is_file() and not any(p in str(f) for p in (".git", "node_modules", "__pycache__"))
        )
    except (ValueError, OSError):
        current_mtime = time.time()
    _index_cache[key] = (current_mtime, idx)


def _init_denylist(config: AppConfig) -> None:
    """Populate secret denylist from config API keys."""
    keys: list[str] = []
    for m in config.council.members:
        try:
            keys.append(m.resolved_api_key())
        except Exception:
            pass
    try:
        keys.append(config.council.synthesizer.resolved_api_key())
    except Exception:
        pass
    set_denylist_keys(keys)


def _save_report(report: str, fmt: str, num_suggestions: int) -> str | None:
    """Save report to council_reports/ in current working directory. Returns path or None."""
    try:
        cwd = Path(os.getcwd())
        reports_dir = cwd / "council_reports"
        reports_dir.mkdir(exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        ext = "json" if fmt == "json" else "md"
        filename = f"council_{ts}_{num_suggestions}suggestions.{ext}"
        filepath = reports_dir / filename
        filepath.write_text(report, encoding="utf-8")
        log.info("Report saved: %s", filepath)
        return str(filepath)
    except Exception as e:
        log.warning("Failed to save report file: %s", e)
        return None


# ── council ──────────────────────────────────────────────────────

@mcp_server.tool()
async def council(
    request: str,
    file_paths: str = "",
    output_format: str = "markdown",
    mode: str = "full",
    ctx: Context = None,
) -> str:
    """Multi-agent expert council: N agents analyze -> synthesize -> vote -> ranked improvements.

    Args:
        request: Review question or task. E.g., "Review this code and suggest improvements"
        file_paths: File or folder paths, comma-separated. E.g., "src/,config.json"
        output_format: "markdown" (default) or "json"
        mode: "full" (all agents) or "quick" (first N agents, faster). Default "full"
    """
    cid = new_correlation_id()
    log.info("Council request started [%s]", cid)

    # Rate limit
    if not _rate_limiter.check():
        return "## Error\n\nRate limit exceeded. Please wait before retrying."

    # Input validation
    if not request or not request.strip():
        return "## Error\n\nEmpty request. Please provide a review question."
    if len(request) > MAX_REQUEST_LENGTH:
        return (
            f"## Error\n\nRequest too long ({len(request)} chars, "
            f"max {MAX_REQUEST_LENGTH})."
        )
    if file_paths:
        paths = [p.strip() for p in file_paths.split(",") if p.strip()]
        if len(paths) > MAX_FILE_PATHS:
            return (
                f"## Error\n\nToo many file paths ({len(paths)}, max {MAX_FILE_PATHS})."
            )
    else:
        paths = []

    # Validate mode
    mode = mode.lower().strip()
    if mode not in ("full", "quick"):
        return "## Error\n\nInvalid mode. Use 'full' or 'quick'."

    try:
        config = load_config()
        _init_denylist(config)

        # Quick mode: slice members to first N agents
        if mode == "quick":
            quick_size = config.settings.quick_council_size
            total = len(config.council.members)
            if quick_size < total:
                original_members = config.council.members
                config.council.members = original_members[:quick_size]
                if ctx:
                    models = ", ".join(m.model.split("/")[-1] for m in config.council.members)
                    await ctx.info(f"Quick mode: using {quick_size}/{total} agents [{models}]")

        engine = CouncilEngine(config, client_factory=_client_factory)
        start = time.monotonic()

        # Smart context loading with project indexer
        files_content = ""
        project_index: ProjectIndex | None = None

        if paths:
            allowed_roots = config.settings.allowed_roots or None

            # Check if any path is a folder -> use project indexer
            folder_paths = [p for p in paths if Path(p).expanduser().resolve().is_dir()]
            file_only_paths = [p for p in paths if not Path(p).expanduser().resolve().is_dir()]

            if folder_paths:
                # Use project indexer for the first folder (primary project)
                primary_folder = Path(folder_paths[0]).expanduser().resolve()

                if ctx:
                    await ctx.info(f"Indexing project: {primary_folder.name}...")

                # Try cache first (#6)
                project_index = _get_cached_index(primary_folder)
                if project_index is None:
                    project_index = await asyncio.to_thread(
                        index_project, primary_folder
                    )
                    _cache_index(primary_folder, project_index)

                if ctx:
                    await ctx.info(
                        f"Project indexed: {project_index.total_files} files, "
                        f"tier={project_index.tier}, "
                        f"languages={', '.join(f'{k}({v})' for k, v in sorted(project_index.languages.items(), key=lambda x: -x[1])[:5])}"
                    )

                # Build context based on tier
                files_content = await asyncio.to_thread(
                    build_context_for_tier, project_index
                )

                # Also read any additional individual files
                if file_only_paths or len(folder_paths) > 1:
                    extra_paths = file_only_paths + folder_paths[1:]
                    extra_content = await read_paths_async(extra_paths, allowed_roots)
                    if extra_content:
                        files_content += "\n\n## Additional Files\n" + extra_content
            else:
                # Individual files only — use traditional reader
                files_content = await read_paths_async(paths, allowed_roots)

        # Progress callback
        async def progress_cb(msg: str) -> None:
            if ctx:
                await ctx.info(msg)

        # Run pipeline (with project index for tool-based exploration)
        agent_results, suggestions, votes, final = await engine.run(
            request, files_content, progress_cb, project_index
        )

        elapsed = time.monotonic() - start

        # Format output
        if output_format == "json":
            report = format_json_report(
                request, agent_results, suggestions, votes, final,
                engine.total_in, engine.total_out, engine.total_cached, elapsed,
            )
        else:
            report = format_report(
                request, agent_results, suggestions, votes, final,
                engine.total_in, engine.total_out, engine.total_cached, elapsed,
                show_thinking=config.settings.show_thinking or config.settings.debug,
            )

        # Save to history
        try:
            store = HistoryStore()
            session_id = store.save(
                request=request,
                file_paths=file_paths,
                num_suggestions=len(suggestions),
                num_agents=len(config.council.members),
                total_in=engine.total_in,
                total_out=engine.total_out,
                elapsed=elapsed,
                report=report,
            )
            log.info("Session saved: #%d", session_id)
        except Exception as e:
            log.warning("Failed to save history: %s", e)

        # Save report to file in CWD/council_reports/
        report_path = _save_report(report, output_format, len(suggestions))
        if report_path:
            report += f"\n\n---\n*Report saved to: `{report_path}`*\n"

        return report

    except QuorumError as e:
        return f"## Quorum Error\n\n{e}"
    except CouncilError as e:
        log.exception("Council error [%s]", cid)
        return f"## Error\n\n{sanitize_error(e)}"
    except Exception as e:
        log.exception("Council failed [%s]", cid)
        return f"## Error\n\n{type(e).__name__}: {sanitize_error(e)}"


# ── council_test ─────────────────────────────────────────────────

@mcp_server.tool()
async def council_test(ctx: Context = None) -> str:
    """Test API connectivity and thinking support for all agents and synthesizer."""
    config = load_config()
    _init_denylist(config)
    members = config.council.members
    synth = config.council.synthesizer
    all_endpoints: list[tuple[str, MemberConfig]] = [
        *(( f"Agent #{i+1}", m) for i, m in enumerate(members)),
        ("Synthesizer", synth),
    ]

    factory = _client_factory
    semaphore = asyncio.Semaphore(config.settings.max_concurrent)

    async def test_one(label: str, member: MemberConfig) -> dict:
        start = time.monotonic()
        try:
            client = factory.get_client(member)
            thinking_budget = 1024 if member.format == "anthropic" else 0
            async with semaphore:
                resp = await asyncio.wait_for(
                    client.generate(
                        model=member.model,
                        system="You are a test assistant.",
                        messages=[{"role": "user", "content": "Respond with exactly: OK"}],
                        max_tokens=2048,
                        thinking_budget=thinking_budget,
                    ),
                    timeout=config.settings.timeout_seconds,
                )
            elapsed = time.monotonic() - start
            return {
                "label": label,
                "model": member.model,
                "status": "OK",
                "response": resp.content[:100],
                "thinking": "YES" if resp.thinking else "NO",
                "latency": f"{elapsed:.1f}s",
            }
        except Exception as e:
            return {
                "label": label,
                "model": member.model,
                "status": "ERROR",
                "response": sanitize_error(e)[:200],
                "thinking": "N/A",
                "latency": f"{time.monotonic() - start:.1f}s",
            }

    if ctx:
        await ctx.info(f"Testing {len(all_endpoints)} endpoints...")

    tasks = [test_one(label, member) for label, member in all_endpoints]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    # Filter exceptions
    results = [r for r in results if not isinstance(r, BaseException)]

    lines = ["# Council Connection Test\n"]
    lines.append("| # | Model | Status | Thinking | Latency | Response |")
    lines.append("|---|-------|--------|----------|---------|----------|")

    ok_count = 0
    for r in results:
        if r["status"] == "OK":
            ok_count += 1
        resp_short = r["response"][:60].replace("|", "/").replace("\n", " ")
        lines.append(
            f"| {r['label']} | `{r['model']}` | **{r['status']}** "
            f"| {r['thinking']} | {r['latency']} | {resp_short} |"
        )

    lines.append(f"\n**Result: {ok_count}/{len(results)} OK**")
    if ok_count == len(results):
        lines.append("\nAll agents ready.")
    else:
        lines.append(f"\n**{len(results) - ok_count} agent(s) failed.**")

    return "\n".join(lines)


# ── council_health ───────────────────────────────────────────────

@mcp_server.tool()
async def council_health(ctx: Context = None) -> str:
    """Lightweight health check -- validates config without making LLM calls."""
    try:
        config = load_config()
        return json.dumps(
            {
                "status": "ok",
                "version": "2.3.0",
                "members": [
                    {"model": m.model, "format": m.format}
                    for m in config.council.members
                ],
                "synthesizer": config.council.synthesizer.model,
                "features": [
                    "project_indexer",
                    "tiered_context",
                    "tool_exploration",
                    "adaptive_context",
                    "prompt_caching",
                    "circuit_breaker",
                    "streaming_progress",
                    "stage_reporting",
                    "early_termination",
                    "code_structure_extraction",
                    "smart_truncation",
                ],
                "settings": {
                    "timeout": config.settings.timeout_seconds,
                    "max_concurrent": config.settings.max_concurrent,
                    "min_quorum": config.settings.min_quorum,
                    "rate_limit": config.settings.rate_limit_per_minute,
                },
            },
            indent=2,
            ensure_ascii=False,
        )
    except Exception as e:
        return json.dumps(
            {"status": "error", "detail": sanitize_error(e)},
            indent=2,
        )


# ── council_history ──────────────────────────────────────────────

@mcp_server.tool()
async def council_history(limit: int = 10, ctx: Context = None) -> str:
    """View recent council session history.

    Args:
        limit: Number of recent sessions to show (default 10)
    """
    try:
        store = HistoryStore()
        sessions = store.list_recent(limit)
        if not sessions:
            return "No council sessions recorded yet."

        lines = ["# Council Session History\n"]
        lines.append("| ID | Time | Request | Suggestions | Tokens | Duration |")
        lines.append("|----|------|---------|-------------|--------|----------|")
        for s in sessions:
            req = s["request"][:50].replace("|", "/").replace("\n", " ")
            lines.append(
                f"| {s['id']} "
                f"| {s['timestamp'][:16]} "
                f"| {req} "
                f"| {s['num_suggestions']} "
                f"| {s['total_in_tokens']:,}+{s['total_out_tokens']:,} "
                f"| {s['elapsed_seconds']:.0f}s |"
            )
        lines.append(f"\n*Showing {len(sessions)} most recent sessions*")
        return "\n".join(lines)
    except Exception as e:
        return f"## Error\n\n{sanitize_error(e)}"
