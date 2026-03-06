"""Utility functions: JSON parsing, bool parsing, markdown escaping."""
from __future__ import annotations

import json
import re

from .exceptions import ParseError


def parse_bool(value: object) -> bool:
    """Parse boolean from LLM response, correctly handling string 'false'."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def escape_md_cell(text: str) -> str:
    """Escape characters that break markdown table cells."""
    return text.replace("|", "\\|").replace("\n", " ").strip()


def parse_json_response(text: str) -> dict | list:
    """Extract JSON from LLM response text.

    Tries in order:
    1. Direct JSON parse
    2. Markdown code block extraction
    3. Incremental bracket matching
    """
    text = text.strip()

    # 1. Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. Markdown code blocks (find all, try each)
    for m in re.finditer(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL):
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            continue

    # 3. Incremental bracket matching (non-greedy via JSONDecoder)
    decoder = json.JSONDecoder()
    for start_char in ("[", "{"):
        idx = text.find(start_char)
        while idx != -1:
            try:
                obj, end = decoder.raw_decode(text, idx)
                return obj
            except json.JSONDecodeError:
                idx = text.find(start_char, idx + 1)

    raise ParseError(f"Cannot parse JSON from response: {text[:300]}")
