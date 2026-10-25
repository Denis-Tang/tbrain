"""``tbrain`` command line entry point.

The CLI is deliberately thin: host configuration lives in :mod:`tbrain.hosts`,
installation/upgrade in :mod:`tbrain.installer` / :mod:`tbrain.update`, and the
memory workflow in :class:`tbrain.service.TbrainService`.  ``hook`` and ``tool``
print one compact JSON object so host processes can consume stdout directly.

Exit codes: 0 success, 1 a handled error (JSON on stdout, except ``mcp`` which
writes diagnostics to stderr because stdout belongs to the MCP transport).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, installer, paths, update


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tbrain", description="Tbrain 本地共享记忆")
    parser.add_argument("--version", action="version", version=json.dumps({"version": __version__}))
    parser.add_argument("--home", type=Path, help="应用数据目录（默认 ~/.tbrain）")
    commands = parser.add_subparsers(dest="command")

    def sub(name: str, help_text: str):
        item = commands.add_parser(name, help=help_text)
        item.add_argument("--home", type=Path, default=argparse.SUPPRESS, help="应用数据目录")
        return item

    sub("desktop", "打开桌面界面（默认）")
    sub("mcp", "启动 stdio MCP 服务")
    hook = sub("hook", "读取宿主 hook 的 stdin JSON，输出宿主可消费的 JSON")
    hook.add_argument("--host", required=True, help="claude-code|codex|dsh|pi")
    tool = sub("tool", "以命令行调用 MCP 工具（供 pi 扩展等使用）")
    tool.add_argument("name", help="bootstrap|search|save|resume")
    tool.add_argument("payload", nargs="?", help="JSON 参数（省略或 - 时从 stdin 读取）")

    for name in ("connect", "disconnect"):
        item = sub(name, "接入/移除一个宿主的 Tbrain 配置")
        item.add_argument("host", help="claude-code|codex|dsh|pi")
        item.add_argument("--profile", default=None, help="DSH profile 名（默认 desktop）")
        item.add_argument("--root", type=Path, default=None, help="安装目录（默认自动解析）")

    doctor = sub("doctor", "检查安装、进程与四宿主接入状态")
    doctor.add_argument("--host", default=None, help="只检查一个宿主")
    doctor.add_argument("--profile", default=None, help="DSH profile 名")
    doctor.add_argument("--root", type=Path, default=None, help="安装目录")

    install = sub("install", "从构建产物/发布包安装一个版本")
    install.add_argument("--from", dest="source", default="",
                         help="安装包目录或 zip 路径（在解压后的发布包里运行时省略）")
    install.add_argument("--version", default="", help="版本号（默认从安装包读取）")
    install.add_argument("--root", type=Path, default=None, help="安装目录")

    uninstall = sub("uninstall", "移除程序文件（保留应用数据目录）")
    uninstall.add_argument("--root", type=Path, default=None, help="安装目录")

    rollback = sub("rollback", "切换到上一个已安装版本")
    rollback.add_argument("--root", type=Path, default=None, help="安装目录")

    upgrade = sub("upgrade", "检查/下载/安装新版本")
    upgrade.add_argument("--from", dest="source", default="", help="本地包目录、zip 或 URL")
    upgrade.add_argument("--version", default="", help="升级包版本（默认从包读取）")
    upgrade.add_argument("--root", type=Path, default=None, help="安装目录")
    upgrade.add_argument("--check", action="store_true", help="只检查更新，不升级")

    sub("time", "显示当前时间（校时状态、时区）")
    sessions = sub("sessions", "列出会话总结")
    sessions.add_argument("--filter", default="all", help="状态筛选（默认 all）")
    project = sub("project", "查看/维护项目配置")
    project.add_argument("action", nargs="?", default="list", choices=["list", "add", "remove"])
    project.add_argument("--name", default="", help="项目名")
    project.add_argument("--path", dest="paths_", action="append", default=[], help="工作目录（可重复）")
    project.add_argument("--parent", default="", help="父项目名")
    project.add_argument("--allow-child", action="store_true", help="允许读取子项目内容")
    project.add_argument("--rename-from", default="", help="重命名前的旧项目名")
    prompt = sub("prompt", "查看/保存全局或项目提示词")
    prompt.add_argument("scope", nargs="?", default="global", choices=["global", "project"])
    prompt.add_argument("--name", default="", help="项目名（scope=project 时）")
    prompt.add_argument("--save", default=None, help="把文件内容保存为提示词（项目用 JSON sections）")
    migrate = sub("migrate", "旧版数据迁移（默认 dry-run；--execute 正式写入新 vault）")
    migrate.add_argument("--old-home", required=True, help="旧版应用数据目录（如 ~/.shared-brain）")
    migrate.add_argument("--dry-run", action="store_true", default=False, help="只报告，不写入")
    migrate.add_argument("--execute", action="store_true", help="正式执行迁移（写入新 vault，旧数据只读）")
    migrate.add_argument("--to", default="", help="目标 vault（默认 settings.vault_path）")
    sub("version", "显示版本与安装状态")
    return parser


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _app_home(args) -> Path | None:
    return getattr(args, "home", None)


def _context(args, store_needed: bool = False):
    """(home, settings, install_root[, settings_store]) resolved from flags/env/settings."""
    from .settings import SettingsStore

    requested = _app_home(args)
    store = SettingsStore(requested)
    settings = store.load()
    home = requested if requested is not None else paths.app_home(None)
    root = getattr(args, "root", None) or paths.install_root(home, settings)
    if store_needed:
        return home, settings, Path(root), store
    return home, settings, Path(root)


def _service(home: Path | None):
    from .service import TbrainService

    return TbrainService(home)


def _print(payload, indent: int = 2) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=indent))


def _doctor_callable(home, root, settings, profile=None):
    from . import hosts

    return lambda: hosts.doctor_all(home, root, settings, profile=profile)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def _cmd_hook(args) -> dict:
    raw: dict = {}
    if not sys.stdin.isatty():
        text = sys.stdin.read()
        if text.strip():
            try:
                payload = json.loads(text)
                raw = payload if isinstance(payload, dict) else {}
            except ValueError:
                raw = {}
    result = _service(_app_home(args)).hook(args.host, raw)
    return result if isinstance(result, dict) else {}


def _cmd_tool(args) -> dict:
    text = sys.stdin.read() if args.payload in (None, "", "-") else str(args.payload)
    try:
        payload = json.loads(text) if str(text).strip() else {}
    except ValueError:
        raise RuntimeError("tool 参数不是有效 JSON") from None
    if not isinstance(payload, dict):
        raise RuntimeError("tool 参数须为 JSON 对象")
    result = _service(_app_home(args)).tool(args.name, payload)
    return result if isinstance(result, dict) else {"result": result}


def _cmd_project(args) -> dict:
    service = _service(_app_home(args))
    if args.action == "list":
        return {"projects": service.projects()}
    if not args.name:
        raise RuntimeError("请用 --name 指定项目名")
    if args.action == "remove":
        return service.delete_project(args.name)
    return service.configure_project(args.name, list(args.paths_), parent=args.parent,
                                     allow_child=bool(args.allow_child),
                                     rename_from=args.rename_from)


def _cmd_prompt(args) -> dict:
    service = _service(_app_home(args))
    if args.scope == "global":
        if args.save:
            text = Path(args.save).read_text(encoding="utf-8")
            return service.save_global_prompt(text)
        return service.global_prompt()
    if not args.name:
        raise RuntimeError("scope=project 时需要 --name 指定项目名")
    if args.save:
        text = Path(args.save).read_text(encoding="utf-8")
        try:
            sections = json.loads(text)
        except ValueError:
            sections = {"最终目标": text}
        return service.save_project_prompt(args.name, sections)
    return service.project_prompt(args.name)


def _cmd_time(args) -> dict:
    return _service(_app_home(args)).time_now()


def _cmd_sessions(args) -> dict:
    rows = _service(_app_home(args)).sessions(args.filter)
    return {"sessions": rows}


def _cmd_migrate(args) -> dict:
    service = _service(_app_home(args))
    target = Path(args.to).expanduser() if getattr(args, "to", "") else None
    if getattr(args, "execute", False) and not getattr(args, "dry_run", False):
        return service.migrate(args.old_home, execute=True, new_vault=target)
    return service.migrate(args.old_home, execute=False, new_vault=target)


def _cmd_install(args) -> dict:
    import sys

    home, settings, root, store = _context(args, store_needed=True)
    if args.source:
        source = Path(args.source).expanduser()
    elif getattr(sys, "frozen", False):
        source = Path(sys.executable).resolve().parent
        if source.parent.name == "versions":
            raise RuntimeError("Tbrain 已安装；升级请用 tbrain upgrade --from <新版本包>，回退用 tbrain rollback")
    else:
        raise RuntimeError("源码运行请用 --from 指定安装包目录或 zip")
    if not source.exists():
        raise RuntimeError(f"找不到安装包：{source}")
    version = str(args.version or "").strip() or update.infer_package_version(source)
    if not version:
        raise RuntimeError("无法从安装包确定版本号，请用 --version 指定")
    result = installer.install_from_package(source, version, root=root, make_current=True)
    if not str(settings.install_root or "").strip():
        try:
            store.save({"install_root": str(root)})
            result["install_root_saved"] = True
        except Exception:
            result["install_root_saved"] = False
    result["message"] = (f"已安装 v{version}；宿主请运行 tbrain connect <host>，"
                         f"固定入口：{installer.entry_command(root)}")
    return result


def _cmd_uninstall(args) -> dict:
    home, settings, root = _context(args)
    return installer.uninstall(root)


def _cmd_rollback(args) -> dict:
    home, settings, root = _context(args)
    return installer.rollback(root)


def _cmd_connect(args) -> dict:
    from . import hosts

    home, settings, root = _context(args)
    return hosts.connect(args.host, home, root, settings, profile=args.profile)


def _cmd_disconnect(args) -> dict:
    from . import hosts

    home, settings, root = _context(args)
    return hosts.disconnect(args.host, home, root, settings, profile=args.profile)


def _cmd_doctor(args) -> dict:
    from . import hosts

    home, settings, root = _context(args)
    if args.host:
        item = hosts.doctor(args.host, home, root, settings, profile=args.profile)
        return {"ok": item.get("status") != "red", "items": [item], "version": __version__}
    report = hosts.doctor_all(home, root, settings, profile=args.profile)
    report["version"] = __version__
    return report


def _cmd_upgrade(args) -> dict:
    home, settings, root = _context(args)
    doctor = _doctor_callable(home, root, settings)
    if args.check:
        result = update.check_latest(home, force=True)
    elif args.source:
        result = update.upgrade_from_package(args.source, args.version, home=home,
                                             install_root=root, doctor=doctor)
    else:
        result = update.upgrade(home=home, install_root=root, force=True, doctor=doctor)
    if isinstance(result, dict):
        result.setdefault("current_version", __version__)
    return result


def _cmd_version(args) -> dict:
    from .settings import SettingsStore

    home = _app_home(args) or paths.app_home(None)
    settings = SettingsStore(home).load()
    root = paths.install_root(home, settings)
    return {"version": __version__, "app_home": str(home), "install_root": str(root),
            "installed_version": installer.current_version(root),
            "installed": bool(installer.installed_versions(root))}


def run_command(command: str, args) -> dict:
    handlers = {
        "hook": _cmd_hook,
        "tool": _cmd_tool,
        "install": _cmd_install,
        "uninstall": _cmd_uninstall,
        "rollback": _cmd_rollback,
        "connect": _cmd_connect,
        "disconnect": _cmd_disconnect,
        "doctor": _cmd_doctor,
        "upgrade": _cmd_upgrade,
        "time": _cmd_time,
        "sessions": _cmd_sessions,
        "project": _cmd_project,
        "prompt": _cmd_prompt,
        "migrate": _cmd_migrate,
        "version": _cmd_version,
    }
    return handlers[command](args)


def main(argv=None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    command = args.command or "desktop"
    try:
        if command == "mcp":
            from .mcp_server import run

            run(home=_app_home(args))
            return 0
        if command == "desktop":
            from .desktop import run

            run(home=_app_home(args))
            return 0
        result = run_command(command, args)
        compact = command in ("hook", "tool")
        _print(result, indent=None if compact else 2)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        output = sys.stderr if command == "mcp" else sys.stdout
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=output)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
