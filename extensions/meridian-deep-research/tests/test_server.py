"""Regression tests for meridian_deep_research.server (item da8fbeec).

These tests need the real ``open_deep_research`` package importable (it is a
declared dependency of this package, installed whenever
``meridian-deep-research-mcp`` itself is installed -- e.g. via the repo's
opt-in ``deep_research`` pixi feature, or a standalone ``pip install -e
extensions/meridian-deep-research``) so the acceptance-criterion assertions
below are checked against the REAL ``Configuration``/``SearchAPI`` classes,
not a hand-rolled stand-in. ``pytest.importorskip`` below makes this file
skip cleanly (never fail) if that optional dependency genuinely isn't
installed in whatever environment collected it -- e.g. the parent repo's
default ``pixi run test`` sweep, which deliberately does NOT install this
extension's heavy dependency tree (see root pixi.toml's
``[feature.deep_research]``).

THE CORE ACCEPTANCE CRITERION (item da8fbeec, section (d)): the wrapper must
never depend on OPENAI_API_KEY/TAVILY_API_KEY -- only ANTHROPIC_API_KEY.
``test_deep_research_completes_with_only_anthropic_api_key_set`` is the test
that proves it: it explicitly deletes OPENAI_API_KEY/TAVILY_API_KEY from the
environment (never assumes their absence), sets only a fake
ANTHROPIC_API_KEY, mocks ``deep_researcher.ainvoke`` (no live network call,
no real tokens spent) and asserts the ``Configuration`` actually passed to it
is Anthropic-only.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("open_deep_research")

from meridian_deep_research import server  # noqa: E402


# ---------------------------------------------------------------------------
# get_deep_research_status -- no network, no API key required.
# ---------------------------------------------------------------------------


def test_status_reports_available_when_package_importable():
    status = server.get_deep_research_status()
    assert status["available"] is True
    assert status["defaults"]["search_api"] == "anthropic"
    assert status["defaults"]["research_model"].startswith("anthropic:")
    assert status["defaults"]["summarization_model"].startswith("anthropic:")
    assert status["defaults"]["compression_model"].startswith("anthropic:")
    assert status["defaults"]["final_report_model"].startswith("anthropic:")


def test_status_reports_not_ready_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    status = server.get_deep_research_status()
    assert status["api_key_configured"] is False
    assert status["ready"] is False


def test_status_reports_ready_with_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
    status = server.get_deep_research_status()
    assert status["api_key_configured"] is True
    assert status["ready"] is True


# ---------------------------------------------------------------------------
# _build_configuration -- the acceptance-criterion seam (module docstring's
# CRITICAL section): every call path MUST go through this, and it MUST always
# produce search_api="anthropic" + all four model fields anthropic:claude-*,
# regardless of upstream's own tavily/openai defaults.
# ---------------------------------------------------------------------------


def test_build_configuration_defaults_are_anthropic_only():
    configuration = server._build_configuration()
    assert configuration.search_api == server.SearchAPI.ANTHROPIC
    assert configuration.search_api.value == "anthropic"
    assert configuration.research_model.startswith("anthropic:")
    assert configuration.summarization_model.startswith("anthropic:")
    assert configuration.compression_model.startswith("anthropic:")
    assert configuration.final_report_model.startswith("anthropic:")
    # Never upstream's own tavily/openai defaults, under any circumstance.
    assert configuration.search_api != server.SearchAPI.TAVILY
    assert "openai:" not in configuration.research_model
    assert "openai:" not in configuration.summarization_model
    assert "openai:" not in configuration.compression_model
    assert "openai:" not in configuration.final_report_model


def test_build_configuration_honors_explicit_model_overrides():
    configuration = server._build_configuration(
        research_model="anthropic:claude-opus-5",
        max_concurrent_research_units=2,
        max_researcher_iterations=1,
    )
    assert configuration.research_model == "anthropic:claude-opus-5"
    assert configuration.search_api == server.SearchAPI.ANTHROPIC
    assert configuration.max_concurrent_research_units == 2
    assert configuration.max_researcher_iterations == 1
    # Overrides are per-field -- the three fields not overridden still fall
    # back to this wrapper's Anthropic-only defaults, never upstream's.
    assert configuration.summarization_model.startswith("anthropic:")
    assert configuration.compression_model.startswith("anthropic:")
    assert configuration.final_report_model.startswith("anthropic:")


# ---------------------------------------------------------------------------
# deep_research -- the real tool. Graceful-degrade paths never touch the
# network; the success/failure paths mock deep_researcher.ainvoke so this
# test file never makes a live call or spends real tokens.
# ---------------------------------------------------------------------------


def test_deep_research_degrades_gracefully_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = asyncio.run(server.deep_research("what is the capital of France?"))
    assert result["ok"] is False
    assert result["api_key_configured"] is False
    assert "ANTHROPIC_API_KEY" in result["error"]


def test_deep_research_degrades_gracefully_when_package_unavailable(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
    monkeypatch.setattr(server, "Configuration", None)
    monkeypatch.setattr(server, "deep_researcher", None)
    result = asyncio.run(server.deep_research("what is the capital of France?"))
    assert result["ok"] is False
    assert result["available"] is False


def test_deep_research_completes_with_only_anthropic_api_key_set(monkeypatch):
    """THE acceptance-criterion test for item da8fbeec section (d).

    Explicitly removes OPENAI_API_KEY/TAVILY_API_KEY (never assumes they're
    absent), sets only a fake ANTHROPIC_API_KEY, mocks
    ``deep_researcher.ainvoke`` so no real network call is made, and asserts
    the tool completes successfully with a Configuration that never touched
    OpenAI or Tavily.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")

    fake_result = {
        "final_report": "# Fake report\n\nParis is the capital of France.",
        "notes": ["Paris is the capital of France."],
        "raw_notes": ["raw note 1", "raw note 2"],
    }
    mock_ainvoke = AsyncMock(return_value=fake_result)
    # Replace the module-level deep_researcher wholesale with a stand-in
    # exposing only .ainvoke, rather than mutating an attribute on the real
    # compiled LangGraph graph object (whose class may not support arbitrary
    # instance attribute assignment) -- equally valid here since the
    # assertion below is entirely about the *arguments* dispatched to
    # ainvoke, not about langgraph's own internals.
    monkeypatch.setattr(server, "deep_researcher", SimpleNamespace(ainvoke=mock_ainvoke))

    result = asyncio.run(server.deep_research("what is the capital of France?"))

    assert result["ok"] is True
    assert result["final_report"] == fake_result["final_report"]
    assert result["raw_notes_count"] == 2

    # The wrapper never read OPENAI_API_KEY/TAVILY_API_KEY to get here.
    import os

    assert "OPENAI_API_KEY" not in os.environ
    assert "TAVILY_API_KEY" not in os.environ

    # And the Configuration actually dispatched to ainvoke is Anthropic-only.
    mock_ainvoke.assert_awaited_once()
    call_args, call_kwargs = mock_ainvoke.call_args
    dispatched_input = call_args[0]
    assert dispatched_input == {
        "messages": [{"role": "user", "content": "what is the capital of France?"}]
    }
    dispatched_configurable = call_kwargs["config"]["configurable"]
    assert dispatched_configurable["search_api"] == "anthropic"
    assert dispatched_configurable["research_model"].startswith("anthropic:")
    assert dispatched_configurable["summarization_model"].startswith("anthropic:")
    assert dispatched_configurable["compression_model"].startswith("anthropic:")
    assert dispatched_configurable["final_report_model"].startswith("anthropic:")
    # Also echoed back on the tool's own response for the caller to confirm.
    assert result["configuration"]["search_api"] == "anthropic"


def test_deep_research_reports_call_failures_without_raising(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-test-key")
    mock_ainvoke = AsyncMock(side_effect=RuntimeError("simulated Anthropic API error"))
    monkeypatch.setattr(server, "deep_researcher", SimpleNamespace(ainvoke=mock_ainvoke))

    result = asyncio.run(server.deep_research("a query that will fail"))

    assert result["ok"] is False
    assert result["error_type"] == "RuntimeError"
    assert "simulated Anthropic API error" in result["error"]
