from __future__ import annotations

import pytest
from praman.adapters.llm import FakeLLMClient, UnimplementedLLMClient, get_llm_client
from praman.config import Settings


def test_get_llm_client_returns_fake_for_fake_provider() -> None:
    settings = Settings(llm_provider="fake")
    client = get_llm_client(settings)
    assert isinstance(client, FakeLLMClient)


def test_get_llm_client_returns_unimplemented_for_unknown_provider() -> None:
    settings = Settings(llm_provider="some-future-provider")
    client = get_llm_client(settings)
    assert isinstance(client, UnimplementedLLMClient)


async def test_unimplemented_client_fails_loudly_not_silently() -> None:
    client = UnimplementedLLMClient("openai")
    with pytest.raises(NotImplementedError, match="openai"):
        await client.generate_json("prompt")


def test_get_llm_client_returns_gemini_for_gemini_provider() -> None:
    settings = Settings(llm_provider="gemini", llm_api_key="fake-key-for-constructor-only")
    client = get_llm_client(settings)
    assert type(client).__name__ == "GeminiLLMClient"
