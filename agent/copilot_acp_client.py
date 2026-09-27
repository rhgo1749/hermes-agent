"""OpenAI-compatible shim that forwards Hermes requests to `copilot --acp`.

Each request starts a short-lived ACP session, sends the formatted conversation
as one prompt, collects text chunks, and returns the minimal OpenAI-client shape.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import queue
import re
import shlex
import subprocess
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent.acp_openai_bridge import (
    completion_to_stream_chunks as _completion_to_stream_chunks,
    extract_tool_calls_from_text as _extract_tool_calls_from_text,
    render_tool_bridge_sections as _render_tool_bridge_sections,
)
from agent.file_safety import (
    get_nt_namespace_error, get_read_block_error, get_write_denied_error, is_write_approval_required)
from agent.redact import redact_sensitive_text
from tools.environments.local import hermes_subprocess_env

ACP_MARKER_BASE_URL = "acp://copilot"
logger = logging.getLogger(__name__)
_DEFAULT_TIMEOUT_SECONDS = 900.0
# Stderr fingerprint of the deprecated `gh copilot` extension. Require BOTH the product name
# AND a deprecation marker: the NEW `@github/copilot` CLI legitimately mentions "copilot-cli".
_DEPRECATION_REQUIRED = ("gh-copilot",)
_DEPRECATION_MARKERS = ("has been deprecated", "no commands will be executed")
_ROLE_LABELS = {"system": "System", "user": "User", "assistant": "Assistant", "tool": "Tool", "context": "Context"}
# Probe verdicts per binary path (~50ms --help paid once per process). Only definitive
# True/False is cached, so a CLI installed mid-session is picked up.
_ACP_PROBE_CACHE: dict[str, bool] = {}
_PROMPT_PREAMBLE = (
    "You are being used as the active ACP agent backend for Hermes.",
    "Use ACP capabilities to complete tasks.",
    "IMPORTANT: If you take an action with a tool, you MUST output tool calls using <tool_call>{...}</tool_call> blocks with JSON exactly in OpenAI function-call shape.",
    "If no tool is needed, answer normally.",
)
_TOOL_HISTORY_CONTINUATION_NOTE = (
    "Continuation rule: tool calls and tool responses in the conversation transcript are completed past actions. "
    "Use their returned content as current evidence. Do not repeat an identical tool call merely because a standing "
    "system instruction says to call that tool first, orient, initialize, or inspect state. Repeat a completed call "
    "only when new evidence makes a fresh read necessary. If a tool response says the state/result is unchanged or "
    "that repeating the call makes no progress, continue with the result already present instead of calling it again."
)
_DUPLICATE_NO_PROGRESS_CORRECTION = (
    "Your proposed tool call exactly repeats the most recent completed call, and that call's tool response explicitly "
    "reported that the result/state is unchanged. That exact tool is deliberately unavailable for this correction "
    "turn. Do not emit it again. Continue from the existing result: choose a different available tool only if it "
    "advances the task, otherwise answer or perform the next required action."
)
_MAX_DUPLICATE_NO_PROGRESS_CORRECTIONS = 2
_INITIALIZE_PARAMS = {
    "protocolVersion": 1,
    "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}},
    "clientInfo": {"name": "hermes-agent", "title": "Hermes Agent", "version": "0.0.0"},
}
_DEPRECATED_CLI_ERROR = (
    "Hermes ACP mode requires the NEW GitHub Copilot CLI (github.com/github/copilot-cli), but the binary it just "
    "spawned is the deprecated `gh copilot` extension.\n\n"
    "Install the new CLI:\n  npm install -g @github/copilot\n  # then verify with: copilot --help\n\n"
    "If `copilot` already resolves to the new CLI but you still see this,\npoint Hermes at it explicitly:\n"
    "  export HERMES_COPILOT_ACP_COMMAND=/path/to/new/copilot\n\n"
    "Alternative: use the `copilot` provider (no ACP, hits the Copilot API\ndirectly with a Copilot subscription "
    "token) via `hermes setup`.\n\nOriginal error:\n"
)


def _is_gh_copilot_deprecation_message(stderr_text: str) -> bool:
    """True iff stderr looks like the deprecated gh-copilot extension's banner."""
    lower = stderr_text.lower()
    return any(req in lower for req in _DEPRECATION_REQUIRED) and any(m in lower for m in _DEPRECATION_MARKERS)


def _resolve_command() -> str:
    return os.getenv("HERMES_COPILOT_ACP_COMMAND", "").strip() or os.getenv("COPILOT_CLI_PATH", "").strip() or "copilot"


def _resolve_args() -> list[str]:
    return shlex.split(os.getenv("HERMES_COPILOT_ACP_ARGS", "").strip()) or ["--acp", "--stdio"]


