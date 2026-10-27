"""第 5 步：调研报告第 2 节的 8 条验收场景（伪造宿主 hook 输入，不调用大模型）。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from tbrain import handoff


def _summary_of(brain, session_id):
    brain.service.refresh_index()
    row = brain.row(session_id)
    path = brain.vault / row["path"]
    return handoff.parse(path.read_text(encoding="utf-8")), path


# 1 -------------------------------------------------------------------------
def test_scenario1_thin_record_then_interrupt_can_recover_requirements(brain):
    claude = brain.host("claude-code")
    work = brain.workdir("proj")
    task = "任务要求：实现 Tbrain 的记忆闭环，把会话总结写成 Markdown，证据要带文件行号。"
    claude.first_message("sc1-session-1", work, task)
    claude.end("sc1-session-1", work)
    summary, path = _summary_of(brain, "claude-code:sc1-session-1")
    assert summary.status == "已中断"
    assert summary.raw_command == task
    assert task in path.read_text(encoding="utf-8")

    # 新会话启动包能看到被中断的会话，任务要求仍可从总结读回
    context = claude.first_message("sc1-session-2", work, "继续刚才那个任务")
    name = brain.name_of("claude-code:sc1-session-1")
    assert name in context
    detail = brain.service.session_detail("claude-code:sc1-session-1")
    assert task in detail["markdown"]


# 2 -------------------------------------------------------------------------
def test_scenario2_new_requirement_visible_with_old_progress(brain):
    claude = brain.host("claude-code")
    work = brain.workdir("proj2")
    claude.first_message("sc2-session-1", work, "任务要求：先做存储层，再写验收测试。")
    brain.service.save("claude-code:sc2-session-1", [
        {"op": "append", "section": "done", "items": [{"text": "存储层骨架完成"}]},
    ])
    claude.message("sc2-session-1", work, "新要求：所有文档改成中文，测试必须用临时目录。")
    brain.service.save("claude-code:sc2-session-1", [
        {"op": "replace", "section": "next_step", "text": "补中文文档并跑测试验证"},
    ])
    claude.end("sc2-session-1", work)

    codex = brain.host("codex")
    codex.first_message("sc2-session-2", work, "继续上次的任务")
    name = brain.name_of("claude-code:sc2-session-1")
    resumed = brain.service.resume("codex:sc2-session-2", name)
    assert resumed["ok"] is True
    handoff_text = resumed["handoff"]
    assert "存储层骨架完成" in handoff_text
    assert "新要求：所有文档改成中文" in handoff_text
    assert "任务要求：先做存储层" in handoff_text


# 3 -------------------------------------------------------------------------
def test_scenario3_after_stage_save_resume_has_done_evidence_next(brain):
    claude = brain.host("claude-code")
    work = brain.workdir("proj3")
    claude.first_message("sc3-session-1", work, "实现 Tbrain 的 handoff/v1 文档模型")
    brain.service.save("claude-code:sc3-session-1", [
        {"op": "append", "section": "evidence", "items": [{"kind": "file", "text": "src/tbrain/handoff.py:1"}]},
        {"op": "append", "section": "done", "items": [{"text": "解析与渲染函数完成 [证据:E1]"}]},
        {"op": "replace", "section": "next_step", "text": "跑 handoff 测试并验证中文编号"},
    ])
    claude.end("sc3-session-1", work)
    codex = brain.host("codex")
    codex.first_message("sc3-session-2", work, "继续 handoff 的工作")
    name = brain.name_of("claude-code:sc3-session-1")
    resumed = brain.service.resume("codex:sc3-session-2", name)
    text = resumed["handoff"]
    assert "解析与渲染函数完成" in text
    assert "src/tbrain/handoff.py:1" in text
    assert "跑 handoff 测试并验证中文编号" in text


# 4 -------------------------------------------------------------------------
WORKER = r"""
import sys
import time
from pathlib import Path
from tbrain.service import TbrainService
from tbrain import session as flow

home, vault, cwd, session = sys.argv[1:5]
service = TbrainService(Path(home), vault=Path(vault))
next_sequence = flow.next_sequence
def simultaneous_sequence(*args):
    sequence = next_sequence(*args)
    (Path(home) / (session + ".ready")).touch()
    deadline = time.monotonic() + 15
    while len(list(Path(home).glob("*.ready"))) < 2:
        if time.monotonic() >= deadline:
            raise TimeoutError("另一会话没有到达命名位置")
        time.sleep(0.01)
    return sequence
flow.next_sequence = simultaneous_sequence
service.hook("codex", {"hook_event_name": "UserPromptSubmit", "session_id": session,
                       "cwd": cwd, "prompt": "并发写入验收。同一项目两个会话同时保存 " + session})
for index in range(12):
    result = service.save("codex:" + session, [
        {"op": "append", "section": "done", "items": [{"text": f"并发项 {index} by {session}"}]},
    ])
    if not result.get("ok"):
        print("SAVE-FAILED", result)
        sys.exit(2)
