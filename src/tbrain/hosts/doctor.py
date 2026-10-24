"""``tbrain doctor``: check the installation, the process registry and all four hosts.

Item shape::

    {"id": "install|processes|claude-code|codex|dsh|pi",
     "host": <same as id>,          # compatibility with docs/ARCHITECTURE.md §13
     "status": "green|yellow|red",
     "detail": "..."}

Green means "nothing to do", yellow means "a user action is pending" (connect a
host, trust Codex hooks, install a DSH bridge) and red means "broken" (missing
fixed entry, version mismatch, old Shared Brain entries left behind, missing DSH
dependency).
"""

from __future__ import annotations

from pathlib import Path

from .. import __version__, installer, paths, proc
from . import base, claude, codex, config, dsh, pi

HOST_MODULES = {"claude-code": claude, "codex": codex, "dsh": dsh, "pi": pi}


def _item(key: str, status: str, detail: str) -> dict:
    return {"id": key, "host": key, "status": status, "detail": detail}


def install_item(app_home: Path | None = None, install_root: Path | None = None,
                 settings=None) -> dict:
    """Is the fixed-entry versioned installation complete and selected?"""
    home = base.default_app_home(app_home)
    root = base.resolve_install_root(home, install_root, settings)
    entry = paths.bin_entry(root)
    versions = installer.installed_versions(root)
    current = installer.current_version(root)
    if not versions and not current:
        return _item("install", "yellow",
                     f"未安装（源码运行或首次使用）；运行 tbrain install。固定入口将位于 {entry}")
    if not current:
        return _item("install", "red", f"current.txt 缺失或为空：{paths.current_file(root)}；重新安装或运行 tbrain install。")
    if not entry.is_file():
        return _item("install", "red", f"固定入口缺失：{entry}；重新安装或运行 tbrain install。")
    verify = installer.verify_layout(current, root)
    if not verify["ok"]:
        return _item("install", "red",
                     f"版本 {current} 安装不完整（缺少 " + "、".join(verify["missing"]) + "）；重新安装。")
    return _item("install", "green",
                 f"已安装 v{current}，固定入口 {entry}，共 {len(versions)} 个版本。")


def processes_item(app_home: Path | None = None, install_root: Path | None = None,
                   settings=None) -> dict:
    """Are any processes from an older version still running?"""
    home = base.default_app_home(app_home)
    root = base.resolve_install_root(home, install_root, settings)
    current = installer.current_version(root) or __version__
    try:
        live = proc.live_processes(home)
    except Exception as exc:  # registry problems must not break doctor
        return _item("processes", "yellow", f"无法读取进程登记：{exc}")
    older = [item for item in live if str(item.get("version") or "") != str(current)]
    if older:
        pids = "、".join(f"PID {item.get('pid')}(v{item.get('version')})" for item in older)
        return _item("processes", "yellow",
                     f"仍有旧版本进程在运行：{pids}；新版本启动时会通知其退出，超时强制结束。")
    if not live:
        return _item("processes", "green", "没有运行中的 Tbrain 进程。")
    return _item("processes", "green", f"{len(live)} 个运行中的进程，版本均为 {current}。")


def doctor(host: str, app_home: Path | None = None, install_root: Path | None = None,
           settings=None, profile: str | None = None) -> dict:
    """Doctor result for one host."""
    key = config.canonical_host(host)
    home = base.default_app_home(app_home)
    root = base.resolve_install_root(home, install_root, settings)
    module = HOST_MODULES[key]
    try:
        if key == "dsh":
            return module.doctor(key, home, root, settings, profile=profile)
        return module.doctor(key, home, root, settings)
    except config.HostError as exc:
        return _item(key, "red", str(exc))
    except Exception as exc:  # a broken config file must not hide other items
        return _item(key, "yellow", f"检查 {key} 配置时出错：{exc}")


def doctor_all(app_home: Path | None = None, install_root: Path | None = None,
               settings=None, profile: str | None = None) -> dict:
    """Full doctor report used by the desktop app and ``tbrain doctor``."""
    home = base.default_app_home(app_home)
    root = base.resolve_install_root(home, install_root, settings)
    items = [install_item(home, root, settings), processes_item(home, root, settings)]
    for key in config.SUPPORTED_HOSTS:
        items.append(doctor(key, home, root, settings, profile=profile))
    ok = all(item.get("status") != "red" for item in items)
    return {"ok": ok, "items": items}