def _acp_supported(command: str, args: list[str]) -> bool | None:
    """Tri-state ``--acp`` probe (a CLI without the flag exits 1 and the parent would wait the
    full child timeout for stdout that never arrives). True = help advertises --acp; False =
    help ran cleanly without it (caller fast-fails); None = inconclusive (binary missing /
    --help failed → normal spawn error). Skipped when ``--acp`` is not in ``args`` (custom transport)."""
    if "--acp" not in args:
        return True
    if (cached := _ACP_PROBE_CACHE.get(command)) is not None:
        return cached
    try:
        probe = subprocess.run(
            [command, "--help"],
            # Explicit codec because text=True alone decodes with the
            # locale default and crashes on non-ASCII help text under
            # GBK/CP932 locales.
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=5,
            stdin=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if probe.returncode != 0:
        return None
    # ``--acp`` as a flag token; tolerate spacing and ``[--acp]`` variants.
    verdict = _ACP_PROBE_CACHE[command] = bool(re.search(r"(?:^|[\s\[])--acp(?:[\s=\],]|$)", probe.stdout, re.MULTILINE))
    return verdict


def _resolve_home_dir() -> str:
    """Stable HOME for child ACP processes; the temp dir as a last resort so the child never starts HOME-less."""
    if home := os.environ.get("HOME", "").strip():
        return home
    if (expanded := os.path.expanduser("~")) and expanded != "~":
        return expanded
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_dir.strip() or tempfile.gettempdir()  # windows-footgun: ok — POSIX fallback inside try/except (pwd import fails on Windows)
    except Exception:
        return tempfile.gettempdir()


def _build_subprocess_env() -> dict[str, str]:
    from hermes_constants import apply_subprocess_home_env

    # Copilot ACP drives a model and needs LLM provider credentials; the central helper still
    # strips Tier-1 secrets (bot tokens, GitHub auth, infra).
    # See #29157.
    env = hermes_subprocess_env(inherit_credentials=True)
    env["HOME"] = _resolve_home_dir()
    apply_subprocess_home_env(env)
    return env


def _jsonrpc_result(message_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _jsonrpc_error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def _enabled_id_list(entries: Any, key: str) -> list[str]:
    """Ordered ids whose ``_meta.copilotEnablement`` is not ``disabled``."""
    seen: set[str] = set()
    result: list[str] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        value = str(entry.get(key) or "").strip()
        if (not value or value in seen
                or str((entry.get("_meta") or {}).get("copilotEnablement") or "").strip().lower() == "disabled"):
            continue
        seen.add(value)
        result.append(value)
    return result


def _model_config_option(session: dict[str, Any]) -> dict[str, Any] | None:
    return next((option for option in (session.get("configOptions") or []) if isinstance(option, dict)
                 and "model" in (option.get("category"), option.get("id"))), None)


def _session_model_ids(session: dict[str, Any]) -> list[str]:
    """Account-authorized model ids advertised by ``session/new`` in ACP v1 or its legacy extension."""
    if option := _model_config_option(session):
        return _enabled_id_list(option.get("options"), "value")
    return _legacy_session_model_ids(session)


def _legacy_session_model_ids(session: dict[str, Any]) -> list[str]:
    return _enabled_id_list((session.get("models") or {}).get("availableModels"), "modelId")


def _model_selection_request(session: dict[str, Any], requested_model: str) -> tuple[str, dict[str, str]] | None:
    """ACP request selecting ``requested_model`` for ``session``: stable v1
    ``session/set_config_option``, else Copilot's pre-stabilization ``session/set_model``
    when no model config option is advertised. A reported model list is authoritative:
    unknown and policy-disabled ids return None instead of being sent."""
    session_id = str(session.get("sessionId") or "").strip()
    requested_model = str(requested_model or "").strip()
    if not session_id or not requested_model or requested_model == "copilot-acp":
        return None
    option = _model_config_option(session)
    if option:
        if requested_model not in _enabled_id_list(option.get("options"), "value"):
            return None
        return "session/set_config_option", {"sessionId": session_id, "configId": str(option.get("id") or "model"), "value": requested_model}
    available = _legacy_session_model_ids(session)
    return None if available and requested_model not in available else ("session/set_model", {"sessionId": session_id, "modelId": requested_model})


def _format_messages_as_prompt(
    messages: list[dict[str, Any]], model: str | None = None, tools: list[dict[str, Any]] | None = None, tool_choice: Any = None,
) -> str:
    # Deliberately no "requested model" line: the model is applied for real via ACP session/set_model;
    # a prompt-text mention makes a substituted backend model FALSELY self-identify as the requested
    # one. Copilot has no tools of its own that collide with Hermes', so forward the whole toolset.
    sections: list[str] = [*_PROMPT_PREAMBLE, *_render_tool_bridge_sections(tools, tool_choice)]
    transcript: list[str] = []
    for message in (m for m in messages if isinstance(m, dict)):
        role = str(message.get("role") or "unknown").strip().lower()
        if rendered := _render_prompt_message(message, role):
            transcript.append(f"{_ROLE_LABELS.get(role, 'Context')}:\n{rendered}")
    if transcript:
        sections.append("Conversation transcript:\n\n" + "\n\n".join(transcript))
    if _has_completed_tool_history(messages):
        sections.append(_TOOL_HISTORY_CONTINUATION_NOTE)
    sections.append("Continue the conversation from the latest user request.")
    return "\n\n".join(section.strip() for section in sections if section and section.strip())


def _has_completed_tool_history(messages: list[dict[str, Any]]) -> bool:
    """Whether replay history contains at least one tool result tied to a prior call.

    ACP backends receive OpenAI history flattened into prompt text on every turn. A
    completed tool row must therefore be called out explicitly as *history*, otherwise
    some backends reinterpret standing "call X first" instructions as a fresh action on
    every continuation turn and loop on the same tool despite already having its result.
    """
    return any(
        isinstance(message, dict)
        and str(message.get("role") or "").strip().lower() == "tool"
        and bool(str(message.get("tool_call_id") or "").strip())
        for message in messages
    )


def _render_prompt_message(message: dict[str, Any], role: str) -> str:
    """Render one history row without orphaning tool results from their calls."""
    content = _render_message_content(message.get("content"))

    if role == "assistant":
        parts = [content] if content else []
        for tool_call in message.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            if not isinstance(function, dict) or not str(function.get("name") or "").strip():
                continue
            payload = {
                "id": str(tool_call.get("id") or ""),
                "type": str(tool_call.get("type") or "function"),
                "function": {
                    "name": str(function.get("name") or ""),
                    "arguments": function.get("arguments", "{}"),
                },
            }
            parts.append(f"<tool_call>\n{json.dumps(payload, ensure_ascii=False, default=str)}\n</tool_call>")
        return "\n".join(parts).strip()

    if role == "tool" and message.get("tool_call_id"):
        result: Any = content
        if content.lstrip().startswith(("{", "[")):
            try:
                result = json.loads(content)
            except json.JSONDecodeError:
                pass
        payload: dict[str, Any] = {"tool_call_id": str(message["tool_call_id"]), "content": result}
        if message.get("name"):
            payload["name"] = str(message["name"])
        return f"<tool_response>\n{json.dumps(payload, ensure_ascii=False, default=str)}\n</tool_response>"

    return content


def _normalized_tool_arguments(arguments: Any) -> str:
    if isinstance(arguments, str):
        text = arguments.strip() or "{}"
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return text
        return json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return json.dumps(arguments if arguments is not None else {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _tool_result_reports_unchanged(content: Any) -> bool:
    text = _render_message_content(content)
    if not text:
        return False
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict) and parsed.get("unchanged") is True:
        return True
    compact = "".join(text.lower().split())
    return '"unchanged":true' in compact


def _latest_unchanged_tool_signature(messages: list[dict[str, Any]]) -> tuple[str, str] | None:
    """Return the most recent completed tool call iff its result explicitly says unchanged."""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, dict) or str(message.get("role") or "").strip().lower() != "tool":
            continue
        call_id = str(message.get("tool_call_id") or "").strip()
        if not call_id or not _tool_result_reports_unchanged(message.get("content")):
            return None
        for prior in range(index - 1, -1, -1):
            assistant = messages[prior]
            if not isinstance(assistant, dict) or str(assistant.get("role") or "").strip().lower() != "assistant":
                continue
            for tool_call in assistant.get("tool_calls") or []:
                if not isinstance(tool_call, dict) or str(tool_call.get("id") or "").strip() != call_id:
                    continue
                function = tool_call.get("function")
                if not isinstance(function, dict):
                    return None
                name = str(function.get("name") or "").strip()
                if not name:
                    return None
                return name, _normalized_tool_arguments(function.get("arguments", "{}"))
        return None
    return None


def _repeats_latest_unchanged_call(messages: list[dict[str, Any]], tool_calls: list[Any]) -> bool:
    if len(tool_calls) != 1:
        return False
    previous = _latest_unchanged_tool_signature(messages)
    if previous is None:
        return False
    call = tool_calls[0]
    function = getattr(call, "function", None)
    name = str(getattr(function, "name", "") or "").strip()
    arguments = getattr(function, "arguments", "{}")
    return (name, _normalized_tool_arguments(arguments)) == previous


def _without_openai_tool(tools: list[dict[str, Any]] | None, blocked_name: str) -> list[dict[str, Any]]:
    """Return tool schemas excluding one exact function name; malformed entries pass through."""
    filtered: list[dict[str, Any]] = []
    for tool in tools or []:
        function = tool.get("function") if isinstance(tool, dict) else None
        name = str(function.get("name") or "").strip() if isinstance(function, dict) else ""
        if name == blocked_name:
            continue
        filtered.append(tool)
    return filtered


def _render_message_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, dict):
        if "text" in content:
            return str(content.get("text") or "").strip()
        return content["content"].strip() if isinstance(content.get("content"), str) else json.dumps(content, ensure_ascii=True)
    if isinstance(content, list):
        parts = [item if isinstance(item, str) else item["text"].strip() for item in content if isinstance(item, str)
                 or (isinstance(item, dict) and isinstance(item.get("text"), str) and item["text"].strip())]
        return "\n".join(parts).strip()
    return str(content).strip()


def _conversation_affinity(messages: list[dict[str, Any]], cwd: str) -> str:
    """Opaque stable key for account/cache locality across short-lived ACP sessions.

    Only the immutable opening prefix through the first user message participates, so
    later tool/user turns do not move a conversation between provider account caches.
    The subprocess receives only the digest, never prompt text.
    """
    opening: list[str] = []
    saw_user = False
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip().lower()
        if role in {"assistant", "tool"}:
            break
        if role not in {"system", "context", "user"}:
            continue
        opening.append(f"{role}\0{_render_message_content(message.get('content'))}")
        if role == "user":
            saw_user = True
            break
    if not saw_user:
        return ""
    material = f"{Path(cwd).resolve()}\0" + "\0".join(opening)
    return "sha256:" + hashlib.sha256(material.encode("utf-8", errors="surrogatepass")).hexdigest()


def _ensure_path_within_cwd(path_text: str, cwd: str, *, verb: str) -> Path:
    # Raw-string check BEFORE resolve(): resolving an NT-namespace path is the NTLM-leak trigger.
    if nt_error := get_nt_namespace_error(path_text, verb=verb):
        raise PermissionError(nt_error)
    if not Path(path_text).is_absolute():
        raise PermissionError("ACP file-system paths must be absolute.")
    resolved, root = Path(path_text).resolve(), Path(cwd).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PermissionError(f"Path '{resolved}' is outside the session cwd '{root}'.") from exc
    return resolved


def _effective_timeout(timeout: Any) -> float:
    """Normalise a float or httpx.Timeout-like object to wall-clock seconds (largest component wins)."""
    if isinstance(timeout, (int, float)):
        return float(timeout)
    candidates = [getattr(timeout, attr, None) for attr in ("read", "write", "connect", "pool", "timeout")]
    return max((float(v) for v in candidates if isinstance(v, (int, float))), default=_DEFAULT_TIMEOUT_SECONDS)


def _fs_read_text_file(params: dict[str, Any], cwd: str) -> Any:
    path = _ensure_path_within_cwd(str(params.get("path") or ""), cwd, verb="Read")
    if block_error := get_read_block_error(str(path)):
        raise PermissionError(block_error)
    try:
        content = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        content = ""
    line, limit = params.get("line"), params.get("limit")
    if isinstance(line, int) and line > 1:
        end = line - 1 + limit if isinstance(limit, int) and limit > 0 else None
        content = "".join(content.splitlines(keepends=True)[line - 1:end])
    return {"content": redact_sensitive_text(content, force=True) if content else content}


def _fs_write_text_file(params: dict[str, Any], cwd: str) -> Any:
    path = _ensure_path_within_cwd(str(params.get("path") or ""), cwd, verb="Write")
    if denied := get_write_denied_error(str(path)):
        raise PermissionError(denied)
    if is_write_approval_required(str(path)):  # soft-gated for interactive tools; the ACP shim has no human channel → fail closed
        raise PermissionError(f"Write denied: '{path}' requires interactive approval and cannot be written through the ACP file bridge.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(params.get("content") or ""), encoding="utf-8")
    return None


_FS_HANDLERS = {"fs/read_text_file": _fs_read_text_file, "fs/write_text_file": _fs_write_text_file}


@dataclass(frozen=True)
class _ACPConversationState:
    session_id: str
    context_fingerprint: str
    request_fingerprints: tuple[str, ...]
    response_fingerprint: str


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


def _fingerprint_prompt_message(message: dict[str, Any], role: str) -> str:
    """Semantic prompt row for frontier checks, normalizing JSON tool arguments only.

    Hermes may round-trip ``{"a": 1}`` as ``{"a":1}`` while preserving the exact
    tool call. Treat those as identical without changing the real prompt bytes sent to ACP.
    """
    if role != "assistant":
        return _render_prompt_message(message, role)

    content = _render_message_content(message.get("content"))
    parts = [content] if content else []
    for tool_call in message.get("tool_calls") or []:
        if not isinstance(tool_call, dict):
            continue
        function = tool_call.get("function")
        if not isinstance(function, dict) or not str(function.get("name") or "").strip():
            continue
        payload = {
            "id": str(tool_call.get("id") or ""),
            "type": str(tool_call.get("type") or "function"),
            "function": {
                "name": str(function.get("name") or ""),
                "arguments": _normalized_tool_arguments(function.get("arguments", "{}")),
            },
        }
        parts.append(json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
        ))
    return "\n".join(parts).strip()


def _message_fingerprint(message: dict[str, Any]) -> str:
    """Fingerprint exactly the semantic row representation this ACP bridge replays."""
    role = str(message.get("role") or "unknown").strip().lower()
    rendered = _fingerprint_prompt_message(message, role)
    return _sha256_text(f"{role}\0{rendered}")


def _context_fingerprint(model: str | None, tools: list[dict[str, Any]] | None, tool_choice: Any) -> str:
    rendered_tools = _render_tool_bridge_sections(tools, tool_choice)
    payload = json.dumps(
        {"model": str(model or ""), "tool_bridge": rendered_tools},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    )
    return _sha256_text(payload)


def _response_fingerprint(content: str, tool_calls: list[Any]) -> str:
    message: dict[str, Any] = {"role": "assistant", "content": content or ""}
    if tool_calls:
        rendered_calls: list[dict[str, Any]] = []
        for call in tool_calls:
            function = getattr(call, "function", None)
            rendered_calls.append({
                "id": str(getattr(call, "id", "") or ""),
                "type": str(getattr(call, "type", "function") or "function"),
                "function": {
                    "name": str(getattr(function, "name", "") or ""),
                    "arguments": getattr(function, "arguments", "{}"),
                },
            })
        message["tool_calls"] = rendered_calls
    return _message_fingerprint(message)


def _continuation_delta(
    messages: list[dict[str, Any]], state: _ACPConversationState, context_fingerprint: str,
) -> list[dict[str, Any]] | None:
    """Return append-only Hermes rows after the ACP-native assistant turn, else fail closed.

    The prior request rows must be byte-equivalent under the bridge's renderer and the
    next row must be the exact assistant response emitted by that ACP session. Compression,
    rewind, prompt/tool changes, or response rewriting therefore force a full replay.
    """
    if state.context_fingerprint != context_fingerprint:
        return None
    prefix_len = len(state.request_fingerprints)
    if len(messages) <= prefix_len:
        return None
    if tuple(_message_fingerprint(m) for m in messages[:prefix_len]) != state.request_fingerprints:
        return None
    assistant = messages[prefix_len]
    if (not isinstance(assistant, dict)
            or str(assistant.get("role") or "").strip().lower() != "assistant"
            or _message_fingerprint(assistant) != state.response_fingerprint):
        return None
    delta = messages[prefix_len + 1:]
    if not delta:
        return None
    # A new system row is a context rewrite, never an append-only continuation.
    if any(str(m.get("role") or "").strip().lower() == "system" for m in delta if isinstance(m, dict)):
        return None
    return delta


def _format_continuation_delta(messages: list[dict[str, Any]]) -> str:
    transcript: list[str] = []
    for message in (m for m in messages if isinstance(m, dict)):
        role = str(message.get("role") or "unknown").strip().lower()
        if rendered := _render_prompt_message(message, role):
            transcript.append(f"{_ROLE_LABELS.get(role, 'Context')}:\n{rendered}")
    sections = [
        "Hermes continuation delta: the ACP session already contains all earlier conversation context and your previous response.",
    ]
    if transcript:
        sections.append("New conversation events since your previous ACP turn:\n\n" + "\n\n".join(transcript))
    if _has_completed_tool_history(messages):
        sections.append(_TOOL_HISTORY_CONTINUATION_NOTE)
    sections.append("Continue from these new events only; do not restart or repeat completed earlier work.")
    return "\n\n".join(section.strip() for section in sections if section.strip())


def _resume_session_missing(exc: BaseException) -> bool:
    text = str(exc).lower()
    return (
        "session not found" in text
        or "no session with this id" in text
        or "current gemini_home" in text
    )


class CopilotACPClient:
    """Minimal OpenAI-client-compatible facade for Copilot ACP."""

    # Declared for agent/auxiliary_client.py: this shim drives an ACP subprocess over stdio, so it is
    # already a complete client (never re-dispatch through a wire adapter) and async-safe as-is.
    HERMES_SKIP_TRANSPORT_WRAP = True
    HERMES_SKIP_ASYNC_WRAP = True

    def __init__(
        self, *, api_key: str | None = None, base_url: str | None = None, default_headers: dict[str, str] | None = None,
        acp_command: str | None = None, acp_args: list[str] | None = None, acp_cwd: str | None = None, command: str | None = None,
        args: list[str] | None = None, **_: Any,
    ):
        self.api_key, self.base_url = api_key or "copilot-acp", base_url or ACP_MARKER_BASE_URL
        self._default_headers = dict(default_headers or {})
        self._acp_command = acp_command or command or _resolve_command()
        self._acp_args = list(acp_args or args or _resolve_args())
        self._acp_cwd = str(Path(acp_cwd or os.getcwd()).resolve())
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create_chat_completion))
        self.is_closed = False
        # Clients are cached and shared across concurrent callers (auxiliary tasks, async
        # dispatch), so several ACP sessions can be live on one instance. Track every live
        # child — a single slot would let one session's teardown kill a sibling's process
        # while its own leaked.
        self._active_processes: set[subprocess.Popen[str]] = set()
        self._active_process_lock = threading.Lock()
        self._conversation_states: dict[str, _ACPConversationState] = {}
        self._conversation_state_lock = threading.Lock()

    @staticmethod
    def _terminate_process(proc: subprocess.Popen[str]) -> None:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            with contextlib.suppress(Exception):
                proc.kill()

    def _release_process(self, proc: subprocess.Popen[str]) -> None:
        """Reap one session's own child. ``is_closed`` flips only when the last live
        session drains — marking it while siblings still run would tell lifecycle code
        to rebuild a client that is mid-request."""
        with self._active_process_lock:
            self._active_processes.discard(proc)
        # The OpenAI-compatible client is reusable even though each ACP child is intentionally
        # short-lived. Keeping the facade open lets Hermes' request-client slot preserve the
        # durable ACP session id/frontier across sequential model calls.
        self._terminate_process(proc)

    def close(self) -> None:
        with self._active_process_lock:
            procs, self._active_processes = tuple(self._active_processes), set()
        self.is_closed = True
        with self._conversation_state_lock:
            self._conversation_states.clear()
        for proc in procs:
            self._terminate_process(proc)

    def _create_chat_completion(
        self, *, model: str | None = None, messages: list[dict[str, Any]] | None = None, timeout: float | None = None,
        tools: list[dict[str, Any]] | None = None, tool_choice: Any = None, stream: bool = False, **_: Any,
    ) -> Any:
        request_messages = messages or []
        full_prompt_text = _format_messages_as_prompt(request_messages, model=model, tools=tools, tool_choice=tool_choice)
        affinity_key = _conversation_affinity(request_messages, self._acp_cwd)
        context_fingerprint = _context_fingerprint(model, tools, tool_choice)

        resume_session_id = ""
        prompt_text = full_prompt_text
        if affinity_key:
            with self._conversation_state_lock:
                prior_state = self._conversation_states.get(affinity_key)
            if prior_state is not None:
                delta = _continuation_delta(request_messages, prior_state, context_fingerprint)
                if delta is not None:
                    resume_session_id = prior_state.session_id
                    prompt_text = _format_continuation_delta(delta)
                else:
                    logger.info("ACP continuation frontier changed; rebuilding session with full replay.")

        timeout_seconds = _effective_timeout(timeout)
        started = time.monotonic()
        session_result: dict[str, Any] = {}
        response_text, reasoning = self._run_prompt(
            prompt_text, timeout_seconds=timeout_seconds, model=model, affinity_key=affinity_key,
            resume_session_id=resume_session_id, full_prompt_text=full_prompt_text, session_result=session_result,
        )
        tool_calls, cleaned_text = _extract_tool_calls_from_text(response_text)
        duplicate_signature = _latest_unchanged_tool_signature(request_messages)
        if duplicate_signature is not None and _repeats_latest_unchanged_call(request_messages, tool_calls):
            blocked_name, _ = duplicate_signature
            correction_tools = _without_openai_tool(tools, blocked_name)
            correction_choice = tool_choice if correction_tools else None
            correction_base = _format_messages_as_prompt(
                request_messages, model=model, tools=correction_tools, tool_choice=correction_choice,
            )
            for correction_index in range(_MAX_DUPLICATE_NO_PROGRESS_CORRECTIONS):
                remaining = timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    break
                corrected_prompt = (
                    f"{correction_base}\n\n{_DUPLICATE_NO_PROGRESS_CORRECTION} "
                    f"Blocked tool: {blocked_name}. Correction attempt {correction_index + 1}."
                )
                # A correction changes the available tool schema, so it deliberately starts a
                # fresh ACP session rather than polluting the resumable conversation frontier.
                correction_result: dict[str, Any] = {}
                response_text, reasoning = self._run_prompt(
                    corrected_prompt, timeout_seconds=remaining, model=model, affinity_key=affinity_key,
                    resume_session_id="", full_prompt_text=corrected_prompt, session_result=correction_result,
                )
                tool_calls, cleaned_text = _extract_tool_calls_from_text(response_text)
                if not _repeats_latest_unchanged_call(request_messages, tool_calls):
                    session_result = correction_result
                    context_fingerprint = _context_fingerprint(model, correction_tools, correction_choice)
                    break
            if _repeats_latest_unchanged_call(request_messages, tool_calls):
                raise RuntimeError(
                    f"ACP backend repeated unchanged no-progress tool call {blocked_name!r} "
                    f"after {_MAX_DUPLICATE_NO_PROGRESS_CORRECTIONS} correction attempts; refusing to execute it again."
                )

        session_id = str(session_result.get("session_id") or "").strip()
        if affinity_key and session_id:
            state = _ACPConversationState(
                session_id=session_id,
                context_fingerprint=context_fingerprint,
                request_fingerprints=tuple(_message_fingerprint(m) for m in request_messages if isinstance(m, dict)),
                response_fingerprint=_response_fingerprint(cleaned_text, tool_calls),
            )
            with self._conversation_state_lock:
                self._conversation_states[affinity_key] = state

        message = SimpleNamespace(
            content=cleaned_text, tool_calls=tool_calls, reasoning=reasoning or None, reasoning_content=reasoning or None,
            reasoning_details=None,
        )
        completion = SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="tool_calls" if tool_calls else "stop")],
            # ACP v1 does not define token-usage metadata on session/prompt.  Treat
            # that as unavailable instead of fabricating an exact-looking zero;
            # Hermes will keep accounting gated on real provider usage and use a
            # clearly-marked local estimate for context occupancy.
            usage=None,
            model=model or "copilot-acp",
        )
        return _completion_to_stream_chunks(completion) if stream else completion

    def _spawn(self, *, affinity_key: str = "") -> subprocess.Popen[str]:
        # Fast-fail when the CLI rejects --acp (else the parent waits the full child timeout for stdout that
        # never arrives). ``None`` falls through to the spawn's established start error.
        if _acp_supported(self._acp_command, self._acp_args) is False:
            preview = " ".join(self._acp_args[:3]) if self._acp_args else "(none)"
            raise RuntimeError(
                f"ACP transport not supported by '{self._acp_command}': `{preview}` is rejected as an unknown option. This "
                "usually means the CLI is an older release (e.g. Claude Code v2.x) or a different tool than expected. Either "
                "install a CLI that ships with --acp support (e.g. `@github/copilot` late 2025+), or set "
                "HERMES_COPILOT_ACP_COMMAND / HERMES_COPILOT_ACP_ARGS to a working pair."
            )
        try:
            from hermes_cli._subprocess_compat import windows_hide_flags  # hide the Windows console flash (#56747); pipes intact for the ACP wire

            # Hide the console the CLI child would otherwise flash on Windows (#56747). Hide-only — stdio
            # pipes stay intact for the ACP wire.
            child_env = _build_subprocess_env()
            if affinity_key:
                child_env["ACP_MUX_AFFINITY"] = affinity_key
            proc = subprocess.Popen(
                [self._acp_command] + self._acp_args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding='utf-8', errors='replace', bufsize=1, cwd=self._acp_cwd, env=child_env,
                creationflags=windows_hide_flags(),
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"Could not start Copilot ACP command '{self._acp_command}'. Install GitHub Copilot CLI or set "
                               "HERMES_COPILOT_ACP_COMMAND/COPILOT_CLI_PATH.") from exc
        if proc.stdin is None or proc.stdout is None:
            proc.kill()
            raise RuntimeError("Copilot ACP process did not expose stdin/stdout pipes.")
        with self._active_process_lock:
            self._active_processes.add(proc)
            self.is_closed = False
        return proc

    @contextlib.contextmanager
    def _session(
        self, timeout_seconds: float, *, allow_file_requests: bool = True, affinity_key: str = "",
        resume_session_id: str = "",
    ) -> Iterator[tuple[dict[str, Any], Callable[..., Any]]]:
        """Start one ACP process and yield a new or resumed session plus request callable."""
        proc = self._spawn(affinity_key=affinity_key)
        inbox: queue.Queue[dict[str, Any]] = queue.Queue()
        stderr_tail: deque[str] = deque(maxlen=40)

        def _decode(line: str) -> dict[str, Any]:
            try:
                return json.loads(line)
            except Exception:
                return {"raw": line.rstrip("\n")}

        def _pump(stream, sink) -> None:
            for line in stream or ():
                sink(line)

        threading.Thread(target=_pump, args=(proc.stdout, lambda line: inbox.put(_decode(line))), daemon=True).start()
        stderr_pump = threading.Thread(
            target=_pump, args=(proc.stderr, lambda line: stderr_tail.append(line.rstrip("\n"))), daemon=True)
        stderr_pump.start()
        request_ids = iter(range(1, 1 << 62))
        # One budget for the WHOLE session (initialize + session/new + any prompt), not per
        # request: a hung CLI must not get 2x the caller's timeout on the foreground /model path.
        session_deadline = time.monotonic() + timeout_seconds

        def _request(method: str, params: dict[str, Any], *, text_parts: list[str] | None = None,
                     reasoning_parts: list[str] | None = None) -> Any:
            request_id = next(request_ids)
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n")
            proc.stdin.flush()
            deadline = session_deadline
            while time.monotonic() < deadline and proc.poll() is None:
                try:
                    msg = inbox.get(timeout=0.1)
                except queue.Empty:
                    continue
                if self._handle_server_message(
                    msg, process=proc, cwd=self._acp_cwd, text_parts=text_parts,
                    reasoning_parts=reasoning_parts, allow_file_requests=allow_file_requests,
                ) or msg.get("id") != request_id:
                    continue
                if "error" in msg:
                    err = msg.get("error") or {}
                    raise RuntimeError(f"Copilot ACP {method} failed: {err.get('message') or err}")
                return msg.get("result")
            if proc.poll() is not None:
                # The pump can still hold the crash text when poll() first sees the exit; reading
                # it too early turned a dead CLI into a TimeoutError, which retries differently.
                stderr_pump.join(timeout=1.0)
                stderr_text = "\n".join(stderr_tail).strip()
                if _is_gh_copilot_deprecation_message(stderr_text):
                    raise RuntimeError(_DEPRECATED_CLI_ERROR + stderr_text)
                raise RuntimeError(f"Copilot ACP process exited early: {stderr_text or f'exit code {proc.returncode}'}")
            raise TimeoutError(f"Timed out waiting for Copilot ACP response to {method}.")

        try:
            _request("initialize", _INITIALIZE_PARAMS)
            resume_session_id = str(resume_session_id or "").strip()
            if resume_session_id:
                session = _request(
                    "session/resume",
                    {"sessionId": resume_session_id, "cwd": self._acp_cwd, "mcpServers": []},
                ) or {}
                session = dict(session)
                session.setdefault("sessionId", resume_session_id)
                session["_hermes_resumed"] = True
            else:
                session = _request("session/new", {"cwd": self._acp_cwd, "mcpServers": []}) or {}
            if not str(session.get("sessionId") or "").strip():
                raise RuntimeError("Copilot ACP did not return a sessionId.")
            yield session, _request
        finally:
            self._release_process(proc)

    def list_models(self, *, timeout_seconds: float = 15.0) -> list[str]:
        """Return enabled models from a deliberately one-shot discovery client."""
        try:
            with self._session(timeout_seconds, allow_file_requests=False) as (session, _):
                return _session_model_ids(session)
        finally:
            self.close()

    def _run_prompt(
        self, prompt_text: str, *, timeout_seconds: float, model: str | None = None, affinity_key: str = "",
        resume_session_id: str = "", full_prompt_text: str | None = None,
        session_result: dict[str, Any] | None = None,
    ) -> tuple[str, str]:
        """Run one prompt, resuming the ACP-native session when possible.

        A resume miss (notably after mux account failover) is the one safe automatic
        downgrade: create a fresh session and replay ``full_prompt_text`` once. Other
        resume errors propagate so the provider's bounded retry/failover policy owns them.
        """
        requested_model = str(model or "").strip()

        def _run_once(text: str, resume_id: str) -> tuple[str, str]:
            with self._session(
                timeout_seconds, affinity_key=affinity_key, resume_session_id=resume_id,
            ) as (session, _request):
                session_id = str(session.get("sessionId") or "").strip()
                resumed = bool(session.get("_hermes_resumed"))
                # New sessions need explicit model selection. Resumed sessions already persist
                # the selected model; avoiding a redundant set_config call keeps the native
                # continuation prefix stable.
                if not resumed and requested_model and requested_model != "copilot-acp":
                    try:
                        if (selection := _model_selection_request(session, requested_model)) is not None:
                            _request(*selection)
                        else:
                            logger.warning("Copilot ACP does not offer model %r; using the session default.", requested_model)
                    except Exception as exc:
                        logger.warning(
                            "Copilot ACP model selection for %r failed; continuing with the session default: %s",
                            requested_model, exc,
                        )
                text_parts: list[str] = []
                reasoning_parts: list[str] = []
                prompt = {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]}
                _request("session/prompt", prompt, text_parts=text_parts, reasoning_parts=reasoning_parts)
                if session_result is not None:
                    session_result.clear()
                    session_result.update(session_id=session_id, resumed=resumed)
                return "".join(text_parts), "".join(reasoning_parts)

        resume_session_id = str(resume_session_id or "").strip()
        if resume_session_id:
            try:
                return _run_once(prompt_text, resume_session_id)
            except RuntimeError as exc:
                if not _resume_session_missing(exc):
                    raise
                logger.info(
                    "ACP session %s is unavailable in the selected account/home; rebuilding with one full replay.",
                    resume_session_id[:12],
                )
                return _run_once(full_prompt_text or prompt_text, "")
        return _run_once(prompt_text, "")

    def _handle_server_message(
        self, msg: dict[str, Any], *, process: subprocess.Popen[str], cwd: str, text_parts: list[str] | None, reasoning_parts: list[str] | None,
        allow_file_requests: bool = True,
    ) -> bool:
        """Consume a server->client message; True when handled (notification or request answered)."""
        method = msg.get("method")
        if not isinstance(method, str):
            return False
        if method == "session/update":
            update = (msg.get("params") or {}).get("update") or {}
            content = update.get("content") or {}
            chunk_text = str(content.get("text") or "") if isinstance(content, dict) else ""
            sinks = {"agent_message_chunk": text_parts, "agent_thought_chunk": reasoning_parts}
            if chunk_text and (sink := sinks.get(str(update.get("sessionUpdate") or "").strip())) is not None:
                sink.append(chunk_text)
            return True
        if process.stdin is None:
            return True
        message_id = msg.get("id")
        if method == "session/request_permission":
            response = _jsonrpc_result(message_id, {"outcome": {"outcome": "cancelled"}})
        elif method in _FS_HANDLERS:
            if not allow_file_requests:
                response = _jsonrpc_error(message_id, -32601, "File access is unavailable during model discovery.")
            else:
                try:
                    response = _jsonrpc_result(message_id, _FS_HANDLERS[method](msg.get("params") or {}, cwd))
                except Exception as exc:
                    response = _jsonrpc_error(message_id, -32602, str(exc))
        else:
            response = _jsonrpc_error(message_id, -32601, f"ACP client method '{method}' is not supported by Hermes yet.")
        process.stdin.write(json.dumps(response) + "\n")
        process.stdin.flush()
        return True
