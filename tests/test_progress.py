"""Goal clarification, no-progress pause, and the resumable L0 handoff."""

from __future__ import annotations

import io
import json
from pathlib import Path

import yaml

from tbrain import cli, handoff
from tbrain.settings import SettingsStore
from tbrain.check import (
    handoff_personal_path,
    paused_without_handoff,
    run_check,
    unresolved_marked_pass,
)
from tbrain.service import TbrainService


def _service(tmp_path, monkeypatch):
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    monkeypatch.setenv("TBRAIN_HOME", str(home))
    service = TbrainService(home, vault=vault)
    service.settings.update({"vault_path": str(vault), "ntp_enabled": False, "timezone": "Asia/Shanghai", "task_mode": True})
    return service, home, vault


def _prompt(service, session, cwd, text, host="codex"):
    return service.hook(host, {"hook_event_name": "UserPromptSubmit", "session_id": session,
                               "cwd": str(cwd), "prompt": text})


def _load(service, session_id):
    from tbrain import session as flow

    return flow.load_session(service, session_id)


def _criteria(vault, project):
    return vault / "项目" / project / "完成标准.md"


def _clear(vault, project):
    path = _criteria(vault, project)
    text = path.read_text(encoding="utf-8")
    text = text.replace("（待填写：这个项目要做成什么样）", "已有目标：把本地检查做成每天的门禁")
    text = text.replace("- [ ] （待填写：可以核对的完成条件）", "- [ ] tbrain check 退出码为 0")
    path.write_text(text, encoding="utf-8")
    return path


def _attempt(text, productive=False):
    return {"op": "attempt", "route": text, "evidence": text, "productive": productive}


def _confirm(question, **extra):
    payload = {"op": "confirm", "question": question, "why": "这是要交给人用的结果",
               "tradeoffs": "先不扩大范围", "decided_by": "用户"}
    payload.update(extra)
    return payload


def _understanding(**extra):
    return {"op": "understanding", "goal": "把错误提示改成中文", "scope": "解析器错误提示",
            "deliverables": "修改后的代码", "done": "原有功能不变，错误提示使用中文", **extra}


def test_first_message_requires_task_understanding_even_with_project_memory(tmp_path, monkeypatch):
    service, _, vault = _service(tmp_path, monkeypatch)
    folder = vault.parent / "clear"
    service.configure_project("清楚项目", [str(folder)])
    path = _clear(vault, "清楚项目")
    before = path.read_bytes()
    package = service.bootstrap("codex:one", "codex", str(folder))["package"]
    assert "## 待确认的任务理解" not in package
    package = json.dumps(_prompt(service, "one", folder, "把解析器错误改成中文"), ensure_ascii=False)
    assert "待确认的任务理解" in package
    assert all(label in package for label in ("任务目标", "本次范围", "交付结果", "完成标准"))
    assert "不是具体技术方案" in package
    assert "原生确认弹窗" in package and "不猜确认意图" in package
    assert path.read_bytes() == before
    blocked = service.save("codex:one", [
        {"op": "append", "section": "done", "items": [{"text": "改完了"}]},
        {"op": "set_status", "status": "已完成"},
        {"op": "project_prompt", "sections": {"最终目标": "擅自执行"}},
    ])
    assert not blocked["ok"] and len(blocked["rejected"]) == 3
    assert service.save("codex:one", [_understanding()])["ok"]
    assert not service.save("codex:one", [_confirm("Q1", source="U1")])["ok"]
    _prompt(service, "one", folder, "确认")
    assert service.save("codex:one", [_confirm("Q1", source="U2")])["ok"]
    later = _prompt(service, "one", folder, "继续完成任务")
    assert "待确认的任务理解" not in json.dumps(later, ensure_ascii=False)
    assert "待确认的任务理解" not in service.bootstrap("codex:one", "codex", str(folder))["package"]
    assert service.save("codex:one", [{"op": "set_status", "status": "已完成"}])["ok"]
    assert path.read_bytes() == before
    other = _prompt(service, "two", folder, "继续同一件事")
    assert "待确认的任务理解" in json.dumps(other, ensure_ascii=False)


