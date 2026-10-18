# H4V3 fork: native Hermes updates and explicit upstream integration

## Authority and update-button contract

- **Docker's running source:** `/ws/hermes-agent`, Git branch `production`.
- **`origin`:** `rhgo1749/hermes-agent` (the owner's fork, source of truth).
- **`upstream`:** `NousResearch/hermes-agent` (fetch-only).
- Fork `main` remains a separate PR/development branch; **fork `production` is the deployed branch**.
- Normal **Hermes Update**, including the Gateway's update action, uses the **official Hermes source update pipeline** against `origin/production` only. It preserves the fork branch and runs stock fetch, checkout, PM, gateway draining, restart and health verification. It never implicitly squashes or pushes upstream.
- `upstream/main` is integrated **separately and explicitly** with `python3 -m hermes_cli.h4v3_fork_update sync` only after review. That command uses the existing isolated squash candidate and non-force publish to `origin/production`. Run normal `hermes update` afterward to install the reviewed commit.
- Official Hermes **main-branch fork sync** cannot replace separate integration: it fast-forwards upstream only when the fork has no independent commits; for this divergent fork it skips the merge rather than discarding local changes.
- Official Hermes also supports `updates.parked_branch_strategy: update_in_place`: when the updater targets `origin/main` while checked out on a different, parked branch, it can merge that target into the current branch and preserve commits. **This is not a drop-in switch here**: `origin` is the H4V3 fork, the active branch `production` is the selected update target, and prior official changes were squashed (not merged as ancestry). Using this native strategy to follow *official* main requires a separately reviewed remote/branch migration and a one-time real merge/conflict audit, plus updating fork publishing and H4V3 consumers. Do not flip remotes or that config in production just to try it.
- Prior production commit SHAs are not rewritten: each upstream advance is represented by one new commit with `H4V3-Upstream-SHA:` trailer. Only the difference since the preceding upstream revision is applied on subsequent updates; repeated clicks do not reapply an old upstream patch.
- During explicit upstream `sync`, a conflict (including upstream history rewrites, unexpected local changes, concurrent fork pushes, staging errors, or invalid Python source) **aborts before changing local production or its fork ref**. The isolated candidate stays for inspection. No conflict is auto-resolved. `rerere` remembers manual resolutions but does not automatically stage them.
- A successful explicit squash *publication* precedes the regular Hermes update; if the latter fails, the fork is ahead but the existing running checkout remains available. **Click Update again** after addressing the stock updater's reported failure.
- For this fork, `hermes --version` reports **synced upstream SHA** plus local fork SHA; release tags on squashed ancestry do **not** prove the fork runs a particular upstream semver tag. A published upstream change alone is not an available native update until merged into `origin/production`.

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

Use the ordinary Hermes Update button or `hermes update` to install changes **already merged into the fork's `origin/production`**, using the official Hermes update implementation. The update notification reports fork-origin drift only; upstream drift is informational until integration is reviewed and published. Avoid `git pull upstream main`, forced pushes or direct `hermes update --branch main` in the production checkout. Unexpected tracked local edits must first be reviewed and committed.

For status and separate, reviewed upstream integration:

```bash
cd /home/gonus/hermes-cloudcli/HermesWorkspace/hermes-agent
python3 -m hermes_cli.h4v3_fork_update status
# After reviewing an upstream delta and checking expected H4V3 lifecycle behavior:
python3 -m hermes_cli.h4v3_fork_update sync
# Only after sync successfully publishes the fork production commit:
hermes update
```

The `sync` command is an explicit mutation: it may publish a new fork commit after its syntax/conflict preflight, **not** after a full runtime E2E. Prefer integrating via a separately reviewed GitHub PR when changes touch Kanban, ACP, cron, or session lifecycle; then `hermes update` installs that PR after merge.

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

The opt-in hook now **only routes** normal updates to `origin/production` and refuses an unexpected branch, remote or dirty tracked tree. It does not fetch, squash, or push upstream. Stock Hermes continues to own restart policy, package management, receipts, and rollback. The explicit upstream-sync function remains available for this divergent fork. Git history and a tree-level conflict test cannot prove the application will run with a newer dependency graph: review updater health and PM results.
