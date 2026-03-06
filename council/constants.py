"""Named constants — replaces all magic numbers and inline collections."""
from __future__ import annotations

# Priority system
PRIORITY_RANK: dict[str, int] = {
    "critical": 4, "high": 3, "medium": 2, "low": 1,
}
PRIORITY_ICONS: dict[str, str] = {
    "critical": "\U0001f534", "high": "\U0001f7e0",
    "medium": "\U0001f7e1", "low": "\U0001f7e2",
}
VALID_CATEGORIES = frozenset({
    "architecture", "performance", "security", "quality",
    "ux", "maintainability", "bug", "other",
})
VALID_PRIORITIES = frozenset({"critical", "high", "medium", "low"})

# File reading
TEXT_EXTENSIONS = frozenset({
    ".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".yaml", ".yml", ".toml",
    ".md", ".txt", ".html", ".css", ".scss", ".sql", ".sh", ".bat", ".ps1",
    ".env", ".cfg", ".ini", ".xml", ".csv", ".rs", ".go", ".java", ".c",
    ".cpp", ".h", ".hpp", ".rb", ".php", ".swift", ".kt", ".dart", ".vue",
    ".svelte", ".astro", ".prisma", ".graphql", ".proto", ".tf", ".dockerfile",
    ".lock", ".editorconfig", ".gitignore", ".dockerignore",
    # DevOps/infrastructure (#17)
    ".hcl", ".tfvars", ".tpl", ".conf", ".nginx", ".j2", ".jinja2",
})
SPECIAL_FILENAMES = frozenset({
    "makefile", "dockerfile", "gemfile", "rakefile",
    ".env.local", ".env.example", ".prettierrc", ".eslintrc", "tsconfig.json",
    "jenkinsfile", "caddyfile", "procfile", "brewfile",
})
SKIP_DIRS = frozenset({
    ".git", ".venv", "node_modules", "__pycache__",
    ".next", "dist", "build", ".claude", ".idea", ".vscode",
})
SENSITIVE_FILE_PATTERNS = frozenset({
    "*.pem", "*.key", "id_rsa", "id_ed25519", "*.p12", "*.pfx",
    "secrets.*", ".env*", "credentials.*", "*.secret",
})

# Limits
MAX_FILE_SIZE = 100_000          # 100KB per file
MAX_FOLDER_SIZE = 5_000_000      # 5MB total for folder (enterprise projects)
MAX_REQUEST_LENGTH = 50_000
MAX_FILE_PATHS = 50
DEFAULT_TIMEOUT = 60000
DEFAULT_MAX_CONCURRENT = 20
DEFAULT_CONTEXT_WINDOW = 200_000
DEFAULT_THINKING_RESERVE = 4096
DEFAULT_RATE_LIMIT = 10          # calls per minute

# Multi-round exploration (tool-based for large/huge projects)
MAX_TOOL_ROUNDS = 3              # max rounds of tool-use per agent (was 2)
MAX_TOOL_CALLS_PER_ROUND = 20   # max file reads per round (was 10)
MAX_TOOL_READ_SIZE = 100_000    # max chars per tool-read file (was 50_000)

# Per-attempt and pipeline timeouts
PER_ATTEMPT_TIMEOUT = 300        # individual LLM attempt timeout (seconds)
EARLY_FINISH_RATIO = 1.0        # wait for ALL agents that are still connected
HEARTBEAT_INTERVAL = 30         # seconds between progress heartbeats

# Code structure extraction
STRUCTURE_EXTRACT_THRESHOLD = 30_000  # extract signatures above this size

# Retry
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_MIN_WAIT = 2
DEFAULT_RETRY_MAX_WAIT = 30

# Circuit breaker
CIRCUIT_FAILURE_THRESHOLD = 3
CIRCUIT_RECOVERY_TIMEOUT = 60.0

# Cost estimation (per 1M tokens)
MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-6": {"input": 15.0, "output": 75.0},
    "claude-sonnet-4-5-20250929": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5": {"input": 0.80, "output": 4.0},
    "_default": {"input": 3.0, "output": 15.0},
}
