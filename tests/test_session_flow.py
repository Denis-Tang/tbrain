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
        {"op": "append", "section": "evidence", "items": [{"kind": "commit", "text": "4c8fa05"}]},
        {"op": "append", "section": "done", "items": [{"text": "合格条目 [证据:E1]"}, {"text": "x" * 260}]},
        {"op": "append", "section": "dead_end", "items": [{"attempt": "直接删除旧索引", "reason_kind": "未知",
                                                           "conditions": "Windows 10"}]},
        {"op": "replace", "section": "next_step", "text": "跑 pytest -q 验证"},
        {"op": "set_status", "status": "已中断"},
        {"op": "append", "section": "decision", "items": [{"text": "只增不删 | 用户修改优先"}]},
    ])
    assert result["ok"] is False
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
    bad = [
        {"op": "append", "section": "done", "items": [{"text": "有效条目"}]},
        {"op": "append", "section": "decision", "items": [{"text": "缺少理由"}]},
    ]
    for _ in range(3):
        result = service.save(sid("claude-code", session), bad)
    assert not result["ok"] and result["rejected"]
    summary, _ = _load(service, service.index.session(sid("claude-code", session)))
    assert summary.pending
    assert any("未校验" in item for item in summary.pending)
    assert len(summary.pending) == 1 and "缺少理由" in summary.pending[0]
    assert "有效条目" not in summary.pending[0]
    again = service.save(sid("claude-code", session), bad)
    assert not again["ok"] and again["rejected"] and not again["pending"]
    assert service.hook("claude-code", {"hook_event_name": "Stop", "session_id": session,
                                       "cwd": str(work)})["decision"] == "block"


def test_rejected_save_keeps_stop_reminder(env):
    service, _, work = env
    session = "rejected-save"
    prompt(service, "claude-code", session, work, "保存失败后仍需提醒")
    stop = {"hook_event_name": "Stop", "session_id": session, "cwd": str(work)}
    assert service.hook("claude-code", stop)["decision"] == "block"
    result = service.save(sid("claude-code", session), [
        {"op": "append", "section": "decision", "items": [{"text": "无理由"}]}])
    assert not result["ok"] and result["rejected"]
    assert service.hook("claude-code", stop)["decision"] == "block"


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
    again = service.save(sid("claude-code", session), [
        {"op": "replace", "section": "target", "text": "再次覆盖"},
        {"op": "replace", "section": "next_step", "text": "跑 pytest 验证手改目标仍在"},
    ])
    assert again["rejected"]
    summary, _ = _load(service, service.index.session(sid("claude-code", session)))
    assert summary.target == "用户手写的目标"
    assert summary.next_step == "跑 pytest 验证手改目标仍在"


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
    assert result["session_id"] == sid("codex", new_session)
    assert result["resumed_session_id"] == sid("claude-code", old_session)
    from tbrain import session as flow
    old_summary, old_path = flow.load_session(service, sid("claude-code", old_session))
    old_summary.extra["旧总结原文（迁移）"] = "旧版下一步：验证任务接续"
    flow.write_session(service, old_summary, old_path)
    before = old_path.read_bytes()
    result = service.resume(sid("codex", new_session), old_name)
    assert "旧版下一步：验证任务接续" in result["handoff"]
    service.save(result["session_id"], [
        {"op": "append", "section": "done", "items": [{"text": "新会话工作"}]}])
    assert old_path.read_bytes() == before


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


