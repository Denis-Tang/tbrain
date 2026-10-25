"""Process registry, old-version eviction and the voluntary-exit watcher.

Every long-lived Tbrain process (desktop, MCP server) registers itself in
``<app_home>/processes/<pid>.json``.  A newer installation can then ask older
versions to quit by writing ``<pid>.exit`` next to that file; the old process
watches for it in a background thread and shuts itself down.  If it does not
react within the timeout it is terminated with ``taskkill /F``.

The registry is intentionally separate from the SQLite index: it is tiny, written
by many processes and must never contend with user data writes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, paths

DEFAULT_INTERVAL = 0.5
DEFAULT_TIMEOUT = 5.0
ROLES = ("desktop", "mcp", "hook", "cli", "test")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _utc_iso() -> str:
    # millisecond precision: heartbeats within the same second must still differ
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _atomic_write(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _write_json(path: Path, payload: dict) -> None:
    _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _read_json(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _dir(home: Path | None) -> Path:
    return paths.processes_dir(home)


def _process_path(pid: int, home: Path | None) -> Path:
    return paths.process_file(pid, home)


def _exit_path(pid: int, home: Path | None) -> Path:
    return paths.exit_request_file(pid, home)


def _exe_path() -> str:
    executable = getattr(sys, "executable", "") or ""
    return str(executable or sys.argv[0] or "")


# ---------------------------------------------------------------------------
# liveness
# ---------------------------------------------------------------------------

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259


def _pid_alive_windows(pid: int) -> bool | None:
    """ctypes OpenProcess probe; None means "could not determine, try tasklist"."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # pragma: no cover - non-Windows / restricted interpreter
        return None


def _pid_alive_tasklist(pid: int) -> bool | None:
    try:
        completed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in (completed.stdout or "").splitlines():
        fields = [field.strip().strip('"') for field in line.split('","')]
        if len(fields) >= 2:
            try:
                return int(fields[1]) == int(pid)
            except ValueError:
                continue
    return False


def _pid_alive(pid: int) -> bool:
    """True when a process with this pid currently exists."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        answer = _pid_alive_windows(pid)
        if answer is None:
            answer = _pid_alive_tasklist(pid)
        return bool(answer)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def is_alive(pid: int) -> bool:
    """Public alias (tests may monkeypatch :func:`_pid_alive` instead)."""
    return _pid_alive(pid)


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------

def register(role: str = "cli", version: str | None = None, home: Path | None = None,
             exe: str | None = None, pid: int | None = None) -> dict:
    """Record this process so a newer version can ask it to quit."""
    pid = int(pid or os.getpid())
    now = _utc_iso()
    payload = {
        "pid": pid,
        "version": str(version or __version__),
        "role": str(role or "cli"),
        "exe": str(exe or _exe_path()),
        "started_at": now,
        "updated_at": now,
    }
    _write_json(_process_path(pid, home), payload)
    return payload


def touch(home: Path | None = None, pid: int | None = None) -> dict | None:
    """Refresh ``updated_at``; returns the updated record or None when unknown."""
    pid = int(pid or os.getpid())
    path = _process_path(pid, home)
    payload = _read_json(path)
    if payload is None:
        return None
    payload["updated_at"] = _utc_iso()
    _write_json(path, payload)
    return payload


def unregister(home: Path | None = None, pid: int | None = None) -> bool:
    """Remove the registry entry (and any pending exit request) for a process."""
    pid = int(pid or os.getpid())
    removed = False
    for path in (_process_path(pid, home), _exit_path(pid, home)):
        try:
            path.unlink()
            removed = True
        except OSError:
            pass
    return removed


def live_processes(home: Path | None = None, alive=None, prune: bool = True) -> list[dict]:
    """Registry entries whose pid is still alive; stale files are pruned."""
    alive = alive or _pid_alive
    folder = _dir(home)
    try:
        candidates = sorted(folder.glob("*.json"))
    except OSError:
        return []
    result: list[dict] = []
    for path in candidates:
        try:
            pid = int(path.stem)
        except ValueError:
            continue
        payload = _read_json(path)
        if payload is None:
            if prune:
                try:
                    path.unlink()
                except OSError:
                    pass
            continue
        if not alive(pid):
            if prune:
                for stale in (path, _exit_path(pid, home)):
                    try:
                        stale.unlink()
                    except OSError:
                        pass
            continue
        payload.setdefault("pid", pid)
        payload["pid"] = pid
        result.append(payload)
    result.sort(key=lambda item: str(item.get("started_at") or ""))
    return result


def live_versions(home: Path | None = None, alive=None) -> list[str]:
    """Versions of all live registered processes (used for the version warning)."""
    versions = []
    for item in live_processes(home=home, alive=alive):
        version = str(item.get("version") or "").strip()
        if version:
            versions.append(version)
    return versions


# ---------------------------------------------------------------------------
# exit requests and eviction
# ---------------------------------------------------------------------------

def read_exit_request(pid: int | None = None, home: Path | None = None) -> dict | None:
    return _read_json(_exit_path(int(pid or os.getpid()), home))


def request_exit(pid: int, home: Path | None = None, requester: int | None = None,
                 reason: str = "", current_version: str = "") -> dict:
    """Create ``<pid>.exit`` asking that process to stop gracefully."""
    payload = {
        "pid": int(pid),
        "requester": int(requester or os.getpid()),
        "reason": str(reason or "旧版本进程退出请求"),
        "current_version": str(current_version or __version__),
        "requested_at": _utc_iso(),
    }
    _write_json(_exit_path(int(pid), home), payload)
    return payload


def _default_killer(pid: int) -> bool:
    """Force-terminate one process; returns True when the kill was issued."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 4 or pid == os.getpid():
        return False
    try:
        if os.name == "nt":
            completed = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, text=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return completed.returncode == 0
        os.kill(pid, 15)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def evict_old_versions(current_version: str, timeout: float = DEFAULT_TIMEOUT,
                       home: Path | None = None, killer=None, alive=None,
                       sleep=None, interval: float = 0.2) -> dict:
    """Ask every live older-version process to exit, then force-kill leftovers.

    ``killer``/``alive``/``sleep`` are injectable so tests never terminate a real
    process.  The current process is never included in the eviction set.
    """
    home = paths.app_home(home)
    killer = killer or _default_killer
    alive = alive or _pid_alive
    sleep = sleep or time.sleep
    current = str(current_version or __version__)
    mine = os.getpid()
    from .installer import _version_key

    candidates = [item for item in live_processes(home=home, alive=alive)
                  if int(item.get("pid") or 0) not in (mine, 0)
                  and _version_key(str(item.get("version") or "")) < _version_key(current)]
    requested: list[int] = []
    for item in candidates:
        pid = int(item["pid"])
        try:
            request_exit(pid, home=home, reason="检测到旧版本进程，通知其退出",
                         current_version=current)
            requested.append(pid)
        except OSError:
            continue

    def still_running() -> list[int]:
        # Liveness, not registry presence: the watcher deletes its registry file
        # before the callback finishes, so a process that fails to shut down
        # (e.g. an old desktop still blocked on its child) must still be killed.
        return [pid for pid in requested if alive(pid)]

    deadline = time.monotonic() + max(0.0, float(timeout))
    running = still_running()
    while running and time.monotonic() < deadline:
        sleep(min(interval, max(0.0, deadline - time.monotonic())))
        running = still_running()

    exited = [pid for pid in requested if pid not in running]
    forced: list[int] = []
    for pid in running:
        if killer(pid):
            forced.append(pid)
    if forced:  # give the OS a moment to reap the force-killed processes
        grace = time.monotonic() + min(1.0, max(0.0, float(timeout)))
        remaining = [pid for pid in running if alive(pid)]
        while remaining and time.monotonic() < grace:
            sleep(min(interval, max(0.0, grace - time.monotonic())))
            remaining = [pid for pid in remaining if alive(pid)]
        running = remaining

    for pid in requested:
        if not alive(pid):
            for stale in (_process_path(pid, home), _exit_path(pid, home)):
                try:
                    stale.unlink()
                except OSError:
                    pass

    return {
        "current": current,
        "requested": requested,
        "exited": exited,
        "forced": forced,
        "remaining": running,
        "timed_out": bool(running),
    }


