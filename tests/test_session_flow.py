"""流程层集成测试：hook → bootstrap → save → search → resume，全部用临时目录。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from tbrain import handoff
from tbrain.service import TbrainService


@pytest.fixture()
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    work = tmp_path / "work"
    vault.mkdir()
    work.mkdir()
    monkeypatch.setenv("TBRAIN_HOME", str(home))
    service = TbrainService(home, vault=vault)
    service.settings.update({"vault_path": str(vault), "timezone": "Asia/Shanghai",
                             "ntp_enabled": False, "timeout_hours": 6})
    service.reset_vault()
    return service, vault, work


def prompt(service, host, session, cwd, text, event="UserPromptSubmit"):
    return service.hook(host, {"hook_event_name": event, "session_id": session,
                               "cwd": str(cwd), "prompt": text})


def sid(host, session):
    return f"{host}:{session}"


def test_first_message_creates_summary_and_startup_package(env):
    service, vault, work = env
    session = "aaaaaaaa-1111-2222-3333-444444444444"
    output = prompt(service, "claude-code", session, work, "帮我重构 Tbrain 的存储层并跑通测试")
    assert "hookSpecificOutput" in output
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "session_id: claude-code:" + session in context
    assert "## 当前时间" in context
    row = service.index.session(sid("claude-code", session))
    assert row is not None
    summary, path = _load(service, row)
    assert summary.raw_command == "帮我重构 Tbrain 的存储层并跑通测试"
    assert summary.status == "进行中"
    assert summary.project == ""
    assert path.is_file()


def test_second_message_appends_u2_and_stop_reminder_blocks_until_saved(env):
    service, vault, work = env
    session = "bbbbbbbb-1111-2222-3333-444444444444"
    prompt(service, "claude-code", session, work, "第一条：把索引改成 SQLite")
    assert prompt(service, "claude-code", session, work, "第二条：测试必须用临时目录") == {}
    row = service.index.session(sid("claude-code", session))
    summary, _ = _load(service, row)
    assert len(summary.users) == 2
    stop = service.hook("claude-code", {"hook_event_name": "Stop", "session_id": session,
                                        "cwd": str(work), "stop_hook_active": False})
    assert stop["decision"] == "block"
    assert "save" in stop["reason"]
    assert service.hook("claude-code", {"hook_event_name": "Stop", "session_id": session,
                                        "cwd": str(work), "stop_hook_active": True}) == {}
    service.save(sid("claude-code", session), [{"op": "append", "section": "done",
                                                "items": [{"text": "完成存储层"}]}])
    assert service.hook("claude-code", {"hook_event_name": "Stop", "session_id": session,
                                        "cwd": str(work), "stop_hook_active": False}) == {}


def test_session_start_resume_reinjects_package_and_records_end(env):
    service, vault, work = env
    session = "cccccccc-1111-2222-3333-444444444444"
    prompt(service, "claude-code", session, work, "把会话总结迁移到 Markdown")
    service.save(sid("claude-code", session), [
        {"op": "append", "section": "done", "items": [{"text": "读完 canvas 设计稿"}]},
        {"op": "replace", "section": "next_step", "text": "写索引事务并跑 pytest 验证"},
    ])
    service.hook("claude-code", {"hook_event_name": "SessionEnd", "session_id": session,
                                 "cwd": str(work), "reason": "other"})
    row = service.index.session(sid("claude-code", session))
    assert _load(service, row)[0].status == "已中断"
    started = service.hook("claude-code", {"hook_event_name": "SessionStart", "source": "resume",
                                           "session_id": session, "cwd": str(work)})
    context = started["hookSpecificOutput"]["additionalContext"]
    assert "把会话总结迁移到 Markdown" in context
    assert "写索引事务并跑 pytest 验证" in context
    row = service.index.session(sid("claude-code", session))
    assert _load(service, row)[0].status == "进行中"


def test_save_partial_accept_and_validation_errors(env):
    service, vault, work = env
    session = "dddddddd-1111-2222-3333-444444444444"
    prompt(service, "claude-code", session, work, "实现 save 的逐条校验")
    result = service.save(sid("claude-code", session), [
        {"op": "append", "section": "done", "items": [{"text": "合格条目 [证据:E1]"}, {"text": "x" * 260}]},
        {"op": "append", "section": "dead_end", "items": [{"attempt": "直接删除旧索引", "reason_kind": "未知",
                                                           "conditions": "Windows 10"}]},
        {"op": "replace", "section": "next_step", "text": "跑 pytest -q 验证"},
        {"op": "set_status", "status": "已中断"},
        {"op": "append", "section": "decision", "items": [{"text": "只增不删 | 用户修改优先"}]},
    ])
    assert result["ok"] is True
    written = result["written"]
    assert written["done"] == ["D1"]
    assert written["decisions"] == ["K1"]
    assert written["next_step"]
    reasons = [item["reason"] for item in result["rejected"]]
    assert any("200 字" in reason for reason in reasons)
    assert any("reason_kind" in reason for reason in reasons)
    assert any("已中断" in reason for reason in reasons)
    summary, _ = _load(service, service.index.session(sid("claude-code", session)))
    assert len(summary.items("已完成")) == 1


def test_repeated_bad_operations_move_to_pending(env):
    service, vault, work = env
    session = "eeeeeeee-1111-2222-3333-444444444444"
    prompt(service, "claude-code", session, work, "测试不合格条目的待整理兜底")
    bad = [{"op": "append", "section": "done", "items": [{"text": "x" * 260}]}]
    for _ in range(3):
        result = service.save(sid("claude-code", session), bad)
        assert result["rejected"]
    summary, _ = _load(service, service.index.session(sid("claude-code", session)))
    assert summary.pending
    assert any("未校验" in item for item in summary.pending)


def test_user_edited_target_is_not_overwritten(env):
    service, vault, work = env
    session = "ffffffff-1111-2222-3333-444444444444"
    prompt(service, "claude-code", session, work, "验证手改保护")
    service.save(sid("claude-code", session), [{"op": "replace", "section": "target", "text": "程序写的目标"}])
    row = service.index.session(sid("claude-code", session))
    summary, path = _load(service, row)
    text = path.read_text(encoding="utf-8")
    text = text.replace("程序写的目标", "用户手写的目标")
    path.write_text(text, encoding="utf-8")
    result = service.save(sid("claude-code", session), [
        {"op": "replace", "section": "target", "text": "模型想改的目标"},
        {"op": "append", "section": "done", "items": [{"text": "追加仍然允许"}]},
    ])
    assert result["user_modified"] is True
    assert any("手改" in item["reason"] for item in result["rejected"])
    summary, _ = _load(service, service.index.session(sid("claude-code", session)))
    assert summary.target == "用户手写的目标"
    assert summary.conflicts
    assert summary.items("已完成")


def test_resume_returns_handoff_and_marks_resumed_from(env):
    service, vault, work = env
    work2 = work.parent / "other"
    work2.mkdir()
    old_session = "11111111-aaaa-bbbb-cccc-000000000001"
    new_session = "11111111-aaaa-bbbb-cccc-000000000002"
    prompt(service, "claude-code", old_session, work, "实现 Tbrain 的时间工具")
    service.save(sid("claude-code", old_session), [
        {"op": "append", "section": "done", "items": [{"text": "实现 SNTP 校时"}]},
        {"op": "append", "section": "evidence", "items": [{"kind": "file", "text": "src/tbrain/clock.py:1"}]},
        {"op": "append", "section": "goal", "items": [{"text": "补齐 HTTPS Date 兜底"}]},
        {"op": "replace", "section": "next_step", "text": "补 HTTPS 兜底并跑时间测试"},
    ])
    old_name = service.index.session(sid("claude-code", old_session))["name"]
    prompt(service, "codex", new_session, work2, "接着上次的时间工具继续")
    result = service.resume(sid("codex", new_session), old_name)
    assert result["ok"] is True
    assert "实现 Tbrain 的时间工具" in result["handoff"]
    assert "补齐 HTTPS Date 兜底" in result["handoff"]
    assert "补 HTTPS 兜底并跑时间测试" in result["handoff"]
    new_summary, _ = _load(service, service.index.session(sid("codex", new_session)))
    assert new_summary.header["resumed_from"] == f"[[{old_name}]]"


def test_search_scopes_project_public_and_independent(env):
    service, vault, work = env
    project_work = work
    service.store.save_project("Tbrain", [str(project_work)])
    indep_work = work.parent / "indep"
    indep_work.mkdir()
    prompt(service, "claude-code", "12345678-0000-0000-0000-000000000001", project_work,
           "项目会话：实现中文检索召回")
    prompt(service, "pi", "12345678-0000-0000-0000-000000000002", indep_work,
           "独立会话：中文检索的另一种尝试")
    service.save(sid("claude-code", "12345678-0000-0000-0000-000000000001"), [
        {"op": "append", "section": "done", "items": [{"text": "做了中文二字切分检索"}]}])
    service.save(sid("pi", "12345678-0000-0000-0000-000000000002"), [
        {"op": "append", "section": "done", "items": [{"text": "独立会话也试了中文检索"}]}])
    result = service.search("中文检索", sid("claude-code", "12345678-0000-0000-0000-000000000001"))
    assert result["ok"] is True
    assert result["results"]
    assert result["results"][0]["project"] == "Tbrain"
    assert len(result["results"]) <= 5


def test_independent_session_bootstrap_and_resume(env):
    service, vault, work = env
    work2 = work.parent / "free"
    work2.mkdir()
    result = service.bootstrap("pi:99999999-0000-0000-0000-000000000001", "pi", str(work2))
    assert result["ok"] is True
    assert result["project"] == ""
    prompt(service, "pi", "99999999-0000-0000-0000-000000000001", work2, "独立会话：整理迁移报告")
    service.save(sid("pi", "99999999-0000-0000-0000-000000000001"), [
        {"op": "replace", "section": "next_step", "text": "写 dry-run 报告"}])
    current_name = service.index.session(sid("pi", "99999999-0000-0000-0000-000000000001"))["name"]
    resumed = service.resume(sid("pi", "99999999-0000-0000-0000-000000000001"), current_name)
    assert resumed["ok"] is False  # 不能接续自己
    # 另一个独立会话可以接续它
    prompt(service, "pi", "99999999-0000-0000-0000-000000000002", work2, "接着独立会话继续做迁移")
    resumed = service.resume(sid("pi", "99999999-0000-0000-0000-000000000002"), current_name)
    assert resumed["ok"] is True
    assert "写 dry-run 报告" in resumed["handoff"]


def test_timeout_fallback_marks_interrupted(env):
    service, vault, work = env
    session = "77777777-0000-0000-0000-000000000001"
    prompt(service, "claude-code", session, work, "测试超时兜底")
    row = service.index.session(sid("claude-code", session))
    summary, path = _load(service, row)
    old = (datetime.now(timezone.utc) - timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
    summary.header["last_activity_at"] = old
    from tbrain import session as flow

    flow.write_session(service, summary, path)
    changed = flow.timeout_fallback(service)
    assert changed == 1
    row = service.index.session(sid("claude-code", session))
    assert _load(service, row)[0].status == "已中断"


def test_multi_host_same_records(env):
    service, vault, work = env
    session = "88888888-0000-0000-0000-000000000001"
    prompt(service, "codex", session, work, "记录一套跨宿主的经验")
    service.save(sid("codex", session), [
        {"op": "append", "section": "done", "items": [{"text": "写完了跨宿主测试"}]}])
    # 另一个宿主用同一 cwd + 原命令（新 ID）应复用同一总结
    other = "88888888-0000-0000-0000-000000000009"
    prompt(service, "pi", other, work, "记录一套跨宿主的经验")
    reused = service.index.session(sid("codex", session))
    assert reused is not None
    links = service.index.links_for_session(reused["session_id"])
    assert any(link["host"] == "pi" for link in links)
    result = service.search("跨宿主", sid("pi", other))
    assert result["results"]


def test_project_prompt_through_save(env):
    service, vault, work = env
    service.store.save_project("Tbrain", [str(work)])
    session = "99999999-0000-0000-0000-000000000001"
    prompt(service, "claude-code", session, work, "写项目提示词")
    result = service.save(sid("claude-code", session), [{
        "op": "project_prompt",
        "sections": {
            "最终目标": "完成 Tbrain 0.6.0 最小闭环",
            "硬规则": "- 所有测试用临时目录（来源：U1）",
            "当前阶段": "第 4 步：实现流程层",
            "关键路径和命令": "- 测试命令：.venv\\Scripts\\python.exe -m pytest -q",
        },
    }])
    assert result["ok"] is True
    assert result["written"]["project_prompt"]
    stored = service.store.project_prompt("Tbrain")
    assert "完成 Tbrain 0.6.0 最小闭环" in stored["sections"]["最终目标"]


def _load(service, row):
    text = service.store.read_text(service.vault_root() / row["path"])
    return handoff.parse(text), service.vault_root() / row["path"]
