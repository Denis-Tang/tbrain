"""Host-neutral pieces shared by the four adapters.

The adapters translate between a host's wire format and the neutral
:class:`tbrain.hooks.HookEvent` / :class:`tbrain.hooks.HookResult` pair.  Commands
always point at the *fixed entry* (``bin/tbrain.cmd``), which reads ``current.txt``
and starts the selected version, so an upgrade never changes host configuration --

    "<install root>/bin/tbrain.cmd" hook --host <host> [--home <app home>]

The ``--home`` argument is only appended when the application data directory is
not the default ``~/.tbrain`` (keeps the common command short and stable).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .. import installer, paths
from ..hooks import HookEvent, HookResult
from . import config

HOST_EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "PreCompact", "SessionEnd")

# Host event name -> neutral kind.  Unknown events normalize to kind="" and are
# ignored by the service layer.
EVENT_KINDS = {
    "SessionStart": "session-start",
    "session_start": "session-start",
    "UserPromptSubmit": "user-prompt",
    "user_prompt_submit": "user-prompt",
    "Stop": "stop",
    "stop": "stop",
    "PreCompact": "pre-compact",
    "pre_compact": "pre-compact",
    "SessionEnd": "session-end",
    "session_end": "session-end",
    "Context": "context",
    "context": "context",
    "PostToolUse": "tool-result",
    "ToolResult": "tool-result",
}

PI_SOURCE_MAP = {"startup": "startup", "reload": "startup", "new": "startup",
                 "resume": "resume", "fork": "fork", "compact": "compact", "clear": "clear"}


@dataclass
class HostSpec:
    key: str
    display: str
    supports_context: bool
    events: tuple = HOST_EVENTS
    notes: str = ""
    hook_config_paths: list[Path] = field(default_factory=list)


HOST_SPECS = {
    "claude-code": HostSpec("claude-code", "Claude Code", supports_context=False,
                            notes="用户级 settings.json hooks + ~/.claude.json MCP"),
    "codex": HostSpec("codex", "Codex", supports_context=False,
                      notes="hooks.json（需信任）+ config.toml MCP"),
    "dsh": HostSpec("dsh", "DeepSeek Harness", supports_context=True,
                    notes="cordis.patch.yml：MCP + Claude hooks 桥 + 原生插件"),
    "pi": HostSpec("pi", "pi coding agent", supports_context=True,
                   notes="TS 扩展，直接调用固定入口，无 MCP"),
}


def host_spec(host: str) -> HostSpec:
    return HOST_SPECS[config.canonical_host(host)]


def default_app_home(app_home: Path | None = None) -> Path:
    return Path(app_home) if app_home is not None else paths.app_home()


def resolve_install_root(app_home: Path | None, install_root: Path | None, settings=None) -> Path:
    if install_root is not None:
        return Path(install_root)
    return paths.install_root(app_home, settings)


def entry_for(app_home: Path | None = None, install_root: Path | None = None,
              settings=None) -> tuple[Path, str]:
    home = default_app_home(app_home)
    root = resolve_install_root(home, install_root, settings)
    return root, installer.entry_command(root)


# ---------------------------------------------------------------------------
# command strings
# ---------------------------------------------------------------------------

def is_default_app_home(app_home: Path) -> bool:
    try:
        return Path(app_home).expanduser().resolve() == (Path.home() / ".tbrain").resolve()
    except OSError:  # pragma: no cover - defensive
        return False


def hook_command(host: str, entry: str, app_home: Path) -> str:
    """The quoted hook command used in every host config."""
    command = f'"{entry}" hook --host {config.canonical_host(host)}'
    if not is_default_app_home(app_home):
        command += f' --home "{Path(app_home)}"'
    return command


def powershell_command(host: str, entry: str, app_home: Path) -> str:
    return "& " + hook_command(host, entry, app_home)


def mcp_args(app_home: Path, host: str = "") -> list[str]:
    return ["--home", str(Path(app_home)), "mcp"] + (["--agent", host] if host else [])


def mcp_server_entry(app_home: Path, entry: str, host: str = "") -> dict:
    return {"type": "stdio", "command": entry, "args": mcp_args(app_home, host)}


# ---------------------------------------------------------------------------
# hooks payload builders
# ---------------------------------------------------------------------------

def _normalize_command_key(command: str) -> str:
    return command.replace("\\\\", "\\").lower()


def is_tbrain_handler(handler: dict | str, entry: str = "") -> bool:
    """True for a hook handler this program wrote (fixed entry or --host marker)."""
    if isinstance(handler, dict):
        texts = [str(handler.get(key) or "") for key in ("command", "commandWindows")]
    else:
        texts = [str(handler or "")]
    for text in texts:
        lowered = _normalize_command_key(text)
        if " hook --host " in lowered and "tbrain" in lowered:
            return True
        if entry and _normalize_command_key(entry) in lowered:
            return True
    return False


def _handler_groups(payload: dict, event: str) -> list:
    hooks = payload.get("hooks")
    if not isinstance(hooks, dict):
        return []
    groups = hooks.get(event)
    return groups if isinstance(groups, list) else []


def remove_tbrain_handlers(payload: dict, entry: str = "") -> int:
    """Drop Tbrain's hook handlers, keeping every other handler and event."""
    hooks = payload.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    removed = 0
    for event in list(hooks):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for group in groups:
            if not isinstance(group, dict):
                kept_groups.append(group)
                continue
            handlers = group.get("hooks")
            if isinstance(handlers, list):
                kept = [item for item in handlers if not is_tbrain_handler(item, entry)]
                removed += len(handlers) - len(kept)
                if not kept:
                    continue
                group = {**group, "hooks": kept}
            kept_groups.append(group)
        if kept_groups:
            hooks[event] = kept_groups
        else:
            hooks.pop(event, None)
    if not hooks:
        payload.pop("hooks", None)
    return removed


