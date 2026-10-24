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
import subprocess
import sys
import threading

from .service import TbrainService


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
