"""桌面后端 API 契约测试（不启动 WPF，只验证 DesktopAPI 的所有方法）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tbrain.desktop import DesktopAPI
from tbrain import __version__
from tbrain.service import TbrainService


@pytest.fixture()
def api(tmp_path, monkeypatch):
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    work = tmp_path / "work"
    vault.mkdir()
    work.mkdir()
    monkeypatch.setenv("TBRAIN_HOME", str(home))
    monkeypatch.setenv("TBRAIN_INSTALL_ROOT", str(tmp_path / "install"))
    service = TbrainService(home, vault=vault)
    service.settings.update({"vault_path": str(vault), "timezone": "Asia/Shanghai",
                             "ntp_enabled": False})
    service.reset_vault()
    brain = DesktopAPI(service)
    return brain, service, vault, work


def _create_session(service, work, host="claude-code", session="desktop-1", text="桌面后端契约测试"):
    service.hook(host, {"hook_event_name": "UserPromptSubmit", "session_id": session,
                        "cwd": str(work), "prompt": text})
    service.save(f"{host}:{session}", [
        {"op": "append", "section": "done", "items": [{"text": "实现桌面 API"}]},
        {"op": "replace", "section": "next_step", "text": "跑桌面契约测试"},
    ])
    return f"{host}:{session}"


def test_status_and_settings(api):
    brain, service, vault, work = api
    status = brain.status()
    assert status["version"] == __version__
    assert status["ready"] is True
    assert str(vault) in status["vault_path"]
    assert isinstance(status["timezones"], list) and "Asia/Shanghai" in status["timezones"]
    settings_view = brain.settings()
    assert settings_view["settings"]["vault_path"] == str(vault)
    saved = brain.save_settings({"timezone": "UTC", "timeout_hours": 3})
    assert saved["ok"] is True
    assert saved["settings"]["timezone"] == "UTC"
    assert brain.time()["timezone"] == "UTC"


def test_first_run_setup_installs_only_selected_hosts(tmp_path, monkeypatch):
    import shutil
    import sys
    from tbrain import desktop, installer
    from test_installer import make_package

    home, vault, root = tmp_path / "home", tmp_path / "中文知识库", tmp_path / "install"
    for key, name in (("TBRAIN_INSTALL_ROOT", "install"), ("CLAUDE_CONFIG_DIR", "claude"),
                      ("CODEX_HOME", "codex"), ("DSH_HOME", "dsh"),
                      ("PI_CODING_AGENT_DIR", "pi"), ("APPDATA", "appdata"), ("LOCALAPPDATA", "local")):
        monkeypatch.setenv(key, str(tmp_path / name))
    dsh_exe = tmp_path / "local/Programs/DeepSeek Harness/DeepSeek Harness.exe"
    dsh_exe.parent.mkdir(parents=True)
    dsh_exe.touch()
    package = make_package(tmp_path, __version__)
    shutil.copytree(Path(__file__).parents[1] / "integrations", package / "integrations", dirs_exist_ok=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(package / "tbrain.exe"))
    monkeypatch.setattr(desktop.shutil, "which", lambda name: f"test-{name}")
    service = TbrainService(home)
    service.settings.save({"ntp_enabled": False})
    brain = DesktopAPI(service)
    assert brain.status()["ready"] is False
    assert brain.status()["vault_path"] == ""
    assert brain.status()["vaults"] == []
    assert brain.setup_info()["required"] is True
    for _ in range(2):
        result = brain.setup(str(vault), ["claude-code", "dsh", "pi"])
        assert result["ok"] and all(item["ok"] for item in result["hosts"])
    assert installer.current_version(root) == __version__
    assert brain.status()["ready"] is True
    assert brain.setup_info()["required"] is False
    assert not (tmp_path / "codex").exists()
    text = (tmp_path / "dsh/profiles/desktop/cordis.patch.yml").read_text(encoding="utf-8")
    assert text.count("id: tbrain-mcp") == text.count("id: tbrain-native") == 1
    assert (vault / "全局提示词.md").exists()
    if sys.platform == "win32":
        assert (tmp_path / "appdata/Microsoft/Windows/Start Menu/Programs/Tbrain.lnk").is_file()


def test_setup_rejects_missing_agent_and_invalid_paths_before_install(api, monkeypatch):
    brain, service, vault, _ = api
    monkeypatch.setenv("LOCALAPPDATA", str(vault / "no-apps"))
    monkeypatch.setattr("tbrain.desktop.shutil.which", lambda name: f"old-cli-{name}")
    assert brain.setup_info()["hosts"]["dsh"] is False
    before = service.settings.path.read_bytes()
    assert "完整路径" in brain.setup("relative", ["dsh"])["error"]
    assert "未检测到" in brain.setup(str(vault), ["dsh"])["error"]
    assert "支持的" in brain.setup(str(vault), ["unknown"])["error"]
    assert service.settings.path.read_bytes() == before


def test_sessions_detail_and_rename(api):
    brain, service, vault, work = api
    session_id = _create_session(service, work)
    rows = brain.sessions("all")
    assert len(rows) == 1
    assert rows[0]["updated"].endswith("Z")            # ISO 时间给前端本地化
    assert rows[0]["next_step"] == "跑桌面契约测试"
    filtered = brain.sessions("进行中")
    assert len(filtered) == 1
    assert brain.sessions("已完成") == []
    detail = brain.session_detail(rows[0]["path"])
    assert detail["ok"] is True
    assert "桌面后端契约测试" in detail["markdown"]
    renamed = brain.rename_session(rows[0]["path"], "改名后的会话")
    assert renamed["ok"] is True
    assert brain.sessions("all")[0]["name"] == "改名后的会话"


def test_session_refresh_reads_external_edits_and_deletions(api):
    brain, service, vault, work = api
    _create_session(service, work)
    row = brain.sessions()[0]
    path = vault / row["path"]
    text = path.read_text(encoding="utf-8").replace("status: 进行中", "status: 已完成")
    path.write_text(text, encoding="utf-8")
    assert brain.sessions("进行中") == []
    assert brain.sessions("已完成")[0]["session_id"] == row["session_id"]
    path.unlink()
    assert brain.sessions() == []


def test_projects_keep_parent_child_relationship(api):
    brain, _, _, work = api
    child_work = work / "child"
    child_work.mkdir()
    brain.project_save("主项目", [str(work)])
    brain.project_save("子项目", [str(child_work)], parent="主项目")
    projects = {item["name"]: item for item in brain.projects()}
    assert projects["主项目"]["parent"] == ""
    assert projects["子项目"]["parent"] == "主项目"


def test_global_prompt_roundtrip(api):
    brain, service, vault, work = api
    text = "<!-- owner:user -->\n- 用户偏好：中文\n<!-- /owner:user -->\n<!-- owner:model -->\n<!-- /owner:model -->\n"
    assert brain.save_global_prompt(text)["ok"] is True
    assert "用户偏好：中文" in brain.global_prompt()["markdown"]


def test_projects_crud_and_prompt(api):
    brain, service, vault, work = api
    saved = brain.project_save("Tbrain", [str(work)], parent="", allow_child=False)
    assert saved["name"] == "Tbrain"
    projects = brain.projects()
    assert projects and projects[0]["name"] == "Tbrain"
    prompt_saved = brain.project_prompt_save("Tbrain", {
        "最终目标": "完成 Tbrain 0.6.0 最小闭环",
        "硬规则": "- 测试用临时目录（来源：U1）",
        "当前阶段": "第 4 步：桌面后端",
        "关键路径和命令": "- 测试命令：.venv\\Scripts\\python.exe -m pytest -q",
    })
    assert prompt_saved["written"]
    prompt = brain.project_prompt("Tbrain")
    assert "完成 Tbrain 0.6.0 最小闭环" in prompt["sections"]["最终目标"]
    deleted = brain.project_delete("Tbrain")
    assert deleted.get("ok") is not False
    assert brain.projects() == []


def test_doctor_connect_disconnect_do_not_touch_real_configs(api, monkeypatch, tmp_path):
    brain, service, vault, work = api
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "fake-home"))
    doctor = brain.doctor()
    assert "items" in doctor
    assert {item["id"] for item in doctor["items"]} >= {"install", "processes", "claude-code", "codex", "dsh", "pi"}
    result = brain.connect("pi")
    assert result.get("ok") is True
    assert result.get("files")
    disconnect = brain.disconnect("pi")
    assert disconnect.get("ok") is True


def test_open_document_missing_returns_error(api, tmp_path):
    brain, service, vault, work = api
    result = brain.open_document(str(tmp_path / "missing.md"))
    assert result["ok"] is False
    assert "不存在" in result["error"]


def test_update_check_failure_degrades(api, monkeypatch):
    brain, service, vault, work = api

    def broken(*args, **kwargs):
        raise RuntimeError("offline")

    from tbrain import update

    monkeypatch.setattr(update, "check_latest", broken)
    result = brain.update_check(False)
    assert result.get("ok") is False or "error" in result


def test_upgrade_requests_restart_after_reply(api, monkeypatch):
    desktop, service, vault, _ = api
    root = vault.parent / 'install'
    monkeypatch.setattr(service, 'apply_upgrade', lambda: {
        'ok': True, 'upgraded': True, 'install_root': str(root), 'to': '0.6.4'})
    result = desktop.update_apply()
    assert result['restarting'] and desktop._quit_requested
    assert desktop._restart_executable == root / 'versions' / '0.6.4' / 'tbrain.exe'


@pytest.mark.parametrize('installed,should_install', [('0.6.2', True), ('99.0.0', False)])
def test_packaged_desktop_selects_new_version_before_host_reconnect(api, monkeypatch, installed, should_install):
    from tbrain import desktop, installer

    _, service, _, _ = api
    root = Path(service.settings.load().install_root or desktop.paths.install_root(service.home))
    root.mkdir(parents=True, exist_ok=True)
    (root / 'current.txt').write_text(installed, encoding='utf-8')
    executable = root.parent / 'download' / 'tbrain.exe'
    monkeypatch.setattr(desktop.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(desktop.sys, 'executable', str(executable))
    copied = []
    monkeypatch.setattr(installer, 'install_from_dir', lambda *args: copied.append(args))
    desktop._prepare_installation(service)
    assert copied == ([(executable.parent, __version__, root)] if should_install else [])
