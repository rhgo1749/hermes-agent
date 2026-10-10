"""Regression: the Kanban terminal nudge requires one real tool call only in owned workers."""
from types import SimpleNamespace

from agent import turn_api_request, turn_stop_gates


def test_stop_nudge_arms_tool_choice_and_consumes_once(monkeypatch):
    monkeypatch.setattr(turn_stop_gates, "_verify_on_stop_nudge", lambda agent: None)
    monkeypatch.setattr(turn_stop_gates, "_pre_verify_nudge", lambda *args: None)
    monkeypatch.setattr(turn_stop_gates, "_kanban_stop_nudge", lambda *args: "Finish via a Kanban tool.")
    monkeypatch.setattr("agent.delegation_context.owned_kanban_task", lambda: "t_00000001")
    agent = SimpleNamespace(api_mode="chat_completions", _kanban_stop_nudges=0,
                            _interim_content_was_streamed=lambda _: False,
                            _emit_diagnostic_status=lambda _: None)
    messages = [{"role": "user", "content": "work task"}]
    verdict = turn_stop_gates.apply_stop_gates(
        agent, {"role": "assistant", "content": "Done."},
        final_response="Done.", messages=messages,
        conversation_history=None, pending_verification_response=None,
        pending_verification_response_previewed=None)
    assert verdict.continue_turn
    assert agent._kanban_terminal_tool_required is True
    kwargs = {"model": "local", "tools": [{"type": "function", "function": {"name": "kanban_block"}}]}
    turn_api_request._apply_kanban_terminal_tool_choice(agent, kwargs)
    assert kwargs["tool_choice"] == "required"
    assert agent._kanban_terminal_tool_required is False
    later = {"tools": kwargs["tools"]}
    turn_api_request._apply_kanban_terminal_tool_choice(agent, later)
    assert "tool_choice" not in later


def test_unsupported_or_unowned_request_does_not_force_choice(monkeypatch):
    monkeypatch.setattr("agent.delegation_context.owned_kanban_task", lambda: None)
    for mode, tools in (("chat_completions", [{"type": "function"}]),
                        ("codex_responses", [{"type": "function"}]),
                        ("chat_completions", [])):
        agent = SimpleNamespace(api_mode=mode, _kanban_terminal_tool_required=True)
        kwargs = {"tools": tools}
        turn_api_request._apply_kanban_terminal_tool_choice(agent, kwargs)
        assert "tool_choice" not in kwargs
        assert not agent._kanban_terminal_tool_required
