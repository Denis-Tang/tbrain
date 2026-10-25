"""Claude Code adapter: user-level hooks in ``~/.claude/settings.json`` and the
user-scope MCP server in ``~/.claude.json``.

Only Tbrain's own handlers/entries are added or removed; backups are written next
to each modified file.  Hook commands use ``shell: "powershell"`` with the fully
quoted entry because exec-form commands degrade badly on Windows (see
docs/RESEARCH-hosts.md §1.1).
"""

from __future__ import annotations

from pathlib import Path

from . import base, config

HOST = "claude-code"


def _entry(app_home: Path | None, install_root: Path | None, settings) -> tuple[Path, Path, str]:
    home = base.default_app_home(app_home)
    _, entry = base.entry_for(home, install_root, settings)
    return home, base.resolve_install_root(home, install_root, settings), entry


def _hooks_configured(payload: dict, entry: str) -> list[str]:
    configured = []
    for event in config.HOOK_EVENTS:
        groups = base._handler_groups(payload, event)
        if any(base.is_tbrain_handler(handler, entry)
               for group in groups if isinstance(group, dict)
               for handler in (group.get("hooks") or [])):
            configured.append(event)
    return configured


def connect(host: str = HOST, app_home: Path | None = None,
            install_root: Path | None = None, settings=None) -> dict:
    home, root, entry = _entry(app_home, install_root, settings)
    files: list[str] = []
    backups: list[str] = []
    warnings: list[str] = []
    locations = config.claude_paths()

    if not Path(entry).is_file():
        warnings.append(f"固定入口尚未安装：{entry}（先运行 tbrain install）")

    # 1) user-level hook settings
    settings_path = locations["settings"]
    payload = config.read_json(settings_path)
    if not isinstance(payload, dict):
        payload = {}
    backup = config.backup_file(settings_path)
    if backup:
        backups.append(str(backup))
    base.merge_tbrain_hooks(payload, base.claude_handlers(HOST, entry, home), entry)
    config.write_json_atomic(settings_path, payload)
    files.append(str(settings_path))

    # 2) user-scope MCP server
    mcp_path = locations["mcp"]
    mcp_payload = config.read_json(mcp_path)
    if not isinstance(mcp_payload, dict):
        mcp_payload = {}
    backup = config.backup_file(mcp_path)
    if backup:
        backups.append(str(backup))
    servers = mcp_payload.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
        mcp_payload["mcpServers"] = servers
    servers["tbrain"] = base.mcp_server_entry(home, entry)
    config.write_json_atomic(mcp_path, mcp_payload)
    files.append(str(mcp_path))

    config.write_connect_state(home, HOST, {"entry": entry, "install_root": str(root),
                                            "files": files, "backups": backups})
    return {"ok": True, "host": HOST, "entry": entry, "install_root": str(root),
            "files": files, "backups": backups, "warnings": warnings,
            "detail": "Claude Code 已接入固定入口；重开 Claude Code 会话后生效。"}


def disconnect(host: str = HOST, app_home: Path | None = None,
               install_root: Path | None = None, settings=None) -> dict:
    home, root, entry = _entry(app_home, install_root, settings)
    files: list[str] = []
    backups: list[str] = []
    removed = 0
    locations = config.claude_paths()

    settings_path = locations["settings"]
    payload = config.read_json(settings_path)
    if isinstance(payload, dict) and payload:
        backup = config.backup_file(settings_path)
        if backup:
            backups.append(str(backup))
        removed += base.remove_tbrain_handlers(payload, entry)
        config.write_json_atomic(settings_path, payload)
        files.append(str(settings_path))

    mcp_path = locations["mcp"]
    mcp_payload = config.read_json(mcp_path)
    if isinstance(mcp_payload, dict) and mcp_payload:
        backup = config.backup_file(mcp_path)
        if backup:
            backups.append(str(backup))
        servers = mcp_payload.get("mcpServers")
        if isinstance(servers, dict) and servers.pop("tbrain", None) is not None:
            removed += 1
            if not servers:
                mcp_payload.pop("mcpServers", None)
        config.write_json_atomic(mcp_path, mcp_payload)
        files.append(str(mcp_path))

    config.clear_connect_state(home, HOST)
    return {"ok": True, "host": HOST, "entry": entry, "files": files,
            "backups": backups, "removed": removed,
            "detail": "已移除 Claude Code 中的 Tbrain hook 与 MCP 条目；其他配置保留。"}


def doctor(host: str = HOST, app_home: Path | None = None,
           install_root: Path | None = None, settings=None) -> dict:
    home, root, entry = _entry(app_home, install_root, settings)
    locations = config.claude_paths()
    payload = config.read_json(locations["settings"])
    mcp_payload = config.read_json(locations["mcp"])
    configured = _hooks_configured(payload, entry) if isinstance(payload, dict) else []
    servers = mcp_payload.get("mcpServers") if isinstance(mcp_payload, dict) else {}
    mcp_entry = servers.get("tbrain") if isinstance(servers, dict) else None
    item = {"id": HOST, "host": HOST}

    if not configured and not mcp_entry:
        item.update(status="yellow", detail="未接入 Claude Code；运行 tbrain connect claude-code。")
    elif not Path(entry).is_file():
        item.update(status="red", detail=f"固定入口缺失：{entry}（重新安装 Tbrain）")
    elif not mcp_entry:
        item.update(status="yellow", detail="hook 已写入，但 ~/.claude.json 缺少 tbrain MCP 条目。")
    elif str((mcp_entry or {}).get("command") or "") != entry:
        item.update(status="yellow",
                    detail="MCP 条目和固定入口不一致；重新运行 tbrain connect claude-code。")
    else:
        item.update(status="green",
                    detail=f"已接入 {len(configured)}/5 个 hook 事件与用户级 MCP；新会话生效。")
    return item
