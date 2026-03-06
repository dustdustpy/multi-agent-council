"""LLM client factory with caching, connection pooling, and circuit breaker."""
from __future__ import annotations

import logging

import httpx

from .base import BaseLLMClient
from .anthropic_client import AnthropicClient
from .openai_client import OpenAIClient
from .circuit_breaker import CircuitBreaker
from ..config import MemberConfig

log = logging.getLogger("council.llm.factory")

# Shared connection pool limits
_POOL_LIMITS = httpx.Limits(
    max_connections=30,
    max_keepalive_connections=15,
)
_TIMEOUT = httpx.Timeout(connect=15.0, read=300.0, write=30.0, pool=30.0)


class LLMClientFactory:
    """Creates and caches LLM clients per unique (format, base_url, api_key).

    Uses shared httpx.AsyncClient with connection pooling to reduce
    TLS handshake overhead and prevent socket exhaustion under concurrency.
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str, str], BaseLLMClient] = {}
        self._http_pools: dict[str, httpx.AsyncClient] = {}
        self.circuit_breaker = CircuitBreaker()

    def _get_http_client(self, base_url: str) -> httpx.AsyncClient:
        """Get or create a shared httpx client per base_url."""
        if base_url not in self._http_pools:
            self._http_pools[base_url] = httpx.AsyncClient(
                limits=_POOL_LIMITS,
                timeout=_TIMEOUT,
            )
            log.info("Created connection pool for %s", base_url)
        return self._http_pools[base_url]

    def get_client(self, member: MemberConfig) -> BaseLLMClient:
        key = (member.format, member.resolved_base_url(), member.resolved_api_key())
        if key not in self._cache:
            base_url = member.resolved_base_url()
            api_key = member.resolved_api_key()
            http_client = self._get_http_client(base_url)

            if member.format == "anthropic":
                self._cache[key] = AnthropicClient(
                    base_url=base_url,
                    api_key=api_key,
                    http_client=http_client,
                )
            elif member.format == "openai":
                self._cache[key] = OpenAIClient(
                    base_url=base_url,
                    api_key=api_key,
                    http_client=http_client,
                )
            else:
                raise ValueError(f"Unsupported format: {member.format}")
            log.info("Created %s client for %s", member.format, member.model)
        return self._cache[key]

    def endpoint_key(self, member: MemberConfig) -> str:
        return f"{member.format}:{member.resolved_base_url()}:{member.model}"

    async def close_all(self) -> None:
        for client in self._cache.values():
            try:
                await client.close()
            except Exception:
                pass
        self._cache.clear()
        for pool in self._http_pools.values():
            try:
                await pool.aclose()
            except Exception:
                pass
        self._http_pools.clear()
