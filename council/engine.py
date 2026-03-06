"""Council Engine — pipeline orchestrator with stage-based progress, streaming, and smart context."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Callable, Awaitable, Optional

from pydantic import BaseModel, ValidationError

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import AppConfig, MemberConfig
from .constants import (
    DEFAULT_RETRY_ATTEMPTS,
    DEFAULT_RETRY_MAX_WAIT,
    DEFAULT_RETRY_MIN_WAIT,
    DEFAULT_THINKING_RESERVE,
    EARLY_FINISH_RATIO,
    MAX_TOOL_CALLS_PER_ROUND,
    MAX_TOOL_ROUNDS,
    PER_ATTEMPT_TIMEOUT,
    PRIORITY_RANK,
    STRUCTURE_EXTRACT_THRESHOLD,
)
from .exceptions import LLMError, LLMTimeoutError, QuorumError
from .file_reader import (
    extract_code_structure,
    list_directory,
    read_file_raw,
    search_in_project,
)
from .llm.factory import LLMClientFactory
from .models import FinalItem, LLMResponse, Suggestion, VoteResult
from .project_indexer import ProjectIndex, detect_language
from .security import redact_secrets, sanitize_error
from .utils import escape_md_cell, parse_bool, parse_json_response

log = logging.getLogger("council.engine")

ProgressCallback = Optional[Callable[[str], Awaitable[None]]]

# ── Few-shot examples for explore prompt ─────────────────────────

_FEW_SHOT_EXAMPLES = """
### Examples of GOOD suggestions (be this specific):
```json
[
  {
    "title": "Fix race condition in session token refresh",
    "description": "In auth/token_manager.py:142, `refresh_token()` reads and writes `self._token` without a lock. Under concurrent requests, two coroutines can both see an expired token, trigger two refresh calls, and one overwrites the other's valid token. Fix: wrap the read-check-write in an `asyncio.Lock`. Affects: all authenticated API calls under concurrency.",
    "category": "bug",
    "priority": "critical"
  },
  {
    "title": "Replace O(N*M) suggestion dedup with hash-based approach",
    "description": "In engine.py:315, `_fallback_synthesize()` does title-based dedup using a set, but the AI synthesizer prompt at line 260 doesn't instruct component-aware merging, causing 'Add error handling in auth' and 'Add error handling in payments' to be incorrectly merged. Fix: add instruction 'Only merge when addressing the exact same change in the same file/location'.",
    "category": "performance",
    "priority": "high"
  }
]
```
### Examples of WEAK suggestions (avoid this):
- "Consider improving error handling" (too vague, no location)
- "Code could be better organized" (no specific change proposed)
- "Add more tests" (what tests? for which functions?)
"""

# ── Tool call validation ─────────────────────────────────────────


class ToolCallModel(BaseModel):
    tool: str
    path: str = ""
    query: str = ""
    glob: str = "*"


def _parse_tool_calls(raw: object) -> list[ToolCallModel]:
    """Robustly parse tool_calls from LLM output with fallback."""
    if not isinstance(raw, list):
        return []
    results = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            results.append(ToolCallModel(**item))
        except ValidationError:
            tool = item.get("tool") or item.get("name") or item.get("type", "")
            if tool in ("read_file", "search", "list_dir"):
                results.append(ToolCallModel(
                    tool=tool,
                    path=str(item.get("path", item.get("arguments", {}).get("path", ""))),
                    query=str(item.get("query", item.get("arguments", {}).get("query", ""))),
                    glob=str(item.get("glob", item.get("arguments", {}).get("glob", "*"))),
                ))
    return results


# ── Helper: short model name ─────────────────────────────────────

def _short_model(model: str, max_len: int = 25) -> str:
    """Shorten model name for progress display."""
    name = model.split("/")[-1]
    if len(name) > max_len:
        name = name[:max_len - 1] + "~"
    return name


class CouncilEngine:
    def __init__(
        self,
        config: AppConfig,
        client_factory: LLMClientFactory | None = None,
    ):
        self.config = config
        self.factory = client_factory or LLMClientFactory()
        self.semaphore = asyncio.Semaphore(config.settings.max_concurrent)
        self.total_in = 0
        self.total_out = 0
        self.total_cached = 0
        self._cancelled = asyncio.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    def _check_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise LLMError("Pipeline cancelled")

    # ── Token estimation ─────────────────────────────────────────

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        return int(len(text) / 3.5)

    @staticmethod
    def _is_native_anthropic(member: MemberConfig) -> bool:
        """Check if model is truly Anthropic (not proxied GPT/Gemini/etc)."""
        ml = member.model.lower()
        return member.format == "anthropic" and (
            "claude" in ml or "anthropic" in ml
        )

    def _thinking_budget(self, member: MemberConfig) -> int:
        if not self._is_native_anthropic(member):
            return 0
        return max(0, member.max_output - DEFAULT_THINKING_RESERVE)

    def _adaptive_max_chars(self, member: MemberConfig) -> int:
        """Calculate max context chars with graduated safety margin."""
        max_input_tokens = member.context_window - member.max_output
        if member.context_window >= 500_000:
            safety = 0.80
        elif member.context_window >= 200_000:
            safety = 0.75
        else:
            safety = 0.70
        usable_tokens = int(max_input_tokens * safety)
        return int(usable_tokens * 3.5)

    # ── LLM call with timeout, retry, circuit breaker, progress ──

    async def _call(
        self, member: MemberConfig, system: str, user_msg: str,
        on_progress: ProgressCallback = None,
        label: str = "",
    ) -> LLMResponse:
        self._check_cancelled()

        endpoint_key = self.factory.endpoint_key(member)
        if not self.factory.circuit_breaker.can_call(endpoint_key):
            raise LLMError(f"Circuit open for {member.model}, skipping")

        client = self.factory.get_client(member)
        timeout = self.config.settings.timeout_seconds

        # Streaming progress: LLM client reports chars received
        async def _stream_progress(chars: int) -> None:
            if on_progress:
                await on_progress(f"  {label} Streaming... {chars:,} chars received")

        async with self.semaphore:
            try:
                resp = await asyncio.wait_for(
                    self._call_with_retry(client, member, system, user_msg, _stream_progress),
                    timeout=timeout,
                )
                self.factory.circuit_breaker.record_success(endpoint_key)
            except asyncio.TimeoutError:
                self.factory.circuit_breaker.record_failure(endpoint_key)
                raise LLMTimeoutError(f"{member.model} timed out after {timeout}s")
            except Exception:
                self.factory.circuit_breaker.record_failure(endpoint_key)
                raise

        self.total_in += resp.input_tokens
        self.total_out += resp.output_tokens
        self.total_cached += resp.cached_tokens
        return resp

    async def _call_multi(
        self, member: MemberConfig, system: str, messages: list[dict],
        on_progress: ProgressCallback = None,
        label: str = "",
    ) -> LLMResponse:
        """Call LLM with multi-turn messages (for tool-based exploration)."""
        self._check_cancelled()

        endpoint_key = self.factory.endpoint_key(member)
        if not self.factory.circuit_breaker.can_call(endpoint_key):
            raise LLMError(f"Circuit open for {member.model}, skipping")

        client = self.factory.get_client(member)
        timeout = self.config.settings.timeout_seconds

        async def _stream_progress(chars: int) -> None:
            if on_progress:
                await on_progress(f"  {label} Streaming... {chars:,} chars received")

        async with self.semaphore:
            try:
                resp = await asyncio.wait_for(
                    self._call_multi_retry(client, member, system, messages, _stream_progress),
                    timeout=timeout,
                )
                self.factory.circuit_breaker.record_success(endpoint_key)
            except asyncio.TimeoutError:
                self.factory.circuit_breaker.record_failure(endpoint_key)
                raise LLMTimeoutError(f"{member.model} timed out after {timeout}s")
            except Exception:
                self.factory.circuit_breaker.record_failure(endpoint_key)
                raise

        self.total_in += resp.input_tokens
        self.total_out += resp.output_tokens
        self.total_cached += resp.cached_tokens
        return resp

    @retry(
        stop=stop_after_attempt(DEFAULT_RETRY_ATTEMPTS),
        wait=wait_exponential(
            multiplier=1, min=DEFAULT_RETRY_MIN_WAIT, max=DEFAULT_RETRY_MAX_WAIT
        ),
        retry=retry_if_exception_type((ConnectionError, TimeoutError, OSError)),
        reraise=True,
    )
    async def _call_with_retry(
        self, client: object, member: MemberConfig, system: str, user_msg: str,
        on_progress=None,
    ) -> LLMResponse:
        per_attempt = min(self.config.settings.timeout_seconds, PER_ATTEMPT_TIMEOUT)
        return await asyncio.wait_for(
            client.generate(  # type: ignore[union-attr]
                model=member.model, system=system,
                messages=[{"role": "user", "content": user_msg}],
                max_tokens=member.max_output,
                thinking_budget=self._thinking_budget(member),
                on_progress=on_progress,
            ),
            timeout=per_attempt,
        )

    @retry(
        stop=stop_after_attempt(DEFAULT_RETRY_ATTEMPTS),
        wait=wait_exponential(
            multiplier=1, min=DEFAULT_RETRY_MIN_WAIT, max=DEFAULT_RETRY_MAX_WAIT
        ),
        retry=retry_if_exception_type((ConnectionError, TimeoutError, OSError)),
        reraise=True,
    )
    async def _call_multi_retry(
        self, client: object, member: MemberConfig, system: str, messages: list[dict],
        on_progress=None,
    ) -> LLMResponse:
        per_attempt = min(self.config.settings.timeout_seconds, PER_ATTEMPT_TIMEOUT)
        return await asyncio.wait_for(
            client.generate(  # type: ignore[union-attr]
                model=member.model, system=system, messages=messages,
                max_tokens=member.max_output,
                thinking_budget=self._thinking_budget(member),
                on_progress=on_progress,
            ),
            timeout=per_attempt,
        )

    # ── Smart context truncation ─────────────────────────────────

    def _smart_truncate_context(self, files_content: str, max_chars: int) -> str:
        """Truncate at file boundaries, extract code structure for large files."""
        if len(files_content) <= max_chars:
            return files_content

        file_sections = re.split(r"(?=^### )", files_content, flags=re.MULTILINE)
        if len(file_sections) <= 1:
            return files_content[:max_chars] + "\n\n[TRUNCATED: content exceeds context window]"

        result_parts: list[str] = []
        total = 0

        for section in file_sections:
            section_len = len(section)
            if total + section_len <= max_chars:
                result_parts.append(section)
                total += section_len
                continue

            remaining = max_chars - total
            if remaining < 500:
                break

            header_match = re.match(r"^### (.+?)(?:\s*\[.+\])?\s*\n", section)
            if header_match:
                filename = header_match.group(1).strip()
                code_match = re.search(r"```\n?(.*?)```", section, re.DOTALL)
                if code_match:
                    code = code_match.group(1)
                    lang = detect_language(Path(filename))
                    structure = extract_code_structure(code, lang, remaining - 200)
                    if len(structure) < len(code):
                        compact = f"### {filename} [STRUCTURE ONLY]\n```\n{structure}\n```\n"
                        if total + len(compact) <= max_chars:
                            result_parts.append(compact)
                            total += len(compact)
                            continue

            result_parts.append(section[:remaining])
            total += remaining
            break

        remaining_count = len(file_sections) - len(result_parts)
        if remaining_count > 0:
            result_parts.append(
                f"\n\n[TRUNCATED: {remaining_count} more files not included. "
                f"Used {total:,} of {len(files_content):,} chars]"
            )

        return "".join(result_parts)

    # ── Step 1: Explore with per-agent stage progress ────────────

    def _build_explore_prompt(self, idx: int, request: str, files_content: str) -> str:
        prompt = (
            f"You are Agent #{idx + 1} in a council of expert reviewers.\n\n"
            f"## Task\n{request}\n\n"
            f"## Project Files\n{files_content}\n\n"
            f"## Instructions\n"
            f"1. Read and understand ALL the code/files thoroughly\n"
            f"2. Identify improvements, issues, and suggestions\n"
            f"3. Return a JSON array of suggestions. Each suggestion:\n"
            f"```json\n"
            f"[\n"
            f'  {{\n'
            f'    "title": "Short title of the improvement",\n'
            f'    "description": "Detailed description: what to change, where, why, how. '
            f'CITE exact file paths, function names, and line numbers.",\n'
            f'    "category": "architecture|performance|security|quality|ux|maintainability|bug|other",\n'
            f'    "priority": "critical|high|medium|low"\n'
            f'  }}\n'
            f"]\n"
            f"```\n"
            f"{_FEW_SHOT_EXAMPLES}\n"
            f"Return ONLY the JSON array. Be specific — cite files, functions, line numbers."
        )
        if self.config.prompts.explore_template:
            prompt = self.config.prompts.explore_template.format(
                agent_num=idx + 1, request=request, files_content=files_content,
            )
        return prompt

    async def _explore_one(
        self, idx: int, request: str, files_content: str,
        progress_cb: ProgressCallback = None,
    ) -> dict:
        member = self.config.council.members[idx]
        model = _short_model(member.model)
        label = f"#{idx+1} ({model}):"
        t0 = time.monotonic()

        async def _report(msg: str) -> None:
            if progress_cb:
                await progress_cb(f"  {label} {msg}")

        # ── Stage 1: Prepare context ──
        max_chars = self._adaptive_max_chars(member)
        await _report(f"Reading context... {len(files_content):,} chars")

        if len(files_content) > max_chars:
            files_content = self._smart_truncate_context(files_content, max_chars)
            await _report(f"Smart-truncated to {len(files_content):,} chars (budget: {max_chars:,})")

        prompt = self._build_explore_prompt(idx, request, files_content)

        try:
            # ── Stage 2: Call LLM ──
            await _report(f"Calling LLM... ({self._estimate_tokens(prompt):,} est. tokens)")

            resp = await self._call(
                member, self.config.prompts.system_prompt, prompt,
                on_progress=progress_cb, label=f"{label}",
            )
            elapsed = time.monotonic() - t0

            # ── Stage 3: Parse response ──
            await _report(f"Parsing response... ({resp.output_tokens:,} tokens, {elapsed:.0f}s)")

            items = parse_json_response(resp.content)
            if isinstance(items, dict):
                items = items.get("suggestions", [items])

            await _report(
                f"Done! {len(items)} suggestions "
                f"[{resp.input_tokens:,}in + {resp.output_tokens:,}out, {elapsed:.0f}s]"
            )

            return {
                "agent": idx,
                "model": member.model,
                "suggestions": items if isinstance(items, list) else [],
                "thinking": resp.thinking,
                "tokens_in": resp.input_tokens,
                "tokens_out": resp.output_tokens,
            }
        except Exception as e:
            elapsed = time.monotonic() - t0
            await _report(f"FAILED after {elapsed:.0f}s: {sanitize_error(e)[:80]}")
            log.error("Agent #%d (%s) explore failed: %s", idx + 1, member.model, sanitize_error(e))
            return {
                "agent": idx, "model": member.model,
                "suggestions": [], "thinking": "",
                "tokens_in": 0, "tokens_out": 0,
                "error": sanitize_error(e),
            }

    # ── Multi-round tool-based exploration with stage progress ───

    async def _explore_one_with_tools(
        self, idx: int, request: str, project_index: ProjectIndex, base_context: str,
        progress_cb: ProgressCallback = None,
    ) -> dict:
        member = self.config.council.members[idx]
        model = _short_model(member.model)
        label = f"#{idx+1} ({model}):"
        t0 = time.monotonic()

        async def _report(msg: str) -> None:
            if progress_cb:
                await progress_cb(f"  {label} {msg}")

        # ── Stage 1: Prepare context ──
        max_chars = self._adaptive_max_chars(member)
        await _report(f"Reading project context... {len(base_context):,} chars")

        if len(base_context) > max_chars:
            base_context = self._smart_truncate_context(base_context, max_chars)
            await _report(f"Smart-truncated to {len(base_context):,} chars")

        tool_desc = (
            "You have access to tools for exploring this project. "
            "You may request files to read before giving suggestions.\n\n"
            "To request files, include a `tool_calls` array in your JSON response:\n"
            "```json\n"
            '{\n'
            '  "tool_calls": [\n'
            '    {"tool": "read_file", "path": "relative/path/to/file.py"},\n'
            '    {"tool": "search", "query": "pattern", "glob": "*.py"},\n'
            '    {"tool": "list_dir", "path": "src/components"}\n'
            '  ]\n'
            '}\n'
            "```\n"
            "Available tools:\n"
            "- `read_file`: Read a file by relative path. Args: `path`\n"
            "- `search`: Search file contents with regex. Args: `query`, `glob` (default `*`)\n"
            "- `list_dir`: List directory contents. Args: `path` (relative dir)\n\n"
            f"You have up to {MAX_TOOL_ROUNDS} rounds of tool calls. "
            "After receiving results, request more or give final suggestions.\n"
            "If you already have enough context, skip tool_calls and return suggestions directly.\n"
        )

        round1_prompt = (
            f"You are Agent #{idx + 1} in a council of expert reviewers.\n\n"
            f"## Task\n{request}\n\n"
            f"## Project Context\n{base_context}\n\n"
            f"## Tools Available\n{tool_desc}\n"
            f"## Instructions\n"
            f"Review the project summary and file listing. "
            f"If you need to read specific files for deeper analysis, "
            f"include tool_calls. Otherwise, provide suggestions directly.\n\n"
            f"{_FEW_SHOT_EXAMPLES}\n"
            f"Return JSON only (either tool_calls object or suggestions array)."
        )

        try:
            messages: list[dict] = [{"role": "user", "content": round1_prompt}]
            all_thinking = ""
            total_in = 0
            total_out = 0
            tool_rounds = 0

            for round_num in range(MAX_TOOL_ROUNDS + 1):
                # ── Call LLM ──
                round_label = f"Round {round_num + 1}" if round_num > 0 else "Initial analysis"
                await _report(f"{round_label}: Calling LLM...")

                resp = await self._call_multi(
                    member, self.config.prompts.system_prompt, messages,
                    on_progress=progress_cb, label=f"{label}",
                )
                all_thinking += (resp.thinking or "") + "\n"
                total_in += resp.input_tokens
                total_out += resp.output_tokens

                # Parse for tool calls
                parsed = None
                try:
                    parsed = parse_json_response(resp.content)
                except Exception:
                    pass

                tool_calls_raw = []
                if isinstance(parsed, dict):
                    tool_calls_raw = parsed.get("tool_calls", [])
                tool_calls = _parse_tool_calls(tool_calls_raw)

                if not tool_calls or round_num >= MAX_TOOL_ROUNDS:
                    if tool_calls and round_num >= MAX_TOOL_ROUNDS:
                        await _report(f"Max tool rounds ({MAX_TOOL_ROUNDS}) reached, finalizing...")
                    break

                # ── Fulfill tool calls ──
                tool_rounds += 1
                tool_names = [f"{tc.tool}({tc.path or tc.query})" for tc in tool_calls[:5]]
                await _report(
                    f"Tool round {tool_rounds}: {len(tool_calls)} calls "
                    f"[{', '.join(tool_names)}{'...' if len(tool_calls) > 5 else ''}]"
                )

                tool_results = self._fulfill_tool_calls(tool_calls, project_index.root)
                messages.append({"role": "assistant", "content": resp.content})
                messages.append({
                    "role": "user",
                    "content": (
                        f"## Tool Results (round {tool_rounds})\n{tool_results}\n\n"
                        f"You have {MAX_TOOL_ROUNDS - tool_rounds} tool rounds remaining. "
                        f"Request more files or provide your final suggestions.\n"
                        f"Return JSON: either tool_calls object or suggestions array."
                    ),
                })

            # ── Extract suggestions ──
            items: list = []
            if isinstance(parsed, list):
                items = parsed
            elif isinstance(parsed, dict):
                if "suggestions" in parsed:
                    items = parsed["suggestions"]
                elif "tool_calls" not in parsed:
                    items = [parsed]

            elapsed = time.monotonic() - t0
            await _report(
                f"Done! {len(items)} suggestions, {tool_rounds + 1} rounds "
                f"[{total_in:,}in + {total_out:,}out, {elapsed:.0f}s]"
            )

            return {
                "agent": idx, "model": member.model,
                "suggestions": items if isinstance(items, list) else [],
                "thinking": all_thinking.strip(),
                "tokens_in": total_in,
                "tokens_out": total_out,
                "tool_rounds": tool_rounds + 1,
            }
        except Exception as e:
            elapsed = time.monotonic() - t0
            await _report(f"FAILED after {elapsed:.0f}s: {sanitize_error(e)[:80]}")
            log.error("Agent #%d (%s) tool-explore failed: %s", idx + 1, member.model, sanitize_error(e))
            return {
                "agent": idx, "model": member.model,
                "suggestions": [], "thinking": "",
                "tokens_in": 0, "tokens_out": 0,
                "error": sanitize_error(e),
            }

    def _fulfill_tool_calls(self, tool_calls: list[ToolCallModel], project_root: Path) -> str:
        results: list[str] = []
        for i, tc in enumerate(tool_calls):
            if i >= MAX_TOOL_CALLS_PER_ROUND:
                results.append(f"[Limit: max {MAX_TOOL_CALLS_PER_ROUND} tool calls per round]")
                break
            if tc.tool == "read_file" and tc.path:
                content = read_file_raw(project_root, tc.path)
                if len(content) > STRUCTURE_EXTRACT_THRESHOLD and not content.startswith("ERROR"):
                    lang = detect_language(Path(tc.path))
                    structure = extract_code_structure(content, lang)
                    results.append(
                        f"### read_file: {tc.path} [STRUCTURE — {len(content):,} chars total]\n"
                        f"```\n{structure}\n```\n"
                    )
                else:
                    results.append(f"### read_file: {tc.path}\n```\n{content}\n```\n")
            elif tc.tool == "search" and tc.query:
                content = search_in_project(project_root, tc.query, tc.glob)
                results.append(f"### search: '{tc.query}' in {tc.glob}\n{content}\n")
            elif tc.tool == "list_dir":
                content = list_directory(project_root, tc.path)
                results.append(f"### list_dir: {tc.path or '.'}\n{content}\n")
            else:
                results.append(f"### Unknown tool: {tc.tool}\n")
        return "\n".join(results) if results else "(no tool calls fulfilled)"

    # ── Explore all with early termination ────────────────────────

    async def explore_all(
        self,
        request: str,
        files_content: str,
        progress_cb: ProgressCallback = None,
        project_index: ProjectIndex | None = None,
    ) -> list[dict]:
        size = len(self.config.council.members)
        use_tools = project_index is not None and project_index.tier in ("large", "huge")
        min_required = max(
            self.config.settings.min_quorum,
            int(size * EARLY_FINISH_RATIO),
        )

        results: list[dict | None] = [None] * size
        completed = 0
        successful = 0

        async def _run_agent(idx: int) -> tuple[int, dict]:
            if use_tools:
                result = await self._explore_one_with_tools(
                    idx, request, project_index, files_content, progress_cb  # type: ignore
                )
            else:
                result = await self._explore_one(idx, request, files_content, progress_cb)
            return idx, result

        tasks = [asyncio.create_task(_run_agent(i), name=f"explore-{i}") for i in range(size)]

        for coro in asyncio.as_completed(tasks):
            try:
                idx, result = await coro
                results[idx] = result
                completed += 1
                if not result.get("error"):
                    successful += 1
                if progress_cb:
                    model = _short_model(result["model"])
                    n_sugg = len(result["suggestions"])
                    rounds = result.get("tool_rounds", 1)
                    tok_info = ""
                    if result.get("tokens_in"):
                        tok_info = f" [{result['tokens_in']:,}+{result['tokens_out']:,} tok]"
                    status = f"{n_sugg} suggestions" if not result.get("error") else f"ERROR"
                    if rounds > 1:
                        status += f" ({rounds} rounds)"
                    await progress_cb(
                        f"[1/4] #{idx+1} ({model}) complete ({completed}/{size}): {status}{tok_info}"
                    )
            except asyncio.CancelledError:
                completed += 1
            except Exception as e:
                completed += 1
                log.error("Explore task exception: %s", e)

            # Early termination
            if successful >= min_required and completed < size:
                remaining_count = size - completed
                if progress_cb:
                    await progress_cb(
                        f"[1/4] Quorum met ({successful}/{size} agents done). "
                        f"Proceeding, skipping {remaining_count} slow agent(s)."
                    )
                for t in tasks:
                    if not t.done():
                        t.cancel()
                for t in tasks:
                    if not t.done():
                        try:
                            await t
                        except (asyncio.CancelledError, Exception):
                            pass
                break

        return [r for r in results if r is not None]

    # ── Step 2: Synthesize with progress ─────────────────────────

    async def synthesize(
        self, request: str, agent_results: list[dict],
        progress_cb: ProgressCallback = None,
    ) -> list[Suggestion]:
        all_raw = ""
        for r in agent_results:
            if r.get("error"):
                continue
            all_raw += f"\n### Agent #{r['agent']+1} ({r['model']}):\n"
            all_raw += json.dumps(r["suggestions"], ensure_ascii=False, indent=2)
            all_raw += "\n"

        synth = self.config.council.synthesizer
        model = _short_model(synth.model)
        label = f"Synthesizer ({model}):"

        async def _report(msg: str) -> None:
            if progress_cb:
                await progress_cb(f"  {label} {msg}")

        prompt = (
            f"You are the Synthesizer. Below are improvement suggestions "
            f"from {len(agent_results)} expert agents.\n\n"
            f"## Original Task\n{request}\n\n"
            f"## All Agent Suggestions\n{all_raw}\n\n"
            f"## Instructions\n"
            f"1. Merge duplicate/overlapping suggestions into one\n"
            f"   - ONLY merge when the exact same change in the exact same file/location is suggested\n"
            f"   - Preserve suggestions that address different components/files even if titles are similar\n"
            f"2. Keep ALL unique ideas — do not drop anything valuable\n"
            f"3. Assign a sequential ID starting from 1\n"
            f"4. Normalize categories and priorities\n"
            f"5. Make descriptions specific and actionable (cite files, lines, functions)\n\n"
            f"Return ONLY a JSON array:\n"
            f"```json\n"
            f"[\n"
            f'  {{\n'
            f'    "id": 1,\n'
            f'    "title": "Short title",\n'
            f'    "description": "Detailed: what, where, why, how",\n'
            f'    "category": "architecture|performance|security|quality|ux|maintainability|bug|other",\n'
            f'    "priority": "critical|high|medium|low",\n'
            f'    "source_agents": [1, 3]\n'
            f'  }}\n'
            f"]\n```\n"
            f"JSON array only."
        )

        if self.config.prompts.synthesize_template:
            prompt = self.config.prompts.synthesize_template.format(
                request=request, agent_count=len(agent_results), all_suggestions=all_raw,
            )

        try:
            await _report(f"Merging suggestions... ({self._estimate_tokens(prompt):,} est. tokens)")
            resp = await self._call(
                synth, self.config.prompts.system_prompt, prompt,
                on_progress=progress_cb, label=f"{label}",
            )
            await _report(f"Parsing merged results... ({resp.output_tokens:,} tokens)")

            items = parse_json_response(resp.content)
            if isinstance(items, dict):
                items = items.get("suggestions", [items])

            suggestions: list[Suggestion] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                try:
                    suggestions.append(Suggestion(
                        id=int(item.get("id", len(suggestions) + 1)),
                        title=str(item.get("title", "")),
                        description=str(item.get("description", "")),
                        category=str(item.get("category", "other")),
                        priority=str(item.get("priority", "medium")),
                        source_agents=item.get("source_agents", []),
                    ))
                except (ValueError, TypeError) as e:
                    log.warning("Skipping malformed suggestion: %s", e)

            merged = sum(len(r["suggestions"]) for r in agent_results if not r.get("error"))
            await _report(f"Done! {merged} raw -> {len(suggestions)} unique suggestions")
            log.info("Synthesized %d raw -> %d deduplicated suggestions", merged, len(suggestions))
            return suggestions
        except Exception as e:
            await _report(f"FAILED: {sanitize_error(e)[:80]}. Using fallback dedup...")
            log.error("Synthesize failed: %s", sanitize_error(e))
            return self._fallback_synthesize(agent_results)

    def _fallback_synthesize(self, agent_results: list[dict]) -> list[Suggestion]:
        seen: set[tuple[str, str]] = set()
        suggestions: list[Suggestion] = []
        sid = 1
        for r in agent_results:
            if r.get("error"):
                continue
            for item in r["suggestions"]:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title", "")).strip().lower()
                cat = str(item.get("category", "other")).strip().lower()
                key = (cat, title)
                if key in seen:
                    continue
                seen.add(key)
                suggestions.append(Suggestion(
                    id=sid,
                    title=str(item.get("title", "")),
                    description=str(item.get("description", "")),
                    category=cat,
                    priority=str(item.get("priority", "medium")),
                    source_agents=[r["agent"] + 1],
                ))
                sid += 1
        log.warning("Fallback synthesizer: %d suggestions (category+title dedup)", len(suggestions))
        return suggestions

    # ── Step 3: Vote with per-agent stage progress ───────────────

    async def _vote_one_agent(
        self, idx: int, request: str, suggestions: list[Suggestion],
        progress_cb: ProgressCallback = None,
    ) -> list[VoteResult]:
        member = self.config.council.members[idx]
        model = _short_model(member.model)
        label = f"#{idx+1} ({model}):"

        async def _report(msg: str) -> None:
            if progress_cb:
                await progress_cb(f"  {label} {msg}")

        suggestions_text = ""
        for s in suggestions:
            suggestions_text += (
                f"\n### #{s.id}: {s.title}\n"
                f"**Category:** {s.category} | **Priority:** {s.priority}\n"
                f"{s.description}\n"
            )

        prompt = (
            f"You are Agent #{idx + 1}. Vote on each improvement suggestion below.\n\n"
            f"## Original Task\n{request}\n\n"
            f"## Suggestions to Vote On\n{suggestions_text}\n\n"
            f"## Instructions\n"
            f"For EACH suggestion, provide:\n"
            f"- agree: true/false (should we implement this?)\n"
            f"- score: 1-10 (importance/impact)\n"
            f"- reasoning: 1 sentence why\n\n"
            f"Return ONLY a JSON array:\n"
            f"```json\n"
            f"[\n"
            f'  {{"id": 1, "agree": true, "score": 8, "reasoning": "..."}},\n'
            f'  {{"id": 2, "agree": false, "score": 3, "reasoning": "..."}}\n'
            f"]\n```\n"
            f"JSON array only. One entry per suggestion."
        )

        if self.config.prompts.vote_template:
            prompt = self.config.prompts.vote_template.format(
                agent_num=idx + 1, request=request, suggestions_text=suggestions_text,
            )

        try:
            await _report(f"Voting on {len(suggestions)} suggestions...")

            resp = await self._call(
                member, self.config.prompts.system_prompt, prompt,
                on_progress=progress_cb, label=f"{label}",
            )

            await _report(f"Parsing votes... ({resp.output_tokens:,} tokens)")

            items = parse_json_response(resp.content)
            if isinstance(items, dict):
                items = items.get("votes", [items])

            votes: list[VoteResult] = []
            seen_ids: set[int] = set()
            for item in items:
                if not isinstance(item, dict):
                    continue
                try:
                    sid = int(item.get("id", 0))
                    if sid in seen_ids:
                        continue
                    seen_ids.add(sid)
                    votes.append(VoteResult(
                        agent_index=idx, agent_model=member.model,
                        suggestion_id=sid,
                        score=max(1, min(10, int(item.get("score", 5)))),
                        agree=parse_bool(item.get("agree", True)),
                        reasoning=str(item.get("reasoning", "")),
                    ))
                except (ValueError, TypeError, KeyError) as e:
                    log.warning("Agent #%d: skipping malformed vote: %s", idx + 1, e)

            await _report(f"Done! {len(votes)} votes cast")
            return votes
        except Exception as e:
            await _report(f"Vote FAILED: {sanitize_error(e)[:60]}")
            log.error("Agent #%d vote failed: %s", idx + 1, sanitize_error(e))
            return []

    async def vote_all(
        self,
        request: str,
        suggestions: list[Suggestion],
        progress_cb: ProgressCallback = None,
    ) -> list[VoteResult]:
        size = len(self.config.council.members)
        min_required = max(
            self.config.settings.min_quorum,
            int(size * EARLY_FINISH_RATIO),
        )
        completed = 0
        successful = 0

        async def _run_vote(idx: int) -> tuple[int, list[VoteResult]]:
            return idx, await self._vote_one_agent(idx, request, suggestions, progress_cb)

        tasks = [asyncio.create_task(_run_vote(i), name=f"vote-{i}") for i in range(size)]
        all_votes: list[VoteResult] = []

        for coro in asyncio.as_completed(tasks):
            try:
                idx, result = await coro
                all_votes.extend(result)
                completed += 1
                successful += 1
                if progress_cb:
                    model = _short_model(self.config.council.members[idx].model)
                    await progress_cb(
                        f"[3/4] #{idx+1} ({model}) voted ({completed}/{size}): {len(result)} votes"
                    )
            except asyncio.CancelledError:
                completed += 1
            except Exception as e:
                completed += 1
                log.error("Vote task exception: %s", e)

            # Early termination
            if successful >= min_required and completed < size:
                remaining_count = size - completed
                if progress_cb:
                    await progress_cb(
                        f"[3/4] Vote quorum met ({successful}/{size}). "
                        f"Skipping {remaining_count} slow agent(s)."
                    )
                for t in tasks:
                    if not t.done():
                        t.cancel()
                for t in tasks:
                    if not t.done():
                        try:
                            await t
                        except (asyncio.CancelledError, Exception):
                            pass
                break

        return all_votes

    # ── Step 4: Compile ──────────────────────────────────────────

    def compile_results(
        self, suggestions: list[Suggestion], votes: list[VoteResult]
    ) -> list[FinalItem]:
        total_agents = len(self.config.council.members)

        votes_by_sid: dict[int, list[VoteResult]] = {s.id: [] for s in suggestions}
        for v in votes:
            if v.suggestion_id in votes_by_sid:
                votes_by_sid[v.suggestion_id].append(v)

        items: list[FinalItem] = []
        for s in suggestions:
            s_votes = sorted(votes_by_sid[s.id], key=lambda v: v.agent_index)
            agree_count = sum(1 for v in s_votes if v.agree)
            avg_score = sum(v.score for v in s_votes) / len(s_votes) if s_votes else 0
            voters = len(s_votes) or total_agents
            items.append(FinalItem(
                suggestion=s,
                avg_score=round(avg_score, 1),
                agree_count=agree_count,
                total_voters=total_agents,
                agree_percent=round(agree_count / voters * 100, 1) if voters else 0,
                votes=s_votes,
            ))

        items.sort(
            key=lambda x: (x.agree_percent, PRIORITY_RANK.get(x.suggestion.priority, 0), x.avg_score),
            reverse=True,
        )
        return items

    # ── Pipeline with full progress reporting ────────────────────

    async def run(
        self,
        request: str,
        files_content: str,
        progress_cb: ProgressCallback = None,
        project_index: ProjectIndex | None = None,
    ) -> tuple[list[dict], list[Suggestion], list[VoteResult], list[FinalItem]]:
        self.total_in = 0
        self.total_out = 0
        self.total_cached = 0
        self._cancelled = asyncio.Event()
        size = len(self.config.council.members)

        tier_info = ""
        if project_index:
            tier_info = f" (tier={project_index.tier}, {project_index.total_files} files)"

        # ── Step 1: Explore ──
        t0 = time.monotonic()
        if progress_cb:
            mode = "with tools" if (project_index and project_index.tier in ("large", "huge")) else "direct"
            models = ", ".join(_short_model(m.model, 15) for m in self.config.council.members)
            await progress_cb(f"[1/4] {size} agents starting analysis{tier_info} [{mode}]")
            await progress_cb(f"  Models: {models}")

        agent_results = await self.explore_all(request, files_content, progress_cb, project_index)

        successful = [r for r in agent_results if not r.get("error")]
        if len(successful) < self.config.settings.min_quorum:
            raise QuorumError(
                f"Only {len(successful)}/{size} agents succeeded, "
                f"minimum quorum is {self.config.settings.min_quorum}"
            )
        explore_time = time.monotonic() - t0

        # ── Step 2: Synthesize ──
        total_raw = sum(len(r["suggestions"]) for r in agent_results)
        if progress_cb:
            await progress_cb(
                f"[2/4] Synthesizing {total_raw} suggestions from {len(successful)} agents... "
                f"(explore took {explore_time:.0f}s)"
            )
        suggestions = await self.synthesize(request, agent_results, progress_cb)

        if not suggestions:
            return agent_results, [], [], []

        synth_time = time.monotonic() - t0 - explore_time

        # ── Step 3: Vote ──
        if progress_cb:
            await progress_cb(
                f"[3/4] {size} agents voting on {len(suggestions)} suggestions... "
                f"(synth took {synth_time:.0f}s)"
            )
        votes = await self.vote_all(request, suggestions, progress_cb)

        agents_that_voted = {v.agent_index for v in votes}
        if len(agents_that_voted) < self.config.settings.min_quorum:
            log.warning("Only %d agents voted (quorum=%d)", len(agents_that_voted), self.config.settings.min_quorum)

        vote_time = time.monotonic() - t0 - explore_time - synth_time

        # ── Step 4: Compile ──
        if progress_cb:
            total_time = time.monotonic() - t0
            await progress_cb(
                f"[4/4] Compiling results... "
                f"(explore={explore_time:.0f}s, synth={synth_time:.0f}s, vote={vote_time:.0f}s, "
                f"total={total_time:.0f}s, tokens={self.total_in:,}in/{self.total_out:,}out)"
            )
        final = self.compile_results(suggestions, votes)

        return agent_results, suggestions, votes, final
