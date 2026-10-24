"""DeepSeek Harness (DSH) adapter.

DSH's MCP servers and plugins live in the profile patch file
``~/.dsh/profiles/<profile>/cordis.patch.yml``.  Tbrain inserts three entries:

* ``tbrain-mcp``       -- ``@deepseek-ai/dsh-mcp-client`` for the stdio MCP server;
* ``tbrain-cc-hooks``  -- ``@deepseek-ai/dsh-hooks-claude-code`` pointing at the
  shared ``<app_home>/hooks.json`` (same events as Claude/Codex);
* ``tbrain-native``    -- a small cordis plugin that adds what the bridge cannot:
  PreCompact reminders, SessionEnd, context-usage based switch hints.

The patch file is edited as a marked YAML block so every user entry, comment and
``!!js`` expression stays byte-for-byte untouched.  Installing the bridge/native
dependencies never touches the network: ``connect`` writes the config and
``doctor`` explains the manual ``dsh plugin add`` step when the bridge package is
missing.
"""

from __future__ import annotations

import re
from pathlib import Path

from .. import __version__, paths
from . import base, config

HOST = "dsh"
EXPECTED_BRIDGE_VERSION = "0.2.1-rc.3"
BRIDGE_PACKAGE = "@deepseek-ai/dsh-hooks-claude-code"
MCP_CLIENT_PACKAGE = "@deepseek-ai/dsh-mcp-client"
REQUIRED_IDS = ("tbrain-mcp", "tbrain-cc-hooks", "tbrain-native")
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
        "        args: []",
        "    - id: tbrain-cc-hooks",
        f"      name: {config.yaml_scalar(BRIDGE_PACKAGE)}",
        "      config:",
        f"        configPath: {config.yaml_scalar(str(paths.hooks_json(app_home)))}",
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
    config.write_text_atomic(target, source.read_text(encoding="utf-8"))
    files.append(str(target))
    package = target_dir / "package.json"
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


def bridge_status(locations: dict) -> dict:
    """Is the DSH Claude-hooks bridge package installed in the profile?"""
    package_json = config.read_json(locations["package"])
    declared = {}
    if isinstance(package_json, dict):
        declared = {**(package_json.get("dependencies") or {}), **(package_json.get("devDependencies") or {})}
    installed_file = locations["node_modules"] / BRIDGE_PACKAGE / "package.json"
    version = ""
    if installed_file.is_file():
        payload = config.read_json(installed_file)
        version = str(payload.get("version") or "") if isinstance(payload, dict) else ""
    installed = bool(version) or BRIDGE_PACKAGE in declared
    expected = EXPECTED_BRIDGE_VERSION
    version_match = (not version) or version == expected
    return {"installed": installed, "version": version,
            "declared": BRIDGE_PACKAGE in declared,
            "expected": expected, "version_match": version_match}


def connect(host: str = HOST, app_home: Path | None = None,
            install_root: Path | None = None, settings=None, profile: str | None = None) -> dict:
    home, root, entry = _context(app_home, install_root, settings)
    locations = config.dsh_paths(profile)
    files: list[str] = []
    backups: list[str] = []
    warnings: list[str] = []

    if not Path(entry).is_file():
        warnings.append(f"固定入口尚未安装：{entry}（先运行 tbrain install）")

    # 1) shared hooks file for the Claude-hooks bridge
    files.append(str(base.write_bridge_hooks(home, entry)))

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
    text = config.strip_yaml_block(text)
    text = config.append_yaml_block(text, _patch_block(home, entry, plugin_uri))
    config.write_text_atomic(patch_path, text)
    files.append(str(patch_path))

    bridge = bridge_status(locations)
    if not bridge["installed"]:
        warnings.append("未检测到 DSH 桥 " + BRIDGE_PACKAGE + "：请在 DSH profile 中执行 "
                        "dsh plugin add " + BRIDGE_PACKAGE + "@" + EXPECTED_BRIDGE_VERSION +
                        "（Tbrain 不联网安装依赖），然后重开 DSH。")
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
            "warnings": warnings, "bridge": bridge,
            "detail": "DeepSeek Harness 已接入（MCP + Claude hooks 桥 + 原生插件）；"
                      "MCP 配置可热重连，桥/插件需重开 DSH。"}


def _force_strip_tbrain_items(text: str) -> tuple[str, int]:
    """Fallback for patches written without our markers (PyYAML, best effort)."""
    try:
        import yaml

        data = yaml.safe_load(text)
    except Exception:
        return text, 0
    if not isinstance(data, list):
        return text, 0
    removed = 0
    output: list = []
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("insert"), list):
            output.append(item)
            continue
        kept = []
        for entry in item["insert"]:
            if isinstance(entry, dict) and str(entry.get("id") or "").startswith("tbrain-"):
                removed += 1
                continue
            kept.append(entry)
        if kept:
            output.append({**item, "insert": kept})
    if not removed:
        return text, 0
    try:
        return yaml.safe_dump(output, sort_keys=False, allow_unicode=True), removed
    except Exception:
        return text, 0


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
        stripped = config.strip_yaml_block(text)
        removed += max(0, len(config.find_yaml_ids(text)) - len(config.find_yaml_ids(stripped)))
        if stripped.strip() == text.strip() and any(
                item.startswith("tbrain-") for item in config.find_yaml_ids(text)):
            stripped, forced = _force_strip_tbrain_items(stripped)
            removed += forced
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
    item = {"id": HOST, "host": HOST}
    patch_path = locations["patch"]
    text = config.read_text(patch_path)
    if not text:
        item.update(status="yellow",
                    detail=f"未接入 DSH（缺少 profile patch：{patch_path}）；运行 tbrain connect dsh。")
        return item
    ids = config.find_yaml_ids(text)
    tbrain_ids = [name for name in ids if name.startswith("tbrain-")]
    if not tbrain_ids:
        item.update(status="yellow",
                    detail=f"未接入 DSH（profile patch 中没有 Tbrain 条目）；运行 tbrain connect dsh。")
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
    hooks_file = paths.hooks_json(home)
    if not hooks_file.is_file():
        item.update(status="red",
                    detail=f"共用 hooks 文件缺失：{hooks_file}；重新运行 tbrain connect dsh。")
        return item
    native = paths.integrations_dir(home) / NATIVE_NAME
    if not native.is_file():
        item.update(status="red",
                    detail="DSH 原生插件未安装：运行 tbrain connect dsh 复制，或手动把安装目录中的 "
                           "integrations/tbrain-dsh.mjs 复制到 " + str(native) + "。")
        return item
    bridge = bridge_status(locations)
    if not bridge["installed"]:
        item.update(status="yellow",
                    detail="DSH 桥未安装（" + BRIDGE_PACKAGE + "）：Tbrain 不联网安装依赖，请在 DSH profile 执行 "
                           "dsh plugin add " + BRIDGE_PACKAGE + "@" + EXPECTED_BRIDGE_VERSION +
                           "，或手动安装到 profile 的 node_modules 后重开 DSH。")
        return item
    if bridge["version"] and not bridge["version_match"]:
        item.update(status="yellow",
                    detail=f"DSH 桥版本 {bridge['version']} 与当前引擎期望的 {EXPECTED_BRIDGE_VERSION} 不一致；"
                           "DSH 升级后请同步升级桥。")
        return item
    item.update(status="green",
                detail=f"已接入 DSH（MCP + hooks 桥 + 原生插件，profile={locations['profile']}）。")
    return item
