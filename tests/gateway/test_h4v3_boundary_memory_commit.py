"""Regression for upstream's session-boundary agent cache contract.

The fork's normal agent cache eviction remains resource-only; a reset/close
boundary must commit both provider memory and context-engine state before
the cached agent releases clients. Does not run a real Gateway or user session.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import Mock

from gateway.run import GatewayRunner


def _runner(agent, key="agent:main:cli:boundary"):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._agent_cache = {key: (agent, "signature")}
    runner._agent_cache_lock = threading.Lock()
    runner._peek_session_state = lambda _key: None
    runner._running_agent_ids = lambda: set()
    # Keep the test deterministic; the real implementation runs this under
    # the agent's owning profile on a background thread.
    runner._spawn_release_thread = lambda target, args, *_a, **_kw: target(*args)
    return runner, key


def _agent(*, has_provider=False, has_engine=False):
    return SimpleNamespace(
        _memory_manager=object() if has_provider else None,
        context_compressor=object() if has_engine else None,
        _session_messages=[{"role": "user", "content": "remember the current task"}],
        commit_memory_session=Mock(),
        release_clients=Mock(),
    )


def test_boundary_eviction_commits_context_engine_even_without_provider():
    agent = _agent(has_engine=True)
    messages = agent._session_messages
    runner, key = _runner(agent)
    runner._evict_cached_agent_at_boundary(key)
    agent.commit_memory_session.assert_called_once_with(messages)
    agent.release_clients.assert_called_once_with()
    assert key not in runner._agent_cache


def test_normal_eviction_does_not_end_a_context_engine_only_session():
    agent = _agent(has_engine=True)
    runner, key = _runner(agent)
    runner._evict_cached_agent(key)
    agent.commit_memory_session.assert_not_called()
    agent.release_clients.assert_called_once_with()


def test_boundary_eviction_commits_external_memory_provider():
    agent = _agent(has_provider=True)
    messages = agent._session_messages
    runner, key = _runner(agent)
    runner._evict_cached_agent_at_boundary(key)
    agent.commit_memory_session.assert_called_once_with(messages)
    agent.release_clients.assert_called_once_with()


def test_boundary_eviction_skips_commit_when_no_memory_or_engine():
    agent = _agent()
    runner, key = _runner(agent)
    runner._evict_cached_agent_at_boundary(key)
    agent.commit_memory_session.assert_not_called()
    agent.release_clients.assert_called_once_with()
