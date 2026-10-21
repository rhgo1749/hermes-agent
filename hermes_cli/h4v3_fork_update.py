"""H4V3 fork identity and optional, separate upstream integration.

Normal hermes update uses the official Hermes source updater to pull
origin/main without merging upstream or rewriting fork history.
An explicit python -m hermes_cli.h4v3_fork_update sync prepares the existing
conflict-safe upstream squash for a subsequent native update. The stock updater
owns dependency selection, backups, restart and health checks.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

FORK_URL = "https://github.com/rhgo1749/hermes-agent.git"
UPSTREAM_URL = "https://github.com/NousResearch/hermes-agent.git"
FORK_BRANCH = "main"
CANDIDATE_BRANCH = "h4v3/upstream-squash-candidate"
CANDIDATE_DIR = ".worktrees/h4v3-upstream-squash"
MARKER = "h4v3-fork-update.json"
TRAILER = "H4V3-Upstream-SHA: "
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class ForkUpdateError(RuntimeError):
    """Refuse publication without disturbing the main checkout."""


def _git(root: Path, *args: str, cwd: Path | None = None, input_bytes: bytes | None = None,
         check: bool = True) -> subprocess.CompletedProcess:
    git_cwd = cwd or root
    if args and args[0] in {"fetch", "push", "ls-remote"}:
        # Reuse the native updater's bounded, no-prompt network path and its
        # process-tree custody. The explicit sync must not leave a remote helper
        # running after the operator believes a timed-out publish was refused.
        from hermes_cli.update_cmd import _git_run
        result = _git_run(["git"], list(args), cwd=git_cwd, network=True)
        proc = subprocess.CompletedProcess(
            result.args, result.returncode,
            (result.stdout or "").encode("utf-8"), (result.stderr or "").encode("utf-8"))
    else:
        try:
            proc = subprocess.run(["git", *args], cwd=git_cwd, input=input_bytes,
                                  capture_output=True, check=False, timeout=300)
        except subprocess.TimeoutExpired as exc:
            raise ForkUpdateError(f"git {args[0]} timed out after 300s") from exc
    if check and proc.returncode:
        message = proc.stderr.decode("utf-8", "replace").strip()
        raise ForkUpdateError(f"git {args[0]} failed: {message[-800:]}")
    return proc


def _out(root: Path, *args: str, cwd: Path | None = None) -> str:
    return _git(root, *args, cwd=cwd).stdout.decode("utf-8", "replace").strip()


def _sha(root: Path, ref: str) -> str:
    return _out(root, "rev-parse", "--verify", f"{ref}^{{commit}}")


def _marker(root: Path) -> Path:
    git_dir = Path(_out(root, "rev-parse", "--absolute-git-dir"))
    return git_dir / MARKER


def _pending_file(root: Path) -> Path:
    return _marker(root).with_name("h4v3-upstream-pending.json")


def _url(url: str) -> str:
    return url.removesuffix(".git").rstrip("/")


def _enabled(root: Path) -> bool:
    # Source status also probes Nix/embedded installs without a Git checkout.
    # Those are ordinary upstream installations, not opted-in fork deployments.
    if not (root / ".git").exists():
        return False
    result = _git(root, "rev-parse", "--absolute-git-dir", check=False)
    if result.returncode:
        return False
    return (Path(result.stdout.decode("utf-8", "replace").strip()) / MARKER).is_file()


def _verify(root: Path) -> None:
    if _url(_out(root, "remote", "get-url", "origin")) != _url(FORK_URL):
        raise ForkUpdateError("origin must be the H4V3 fork")
    if _url(_out(root, "remote", "get-url", "upstream")) != _url(UPSTREAM_URL):
        raise ForkUpdateError("upstream must be NousResearch/hermes-agent")
    if _out(root, "symbolic-ref", "--short", "HEAD") != FORK_BRANCH:
        raise ForkUpdateError("running checkout must be on main branch")
    if _out(root, "status", "--porcelain=v1", "--untracked-files=no"):
        raise ForkUpdateError("main has uncommitted changes; commit them before updating")


def _last_synced(root: Path, main_sha: str) -> str | None:
    message = _out(root, "log", "-1", "--format=%B", "--grep=H4V3-Upstream-SHA:",
                   main_sha)
    rows = re.findall(r"^H4V3-Upstream-SHA:\s*([0-9a-f]{40})$", message, re.MULTILINE)
    return rows[-1] if rows else None


def fork_banner_identity(root: Path) -> dict[str, str] | None:
    """Identify the activated main fork and its recorded upstream baseline.

    An upstream squash is not a Git ancestor of the fork HEAD; neither git
    describe nor origin/main reliably identifies its code version. Do not
    claim a release version from a squash commit; surface exact SHAs.
    """
    if not _enabled(root):
        return None
    try:
        if (_url(_out(root, "remote", "get-url", "origin")) != _url(FORK_URL)
                or _url(_out(root, "remote", "get-url", "upstream")) != _url(UPSTREAM_URL)
                or _out(root, "branch", "--show-current") != FORK_BRANCH):
            return None
        local = _sha(root, "HEAD")
        synced = _last_synced(root, local)
    except ForkUpdateError:
        return None
    if not (SHA_RE.fullmatch(local) and synced and SHA_RE.fullmatch(synced)):
        return None
    return {"upstream": synced, "local": local}


def _candidate(root: Path) -> Path:
    return root / CANDIDATE_DIR


def _pending(root: Path) -> bool:
    worktree = _candidate(root)
    ref = _git(root, "show-ref", "--verify", "--quiet",
               f"refs/heads/{CANDIDATE_BRANCH}", check=False)
    return worktree.exists() or ref.returncode == 0


def _apply_delta(root: Path, candidate: Path, previous: str | None, upstream: str) -> None:
    if previous:
        # The preceding squash has no upstream parent, so merge-base would
        # replay old upstream changes. Apply only the increment with Git's
        # real three-way index conflict detection instead.
        patch = _git(root, "diff", "--binary", previous, upstream).stdout
        if patch:
            result = _git(root, "apply", "--3way", "--binary", "-", cwd=candidate,
                          input_bytes=patch, check=False)
            if result.returncode:
                raise ForkUpdateError("upstream delta conflicts with H4V3 main")
    else:
        result = _git(root, "merge", "--squash", upstream, cwd=candidate, check=False)
        if result.returncode:
            raise ForkUpdateError("initial upstream squash conflicts with H4V3 main")


def _resolve_upstream(root: Path, main_sha: str) -> tuple[str, str | None, str]:
    # Only fetch the two branch heads, not thousands of task branches.
    _git(root, "fetch", "--no-tags", "origin",
         f"+refs/heads/{FORK_BRANCH}:refs/remotes/origin/{FORK_BRANCH}")
    _git(root, "fetch", "--no-tags", "upstream",
         "+refs/heads/main:refs/remotes/upstream/main")
    fork_sha = _sha(root, f"refs/remotes/origin/{FORK_BRANCH}")
    if main_sha != fork_sha and _git(
        root, "merge-base", "--is-ancestor", main_sha, fork_sha,
        check=False).returncode:
        raise ForkUpdateError("fork main diverged; reconcile before update")
    upstream = _sha(root, "refs/remotes/upstream/main")
    previous = _last_synced(root, main_sha)
    if previous:
        if _git(root, "merge-base", "--is-ancestor", previous, upstream, check=False).returncode:
            raise ForkUpdateError("upstream history was rewritten; rebase/reconcile manually")
    return upstream, previous, fork_sha


def _verify_candidate(root: Path, candidate: Path) -> None:
    unmerged = _out(root, "ls-files", "-u", cwd=candidate)
    if unmerged:
        raise ForkUpdateError("unresolved conflicts in isolated integration worktree")
    status = _out(root, "status", "--porcelain=v1", "--untracked-files=all", cwd=candidate)
    if any(row.startswith("?? ") for row in status.splitlines()):
        raise ForkUpdateError("untracked files in isolated integration worktree; stage or remove them")
    _git(root, "diff", "--cached", "--check", cwd=candidate)
    _git(root, "diff", "--check", cwd=candidate)
    # The normal updater validates the new PM environment *after* fetching.
    # Before publication, enforce Python source syntax without requiring
    # pytest or installing anything in the currently-running generation.
    changed = _git(root, "diff", "--cached", "--name-only", "-z",
                   "--diff-filter=ACMRT", cwd=candidate).stdout
    for relative in (name for name in changed.split(b"\0") if name.endswith(b".py")):
        path = candidate / os.fsdecode(relative)
        if path.is_file():
            try:
                compile(path.read_bytes(), str(path), "exec")
            except SyntaxError as exc:
                raise ForkUpdateError(f"invalid Python syntax: {relative!r}: {exc}") from exc


def _publish_candidate(root: Path, candidate: Path, upstream_sha: str,
                       main_sha: str, *, validate: bool) -> None:
    if validate:
        _verify_candidate(root, candidate)
    # A marked commit may have no tree delta (upstream already merged
    # elsewhere); the provenance trailer still prevents replay next time.
    _git(root, "add", "-u", cwd=candidate)
    _git(root, "commit", "--allow-empty", "-m",
         f"chore(h4v3): squash upstream {upstream_sha[:12]}",
         "-m", f"{TRAILER}{upstream_sha}", cwd=candidate)
    if _git(root, "merge-base", "--is-ancestor", main_sha,
            _sha(root, CANDIDATE_BRANCH), check=False).returncode:
        raise ForkUpdateError("candidate no longer preserves main commits")
    # Never force push. A concurrent fork push must reject this update.
    _git(root, "push", "origin", f"{CANDIDATE_BRANCH}:refs/heads/{FORK_BRANCH}")
    print("✓ Upstream changes squash-published to fork main.")
    print("  Existing main commits preserved; stock Hermes updater will now apply the new commit.")


def prepare_update(root: Path) -> bool:
    """Return True only for an enabled fork. Refuse before stock updater mutates main."""
    if not _enabled(root):
        return False
    _verify(root)
    if _pending(root):
        raise ForkUpdateError(f"Previous integration pending at {_candidate(root)}; "
                              "resolve/abort it before another update")
    main_sha = _sha(root, FORK_BRANCH)
    upstream, previous, fork_sha = _resolve_upstream(root, main_sha)
    if fork_sha != main_sha:
        print("✓ Fork main already has a newer commit; letting the stock updater fast-forward it.")
        return True
    if previous == upstream or (not previous and
                               _git(root, "merge-base", "--is-ancestor", upstream,
                                    main_sha, check=False).returncode == 0):
        print("✓ H4V3 fork already includes this upstream revision.")
        return True

    worktree = _candidate(root)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", "-b", CANDIDATE_BRANCH,
         str(worktree), main_sha)
    _pending_file(root).write_text(json.dumps({"base": main_sha, "upstream": upstream}) + "\n", encoding="utf-8")
    try:
        _apply_delta(root, worktree, previous, upstream)
        _publish_candidate(root, worktree, upstream, main_sha, validate=True)
    except ForkUpdateError:
        print(f"⚠ Stopped: main and fork left unchanged. Candidate: {worktree}")
        print("  Resolve conflicts in the candidate or run:")
        print("  python -m hermes_cli.h4v3_fork_update abort")
        raise
    _git(root, "worktree", "remove", str(worktree))
    _git(root, "branch", "-D", CANDIDATE_BRANCH)
    _pending_file(root).unlink(missing_ok=True)
    return True


def check_fork_updates(root: Path) -> dict | None:
    """Report only changes the native updater can install from origin/main.

    Upstream drift is informational until an explicit reviewed sync publishes
    it to the fork. An unknown commit distance must not be called 1 commit.
    """
    if not _enabled(root):
        return None
    if (_url(_out(root, "remote", "get-url", "origin")) != _url(FORK_URL)
            or _url(_out(root, "remote", "get-url", "upstream")) != _url(UPSTREAM_URL)
            or _out(root, "branch", "--show-current") != FORK_BRANCH):
        raise ForkUpdateError("fork update check requires the configured main checkout")

    def remote_head(remote: str, branch: str) -> str:
        lines = _out(root, "ls-remote", "--heads", remote, branch).splitlines()
        if len(lines) != 1:
            raise ForkUpdateError(f"cannot verify {remote}/{branch} against remote")
        rev, ref = lines[0].split()
        if ref != f"refs/heads/{branch}" or not SHA_RE.fullmatch(rev):
            raise ForkUpdateError(f"invalid {remote}/{branch} revision")
        return rev

    local = _sha(root, FORK_BRANCH)
    remote_main = remote_head("origin", FORK_BRANCH)
    upstream = remote_head("upstream", "main")
    previous = _last_synced(root, local)
    upstream_pending = previous != upstream if previous else True
    # SOURCE_CHECK.UPDATE_AVAILABLE_NO_COUNT = -1; remote SHA alone does not
    # prove the number of reachable commits without fetching.
    behind = -1 if local != remote_main else 0
    return {"behind": behind, "updateAvailable": behind != 0,
            "targetSha": remote_main, "commits": [],
            "upstreamPending": upstream_pending,
            "upstreamSyncedSha": previous,
            "upstreamTargetSha": upstream}


def maybe_prepare_update(args, root: Path) -> bool:
    """Route an activated fork through the official origin/main updater.

    Return whether this is the H4V3 fork mode, so the native updater can keep its
    automatic upstream synchronization disabled for this explicitly managed fork.
    No upstream fetch, squash, push or checkout mutation occurs in this hook.
    """
    if not _enabled(root):
        return False
    if getattr(args, "branch", None) or getattr(args, "channel", None):
        raise SystemExit("H4V3 fork update uses the main branch; omit --branch/--channel")
    try:
        _verify(root)
    except ForkUpdateError as exc:
        print(f"✗ H4V3 fork update stopped safely: {exc}")
        raise SystemExit(1) from exc
    args.branch = FORK_BRANCH
    print("✓ H4V3 fork: using Hermes native update from origin/main.")
    print("  To integrate upstream/main into the fork, run h4v3_fork_update sync separately.")
    return True


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "sync", "abort", "continue"))
    parser.add_argument("--discard", action="store_true", help="Discard ONLY the isolated candidate worktree")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    if not _enabled(root):
        print("H4V3 fork mode is not enabled")
        return 2
    if args.action == "status":
        local = _sha(root, FORK_BRANCH)
        print("Fork main:", local[:12])
        print("Synced upstream:", (_last_synced(root, local) or "unrecorded")[:12])
        print("Pending integration:", _candidate(root) if _pending(root) else "none")
        print("Native update: origin/main only; upstream merge: explicit sync.")
        return 0
    if args.action == "sync":
        try:
            prepare_update(root)
        except ForkUpdateError as exc:
            print(f"✗ H4V3 upstream integration stopped safely: {exc}")
            return 1
        print("Upstream integration prepared. Run hermes update to install fork main.")
        return 0
    candidate = _candidate(root)
    if not candidate.is_dir():
        print("No candidate worktree")
        return 2
    if args.action == "abort":
        if not args.discard:
            print("Abort discards manual conflict resolution. Repeat with --discard to confirm.")
            return 2
        _git(root, "worktree", "remove", "--force", str(candidate))
        _git(root, "branch", "-D", CANDIDATE_BRANCH)
        _pending_file(root).unlink(missing_ok=True)
        print("Isolated candidate discarded; main and fork unchanged.")
        return 0
    _verify(root)
    metadata = json.loads(_pending_file(root).read_text(encoding="utf-8-sig"))
    if (_sha(root, FORK_BRANCH) != metadata["base"] or
            _sha(root, f"refs/remotes/origin/{FORK_BRANCH}") != metadata["base"] or
            _sha(root, CANDIDATE_BRANCH) != metadata["base"]):
        raise ForkUpdateError("main or candidate changed during conflict resolution")
    status = _out(root, "status", "--porcelain=v1", "--untracked-files=all", cwd=candidate)
    if any(row.startswith("?? ") for row in status.splitlines()):
        raise ForkUpdateError("untracked candidate files must be staged or removed before continue")
    if any(row[1] != " " for row in status.splitlines()):
        # All conflict resolution must be staged. A staged-only change has
        # porcelain's second column blank.
        raise ForkUpdateError("stage all conflict resolutions before continue")
    _publish_candidate(root, candidate, metadata["upstream"], metadata["base"], validate=True)
    _git(root, "worktree", "remove", str(candidate))
    _git(root, "branch", "-D", CANDIDATE_BRANCH)
    _pending_file(root).unlink(missing_ok=True)
    print("Now click Hermes Update again to activate the published squash commit.")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