@pytest.mark.parametrize("other_host", ["codex", "pi"])
def test_new_session_and_fork_keep_separate_records(env, other_host):
    service, vault, work = env
    session = "88888888-0000-0000-0000-000000000001"
    prompt(service, "codex", session, work, "记录一套跨宿主的经验")
    service.save(sid("codex", session), [
        {"op": "append", "section": "done", "items": [{"text": "写完了跨宿主测试"}]}])
    source_row = service.index.session(sid("codex", session))
    _, source_path = _load(service, source_row)
    original = source_path.read_bytes()
    other = "88888888-0000-0000-0000-000000000009"
    # 旧版本曾猜出的别名也不能把新会话导回父总结。
    service.index.register_link(other_host, other, sid("codex", session), str(work))
    service.hook(other_host, {"hook_event_name": "SessionStart", "source": "fork",
                              "session_id": other, "cwd": str(work)})
    prompt(service, other_host, other, work, "记录一套跨宿主的经验")
    current = service.index.session(sid(other_host, other))
    assert current is not None
    assert current["path"] != source_row["path"]
    result = service.search("跨宿主", sid(other_host, other))
    assert result["results"]
    result = service.save(sid(other_host, other), [
        {"op": "append", "section": "done", "items": [{"text": "新会话或分支只保存自己的进度"}]}])
    assert result["ok"]
    assert result["session"]["session_id"] == sid(other_host, other)
    assert source_path.read_bytes() == original
    resumed = service.resume(sid(other_host, other), sid("codex", session))
    assert resumed["session_id"] == sid(other_host, other)
    assert "写完了跨宿主测试" in resumed["handoff"]
    assert source_path.read_bytes() == original


def test_long_startup_prompt_keeps_identity_and_time(env):
    service, _, work = env
    service.save_global_prompt("长提示词" * 4000)
    result = service.bootstrap("claude-code:long-startup", "claude-code", str(work))
    assert "session_id: claude-code:long-startup" in result["package"]
    assert "## 当前时间" in result["package"]
    assert len(result["package"]) <= 9000


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
    for host in ("claude-code", "codex", "dsh", "pi"):
        package = service.bootstrap(f"{host}:prompt-reader", host, str(work))["package"]
        assert "同次任务" in package and "无变化不改写" in package
        assert stored["sections"]["当前阶段"] in package
    original = service.store.project_prompt_path("Tbrain").read_bytes()
    service.save(sid("claude-code", session), [{"op": "replace", "section": "next_step", "text": "跑相关测试"}])
    assert service.store.project_prompt_path("Tbrain").read_bytes() == original
    assert "完成 Tbrain 0.6.0 最小闭环" in stored["sections"]["最终目标"]


def _load(service, row):
    text = service.store.read_text(service.vault_root() / row["path"])
    return handoff.parse(text), service.vault_root() / row["path"]


def test_pre_compact_output_is_valid_for_claude_code(env):
    """Claude Code 只接受部分事件的 hookSpecificOutput；PreCompact 不能返回它。"""
    service, vault, work = env
    session = "dddddddd-1111-2222-3333-444444444444"
    prompt(service, "claude-code", session, work, "压缩后要能想起这条约束：测试只用临时目录")
    pre = service.hook("claude-code", {"hook_event_name": "PreCompact", "session_id": session,
                                       "cwd": str(work), "source": "auto"})
    assert pre == {}
    assert service.hook("claude-code", {"hook_event_name": "SessionEnd", "session_id": session,
                                        "cwd": str(work)}) == {}


def test_session_start_after_compact_replays_recent_user_instructions(env):
    service, vault, work = env
    session = "eeeeeeee-1111-2222-3333-444444444444"
    prompt(service, "claude-code", session, work, "把启动包裁到宿主上限")
    prompt(service, "claude-code", session, work, "另外保留压缩前的用户指令")
    out = service.hook("claude-code", {"hook_event_name": "SessionStart", "session_id": session,
                                       "cwd": str(work), "source": "compact"})
    context = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "最近 5 条用户指令" in context
    assert "另外保留压缩前的用户指令" in context