def test_user_correction_reopens_only_that_sessions_understanding(tmp_path, monkeypatch):
    service, _, vault = _service(tmp_path, monkeypatch)
    folder = vault.parent / "independent"
    folder.mkdir()
    _prompt(service, "one", folder, "把解析器错误改成中文")
    assert service.save("codex:one", [_understanding()])["ok"]
    _prompt(service, "one", folder, "你的理解不对，需要修改所有错误提示")
    assert not service.save("codex:one", [_confirm("Q1", source="U2")])["ok"]
    assert service.save("codex:one", [_understanding(scope="所有错误提示")])["ok"]
    _prompt(service, "one", folder, "确认")
    assert service.save("codex:one", [_confirm("Q1", source="U3")])["ok"]
    assert not service.save("codex:one", [_understanding()])["ok"]
    _prompt(service, "one", folder, "查看验证结果")
    compact = service.hook("codex", {"hook_event_name": "SessionStart", "source": "compact",
                                  "session_id": "one", "cwd": str(folder)})
    assert "待确认的任务理解" not in json.dumps(compact, ensure_ascii=False)
    _prompt(service, "one", folder, "你理解错了，目标应改为统一全部提示的语言")
    assert service.save("codex:one", [_understanding(goal="统一全部提示的语言", source="U5")])["ok"]
    assert not service.save("codex:one", [{"op": "set_status", "status": "已完成"}])["ok"]
    assert not service.save("codex:one", [_confirm("Q1", source="U3")])["ok"]
    _prompt(service, "one", folder, "确认")
    assert service.save("codex:one", [_confirm("Q1", source="U6")])["ok"]
    assert service.save("codex:one", [{"op": "set_status", "status": "已完成"}])["ok"]


def test_text_confirmation_requires_a_separate_exact_reply(tmp_path, monkeypatch):
    service, _, _ = _service(tmp_path, monkeypatch)
    _prompt(service, "exact", tmp_path, "把错误提示改成中文")
    assert service.save("codex:exact", [_understanding()])["ok"]
    for number, text in enumerate(("对", "可以", "开始吧", "ok", "确认。", "确认你的任务理解",
                                   "没有确认任务理解", "等我确认任务理解后再开始",
                                   "补充：保留原文", "范围改为所有错误提示，确认。"), start=2):
        _prompt(service, "exact", tmp_path, text)
        result = service.save("codex:exact", [_confirm("Q1", source=f"U{number}")])
        assert not result["ok"]
        assert "单独回复“确认”" in result["rejected"][0]["reason"]
    assert service.save("codex:exact", [_understanding(scope="所有错误提示，保留原文")])["ok"]
    stale = service.save("codex:exact", [_confirm("Q1", source="U11")])
    assert not stale["ok"] and "不要重试旧来源" in stale["rejected"][0]["reason"]
    _prompt(service, "exact", tmp_path, " 确认\n")
    assert service.save("codex:exact", [_confirm("Q1", source="U12")])["ok"]
    summary, _ = _load(service, "codex:exact")
    assert "所有错误提示，保留原文" in summary.extra["任务理解"]
    assert "确认来源：U12" in summary.extra["任务理解"]


def test_dialog_confirmation_requires_the_submitted_confirm_option(tmp_path, monkeypatch):
    service, _, _ = _service(tmp_path, monkeypatch)
    _prompt(service, "dialog", tmp_path, "把错误提示改成中文")
    assert not service.save("codex:dialog", [_confirm("Q1", dialog_answer="确认")])["ok"]
    assert service.save("codex:dialog", [_understanding()])["ok"]
    for answer in (None, False, True, "", "继续补充", "同意", "确认。"):
        result = service.save("codex:dialog", [_confirm("Q1", dialog_answer=answer)])
        assert not result["ok"] and "弹窗尚未确认" in result["rejected"][0]["reason"]
    assert service.save("codex:dialog", [_confirm("Q1", dialog_answer="确认")])["ok"]
    summary, _ = _load(service, "codex:dialog")
    assert len(summary.users) == 1
    assert "确认来源：弹窗" in summary.extra["任务理解"]
    assert summary.header["goal_questions"][0]["status"] == "confirmed"


