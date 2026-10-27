"""DSH 接入：固定入口 MCP + 统一原生生命周期插件。"""

from __future__ import annotations

import re
import json

import yaml
from pathlib import Path

from .. import __version__, paths
from . import base, config

HOST = "dsh"
MCP_CLIENT_PACKAGE = "@deepseek-ai/dsh-mcp-client"
REQUIRED_IDS = ("tbrain-mcp", "tbrain-native")
NATIVE_NAME = "tbrain-dsh.mjs"


def _context(app_home: Path | None, install_root: Path | None, settings):
    home = base.default_app_home(app_home)
    root = base.resolve_install_root(home, install_root, settings)
    _, entry = base.entry_for(home, root, settings)
    return home, root, entry


def _patch_block(app_home: Path, entry: str, plugin_uri: str) -> str:
    lines = [
        "- insert:",
        "    - id: tbrain-mcp",
        f"      name: {config.yaml_scalar(MCP_CLIENT_PACKAGE)}",
        "      config:",
        "        serverName: tbrain",
        "        transport: stdio",
        f"        command: {config.yaml_scalar(entry)}",
        f"        args: {json.dumps(base.mcp_args(app_home, HOST), ensure_ascii=False)}",
        "    - id: tbrain-native",
        f"      name: {config.yaml_scalar(plugin_uri)}",
        "      config:",
        f"        command: {config.yaml_scalar(entry)}",
        f"        home: {config.yaml_scalar(str(app_home))}",
    ]
    return "\n".join(lines)


def _install_native(app_home: Path) -> tuple[Path | None, list[str]]:
    """Copy the native cordis plugin into ``<app_home>/integrations`` (stable path)."""
    files: list[str] = []
    source = config.integrations_source() / NATIVE_NAME
    target_dir = paths.integrations_dir(app_home)
    if not source.is_file():
        return None, files
    target = target_dir / NATIVE_NAME
    target_dir.mkdir(parents=True, exist_ok=True)
    config.backup_file(target)
    config.write_text_atomic(target, source.read_text(encoding="utf-8"))
    files.append(str(target))
    package = target_dir / "package.json"
    config.backup_file(package)
    payload = {
        "name": "tbrain-dsh-native",
        "private": True,
        "type": "module",
        "version": str(__version__),
        "description": "Tbrain native DSH plugin (PreCompact/SessionEnd/context hints)",
    }
    config.write_json_atomic(package, payload)
    files.append(str(package))
    return target, files


def connect(host: str = HOST, app_home: Path | None = None,
            install_root: Path | None = None, settings=None, profile: str | None = None) -> dict:
    home, root, entry = _context(app_home, install_root, settings)
    locations = config.dsh_paths(profile)
    files: list[str] = []
    backups: list[str] = []
    warnings: list[str] = []

    if not Path(entry).is_file():
        warnings.append(f"固定入口尚未安装：{entry}（先运行 tbrain install）")

    # 2) native plugin copy (stable location, survives version switches)
    native, native_files = _install_native(home)
    files.extend(native_files)
    if native is None:
        warnings.append("安装包缺少 integrations/tbrain-dsh.mjs，DSH 原生插件无法复制；"
                        "请重新安装 Tbrain 或运行 doctor 查看手动步骤。")
    plugin_uri = native.resolve().as_uri() if native is not None else (
        paths.integrations_dir(home) / NATIVE_NAME).resolve().as_uri()

    # 3) profile patch file
    patch_path = locations["patch"]
    text = config.read_text(patch_path)
    backup = config.backup_file(patch_path)
    if backup:
        backups.append(str(backup))
    text, _ = _strip_tbrain_items(text)
    text = config.append_yaml_block(text, _patch_block(home, entry, plugin_uri))
    config.write_text_atomic(patch_path, text)
    files.append(str(patch_path))

    config.write_connect_state(home, HOST, {
        "entry": entry,
        "install_root": str(root),
        "profile": locations["profile"],
        "patch": str(patch_path),
        "files": files,
        "native": str(native) if native is not None else "",
    })
    return {"ok": True, "host": HOST, "entry": entry, "install_root": str(root),
            "profile": locations["profile"], "files": files, "backups": backups,
            "warnings": warnings,
            "detail": "DeepSeek Harness 已接入（MCP + 原生插件）；"
                      "MCP 配置可热重连，原生插件需重开 DSH。"}


def _strip_tbrain_items(text: str) -> tuple[str, int]:
    """只移除 Tbrain 条目；宿主可能把用户插件放进原有标记块。"""
    output = []
    skip_indent = None
    removed = 0
    for line in text.splitlines(keepends=True):
        if line.strip() in (config.YAML_MARK_START, config.YAML_MARK_END):
            continue
        indent = len(line) - len(line.lstrip())
        if skip_indent is not None:
            if not line.strip() or indent > skip_indent:
                continue
            skip_indent = None
        if re.match(r"\s*- id: ['\"]?tbrain-", line):
            skip_indent = indent
            removed += 1
            continue
        output.append(line)
    cleaned = []
    for i, line in enumerate(output):
        if line.strip() == "- insert:":
            following = next((value for value in output[i + 1:] if value.strip()), "")
            if not following or len(following) - len(following.lstrip()) <= len(line) - len(line.lstrip()):
                continue
        cleaned.append(line)
    return "".join(cleaned), removed


