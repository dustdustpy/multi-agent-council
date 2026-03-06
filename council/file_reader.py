"""Secure file/folder reader with deduplication, redaction, and code structure extraction."""
from __future__ import annotations

import ast
import asyncio
import logging
import re
from pathlib import Path

from .constants import (
    MAX_FILE_SIZE,
    MAX_FOLDER_SIZE,
    MAX_TOOL_READ_SIZE,
    SKIP_DIRS,
    SPECIAL_FILENAMES,
    STRUCTURE_EXTRACT_THRESHOLD,
    TEXT_EXTENSIONS,
)
from .security import is_sensitive_file, redact_secrets, validate_path

log = logging.getLogger("council.file_reader")


def read_paths(
    paths: list[str],
    allowed_roots: list[str] | None = None,
    apply_redaction: bool = True,
) -> str:
    """Read files/folders, validate paths, deduplicate, redact secrets."""
    resolved_roots = (
        [Path(r).expanduser().resolve() for r in allowed_roots]
        if allowed_roots
        else None
    )
    seen: set[Path] = set()
    sections: list[str] = []

    for p in paths:
        p = p.strip()
        if not p:
            continue
        fp = Path(p).expanduser().resolve()

        # Path validation
        ok, reason = validate_path(fp, resolved_roots)
        if not ok:
            sections.append(f"### {p}\n*ACCESS DENIED: {reason}*\n")
            log.warning("Path denied: %s — %s", p, reason)
            continue

        if fp in seen:
            continue
        seen.add(fp)

        if fp.is_dir():
            sections.append(_read_folder(fp, seen))
        elif fp.is_file():
            sections.append(_read_one_file(fp))
        else:
            sections.append(f"### {p}\n*Does not exist*\n")

    result = "\n".join(sections)
    if apply_redaction:
        result = redact_secrets(result)
    return result


async def read_paths_async(
    paths: list[str],
    allowed_roots: list[str] | None = None,
    apply_redaction: bool = True,
) -> str:
    """Async wrapper — runs synchronous file I/O in thread pool."""
    return await asyncio.to_thread(read_paths, paths, allowed_roots, apply_redaction)


def _read_folder(folder: Path, seen: set[Path] | None = None) -> str:
    """Recursively read text files in folder, skipping hidden/binary/large."""
    if seen is None:
        seen = set()
    sections: list[str] = []
    total_size = 0

    for f in sorted(folder.rglob("*")):
        # Skip unwanted directories
        if any(part in SKIP_DIRS for part in f.parts):
            continue
        if not f.is_file():
            continue

        # Symlink escape check
        try:
            real_f = f.resolve(strict=True)
        except OSError:
            continue
        try:
            real_f.relative_to(folder.resolve())
        except ValueError:
            continue  # symlink escapes folder

        # Deduplication
        if real_f in seen:
            continue
        seen.add(real_f)

        # Extension filter
        if (
            f.suffix.lower() not in TEXT_EXTENSIONS
            and f.name.lower() not in SPECIAL_FILENAMES
        ):
            continue

        # Skip sensitive files
        if is_sensitive_file(f):
            sections.append(
                f"### {f.relative_to(folder)}\n*[SKIPPED: sensitive file]*\n"
            )
            continue

        try:
            content = f.read_text(encoding="utf-8", errors="replace")
            if total_size + len(content) > MAX_FOLDER_SIZE:
                content = content[: max(0, MAX_FOLDER_SIZE - total_size)]
                sections.append(
                    f"### {f.relative_to(folder)}\n```\n{content}\n```\n"
                    f"*[TRUNCATED — reached {MAX_FOLDER_SIZE // 1000}KB limit]*\n"
                )
                total_size = MAX_FOLDER_SIZE
                break
            if len(content) > MAX_FILE_SIZE:
                content = content[:MAX_FILE_SIZE] + f"\n... [truncated at {MAX_FILE_SIZE // 1000}KB]"
            sections.append(f"### {f.relative_to(folder)}\n```\n{content}\n```\n")
            total_size += len(content)
        except Exception as e:
            sections.append(f"### {f.relative_to(folder)}\n*Error: {e}*\n")

    header = f"## Folder: {folder}\n*{len(sections)} files, {total_size:,} bytes*\n"
    return header + "\n".join(sections)


