"""桌面后端 API 契约测试（不启动 WPF，只验证 DesktopAPI 的所有方法）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tbrain.desktop import DesktopAPI
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
    assert status["version"] == "0.6.0"
    assert status["ready"] is True
    assert str(vault) in status["vault_path"]
    assert isinstance(status["timezones"], list) and "Asia/Shanghai" in status["timezones"]
    settings_view = brain.settings()
    assert settings_view["settings"]["vault_path"] == str(vault)
    saved = brain.save_settings({"timezone": "UTC", "timeout_hours": 3})
    assert saved["ok"] is True
    assert saved["settings"]["timezone"] == "UTC"
    assert brain.time()["timezone"] == "UTC"


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