def test_business_gaps_keep_task_confirmed_and_allow_independent_work(tmp_path, monkeypatch):
    service, _, vault = _service(tmp_path, monkeypatch)
    folder = vault.parent / "business"
    folder.mkdir()
    _prompt(service, "business", folder, "按规定的口径整理表格")
    assert service.save("codex:business", [_understanding(
        goal="按业务口径整理表格", scope="转换及导出，保留规则按用户决定")])["ok"]
    _prompt(service, "business", folder, "确认")
    assert service.save("codex:business", [_confirm("Q1", source="U2")])["ok"]
    questions = ["业务缺口：同义条目以什么条件合并？影响：合并逻辑。",
                 "业务缺口：原文与来源须保留哪些信息？影响：内容导出。"]
    assert service.save("codex:business", [{"op": "append", "section": "goal",
        "items": [{"text": text} for text in questions]}])["ok"]
    code = folder / "export.py"
    code.write_text("# export formatting is independent of the business decisions\n", encoding="utf-8")
    assert service.save("codex:business", [
        {"op": "append", "section": "evidence", "items": [{"kind": "file", "text": f"{code}:1"}]},
        {"op": "append", "section": "done", "items": [{"text": "完成独立的导出样式 [证据:E1]"}]},
    ])["ok"]
    package = service.bootstrap("codex:business", "codex", str(folder))["package"]
    assert "待确认的任务理解" not in package
    assert all(question in package for question in questions)
    assert "业务规则" in package and "不依赖" in package
    assert not service.save("codex:business", [{"op": "set_status", "status": "已完成"}])["ok"]
    summary, _ = _load(service, "codex:business")
    assert summary.header["goal_questions"][0]["status"] == "confirmed"
    assert len([goal for goal in summary.goals() if not goal.get("done")]) == 2
    _prompt(service, "business", folder, "只合并已证实同一服务的内容，原文与全部来源必须保留")
    saved = service.save("codex:business", [
        {"op": "append", "section": "decision", "items": [{
            "text": "表格合并与保留口径 | 仅合并同一服务，保留原文及全部来源；来源U3"}]},
        {"op": "set_status", "status": "进行中", "complete_goals": ["G1", "G2"]},
    ])
    assert saved["ok"]
    summary, _ = _load(service, "codex:business")
    assert summary.target == "按业务口径整理表格"
    assert summary.header["goal_questions"][0]["status"] == "confirmed"
    assert not handoff.has_open_goals(summary)
    assert "来源U3" in summary.items("决策")[-1]["text"]
    assert service.save("codex:business", [{"op": "set_status", "status": "已完成"}])["ok"]


def test_different_failures_without_new_evidence_still_pause(tmp_path, monkeypatch):
    service, _, vault = _service(tmp_path, monkeypatch)
    folder = vault.parent / "alternate"
    folder.mkdir()
    _prompt(service, "alt", folder, "连续试三条不同的失败")
    attempts = [
        "pytest -q AssertionError test_parse",
        "ruff F401 src/tbrain/naming.py:4",
        "仍然是 AssertionError test_parse",
    ]
    for text in attempts[:2]:
        assert service.save("codex:alt", [_attempt(text)])["ok"]
        assert _load(service, "codex:alt")[0].status != "已暂停"
    assert service.save("codex:alt", [_attempt(attempts[2])])["ok"]
    summary, _ = _load(service, "codex:alt")
    assert summary.status == "已暂停"
    assert summary.header["stall"]["consecutive"] == 3
    assert [item["effect"] for item in summary.header["stall"]["log"]] == ["start", "unproductive", "repeat"]

    folder2 = vault.parent / "reset"
    folder2.mkdir()
    _prompt(service, "reset", folder2, "新证据才能重新计数")
    assert service.save("codex:reset", [_attempt(attempts[0])])["ok"]
    assert service.save("codex:reset", [_attempt(attempts[1])])["ok"]
    restated = service.save("codex:reset", [{
        "op": "append", "section": "evidence",
        "items": [{"kind": "command", "text": "还是 AssertionError test_parse"}],
    }])
    assert restated["ok"] is True
    stalled, _ = _load(service, "codex:reset")
    assert stalled.status == "已暂停"
    assert stalled.header["stall"]["consecutive"] == 3
    assert "new-evidence" not in [item["effect"] for item in stalled.header["stall"]["log"]]

    folder3 = vault.parent / "fresh-evidence"
    folder3.mkdir()
    _prompt(service, "fresh", folder3, "换一条有文件证据的失败")
    assert service.save("codex:fresh", [_attempt(attempts[0])])["ok"]
    assert service.save("codex:fresh", [_attempt(attempts[1])])["ok"]
    evidence = service.save("codex:fresh", [{
        "op": "append", "section": "evidence",
        "items": [{"kind": "file", "text": "src/tbrain/parser.py:12"}],
    }])
    assert evidence["ok"] is True
    summary, _ = _load(service, "codex:fresh")
    assert summary.status != "已暂停"
    assert summary.header["stall"]["consecutive"] == 0
    assert any(item["effect"] == "new-evidence" for item in summary.header["stall"]["log"])
    assert service.save("codex:fresh", [_attempt("mypy error src/tbrain/clock.py:9", productive=True)])["ok"]
    summary, _ = _load(service, "codex:fresh")
    assert summary.status != "已暂停"
    assert summary.header["stall"]["consecutive"] == 1


