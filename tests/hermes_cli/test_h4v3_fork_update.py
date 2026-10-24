"""Behavior tests for fork-first, conflict-safe, squash updates using real Git repositories."""
from __future__ import annotations

from pathlib import Path
import importlib.util
import subprocess
import sys
from types import SimpleNamespace

import pytest

from hermes_cli import h4v3_fork_update as flow


def run(cwd: Path, *args: str) -> str:
    p = subprocess.run(["git", "-C", str(cwd), *args], text=True,
                       capture_output=True, check=False)
    assert p.returncode == 0, f"{args}: {p.stderr}"
    return p.stdout.strip()


def commit(repo: Path, name: str, contents: str) -> str:
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(contents)
    run(repo, "add", name)
    run(repo, "commit", "-m", f"edit {name}")
    return run(repo, "rev-parse", "HEAD")


@pytest.fixture
def forks(tmp_path, monkeypatch):
    local = tmp_path / "local"
    fork = tmp_path / "fork.git"
    upstream = tmp_path / "official.git"
    writer = tmp_path / "writer"
    local.mkdir()
    run(local, "init", "-b", "main")
    run(local, "config", "user.email", "ci@example.invalid")
    run(local, "config", "user.name", "CI")
    initial = commit(local, "base.txt", "shared\n")
    run(local, "init", "--bare", str(fork))
    run(local, "init", "--bare", str(upstream))
    run(local, "remote", "add", "origin", str(fork))
    run(local, "remote", "add", "upstream", str(upstream))
    run(local, "push", "origin", "main")
    run(local, "push", "upstream", "main")
    run(local, "switch", "-c", "production")
    retained = commit(local, "local.txt", "operator-owned\n")
    run(local, "push", "-u", "origin", "production")
    # Another checkout is the *only* source of upstream commits in these tests.
    run(local, "clone", str(upstream), str(writer))
    run(writer, "switch", "-c", "main", "origin/main")
    run(writer, "config", "user.email", "upstream@example.invalid")
    run(writer, "config", "user.name", "Upstream")
    flow._marker(local).write_text("{}\n")
    monkeypatch.setattr(flow, "_verify_candidate", lambda *_: None)
    monkeypatch.setattr(flow, "FORK_URL", str(fork))
    monkeypatch.setattr(flow, "UPSTREAM_URL", str(upstream))
    monkeypatch.setattr(flow, "FORK_BRANCH", "production")  # Legacy-topology test fixture.
    return SimpleNamespace(root=local, writer=writer, upstream=upstream,
                           fork=fork, initial=initial, retained=retained)


def advance(f):
    run(f.root, "fetch", "origin", "production")
    run(f.root, "merge", "--ff-only", "origin/production")


def test_squash_initial_and_incremental_preserves_all_local_commits(forks):
    f = forks
    upstream_one = commit(f.writer, "upstream.txt", "one\n")
    run(f.writer, "push", "origin", "main")
    original = run(f.root, "rev-parse", "HEAD")
    assert flow.prepare_update(f.root)
    # Publication changes only the fork; runtime source is untouched.
    assert run(f.root, "rev-parse", "HEAD") == original
    first = run(f.fork, "rev-parse", "refs/heads/production")
    assert first != original
    # If the standard updater fails after fork publication, a second click
    # must simply retry that fast-forward, not attempt another squash.
    assert flow.prepare_update(f.root)
    assert run(f.fork, "rev-parse", "refs/heads/production") == first
    advance(f)
    assert (f.root / "upstream.txt").read_text() == "one\n"
    assert run(f.root, "merge-base", "--is-ancestor", f.retained, "HEAD") == ""
    assert flow._last_synced(f.root, run(f.root, "rev-parse", "HEAD")) == upstream_one
    # A repeat click is idempotent: no repeated diff or extra squash.
    assert flow.prepare_update(f.root)
    assert run(f.fork, "rev-parse", "refs/heads/production") == first

    upstream_two = commit(f.writer, "upstream.txt", "one\ntwo\n")
    run(f.writer, "push", "origin", "main")
    assert flow.prepare_update(f.root)
    advance(f)
    assert (f.root / "upstream.txt").read_text() == "one\ntwo\n"
    assert (f.root / "local.txt").read_text() == "operator-owned\n"
    assert flow._last_synced(f.root, run(f.root, "rev-parse", "HEAD")) == upstream_two
    assert len(run(f.root, "log", "--format=%s", "--grep=chore(h4v3)").splitlines()) == 2


