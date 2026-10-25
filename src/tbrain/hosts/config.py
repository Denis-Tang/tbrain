"""Filesystem helpers shared by the four host integrations.

Everything here is testable in isolation: the user home, each host's config
directory and the source ``integrations`` directory can all be redirected with
environment variables or an explicit argument, so tests never read or write a real
host configuration.

Writes are conservative:

* an existing file is backed up next to itself as ``<name>.tbrain-<timestamp>.bak``;
* JSON is parsed/merged and written atomically;
* TOML is edited as text around markers because the standard library can only
  *read* TOML -- this preserves every unrelated line and comment;
* YAML patch files are appended/removed as a marked block for the same reason.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

TOML_MARK_START = "# >>> tbrain (managed by Tbrain; do not edit inside this block)"
TOML_MARK_END = "# <<< tbrain"
YAML_MARK_START = "# >>> tbrain (managed by Tbrain; safe to remove)"
YAML_MARK_END = "# <<< tbrain"
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "PreCompact", "SessionEnd")
SUPPORTED_HOSTS = ("claude-code", "codex", "dsh", "pi")
HOST_ALIASES = {"claude": "claude-code", "claude-code": "claude-code",
                "codex": "codex", "dsh": "dsh", "dsh-native": "dsh", "pi": "pi"}


class HostError(RuntimeError):
    """A short, user-facing error from a host adapter."""


def canonical_host(host: str) -> str:
    key = str(host or "").strip().lower()
    key = HOST_ALIASES.get(key, key)
    if key not in SUPPORTED_HOSTS:
        raise HostError(f"未知宿主：{host}（可选：claude-code、codex、dsh、pi）")
    return key


# ---------------------------------------------------------------------------
# base directories
# ---------------------------------------------------------------------------

def user_home() -> Path:
    """Overridable in tests via ``monkeypatch.setattr(config, "user_home", ...)``."""
    return Path.home()


def claude_paths() -> dict:
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR", "").strip() or (user_home() / ".claude"))
    # ~/.claude.json is a sibling of ~/.claude (user-scope MCP servers live there);
    # when CLAUDE_CONFIG_DIR points at a custom folder we keep the sibling layout.
    return {"root": root, "settings": root / "settings.json", "mcp": root.parent / ".claude.json"}


def codex_paths() -> dict:
    root = Path(os.environ.get("CODEX_HOME", "").strip() or (user_home() / ".codex"))
    return {"root": root, "hooks": root / "hooks.json", "config": root / "config.toml"}


def dsh_paths(profile: str | None = None) -> dict:
    root = Path(os.environ.get("DSH_HOME", "").strip() or (user_home() / ".dsh"))
    name = str(profile or os.environ.get("TBRAIN_DSH_PROFILE", "").strip() or "desktop")
    folder = root / "profiles" / name
    return {"root": root, "profile": name, "folder": folder,
            "patch": folder / "cordis.patch.yml", "package": folder / "package.json",
            "node_modules": folder / "node_modules"}


def pi_paths() -> dict:
    root = Path(os.environ.get("PI_CODING_AGENT_DIR", "").strip() or (user_home() / ".pi" / "agent"))
    return {"root": root, "settings": root / "settings.json",
            "extensions": root / "extensions", "extension": root / "extensions" / "tbrain.ts"}


def integrations_source() -> Path:
    """Locate the shipped ``integrations`` directory (repo, installed package or frozen build)."""
    override = os.environ.get("TBRAIN_INTEGRATIONS_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "integrations"
    # src/tbrain/hosts/config.py -> repo root / integrations
    candidate = Path(__file__).resolve().parents[3] / "integrations"
    if candidate.is_dir():
        return candidate
    installed = Path(__file__).resolve().parent.parent / "integrations"
    if installed.is_dir():
        return installed
    raise HostError("找不到 integrations 目录（安装包不完整），请重新安装 Tbrain。")


# ---------------------------------------------------------------------------
# backups and atomic writes
# ---------------------------------------------------------------------------

def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def backup_file(path: Path) -> Path | None:
    """Copy an existing file to ``<name>.tbrain-<timestamp>.bak``; None when absent."""
    path = Path(path)
    if not path.is_file():
        return None
    target = path.with_name(f"{path.name}.tbrain-{timestamp()}.bak")
    counter = 1
    while target.exists():
        target = path.with_name(f"{path.name}.tbrain-{timestamp()}-{counter}.bak")
        counter += 1
    shutil.copy2(path, target)
    return target


def read_json(path: Path, default=None):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {} if default is None else default
    return payload


def write_json_atomic(path: Path, payload) -> None:
    path = Path(path)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    write_text_atomic(path, text)


def write_text_atomic(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def read_text(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8-sig")
    except OSError:
        return ""


def mtime_ns(path: Path) -> int:
    try:
        return Path(path).stat().st_mtime_ns
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# TOML (text-level, comment preserving)
# ---------------------------------------------------------------------------

def toml_string(value: str) -> str:
    """Quote a TOML string; literal form avoids backslash escaping on Windows."""
    text = str(value)
    if "'" not in text:
        return f"'{text}'"
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def strip_managed_block(text: str, start: str = TOML_MARK_START, end: str = TOML_MARK_END) -> str:
    """Remove a previously written marked block (idempotent re-connect)."""
    lines = text.splitlines()
    output: list[str] = []
    inside = False
    for line in lines:
        if line.strip() == start:
            inside = True
            continue
        if inside:
            if line.strip() == end:
                inside = False
            continue
        output.append(line)
    return "\n".join(output).rstrip("\n") + "\n" if output else ""


def append_managed_block(text: str, block: str, start: str = TOML_MARK_START,
                         end: str = TOML_MARK_END) -> str:
    body = text.rstrip("\n")
    managed = f"{start}\n{block.rstrip()}\n{end}"
    return (body + "\n\n" + managed + "\n") if body else (managed + "\n")


def strip_toml_section(text: str, header: str) -> str:
    """Remove one ``[section]`` (or ``[[section]]``) block; keeps everything else."""
    lines = text.splitlines()
    output: list[str] = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        is_header = stripped.startswith("[") and stripped.endswith("]")
        if is_header:
            skipping = stripped == header
            if skipping:
                continue
        if skipping:
            continue
        output.append(line)
    return "\n".join(output).rstrip("\n") + "\n" if output else ""


def has_toml_section(text: str, header: str) -> bool:
    for line in text.splitlines():
        if line.strip() == header:
            return True
    return False


# ---------------------------------------------------------------------------
# YAML markers
# ---------------------------------------------------------------------------

def strip_yaml_block(text: str) -> str:
    lines = text.splitlines()
    output: list[str] = []
    inside = False
    for line in lines:
        if line.strip() == YAML_MARK_START:
            inside = True
            continue
        if inside:
            if line.strip() == YAML_MARK_END:
                inside = False
            continue
        output.append(line)
    return "\n".join(output).rstrip("\n") + "\n" if output else ""


def append_yaml_block(text: str, block: str) -> str:
    body = text.rstrip("\n")
    managed = f"{YAML_MARK_START}\n{block.rstrip()}\n{YAML_MARK_END}"
    return (body + "\n\n" + managed + "\n") if body else (managed + "\n")


def yaml_scalar(value: str) -> str:
    text = str(value)
    return "'" + text.replace("'", "''") + "'"


def find_yaml_ids(text: str) -> list[str]:
    return re.findall(r"^\s*-\s+id:\s*['\"]?([\w.-]+)", text, flags=re.MULTILINE)


# ---------------------------------------------------------------------------
# connect state (doctor heuristics, e.g. Codex hook trust freshness)
# ---------------------------------------------------------------------------

def connect_state_path(app_home: Path) -> Path:
    return Path(app_home) / "connect-state.json"


def read_connect_state(app_home: Path) -> dict:
    payload = read_json(connect_state_path(app_home))
    return payload if isinstance(payload, dict) else {}


def write_connect_state(app_home: Path, host: str, values: dict) -> dict:
    path = connect_state_path(app_home)
    state = read_connect_state(app_home)
    state[host] = {**(state.get(host) or {}), **values, "enabled": True, "at": datetime.now().isoformat(timespec="seconds")}
    write_json_atomic(path, state)
    return state


def clear_connect_state(app_home: Path, host: str) -> None:
    path = connect_state_path(app_home)
    state = read_connect_state(app_home)
    state[host] = {'enabled': False}
    write_json_atomic(path, state)