def test_restore_keeps_repeated_instructions_transcript_and_indexes(env):
    service, vault, work = env
    service.configure_project('项目', [str(work)])
    service.hook('codex', {'hook_event_name': 'UserPromptSubmit', 'session_id': 'trace',
                          'cwd': str(work), 'prompt': '实现恢复要求', 'transcript_path': 'C:/sessions/source.jsonl'})
    for _ in range(2):
        prompt(service, 'codex', 'trace', work, '只读检查，不改源码')
    service.save('codex:trace', [{'op': 'replace', 'section': 'next_step', 'text': '检查接口返回值'}])
    summary, path = _load(service, service.index.session('codex:trace'))
    assert len(summary.users) == 3 and summary.header['host_record'] == 'C:/sessions/source.jsonl'
    context = service.bootstrap('codex:trace', 'codex', str(work))['package']
    assert '只读检查，不改源码' in context and '检查接口返回值' in context
    index = service.store.project_index_path('项目').read_text(encoding='utf-8')
    assert summary.name in index and '检查接口返回值' in index
    path.write_text(path.read_text(encoding='utf-8').replace('检查接口返回值', '用户改写的下一步'), encoding='utf-8')
    service.refresh_index()
    assert '用户改写的下一步' in service.store.project_index_path('项目').read_text(encoding='utf-8')


def test_parent_rules_prompt_conflicts_and_source_numbers(env):
    service, _, work = env
    service.configure_project('父项目', [str(work)])
    service.configure_project('子项目', [str(work / 'child')], parent='父项目')
    prompt(service, 'codex', 'rules', work, '所有测试用临时目录')
    source = service.index.session('codex:rules')['name']
    result = service.save('codex:rules', [{'op': 'project_prompt', 'sections': {
        '最终目标': '程序目标', '硬规则': f'- 所有测试用临时目录（来源：[[{source}]]#U1）'}}])
    assert not result['rejected']
    child = service.bootstrap('codex:child-rules', 'codex', str(work / 'child'))
    assert '所有测试用临时目录' in child['package']
    result = service.save('codex:rules', [{'op': 'project_prompt', 'sections': {
        '硬规则': f'- 无来源规则（来源：[[{source}]]#U999）'}}])
    assert result['rejected'] and not result['written']['project_prompt']
    path = service.store.project_prompt_path('父项目')
    path.write_text(path.read_text(encoding='utf-8').replace('程序目标', '用户手改的目标'), encoding='utf-8')
    for _ in range(2):
        result = service.save('codex:rules', [{'op': 'project_prompt', 'sections': {
            '最终目标': '模型想覆盖的目标', '当前阶段': '修复阶段：通过回归'}}])
        assert result['written']['project_prompt']['当前阶段'] == '修复阶段：通过回归'
        assert result['rejected']
    restarted = TbrainService(service.home)
    context = restarted.bootstrap('codex:rules-new', 'codex', str(work))['package']
    assert '待用户确认' in context and '模型想覆盖的目标' in context
    assert '用户手改的目标' in context


def test_evidence_is_typed_exact_and_dead_end_requires_existing_reference(env):
    service, _, work = env
    prompt(service, 'codex', 'evidence', work, '记录证据')
    file = work / 'sample.py'
    file.write_text('a\nb\n', encoding='utf-8')
    result = service.save('codex:evidence', [{'op': 'append', 'section': 'evidence', 'items': [
        {'kind': 'file', 'text': 'sample.py:1'}, {'kind': 'file', 'text': 'sample.py:2'},
        {'kind': 'file', 'text': 'sample.py:1'}, {'text': '任意文字'}]},
        {'op': 'append', 'section': 'dead_end', 'items': [{'attempt': '运行测试', 'conditions': 'Python',
         'reason_kind': '已证实不可行', 'strength': '已排除', 'evidence': 'E999'}]}])
    assert result['written']['evidence'] == ['E1', 'E2', 'E1']
    assert len(result['rejected']) == 2 and not result['written']['dead_ends']


def test_timeout_on_search_bootstrap_and_independent_cwd_priority(env):
    service, _, work = env
    prompt(service, 'codex', 'local', work, '苹果项目调研')
    prompt(service, 'codex', 'foreign', work / 'other', '香蕉接口检查')
    from tbrain import session as flow
    summary, path = flow.load_session(service, 'codex:foreign')
    summary.header['last_activity_at'] = '2020-01-01T00:00:00Z'
    flow.write_session(service, summary, path)
    service.search('香蕉')
    assert flow.load_session(service, 'codex:foreign')[0].status == '已中断'
    service.bootstrap('codex:new-local', 'codex', str(work))
    rows = flow._resume_rows(service, flow.load_session(service, 'codex:new-local')[0], '', 5)
    assert rows[0]['session_id'] == 'codex:local'
    service.bootstrap('codex:foreign', 'codex', str(work / 'other'))
    assert flow.load_session(service, 'codex:foreign')[0].status == '进行中'


