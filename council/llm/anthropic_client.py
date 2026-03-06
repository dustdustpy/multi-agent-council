"""Anthropic LLM client with prompt caching, streaming, and progress reporting."""
from __future__ import annotations

import logging
import time

import httpx

from .base import BaseLLMClient, StreamProgressCB
from ..models import LLMResponse

log = logging.getLogger("council.llm.anthropic")

# Report streaming progress every N seconds
_PROGRESS_INTERVAL = 3.0


class AnthropicClient(BaseLLMClient):
    def __init__(self, base_url: str, api_key: str, http_client: httpx.AsyncClient | None = None):
        from anthropic import AsyncAnthropic
        kwargs: dict = dict(base_url=base_url, api_key=api_key)
        if http_client is not None:
            kwargs["http_client"] = http_client
        self.client = AsyncAnthropic(**kwargs)

    async def generate(
        self,
        model: str,
        system: str,
        messages: list[dict],
        max_tokens: int = 4096,
        thinking_budget: int = 0,
        on_progress: StreamProgressCB = None,
    ) -> LLMResponse:
        # System with prompt caching (cache_control on system block)
        system_blocks = [
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }
        ]

        # Cache large user messages (file content > 3KB for better hit rate)
        cached_messages = []
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > 3_000:
                cached_messages.append({
                    "role": msg["role"],
                    "content": [
                        {
                            "type": "text",
                            "text": content,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                })
            else:
                cached_messages.append(msg)

        kwargs: dict = dict(
            model=model,
            system=system_blocks,
            messages=cached_messages,
            max_tokens=max_tokens,
        )
        if thinking_budget > 0:
            kwargs["thinking"] = {"type": "adaptive", "budget_tokens": thinking_budget}

        # MUST use streaming — non-streaming fails with "Streaming is required
        # for operations that may take longer than 10 minutes" on large contexts
        chars_received = 0
        last_progress = time.monotonic()

        async with self.client.messages.stream(**kwargs) as stream:
            async for event in stream:
                # Track streaming progress for reporting
                if on_progress:
                    delta = getattr(event, "delta", None)
                    if delta:
                        text = getattr(delta, "text", "") or ""
                        thinking = getattr(delta, "thinking", "") or ""
                        added = len(text) + len(thinking)
                        if added:
                            chars_received += added
                            now = time.monotonic()
                            if now - last_progress >= _PROGRESS_INTERVAL:
                                try:
                                    await on_progress(chars_received)
                                except Exception:
                                    pass
                                last_progress = now

            resp = await stream.get_final_message()

        # Final progress report
        if on_progress and chars_received > 0:
            try:
                await on_progress(chars_received)
            except Exception:
                pass

        thinking_text = ""
        content_text = ""
        if resp.content:
            for block in resp.content:
                bt = getattr(block, "type", "")
                if bt == "thinking":
                    thinking_text += getattr(block, "thinking", "")
                elif bt == "text":
                    content_text += getattr(block, "text", "")
            if not content_text:
                for block in resp.content:
                    if hasattr(block, "text") and getattr(block, "type", "") != "thinking":
                        content_text = block.text
                        break

        usage = resp.usage
        return LLMResponse(
            content=content_text,
            thinking=thinking_text,
            model=resp.model,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cached_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        )

    async def close(self) -> None:
        await self.client.close()
