"""Regression: a NEW Kanban task worktree in a remote-backed repo must branch
from the freshly fetched remote trunk, never from a possibly stale local HEAD.

Incident (antigravity-acp-harness Issue #45): a Developer worktree was created
at a local main nine commits behind origin, missing ``scripts/
audit_public_tree.py`` that had already merged via PR #44; the task could only
fail-closed on a contract the checkout contradicted. All fixtures use real
local (bare) git remotes — deterministic, no network.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db_workspace as kbw


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
    )
    return (proc.stdout or "").strip()


def _git_rc(args: list[str], cwd: Path) -> int:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False,
    ).returncode


@pytest.fixture()
def remote_repo(tmp_path: Path) -> dict:
    """Bare origin + seed + clone, shaped like the incident: the clone's main
    stays at ``old_sha`` while remote main advances to ``new_sha`` which adds
    ``scripts/audit_public_tree.py`` (the merged-auditor shape of PR #44)."""
    remote = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    clone = tmp_path / "clone"

    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    _git(["symbolic-ref", "HEAD", "refs/heads/main"], remote)

    seed.mkdir()
    _git(["init", "-q", "-b", "main"], seed)
    _git(["config", "user.email", "t@example.invalid"], seed)
    _git(["config", "user.name", "T"], seed)
    (seed / "README.md").write_text("base\n", encoding="utf-8")
    _git(["add", "README.md"], seed)
    _git(["commit", "-qm", "old base"], seed)
    _git(["remote", "add", "origin", str(remote)], seed)
    _git(["push", "-q", "origin", "main"], seed)
    old_sha = _git(["rev-parse", "HEAD"], seed)

    subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True)
    _git(["config", "user.email", "t@example.invalid"], clone)
    _git(["config", "user.name", "T"], clone)

    (auditor := seed / "scripts" / "audit_public_tree.py").parent.mkdir(parents=True, exist_ok=True)
    auditor.write_text("def main() -> None: ...\n", encoding="utf-8")
    _git(["add", "-A"], seed)
    _git(["commit", "-qm", "add current-tree auditor (merged PR shape)"], seed)
    _git(["push", "-q", "origin", "main"], seed)
    new_sha = _git(["rev-parse", "HEAD"], seed)

    return {
        "tmp": tmp_path, "remote": remote, "seed": seed, "clone": clone,
        "old_sha": old_sha, "new_sha": new_sha,
    }


def test_new_branch_uses_fetched_remote_tip_not_stale_head(remote_repo) -> None:
    """The core regression: local HEAD lags the remote, yet the new task
    worktree must start exactly at the fetched remote tip and contain the
    file the stale baseline lacked."""
    clone, tmp = remote_repo["clone"], remote_repo["tmp"]
    assert _git(["rev-parse", "HEAD"], clone) == remote_repo["old_sha"]

    target = tmp / "wt" / "t_new"
    kbw._ensure_git_worktree(clone, target, "wt/t_new")

    assert _git(["rev-parse", "HEAD"], target) == remote_repo["new_sha"]
    assert (target / "scripts" / "audit_public_tree.py").is_file()
    # The fetch moved only the remote-tracking ref, never the checkout.
    assert _git(["rev-parse", "refs/remotes/origin/main"], clone) == remote_repo["new_sha"]
    assert _git(["rev-parse", "HEAD"], clone) == remote_repo["old_sha"]


def test_operator_checkout_dirty_and_ahead_is_never_moved(remote_repo) -> None:
    """A human's working checkout with a local-only commit and uncommitted
    changes stays exactly where it is; only the new task worktree derives
    from the remote tip."""
    clone, tmp = remote_repo["clone"], remote_repo["tmp"]
    (clone / "local-only.txt").write_text("unpushed work\n", encoding="utf-8")
    _git(["add", "local-only.txt"], clone)
    _git(["commit", "-qm", "local-only commit"], clone)
    (clone / "README.md").write_text("dirty uncommitted edit\n", encoding="utf-8")
    local_head = _git(["rev-parse", "HEAD"], clone)

    target = tmp / "wt" / "t_new"
    kbw._ensure_git_worktree(clone, target, "wt/t_new")

    assert _git(["rev-parse", "HEAD"], target) == remote_repo["new_sha"]
    assert _git(["rev-parse", "main"], clone) == local_head
    assert _git(["rev-parse", "HEAD"], clone) == local_head
    assert (clone / "README.md").read_text(encoding="utf-8") == "dirty uncommitted edit\n"
    assert (clone / "local-only.txt").is_file()


def test_fetch_failure_fails_closed_without_worktree(remote_repo) -> None:
    """Fetch failure must block the run, not silently base the task on the
    (perfectly healthy) local HEAD."""
    clone, tmp = remote_repo["clone"], remote_repo["tmp"]
    _git(["remote", "set-url", "origin", str(tmp / "gone.git")], clone)

    target = tmp / "wt" / "t_new"
    with pytest.raises(RuntimeError, match="fetch"):
        kbw._ensure_git_worktree(clone, target, "wt/t_new")

    assert not target.exists()
    assert _git_rc(["show-ref", "--verify", "refs/heads/wt/t_new"], clone) != 0


