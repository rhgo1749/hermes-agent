"""Tests for scripts/ci/live_comment.py run selection.

The poller now reports on a run it is not part of, and merges jobs from
sibling runs of the same commit (the Docker image build, which left ci.yml
to stop holding the CI run open). ``select_watched_runs`` decides which
sibling runs count.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "live_comment.py"
_spec = importlib.util.spec_from_file_location("live_comment", _PATH)
if _spec is None or _spec.loader is None:
    raise ImportError("Failed to load live_comment.py")
_mod = importlib.util.module_from_spec(_spec)
sys.modules["live_comment"] = _mod
_spec.loader.exec_module(_mod)

select_watched_runs = _mod.select_watched_runs
classify_jobs = _mod.classify_jobs

DOCKER = "Docker Build, Test, and Publish"


def _run(run_id: int, name: str, created_at: str) -> dict:
    return {"id": run_id, "name": name, "created_at": created_at}


def test_selects_only_named_workflows():
    runs = [
        _run(1, DOCKER, "2026-08-08T10:00:00Z"),
        _run(2, "Deploy site", "2026-08-08T10:00:00Z"),
        _run(3, "CI", "2026-08-08T10:00:00Z"),
    ]
    selected = select_watched_runs(runs, [DOCKER])
    assert [r["id"] for r in selected] == [1]


def test_keeps_newest_attempt_per_workflow():
    """A rerun makes a second run for the same commit; the old one is stale."""
    runs = [
        _run(1, DOCKER, "2026-08-08T10:00:00Z"),
        _run(2, DOCKER, "2026-08-08T11:30:00Z"),
    ]
    selected = select_watched_runs(runs, [DOCKER])
    assert [r["id"] for r in selected] == [2]


def test_excludes_the_ci_run_itself():
    runs = [_run(7, "CI", "2026-08-08T10:00:00Z")]
    assert select_watched_runs(runs, ["CI"], exclude_run_id="7") == []
    assert len(select_watched_runs(runs, ["CI"], exclude_run_id="8")) == 1


def test_no_watch_names_selects_nothing():
    runs = [_run(1, DOCKER, "2026-08-08T10:00:00Z")]
    assert select_watched_runs(runs, []) == []
    assert select_watched_runs(runs, [""]) == []


def test_watched_run_jobs_carry_the_workflow_name_into_the_comment():
    """A watched run's jobs must stay distinguishable from CI's own jobs."""
    jobs = [
        {"name": "build (amd64)", "status": "completed", "conclusion": "failure",
         "html_url": "https://example/1", "_workflow_name": DOCKER},
        {"name": "Python tests", "status": "completed", "conclusion": "success",
         "html_url": "https://example/2"},
    ]
    completed, pending, job_urls = classify_jobs(jobs)
    assert completed[f"{DOCKER} / build (amd64)"] == "failure"
    assert completed["Python tests"] == "success"
    assert pending == []
    assert job_urls[f"{DOCKER} / build (amd64)"] == "https://example/1"


def test_parse_watch_workflows_keeps_commas_inside_a_name():
    """Workflow names contain commas, so the list is newline-separated."""
    assert _mod.parse_watch_workflows("Docker Build, Test, and Publish\n") == [
        "Docker Build, Test, and Publish"
    ]
    assert _mod.parse_watch_workflows("A\nB\n\n  C  \n") == ["A", "B", "C"]
    assert _mod.parse_watch_workflows("") == []


def test_workflow_watch_list_names_a_workflow_that_exists():
    """The names the workflow passes must match real workflow ``name:`` values.

    A name that matches nothing makes the poller silently drop that run
    from the comment, which no unit test on its own would notice.
    """
    yaml = pytest.importorskip("hermes_yaml")
    root = Path(__file__).resolve().parents[2]
    caller = yaml.safe_load(
        (root / ".github/workflows/ci-review-comment.yml").read_text(encoding="utf-8")
    )
    step = next(
        s for s in caller["jobs"]["comment"]["steps"]
        if "WATCH_WORKFLOWS" in (s.get("env") or {})
    )
    watched = _mod.parse_watch_workflows(step["env"]["WATCH_WORKFLOWS"])
    assert watched, "the poller is watching nothing"

    known = set()
    for path in (root / ".github/workflows").glob("*.yml"):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(doc, dict) and isinstance(doc.get("name"), str):
            known.add(doc["name"])

    assert set(watched) <= known, f"unknown workflow names: {set(watched) - known}"


def test_poller_never_watches_its_own_workflow():
    """The poller's own run must never gate completion.

    ``runs_all_completed`` waits until every relevant run is completed.
    The poller's run is in progress for as long as it polls, so watching
    itself would make the loop wait for itself and only ever exit on
    timeout.
    """
    yaml = pytest.importorskip("hermes_yaml")
    root = Path(__file__).resolve().parents[2]
    doc = yaml.safe_load(
        (root / ".github/workflows/ci-review-comment.yml").read_text(encoding="utf-8")
    )
    own_name = doc["name"]
    step = next(
        s for s in doc["jobs"]["comment"]["steps"]
        if "WATCH_WORKFLOWS" in (s.get("env") or {})
    )
    watched = _mod.parse_watch_workflows(step["env"]["WATCH_WORKFLOWS"])
    assert own_name not in watched


def test_the_merge_gate_never_shows_as_a_job_in_the_comment():
    """The gate's display name is versioned (bumped to void pre-change greens);
    every rename must reach the infra list, or the comment lists the gate as a job."""
    yaml = pytest.importorskip("hermes_yaml")
    root = Path(__file__).resolve().parents[2]
    ci = yaml.safe_load((root / ".github/workflows/ci.yaml").read_text(encoding="utf-8"))
    gate = ci["jobs"]["all-checks-pass"]["name"]
    completed, pending, _ = classify_jobs([{"name": gate, "status": "completed", "conclusion": "success"}])
    assert (completed, pending) == ({}, [])


# ─── runs_all_completed ───────────────────────────────────────────────


def test_runs_all_completed_true_only_when_every_run_finished():
    done = {"status": "completed"}
    running = {"status": "in_progress"}
    queued = {"status": "queued"}
    assert _mod.runs_all_completed([done])
    assert _mod.runs_all_completed([done, done])
    assert not _mod.runs_all_completed([done, running])
    assert not _mod.runs_all_completed([queued])


def test_runs_all_completed_empty_list_is_not_done():
    """No run info at all must not read as 'everything passed'."""
    assert not _mod.runs_all_completed([])


def test_runs_all_completed_missing_status_is_not_done():
    assert not _mod.runs_all_completed([{}])


# ─── artifact name/path boundary (Issue #29 security review) ──────────

# Every producer in .github/workflows uses this shape; the validation
# must keep accepting all of them while rejecting everything else.
ACCEPTED_ARTIFACT_NAMES = [
    "review-status-prompt-surface",
    "review-status-history-check",
    "review-status-uv-lockfile",
    "review-status-ci-timings",
    "review-status-contributor-check",
    "review-status-e2e-desktop",
    "review-status-supply-chain",
    "review-status-lockfile-diff",
]


@pytest.mark.parametrize("name", ACCEPTED_ARTIFACT_NAMES)
def test_real_producer_artifact_names_pass_validation(name):
    assert _mod.validate_artifact_name(name) == name


@pytest.mark.parametrize("name", [
    "",
    "review-status-",                      # empty slug
    "review-status-../evil",               # traversal
    "../evil",                             # traversal, no prefix
    "..",
    "/review-status-x",                    # absolute
    "review-status-a/b",                   # posix separator
    "review-status-a\\b",                  # windows separator
    "C:review-status-x",                   # drive path
    "C:\\review-status-x",                 # drive path
    "review-status-x/../y",                # traversal mid-name
    "review-status-\u00e9vil",             # non-ASCII
    "review-status\u2011evil",             # unicode look-alike hyphen
    "review-status-x\n",                   # control byte (\Z blocks it)
    "review-status-x\0",                   # NUL
    "review-status- evil",                 # space
    "review-status-X",                     # uppercase outside the class
    "review-status-x.json",                # dot outside the class
    "review-status-" + "z" * 65,           # slug over the 64-char bound
    None,
    5,
    {"name": "x"},
])
def test_malformed_artifact_names_are_rejected(name):
    assert _mod.validate_artifact_name(name) is None


def test_malformed_artifact_name_never_reaches_network_or_disk(monkeypatch, tmp_path):
    def boom(*args, **kwargs):
        raise AssertionError("network reached for an invalid artifact name")

    monkeypatch.setattr(urllib.request, "build_opener", boom)
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    dest = tmp_path / "dl"
    dest.mkdir()
    res = _mod._download_artifact(
        "t", "o/r",
        {"name": "review-status-../../pwn",
         "archive_download_url": "https://api.example/download"},
        dest,
    )
    assert res is None
    assert list(dest.iterdir()) == []


def _mkzip(path: Path, entries: list[tuple[str, bytes, int]]) -> None:
    """Build a zip with explicit unix mode bits (0 = windows-style entry)."""
    with zipfile.ZipFile(path, "w") as zf:
        for name, data, mode in entries:
            info = zipfile.ZipInfo(name)
            info.external_attr = mode << 16
            zf.writestr(info, data)


def _extract(tmp_path, entries):
    zp = tmp_path / "artifact.zip"
    ex = tmp_path / "ex"
    ex.mkdir()
    _mkzip(zp, entries)
    return _mod._extract_status_file(zp, ex), ex


REG = 0o100644
LNK = 0o120777
DIR = 0o040755
SOCK = 0o140644
CHR = 0o020644


def test_extract_accepts_regular_top_level_status_entry(tmp_path):
    got, ex = _extract(tmp_path, [("review-status.json", b"review_status=[]", REG)])
    assert got == ex / "review-status.json"
    assert got.read_bytes() == b"review_status=[]"


def test_extract_accepts_windows_style_entry_without_mode_bits(tmp_path):
    got, _ = _extract(tmp_path, [("review-status.json", b"x", 0)])
    assert got is not None


def test_extract_rejects_symlink_status_entry_and_writes_nothing(tmp_path):
    got, ex = _extract(tmp_path, [("review-status.json", b"/etc/passwd", LNK)])
    assert got is None
    assert not (ex / "review-status.json").exists()


@pytest.mark.parametrize("mode,name", [
    (DIR, "review-status.json/"),
    (SOCK, "review-status.json"),
    (CHR, "review-status.json"),
])
def test_extract_rejects_abnormal_entry_types(tmp_path, mode, name):
    got, ex = _extract(tmp_path, [(name, b"x", mode)])
    assert got is None
    assert list(ex.iterdir()) == []


def test_extract_ignores_path_escape_and_never_writes_outside(tmp_path):
    got, ex = _extract(tmp_path, [
        ("../evil.json", b"pwn", REG),
        ("/tmp/evil-abs.json", b"pwn", REG),
        ("C:\\evil-win.json", b"pwn", REG),
        ("..\\..\\evil-bat.json", b"pwn", REG),
        ("sub/../../deep.json", b"pwn", REG),
        ("review-status.json", b"review_status=[]", REG),
    ])
    assert got == ex / "review-status.json"
    assert sorted(p.name for p in ex.rglob("*")) == ["review-status.json"]
    assert not (tmp_path / "evil.json").exists()


def test_extract_ignores_nested_status_file_without_top_level_entry(tmp_path):
    got, ex = _extract(tmp_path, [("sub/review-status.json", b"x", REG)])
    assert got is None
    assert list(ex.iterdir()) == []


def test_extract_survives_missing_and_corrupt_archives(tmp_path):
    assert _mod._extract_status_file(tmp_path / "nope.zip", tmp_path) is None
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    assert _mod._extract_status_file(bad, tmp_path) is None


def test_nonnumeric_run_id_skips_artifact_work_without_network(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("network reached for an invalid run id")

    monkeypatch.setattr(_mod, "_api_get_paginated", boom)
    assert _mod.fetch_all_review_statuses("t", "o/r", "../pwned") == []


# ─── review-comment ownership (Issue #29 security review) ─────────────

BOT = _mod.DEFAULT_BOT_LOGIN


def _comment(cid, login=BOT, marker=True, repo="o/r", pr="33",
             with_user=True, with_pr=True):
    c = {"id": cid,
         "body": (_mod.COMMENT_MARKER + " review") if marker else "plain comment"}
    if with_user:
        c["user"] = {"login": login}
    if with_pr:
        c["pull_request"] = {"url": f"https://api.github.com/repos/{repo}/pulls/{pr}"}
    return c


def _serve_comments(monkeypatch, comments):
    monkeypatch.setattr(_mod, "_api_get_paginated", lambda *a, **k: comments)


def test_find_comment_accepts_own_bot_comment(monkeypatch):
    _serve_comments(monkeypatch, [_comment(7)])
    assert _mod.find_comment_id("t", "o/r", "33") == 7


def test_find_comment_ignores_foreign_marker_comment(monkeypatch):
    """A different user pasting our marker must never be PATCHed."""
    _serve_comments(monkeypatch, [_comment(7, login="evil-user")])
    assert _mod.find_comment_id("t", "o/r", "33") is None


def test_find_comment_ignores_comment_with_unknown_author(monkeypatch):
    """No identity in the payload -> identity cannot be established -> not patchable."""
    _serve_comments(monkeypatch, [_comment(7, with_user=False)])
    assert _mod.find_comment_id("t", "o/r", "33") is None


@pytest.mark.parametrize("kwargs", [
    {"pr": "99"},                 # marker on a comment for another PR
    {"repo": "attacker/repo"},    # marker tied to another repository
])
def test_find_comment_requires_pr_target_match(monkeypatch, kwargs):
    _serve_comments(monkeypatch, [_comment(7, **kwargs)])
    assert _mod.find_comment_id("t", "o/r", "33") is None


def test_find_comment_ignores_comment_without_pr_link(monkeypatch):
    _serve_comments(monkeypatch, [_comment(7, with_pr=False)])
    assert _mod.find_comment_id("t", "o/r", "33") is None


def test_find_comment_skips_non_marker_comments(monkeypatch):
    _serve_comments(monkeypatch, [_comment(7, marker=False)])
    assert _mod.find_comment_id("t", "o/r", "33") is None


def test_find_comment_rejects_nonnumeric_pr_number_without_api(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("API called with an invalid PR number")

    monkeypatch.setattr(_mod, "_api_get_paginated", boom)
    assert _mod.find_comment_id("t", "o/r", "33/../4") is None


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _capture_write(monkeypatch):
    requests = []

    def fake_urlopen(req):
        requests.append((req.full_url, req.get_method()))
        return _Resp({"id": 9})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return requests


def test_upsert_patches_only_the_verified_own_comment(monkeypatch):
    reqs = _capture_write(monkeypatch)
    _serve_comments(monkeypatch, [_comment(7)])
    assert _mod.upsert_comment("t", "o/r", "33", "body") == 9
    assert reqs == [("https://api.github.com/repos/o/r/issues/comments/7", "PATCH")]


def test_upsert_posts_new_instead_of_editing_a_foreign_marker(monkeypatch):
    reqs = _capture_write(monkeypatch)
    _serve_comments(monkeypatch, [
        _comment(7, login="evil-user"),      # same marker, wrong author
        _comment(8, login="attacker", with_user=False),  # marker, unknown author
    ])
    assert _mod.upsert_comment("t", "o/r", "33", "body") == 9
    assert reqs == [("https://api.github.com/repos/o/r/issues/33/comments", "POST")]


def test_upsert_rejects_nonnumeric_pr_number_without_request(monkeypatch):
    reqs = _capture_write(monkeypatch)
    assert _mod.upsert_comment("t", "o/r", "33; rm -rf /", "body") is None
    assert reqs == []


def test_upsert_accepts_custom_expected_login(monkeypatch):
    reqs = _capture_write(monkeypatch)
    _serve_comments(monkeypatch, [_comment(7, login="ci-bot")])
    assert _mod.upsert_comment("t", "o/r", "33", "body", expected_login="ci-bot") == 9
    assert reqs[0][1] == "PATCH"


def test_commenter_workflow_pins_the_expected_bot_login():
    """The workflow's configured login must equal the code default, so the
    ownership check cannot drift silently."""
    yaml = pytest.importorskip("hermes_yaml")
    root = Path(__file__).resolve().parents[2]
    doc = yaml.safe_load(
        (root / ".github/workflows/ci-review-comment.yml").read_text(encoding="utf-8")
    )
    step = next(
        s for s in doc["jobs"]["comment"]["steps"]
        if "PR_NUMBER" in (s.get("env") or {})
    )
    assert step["env"]["CI_BOT_COMMENT_LOGIN"] == _mod.DEFAULT_BOT_LOGIN
