<p align="center">
  <img src="https://img.shields.io/badge/version-2.3.0-blue" alt="Version">
  <img src="https://img.shields.io/badge/python-3.11%2B-green" alt="Python">
  <img src="https://img.shields.io/badge/protocol-MCP-purple" alt="MCP">
  <img src="https://img.shields.io/badge/license-MIT-orange" alt="License">
  <img src="https://img.shields.io/badge/agents-multi--model-red" alt="Multi-Model">
</p>

# Multi-Agent Council Review

> **N AI models analyze your code independently, debate each suggestion through voting, and deliver consensus-ranked improvements — like a senior engineering council that never sleeps.**

A production-grade **Model Context Protocol (MCP) server** that orchestrates multiple LLM agents (Claude, GPT, Gemini, Qwen, DeepSeek, and more) to perform deep, multi-perspective code reviews. Each agent independently explores your codebase, proposes improvements, then the council votes to surface only the suggestions that achieve consensus.

```
You ──► MCP Client (Claude Code, Cursor, etc.)
              │
              ▼
        ┌─────────────────────────────────┐
        │   Council MCP Server (FastMCP)  │
        │                                 │
        │  [1/4] EXPLORE ── N agents ───► │──► Agent 1 (Claude Opus)
        │                                 │──► Agent 2 (GPT-5.4)
        │                                 │──► Agent 3 (Gemini 3.1 Pro)
        │                                 │──► Agent 4 (Qwen3 Max)
        │                                 │──► Agent N (...)
        │                                 │
        │  [2/4] SYNTHESIZE ── merge ───► │──► Synthesizer deduplicates
        │  [3/4] VOTE ── consensus ─────► │──► All agents score 1-10
        │  [4/4] COMPILE ── rank ───────► │──► Final ranked report
        └─────────────────────────────────┘
              │
              ▼
        Markdown / JSON Report
```

---

## Table of Contents