def test_update_button_status_uses_native_fork_only(forks):
    f = forks
    initial = flow.check_fork_updates(f.root)
    assert initial["behind"] == 0
    assert initial["upstreamPending"] is True  # no recorded squash yet
    upstream_sha = commit(f.writer, "upstream.txt", "new version\n")
    run(f.writer, "push", "origin", "main")
    # Upstream changes do not masquerade as a native origin/production update.
    status = flow.check_fork_updates(f.root)
    assert status["behind"] == 0
    assert status["updateAvailable"] is False
    assert status["upstreamPending"] is True
    assert status["upstreamTargetSha"] == upstream_sha
    flow.prepare_update(f.root)  # explicit upstream sync
    status = flow.check_fork_updates(f.root)
    assert status["behind"] == -1  # exact count unknown until fetch
    assert status["targetSha"] == run(f.fork, "rev-parse", "refs/heads/production")
    assert status["upstreamPending"] is True  # checkout not yet updated
    advance(f)  # native Hermes updater's fast-forward effect
    status = flow.check_fork_updates(f.root)
    assert status["behind"] == 0
    assert status["upstreamPending"] is False
    assert status["upstreamSyncedSha"] == upstream_sha


def test_conflict_leaves_both_fork_and_running_checkout_unmodified(forks):
    f = forks
    local = commit(f.root, "base.txt", "local change\n")
    run(f.root, "push", "origin", "production")
    commit(f.writer, "base.txt", "upstream change\n")
    run(f.writer, "push", "origin", "main")
    with pytest.raises(flow.ForkUpdateError, match="conflicts"):
        flow.prepare_update(f.root)
    assert run(f.root, "rev-parse", "HEAD") == local
    assert run(f.fork, "rev-parse", "refs/heads/production") == local
    assert flow._candidate(f.root).exists()
    with pytest.raises(flow.ForkUpdateError, match="pending"):
        flow.prepare_update(f.root)


def test_upstream_rewrite_refuses_without_publication(forks):
    f = forks
    commit(f.writer, "upstream.txt", "first\n")
    run(f.writer, "push", "origin", "main")
    flow.prepare_update(f.root)
    advance(f)
    last = run(f.fork, "rev-parse", "refs/heads/production")
    # Simulate a force-pushed upstream history unrelated to the prior head.
    run(f.writer, "checkout", "--orphan", "rewritten")
    commit(f.writer, "new.txt", "new universe\n")
    run(f.writer, "push", "--force", "origin", "rewritten:main")
    with pytest.raises(flow.ForkUpdateError, match="rewritten"):
        flow.prepare_update(f.root)
    assert run(f.fork, "rev-parse", "refs/heads/production") == last


def test_manual_conflict_resolution_publishes_squash_without_rewriting_local(forks, monkeypatch):
    f = forks
    old = commit(f.root, "base.txt", "operator version\n")
    run(f.root, "push", "origin", "production")
    up = commit(f.writer, "base.txt", "upstream version\n")
    run(f.writer, "push", "origin", "main")
    with pytest.raises(flow.ForkUpdateError):
        flow.prepare_update(f.root)
    isolated = flow._candidate(f.root)
    (isolated / "base.txt").write_text("reviewed combined resolution\n")
    run(isolated, "add", "base.txt")
    monkeypatch.setattr(flow, "__file__", str(f.root / "hermes_cli" / "h4v3_fork_update.py"))
    monkeypatch.setattr(sys, "argv", ["h4v3_fork_update", "continue"])
    assert flow._main() == 0
    assert run(f.root, "rev-parse", "HEAD") == old
    assert run(f.fork, "rev-parse", "refs/heads/production") != old
    advance(f)
    assert (f.root / "base.txt").read_text() == "reviewed combined resolution\n"
    assert flow._last_synced(f.root, run(f.root, "rev-parse", "HEAD")) == up


def test_user_confirmed_abort_discards_only_candidate(forks, monkeypatch):
    f = forks
    old = commit(f.root, "base.txt", "local change\n")
    run(f.root, "push", "origin", "production")
    commit(f.writer, "base.txt", "upstream change\n")
    run(f.writer, "push", "origin", "main")
    with pytest.raises(flow.ForkUpdateError):
        flow.prepare_update(f.root)
    monkeypatch.setattr(flow, "__file__", str(f.root / "hermes_cli" / "h4v3_fork_update.py"))
    monkeypatch.setattr(sys, "argv", ["h4v3_fork_update", "abort"])
    assert flow._main() == 2
    assert flow._candidate(f.root).exists()
    monkeypatch.setattr(sys, "argv", ["h4v3_fork_update", "abort", "--discard"])
    assert flow._main() == 0
    assert not flow._candidate(f.root).exists()
    assert run(f.root, "rev-parse", "HEAD") == old
    assert run(f.fork, "rev-parse", "refs/heads/production") == old