def test_reworded_failure_stalls_and_new_evidence_counts(tmp_path, monkeypatch):
    service, _, vault = _service(tmp_path, monkeypatch)
    folder = vault.parent / "stall"
    folder.mkdir()
    _prompt(service, "stall", folder, "修解析器的 AssertionError")
    failures = [
        "pytest -q AssertionError test_parse",
        "还是同样的失败，AssertionError，测试 test_parse 没有过",
        "failed again: pytest AssertionError test_parse",
    ]
    for index, text in enumerate(failures, start=1):
        saved = service.save("codex:stall", [_attempt(text)])
        summary, _ = _load(service, "codex:stall")
        if index < 3:
            assert saved["ok"] is True
            assert summary.status != "已暂停"
        else:
            assert summary.status == "已暂停"
    effects = [item["effect"] for item in summary.header["stall"]["log"]]
    assert effects == ["start", "repeat", "repeat"]
    assert summary.header["stall"]["consecutive"] == 3
    assert "progress" not in effects
    assert "new-evidence" not in effects

    marked = vault.parent / "marked"
    marked.mkdir()
    _prompt(service, "marked", marked, "同一失败标成有进展也不清零")
    assert service.save("codex:marked", [_attempt(failures[0])])["ok"]
    assert service.save("codex:marked", [_attempt(failures[2], productive=True)])["ok"]
    marked_summary, _ = _load(service, "codex:marked")
    assert marked_summary.status != "已暂停"
    assert marked_summary.header["stall"]["consecutive"] == 2
    assert [item["effect"] for item in marked_summary.header["stall"]["log"]] == ["start", "repeat"]

    same = service.save("codex:stall", [_attempt(failures[0], productive=True)])
    assert same["ok"] is False
    assert _load(service, "codex:stall")[0].header["stall"]["consecutive"] == 3

    folder2 = vault.parent / "fresh"
    folder2.mkdir()
    _prompt(service, "fresh", folder2, "修解析器的另一处失败")
    assert service.save("codex:fresh", [_attempt(failures[0])])["ok"]
    assert service.save("codex:fresh", [_attempt(failures[1])])["ok"]
    evidence = service.save("codex:fresh", [{
        "op": "append", "section": "evidence",
        "items": [{"kind": "file", "text": "src/tbrain/parser.py:12"}],
    }])
    assert evidence["ok"] is True
    summary, _ = _load(service, "codex:fresh")
    assert summary.status != "已暂停"
    assert summary.header["stall"]["consecutive"] == 0
    assert any(item["effect"] == "new-evidence" for item in summary.header["stall"]["log"])
    assert service.save("codex:fresh", [_attempt("pytest -q AssertionError test_other")])["ok"]
    assert _load(service, "codex:fresh")[0].status != "已暂停"


