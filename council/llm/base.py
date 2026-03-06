"""Abstract base class for LLM clients."""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Callable, Awaitable, Optional

from ..models import LLMResponse

# Callback: (output_chars_received_so_far) -> None
StreamProgressCB = Optional[Callable[[int], Awaitable[None]]]


class BaseLLMClient(ABC):
    """Interface that all LLM providers must implement."""

    @abstractmethod
    async def generate(
        self,
        model: str,
        system: str,
        messages: list[dict],
        max_tokens: int = 4096,
        thinking_budget: int = 0,
        on_progress: StreamProgressCB = None,
    ) -> LLMResponse:
        ...

    async def close(self) -> None:
        """Clean up HTTP connections."""
