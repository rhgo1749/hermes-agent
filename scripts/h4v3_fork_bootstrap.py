#!/usr/bin/env python3
"""One-time adoption of the running H4V3 Hermes checkout as fork production.

Safety: fail closed on unfamiliar branch/remotes/changes; never force push,
reset, rebase or restart the gateway. Requires normal host-user Git write access.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
FORK = "https://github.com/rhgo1749/hermes-agent.git"
UPSTREAM = "https://github.com/NousResearch/hermes-agent.git"
SOURCE = "local-hotfix/runtime-minimal-carry-20261006"
FILES = (
    "agent/turn_api_request.py",
    "agent/turn_stop_gates.py",
    "hermes_cli/update_cmd.py",
    "hermes_cli/source_check.py",
    "hermes_cli/h4v3_fork_update.py",
    "tests/agent/test_kanban_terminal_request.py",
    "tests/hermes_cli/test_h4v3_fork_update.py",
    "scripts/h4v3_fork_bootstrap.py",
    "docs/H4V3_FORK_UPDATE.md",
)
MARKER = "h4v3-fork-update.json"


def git(*args: str, check=True):
    p = subprocess.run(["git", "-C", str(ROOT), *args],
                       text=True, capture_output=True)
    if check and p.returncode:
        raise RuntimeError(f"git {args[0]}: {p.stderr.strip()[-800:]}")
    return p


def out(*args: str) -> str:
    return git(*args).stdout.strip()


def safe_remote(name: str, expected: str):
    if out("remote", "get-url", name).removesuffix(".git") != expected.removesuffix(".git"):
        raise RuntimeError(f"unexpected {name} remote; aborting")


def activate():
    current = out("branch", "--show-current")
    if current == "production":
        safe_remote("origin", FORK)
        safe_remote("upstream", UPSTREAM)
        if out("status", "--porcelain=v1", "--untracked-files=no"):
            raise RuntimeError("production has uncommitted tracked changes")
        git("push", "-u", "origin", "production:production")
        marker = Path(out("rev-parse", "--absolute-git-dir")) / MARKER
        marker.write_text(json.dumps({"mode": "fork-first-squash-v1"}) + "\n")
        print("✓ production branch tracking fork; one-click updates enabled.")
        return

    if current != SOURCE:
        raise RuntimeError(f"expected {SOURCE}, found {current}; no changes made")
    safe_remote("origin", UPSTREAM)
    safe_remote("h4v3", FORK)
    if git("show-ref", "--verify", "--quiet", "refs/heads/production", check=False).returncode == 0:
        raise RuntimeError("production already exists; inspect it before setup")
    # Do NOT use out(...): .strip() removes Git porcelain's leading status
    # space (e.g. " M agent/x.py"), corrupting the first filename.
    raw = git("status", "--porcelain=v1", "--untracked-files=all").stdout
    rows = raw.splitlines()
    if any(row.startswith(("R", "C")) for row in rows):
        raise RuntimeError("renames/copies require manual review before migration")
    paths = {row[3:] for row in rows}
    unexpected = paths - set(FILES)
    if unexpected:
        raise RuntimeError("other worker has uncommitted changes: " + ", ".join(sorted(unexpected)))
    if not all((ROOT / f).is_file() for f in FILES):
        raise RuntimeError("workflow files missing")
    # Accept fully committed work, or a small follow-up fix while other
    # workflow files are already committed. Reject any missing workflow file
    # that Git neither tracks nor lists as a pending add.
    missing = [f for f in FILES if f not in paths and
               git("ls-files", "--error-unmatch", "--", f, check=False).returncode != 0]
    if missing:
        raise RuntimeError("missing tracked workflow files: " + ", ".join(missing))
    git("diff", "--check")
    # Stage a reviewed, deterministic list only: never sweep unrelated work.
    # The upstream-sync candidate is built later in an isolated worktree.
    # The live checkout is not reset or rebased here.
    # Offline runtime validation was done while developing this carry.
    if "_kanban_terminal_tool_required" not in (ROOT / FILES[0]).read_text():
        raise RuntimeError("missing live Core stop-guard patch")
    git("fetch", "--no-tags", "h4v3",
        "+refs/heads/main:refs/remotes/h4v3/main")
    if git("merge-base", "--is-ancestor", "HEAD", "h4v3/main", check=False).returncode:
        # Divergence is normal here. We retain ALL local commits by branching
        # at exactly the operational HEAD rather than resetting to fork main.
        print("ℹ Existing local commits differ from fork main; preserving them unchanged.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    git("branch", f"backup/h4v3-before-production-{stamp}", "HEAD")
    git("switch", "-c", "production")
    if rows:
        git("add", "--", *FILES)
        git("diff", "--cached", "--check")
        git("commit", "-m", "chore(h4v3): anchor Docker operation on fork production")
    else:
        print("✓ Reviewed operating changes already committed; preserving existing commit SHAs.")
    git("remote", "rename", "origin", "upstream")
    git("remote", "rename", "h4v3", "origin")
    safe_remote("origin", FORK)
    safe_remote("upstream", UPSTREAM)
    git("remote", "set-url", "--push", "upstream", "PUSH_DISABLED_USE_FORK")
    git("config", "--local", "rerere.enabled", "true")
    git("config", "--local", "rerere.autoupdate", "false")
    git("config", "--local", "pull.ff", "only")
    git("config", "--local", "remote.pushDefault", "origin")
    git("config", "--local", "branch.production.remote", "origin")
    git("config", "--local", "branch.production.merge", "refs/heads/production")
    git("push", "-u", "origin", "production:production")
    marker = Path(out("rev-parse", "--absolute-git-dir")) / MARKER
    marker.write_text(json.dumps({"mode": "fork-first-squash-v1"}) + "\n")
    print("✓ Docker production has a stable Git branch on the H4V3 fork.")
    print("✓ origin = H4V3 fork; upstream = NousResearch (push blocked).")
    print("✓ From now on, normal Hermes Update automatically prepares upstream squashes.")
    print("  Conflict: original production/fork unchanged; inspect the isolated worktree.")


if __name__ == "__main__":
    try:
        activate()
    except (RuntimeError, OSError) as error:
        print("✗ Fork migration stopped:", error, file=sys.stderr)
        sys.exit(1)