def _read_one_file(fp: Path) -> str:
    """Read a single file with size limit."""
    if is_sensitive_file(fp):
        return f"### {fp.name}\n*[SKIPPED: sensitive file]*\n"
    try:
        content = fp.read_text(encoding="utf-8", errors="replace")
        if len(content) > MAX_FILE_SIZE:
            content = content[:MAX_FILE_SIZE] + f"\n... [truncated at {MAX_FILE_SIZE // 1000}KB]"
        return f"### {fp.name}\n```\n{content}\n```\n"
    except Exception as e:
        return f"### {fp.name}\n*Error: {e}*\n"


# ── On-demand file access (for tool-based exploration) ───────────


def read_file_raw(
    root: Path,
    relative_path: str,
    max_size: int = MAX_TOOL_READ_SIZE,
) -> str:
    """Read a single file by relative path. Used by agent tool calls."""
    fp = (root / relative_path).resolve()
    # Security: must be inside root
    try:
        fp.relative_to(root.resolve())
    except ValueError:
        return f"ERROR: Path escapes project root"

    if not fp.is_file():
        return f"ERROR: File not found: {relative_path}"
    if is_sensitive_file(fp):
        return f"ERROR: Sensitive file, access denied"

    try:
        content = fp.read_text(encoding="utf-8", errors="replace")
        if len(content) > max_size:
            content = content[:max_size] + f"\n... [truncated at {max_size // 1000}KB]"
        return content
    except Exception as e:
        return f"ERROR: {e}"


def search_in_project(
    root: Path,
    query: str,
    glob_pattern: str = "*.py",
    max_results: int = 20,
) -> str:
    """Search for a string pattern across project files. Returns matching snippets."""
    import re as _re

    results: list[str] = []
    try:
        pattern = _re.compile(query, _re.IGNORECASE)
    except _re.error:
        pattern = _re.compile(_re.escape(query), _re.IGNORECASE)

    for f in sorted(root.rglob(glob_pattern)):
        if any(part in SKIP_DIRS for part in f.parts):
            continue
        if not f.is_file():
            continue
        try:
            content = _read_with_fallback_encoding(f)
        except Exception:
            continue

        matches = []
        for i, line in enumerate(content.split("\n"), 1):
            if pattern.search(line):
                matches.append(f"  L{i}: {line.rstrip()[:200]}")

        if matches:
            rel = str(f.relative_to(root)).replace("\\", "/")
            results.append(f"### {rel}\n" + "\n".join(matches[:10]))
            if len(results) >= max_results:
                break

    if not results:
        return f"No matches for '{query}' in {glob_pattern}"
    return "\n\n".join(results)


# ── Enhanced encoding detection ──────────────────────────────────

_ENCODINGS = ("utf-8", "utf-8-sig", "latin-1", "cp1252", "gb2312", "shift_jis")


def _read_with_fallback_encoding(fp: Path, max_size: int = 0) -> str:
    """Read file with multiple encoding attempts."""
    for enc in _ENCODINGS:
        try:
            content = fp.read_text(encoding=enc)
            if max_size and len(content) > max_size:
                content = content[:max_size]
            return content
        except (UnicodeDecodeError, UnicodeError):
            continue
    # Last resort: replace errors
    return fp.read_text(encoding="utf-8", errors="replace")


# ── Code structure extraction ────────────────────────────────────


def extract_code_structure(content: str, language: str, max_size: int = 0) -> str:
    """Extract class/function signatures from code for context-efficient representation.

    Returns a compact summary showing imports, class definitions, function signatures,
    and docstrings — without full implementation bodies. This allows agents to understand
    the structure of large files without consuming too many tokens.
    """
    if language == "python":
        return _extract_python_structure(content, max_size)
    elif language in ("javascript", "typescript", "react", "react-ts"):
        return _extract_js_structure(content, max_size)
    elif language in ("go", "rust", "java", "c", "cpp", "c-header"):
        return _extract_c_family_structure(content, language, max_size)
    # Fallback: head + tail
    return _extract_head_tail(content, max_size or STRUCTURE_EXTRACT_THRESHOLD)


