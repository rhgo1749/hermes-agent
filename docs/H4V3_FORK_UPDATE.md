# H4V3 Hermes fork — single-main source and updates

## Canonical authority

- **Target after migration:** `/ws/hermes-agent` is checked out on `main`, tracking the owner's fork `origin/main` (`rhgo1749/hermes-agent`).
- `upstream/main` (`NousResearch/hermes-agent`) is a **separate source of upstream changes**, not a runtime/deployment target.
- `production` is the *former* deployed branch. Preserve its last confirmed SHA as a rollback reference through cutover. **Never delete it before on-host rollback and post-cutover checks pass.**
- The active Docker checkout is not changed simply by merging the migration PR. Git/PM/restart changes require a separately controlled maintenance operation.

## Normal update contract

Normal `hermes update` and the Gateway update action use the stock Hermes update transaction (fetch, checkout, PM, drain, restart, health and rollback) with `origin/main` as the selected fork branch. The opt-in fork hook validates the expected remotes and a clean `main` checkout, and sets `args.branch = "main"` before passing control to the stock updater. It **does not implicitly fetch, squash, push, or merge upstream**.

The native source-update notification measures drift against `origin/main` only. Official upstream drift is informational, and must not be misrepresented as a native update already present in the fork.

### Explicit upstream integration

After reviewing changes against H4V3 Cron, ACP, Kanban, session/memory, and Gateway contracts, prefer a dedicated integration PR to the fork's `main`. The legacy explicit upstream-squash helper remains available:

```bash
python3 -m hermes_cli.h4v3_fork_update status
python3 -m hermes_cli.h4v3_fork_update sync
# Only after that reviewed sync has published successfully:
hermes update
```

`sync` uses an isolated candidate worktree, verifies the previous recorded `H4V3-Upstream-SHA` and fork ancestry, rejects unexpected concurrent movement, and publishes *only to the fork's main* without force-pushing. This is not an approval to run an automatic upstream push or to treat syntax checking as runtime validation. Conflicts remain in the candidate worktree for operator review:

```bash
git -C .worktrees/h4v3-upstream-squash status
git -C .worktrees/h4v3-upstream-squash diff --check
python3 -m hermes_cli.h4v3_fork_update continue
# Or, explicitly discard only the isolated candidate:
python3 -m hermes_cli.h4v3_fork_update abort --discard
```

The fork's squash ancestry and release tags do not prove which upstream release has actually been integrated. Version displays report recorded upstream SHA and local SHA; an unrecorded or older trailer is not a claim of feature parity.

## Cutover checklist (once PR and tests are accepted)

1. Record the live `production` commit SHA and verify it is still published at `origin/production`. Keep the branch as the rollback anchor.
2. Confirm the reviewed integration merge is at the current `origin/main`; check clean worktree and active jobs/gateway shutdown conditions.
3. Transition **only the canonical host checkout** `/home/gonus/hermes-cloudcli/HermesWorkspace/hermes-agent` to track `origin/main`, using the stock Hermes update/deploy transaction and its rollback procedure. Do not repoint a running process by casually changing a symlink or overwriting the checkout.
4. Verify Gateway runtime provenance, session continuation, HGK Kanban worker lifecycle, API session-key identity, Cron drain/one-shots, ACP pairing, memory hooks, and update/status routing. Running process modules must correspond to the new commit after the controlled restart.
5. Only after successful health/rollback confirmation, retire the old `production` branch and stale development refs. Avoid deleting branches referenced by live tasks or active worktrees.

The legacy `scripts/h4v3_fork_bootstrap.py` is **historical**. It was written for the one-time `production` setup and **must not be rerun** as part of the new main-only flow. It is not a cutover tool.