- [Why Multi-Agent?](#why-multi-agent)
- [Features](#features)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [MCP Integration](#mcp-integration)
- [Usage](#usage)
- [Architecture](#architecture)
- [Advanced](#advanced)
- [API Reference](#api-reference)
- [Troubleshooting](#troubleshooting)

---

## Why Multi-Agent?

Single-model reviews have **blind spots**. Claude excels at architecture, GPT at edge cases, Gemini at performance patterns. By running them in parallel and forcing a vote, you get:

| Single Agent | Multi-Agent Council |
|---|---|
| One perspective | 3-8 independent perspectives |
| Model-specific biases | Bias cancellation through voting |
| Miss edge cases | Cross-model coverage |
| No confidence signal | Consensus % per suggestion |
| Static quality | Self-evolving — swap models anytime |

The council doesn't just find more issues — it **filters out noise** through voting. A suggestion that only one agent sees gets low consensus. A critical bug that 7/8 agents flag gets prioritized to the top.

---

## Features

### Core Pipeline
- **Parallel exploration** — All agents analyze simultaneously with streaming progress
- **Smart deduplication** — Synthesizer merges overlapping suggestions across agents
- **Consensus voting** — Each agent scores every suggestion (1-10) with agree/disagree + reasoning
- **Priority ranking** — Final output ranked by consensus %, then priority (critical → low)

### Intelligence
- **Adaptive context management** — Each agent gets optimized context based on its window size (128K → 1M tokens)
- **Smart truncation** — Cuts at file boundaries, extracts code structure (AST-based) for oversized files instead of raw truncation
- **Tool-based exploration** — For huge projects (>10K files), agents navigate the codebase on-demand with `read_file`, `search`, `list_dir` tools
- **Project indexing** — Dependency graph extraction, framework detection, entry point discovery
- **Tiered strategies** — Tiny (<50 files) to Huge (>10K files), automatically selected

### Resilience
- **Circuit breaker** — Per-endpoint failure tracking, auto-recovery after 60s
- **Retry with backoff** — 3 attempts, exponential backoff (2s → 30s)
- **Per-agent isolation** — One agent failing doesn't kill the session
- **Early termination** — Configurable quorum (proceed when 75% agents finish)
- **Streaming heartbeat** — 30s progress updates to detect stuck agents

### Security
- **Secret redaction** — Auto-detects and masks API keys (OpenAI `sk-`, AWS `AKIA`, GitHub `ghp_`, etc.)
- **Path validation** — Blocks symlink escapes, sensitive directories (`.ssh`, `.aws`, `.gnupg`)
- **Sensitive file blocking** — Skips `.env`, `*.key`, `*.pem`, `credentials.*`
- **Rate limiting** — Token bucket, 10 calls/minute default

### Observability
- **Session history** — SQLite-backed tracking with token counts, duration, and full reports
- **Cost estimation** — Per-model pricing with USD totals
- **Token tracking** — Input, output, and cached tokens per agent
- **Detailed logging** — Structured logs with sanitized errors

---

## Quick Start

### 1. Clone & Install

```bash
git clone https://github.com/dustdustpy/multi-agent-council.git
cd multi-agent-council

python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

### 2. Configure

```bash
cp council_config.example.json council_config.json
```

Edit `council_config.json` with your API keys and models:

```json
{
  "council": {
    "members": [
      {
        "model": "claude-sonnet-4-5-20250929",
        "format": "anthropic",
        "base_url": "https://api.anthropic.com",
        "api_key": "sk-ant-...",
        "context_window": 200000,
        "max_output": 64000
      },
      {
        "model": "gpt-4o",
        "format": "openai",
        "base_url": "https://api.openai.com/v1",
        "api_key": "sk-...",
        "context_window": 128000,
        "max_output": 16384
      },
      {
        "model": "gemini-2.5-pro",
        "format": "openai",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "api_key": "AIza...",
        "context_window": 1000000,
        "max_output": 65536
      }
    ],
    "synthesizer": {
      "model": "claude-sonnet-4-5-20250929",
      "format": "anthropic",
      "base_url": "https://api.anthropic.com",
      "api_key": "sk-ant-...",
      "context_window": 200000,
      "max_output": 64000
    }
  }
}
```

### 3. Test Connectivity

```bash
python -c "
from council.tools import mcp_server
# Or use council_test via MCP
"
```

### 4. Add to Your MCP Client

See [MCP Integration](#mcp-integration) below.

---

## Configuration

### Full Config Schema

```json
{
  "council": {
    "members": [
      {
        "model": "string",          // Model name (e.g., "claude-opus-4-6")
        "format": "anthropic|openai", // API protocol
        "base_url": "string",        // API endpoint (supports ${ENV_VAR})
        "api_key": "string",         // API key (supports ${ENV_VAR})
        "context_window": 200000,    // Max input tokens
        "max_output": 4096           // Max output tokens
      }
    ],
    "synthesizer": { /* same schema as member */ }
  },
  "prompts": {
    "system_prompt": "string",       // Override system message
    "explore_template": "",          // Custom explore prompt
    "synthesize_template": "",       // Custom synthesize prompt
    "vote_template": ""              // Custom vote prompt
  },
  "settings": {
    "timeout_seconds": 600,          // Global timeout (≥10)
    "max_concurrent": 20,            // Max parallel LLM calls
    "allowed_roots": [],             // Restrict file access paths
    "show_thinking": false,          // Include thinking in report
    "debug": false,                  // Extended logging
    "min_quorum": 1,                 // Min agents required
    "rate_limit_per_minute": 10,     // Rate limit
    "quick_council_size": 3          // Agents for quick mode
  }
}
```

### Environment Variable Support

Use `${VAR_NAME}` in `api_key` and `base_url` to reference environment variables:

```json
{
  "model": "claude-opus-4-6",
  "base_url": "${ANTHROPIC_BASE_URL}",
  "api_key": "${ANTHROPIC_API_KEY}"
}
```

### Supported Models & Formats

| Format | Models | Notes |
|--------|--------|-------|
| `anthropic` | Claude Opus, Sonnet, Haiku | Native thinking, prompt caching |
| `openai` | GPT-4o, GPT-5, o3/o4 | Streaming with usage tracking |
| `openai` | Gemini (via OpenAI compat) | 1M context window |
| `openai` | Qwen, DeepSeek, GLM, Kimi | Via compatible endpoints |
| `anthropic` | Any model via proxy | Proxy translates to native API |

### Proxy Support (Unified Endpoint)

If you use a proxy that supports multiple models through a single endpoint:

```json
{
  "model": "claude-opus-4-6(max)",
  "format": "anthropic",
  "base_url": "http://your-proxy:8317",
  "api_key": "your-key"
}
```

Model name suffixes like `(max)`, `(xhigh)` can control thinking/reasoning effort when the proxy supports it.

---

## MCP Integration

### Claude Code

Add to your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "council-review": {
      "command": "/path/to/multi-agent-council/.venv/bin/python",
      "args": ["/path/to/multi-agent-council/council_server.py"],
      "env": {
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

Or add globally to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "council-review": {
      "command": "python",
      "args": ["/absolute/path/to/council_server.py"],
      "env": {
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

### Cursor / Windsurf / VS Code

Add to your MCP settings (varies by editor):

```json
{
  "council-review": {
    "command": "python",
    "args": ["council_server.py"],
    "cwd": "/path/to/multi-agent-council"
  }
}
```

### Verify Connection

Once connected, use the `council_test` tool to verify all agents are reachable:

```
> Use council_test to check connectivity

✅ #1 claude-opus-4-6     OK  2.3s  thinking=yes
✅ #2 gpt-4o              OK  1.1s  thinking=no
✅ #3 gemini-2.5-pro      OK  1.8s  thinking=no
✅ Synthesizer            OK  2.1s  thinking=yes
```

---

## Usage

### Basic — Review Files

```
Review src/auth/login.py and src/auth/token_manager.py for security issues
```

The council will:
1. Send both files to all agents in parallel
2. Each agent independently identifies issues
3. Synthesizer merges duplicate suggestions
4. All agents vote on each suggestion
5. Return ranked results with consensus %

### Review a Folder

```
Review the entire src/api/ directory for performance and architecture
```

Folders are automatically traversed with intelligent filtering (skips `node_modules`, `.git`, binaries, etc.).

### Quick Mode

```
Use council with mode="quick" to review src/utils.py
```

Quick mode uses fewer agents (`quick_council_size`, default 3) for faster results.

### JSON Output

```
Use council with output_format="json" to review src/models/
```

Returns structured JSON for programmatic consumption / CI integration.

### Direct CLI (No MCP)

```bash
python run_council.py "Review for bugs and security" "src/" "full"
```

Features real-time ANSI display with per-agent streaming progress:

```
  [03:42] [1/4] EXPLORE  5/7 agents streaming...
  ──────────────────────────────────────────────
  OK  #01  claude-opus-4-6       Done! 12 suggestions [52,340in + 3,201out, 45s]
  OK  #02  gpt-5.4               Done! 8 suggestions [48,120in + 2,890out, 38s]
  <>  #03  gemini-3.1-pro        Streaming... 24,500 chars received
  OK  #04  kimi-k2.5             Done! 11 suggestions [48,519in + 2,447out, 32s]
  OK  #05  qwen3-max             Done! 5 suggestions [49,395in + 709out, 28s]
  <>  #06  glm-5                 Streaming... 10,850 chars received
  OK  #07  MiniMax-M2.5          Done! 9 suggestions [45,200in + 1,980out, 35s]
  Syn  claude-opus-4-6           ..  waiting
  ──────────────────────────────────────────────
  Tokens: 341,574in + 11,227out | Time: 03:42
```

---

## Architecture

### 4-Stage Pipeline

```
┌─────────────────────────────────────────────────────────────────┐
│                                                                 │
│  [1/4] EXPLORE                                                  │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐             │
│  │ Agent 1  │ │ Agent 2  │ │ Agent 3  │ │ Agent N  │  parallel  │
│  │ Claude   │ │ GPT      │ │ Gemini   │ │ ...      │            │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘            │
│       │            │            │            │                  │
│       ▼            ▼            ▼            ▼                  │
│  suggestions₁  suggestions₂ suggestions₃  suggestionsₙ         │
│                                                                 │
│  [2/4] SYNTHESIZE                                               │
│  ┌──────────────────────────────────────────┐                   │
│  │ Synthesizer (Claude/GPT)                 │                   │
│  │ Merge duplicates, normalize categories   │                   │
│  └──────────────────┬───────────────────────┘                   │
│                     │                                           │
│                     ▼                                           │
│            deduplicated_suggestions                             │
│                                                                 │
│  [3/4] VOTE                                                     │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐             │
│  │ Agent 1  │ │ Agent 2  │ │ Agent 3  │ │ Agent N  │  parallel  │
│  │ Score    │ │ Score    │ │ Score    │ │ Score    │            │
│  │ 1-10     │ │ 1-10     │ │ 1-10     │ │ 1-10     │            │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘            │
│       │            │            │            │                  │
│       ▼            ▼            ▼            ▼                  │
│                  vote_results[]                                 │
│                                                                 │
│  [4/4] COMPILE                                                  │
│  ┌──────────────────────────────────────────┐                   │
│  │ Rank by: consensus % → priority → score  │                   │
│  │ Generate: Markdown or JSON report        │                   │
│  └──────────────────────────────────────────┘                   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### Project Structure

```
multi-agent-council/
├── council_server.py              # MCP server entry point (FastMCP)
├── run_council.py                 # CLI runner with ANSI display
├── council_config.json            # Your config (gitignored)
├── council_config.example.json    # Example config template
├── requirements.txt               # Dependencies
├── pyproject.toml                 # Project metadata
│
├── council/
│   ├── __init__.py
│   ├── tools.py                   # MCP tool definitions (4 tools)
│   ├── engine.py                  # Core pipeline orchestrator
│   ├── config.py                  # Configuration schema (Pydantic)
│   ├── constants.py               # Limits, thresholds, mappings
│   ├── models.py                  # Data models (Suggestion, Vote, etc.)
│   ├── project_indexer.py         # Codebase analysis & indexing
│   ├── file_reader.py             # Safe file I/O with validation
│   ├── security.py                # Redaction, path validation, rate limit
│   ├── history.py                 # SQLite session tracking
│   ├── utils.py                   # JSON parsing, bool handling
│   ├── exceptions.py              # Error hierarchy
│   ├── logging_config.py          # Structured logging setup
│   │
│   ├── llm/
│   │   ├── base.py                # Abstract LLM client interface
│   │   ├── anthropic_client.py    # Anthropic (streaming, caching, thinking)
│   │   ├── openai_client.py       # OpenAI-compatible (streaming, fallback)
│   │   ├── circuit_breaker.py     # Failure tracking & recovery
│   │   └── factory.py             # Client caching & connection pooling
│   │
│   └── formatters/
│       ├── markdown.py            # Markdown report with vote tables
│       └── json_fmt.py            # Structured JSON output
│
└── council_reports/               # Generated reports (timestamped)
```

### Context Management Strategy

The engine automatically selects the optimal strategy based on project size:

| Project Size | Files | Strategy |
|---|---|---|
| **Tiny** | < 50 | Load all files directly |
| **Small** | 50 - 200 | All files + project summary |
| **Medium** | 200 - 1K | Smart selection by relevance + summary |
| **Large** | 1K - 10K | Summary + changed files + dependency chain |
| **Huge** | > 10K | Tool-based exploration (agents navigate on-demand) |

For each agent, context is adapted to its specific window size with graduated safety margins (70-80% utilization).

---

## Advanced

### Self-Review (Meta-Testing)

Use the council to review its own code — the ultimate dogfooding:

```
Review council/engine.py and council/llm/ for architecture and performance
```

### Custom Prompts

Override the default system prompt for specialized reviews:

```json
{
  "prompts": {
    "system_prompt": "You are a security auditor focused on OWASP Top 10 vulnerabilities. Flag every potential injection, XSS, CSRF, and auth bypass."
  }
}
```

### Model Mixing Strategies

**Balanced Council** — Mix model families for diverse perspectives:
```json
{
  "members": [
    {"model": "claude-opus-4-6", "format": "anthropic"},
    {"model": "gpt-5.4", "format": "openai"},
    {"model": "gemini-3.1-pro", "format": "openai"},
    {"model": "qwen3-max", "format": "openai"},
    {"model": "deepseek-v3", "format": "openai"}
  ]
}
```

**Speed Council** — Fast models for quick iterations:
```json
{
  "members": [
    {"model": "claude-haiku-4-5", "format": "anthropic"},
    {"model": "gpt-4o-mini", "format": "openai"},
    {"model": "gemini-2.0-flash", "format": "openai"}
  ],
  "settings": {"quick_council_size": 3}
}
```

**Deep Analysis Council** — Heavy models with extended thinking:
```json
{
  "members": [
    {"model": "claude-opus-4-6", "format": "anthropic", "max_output": 128000},
    {"model": "gpt-5.4", "format": "openai", "max_output": 128000},
    {"model": "gemini-3.1-pro", "format": "openai", "max_output": 65536}
  ],
  "settings": {"timeout_seconds": 600}
}
```

### Thinking / Reasoning Control

For models behind a unified proxy, append thinking level to model name:

```json
{"model": "claude-opus-4-6(max)"}      // Max thinking budget
{"model": "gpt-5.4(xhigh)"}           // Extended reasoning
{"model": "gemini-3.1-pro(max)"}      // Max thinking
```

For native Anthropic models, the engine automatically allocates thinking budget:
```
thinking_budget = max_output - 4096 (reserve)
```

### Tool-Based Exploration (Huge Projects)

For projects exceeding 10K files, agents don't receive a static context dump. Instead, they get navigation tools:

- **`read_file(path)`** — Read specific files on-demand (max 100KB)
- **`search(query)`** — Regex search across the project
- **`list_dir(path)`** — Browse directory structure

Each agent gets up to **3 rounds** of tool use with **20 calls per round**, allowing intelligent, targeted exploration.

### CI/CD Integration

Use JSON output for automated pipelines:

```bash
python run_council.py "Review for critical bugs" "src/" "full" > report.json
```

Parse the output to gate deployments:

```python
import json

report = json.load(open("report.json"))
critical = [s for s in report["suggestions"] if s["priority"] == "critical"]
if critical:
    print(f"BLOCKED: {len(critical)} critical issues found")
    exit(1)
```

### History & Tracking

All sessions are recorded in `council_history.db`:

```
> Use council_history

| # | Date       | Request          | Suggestions | Tokens        | Time |
|---|------------|------------------|-------------|---------------|------|
| 1 | 2026-03-06 | Review auth...   | 15          | 342K in + 12K | 3m42s|
| 2 | 2026-03-05 | Security audit...| 8           | 198K in + 6K  | 2m15s|
```

---

## API Reference

### MCP Tools

#### `council`

Main review tool. Orchestrates the full 4-stage pipeline.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `request` | string | *required* | Review instructions (max 50KB) |
| `file_paths` | string | `""` | Comma-separated file/folder paths (max 50) |
| `output_format` | string | `"markdown"` | `"markdown"` or `"json"` |
| `mode` | string | `"full"` | `"full"` (all agents) or `"quick"` (subset) |

#### `council_test`

Tests API connectivity for all configured agents and synthesizer.

*No parameters.*

#### `council_health`

Lightweight config validation without LLM calls.

*No parameters.* Returns: version, models, features, status.

#### `council_history`

View recent review sessions.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `limit` | int | `10` | Number of recent sessions |

### Output Format — Suggestion

Each suggestion in the final report contains:

```json
{
  "id": 1,
  "title": "Fix race condition in token refresh",
  "description": "In auth/token_manager.py:142, refresh_token() reads and writes self._token without a lock...",
  "category": "bug",
  "priority": "critical",
  "consensus": "87.5%",
  "avg_score": 8.7,
  "agree": "7/8",
  "votes": [
    {"agent": "claude-opus-4-6", "score": 9, "agree": true, "reasoning": "..."},
    {"agent": "gpt-5.4", "score": 8, "agree": true, "reasoning": "..."}
  ]
}
```

**Categories:** `architecture` | `performance` | `security` | `quality` | `ux` | `maintainability` | `bug` | `other`

**Priorities:** `critical` | `high` | `medium` | `low`

---

## Troubleshooting

### Agents stuck at "waiting..."

1. **Check connectivity:** Run `council_test` first
2. **Timeout too low:** Increase `timeout_seconds` to 300-600 for large codebases
3. **Circuit breaker open:** If an endpoint failed 3+ times, it's blocked for 60s. Restart the MCP server to reset.

### "Streaming is required" error (Anthropic)

This is handled automatically. The client always uses streaming. If you see this, update the `anthropic` package:
```bash
pip install --upgrade anthropic
```

### Token budget exceeded

Reduce context by:
- Reviewing fewer files at once
- Lowering `context_window` in config
- Using `quick` mode for iterative reviews

### Rate limit errors

Increase `rate_limit_per_minute` in settings, or add delays between reviews.

---

## Dependencies

| Package | Version | Purpose |
|---|---|---|
| `mcp[cli]` | ≥ 1.9.0 | MCP server framework |
| `anthropic` | ≥ 0.45.0 | Anthropic API client |
| `openai` | ≥ 1.60.0 | OpenAI-compatible client |
| `pydantic` | ≥ 2.0.0 | Config validation |
| `tenacity` | ≥ 8.2.0 | Retry logic |

---

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Use the council to review your own changes (yes, really)
4. Commit and push
5. Open a Pull Request

---

<p align="center">

**Built with the belief that the best code review comes from multiple perspectives.**

`#multi-agent` `#code-review` `#mcp` `#llm` `#claude` `#gpt` `#gemini` `#ai-council` `#consensus-voting` `#self-evolving` `#model-context-protocol` `#code-quality` `#static-analysis` `#multi-model` `#agentic-ai`

</p>