def _extract_python_structure(content: str, max_size: int = 0) -> str:
    """Extract Python class/function signatures with docstrings using AST."""
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return _extract_head_tail(content, max_size or STRUCTURE_EXTRACT_THRESHOLD)

    lines = content.split("\n")
    sections: list[str] = []

    # Module docstring
    if (tree.body and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)
            and isinstance(tree.body[0].value.value, str)):
        doc = tree.body[0].value.value
        sections.append(f'"""{doc}"""')
        sections.append("")

    # Imports (first 50 lines of imports)
    import_lines = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            lineno = node.lineno - 1
            if 0 <= lineno < len(lines):
                import_lines.append(lines[lineno])
    if import_lines:
        sections.append("# --- Imports ---")
        sections.extend(import_lines[:50])
        if len(import_lines) > 50:
            sections.append(f"# ... {len(import_lines) - 50} more imports")
        sections.append("")

    # Top-level assignments (constants, type aliases)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            lineno = node.lineno - 1
            if 0 <= lineno < len(lines):
                line = lines[lineno]
                if line.strip() and not line.strip().startswith("#"):
                    sections.append(line)
        elif isinstance(node, ast.AnnAssign):
            lineno = node.lineno - 1
            if 0 <= lineno < len(lines):
                sections.append(lines[lineno])

    sections.append("")

    # Classes and functions
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            sections.extend(_extract_class_node(node, lines, indent=0))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            sections.extend(_extract_func_node(node, lines, indent=0))

    result = "\n".join(sections)
    total = len(lines)
    result += f"\n\n# [Structure extracted: {total} lines total, full impl omitted]"
    return result


def _extract_class_node(node: ast.ClassDef, lines: list[str], indent: int) -> list[str]:
    """Extract a class definition with its methods."""
    prefix = "    " * indent
    result: list[str] = []

    # Decorators
    for deco in node.decorator_list:
        lineno = deco.lineno - 1
        if 0 <= lineno < len(lines):
            result.append(lines[lineno])

    # Class definition line
    lineno = node.lineno - 1
    if 0 <= lineno < len(lines):
        result.append(lines[lineno])

    # Class docstring
    if (node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)):
        doc = node.body[0].value.value
        if "\n" in doc:
            result.append(f'{prefix}    """{doc}"""')
        else:
            result.append(f'{prefix}    """{doc}"""')

    # Methods
    for child in node.body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result.extend(_extract_func_node(child, lines, indent + 1))
        elif isinstance(child, ast.ClassDef):
            result.extend(_extract_class_node(child, lines, indent + 1))
        elif isinstance(child, (ast.Assign, ast.AnnAssign)):
            cline = child.lineno - 1
            if 0 <= cline < len(lines):
                result.append(lines[cline])

    result.append("")
    return result


def _extract_func_node(
    node: ast.FunctionDef | ast.AsyncFunctionDef, lines: list[str], indent: int
) -> list[str]:
    """Extract a function signature with docstring."""
    prefix = "    " * indent
    result: list[str] = []

    # Decorators
    for deco in node.decorator_list:
        lineno = deco.lineno - 1
        if 0 <= lineno < len(lines):
            result.append(lines[lineno])

    # Function def — may span multiple lines
    start = node.lineno - 1
    # Find the colon that ends the signature
    end = start
    for i in range(start, min(start + 10, len(lines))):
        if lines[i].rstrip().endswith(":"):
            end = i
            break
    for i in range(start, end + 1):
        if 0 <= i < len(lines):
            result.append(lines[i])

    # Docstring
    if (node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)):
        doc = node.body[0].value.value
        doc_lines = doc.strip().split("\n")
        if len(doc_lines) <= 5:
            for dl in doc_lines:
                result.append(f"{prefix}    {dl.strip()}" if dl.strip() else "")
        else:
            result.append(f'{prefix}    """{doc_lines[0]}')
            result.append(f"{prefix}    ... ({len(doc_lines)} lines)")
            result.append(f'{prefix}    """')

    result.append(f"{prefix}    ...")
    result.append("")
    return result


def _extract_js_structure(content: str, max_size: int = 0) -> str:
    """Extract JS/TS class/function/export signatures using regex."""
    lines = content.split("\n")
    sections: list[str] = []

    # Imports
    import_lines = [l for l in lines if re.match(r"^\s*import\s+", l)]
    if import_lines:
        sections.append("// --- Imports ---")
        sections.extend(import_lines[:50])
        sections.append("")

    # Exports, classes, functions, interfaces, types
    patterns = [
        re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+"),
        re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+"),
        re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+\w+\s*[=:]"),
        re.compile(r"^\s*(?:export\s+)?interface\s+"),
        re.compile(r"^\s*(?:export\s+)?type\s+\w+"),
        re.compile(r"^\s*(?:export\s+)?enum\s+"),
        re.compile(r"^\s*module\.exports\s*="),
    ]

    for i, line in enumerate(lines):
        for pat in patterns:
            if pat.match(line):
                # Include up to next empty line or closing brace (max 5 lines for signature)
                sig_lines = [line]
                for j in range(i + 1, min(i + 5, len(lines))):
                    if lines[j].strip() == "" or lines[j].strip().startswith("//"):
                        break
                    if lines[j].strip() in ("{", "}", ");"):
                        sig_lines.append(lines[j])
                        break
                    sig_lines.append(lines[j])
                sections.extend(sig_lines)
                sections.append("  // ...")
                sections.append("")
                break

    result = "\n".join(sections)
    total = len(lines)
    result += f"\n\n// [Structure extracted: {total} lines total, full impl omitted]"
    return result