def test_pause_saves_handoff_and_resume_keeps_it(tmp_path, monkeypatch):
    service, _, vault = _service(tmp_path, monkeypatch)
    folder = vault.parent / "handoff"
    saved = service.configure_project("交接项目", [str(folder)])
    _clear(vault, "交接项目")
    _prompt(service, "work", folder, "把热交接写进会话")
    assert service.save("codex:work", [_understanding(goal="把热交接写进会话")])["ok"]
    _prompt(service, "work", folder, "确认")
    assert service.save("codex:work", [_confirm("Q1", source="U2")])["ok"]
    assert service.save("codex:work", [
        {"op": "append", "section": "evidence", "items": [{"kind": "commit", "text": "4c8fa05"}]},
        {"op": "append", "section": "done", "items": [{"text": "先记下已完成的门禁 [证据:E1]"}]},
        {"op": "append", "section": "decision", "items": [{
            "decision": "热交接写在会话里",
            "reason": "恢复时先读 L0",
            "rationale": "不等冷蒸馏",
            "tradeoffs": "会话总结会变长",
            "decided_by": "用户",
        }]},
    ])["ok"]
    route = "pytest -q exit 1"
    assert service.save("codex:work", [_attempt(route)])["ok"]
    assert service.save("codex:work", [_attempt(route)])["ok"]
    service.hook("codex", {"hook_event_name": "ToolResult", "session_id": "work", "cwd": str(folder),
                          "last_test": {"command": "pytest -q", "exit_code": 1}})
    summary, path = _load(service, "codex:work")
    assert summary.status == "已暂停"
    handoff_text = summary.extra["热交接"]
    for phrase in ("停顿原因", "恢复条件", "### 目标", "### 已确认决定", "理由：不等冷蒸馏",
                   "决定人：用户", "### 已完成", "先记下已完成的门禁", "### 已核实", "### 未核实",
                   "pytest -q 退出码 1", "continue"):
        assert phrase in handoff_text, phrase
    assert "（没有退出码为 0 的验证）" in handoff_text
    assert "/home/" not in handoff_text
    assert "ubuntu" not in handoff_text.casefold()
    assert str(saved["worktree"]) not in handoff_text
    assert saved["worktree_convention"] in handoff_text
    assert "D1" in handoff_text or "先记下已完成的门禁" in path.read_text(encoding="utf-8")
    assert "热交接写在会话里" in path.read_text(encoding="utf-8")

    package = service.bootstrap("codex:work", "codex", str(folder))["package"]
    assert package.startswith("已暂停")
    assert "## 热交接" in package
    assert "不能把那个进程杀掉" in package
    assert summary.status != "已完成"

    stop = service.hook("codex", {"hook_event_name": "Stop", "session_id": "work", "cwd": str(folder)})
    assert stop.get("decision") != "block"
    assert "已暂停" in json.dumps(stop, ensure_ascii=False)
    wrapped = service.hook("codex", {"hook_event_name": "WrapUp", "session_id": "work", "cwd": str(folder)})
    assert wrapped["decision"] == "block"
    assert "已暂停" in wrapped["reason"]
    service.hook("codex", {"hook_event_name": "SessionEnd", "session_id": "work", "cwd": str(folder)})
    assert _load(service, "codex:work")[0].status == "已暂停"

    blocked = service.save("codex:work", [_attempt(route)])
    assert blocked["ok"] is False
    noted = service.save("codex:work", [
        {"op": "append", "section": "evidence", "items": [{"kind": "command", "text": "pytest -q\n1 failed"}]},
        {"op": "replace", "section": "next_step", "text": "改为先补表格驱动测试再改解析"},
    ])
    assert noted["ok"] is True
    assert _load(service, "codex:work")[0].status == "已暂停"
    no_source = service.save("codex:work", [{
        "op": "continue", "route": "改为先补表格驱动测试再改解析",
        "why": "再试一次", "tradeoffs": "不换路", "decided_by": "用户",
    }])
    assert no_source["ok"] is False
    assert any("source" in item["reason"] for item in no_source["rejected"])
    _prompt(service, "work", folder, "继续按建议的新路线做")
    fragment = service.save("codex:work", [{
        "op": "continue", "route": "新路线", "why": "再试一次", "tradeoffs": "不换路",
        "decided_by": "用户", "source": "U3",
    }])
    assert fragment["ok"] is False
    assert "已记下的下一步" in fragment["rejected"][0]["reason"]

    _prompt(service, "next", folder, "接手暂停的会话")
    resumed = service.resume("codex:next", "codex:work")
    assert resumed["ok"] is True
    assert resumed["handoff"].startswith("已暂停")
    assert "热交接" in resumed["handoff"]
    assert "## Dream L1" not in resumed["handoff"]
    current = _load(service, "codex:next")[0]
    assert current.status == "已暂停"
    assert current.status != "已完成"
    again = service.bootstrap("codex:next", "codex", str(folder))["package"]
    assert again.startswith("已暂停")
    assert "不能把那个进程杀掉" in again

    continued = service.save("codex:work", [{
        "op": "continue", "route": "改为先补表格驱动测试再改解析",
        "why": "同一条 pytest 失败已经重复", "tradeoffs": "先不重写解析器",
        "decided_by": "用户", "source": "U3",
    }])
    assert continued["ok"] is True
    restored, _ = _load(service, "codex:work")
    assert restored.status == "进行中"
    assert restored.header["stall"]["paused"] is False
    assert restored.header["stall"]["consecutive"] == 0
    entry = restored.header["stall"]["log"][-1]
    assert entry["effect"] == "continue"
    assert entry["cleared_consecutive"] == 3
    assert "不是静默清零" in restored.extra["热交接"]
    assert "决定人：用户" in restored.items("决策")[-1]["text"]


