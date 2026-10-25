"""Tbrain 桌面后端：把服务能力通过 WebView2 桥暴露给本地页面。

协议沿用 0.5.2：Python 侧启动 ``Tbrain.Desktop.exe``（WPF + WebView2），子进程把
页面请求以 JSON 行写入 stdout，本进程在 stdin 回 {id, result}。页面通过
``window.desktop.<method>(...args)`` 调用本文件 DesktopAPI 的同名方法。
"""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

from .service import TbrainService
from . import __version__, installer, paths


class DesktopAPI:
    def __init__(self, service: TbrainService):
        self._service = service
        self._quit_requested = False

    def _call(self, method, *args):
        try:
            result = method(*args)
            return result
        except Exception as exc:  # 页面需要拿到错误而不是白屏
            return {"ok": False, "error": str(exc)}

    # 基础 -----------------------------------------------------------------
    def status(self):
        return self._call(self._service.status)

    def quit(self):
        self._quit_requested = True
        return {"ok": True}

    def setup_info(self):
        return self._call(self._setup_info)

    def _setup_info(self):
        settings = self._service.settings.load()
        root = paths.install_root(self._service.home, settings)
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
        available = {host: bool(shutil.which(host)) for host in ("claude", "codex", "pi")}
        available["claude-code"] = available.pop("claude") or any(
            (Path.home() / ".vscode/extensions").glob("anthropic.claude-code-*"))
        available["dsh"] = (local / "Programs/DeepSeek Harness/DeepSeek Harness.exe").is_file()
        if os.name == "nt" and not available["codex"]:
            probe = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                                    "@(Get-AppxPackage -Name OpenAI.Codex).Count"],
                                   stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
            available["codex"] = probe.returncode == 0 and probe.stdout.strip() not in ("", "0")
        return {"required": not bool(settings.vault_path),
                "vault_path": settings.vault_path or str(Path.home() / "Documents/TbrainVault"),
                "install_root": str(root), "installed": installer.verify_layout(root=root)["ok"],
                "hosts": available}

    def setup(self, vault_path, hosts):
        return self._call(self._setup, vault_path, hosts)

    def _setup(self, vault_path, selected):
        from .hosts import config

        if not isinstance(selected, list) or any(host not in config.SUPPORTED_HOSTS for host in selected):
            raise ValueError("请选择支持的 Agent")
        if not isinstance(vault_path, str) or not vault_path.strip():
            raise ValueError("请选择知识库文件夹")
        vault = Path(vault_path.strip()).expanduser()
        if not vault.is_absolute() or vault.is_file():
            raise ValueError("知识库必须是文件夹的完整路径")
        info = self._setup_info()
        missing = [host for host in selected if not info["hosts"][host]]
        if missing:
            raise ValueError("未检测到 Agent，请安装后刷新：" + "、".join(missing))
        root = Path(info["install_root"])
        if not installer.verify_layout(__version__, root)["ok"]:
            if not getattr(sys, "frozen", False):
                raise ValueError("请使用构建后的安装包完成安装")
            installer.install_from_dir(Path(sys.executable).parent, __version__, root)
        vault.mkdir(parents=True, exist_ok=True)
        config.backup_file(self._service.settings.path)
        self._service.save_settings({"vault_path": str(vault), "install_root": str(root)})
        if os.name == "nt":
            shortcut = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Tbrain.lnk"
            shortcut.parent.mkdir(parents=True, exist_ok=True)
            config.backup_file(shortcut)
            link = str(shortcut).replace("'", "''")
            target = str(root / "current/tbrain.exe").replace("'", "''")
            app_home = str(self._service.home).replace("'", "''")
            command = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{link}');"
                       f"$s.TargetPath='{target}';$s.Arguments='--home \"{app_home}\" desktop';"
                       "$s.Save()")
            subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15, check=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        results = []
        for host in dict.fromkeys(selected):
            result = self.connect(host)
            results.append({"host": host, **result})
        return {"ok": True, "hosts": results}

    # 会话 -----------------------------------------------------------------
    def sessions(self, status_filter="all"):
        return self._call(self._service.sessions, status_filter)

    def session_detail(self, path_or_id):
        return self._call(self._service.session_detail, path_or_id)

    def rename_session(self, path_or_id, new_name):
        return self._call(self._service.rename_session, path_or_id, new_name)

    # 全局提示词 -----------------------------------------------------------
    def global_prompt(self):
        return self._call(self._service.global_prompt)

    def save_global_prompt(self, text):
        return self._call(self._service.save_global_prompt, text)

    # 项目 -----------------------------------------------------------------
    def projects(self):
        return self._call(self._service.projects)

    def project_save(self, name, paths, parent="", allow_child=False, rename_from=""):
        return self._call(self._service.configure_project, name, paths, parent, allow_child, rename_from)

    def project_delete(self, name):
        return self._call(self._service.delete_project, name)

    def project_prompt(self, name):
        return self._call(self._service.project_prompt, name)

    def project_prompt_save(self, name, sections):
        return self._call(self._service.save_project_prompt, name, sections)

    # 设置 / 时间 ----------------------------------------------------------
    def settings(self):
        status = self._call(self._service.status)
        return {"settings": status.get("settings") or {}, "timezones": status.get("timezones") or [],
                "version": status.get("version"), "current_time": status.get("current_time")}

    def save_settings(self, values):
        return self._call(self._service.save_settings, values)

    def time(self):
        return self._call(self._service.time_now)

    # 宿主 / 升级 ----------------------------------------------------------
    def doctor(self):
        return self._call(self._service.doctor)

    def connect(self, host):
        return self._call(self._service.connect, host)

    def disconnect(self, host):
        return self._call(self._service.disconnect, host)

    def update_check(self, force=True):
        return self._call(self._service.check_update, force)

    def update_apply(self):
        return self._call(self._service.apply_upgrade)

    def open_document(self, path):
        return self._call(self._service.open_document, path)