def disconnect(host: str = HOST, app_home: Path | None = None,
               install_root: Path | None = None, settings=None, profile: str | None = None) -> dict:
    home, root, entry = _context(app_home, install_root, settings)
    locations = config.dsh_paths(profile)
    files: list[str] = []
    backups: list[str] = []
    removed = 0

    patch_path = locations["patch"]
    text = config.read_text(patch_path)
    if text:
        backup = config.backup_file(patch_path)
        if backup:
            backups.append(str(backup))
        stripped, removed = _strip_tbrain_items(text)
        config.write_text_atomic(patch_path, stripped)
        files.append(str(patch_path))

    native_dir = paths.integrations_dir(home)
    for name in (NATIVE_NAME, "package.json"):
        target = native_dir / name
        try:
            target.unlink()
            files.append(str(target))
        except OSError:
            pass
    try:
        if native_dir.is_dir() and not any(native_dir.iterdir()):
            native_dir.rmdir()
    except OSError:
        pass

    config.clear_connect_state(home, HOST)
    return {"ok": True, "host": HOST, "entry": entry, "profile": locations["profile"],
            "files": files, "backups": backups, "removed": removed,
            "detail": "已移除 DSH profile 中的 Tbrain 条目与原生插件；重开 DSH 后生效。"}


def doctor(host: str = HOST, app_home: Path | None = None,
           install_root: Path | None = None, settings=None, profile: str | None = None) -> dict:
    home, root, entry = _context(app_home, install_root, settings)
    locations = config.dsh_paths(profile)
    item = {"id": HOST, "host": HOST, 'mcp': False, 'hooks': False, 'enabled': False,
            'paths': [str(locations['patch']), str(paths.integrations_dir(home) / NATIVE_NAME)],
            'reload': '完整退出并重开 DSH；运行中的会话不会因写入配置立即切换。'}
    patch_path = locations["patch"]
    text = config.read_text(patch_path)
    if not text:
        item.update(status="yellow",
                    detail=f"未接入 DSH（缺少 profile patch：{patch_path}）；运行 tbrain connect dsh。")
        return item
    ids = config.find_yaml_ids(text)
    tbrain_ids = [name for name in ids if name.startswith("tbrain-")]
    item.update(mcp='tbrain-mcp' in ids, hooks='tbrain-native' in ids, enabled=bool(tbrain_ids))
    if not tbrain_ids:
        item.update(status="yellow",
                    detail="未接入 DSH（profile patch 中没有 Tbrain 条目）；运行 tbrain connect dsh。")
        return item
    missing = [name for name in REQUIRED_IDS if name not in ids]
    if re.search(r"shared[-_]brain", text, re.IGNORECASE):
        item.update(status="red",
                    detail="检测到旧 Shared Brain 条目残留：请在 DSH profile 中运行 tbrain disconnect dsh，"
                           "或手动删除 shared-brain-mcp / shared-brain-lifecycle 后重开 DSH。")
        return item
    if missing:
        item.update(status="yellow",
                    detail="DSH 配置不完整（缺少 " + "、".join(missing) + "）；重新运行 tbrain connect dsh。")
        return item
    if not Path(entry).is_file():
        item.update(status="red", detail=f"固定入口缺失：{entry}（重新安装 Tbrain）")
        return item
    native = paths.integrations_dir(home) / NATIVE_NAME
    if not native.is_file():
        item.update(status="red",
                    detail="DSH 原生插件未安装：运行 tbrain connect dsh 复制，或手动把安装目录中的 "
                           "integrations/tbrain-dsh.mjs 复制到 " + str(native) + "。")
        return item
    managed = text.partition(config.YAML_MARK_START)[2].partition(config.YAML_MARK_END)[0]
    entries = {entry.get("id"): entry for block in (yaml.safe_load(managed) or [])
               for entry in block.get("insert", [])}
    mcp = entries.get("tbrain-mcp", {}).get("config", {})
    native_config = entries.get("tbrain-native", {}).get("config", {})
    if (mcp.get("command") != entry or mcp.get("args") != base.mcp_args(home, HOST)
            or native_config.get("command") != entry or native_config.get("home") != str(home)
            or "tbrain-cc-hooks" in ids):
        item.update(status="yellow", detail="DSH 启动参数或原生接入配置过期；重新运行 tbrain connect dsh。")
        return item
    item.update(status="green",
                detail=f"DSH 配置完整（MCP + 原生插件，profile={locations['profile']}）。")
    return item