print("OK")
"""


def test_scenario4_concurrent_sessions_do_not_overwrite(brain):
    home = brain.service.home
    vault = brain.vault
    work = brain.workdir("concurrent")
    brain.service.configure_project("Tbrain", [str(work)])
    env = dict(os.environ, TBRAIN_HOME=str(home), PYTHONIOENCODING="utf-8")
    processes = [
        subprocess.Popen([sys.executable, "-c", WORKER, str(home), str(vault), str(work), session],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        for session in ("worker-a-0001", "worker-b-0002")
    ]
    outputs = [process.communicate(timeout=180) for process in processes]
    for process, (stdout, stderr) in zip(processes, outputs):
        assert process.returncode == 0, (stdout, stderr)
        assert "OK" in stdout

    brain.service.reset_vault()
    brain.service.settings.update({"vault_path": str(vault), "ntp_enabled": False})
    brain.service.refresh_index()
    rows = [brain.service.index.session("codex:" + session) for session in ("worker-a-0001", "worker-b-0002")]
    assert len({row["path"] for row in rows}) == 2
    assert len(list((vault / "会话总结").rglob("*.md"))) == 2
    markdown_index = brain.service.store.project_index_path('Tbrain').read_text(encoding='utf-8')
    assert all(row['name'] in markdown_index for row in rows)
    for session in ("worker-a-0001", "worker-b-0002"):
        summary, _ = _summary_of(brain, "codex:" + session)
        ours = [item for item in summary.items("已完成") if session in item["text"]]
        assert len(ours) == 12
    con = sqlite3.connect(home / "index.sqlite3")
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    con.close()
    results = brain.service.search("并发写入验收", "codex:worker-a-0001")
    assert results["ok"] and results["results"]


# 5 -------------------------------------------------------------------------
def test_scenario5_independent_session_can_resume(brain):
    claude = brain.host("claude-code")
    free = brain.workdir("scratch")
    claude.first_message("sc5-session-1", free, "临时调研：比较几个记忆系统的交接结构")
    brain.service.save("claude-code:sc5-session-1", [
        {"op": "append", "section": "done", "items": [{"text": "比较完 Handoff Debt 与 ACE"}]},
        {"op": "replace", "section": "next_step", "text": "整理成报告并核对引用"},
    ])
    claude.end("sc5-session-1", free)
    pi = brain.host("pi")
    pi.first_message("sc5-session-2", free, "接着临时调研继续")
    name = brain.name_of("claude-code:sc5-session-1")
    resumed = brain.service.resume("pi:sc5-session-2", name)
    assert resumed["ok"] is True
    assert "比较完 Handoff Debt 与 ACE" in resumed["handoff"]
    summary, _ = _summary_of(brain, "pi:sc5-session-2")
    assert summary.project == ""  # 独立会话


# 6 -------------------------------------------------------------------------
def test_scenario6_without_maintenance_round_basic_io_still_works(brain):
    assert not hasattr(brain.service.settings.load(), "maintenance_enabled")
    claude = brain.host("claude-code")
    work = brain.workdir("nodep")
    claude.first_message("sc6-session-1", work, "维护轮关闭时，基础读写照常工作")
    saved = brain.service.save("claude-code:sc6-session-1", [
        {"op": "append", "section": "done", "items": [{"text": "写入一条完成项"}]},
    ])
    assert saved["ok"] is True
    found = brain.service.search("基础读写", "claude-code:sc6-session-1")
    assert found["ok"] and found["results"]
    summary, path = _summary_of(brain, "claude-code:sc6-session-1")
    assert summary.items("已完成")[0]["text"] == "写入一条完成项"
    assert "## 已完成" in path.read_text(encoding="utf-8")


# 7 -------------------------------------------------------------------------
def test_scenario7_user_edited_content_is_not_overwritten(brain):
    claude = brain.host("claude-code")
    work = brain.workdir("edit")
    claude.first_message("sc7-session-1", work, "用户手改保护验收")
    brain.service.save("claude-code:sc7-session-1", [
        {"op": "replace", "section": "target", "text": "程序写的目标"},
    ])
    summary, path = _summary_of(brain, "claude-code:sc7-session-1")
    text = path.read_text(encoding="utf-8").replace("程序写的目标", "用户改成自己的目标")
    path.write_text(text, encoding="utf-8")
    result = brain.service.save("claude-code:sc7-session-1", [
        {"op": "replace", "section": "target", "text": "模型自动写回的目标"},
    ])
    assert result["ok"] is False
    assert result["user_modified"] is True
    summary, _ = _summary_of(brain, "claude-code:sc7-session-1")
    assert summary.target == "用户改成自己的目标"
    assert summary.conflicts


# 8 -------------------------------------------------------------------------
def test_scenario8_other_agent_reads_same_records(brain):
    claude = brain.host("claude-code")
    project_work = brain.workdir("shared")
    brain.service.store.save_project("Tbrain", [str(project_work)])
    claude.first_message("sc8-session-1", project_work, "实现跨 agent 的记忆接续")
    brain.service.save("claude-code:sc8-session-1", [
        {"op": "append", "section": "done", "items": [{"text": "中文检索召回验证通过"}]},
        {"op": "replace", "section": "next_step", "text": "换一个 agent 读同一套记录验证"},
    ])
    codex = brain.host("codex")
    context = codex.first_message("sc8-session-2", project_work, "继续把跨 agent 接续做完")
    assert "中文检索召回验证通过" not in context  # 启动包只给索引行，不给全文
    name = brain.name_of("claude-code:sc8-session-1")
    assert name in context
    resumed = brain.service.resume("codex:sc8-session-2", name)
    assert resumed["ok"] is True
    assert "中文检索召回验证通过" in resumed["handoff"]
    found = brain.service.search("中文检索召回", "codex:sc8-session-2")
    assert found["results"]
    assert any(item.get("project") == "Tbrain" for item in found["results"])
