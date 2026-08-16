"""Unit tests for Knowledge Domain Prompts and System Prompt Composition."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.gateway.application.services.chat_orchestrator import ChatOrchestratorService
from src.gateway.config import GatewaySettings, get_settings
from src.gateway.domain.canonical import CanonicalChatRequest, CanonicalMessage
from src.gateway.domain.prompts import (
    DEFAULT_KNOWLEDGE_SYSTEM_PROMPT,
    compose_system_prompt,
)


def test_compose_system_prompt_default_when_no_client_prompt():
    """Verify compose_system_prompt returns the default knowledge directive when client prompt is None or empty."""
    prompt = compose_system_prompt(client_system_prompt=None, enabled=True)
    assert prompt is not None
    assert "Long-Term Knowledge Base and Project Memory" in prompt
    assert "knowledge_search" in prompt
    assert "knowledge_save" in prompt

    prompt_empty = compose_system_prompt(client_system_prompt="   ", enabled=True)
    assert prompt_empty == DEFAULT_KNOWLEDGE_SYSTEM_PROMPT.strip()


def test_compose_system_prompt_appends_to_client_prompt():
    """Verify compose_system_prompt appends the directive after the client's system prompt."""
    client_prompt = "You are an expert Python developer assistant."
    composed = compose_system_prompt(client_system_prompt=client_prompt, enabled=True)

    assert composed is not None
    assert composed.startswith("You are an expert Python developer assistant.")
    assert "\n\n---\n\n" in composed
    assert "Long-Term Knowledge Base and Project Memory" in composed


def test_compose_system_prompt_disabled():
    """Verify compose_system_prompt returns only client prompt (or None) when disabled."""
    client_prompt = "Custom system instructions"
    assert compose_system_prompt(client_system_prompt=client_prompt, enabled=False) == client_prompt
    assert compose_system_prompt(client_system_prompt=None, enabled=False) is None
    assert compose_system_prompt(client_system_prompt="", enabled=False) is None


def test_compose_system_prompt_custom_override():
    """Verify compose_system_prompt uses custom prompt when provided."""
    custom_directive = "CUSTOM DIRECTIVE: Always search knowledge first."
    composed = compose_system_prompt(
        client_system_prompt="Client prompt",
        enabled=True,
        custom_prompt=custom_directive,
    )
    assert "CUSTOM DIRECTIVE" in composed
    assert "Long-Term Knowledge Base and Project Memory" not in composed
    assert composed.startswith("Client prompt\n\n---\n\nCUSTOM DIRECTIVE")


def test_orchestrator_prepares_enriched_system_prompt_from_request():
    """Verify ChatOrchestratorService enriches system_prompt from CanonicalChatRequest."""
    llm_client = MagicMock()
    orchestrator = ChatOrchestratorService(llm_client=llm_client)

    messages = [CanonicalMessage(role="user", content="How do I configure the database?")]
    upstream = orchestrator._prepare_upstream_messages(
        messages=messages,
        system_prompt="You are a helpful coding assistant.",
    )

    assert len(upstream) == 2
    assert upstream[0]["role"] == "system"
    assert "You are a helpful coding assistant." in upstream[0]["content"]
    assert "Long-Term Knowledge Base and Project Memory" in upstream[0]["content"]
    assert upstream[1]["role"] == "user"
    assert upstream[1]["content"] == "How do I configure the database?"


def test_orchestrator_prepares_enriched_system_prompt_from_system_message():
    """Verify ChatOrchestratorService enriches system message when provided in messages list."""
    llm_client = MagicMock()
    orchestrator = ChatOrchestratorService(llm_client=llm_client)

    messages = [
        CanonicalMessage(role="system", content="System base rule."),
        CanonicalMessage(role="user", content="Hello"),
    ]
    upstream = orchestrator._prepare_upstream_messages(messages=messages)

    assert len(upstream) == 2
    assert upstream[0]["role"] == "system"
    assert "System base rule." in upstream[0]["content"]
    assert "Long-Term Knowledge Base and Project Memory" in upstream[0]["content"]
    assert upstream[1]["role"] == "user"


def test_orchestrator_system_prompt_when_disabled_in_settings():
    """Verify ChatOrchestratorService honors knowledge_system_prompt_enabled=False."""
    llm_client = MagicMock()
    orchestrator = ChatOrchestratorService(llm_client=llm_client)

    with patch("src.gateway.application.services.chat_orchestrator.get_settings") as mock_settings:
        settings_instance = MagicMock()
        settings_instance.gateway.knowledge_system_prompt_enabled = False
        settings_instance.gateway.knowledge_system_prompt_custom = None
        mock_settings.return_value = settings_instance

        messages = [CanonicalMessage(role="user", content="Hello")]
        upstream = orchestrator._prepare_upstream_messages(
            messages=messages,
            system_prompt="Original prompt",
        )

        assert len(upstream) == 2
        assert upstream[0]["role"] == "system"
        assert upstream[0]["content"] == "Original prompt"