def test_failed_pause_write_keeps_the_previous_session(tmp_path, monkeypatch):
    service, _, vault = _service(tmp_path, monkeypatch)
    folder = vault.parent / "disk"
    folder.mkdir()
    _prompt(service, "disk", folder, "核对写入失败不假装暂停")
    assert service.save("codex:disk", [{
        "op": "append", "section": "decision",
        "items": [{"decision": "先留下原决定", "reason": "写入失败不能丢",
                   "rationale": "原子写失败要保留原文", "tradeoffs": "这次暂停不会生效", "decided_by": "用户"}],
    }])["ok"]
    assert service.save("codex:disk", [_attempt("pytest -q exit 1")])["ok"]
    assert service.save("codex:disk", [_attempt("pytest -q exit 1")])["ok"]
    _, path = _load(service, "codex:disk")
    original = path.read_bytes()

    def fail_write(*args, **kwargs):
        raise OSError("模拟磁盘写入失败")

    monkeypatch.setattr(service.store, "write_text", fail_write)
    try:
        service.save("codex:disk", [_attempt("pytest -q exit 1")])
    except OSError as exc:
        assert "模拟磁盘写入失败" in str(exc)
    else:
        raise AssertionError("写入失败时应抛出，不能假装暂停已经生效")
    assert path.read_bytes() == original
    assert "已暂停" not in path.read_text(encoding="utf-8")
    assert "先留下原决定" in path.read_text(encoding="utf-8")


def test_stall_policy_is_configurable_and_ignores_budgets(tmp_path, monkeypatch):
    service, _, vault = _service(tmp_path, monkeypatch)
    folder = vault.parent / "policy"
    folder.mkdir()
    config = folder / ".tbrain"
    config.mkdir()
    (config / "check.yaml").write_text(yaml.safe_dump({
        "version": 1,
        "checks": [],
        "lints": ["decision-missing-why"],
        "stall": {"unproductive_attempts": 2, "max_tokens": 100, "budget_usd": 1},
    }, allow_unicode=True, sort_keys=False), encoding="utf-8")
    _prompt(service, "policy", folder, "用两次相同失败就暂停")
    assert service.save("codex:policy", [_attempt("pytest -q exit 2")])["ok"]
    service.save("codex:policy", [_attempt("pytest -q exit 2")])
    summary, _ = _load(service, "codex:policy")
    assert summary.status == "已暂停"
    assert summary.header["stall"]["policy"] == 2
    assert summary.header["stall"]["consecutive"] == 2
    assert "max_tokens" not in summary.header["stall"]
    assert "budget_usd" not in summary.header["stall"]


