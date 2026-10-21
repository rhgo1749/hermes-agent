"""Regression: dispatcher workspace provisioning must run under the assignee's secret scope.

Live incident (re-bound board, selector spawn runs 867-871): a worktree's fresh-base
fetch got a credential-required refusal from GitHub (transient network window), the
credential fallback called ``get_secret("GITHUB_TOKEN")`` in the dispatcher's UNBOUND
context, and multiplexing's fail-closed ``UnscopedSecretError`` killed every spawn as
``workspace: Hermes could not read this profile's GITHUB_TOKEN`` until the task gave up.

The fix binds ``_worker_profile_scope(assignee_home, bind_home=False)`` around workspace
resolution — the same binder the spawn-env build already uses. These tests prove:
  * the assignee's own token resolves during provisioning (no raise, no give-up),
  * the launch profile's ambient ``os.environ`` token never crosses into another
    profile's provisioning,
  * the fail-closed raise for genuinely unscoped reads is NOT weakened,
  * the binding is tick-local: the scope is reset the moment provisioning returns.
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest


@pytest.fixture()
def multiplex_home(monkeypatch):
    """A HERMES_HOME with one served profile ('alpha', with .env credentials), and
    multiplex semantics active for the current task only."""
    test_home = tempfile.mkdtemp(prefix="kanban_secret_scope_test_")
    os.makedirs(os.path.join(test_home, "profiles", "alpha"), exist_ok=True)
    with open(os.path.join(test_home, "profiles", "alpha", "config.yaml"), "w") as fh:
        fh.write("{}\n")
    with open(os.path.join(test_home, "profiles", "alpha", ".env"), "w") as fh:
        fh.write("GITHUB_TOKEN=alpha-profile-token\n")
    monkeypatch.setenv("HERMES_HOME", test_home)
    # A launch-home credential must never be usable for the served profile.
    monkeypatch.setenv("GITHUB_TOKEN", "launch-ambient-token")
    for mod in list(sys.modules.keys()):
        if mod.startswith("hermes_cli") or mod.startswith("hermes_state") or mod == "hermes_constants":
            del sys.modules[mod]
    from agent import secret_scope
    token = secret_scope.set_multiplex_context(True)
    try:
        yield secret_scope
    finally:
        secret_scope.reset_multiplex_context(token)


def _fake_spawn(*args, **kwargs):
    return 12345


def _dispatch_with_provisioning_probe(multiplex_home, monkeypatch, *, token_in_env_only: bool):
    """Dispatch one ready task for 'alpha' while the fake workspace resolver reads
    get_secret("GITHUB_TOKEN") — exactly what the credential fallback does — and report
    (result, observed_token_values)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import kanban_db_workspace as kbw

    seen: list = []
    workspace_dir = tempfile.mkdtemp(prefix="ws_probe_")

    def fake_resolve(task, *, board=None):
        seen.append(multiplex_home.get_secret("GITHUB_TOKEN"))
        return workspace_dir

    monkeypatch.setattr(kbw, "resolve_workspace", fake_resolve)

    if token_in_env_only:
        # Drop alpha's own token: the scoped miss must return None, not the launch env.
        env_path = os.path.join(os.environ["HERMES_HOME"], "profiles", "alpha", ".env")
        with open(env_path, "w") as fh:
            fh.write("OTHER=karma\n")

    with kbc.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        kb.create_task(conn, title="probe", assignee="alpha")
        result = kbd.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)
    return result, seen


def test_assignee_token_resolves_during_provisioning(multiplex_home, monkeypatch):
    """The credential-fallback read inside provisioning sees the ASSIGNEE profile's
    token — the spawn cannot die as 'workspace: UnscopedSecretError' anymore."""
    result, seen = _dispatch_with_provisioning_probe(
        multiplex_home, monkeypatch, token_in_env_only=False)
    assert seen == ["alpha-profile-token"], seen
    assert result.spawned, result
    assert not result.auto_blocked


def test_launch_env_token_never_crosses(multiplex_home, monkeypatch):
    """With no token in alpha's own scope, provisioning gets None: the launch home's
    ambient GITHUB_TOKEN must not be usable for another profile's repo fetch (#109494
    lineage), and the miss must not raise or block the card either."""
    result, seen = _dispatch_with_provisioning_probe(
        multiplex_home, monkeypatch, token_in_env_only=True)
    assert seen == [None], seen
    assert result.spawned, result


def test_scope_is_tick_local(multiplex_home, monkeypatch):
    """After the tick, no scope remains bound: a later unscoped read under multiplex
    still fails closed — the fix binds, it does not globally widen or soften."""
    from agent.secret_scope import UnscopedSecretError, current_secret_scope

    result, _seen = _dispatch_with_provisioning_probe(
        multiplex_home, monkeypatch, token_in_env_only=False)
    assert result.spawned, result
    assert current_secret_scope() is None
    with pytest.raises(UnscopedSecretError):
        multiplex_home.get_secret("GITHUB_TOKEN")


def test_assignee_profile_home_fixture_semantics(multiplex_home):
    """Unknown/blank assignees resolve to no profile home (the nullcontext branch);
    real profile names resolve to their home under HERMES_HOME."""
    from hermes_cli.kanban_db_dispatch import _assignee_profile_home

    assert _assignee_profile_home(None) is None
    assert _assignee_profile_home("") is None
    home = _assignee_profile_home("alpha")
    assert home and os.path.realpath(home) == os.path.realpath(
        os.path.join(os.environ["HERMES_HOME"], "profiles", "alpha"))