def test_historical_fork_inherits_saved_point_and_keeps_parent_unchanged(env):
    service, _, work = env
    prompt(service, 'codex', 'parent', work, '实现方案')
    saved = service.save('codex:parent', [{'op': 'append', 'section': 'done', 'items': [{'text': '第一轮完成'}]}])
    first_revision = saved['revision']
    prompt(service, 'codex', 'parent', work, '第二轮继续方案A')
    service.save('codex:parent', [{'op': 'append', 'section': 'done', 'items': [{'text': '后来的方案A完成'}]}])
    _, path = _load(service, service.index.session('codex:parent'))
    original = path.read_bytes()
    prompt(service, 'codex', 'fork', work, '从第一轮改做方案B')
    result = service.resume('codex:fork', 'codex:parent', fork_revision=first_revision)
    assert result['ok'] and '第一轮完成' in result['handoff'] and '后来的方案A完成' not in result['handoff']
    summary, _ = _load(service, service.index.session('codex:fork'))
    assert summary.header['branch_user_count'] == 1
    assert summary.raw_command == '从第一轮改做方案B'
    assert [user['text'] for user in summary.users] == ['实现方案', '从第一轮改做方案B']
    assert path.read_bytes() == original
    result = service.resume('codex:fork', 'codex:parent', fork_user_count=99)
    assert not result['ok'] and path.read_bytes() == original
    saved_branch, saved_path = _load(service, service.index.session('codex:fork'))
    branch_bytes = saved_path.read_bytes()
    assert not service.resume('codex:fork', 'codex:parent', fork_revision=first_revision)['ok']
    assert saved_path.read_bytes() == branch_bytes
    result = service.hook('codex', {'hook_event_name': 'SessionStart', 'source': 'fork', 'session_id': 'auto-fork',
        'cwd': str(work), 'parent_session_id': 'codex:parent', 'fork_user_count': 1})
    assert '第一轮完成' in result['hookSpecificOutput']['additionalContext']
    prompt(service, 'codex', 'auto-fork', work, '分支第一条新要求')
    branch, _ = _load(service, service.index.session('codex:auto-fork'))
    assert len(branch.users) == 2 and branch.header['branch_user_count'] == 1
    assert path.read_bytes() == original


def test_dead_end_becomes_retryable_after_dependency_change(env):
    service, _, work = env
    service.store.save_public_card('工具', '示例依赖', {'version': '1'})
    prompt(service, 'codex', 'dead', work, '检查依赖限制')
    result = service.save('codex:dead', [{'op': 'append', 'section': 'dead_end', 'items': [
        {'attempt': '运行旧接口', 'conditions': '示例依赖 1', 'reason_kind': '环境/权限限制', 'strength': '暂缓'}]}])
    assert result['written']['dead_ends'] == ['X1']
    service.store.save_public_card('工具', '示例依赖', {'version': '2'})
    prompt(service, 'codex', 'reader', work, '接续依赖检查')
    result = service.resume('codex:reader', 'codex:dead')
    assert '可重试' in result['handoff']


def test_save_validates_final_references_and_decision_length(env):
    service, _, work = env
    prompt(service, 'codex', 'references', work, '核对证据与决策')
    result = service.save('codex:references', [
        {'op': 'append', 'section': 'evidence', 'items': [{'kind': 'commit', 'text': '4c8fa05'}]},
        {'op': 'append', 'section': 'done', 'items': [
            {'text': '不存在的证据 [证据:E999]'}, {'text': 'x' * 195, 'evidence': 'E1'},
            {'text': '引用不完整 [证据:'}, {'text': '引用有效', 'evidence': 'E1'}]},
        {'op': 'append', 'section': 'decision', 'items': [
            {'decision': '使用 Markdown', 'reason': '保留唯一事实源'},
            {'decision': 'x' * 100, 'reason': 'y' * 100}]},
    ])
    assert not result['ok'] and len(result['rejected']) == 4
    summary, _ = _load(service, service.index.session('codex:references'))
    assert [item['text'] for item in summary.items('已完成')] == ['引用有效 [证据:E1]']
    assert [item['text'] for item in summary.items('决策')] == ['使用 Markdown | 保留唯一事实源']