def merge_tbrain_hooks(payload: dict, handlers: dict[str, list[dict]], entry: str = "") -> dict:
    """Merge Tbrain handlers into a Claude/Codex-style hooks object."""
    remove_tbrain_handlers(payload, entry)
    hooks = payload.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        payload["hooks"] = hooks
    for event, items in handlers.items():
        groups = hooks.get(event)
        if not isinstance(groups, list):
            groups = []
            hooks[event] = groups
        groups.append({"hooks": items})
    return payload


def claude_handlers(host: str, entry: str, app_home: Path) -> dict[str, list[dict]]:
    handler = {"type": "command", "shell": "powershell",
               "command": powershell_command(host, entry, app_home), "timeout": 10}
    return {event: [dict(handler)] for event in HOST_EVENTS}


def codex_handlers(host: str, entry: str, app_home: Path) -> dict[str, list[dict]]:
    command = hook_command(host, entry, app_home)
    handler = {"type": "command", "command": command,
               "commandWindows": "& " + command, "timeout": 10}
    return {event: [{**handler, "timeout": 3 if event == "SessionEnd" else 10}]
            for event in HOST_EVENTS}


# ---------------------------------------------------------------------------
# normalize / render
# ---------------------------------------------------------------------------

def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value) -> bool:
    return bool(value)


def normalize(host: str, raw: dict) -> HookEvent:
    """Host hook payload -> neutral event (unknown payloads become kind="")."""
    key = config.canonical_host(host)
    payload = raw if isinstance(raw, dict) else {}
    event_name = str(payload.get("hook_event_name") or payload.get("event") or "")
    kind = EVENT_KINDS.get(event_name, "")
    source = str(payload.get("source") or payload.get("reason") or "")
    if key == "pi" and source:
        source = PI_SOURCE_MAP.get(source.lower(), source)
    session_id = str(payload.get("session_id") or payload.get("host_session_id")
                     or payload.get("sessionId") or "")
    cwd = str(payload.get("cwd") or payload.get("workspace_root") or "")
    prompt = str(payload.get("prompt") or payload.get("text") or "")
    missing = [name for name, value in (("hook_event_name", event_name), ("session_id", session_id))
               if not str(value or "").strip()]
    return HookEvent(
        host=key,
        kind=kind,
        host_session_id=session_id,
        cwd=cwd,
        source=source,
        prompt=prompt,
        stop_hook_active=_as_bool(payload.get("stop_hook_active")),
        turn_id=str(payload.get("turn_id") or ""),
        context_tokens=_as_int(payload.get("context_tokens")),
        context_window=_as_int(payload.get("context_window")),
        extra={"raw_event": event_name, "missing": missing,
               "host_record": str(payload.get("transcript_path") or payload.get("host_record") or ""),
               "last_test": payload.get("last_test"),
               "parent_session_id": str(payload.get("parent_session_id") or ""),
               "fork_user_count": payload.get("fork_user_count"),
               "fork_revision": str(payload.get("fork_revision") or "")},
    )


# Events whose ``hookSpecificOutput.additionalContext`` the hook runner accepts.
# Claude Code documents model-visible injection only for these events
# (docs/RESEARCH-hosts.md §1.5); any other event name is rejected and the whole
# hook output is dropped, so PreCompact/SessionEnd emit no ``hookSpecificOutput``
# and the context is re-injected by the SessionStart(compact) handler instead.
CONTEXT_EVENTS = {
    "claude-code": frozenset({
        "SessionStart", "SubagentStart", "UserPromptSubmit", "UserPromptExpansion",
        "PreToolUse", "PostToolUse", "PostToolUseFailure", "PostToolBatch",
        "Stop", "SubagentStop", "PostModelSwitch",
    }),
}


def render(host: str, result, event: str = "") -> dict:
    """Neutral result -> the dict the host's hook runner expects on stdout."""
    key = config.canonical_host(host)
    if result is None:
        return {}
    additional = str(getattr(result, "additional_context", "") or "")
    decision = str(getattr(result, "decision", "") or "")
    reason = str(getattr(result, "reason", "") or "")
    if key in ("pi", "dsh"):
        return {"additional_context": additional,
                "decision": "block" if decision == "block" else "",
                "reason": reason}
    if decision == "block":
        return {"decision": "block", "reason": reason}
    event_name = event or "UserPromptSubmit"
    if additional:
        allowed = CONTEXT_EVENTS.get(key)
        if allowed is None or event_name in allowed:
            return {"hookSpecificOutput": {"hookEventName": event_name,
                                           "additionalContext": additional}}
    return {}
