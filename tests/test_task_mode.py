"""Optional task mode: default-off compatibility and per-session selection."""

import json

import pytest

from tbrain import session as flow
from tbrain.service import TbrainService


def message(service, host, name, cwd, text):
    return service.hook(host, {"hook_event_name": "UserPromptSubmit", "session_id": name,
                               "cwd": str(cwd), "prompt": text})


def test_default_off_keeps_memory_and_existing_projects_unchanged(tmp_path):
    service = TbrainService(tmp_path / "home", vault=tmp_path / "vault")
    service.settings.update({"vault_path": str(tmp_path / "vault"), "ntp_enabled": False})
    folder = tmp_path / "code"
    folder.mkdir()
    assert service.settings.load().task_mode is False
    service.configure_project("existing", [str(folder)])
    assert not (folder / ".git").exists()
    assert not (folder / ".tbrain").exists()
    assert not (service.store.project_dir("existing") / "完成标准.md").exists()
    notice = message(service, "codex", "ordinary", folder, "只讨论需求并保留记录")
    assert "普通记忆模式" in json.dumps(notice, ensure_ascii=False)
    assert service.save("codex:ordinary", [{"op": "append", "section": "done",
        "items": [{"text": "需求已经说明"}]}])["ok"]
    for _ in range(3):
        service.hook("codex", {"hook_event_name": "ToolResult", "session_id": "ordinary",
            "cwd": str(folder), "last_test": {"command": "pytest -q", "exit_code": 1}})
    summary, path = flow.load_session(service, "codex:ordinary")
    assert summary.status != "已暂停"
    assert service.save("codex:ordinary", [{"op": "set_status", "status": "已完成"}])["ok"]
    assert service.hook("codex", {"hook_event_name": "WrapUp", "session_id": "ordinary",
                                  "cwd": str(folder)}) == {}
    # A pre-upgrade summary without a mode field remains ordinary after opt-in.
    summary, path = flow.load_session(service, "codex:ordinary")
    summary.header.pop("task_mode")
    flow.write_session(service, summary, path)
    assert service.save_settings({"task_mode": True})["settings"]["task_mode"] is True
    assert not (folder / ".git").exists()
    assert "普通记忆模式" in service.bootstrap("codex:ordinary", "codex", str(folder))["package"]
    assert service.save("codex:ordinary", [{"op": "append", "section": "done",
        "items": [{"text": "旧会话继续保存"}]}])["ok"]


@pytest.mark.parametrize("host", ["claude-code", "codex", "dsh", "pi"])
def test_toggle_affects_new_sessions_and_keeps_running_sessions_fixed(tmp_path, host):
    service = TbrainService(tmp_path / "home", vault=tmp_path / "vault")
    service.settings.update({"vault_path": str(tmp_path / "vault"), "ntp_enabled": False})
    message(service, host, "before", tmp_path, "开关开启前的会话")
    service.save_settings({"task_mode": True})
    # A separate service, as used by MCP or Hook, reads the same setting.
    service = TbrainService(service.home)
    notice = message(service, host, "enabled", tmp_path, "按业务规则完成修改")
    assert "待确认的任务理解" in json.dumps(notice, ensure_ascii=False)
    assert service.save(f"{host}:enabled", [{"op": "understanding", "goal": "完成本次修改",
        "scope": "遵守业务规则", "deliverables": "修改结果", "done": "通过约定的检查"}])["ok"]
    message(service, host, "enabled", tmp_path, "确认")
    assert service.save(f"{host}:enabled", [{"op": "confirm", "question": "Q1", "source": "U2",
        "why": "用户确认目标", "tradeoffs": "不扩大范围", "decided_by": "用户"}])["ok"]
    service.save_settings({"task_mode": False})
    for _ in range(3):
        assert service.save(f"{host}:enabled", [{"op": "attempt", "route": "pytest -q exit 1"}])["ok"]
    summary, _ = flow.load_session(service, f"{host}:enabled")
    assert summary.status == "已暂停"
    assert summary.header["task_mode"] is True
    message(service, host, "after", tmp_path, "关闭后创建的新会话")
    for name in ("before", "after"):
        assert "普通记忆模式" in service.bootstrap(f"{host}:{name}", host, str(tmp_path))["package"]
        assert service.save(f"{host}:{name}", [{"op": "append", "section": "done",
            "items": [{"text": "普通保存不要求任务确认"}]}])["ok"]
    assert not service.save(f"{host}:after", [{"op": "attempt", "route": "pytest -q"}])["ok"]
    # Reading a paused task's handoff must not import its gates into ordinary mode.
    resumed = service.resume(f"{host}:after", f"{host}:enabled")
    assert resumed["ok"]
    summary, _ = flow.load_session(service, f"{host}:after")
    assert summary.status != "已暂停" and summary.header["task_mode"] is False
    assert service.save(f"{host}:after", [{"op": "append", "section": "done",
        "items": [{"text": "普通模式接续仍能保存"}]}])["ok"]