def test_target_branch_missing_on_remote_fails_closed(remote_repo) -> None:
    """No stale-HEAD fallback when the target branch no longer exists on the
    remote even though local HEAD and cached refs are fine."""
    clone, seed, remote, tmp = remote_repo["clone"], remote_repo["seed"], remote_repo["remote"], remote_repo["tmp"]
    # A bare remote refuses deleting the branch its HEAD points at; move HEAD
    # first so the deletion mirrors a real "default branch removed" event.
    _git(["symbolic-ref", "HEAD", "refs/heads/released"], remote)
    _git(["push", "-q", "origin", "--delete", "main"], seed)

    target = tmp / "wt" / "t_new"
    with pytest.raises(RuntimeError, match="fetch"):
        kbw._ensure_git_worktree(clone, target, "wt/t_new")

    assert not target.exists()
    assert _git_rc(["show-ref", "--verify", "refs/heads/wt/t_new"], clone) != 0


def test_ambiguous_remotes_fail_closed(remote_repo) -> None:
    clone, tmp = remote_repo["clone"], remote_repo["tmp"]
    _git(["remote", "rename", "origin", "alpha"], clone)
    _git(["remote", "add", "beta", str(remote_repo["remote"])], clone)

    target = tmp / "wt" / "t_new"
    with pytest.raises(RuntimeError, match="multiple remotes"):
        kbw._ensure_git_worktree(clone, target, "wt/t_new")
    assert not target.exists()


def test_fresh_remote_base_reports_the_triple(remote_repo) -> None:
    assert kbw._fresh_remote_base(remote_repo["clone"]) == (
        "origin", "main", remote_repo["new_sha"],
    )


def test_local_repo_without_remote_keeps_head_base(tmp_path: Path) -> None:
    """Genuinely local-only Kanban work keeps the historic HEAD semantics —
    no remote, no fetch requirement."""
    repo = tmp_path / "solo"
    repo.mkdir()
    _git(["init", "-q", "-b", "main"], repo)
    _git(["config", "user.email", "t@example.invalid"], repo)
    _git(["config", "user.name", "T"], repo)
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(["add", "a.txt"], repo)
    _git(["commit", "-qm", "only commit"], repo)
    head = _git(["rev-parse", "HEAD"], repo)

    assert kbw._fresh_remote_base(repo) is None
    target = tmp_path / "wt" / "t_local"
    kbw._ensure_git_worktree(repo, target, "wt/t_local")
    assert _git(["rev-parse", "HEAD"], target) == head


def test_existing_branch_continuation_is_not_rebased_onto_new_main(remote_repo) -> None:
    """Rework/retry contract: an existing task branch resumes at its own tip.
    Re-materializing it must not recreate it from the (advanced) remote tip."""
    clone, seed, tmp = remote_repo["clone"], remote_repo["seed"], remote_repo["tmp"]
    target = tmp / "wt" / "t_old"
    kbw._ensure_git_worktree(clone, target, "wt/t_old")

    (target / "WORK.md").write_text("round 1\n", encoding="utf-8")
    _git(["add", "WORK.md"], target)
    _git(["commit", "-qm", "round 1 work"], target)
    task_tip = _git(["rev-parse", "HEAD"], target)

    (seed / "later.txt").write_text("later main movement\n", encoding="utf-8")
    _git(["add", "-A"], seed)
    _git(["commit", "-qm", "main moves on"], seed)
    _git(["push", "-q", "origin", "main"], seed)

    _git(["worktree", "remove", str(target)], clone)
    kbw._ensure_git_worktree(clone, target, "wt/t_old")
    assert _git(["rev-parse", "HEAD"], target) == task_tip
    assert (target / "WORK.md").is_file()
    assert not (target / "later.txt").exists()


def test_narrow_clone_without_main_refspec_still_bases_on_remote_tip(remote_repo) -> None:
    """Tag-pinned ``--single-branch`` clones (#125112 class) have no refspec
    for main; the explicit tracking-refspec retry must still prove a fresh
    base instead of falling back to the detached HEAD."""
    seed, tmp, old_sha = remote_repo["seed"], remote_repo["tmp"], remote_repo["old_sha"]
    _git(["tag", "-f", "v0", old_sha], seed)
    _git(["push", "-q", "origin", "v0"], seed)
    narrow = tmp / "narrow"
    subprocess.run(
        ["git", "clone", "-q", "--single-branch", "--branch", "v0",
         str(remote_repo["remote"]), str(narrow)],
        check=True,
    )
    assert _git_rc(["show-ref", "--verify", "refs/remotes/origin/main"], narrow) != 0

    target = tmp / "wt" / "t_narrow"
    kbw._ensure_git_worktree(narrow, target, "wt/t_narrow")
    assert _git(["rev-parse", "HEAD"], target) == remote_repo["new_sha"]
