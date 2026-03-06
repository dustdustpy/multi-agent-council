"""OpenAI-compatible LLM client with streaming, auto-detection, and progress reporting."""
from __future__ import annotations

import logging
import time

import httpx

from .base import BaseLLMClient, StreamProgressCB
from ..models import LLMResponse

log = logging.getLogger("council.llm.openai")

# Report streaming progress every N seconds
_PROGRESS_INTERVAL = 3.0


class OpenAIClient(BaseLLMClient):
    def __init__(self, base_url: str, api_key: str, http_client: httpx.AsyncClient | None = None):
        from openai import AsyncOpenAI
        kwargs: dict = dict(base_url=base_url, api_key=api_key)
        if http_client is not None:
            kwargs["http_client"] = http_client
        self.client = AsyncOpenAI(**kwargs)
        self._streaming_supported: bool | None = None  # auto-detect

    async def generate(
        self,
        model: str,
        system: str,
        messages: list[dict],
        max_tokens: int = 4096,
        thinking_budget: int = 0,
        on_progress: StreamProgressCB = None,
    ) -> LLMResponse:
        oai_msgs = [{"role": "system", "content": system}] + messages

        # Try streaming first (better for large contexts, avoids timeout)
        if self._streaming_supported is not False:
            try:
                return await self._generate_stream(model, oai_msgs, max_tokens, on_progress)
            except Exception as e:
                err_msg = str(e).lower()
                if any(kw in err_msg for kw in ("stream", "not support", "invalid param")):
                    log.warning("Streaming not supported for %s, falling back to sync: %s", model, e)
                    self._streaming_supported = False
                else:
                    raise

        return await self._generate_sync(model, oai_msgs, max_tokens)

    async def _generate_stream(
        self, model: str, messages: list[dict], max_tokens: int,
        on_progress: StreamProgressCB = None,
    ) -> LLMResponse:
        """Stream response chunks for better timeout handling on large contexts."""
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        model_name = model
        input_tokens = 0
        output_tokens = 0
        chars_received = 0
        last_progress = time.monotonic()

        try:
            stream = await self.client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                stream=True,
                stream_options={"include_usage": True},
            )
        except Exception:
            # stream_options might not be supported — retry without it
            stream = await self.client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                stream=True,
            )

        async for chunk in stream:
            if not chunk.choices:
                # Final usage chunk
                if hasattr(chunk, "usage") and chunk.usage:
                    input_tokens = getattr(chunk.usage, "prompt_tokens", 0) or 0
                    output_tokens = getattr(chunk.usage, "completion_tokens", 0) or 0
                if hasattr(chunk, "model") and chunk.model:
                    model_name = chunk.model
                continue

            delta = chunk.choices[0].delta
            if delta:
                if delta.content:
                    content_parts.append(delta.content)
                    chars_received += len(delta.content)
                # Reasoning content (DeepSeek, Qwen, etc.)
                rc = getattr(delta, "reasoning_content", None)
                if rc:
                    reasoning_parts.append(rc)
                    chars_received += len(rc)

            if hasattr(chunk, "model") and chunk.model:
                model_name = chunk.model

            # Report streaming progress
            if on_progress and chars_received > 0:
                now = time.monotonic()
                if now - last_progress >= _PROGRESS_INTERVAL:
                    try:
                        await on_progress(chars_received)
                    except Exception:
                        pass
                    last_progress = now

        self._streaming_supported = True
        content = "".join(content_parts)
        reasoning = "".join(reasoning_parts)

        # Final progress report
        if on_progress and chars_received > 0:
            try:
                await on_progress(chars_received)
            except Exception:
                pass

        # Estimate tokens if streaming didn't provide usage
        if input_tokens == 0:
            total_text = " ".join(
                m.get("content", "") if isinstance(m.get("content"), str)
                else str(m.get("content", ""))
                for m in messages
            )
            input_tokens = int(len(total_text) / 3.5)
        if output_tokens == 0:
            output_tokens = int(len(content + reasoning) / 3.5)

        return LLMResponse(
            content=content,
            thinking=reasoning,
            model=model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    async def _generate_sync(
        self, model: str, messages: list[dict], max_tokens: int,
    ) -> LLMResponse:
        """Non-streaming fallback for proxies that don't support streaming."""
        resp = await self.client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
        )
        choice = resp.choices[0]
        return LLMResponse(
            content=choice.message.content or "",
            thinking=getattr(choice.message, "reasoning_content", "") or "",
            model=resp.model or model,
            input_tokens=(getattr(resp.usage, "prompt_tokens", 0) or 0) if resp.usage else 0,
            output_tokens=(getattr(resp.usage, "completion_tokens", 0) or 0) if resp.usage else 0,
        )

    async def close(self) -> None:
        await self.client.close()
