"""Tests for the kanban worker turn-end stop guard."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.kanban_stop import (
    build_kanban_stop_nudge,
    kanban_stop_nudge_enabled,
    session_called_kanban_terminal,
)


@pytest.fixture
def clear_kanban_env(monkeypatch):
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_STOP_NUDGE"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_env_can_disable(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    clear_kanban_env.setenv("HERMES_KANBAN_STOP_NUDGE", "0")
    assert kanban_stop_nudge_enabled() is False
    assert build_kanban_stop_nudge(messages=[]) is None


def test_nudge_disabled_inside_delegated_child(clear_kanban_env):
    from agent.delegation_context import delegated_child_context

    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_parent")

    assert kanban_stop_nudge_enabled() is True
    with delegated_child_context():
        assert kanban_stop_nudge_enabled() is False
        assert build_kanban_stop_nudge(messages=[]) is None
    assert kanban_stop_nudge_enabled() is True


def test_nudge_disabled_inside_non_dispatcher_context(clear_kanban_env):
    from agent.delegation_context import non_dispatcher_owned_context

    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_parent")

    assert kanban_stop_nudge_enabled() is True
    with non_dispatcher_owned_context():
        assert kanban_stop_nudge_enabled() is False
        assert build_kanban_stop_nudge(messages=[]) is None
    assert kanban_stop_nudge_enabled() is True


def test_nudge_when_no_terminal_tool(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_46be8aa5")
    messages = [
        {"role": "user", "content": "work kanban task"},
        {
            "role": "assistant",
            "content": "Let me write the comprehensive recipe.",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_heartbeat", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_heartbeat", "tool_call_id": "1", "content": "ok"},
    ]
    nudge = build_kanban_stop_nudge(messages=messages, attempts=0)
    assert nudge is not None
    assert "kanban_complete" in nudge
    assert "kanban_block" in nudge
    assert "t_46be8aa5" in nudge


def test_no_nudge_after_kanban_complete(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_complete", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_complete", "tool_call_id": "1", "content": "done"},
    ]
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None


# ── Integration: agent nudge + dispatcher bounded retry ──────────────
# These tests verify the two layers compose correctly: the agent-side
# nudge fires first (up to 2 attempts), and if the worker still exits
# without a terminal call, the dispatcher's bounded retry (streak of 3)
# handles it.  See also tests/hermes_cli/test_kanban_core_functionality.py
# for the dispatcher-side streak tests.


@pytest.mark.parametrize(
    "tool_name,who",
    [
        ("kanban_request_review", "build worker handing off for same-card review"),
        ("kanban_request_changes", "review agent sending the card back"),
    ],
)
def test_no_nudge_after_handoff_tool(clear_kanban_env, tool_name, who):
    """Handoff tools end the worker's turn just like complete/block.

    Both move the card out of ``running``, and the worker is told to call
    them — goals.py's continuation/finalize prompts name
    ``kanban_request_review``; the force-loaded sdlc-review skill names
    ``kanban_request_changes``. Nudging afterwards asks a worker that did
    the right thing to close a card it must not close.
    """
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_handoff")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": tool_name, "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": tool_name, "tool_call_id": "1", "content": "ok"},
    ]
    assert session_called_kanban_terminal(messages) is True, who
    assert build_kanban_stop_nudge(messages=messages) is None


def test_stop_gate_forces_exactly_one_followup_tool_call(clear_kanban_env, monkeypatch):
    """A narrated worker stop arms and consumes one Chat Completions tool requirement."""
    from agent import chat_completion_helpers as helpers
    from agent.turn_stop_gates import apply_stop_gates

    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_force_once")
    agent = SimpleNamespace(
        api_mode="chat_completions",
        tools=[{"type": "function", "function": {"name": "kanban_complete"}}],
        _kanban_stop_nudges=0,
        _turn_file_mutation_paths=set(),
        _verification_stop_nudges=0,
        _pre_verify_nudges=0,
        _interim_content_was_streamed=lambda _text: False,
        _emit_diagnostic_status=lambda _text: None,
    )
    messages = [{"role": "user", "content": "work kanban task t_force_once"}]
    verdict = apply_stop_gates(
        agent,
        {"role": "assistant", "content": "I am done.", "tool_calls": []},
        final_response="I am done.",
        messages=messages,
        conversation_history=None,
        pending_verification_response=None,
        pending_verification_response_previewed=False,
    )
    assert verdict.continue_turn is True
    assert agent._ephemeral_tool_choice == "required"

    monkeypatch.setattr(helpers, "_reasoning_config_for_wire", lambda _agent: None)
    monkeypatch.setattr(helpers, "effective_request_overrides", lambda _agent: {})
    monkeypatch.setattr(helpers, "_prompt_cache_scope_for_agent", lambda _agent: "scope")
    seen = []

    def build(_agent, _messages, _tools, _reasoning, overrides, _scope):
        seen.append(dict(overrides))
        return dict(overrides)

    monkeypatch.setattr(helpers, "_build_chat_completions_kwargs", build)
    forced = helpers._build_api_kwargs_for_mode(agent, messages, agent.tools)
    ordinary = helpers._build_api_kwargs_for_mode(agent, messages, agent.tools)

    assert forced["tool_choice"] == "required"
    assert "tool_choice" not in ordinary
    assert seen == [{"tool_choice": "required"}, {}]
    assert agent._ephemeral_tool_choice is None
    assert agent._kanban_stop_required_inflight is True


def test_required_followup_tool_call_replenishes_stop_nudge_budget(monkeypatch):
    """A successful required-tool recovery allows a later independent stop recovery."""
    from agent import turn_response_intake as intake

    agent = SimpleNamespace(
        _kanban_stop_required_inflight=True,
        _kanban_stop_nudges=2,
        quiet_mode=True,
        verbose_logging=False,
        tool_progress_callback=None,
        api_mode="chat_completions",
        _incomplete_scratchpad_retries=0,
        session_id="s",
        platform="",
        model="test",
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
    )
    message = SimpleNamespace(
        content="",
        finish_reason="tool_calls",
        tool_calls=[SimpleNamespace(function=SimpleNamespace(name="terminal"))],
    )
    monkeypatch.setattr(intake, "normalize_response_for_agent", lambda _agent, _resp: message)
    monkeypatch.setattr(intake, "splice_provider_projection", lambda *_a, **_k: None)
    monkeypatch.setattr(intake, "_fire_post_api_request_hook", lambda *_a, **_k: None)

    verdict = intake.normalize_model_response(
        agent,
        response=object(),
        messages=[],
        api_messages=[],
        conversation_history=None,
        api_call_count=1,
        api_duration=0.1,
        api_start_time=0.0,
        api_request_id="r",
        effective_task_id="t",
        turn_id="turn",
    )

    assert verdict.action == "fallthrough"
    assert agent._kanban_stop_nudges == 0
    assert agent._kanban_stop_required_inflight is False


def test_nudge_still_fires_for_non_terminal_kanban_tool(clear_kanban_env):
    """Widening the set must not swallow the case the guard exists for."""
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = [
        {
            "role": "assistant",
            "content": "Let me open the review next.",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_comment", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_comment", "tool_call_id": "1", "content": "ok"},
    ]
    assert session_called_kanban_terminal(messages) is False
    nudge = build_kanban_stop_nudge(messages=messages)
    assert nudge is not None
    # The nudge offers every worker exit, not just close-out; a card that must go
    # through review must never be steered to ``kanban_complete`` alone.
    assert "kanban_request_review" in nudge and "kanban_block" in nudge
