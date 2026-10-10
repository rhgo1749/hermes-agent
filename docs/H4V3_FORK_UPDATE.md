# H4V3 fork-first Hermes updates

## Authority and update-button contract

- **Docker's running source:** `/ws/hermes-agent`, Git branch `production`.
- **`origin`:** `rhgo1749/hermes-agent` (the owner's fork, source of truth).
- **`upstream`:** `NousResearch/hermes-agent` (fetch-only).
- Fork `main` remains a separate PR/development branch; **fork `production` is the deployed branch**.
- Normal **Hermes Update**, including the Gateway's update action, checks `upstream/main`, prepares a squash commit in a separate Git worktree, publishes that commit via **non-force** push to `origin/production`, then delegates to the **existing Hermes update pipeline** for fetch, checkout, PM, gateway draining, restart, and health verification.
- Prior production commit SHAs are not rewritten: each upstream advance is represented by one new commit with `H4V3-Upstream-SHA:` trailer. Only the difference since the preceding upstream revision is applied on subsequent updates; repeated clicks do not reapply an old upstream patch.
- A conflict (including upstream history rewrites, unexpected local changes, concurrent fork pushes, staging errors, or invalid Python source) **aborts before changing local production or its fork ref**. The isolated candidate stays for inspection. No conflict is auto-resolved. `rerere` remembers manual resolutions but does not automatically stage them.
- A successful squash *publication* precedes the regular Hermes update; if the latter fails, the fork is ahead but the existing running checkout remains available. **Click Update again** after addressing the stock updater's reported failure.

## One-time installation (host user)

This is a deliberate one-time migration from the exact current `local-hotfix/runtime-minimal-carry-20261006` branch. The script refuses unrelated dirty paths. It creates a rescue branch, commits the deployed Core carry and update hook, assigns the fork as `origin`, makes official NousResearch `upstream`, and pushes a **new `production` branch without forcing**. It does not delete or rewrite `main`, stop the gateway, or alter Kanban state.

```bash
cd /home/gonus/hermes-cloudcli/HermesWorkspace/hermes-agent
python3 scripts/h4v3_fork_bootstrap.py
git branch -vv
git remote -v
```

It is idempotent once the production branch and remotes are initialized. The activation marker is intentionally in the *local Git directory*, so changing to a different checkout or copying a fork without explicitly setting it up cannot silently enable special deployment handling.

## Day-to-day usage

Use the ordinary Hermes Update button or `hermes update`. Avoid `git pull upstream main`, forced pushes or direct `hermes update --branch main` in the production checkout. Any unexpected local edits must first be reviewed and committed.

For quick status:

```bash
cd /home/gonus/hermes-cloudcli/HermesWorkspace/hermes-agent
python3 -m hermes_cli.h4v3_fork_update status
```

## When a conflict happens

Nothing in the live checkout or fork production has changed. Inspect:

```bash
cd /home/gonus/hermes-cloudcli/HermesWorkspace/hermes-agent
git -C .worktrees/h4v3-upstream-squash status
git -C .worktrees/h4v3-upstream-squash diff --check
```

Resolve conflict markers **only in this isolated candidate** and stage the resolved files using `git -C .worktrees/h4v3-upstream-squash add <path>`. Then:

```bash
python3 -m hermes_cli.h4v3_fork_update continue
hermes update
```

`continue` revalidates the original upstream and fork commits before publishing. If upstream or fork moved, stop and start from a fresh reviewed base. To throw away just the candidate (never live production), run `python3 -m hermes_cli.h4v3_fork_update abort --discard`. If you prefer a manual rebase, work **only in a separate scratch worktree**, not in `production`: a rebase rewrites local commit IDs and defeats the requested commit-preservation guarantee. When a rebase is necessary, explicitly review the resulting commits and integrate them back via an independent, non-force PR.

## Notes

The hook is intentionally narrow and opt-in. It runs only if the marker exists and the checkout is `production` with the exact expected remotes; explicit alternate update channels are rejected to avoid silently bypassing the fork. The stock Hermes update system continues to own restart policy, package management, receipts, and rollback. Git history and a tree-level conflict test cannot prove the application will run with a newer dependency graph: review the existing updater's health and PM results.
