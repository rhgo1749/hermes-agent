"""Fork-first update hook for the local H4V3 Hermes installation.

The stock updater still owns dependency selection, backups, restart and health
checks. This module only prepares an immutable, squash-integrated commit on the
fork's production branch BEFORE the stock updater fetches that branch.
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
PRODUCTION = "production"
CANDIDATE_BRANCH = "h4v3/upstream-squash-candidate"
CANDIDATE_DIR = ".worktrees/h4v3-upstream-squash"
MARKER = "h4v3-fork-update.json"
TRAILER = "H4V3-Upstream-SHA: "
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class ForkUpdateError(RuntimeError):
    """Refuse publication without disturbing the production checkout."""


def _git(root: Path, *args: str, cwd: Path | None = None, input_bytes: bytes | None = None,
         check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", str(cwd or root), *args],
                          input=input_bytes, capture_output=True, check=False)
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
    if _out(root, "symbolic-ref", "--short", "HEAD") != PRODUCTION:
        raise ForkUpdateError("running checkout must be on production branch")
    if _out(root, "status", "--porcelain=v1", "--untracked-files=no"):
        raise ForkUpdateError("production has uncommitted changes; commit them before updating")


def _last_synced(root: Path, production_sha: str) -> str | None:
    message = _out(root, "log", "-1", "--format=%B", "--grep=H4V3-Upstream-SHA:",
                   production_sha)
    rows = re.findall(r"^H4V3-Upstream-SHA:\s*([0-9a-f]{40})$", message, re.M)
    return rows[-1] if rows else None


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
                raise ForkUpdateError("upstream delta conflicts with H4V3 production")
    else:
        result = _git(root, "merge", "--squash", upstream, cwd=candidate, check=False)
        if result.returncode:
            raise ForkUpdateError("initial upstream squash conflicts with H4V3 production")


def _resolve_upstream(root: Path, production_sha: str) -> tuple[str, str | None, str]:
    # Only fetch the two branch heads, not thousands of task branches.
    _git(root, "fetch", "--no-tags", "origin",
         "+refs/heads/production:refs/remotes/origin/production")
    _git(root, "fetch", "--no-tags", "upstream",
         "+refs/heads/main:refs/remotes/upstream/main")
    fork_sha = _sha(root, "refs/remotes/origin/production")
    if production_sha != fork_sha and _git(
        root, "merge-base", "--is-ancestor", production_sha, fork_sha,
        check=False).returncode:
        raise ForkUpdateError("fork production diverged; reconcile before update")
    upstream = _sha(root, "refs/remotes/upstream/main")
    previous = _last_synced(root, production_sha)
    if previous:
        if _git(root, "merge-base", "--is-ancestor", previous, upstream, check=False).returncode:
            raise ForkUpdateError("upstream history was rewritten; rebase/reconcile manually")
    return upstream, previous, fork_sha


def _verify_candidate(root: Path, candidate: Path) -> None:
    unmerged = _out(root, "ls-files", "-u", cwd=candidate)
    if unmerged:
        raise ForkUpdateError("unresolved conflicts in isolated integration worktree")
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
                       production_sha: str, *, validate: bool) -> None:
    if validate:
        _verify_candidate(root, candidate)
    # A marked commit may have no tree delta (upstream already merged
    # elsewhere); the provenance trailer still prevents replay next time.
    _git(root, "add", "-u", cwd=candidate)
    _git(root, "commit", "--allow-empty", "-m",
         f"chore(h4v3): squash upstream {upstream_sha[:12]}",
         "-m", f"{TRAILER}{upstream_sha}", cwd=candidate)
    if _git(root, "merge-base", "--is-ancestor", production_sha,
            _sha(root, CANDIDATE_BRANCH), check=False).returncode:
        raise ForkUpdateError("candidate no longer preserves production commits")
    # Never force push. A concurrent fork push must reject this update.
    _git(root, "push", "origin", f"{CANDIDATE_BRANCH}:refs/heads/{PRODUCTION}")
    print("✓ Upstream changes squash-published to fork production.")
    print("  Existing production commits preserved; stock Hermes updater will now apply the new commit.")


def prepare_update(root: Path) -> bool:
    """Return True only for an enabled fork. Refuse before stock updater mutates production."""
    if not _enabled(root):
        return False
    _verify(root)
    if _pending(root):
        raise ForkUpdateError(f"Previous integration pending at {_candidate(root)}; "
                              "resolve/abort it before another update")
    production_sha = _sha(root, PRODUCTION)
    upstream, previous, fork_sha = _resolve_upstream(root, production_sha)
    if fork_sha != production_sha:
        print("✓ Fork production already has a newer commit; letting the stock updater fast-forward it.")
        return True
    if previous == upstream or (not previous and
                               _git(root, "merge-base", "--is-ancestor", upstream,
                                    production_sha, check=False).returncode == 0):
        print("✓ H4V3 fork already includes this upstream revision.")
        return True

    worktree = _candidate(root)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", "-b", CANDIDATE_BRANCH,
         str(worktree), production_sha)
    _pending_file(root).write_text(json.dumps({"base": production_sha, "upstream": upstream}) + "\n")
    try:
        _apply_delta(root, worktree, previous, upstream)
        _publish_candidate(root, worktree, upstream, production_sha, validate=True)
    except ForkUpdateError:
        print(f"⚠ Stopped: production and fork left unchanged. Candidate: {worktree}")
        print("  Resolve conflicts in the candidate or run:")
        print(f"  python -m hermes_cli.h4v3_fork_update abort")
        raise
    _git(root, "worktree", "remove", str(worktree))
    _git(root, "branch", "-D", CANDIDATE_BRANCH)
    _pending_file(root).unlink(missing_ok=True)
    return True


def check_fork_updates(root: Path) -> dict | None:
    """Read-only dashboard status for the same upstream/fork refs Update uses."""
    if not _enabled(root):
        return None
    if (_url(_out(root, "remote", "get-url", "origin")) != _url(FORK_URL)
            or _url(_out(root, "remote", "get-url", "upstream")) != _url(UPSTREAM_URL)
            or _out(root, "branch", "--show-current") != PRODUCTION):
        raise ForkUpdateError("fork update check requires the configured production checkout")

    def remote_head(remote: str, branch: str) -> str:
        lines = _out(root, "ls-remote", "--heads", remote, branch).splitlines()
        if len(lines) != 1:
            raise ForkUpdateError(f"cannot verify {remote}/{branch} against remote")
        rev, ref = lines[0].split()
        if ref != f"refs/heads/{branch}" or not SHA_RE.fullmatch(rev):
            raise ForkUpdateError(f"invalid {remote}/{branch} revision")
        return rev

    local = _sha(root, PRODUCTION)
    remote_production = remote_head("origin", PRODUCTION)
    upstream = remote_head("upstream", "main")
    previous = _last_synced(root, local)
    needs_upstream = (previous != upstream if previous else
                      _git(root, "merge-base", "--is-ancestor", upstream, local,
                           check=False).returncode != 0)
    behind = int(local != remote_production or needs_upstream)
    return {"behind": behind, "updateAvailable": bool(behind),
            "targetSha": upstream if needs_upstream else remote_production,
            "commits": []}


def maybe_prepare_update(args, root: Path) -> None:
    """Hook for the normal Hermes update button/CLI, before the stock fetch."""
    if not _enabled(root):
        return
    if getattr(args, "branch", None) or getattr(args, "channel", None):
        raise SystemExit("H4V3 fork update uses the production branch; omit --branch/--channel")
    try:
        prepare_update(root)
    except ForkUpdateError as exc:
        print(f"✗ H4V3 upstream update stopped safely: {exc}")
        raise SystemExit(1) from exc
    args.branch = PRODUCTION


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "abort", "continue"))
    parser.add_argument("--discard", action="store_true", help="Discard ONLY the isolated candidate worktree")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    if not _enabled(root):
        print("H4V3 fork mode is not enabled")
        return 2
    if args.action == "status":
        print("Production:", _sha(root, PRODUCTION)[:12])
        print("Integration:", _candidate(root) if _pending(root) else "none")
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
        print("Isolated candidate discarded; production and fork unchanged.")
        return 0
    _verify(root)
    metadata = json.loads(_pending_file(root).read_text())
    if (_sha(root, PRODUCTION) != metadata["base"] or
            _sha(root, "refs/remotes/origin/production") != metadata["base"] or
            _sha(root, CANDIDATE_BRANCH) != metadata["base"]):
        raise ForkUpdateError("production or candidate changed during conflict resolution")
    if _out(root, "status", "--porcelain=v1", "--untracked-files=no", cwd=candidate).splitlines():
        # All conflict resolution must be staged. A staged-only change has
        # porcelain's second column blank; only the unstaged column blocks.
        status = _out(root, "status", "--porcelain=v1", "--untracked-files=no", cwd=candidate)
        if any(row[1] != " " for row in status.splitlines()):
            raise ForkUpdateError("stage all conflict resolutions before continue")
    _publish_candidate(root, candidate, metadata["upstream"], metadata["base"], validate=True)
    _git(root, "worktree", "remove", str(candidate))
    _git(root, "branch", "-D", CANDIDATE_BRANCH)
    _pending_file(root).unlink(missing_ok=True)
    print("Now click Hermes Update again to activate the published squash commit.")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