def test_tool_result_requires_new_save_and_cannot_be_overridden(env):
    service, _, work = env
    prompt(service, 'codex', 'facts', work, '运行测试')
    service.save('codex:facts', [{'op': 'replace', 'section': 'next_step', 'text': '运行测试并核对'}])
    service.hook('codex', {'hook_event_name': 'ToolResult', 'session_id': 'facts', 'cwd': str(work),
                          'last_test': {'command': 'pytest -q', 'exit_code': 1}})
    stop = {'hook_event_name': 'Stop', 'session_id': 'facts', 'cwd': str(work)}
    assert service.hook('codex', stop)['decision'] == 'block'
    result = service.save('codex:facts', [{'op': 'replace', 'section': 'next_step', 'text': '修复失败用例'}],
                          last_test={'command': 'pytest -q', 'exit_code': 0})
    summary, _ = _load(service, service.index.session('codex:facts'))
    assert summary.header['last_verify']['exit_code'] == 1
    assert not result['ok'] and result['rejected']
    assert service.hook('codex', stop)['decision'] == 'block'
    service.save('codex:facts', [{'op': 'replace', 'section': 'next_step', 'text': '修复失败用例'}])
    assert service.hook('codex', stop) == {}


def test_model_test_report_validates_boundary_and_remains_updatable(env):
    service, _, work = env
    prompt(service, 'codex', 'reports', work, '补报测试')
    operations = [{'op': 'replace', 'section': 'next_step', 'text': '核对测试'}]
    bad = service.save('codex:reports', operations, last_test={'command': 'pytest', 'exit_code': '0'})
    assert not bad['ok'] and bad['rejected']
    for code in (1, 0):
        result = service.save('codex:reports', operations, last_test={'command': 'pytest', 'exit_code': code})
        summary, _ = _load(service, service.index.session('codex:reports'))
        assert result['ok'] and summary.header['last_verify']['exit_code'] == code


def test_completion_requires_explicit_goal_completion(env):
    service, _, work = env
    prompt(service, 'codex', 'goals', work, '完成目标')
    result = service.save('codex:goals', [
        {'op': 'append', 'section': 'goal', 'items': [{'text': '跑通回归'}]},
        {'op': 'set_status', 'status': '已完成'},
    ])
    summary, _ = _load(service, service.index.session('codex:goals'))
    assert not result['ok'] and summary.status == '进行中' and handoff.has_open_goals(summary)
    result = service.save('codex:goals', [{'op': 'set_status', 'status': '已完成', 'complete_goals': ['G1']}])
    summary, _ = _load(service, service.index.session('codex:goals'))
    assert result['ok'] and summary.status == '已完成' and not handoff.has_open_goals(summary)
    result = service.save('codex:goals', [{'op': 'append', 'section': 'goal', 'items': [{'text': '新增验收要求'}]}])
    summary, _ = _load(service, service.index.session('codex:goals'))
    assert result['ok'] and summary.status == '进行中' and handoff.has_open_goals(summary)


def test_search_returns_matching_numbered_memory(env):
    service, _, work = env
    prompt(service, 'codex', 'snippet', work, '修复中文索引')
    service.save('codex:snippet', [{'op': 'append', 'section': 'decision', 'items': [
        {'text': '保留中文分词 | 中文索引需要支持二字查询'}]}])
    result = service.search('二字查询', 'codex:snippet')
    hit = next(item for item in result['results'] if item.get('session_id') == 'codex:snippet')
    assert 'K1 保留中文分词' in hit['snippet'] and '二字查询' in hit['snippet']
    assert len(hit['snippet']) <= 500