@pytest.mark.parametrize("review_state", ["dirty", "committed", "partial"])
def test_one_time_bootstrap_preserves_local_history_and_fork_main(tmp_path, monkeypatch, review_state):
    from pathlib import Path
    source = Path(__file__).resolve().parents[2] / "scripts" / "h4v3_fork_bootstrap.py"
    spec = importlib.util.spec_from_file_location("bootstrap_test_module", source)
    assert spec and spec.loader
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)

    local, upstream, fork = (tmp_path / name for name in ("local", "upstream.git", "fork.git"))
    local.mkdir()
    run(local, "init", "-b", "main")
    run(local, "config", "user.email", "operator@example.invalid")
    run(local, "config", "user.name", "Operator")
    original = commit(local, "base.txt", "base\\n")
    run(local, "init", "--bare", str(upstream))
    run(local, "init", "--bare", str(fork))
    run(local, "remote", "add", "origin", str(upstream))
    run(local, "remote", "add", "h4v3", str(fork))
    run(local, "push", "origin", "main")
    run(local, "push", "h4v3", "main")
    run(local, "switch", "-c", bootstrap.SOURCE)
    a = "agent/turn_api_request.py"
    b = "agent/turn_stop_gates.py"
    (local / "agent").mkdir()
    (local / a).write_text("_kanban_terminal_tool_required = True\\n")
    (local / b).write_text("gate = 1\\n")
    if review_state in {"committed", "partial"}:
        run(local, "add", a, b)
        run(local, "commit", "-m", "reviewed hotfix already committed by worker")
    if review_state == "partial":
        (local / b).write_text("gate = 2\\n")
    reviewed = run(local, "rev-parse", "HEAD")
    bootstrap.ROOT = local
    bootstrap.FORK = str(fork)
    bootstrap.UPSTREAM = str(upstream)
    bootstrap.FILES = (a, b)
    bootstrap.activate()
    assert run(local, "branch", "--show-current") == "production"
    assert run(local, "remote", "get-url", "origin") == str(fork)
    assert run(local, "remote", "get-url", "upstream") == str(upstream)
    assert run(local, "rev-parse", "HEAD") == run(fork, "rev-parse", "refs/heads/production")
    if review_state == "committed":
        assert run(local, "rev-parse", "HEAD") == reviewed
    else:
        assert run(local, "rev-parse", "HEAD") != reviewed
    assert run(fork, "rev-parse", "refs/heads/main") == original
    assert (local / ".git" / flow.MARKER).exists()
    first = run(local, "rev-parse", "HEAD")
    bootstrap.activate()  # no extra commit or forced push on repeated execution
    assert run(local, "rev-parse", "HEAD") == first


def test_normal_update_uses_official_updater_without_mutating_upstream(forks, monkeypatch):
    f = forks
    advance_sha = commit(f.writer, "upstream.txt", "upstream advanced\n")
    run(f.writer, "push", "origin", "main")
    original = run(f.root, "rev-parse", "HEAD")
    fork_before = run(f.fork, "rev-parse", "refs/heads/production")
    monkeypatch.setattr(flow, "prepare_update", lambda *_: pytest.fail("implicit sync is forbidden"))
    args = SimpleNamespace(branch=None, channel=None)
    flow.maybe_prepare_update(args, f.root)
    assert args.branch == "production"
    assert run(f.root, "rev-parse", "HEAD") == original
    assert run(f.fork, "rev-parse", "refs/heads/production") == fork_before
    assert flow.check_fork_updates(f.root)["upstreamTargetSha"] == advance_sha


def test_explicit_cli_sync_publishes_upstream_only_after_request(forks, monkeypatch):
    f = forks
    upstream_sha = commit(f.writer, "upstream.txt", "new bits\n")
    run(f.writer, "push", "origin", "main")
    old = run(f.root, "rev-parse", "HEAD")
    monkeypatch.setattr(flow, "__file__", str(f.root / "hermes_cli" / "h4v3_fork_update.py"))
    monkeypatch.setattr(sys, "argv", ["h4v3_fork_update", "sync"])
    assert flow._main() == 0
    assert run(f.root, "rev-parse", "HEAD") == old
    new = run(f.fork, "rev-parse", "refs/heads/production")
    assert new != old
    assert run(f.fork, "log", "-1", "--format=%B", "refs/heads/production").endswith(
        f"H4V3-Upstream-SHA: {upstream_sha}"
    )


