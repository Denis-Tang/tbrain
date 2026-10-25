"""pi coding-agent adapter.

pi has no built-in MCP server; its integration point is a TypeScript extension
loaded from ``~/.pi/agent/extensions/*.ts``.  ``connect`` copies
``integrations/pi/tbrain.ts`` there.  The extension itself calls the fixed entry
(``tbrain hook --host pi`` / ``tbrain tool <name> <json>``), so upgrades only need
to replace the program files and a new pi session (or ``/reload``) picks it up.
"""

from __future__ import annotations

from pathlib import Path

from . import base, config

HOST = "pi"
EXTENSION_NAME = "tbrain.ts"
MARKER = "Tbrain pi extension"


def _target(app_home: Path | None, install_root: Path | None, settings):
    home = base.default_app_home(app_home)
    _, entry = base.entry_for(home, install_root, settings)
    return home, base.resolve_install_root(home, install_root, settings), entry, config.pi_paths()


def _source_file() -> Path:
    return config.integrations_source() / "pi" / EXTENSION_NAME


def connect(host: str = HOST, app_home: Path | None = None,
            install_root: Path | None = None, settings=None) -> dict:
    home, root, entry, locations = _target(app_home, install_root, settings)
    files: list[str] = []
    backups: list[str] = []
    warnings: list[str] = []
    source = _source_file()
    target = locations["extension"]
    if not source.is_file():
        raise config.HostError("安装包缺少 integrations/pi/tbrain.ts，请重新安装 Tbrain。")
    backup = config.backup_file(target)
    if backup:
        backups.append(str(backup))
    target.parent.mkdir(parents=True, exist_ok=True)
    config.write_text_atomic(target, source.read_text(encoding="utf-8"))
    files.append(str(target))
    if not Path(entry).is_file():
        warnings.append(f"固定入口尚未安装：{entry}（先运行 tbrain install）")
    config.write_connect_state(home, HOST, {"entry": entry, "install_root": str(root),
                                            "extension": str(target), "files": files})
    return {"ok": True, "host": HOST, "entry": entry, "install_root": str(root),
            "files": files, "backups": backups, "warnings": warnings,
            "detail": "pi 扩展已安装；新开 pi 会话或执行 /reload 后生效（无需 MCP 配置）。"}


def disconnect(host: str = HOST, app_home: Path | None = None,
               install_root: Path | None = None, settings=None) -> dict:
    home, root, entry, locations = _target(app_home, install_root, settings)
    files: list[str] = []
    removed = 0
    target = locations["extension"]
    if target.is_file():
        config.backup_file(target)
        try:
            target.unlink()
            files.append(str(target))
            removed += 1
        except OSError:
            pass
    config.clear_connect_state(home, HOST)
    return {"ok": True, "host": HOST, "entry": entry, "files": files,
            "backups": [], "removed": removed,
            "detail": "已移除 pi 中的 Tbrain 扩展；其他扩展与设置保留。"}


def doctor(host: str = HOST, app_home: Path | None = None,
           install_root: Path | None = None, settings=None) -> dict:
    home, root, entry, locations = _target(app_home, install_root, settings)
    target = locations["extension"]
    installed = target.is_file() and MARKER in config.read_text(target)
    item = {"id": HOST, "host": HOST, 'mcp': False, 'hooks': installed, 'tools': installed, 'enabled': installed,
            'paths': [str(target)], 'reload': '新开 pi 会话或执行 /reload。工具与生命周期事件由同一个扩展提供。'}
    if not installed:
        item.update(status="yellow",
                    detail="pi 扩展未安装；运行 tbrain connect pi（新开 pi 会话或 /reload 生效）。")
        return item
    if not Path(entry).is_file():
        item.update(status="red", detail=f"固定入口缺失：{entry}（重新安装 Tbrain）")
        return item
    item.update(status="green",
                detail=f"pi 扩展已安装（{target}）；上下文用量与换会话提醒由扩展提供。")
    return item
