"""Codex adapter: ``~/.codex/hooks.json`` (command + commandWindows) and the
user-level MCP server in ``~/.codex/config.toml``.

Codex only runs a non-managed hook after the user trusts it (trust is recorded as
a hash in ``config.toml [hooks.state.*]``).  We never write ``trusted_hash``
ourselves: the CLI prints the manual step and ``doctor`` reports "pending" as
yellow until the user approves the hooks with Codex's native ``/hooks`` command.
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime
from pathlib import Path

from . import base, config

HOST = "codex"
EVENT_SNAKES = ("session_start", "user_prompt_submit", "stop", "pre_compact", "session_end")


def _entry(app_home: Path | None, install_root: Path | None, settings) -> tuple[Path, Path, str]:
    home = base.default_app_home(app_home)
    _, entry = base.entry_for(home, install_root, settings)
    return home, base.resolve_install_root(home, install_root, settings), entry


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _mcp_block(app_home: Path, entry: str) -> str:
    lines = ["[mcp_servers.tbrain]",
             f"command = {config.toml_string(entry)}",
             "args = [" + ", ".join(config.toml_string(item)
                                    for item in base.mcp_args(app_home, HOST)) + "]",
             "startup_timeout_sec = 20.0",
             "tool_timeout_sec = 120.0"]
    return "\n".join(lines)


def _parse_config(text: str) -> dict:
    try:
        import tomllib

        payload = tomllib.loads(text)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _trust_summary(config_text: str, hooks_path: Path) -> dict:
    payload = _parse_config(config_text)
    state = ((payload.get("hooks") or {}).get("state") or {}) if isinstance(payload, dict) else {}
    expected_path = str(hooks_path).lower().replace("/", "\\")
    events: list[str] = []
    if isinstance(state, dict):
        for key, value in state.items():
            key_text = str(key).lower().replace("/", "\\")
            if expected_path not in key_text:
                continue
            if not isinstance(value, dict) or not value.get("trusted_hash"):
                continue
            for event in EVENT_SNAKES:
                if f":{event}:" in key_text:
                    events.append(event)
    return {"trusted": bool(events), "events": sorted(set(events)),
            "parsed": bool(payload)}


def _strip_trust_sections(text: str, hooks_path: Path) -> str:
    """Remove [hooks.state.*] sections that only describe our hooks.json."""
    expected = str(hooks_path).lower().replace("/", "\\")
    lines = text.splitlines()
    output: list[str] = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            header = stripped.lower().replace("/", "\\")
            skipping = header.startswith("[hooks.state.") and expected in header
            if skipping:
                continue
        if skipping:
            continue
        output.append(line)
    return "\n".join(output).rstrip("\n") + "\n" if output else ""


def connect(host: str = HOST, app_home: Path | None = None,
            install_root: Path | None = None, settings=None) -> dict:
    home, root, entry = _entry(app_home, install_root, settings)
    locations = config.codex_paths()
    hooks_path = locations["hooks"]
    config_path = locations["config"]
    files: list[str] = []
    backups: list[str] = []
    warnings: list[str] = []

    if not Path(entry).is_file():
        warnings.append(f"固定入口尚未安装：{entry}（先运行 tbrain install）")

    # -- hooks.json ------------------------------------------------------
    hooks_before = config.read_text(hooks_path)
    hooks_payload = config.read_json(hooks_path)
    if not isinstance(hooks_payload, dict):
        hooks_payload = {}
    backup = config.backup_file(hooks_path)
    if backup:
        backups.append(str(backup))
    base.merge_tbrain_hooks(hooks_payload, base.codex_handlers(HOST, entry, home), entry)
    config.write_json_atomic(hooks_path, hooks_payload)
    files.append(str(hooks_path))
    hooks_text = hooks_path.read_text(encoding="utf-8-sig")

    # -- config.toml (text-level edit keeps user comments/order) ----------
    config_before = config.read_text(config_path)
    trust_before = _trust_summary(config_before, hooks_path)
    backup = config.backup_file(config_path)
    if backup:
        backups.append(str(backup))
    text = config.strip_managed_block(config_before)
    text = config.strip_toml_section(text, "[mcp_servers.tbrain]")
    text = config.append_managed_block(text, _mcp_block(home, entry))
    config.write_text_atomic(config_path, text)
    files.append(str(config_path))

    # Record enough information for doctor to reason about hook-trust freshness.
    pre_sha = _sha256_text(hooks_before) if hooks_before else ""
    post_sha = _sha256_text(hooks_text)
    trust_at_connect = bool(trust_before["trusted"] and pre_sha and pre_sha == post_sha)
    config.write_connect_state(home, HOST, {
        "entry": entry,
        "install_root": str(root),
        "files": files,
        "hooks": str(hooks_path),
        "config": str(config_path),
        "hooks_sha": post_sha,
        "trust_at_connect": trust_at_connect,
        "configured_at_ns": time.time_ns(),
        "configured_at": datetime.now().isoformat(timespec="seconds"),
    })
    warnings.append("Codex 的 hook 需要用户信任：在 Codex 中执行 /hooks 审核并信任 Tbrain hooks；"
                    "未信任前 hook 不会执行（doctor 显示为待信任）。")
    return {"ok": True, "host": HOST, "entry": entry, "install_root": str(root),
            "files": files, "backups": backups, "warnings": warnings,
            "hook_trust": "pending",
            "detail": "Codex 已接入固定入口（hooks.json + config.toml）；hook 需在 /hooks 中信任，重开 Codex 生效。"}


def disconnect(host: str = HOST, app_home: Path | None = None,
               install_root: Path | None = None, settings=None) -> dict:
    home, root, entry = _entry(app_home, install_root, settings)
    locations = config.codex_paths()
    files: list[str] = []
    backups: list[str] = []
    removed = 0

    hooks_path = locations["hooks"]
    hooks_payload = config.read_json(hooks_path)
    if isinstance(hooks_payload, dict) and hooks_payload:
        backup = config.backup_file(hooks_path)
        if backup:
            backups.append(str(backup))
        removed += base.remove_tbrain_handlers(hooks_payload, entry)
        config.write_json_atomic(hooks_path, hooks_payload)
        files.append(str(hooks_path))

    config_path = locations["config"]
    text = config.read_text(config_path)
    if text:
        backup = config.backup_file(config_path)
        if backup:
            backups.append(str(backup))
        text = config.strip_managed_block(text)
        if config.has_toml_section(text, "[mcp_servers.tbrain]"):
            text = config.strip_toml_section(text, "[mcp_servers.tbrain]")
            removed += 1
        text = _strip_trust_sections(text, hooks_path)
        config.write_text_atomic(config_path, text)
        files.append(str(config_path))

    config.clear_connect_state(home, HOST)
    return {"ok": True, "host": HOST, "entry": entry, "files": files,
            "backups": backups, "removed": removed,
            "detail": "已移除 Codex 中的 Tbrain hook 与 MCP 条目（其他配置保留；信任状态随 hook 一并清除）。"}


def doctor(host: str = HOST, app_home: Path | None = None,
           install_root: Path | None = None, settings=None) -> dict:
    home, root, entry = _entry(app_home, install_root, settings)
    locations = config.codex_paths()
    hooks_path = locations["hooks"]
    config_path = locations["config"]
    hooks_payload = config.read_json(hooks_path)
    configured = 0
    if isinstance(hooks_payload, dict):
        for event in config.HOOK_EVENTS:
            groups = base._handler_groups(hooks_payload, event)
            if any(base.is_tbrain_handler(handler, entry)
                   for group in groups if isinstance(group, dict)
                   for handler in (group.get("hooks") or [])):
                configured += 1
    text = config.read_text(config_path)
    mcp_entry = (_parse_config(text).get('mcp_servers') or {}).get('tbrain')
    mcp_configured = bool(mcp_entry) and mcp_entry.get('enabled', True)
    item = {"id": HOST, "host": HOST, 'mcp': bool(mcp_configured), 'hooks': configured == len(config.HOOK_EVENTS),
            'enabled': bool(mcp_entry or configured), 'paths': [str(config_path), str(hooks_path)],
            'reload': '重开 Codex；在 /hooks 审核并信任 Tbrain Hooks。关闭配置后也需重开。'}
    if not configured and not mcp_configured:
        item.update(status="yellow", detail="未接入 Codex；运行 tbrain connect codex。")
        return item
    if not Path(entry).is_file():
        item.update(status="red", detail=f"固定入口缺失：{entry}（重新安装 Tbrain）")
        return item
    if not mcp_configured:
        item.update(status="yellow", detail="hooks.json 已写入，但 config.toml 缺少 [mcp_servers.tbrain]。")
        return item
    if mcp_entry.get('command') != entry or mcp_entry.get('args') != base.mcp_args(home, HOST) or not item['hooks']:
        item.update(status='yellow', detail='MCP 固定入口或 Hook 配置不完整，重新开启可修复。')
        return item
    trust = _trust_summary(text, hooks_path)
    state = config.read_connect_state(home).get(HOST) or {}
    if not trust["trusted"]:
        item.update(status="yellow",
                    detail="Codex hook 待用户信任：在 Codex 中执行 /hooks 审核并信任 Tbrain hooks（需由你在 Codex 内确认）。")
        return item
    hooks_now = config.read_text(hooks_path)
    current_sha = hashlib.sha256(hooks_now.encode("utf-8")).hexdigest() if hooks_now else ""
    recorded_sha = str(state.get("hooks_sha") or "")
    if recorded_sha and current_sha and current_sha != recorded_sha:
        item.update(status="yellow",
                    detail="hooks.json 在接入后发生变化，信任记录可能失效：重新在 Codex 执行 /hooks 信任。")
        return item
    if bool(state.get("trust_at_connect")):
        item.update(status="green", detail="Codex 已接入且 hook 已信任（固定入口；新会话生效）。")
        return item
    configured_at_ns = int(state.get("configured_at_ns") or 0)
    if configured_at_ns and config.mtime_ns(config_path) >= configured_at_ns:
        item.update(status="green", detail="Codex 已接入且 hook 已信任（固定入口；新会话生效）。")
        return item
    item.update(status="yellow",
                detail="Codex hook 待用户信任（接入后尚未看到信任记录）：在 Codex 执行 /hooks，"
                       "审核并信任 Tbrain hooks；不自动绕过信任。")
    return item