def test_fork_banner_identity_refuses_stale_origin_main(forks):
    f = forks
    upstream_sha = commit(f.writer, "upstream.txt", "candidate\n")
    run(f.writer, "push", "origin", "main")
    flow.prepare_update(f.root)
    advance(f)
    fork = flow.fork_banner_identity(f.root)
    assert fork == {"upstream": upstream_sha, "local": run(f.root, "rev-parse", "HEAD")}

    from hermes_cli import banner
    assert banner._compute_git_banner_state(f.root) == {
        "upstream": upstream_sha[:8],
        "local": fork["local"][:8],
        "ahead": 0,
        "fork": True,
    }


def test_fork_banner_prints_recorded_hashes_not_stale_semver(forks, monkeypatch):
    f = forks
    upstream_sha = commit(f.writer, "upstream.txt", "candidate\n")
    run(f.writer, "push", "origin", "main")
    flow.prepare_update(f.root)
    advance(f)
    from hermes_cli import banner
    monkeypatch.setattr("hermes_cli.config.get_project_root", lambda: f.root)
    monkeypatch.setattr("hermes_cli.update_channel.resolve_update_channel", lambda *_: "main")
    monkeypatch.setattr(banner, "get_git_banner_state", lambda: banner._compute_git_banner_state(f.root))
    label = banner.format_banner_version_label()
    assert "H4V3 source fork" in label
    assert f"synced upstream/main {upstream_sha[:8]}" in label
    assert f"local {run(f.root, 'rev-parse', 'HEAD')[:8]}" in label
    assert "v0.21" not in label


def test_version_status_distinguishes_fork_from_upstream(capsys, monkeypatch):
    from hermes_cli import _startup_fast, banner, source_check

    monkeypatch.setattr(banner, "format_banner_version_label", lambda: "Hermes Agent · H4V3 source fork")
    monkeypatch.setattr(_startup_fast, "project_root_str", lambda: "/tmp/h4v3-fork")
    monkeypatch.setattr(_startup_fast, "read_openai_version", lambda: "2.24.0")
    monkeypatch.setattr("hermes_cli.config.detect_install_method", lambda *_: "git")
    monkeypatch.setattr("hermes_cli.config.recommended_update_command", lambda: "hermes update")
    monkeypatch.setattr(source_check, "check_for_updates", lambda **_: {
        "behind": 0, "upstreamSyncedSha": "a" * 40, "upstreamPending": True,
    })
    _startup_fast.print_fast_version_info()
    result = capsys.readouterr().out
    assert "Fork main up to date" in result
    assert "Official upstream/main has newer code" in result
    assert "h4v3_fork_update sync" in result
    assert "1 commit behind" not in result


def test_main_only_route_uses_fork_main_not_legacy_production(forks, monkeypatch):
    f = forks
    monkeypatch.setattr(flow, "FORK_BRANCH", "main")
    with pytest.raises(flow.ForkUpdateError, match="main branch"):
        flow._verify(f.root)  # Legacy production checkout is rejected after cutover.
    run(f.root, "switch", "main")
    args = SimpleNamespace(branch=None, channel=None)
    flow.maybe_prepare_update(args, f.root)
    assert args.branch == "main"
    state = flow.check_fork_updates(f.root)
    assert state["behind"] == 0
    assert state["targetSha"] == run(f.fork, "rev-parse", "refs/heads/main")
    assert run(f.root, "branch", "--show-current") == "main"


def test_disabled_install_keeps_stock_update_unchanged(tmp_path):
    local = tmp_path / "checkout"
    local.mkdir()
    run(local, "init", "-b", "main")
    args = SimpleNamespace(branch=None, channel=None)
    flow.maybe_prepare_update(args, local)
    assert args.branch is None


def test_explicit_other_update_channel_is_rejected_in_fork_mode(forks):
    args = SimpleNamespace(branch=None, channel="stable")
    with pytest.raises(SystemExit, match="main"):
        flow.maybe_prepare_update(args, forks.root)


def test_git_command_timeout_is_reported_as_fork_update_error(tmp_path, monkeypatch):
    def timed_out(command, **kwargs):
        assert kwargs["timeout"] == flow.GIT_COMMAND_TIMEOUT_SECONDS
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(flow.subprocess, "run", timed_out)
    with pytest.raises(flow.ForkUpdateError, match="git ls-remote timed out"):
        flow._git(tmp_path, "ls-remote", "origin", "main")