def evict_in_background(current_version: str | None = None, home: Path | None = None,
                        timeout: float = DEFAULT_TIMEOUT, on_done=None) -> threading.Thread:
    """Run :func:`evict_old_versions` on a daemon thread.

    Called right after a long-lived process registers itself, so starting the new
    version always removes the previous one without delaying the MCP handshake (a
    host kills a server that is slow to answer ``initialize``).
    """
    version = str(current_version or __version__)
    resolved = paths.app_home(home)

    def _run() -> None:
        try:
            outcome = evict_old_versions(version, timeout=timeout, home=resolved)
        except Exception:  # eviction must never take the process down
            return
        if on_done is not None:
            try:
                on_done(outcome)
            except Exception:
                pass

    thread = threading.Thread(target=_run, name="tbrain-evict", daemon=True)
    thread.start()
    return thread


# ---------------------------------------------------------------------------
# watcher
# ---------------------------------------------------------------------------

class ProcessWatcher:
    """Background thread that exits the process when an exit request appears."""

    def __init__(self, on_exit=None, home: Path | None = None,
                 interval: float = DEFAULT_INTERVAL, pid: int | None = None):
        self.on_exit = on_exit
        self.home = paths.app_home(home)
        self.interval = max(0.05, float(interval))
        self.pid = int(pid or os.getpid())
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="tbrain-exit-watcher", daemon=True)
        self._started = False

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> "ProcessWatcher":
        if not self._started:
            self._started = True
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout)

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def is_running(self) -> bool:
        return self._started and self._thread.is_alive()

    def __enter__(self) -> "ProcessWatcher":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()
        self.join(2.0)

    # -- loop --------------------------------------------------------------
    def _run(self) -> None:
        request_path = _exit_path(self.pid, self.home)
        try:
            while not self._stop.wait(self.interval):
                payload = _read_json(request_path)
                if payload is None:
                    continue
                self._invoke(payload)
                unregister(home=self.home, pid=self.pid)
                return
        finally:
            self._stop.set()

    def _invoke(self, payload: dict) -> None:
        """Callbacks may take the request dict or no arguments at all."""
        callback = self.on_exit
        if callback is None:
            return
        try:
            import inspect

            parameters = [item for item in inspect.signature(callback).parameters.values()
                          if item.kind in (inspect.Parameter.POSITIONAL_ONLY,
                                           inspect.Parameter.POSITIONAL_OR_KEYWORD)]
            takes_argument = len(parameters) >= 1
        except (TypeError, ValueError):
            takes_argument = True
        try:
            if takes_argument:
                callback(payload)
            else:
                callback()
        except Exception:  # never let a callback break shutdown
            pass


def start_watcher(on_exit=None, home: Path | None = None,
                  interval: float = DEFAULT_INTERVAL, pid: int | None = None) -> ProcessWatcher:
    """Start the exit-request watcher and return it (caller owns ``.stop()``)."""
    return ProcessWatcher(on_exit=on_exit, home=home, interval=interval, pid=pid).start()