def _extract_c_family_structure(content: str, language: str, max_size: int = 0) -> str:
    """Extract C/Go/Rust/Java structure using regex heuristics."""
    lines = content.split("\n")
    sections: list[str] = []

    # Language-specific patterns
    if language == "go":
        patterns = [
            re.compile(r"^(?:func|type|var|const|import)\s"),
            re.compile(r"^\s*//"),
        ]
    elif language == "rust":
        patterns = [
            re.compile(r"^(?:pub\s+)?(?:fn|struct|enum|trait|impl|mod|use|type|const|static)\s"),
        ]
    elif language == "java":
        patterns = [
            re.compile(r"^\s*(?:public|private|protected|static|final|abstract)\s"),
            re.compile(r"^\s*(?:import|package)\s"),
            re.compile(r"^\s*(?:class|interface|enum|@)\s"),
        ]
    else:  # C/C++
        patterns = [
            re.compile(r"^(?:#include|#define|#ifndef|#ifdef|typedef|struct|class|enum|namespace)\s"),
            re.compile(r"^(?:(?:static|extern|const|inline|virtual)\s+)*\w+[\s*&]+\w+\s*\("),
        ]

    for i, line in enumerate(lines):
        for pat in patterns:
            if pat.match(line):
                # Include signature (up to opening brace)
                sig = [line]
                for j in range(i + 1, min(i + 5, len(lines))):
                    l = lines[j].strip()
                    if l == "{" or l == "" or l.startswith("//"):
                        break
                    sig.append(lines[j])
                sections.extend(sig)
                sections.append("  // ...")
                sections.append("")
                break

    result = "\n".join(sections)
    total = len(lines)
    result += f"\n\n// [Structure extracted: {total} lines total]"
    return result


def _extract_head_tail(content: str, budget: int) -> str:
    """Fallback: keep head and tail of file."""
    if len(content) <= budget:
        return content
    head_size = int(budget * 0.7)
    tail_size = budget - head_size
    lines = content.split("\n")
    total = len(lines)

    head_lines = []
    chars = 0
    for line in lines:
        if chars + len(line) > head_size:
            break
        head_lines.append(line)
        chars += len(line) + 1

    tail_lines = []
    chars = 0
    for line in reversed(lines):
        if chars + len(line) > tail_size:
            break
        tail_lines.insert(0, line)
        chars += len(line) + 1

    omitted = total - len(head_lines) - len(tail_lines)
    return (
        "\n".join(head_lines)
        + f"\n\n... [{omitted} lines omitted] ...\n\n"
        + "\n".join(tail_lines)
    )


# ── Directory listing (for agent tool calls) ─────────────────────


def list_directory(
    root: Path,
    relative_dir: str = "",
    max_depth: int = 3,
    max_entries: int = 200,
) -> str:
    """List files in a directory for agent tool-based exploration."""
    root_resolved = root.resolve()
    target = (root_resolved / relative_dir) if relative_dir else root_resolved
    target = target.resolve()
    try:
        target.relative_to(root_resolved)
    except ValueError:
        return "ERROR: Path escapes project root"

    if not target.is_dir():
        return f"ERROR: Not a directory: {relative_dir or '.'}"

    lines: list[str] = []
    count = 0

    def _walk(dir_path: Path, depth: int, prefix: str = "") -> None:
        nonlocal count
        if depth > max_depth or count >= max_entries:
            return

        try:
            entries = sorted(dir_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            return

        for entry in entries:
            if count >= max_entries:
                break
            if entry.name in SKIP_DIRS:
                continue

            try:
                rel = str(entry.resolve().relative_to(root_resolved)).replace("\\", "/")
            except ValueError:
                continue  # skip entries outside root

            if entry.is_dir():
                lines.append(f"{prefix}[DIR] {rel}/")
                count += 1
                _walk(entry, depth + 1, prefix + "  ")
            elif entry.is_file():
                try:
                    size = entry.stat().st_size
                    lines.append(f"{prefix}      {rel} ({size:,}b)")
                except OSError:
                    lines.append(f"{prefix}      {rel}")
                count += 1

    _walk(target, 0)

    if count >= max_entries:
        lines.append(f"\n[Listing truncated at {max_entries} entries]")

    return "\n".join(lines) if lines else "(empty directory)"