def _maintenance_free_run(api: DesktopAPI, home: Path) -> None:
    """启动 WPF/WebView2 窗口宿主并转发请求。"""
    assets = Path(__file__).parent / "web"
    frozen = getattr(sys, "frozen", False)
    root = Path(sys.executable).parent if frozen else Path(__file__).resolve().parents[2] / "build"
    host = root / "desktop" / "Tbrain.Desktop.exe"
    if not host.is_file():
        raise FileNotFoundError(f"找不到桌面窗口宿主：{host}（请先运行 scripts/build.ps1 或 dotnet build）")
    with subprocess.Popen([str(host), str(assets), str(home / "webview")],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8") as process:
        try:
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                request = json.loads(line)
                result = getattr(api, str(request["method"]))(*request.get("args") or [])
                process.stdin.write(json.dumps({"id": request["id"], "result": result}, ensure_ascii=False) + "\n")
                process.stdin.flush()
                if api._quit_requested:
                    break
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass


def run(home: Path | None = None):
    if os.name != "nt":
        raise SystemExit("Tbrain 桌面程序仅支持 Windows")
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateMutexW(None, False, "Local\\Tbrain.Desktop")
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    already_running = ctypes.get_last_error() == 183
    try:
        user = ctypes.WinDLL("user32")
        user.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user.FindWindowW.restype = wintypes.HWND
        user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user.SetForegroundWindow.argtypes = [wintypes.HWND]
        # 启动即淘汰：先让其他版本的进程退出，否则旧窗口会一直占着单实例。
        evicted: dict = {}
        try:
            from . import proc

            evicted = proc.evict_old_versions(__version__, timeout=3.0, home=paths.app_home(home))
        except Exception:
            pass
        if evicted.get("requested"):
            # 旧版桌面退出后单实例锁才释放，这里重新申请一次，避免“淘汰了旧窗口却没有新窗口”。
            kernel.CloseHandle(handle)
            handle = kernel.CreateMutexW(None, False, "Local\\Tbrain.Desktop")
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            already_running = ctypes.get_last_error() == 183
        window = user.FindWindowW(None, "Tbrain")
        if already_running or window:
            if window:
                user.ShowWindow(window, 9)
                user.SetForegroundWindow(window)
            return
        service = TbrainService(home)
        stop = threading.Event()
        watcher = None
        try:
            from . import proc

            proc.register("desktop", service.version(), home=service.home)
            watcher = proc.start_watcher(on_exit=stop.set, home=service.home)
            proc.evict_in_background(service.version(), home=service.home)
        except Exception:
            watcher = None
        try:
            _maintenance_free_run(DesktopAPI(service), service.home)
        finally:
            stop.set()
            try:
                if watcher is not None:
                    watcher.stop()
                from . import proc

                proc.unregister(home=service.home)
            except Exception:
                pass
    finally:
        kernel.CloseHandle(handle)
