"""The updater can preserve stale platform pins while refreshing healthy targets."""

from pm import cli
from pm.lock import Lockfile
from pm.store import ALL_TARGETS


def test_skip_target_keeps_its_artifact_pin_while_other_targets_update(tmp_path, monkeypatch):
    skipped = "linux-arm64-bionic"
    assert skipped in ALL_TARGETS
    queried = []

    class RollingPackage:
        name = "rolling-tool"
        version_style = "semver"

        def missing_reason(self, target):
            return None

        def latest_versions(self, target, locked=None):
            queried.append(target)
            return ["2.0.0", "1.0.0"]

        def fetch_urls(self, version, target):
            return [f"https://example.invalid/{target}/{version}.tgz"]

        def known_sha256(self, version, url):
            return "f" * 64

    original = {
        target: {
            "url": f"https://example.invalid/{target}/1.0.0.tgz",
            "sha256": "a" * 64,
        }
        for target in ALL_TARGETS
    }
    lock = Lockfile(tmp_path / "lock.json")
    lock.set_pin("rolling-tool", "1.0.0", original)
    lock.save()
    package = RollingPackage()

    monkeypatch.setattr(cli, "_lockfile", lambda: lock)
    monkeypatch.setattr(cli, "get_package", lambda _name: package)
    monkeypatch.setattr(cli, "_install_names", lambda _names: 0)
    monkeypatch.setattr(cli, "_sync_venv_step", lambda: True)

    from argparse import Namespace

    args = Namespace(
        names=["rolling-tool"],
        target=None,
        skip_target=[skipped],
        check=False,
        uv=False,
        npm=False,
        termux=False,
    )
    assert cli.cmd_update(args) == 0

    updated = Lockfile(lock.path)
    assert queried == [target for target in ALL_TARGETS if target != skipped]
    assert updated.version("rolling-tool") == "2.0.0"
    assert updated.artifacts("rolling-tool", skipped) == [original[skipped]]
    for target in ALL_TARGETS:
        if target != skipped:
            assert updated.artifacts("rolling-tool", target) == [{
                "url": f"https://example.invalid/{target}/2.0.0.tgz",
                "sha256": "f" * 64,
            }]
