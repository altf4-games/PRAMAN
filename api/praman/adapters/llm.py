"""LLM adapter. `LLMClient` is a Protocol so `ingest/extract.py` and (later)
`core/substitution.py`'s ranking step never import a provider SDK directly —
swapping Gemini for OpenAI or Anthropic, or for a fake in tests, is a
one-line change at `get_llm_client()`.

Non-negotiable: nothing in `core/gate.py`, `core/envelope.py`, or
`core/reversibility.py` may import this module. LLM calls are confined to
catalog extraction (offline/inbound ingest) and substitution ranking
(post-filter, non-load-bearing) — see the design spec §0.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol, cast

from praman.config import Settings

_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE_S = 2.0


class LLMClient(Protocol):
    """One generic method. Both catalog extraction and substitution-ranking
    ask the model for JSON text and parse it themselves — the adapter's job
    is only to get bytes to the provider and text back, not to know about
    `ExtractedProduct` or any other domain shape.
    """

    async def generate_json(
        self,
        prompt: str,
        *,
        image_bytes: bytes | None = None,
        mime_type: str | None = None,
    ) -> str:
        """Returns the model's raw text response. Callers are responsible
        for parsing it as JSON and handling malformed output — this method
        raises only on transport/auth failure, never on bad model output.
        """
        ...


class GeminiLLMClient:
    """Wraps `google-genai` against the Gemini API."""

    def __init__(self, api_key: str, model: str) -> None:
        from google import genai  # local import: keep the SDK optional until used

        self._client = genai.Client(api_key=api_key)
        self._model = model

    async def generate_json(
        self,
        prompt: str,
        *,
        image_bytes: bytes | None = None,
        mime_type: str | None = None,
    ) -> str:
        from google.genai import errors, types

        contents: list[str | types.Part] = [prompt]
        if image_bytes is not None:
            contents.append(
                types.Part.from_bytes(data=image_bytes, mime_type=mime_type or "image/png")
            )

        last_error: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                response = await self._client.aio.models.generate_content(
                    model=self._model,
                    contents=cast(Any, contents),
                    config=types.GenerateContentConfig(response_mime_type="application/json"),
                )
                return response.text or ""
            except errors.ServerError as exc:
                # Transient 5xx (e.g. "high demand") — retry with backoff.
                # A 4xx (bad key, bad request) is not retried; it re-raises.
                last_error = exc
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(_RETRY_BACKOFF_BASE_S * (2**attempt))
        assert last_error is not None
        raise last_error


class FakeLLMClient:
    """Deterministic stand-in for tests and offline CI. Returns whatever
    canned response was registered for a prompt/image combination, keyed by
    insertion order — call `enqueue()` before the code under test runs.
    """

    def __init__(self) -> None:
        self._queue: list[str] = []
        self.calls: list[tuple[str, bytes | None, str | None]] = []

    def enqueue(self, response_json: str) -> None:
        self._queue.append(response_json)

    async def generate_json(
        self,
        prompt: str,
        *,
        image_bytes: bytes | None = None,
        mime_type: str | None = None,
    ) -> str:
        self.calls.append((prompt, image_bytes, mime_type))
        if not self._queue:
            raise RuntimeError("FakeLLMClient: no canned response enqueued")
        return self._queue.pop(0)


class UnimplementedLLMClient:
    """Placeholder for a provider that's been selected but not wired up yet
    — fails loudly and specifically rather than silently falling back."""

    def __init__(self, provider: str) -> None:
        self._provider = provider

    async def generate_json(
        self,
        prompt: str,
        *,
        image_bytes: bytes | None = None,
        mime_type: str | None = None,
    ) -> str:
        raise NotImplementedError(
            f"LLM_PROVIDER={self._provider!r} has no adapter implementation yet. "
            "Implement it in adapters/llm.py and wire it into get_llm_client(), "
            "or set LLM_PROVIDER=gemini/fake."
        )


def get_llm_client(settings: Settings) -> LLMClient:
    """Factory selecting the adapter named by `settings.llm_provider`. This
    is the one place that needs to change to add or swap a provider."""
    match settings.llm_provider:
        case "gemini":
            return GeminiLLMClient(api_key=settings.llm_api_key, model=settings.gemini_model)
        case "fake":
            return FakeLLMClient()
        case other:
            return UnimplementedLLMClient(other)
