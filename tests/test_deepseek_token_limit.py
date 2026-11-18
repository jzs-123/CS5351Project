"""DeepSeek output-token limit contract.

DeepSeek's Chat Completions API documents ``max_tokens`` as the output cap
(api-docs.deepseek.com/api/create-chat-completion). LangChain's ChatOpenAI now
renames the configured ``max_tokens`` to ``max_completion_tokens`` in the request
payload, which DeepSeek does not document, so the limit would be ignored. The
DeepSeek subclass maps it back to ``max_tokens`` — and nothing else changes.

These tests inspect the *generated request payload* without sending a request.
The api key is an explicit dummy, never a real credential, and the base URL points
at a localhost placeholder that is never contacted.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from tradingagents.llm_clients.openai_client import (
    DeepSeekChatOpenAI,
    NormalizedChatOpenAI,
)

# Explicitly not a real key / endpoint; `_get_request_payload` performs no I/O.
_DUMMY = {"api_key": "dummy-not-a-real-key", "base_url": "http://localhost:9/v1"}


def _deepseek(**kwargs):
    return DeepSeekChatOpenAI(model="deepseek-v4-flash", **_DUMMY, **kwargs)


def _normalized(**kwargs):
    return NormalizedChatOpenAI(model="gpt-4.1", **_DUMMY, **kwargs)


_MESSAGES = [HumanMessage(content="Rate NVDA.")]


@pytest.mark.unit
def test_configured_limit_is_sent_as_max_tokens():
    payload = _deepseek(max_tokens=2048)._get_request_payload(_MESSAGES)
    assert payload.get("max_tokens") == 2048
    assert "max_completion_tokens" not in payload


@pytest.mark.unit
def test_per_invocation_max_tokens_override_wins():
    client = _deepseek(max_tokens=2048)
    payload = client._get_request_payload(_MESSAGES, max_tokens=512)
    assert payload.get("max_tokens") == 512
    assert "max_completion_tokens" not in payload


@pytest.mark.unit
def test_per_invocation_max_completion_tokens_ends_as_max_tokens():
    # A caller (or LangChain binding) that uses the modern name still reaches the
    # native DeepSeek key with the same value.
    client = _deepseek(max_tokens=2048)
    payload = client._get_request_payload(_MESSAGES, max_completion_tokens=800)
    assert payload.get("max_tokens") == 800
    assert "max_completion_tokens" not in payload


@pytest.mark.unit
def test_unset_limit_stays_unset():
    payload = _deepseek()._get_request_payload(_MESSAGES)
    assert "max_tokens" not in payload
    assert "max_completion_tokens" not in payload


@pytest.mark.unit
def test_reasoning_content_survives_with_limit_and_inputs_not_mutated():
    client = _deepseek(max_tokens=2048)
    prior = AIMessage(content="Plan", additional_kwargs={"reasoning_content": "weighed bull case"})
    messages = [prior, HumanMessage(content="Refine.")]
    snapshot_reasoning = prior.additional_kwargs["reasoning_content"]
    snapshot_content = prior.content

    payload = client._get_request_payload(messages)

    assistant = [m for m in payload["messages"] if m.get("role") == "assistant"]
    assert assistant and assistant[0]["reasoning_content"] == "weighed bull case"
    assert payload.get("max_tokens") == 2048 and "max_completion_tokens" not in payload
    # The input messages are not mutated by payload construction.
    assert prior.additional_kwargs["reasoning_content"] == snapshot_reasoning
    assert prior.content == snapshot_content


@pytest.mark.unit
def test_normalized_openai_keeps_modern_key():
    # Subclass-only fix: the base OpenAI-compatible client is unchanged. With the
    # current LangChain it still emits the modern ``max_completion_tokens`` name; the
    # assertion also tolerates a version that keeps ``max_tokens`` so this proves
    # scope, not a specific SDK version.
    payload = _normalized(max_tokens=2048)._get_request_payload(_MESSAGES)
    if "max_completion_tokens" in payload:
        assert payload["max_completion_tokens"] == 2048
        assert "max_tokens" not in payload
    else:
        assert payload.get("max_tokens") == 2048
