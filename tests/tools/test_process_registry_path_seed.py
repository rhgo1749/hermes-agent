"""The login shell must restore the session toolchain after startup files run."""

from tools.environments.local import _restore_session_toolchain_path


def test_restore_session_toolchain_path_after_login_startup():
    assert _restore_session_toolchain_path("echo hello") == (
        'if [ -n "${_HERMES_SESSION_TOOLCHAIN_PATH_SEED-}" ]; then '
        'PATH="${_HERMES_SESSION_TOOLCHAIN_PATH_SEED}${PATH:+:$PATH}"; '
        'export PATH; fi; unset _HERMES_SESSION_TOOLCHAIN_PATH_SEED; echo hello'
    )
