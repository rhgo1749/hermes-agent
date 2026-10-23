"""Runner-capacity changes must not alter the CI coverage or security contract."""

from importlib import import_module
from pathlib import Path
from typing import Any

from tests.ci import _gha_expr as gha

yaml: Any = import_module("hermes_yaml")
ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


def _workflow(name: str) -> dict[str, Any]:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _on(workflow: dict) -> dict:
    return workflow.get("on", workflow.get(True, {})) or {}


def _step(job: dict[str, Any], name: str) -> dict[str, Any]:
    return next(step for step in job["steps"] if step.get("name") == name)


def _runner(value: str, repository: str, matrix: dict[str, Any] | None = None) -> str:
    return gha.render(value, {"github": {"repository": repository}, "matrix": matrix or {}})


def test_linux_standard_runners_keep_slices_and_test_coverage_contract() -> None:
    workflow = _workflow("tests.yml")
    jobs = workflow["jobs"]
    upstream = "NousResearch/hermes-agent"
    fork = "rhgo1749/hermes-agent"

    assert workflow["permissions"] == {"contents": "read"}
    inputs = _on(workflow)["workflow_call"]["inputs"]
    assert {name: spec["type"] for name, spec in inputs.items()} == {
        "commit": "string",
        "e2e": "boolean",
        "e2e_upgrade": "boolean",
        "strict_acceptance": "string",
    }

    unit = jobs["test"]
    assert _runner(unit["runs-on"], fork) == "ubuntu-latest"
    assert _runner(unit["runs-on"], upstream) == "ubuntu-latest-32-core"
    assert gha.render(unit["timeout-minutes"], {"github": {"repository": fork}}) == 60
    assert gha.render(unit["timeout-minutes"], {"github": {"repository": upstream}}) == 30
    assert unit["name"] == "Run tests (${{ matrix.slice }}/2)"
    assert unit["strategy"]["matrix"]["slice"] == [1, 2]
    unit_workers = _step(unit, "Run tests")["env"]["HERMES_TEST_WORKERS"]
    assert gha.render(unit_workers, {"github": {"repository": fork}}) == "4"
    assert gha.render(unit_workers, {"github": {"repository": upstream}}) == "32"

    e2e = jobs["e2e"]
    assert _runner(e2e["runs-on"], fork) == "ubuntu-latest"
    assert _runner(e2e["runs-on"], upstream) == "ubuntu-latest-32-core"
    assert _step(e2e, "Run e2e tests")["env"]["HERMES_TEST_WORKERS"] == "3"

    upgrade = jobs["e2e-upgrade"]
    assert _runner(upgrade["runs-on"], fork) == "ubuntu-latest"
    assert _runner(upgrade["runs-on"], upstream) == "ubuntu-latest-32-core"
    workers = _step(upgrade, "Run upgrade e2e tests (${{ matrix.shard }})")["env"]["HERMES_TEST_WORKERS"]
    assert gha.render(workers, {"github": {"repository": fork}}) == "4"
    assert gha.render(workers, {"github": {"repository": upstream}}) == "6"


def test_os_standard_runners_preserve_markers_arm64_and_zero_test_guard() -> None:
    workflow = _workflow("tests-os.yml")
    jobs = workflow["jobs"]
    matrix = jobs["os-tests"]["strategy"]["matrix"]["include"]
    rows = {row["name"]: row for row in matrix}
    upstream = "NousResearch/hermes-agent"
    fork = "rhgo1749/hermes-agent"

    assert workflow["permissions"] == {"contents": "read"}
    inputs = _on(workflow)["workflow_call"]["inputs"]
    assert {name: spec["type"] for name, spec in inputs.items()} == {
        "commit": "string",
        "desktop_updater": "boolean",
        "e2e": "boolean",
        "e2e_upgrade": "boolean",
        "strict_acceptance": "string",
    }
    for name, slice_name in (("macOS-only tests (1/2)", "1/2"), ("macOS-only tests (2/2)", "2/2")):
        row = rows[name]
        assert row == {"name": name, "runner": "macos-latest", "marker": "macos", "slice": slice_name}

    x64, arm64 = rows["Windows-only tests"], rows["Windows-only tests (arm64)"]
    assert _runner(x64["runner"], fork) == "windows-latest"
    assert _runner(x64["runner"], upstream) == "windows-latest-32-core"
    assert _runner(arm64["runner"], fork) == "windows-11-arm"
    assert _runner(arm64["runner"], upstream) == "windows-latest-32-arm-core"
    assert x64["marker"] == arm64["marker"] == "windows"
    assert arm64["timeout"] == 60

    os_step = _step(jobs["os-tests"], "Run ${{ matrix.marker }} tests")
    workers = os_step["env"]["HERMES_TEST_WORKERS"]
    matrix_ctx = {"matrix": {"marker": "windows"}}
    assert gha.render(workers, {**matrix_ctx, "github": {"repository": fork}}) == "4"
    assert gha.render(workers, {**matrix_ctx, "github": {"repository": upstream}}) == "16"
    command = os_step["run"]
    assert "scripts/ci/list_os_marked_tests.py" in command
    assert 'if [ ! -s "$LIST" ]; then' in command
    assert 'scripts/run_tests.sh --files "$FILES" --' in command
    assert '-m "platforms and not integration"' in command

    windows_e2e = jobs["e2e-windows"]
    assert _runner(windows_e2e["runs-on"], fork) == "windows-latest"
    assert _runner(windows_e2e["runs-on"], upstream) == "windows-latest-32-core"
    e2e_step = _step(windows_e2e, "Run Windows E2E suite")
    e2e_workers = e2e_step["env"]["HERMES_TEST_WORKERS"]
    assert gha.render(e2e_workers, {"github": {"repository": fork}}) == "4"
    assert gha.render(e2e_workers, {"github": {"repository": upstream}}) == "6"
    assert '-m "platforms and integration"' in e2e_step["run"]

    reusable = jobs["install-update-e2e"]
    assert reusable["uses"] == "./.github/workflows/windows-install-update-e2e.yml"
    assert reusable["with"] == {
        "commit": "${{ inputs.commit }}",
        "strict_acceptance": "${{ inputs.strict_acceptance }}",
    }


def test_javascript_and_rust_jobs_select_standard_runners_for_the_fork() -> None:
    upstream = "NousResearch/hermes-agent"
    fork = "rhgo1749/hermes-agent"

    js = _workflow("js-tests.yml")["jobs"]["check"]
    assert _runner(js["runs-on"], fork) == "ubuntu-latest"
    assert _runner(js["runs-on"], upstream) == "ubuntu-latest-32-core"
    assert gha.render(js["timeout-minutes"], {"github": {"repository": fork}}) == 45
    assert gha.render(js["timeout-minutes"], {"github": {"repository": upstream}}) == 30

    rust = _workflow("rust-tests.yml")["jobs"]["bootstrap-installer"]
    assert _runner(rust["runs-on"], fork) == "ubuntu-latest"
    assert _runner(rust["runs-on"], upstream) == "ubuntu-latest-32-core"


def test_ci_aggregate_name_and_permission_contract_are_unchanged() -> None:
    ci = _workflow("ci.yaml")
    aggregate = ci["jobs"]["all-checks-pass"]

    assert aggregate["name"] == "All required checks pass (v2)"
    assert "js-tests" in aggregate["needs"]
    assert "rust-tests" in aggregate["needs"]
    assert ci["permissions"] == {"contents": "read"}
    assert aggregate["permissions"] == {"contents": "read"}