def test_lints_catch_personal_paths_missing_handoff_and_unconfirmed_pass(tmp_path):
    root = tmp_path / "notes"
    root.mkdir()
    (root / ".tbrain").mkdir()
    (root / ".tbrain" / "check.yaml").write_text(
        "version: 1\nchecks: []\nlints:\n"
        "  - handoff-personal-path\n  - paused-without-handoff\n  - unresolved-marked-pass\n",
        encoding="utf-8",
    )
    note = root / "session.md"
    note.write_text("---\nstatus: 已暂停\n---\n\n## 热交接\n停在 /home/example/proj/src/a.py\n", encoding="utf-8")
    failed = run_check(root)
    assert failed["ok"] is False
    assert handoff_personal_path(root)
    note.write_text("---\nstatus: 已暂停\n---\n\n没有交接\n", encoding="utf-8")
    assert paused_without_handoff(root)
    note.write_text("状态：未确认\n\nPASS\n", encoding="utf-8")
    assert unresolved_marked_pass(root)
    note.write_text(
        "---\nstatus: 已暂停\n---\n\n## 热交接\n约定：../.tbrain-worktrees/app/demo\n状态：未确认\n结论还没写\n",
        encoding="utf-8",
    )
    assert run_check(root)["ok"] is True
    assert handoff.parse(note.read_text(encoding="utf-8")).status == "已暂停"


def test_cli_hook_and_tool_show_questions_then_pause(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    work = tmp_path / "app"
    work.mkdir()
    monkeypatch.setenv("TBRAIN_HOME", str(home))
    SettingsStore(home).save({"vault_path": str(vault), "ntp_enabled": False, "timezone": "Asia/Shanghai", "task_mode": True})
    assert cli.main(["project", "add", "--name", "命令项目", "--path", str(work), "--home", str(home)]) == 0
    capsys.readouterr()
    started = cli.main(["tool", "bootstrap", json.dumps({
        "session_id": "codex:cli-1", "agent": "codex", "cwd": str(work),
    }, ensure_ascii=False), "--home", str(home)])
    boot = json.loads(capsys.readouterr().out)
    assert started == 0, boot
    assert "这个项目要做成什么样" in boot["package"]
    assert boot["session"]["status"] != "已完成"
    for _ in range(2):
        code = cli.main(["tool", "save", json.dumps({
            "session_id": "codex:cli-1",
            "operations": [_attempt("pytest -q exit 7")],
        }, ensure_ascii=False), "--home", str(home)])
        assert code == 0, capsys.readouterr().out
        capsys.readouterr()
    code = cli.main(["tool", "save", json.dumps({
        "session_id": "codex:cli-1",
        "operations": [_attempt("pytest -q exit 7")],
    }, ensure_ascii=False), "--home", str(home)])
    saved = json.loads(capsys.readouterr().out)
    assert code == 0, saved
    assert saved["session"]["status"] == "已暂停"
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "hook_event_name": "Stop", "session_id": "cli-1", "cwd": str(work),
    })))
    assert cli.main(["hook", "--host", "codex", "--home", str(home)]) == 0
    stopped = json.loads(capsys.readouterr().out)
    assert stopped.get("decision") != "block"
    blob = json.dumps(stopped, ensure_ascii=False)
    assert "已暂停" in blob
    assert "不能把那个进程杀掉" in blob
    assert cli.main(["tool", "bootstrap", json.dumps({
        "session_id": "codex:cli-2", "agent": "codex", "cwd": str(work),
    }, ensure_ascii=False), "--home", str(home)]) == 0
    capsys.readouterr()
    assert cli.main(["tool", "resume", json.dumps({
        "session_id": "codex:cli-2", "target": "codex:cli-1",
    }, ensure_ascii=False), "--home", str(home)]) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["ok"] is True
    assert resumed["handoff"].startswith("已暂停")
    assert "热交接" in resumed["handoff"]
