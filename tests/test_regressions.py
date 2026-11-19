"""Public-API regressions confirmed against ea8c789."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tbrain.check import run_check
from tbrain.hosts import config as host_config
from tbrain.hosts import dsh
from tbrain.service import TbrainService


def _service(tmp_path, name="home"):
    home = tmp_path / name
    vault = tmp_path / f"{name}-vault"
    service = TbrainService(home, vault=vault)
    service.settings.update({"vault_path": str(vault), "ntp_enabled": False, "timezone": "Asia/Shanghai"})
    return service


def test_existing_check_command_still_fails_after_project_setup(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    marker = tmp_path / "ran"
    command = (
        f'"{sys.executable}" -c "import pathlib; pathlib.Path(r\'{marker}\').write_text(\'ran\'); raise SystemExit(7)"'
    )
    (app / "tbrain.check.yaml").write_text(
        "version: 1\nchecks:\n  - name: app-tests\n    run: " + json.dumps(command) + "\nlints: []\n",
        encoding="utf-8",
    )
    before = run_check(app)
    assert before["checks"][0]["exit_code"] == 7
    assert before["ok"] is False
    assert marker.is_file()
    marker.unlink()

    service = _service(tmp_path)
    saved = service.configure_project("检查回归", [str(app)])
    assert not (app / ".tbrain" / "check.yaml").exists()
    assert "SystemExit(7)" in (app / "tbrain.check.yaml").read_text(encoding="utf-8")
    after = run_check(app)
    assert after["ok"] is False
    assert after["checks"][0]["exit_code"] == 7
    assert marker.is_file()
    again = service.configure_project("检查回归", [str(app)])
    assert again["paths"] == saved["paths"]
    marker.unlink()
    assert run_check(app)["checks"][0]["exit_code"] == 7


def test_one_selected_directory_reaches_dsh_as_one_directory(tmp_path, monkeypatch):
    app = tmp_path / "app"
    app.mkdir()
    (app / "README.md").write_text("one\n", encoding="utf-8")
    service = _service(tmp_path)
    host_config.write_connect_state(service.home, "dsh", {"entry": "tbrain"})
    seen = []
    original = dsh.create_workspace

    def spy(project, home):
        seen.append(list(project["paths"]))
        return original(project, home)

    monkeypatch.setattr(dsh, "create_workspace", spy)
    saved = service.configure_project("单目录", [str(app)])
    assert len(saved["paths"]) == 1
    assert Path(saved["paths"][0]).resolve() == app.resolve()
    assert seen and len(seen[0]) == 1
    assert any("请更新 Tbrain 接入插件" in item for item in saved["workspace_sync"])
    assert not any("只支持一个目录" in item for item in saved["workspace_sync"])
    assert dsh.create_workspace({"name": "两个根", "paths": [str(app), str(tmp_path / "other")]}, service.home) == (
        "未同步：DSH 工作区只支持一个目录"
    )
    again = service.configure_project("单目录", [str(app)])
    assert len(again["paths"]) == 1
    assert "workspace_sync" not in again


def test_populated_folder_keeps_code_and_does_not_commit_it(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    (app / "app.py").write_text("SENTINEL = 1\n", encoding="utf-8")
    service = _service(tmp_path, "nongit")
    service.configure_project("未入库", [str(app)])
    assert (app / "app.py").read_text(encoding="utf-8") == "SENTINEL = 1\n"
    assert not (app / ".git").exists()


def test_parent_check_runs_from_the_directory_that_owns_its_config(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "ROOT").write_text("marker", encoding="utf-8")
    command = subprocess.list2cmdline([sys.executable, "-c", "from pathlib import Path; assert Path('ROOT').is_file()"])
    (tmp_path / "tbrain.check.yaml").write_text(
        "checks:\n  - run: " + json.dumps(command) + "\nlints: []\n", encoding="utf-8")
    child = tmp_path / "child"
    (child / ".tbrain").mkdir(parents=True)
    (child / ".tbrain" / "check.yaml").write_text("checks: []\nlints: []\n", encoding="utf-8")
    assert run_check(tmp_path)["ok"]
    assert run_check(child)["ok"]
