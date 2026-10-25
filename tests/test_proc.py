"""Process registry, exit requests, eviction and the watcher thread.

Tests use the current Python process pid for "alive" checks, synthetic pids for
dead processes and injected ``alive``/``killer``/``sleep`` callables for eviction
so no real Tbrain (or user) process is ever signalled.
"""

import os
import threading
import time
from pathlib import Path

from tbrain import paths, proc


def _write_registry(tmp_path: Path, pid: int, version: str = "0.5.9") -> dict:
    return proc.register("test", version=version, home=tmp_path, pid=pid)


def test_register_touch_and_unregister(tmp_path):
    record = proc.register("test", version="0.5.9", home=tmp_path)
    assert record["pid"] == os.getpid()
    assert record["role"] == "test"
    assert record["version"] == "0.5.9"
    assert record["started_at"] and record["updated_at"]
    path = paths.process_file(os.getpid(), tmp_path)
    assert path.is_file()

    first = path.read_text(encoding="utf-8")
    time.sleep(0.01)
    touched = proc.touch(home=tmp_path)
    assert touched is not None
    assert path.read_text(encoding="utf-8") != first

    assert proc.unregister(home=tmp_path) is True
    assert not path.exists()
    assert proc.unregister(home=tmp_path) is False


def test_live_processes_filters_dead_pids_and_prunes(tmp_path):
    proc.register("test", version="0.6.0", home=tmp_path)
    _write_registry(tmp_path, 999001, version="0.5.9")
    # Only the current process counts as alive in this simulation.
    live = proc.live_processes(home=tmp_path, alive=lambda pid: pid == os.getpid())
    assert [item["pid"] for item in live] == [os.getpid()]
    assert not paths.process_file(999001, tmp_path).exists()


def test_live_versions_returns_registered_versions(tmp_path):
    proc.register("mcp", version="0.6.0", home=tmp_path)
    _write_registry(tmp_path, 999002, version="0.5.9")
    versions = proc.live_versions(home=tmp_path, alive=lambda pid: True)
    assert sorted(versions) == ["0.5.9", "0.6.0"]


def test_watcher_calls_back_on_exit_request(tmp_path):
    proc.register("test", version="0.5.9", home=tmp_path)
    seen: list[dict] = []
    done = threading.Event()

    def on_exit(payload):
        seen.append(payload)
        done.set()

    watcher = proc.start_watcher(on_exit=on_exit, home=tmp_path, interval=0.02)
    try:
        proc.request_exit(os.getpid(), home=tmp_path, reason="升级", current_version="0.6.0")
        assert done.wait(5), "watcher did not react to the exit request"
        assert seen[0]["reason"] == "升级"
        assert seen[0]["current_version"] == "0.6.0"
        assert seen[0]["requester"] == os.getpid()
        # the watcher unregisters itself before exiting
        deadline = time.monotonic() + 2
        while paths.process_file(os.getpid(), tmp_path).exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not paths.process_file(os.getpid(), tmp_path).exists()
    finally:
        watcher.stop()
        watcher.join(2)
    assert watcher.is_running() is False


def test_watcher_accepts_zero_argument_callback(tmp_path):
    # services pass ``stop.set`` / ``lambda: os._exit(0)`` style callbacks
    proc.register("desktop", version="0.5.9", home=tmp_path)
    fired = threading.Event()
    watcher = proc.start_watcher(on_exit=fired.set, home=tmp_path, interval=0.02)
    try:
        proc.request_exit(os.getpid(), home=tmp_path)
        assert fired.wait(5)
    finally:
        watcher.stop()
        watcher.join(2)


def test_watcher_stop_does_not_unregister_early(tmp_path):
    proc.register("test", version="0.6.0", home=tmp_path)
    watcher = proc.start_watcher(home=tmp_path, interval=0.02)
    watcher.stop()
    watcher.join(2)
    assert paths.process_file(os.getpid(), tmp_path).exists()
    proc.unregister(home=tmp_path)


def test_evict_graceful_exit(tmp_path):
    pids = [990101, 990102]
    for pid in pids:
        _write_registry(tmp_path, pid, version="0.5.9")
    killed: list[int] = []

    result = proc.evict_old_versions(
        "0.6.0", timeout=0.3, home=tmp_path,
        alive=lambda pid: not paths.exit_request_file(pid, tmp_path).exists(),
        killer=lambda pid: killed.append(pid) or True,
        sleep=lambda seconds: time.sleep(0.005),
    )
    assert result["requested"] == pids
    assert sorted(result["exited"]) == pids
    assert result["forced"] == []
    assert result["remaining"] == []
    assert killed == []


def test_evict_force_kills_after_timeout(tmp_path):
    pids = [990201, 990202]
    for pid in pids:
        _write_registry(tmp_path, pid, version="0.5.9")
    killed: list[int] = []

    result = proc.evict_old_versions(
        "0.6.0", timeout=0.10, home=tmp_path,
        alive=lambda pid: True,  # never exits on its own
        killer=lambda pid: killed.append(pid) or True,
        sleep=lambda seconds: time.sleep(0.005),
    )
    assert sorted(result["requested"]) == pids
    assert sorted(result["forced"]) == pids
    assert killed and sorted(killed) == pids
    assert result["timed_out"] is True


def test_evict_skips_current_version_and_self(tmp_path):
    proc.register("desktop", version="0.6.0", home=tmp_path)  # current process
    _write_registry(tmp_path, 990301, version="0.6.0")       # same version
    _write_registry(tmp_path, 990302, version="0.5.9")       # older
    requested: list[int] = []

    proc.evict_old_versions(
        "0.6.0", timeout=0.05, home=tmp_path,
        alive=lambda pid: True,
        killer=lambda pid: requested.append(pid) or False,
        sleep=lambda seconds: None,
    )
    # only the old version is targeted; the current process pid never is
    assert requested == [990302]
    assert os.getpid() not in requested


def test_pid_alive_reports_current_and_invalid_pid():
    assert proc.is_alive(os.getpid()) is True
    assert proc.is_alive(0) is False
    assert proc.is_alive(-5) is False


def test_evict_does_not_use_default_killer(tmp_path, monkeypatch):
    _write_registry(tmp_path, 990401, version="0.5.9")
    called: list[int] = []

    def fake_default(pid):
        called.append(pid)
        return True

    monkeypatch.setattr(proc, "_default_killer", fake_default)
    result = proc.evict_old_versions(
        "0.6.0", timeout=0.05, home=tmp_path,
        alive=lambda pid: True,
        killer=lambda pid: called.append(pid) or True,  # injected: default must not run
        sleep=lambda seconds: None,
    )
    assert result["forced"] == [990401]
    assert called == [990401]


def test_evict_in_background_runs_with_current_version(tmp_path, monkeypatch):
    calls: list[dict] = []

    def fake_evict(version, timeout=0.0, home=None, **kwargs):
        calls.append({"version": version, "home": home})
        return {"requested": []}

    monkeypatch.setattr(proc, "evict_old_versions", fake_evict)
    done: list[dict] = []
    thread = proc.evict_in_background("0.6.9", home=tmp_path, on_done=done.append)
    thread.join(5)
    assert not thread.is_alive()
    assert [item["version"] for item in calls] == ["0.6.9"]
    assert Path(calls[0]["home"]) == tmp_path.resolve()
    assert done == [{"requested": []}]


def test_evict_in_background_swallows_errors(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(proc, "evict_old_versions", boom)
    thread = proc.evict_in_background("0.6.9", home=tmp_path)
    thread.join(5)
    assert not thread.is_alive()
