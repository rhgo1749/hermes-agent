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
                       capture_output=True)
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


def test_update_button_status_uses_upstream_and_production(forks):
    f = forks
    assert flow.check_fork_updates(f.root)["behind"] == 0
    commit(f.writer, "upstream.txt", "new version\n")
    run(f.writer, "push", "origin", "main")
    assert flow.check_fork_updates(f.root)["behind"] == 1
    flow.prepare_update(f.root)
    assert flow.check_fork_updates(f.root)["behind"] == 1  # not applied locally yet
    advance(f)
    status = flow.check_fork_updates(f.root)
    assert status["behind"] == 0
    assert status["updateAvailable"] is False


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


def test_disabled_install_keeps_stock_update_unchanged(tmp_path):
    local = tmp_path / "checkout"
    local.mkdir()
    run(local, "init", "-b", "main")
    args = SimpleNamespace(branch=None, channel=None)
    flow.maybe_prepare_update(args, local)
    assert args.branch is None


def test_explicit_other_update_channel_is_rejected_in_fork_mode(forks):
    args = SimpleNamespace(branch=None, channel="stable")
    with pytest.raises(SystemExit, match="production"):
        flow.maybe_prepare_update(args, forks.root)
