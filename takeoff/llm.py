"""Takeoff-only LLM provider — self-contained, no core/ dependency.

Slim extraction from core/llm.py with only the API-mode code paths
needed for takeoff operations.
"""

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import anthropic

from takeoff.settings import API_CONFIG

logger = logging.getLogger(__name__)


# ─── Exceptions ───────────────────────────────────────────────────────────────

class LLMTimeoutException(Exception):
    """Raised when an LLM API call times out."""
    pass


# ─── Response Dataclass ──────────────────────────────────────────────────────

@dataclass
class LLMResponse:
    """Container for LLM responses."""
    content: str
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    cached: bool = False
    latency_ms: int = 0
    metadata: dict = field(default_factory=dict)


# ─── Cost Constants ───────────────────────────────────────────────────────────

COST_PER_1K = {
    "claude-haiku-4-5-20251001": {"input": 0.001, "output": 0.005},
    "claude-sonnet-4-20250514": {"input": 0.003, "output": 0.015},
}


# ─── LLM Provider ────────────────────────────────────────────────────────────

class LLMProvider:
    """Anthropic API provider for takeoff operations."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        mode: str = "api",
        cache_enabled: bool = True,
    ):
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")
        self.mode = mode
        self.cache_enabled = cache_enabled

        # Response cache
        self._cache: dict = {}

        # Rate limiting
        self._last_call_time = 0.0
        self._rate_limit_seconds = API_CONFIG.get("rate_limit_seconds", 1.0)
        self._rate_limit_enabled = API_CONFIG.get("rate_limit_enabled", True)

        # Cost tracking
        self.total_cost_usd = 0.0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.call_count = 0

        # Initialize Anthropic client
        if self.api_key:
            self.client = anthropic.Anthropic(api_key=self.api_key)
        else:
            self.client = None

    def _get_cache_key(self, system_prompt: str, user_prompt: str, model: str, temperature: float) -> str:
        """Generate a cache key for the request."""
        raw = f"{model}:{temperature}:{system_prompt}:{user_prompt}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _enforce_rate_limit(self):
        """Enforce rate limiting between API calls."""
        if not self._rate_limit_enabled:
            return
        elapsed = time.time() - self._last_call_time
        if elapsed < self._rate_limit_seconds:
            time.sleep(self._rate_limit_seconds - elapsed)
        self._last_call_time = time.time()

    def _calculate_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """Calculate cost for the API call."""
        costs = COST_PER_1K.get(model, {"input": 0.003, "output": 0.015})
        return (input_tokens / 1000 * costs["input"]) + (output_tokens / 1000 * costs["output"])

    def complete(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 4096,
        temperature: float = 0.4,
        model: Optional[str] = None,
        task_type: str = "general",
    ) -> LLMResponse:
        """Send a completion request to the Anthropic API.

        Args:
            system_prompt: System message
            user_prompt: User message
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature
            model: Model to use (defaults to config)
            task_type: Task type for logging

        Returns:
            LLMResponse with the completion
        """
        model = model or API_CONFIG.get("model", "claude-sonnet-4-20250514")

        # Check cache
        if self.cache_enabled:
            cache_key = self._get_cache_key(system_prompt, user_prompt, model, temperature)
            if cache_key in self._cache:
                cached = self._cache[cache_key]
                return LLMResponse(
                    content=cached["content"],
                    model=model,
                    cached=True,
                    metadata={"task_type": task_type},
                )

        if not self.client:
            return LLMResponse(
                content="[LLM ERROR: No API key configured]",
                model=model,
                metadata={"task_type": task_type, "error": "no_api_key"},
            )

        # Rate limit
        self._enforce_rate_limit()

        # API call with retry
        max_retries = 3
        for attempt in range(max_retries):
            try:
                start_time = time.time()
                response = self.client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_prompt}],
                )
                latency_ms = int((time.time() - start_time) * 1000)

                content = response.content[0].text if response.content else ""
                input_tokens = response.usage.input_tokens
                output_tokens = response.usage.output_tokens
                cost = self._calculate_cost(model, input_tokens, output_tokens)

                # Update tracking
                self.total_cost_usd += cost
                self.total_input_tokens += input_tokens
                self.total_output_tokens += output_tokens
                self.call_count += 1

                # Cache the response
                if self.cache_enabled:
                    self._cache[cache_key] = {"content": content}

                return LLMResponse(
                    content=content,
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost,
                    latency_ms=latency_ms,
                    metadata={"task_type": task_type},
                )

            except anthropic.RateLimitError:
                wait_time = 2 ** attempt
                logger.warning("[LLM] Rate limited, waiting %ds (attempt %d/%d)", wait_time, attempt + 1, max_retries)
                time.sleep(wait_time)

            except anthropic.APITimeoutError:
                if attempt < max_retries - 1:
                    logger.warning("[LLM] Timeout, retrying (attempt %d/%d)", attempt + 1, max_retries)
                    continue
                raise LLMTimeoutException(f"API timeout after {max_retries} retries")

            except Exception as e:
                if attempt < max_retries - 1:
                    logger.warning("[LLM] Error: %s, retrying (attempt %d/%d)", e, attempt + 1, max_retries)
                    time.sleep(1)
                    continue
                return LLMResponse(
                    content=f"[LLM ERROR: {e}]",
                    model=model,
                    metadata={"task_type": task_type, "error": str(e)},
                )

        return LLMResponse(
            content="[LLM ERROR: Max retries exceeded]",
            model=model,
            metadata={"task_type": task_type, "error": "max_retries"},
        )
