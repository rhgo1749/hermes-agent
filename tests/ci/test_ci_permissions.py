"""CI token permissions are an execution-boundary contract, not workflow decoration."""

from importlib import import_module
from pathlib import Path
from typing import Any

yaml: Any = import_module("hermes_yaml")

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _on(workflow: dict) -> dict:
    return workflow.get("on", workflow.get(True, {})) or {}


def _assert_read_only(requested: dict, caller: dict, where: str) -> None:
    for scope, level in requested.items():
        assert caller.get(scope) == "read", f"{where} exceeds its caller ceiling: {scope}: {level}"
        assert level == "read", f"{where} requests write permission: {scope}: {level}"


def test_pr_ci_defaults_to_read_only_and_scopes_actions_api_access():
    ci = _workflow("ci.yaml")
    jobs = ci["jobs"]

    assert ci["permissions"] == {"contents": "read"}
    assert "pull_request_target" not in _on(ci)
    assert jobs["detect"]["permissions"] == {"contents": "read"}
    assert jobs["js-tests"]["permissions"] == {"contents": "read"}
    assert jobs["supply-chain"]["permissions"] == {"contents": "read"}
    assert jobs["all-checks-pass"]["permissions"] == {"contents": "read"}
    assert jobs["ci-timings"]["permissions"] == {"contents": "read", "actions": "read"}

    for job_id, job in jobs.items():
        if "permissions" in job:
            _assert_read_only(job["permissions"], job["permissions"], f"ci.yaml/{job_id}")


def test_reusable_workflow_permissions_stay_within_each_caller_ceiling():
    ci = _workflow("ci.yaml")

    for job_id, job in ci["jobs"].items():
        uses = job.get("uses", "")
        if not uses.startswith("./.github/workflows/"):
            continue

        caller = job.get("permissions", ci["permissions"])
        callee = _workflow(uses.rsplit("/", 1)[1])
        declared = callee.get("permissions", caller)
        _assert_read_only(declared, caller, f"ci.yaml/{job_id} caller")

        for child_id, child_job in callee.get("jobs", {}).items():
            _assert_read_only(
                child_job.get("permissions", declared),
                caller,
                f"{uses}/{child_id}",
            )
            assert "secrets" not in child_job, f"{uses}/{child_id} passes secrets"
        assert "secrets" not in job, f"ci.yaml/{job_id} passes reusable-workflow secrets"
        assert "secrets" not in _on(callee).get("workflow_call", {}), (
            f"{uses} declares a workflow_call secret"
        )

    stable_release = _workflow("stable-release.yml")
    stable_ci = stable_release["jobs"]["ci"]
    assert stable_ci["permissions"] == {"contents": "read", "actions": "read"}
    _assert_read_only(ci["permissions"], stable_ci["permissions"], "stable-release.yml/ci")
    _assert_read_only(
        ci["jobs"]["ci-timings"]["permissions"],
        stable_ci["permissions"],
        "stable-release.yml/ci/ci-timings",
    )


def test_only_trusted_default_branch_commenter_has_pull_request_write():
    ci = _workflow("ci.yaml")
    supply_chain = _workflow("supply-chain-audit.yml")
    stable_release = _workflow("stable-release.yml")
    reporter = _workflow("ci-review-comment.yml")
    reporter_on = _on(reporter)

    assert "pull-requests" not in ci["permissions"]
    assert "pull-requests" not in supply_chain["permissions"]
    assert "pull-requests" not in stable_release["jobs"]["ci"]["permissions"]
    assert reporter_on["workflow_run"]["workflows"] == ["CI"]
    assert reporter["permissions"] == {
        "contents": "read",
        "actions": "read",
        "pull-requests": "write",
    }
    assert "head_repository.full_name == github.repository" in reporter["jobs"]["comment"]["if"]

    checkout = next(
        step for step in reporter["jobs"]["comment"]["steps"]
        if step.get("uses", "").startswith("actions/checkout@")
    )["with"]
    assert checkout["ref"] == "${{ github.event.repository.default_branch }}"
    assert checkout["persist-credentials"] is False

    scan_steps = supply_chain["jobs"]["scan"]["steps"]
    assert all("GH_TOKEN" not in step.get("env", {}) for step in scan_steps)
