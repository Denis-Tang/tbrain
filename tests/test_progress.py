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
    service.settings.update({"vault_path": str(vault), "ntp_enabled": False, "timezone": "Asia/Shanghai"})
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
               "tradeoffs": "先不扩大范围", "decided_by": "Kai"}
    payload.update(extra)
    return payload


def test_clear_goals_are_reused(tmp_path, monkeypatch):
    service, _, vault = _service(tmp_path, monkeypatch)
    folder = vault.parent / "clear"
    service.configure_project("清楚项目", [str(folder)])
    path = _clear(vault, "清楚项目")
    before = path.read_bytes()
    _prompt(service, "one", folder, "把本地检查接到每天的门禁")
    package = service.bootstrap("codex:one", "codex", str(folder))["package"]
    assert "不要再问一遍" in package
    assert "状态：未确认" not in package
    assert "这个项目要做成什么样" not in package
    assert path.read_bytes() == before
    _prompt(service, "two", folder, "换一个说法再做同一件事")
    again = service.bootstrap("codex:two", "codex", str(folder))["package"]
    assert "状态：未确认" not in again
    assert service.save("codex:two", [
        {"op": "append", "section": "goal", "items": [{"text": "门禁仍在"}]},
        {"op": "set_status", "status": "已完成", "complete_goals": ["G1"]},
    ])["ok"]


def test_unclear_goals_ask_once_record_why_and_block_pass(tmp_path, monkeypatch):
    service, _, vault = _service(tmp_path, monkeypatch)
    folder = vault.parent / "ask"
    service.configure_project("待定项目", [str(folder)])
    criteria = _criteria(vault, "待定项目")
    original = criteria.read_text(encoding="utf-8")
    original = original.replace("## 验证\n（待填写：本地检查命令，例如 tbrain check）",
                                "## 验证\n（待填写：本地检查命令，例如 tbrain check）\n\n## 备注\n迁移时留下的说明\n")
    criteria.write_text(original, encoding="utf-8")
    _prompt(service, "ask", folder, "把解析器的错误信息改成中文")
    package = service.bootstrap("codex:ask", "codex", str(folder))["package"]
    assert package.startswith("## 待确认的目标和完成标准")
    assert "这个项目要做成什么样" in package
    assert "怎样才算做完" in package
    assert "建议默认：按已说明的任务做完并可交接：把解析器的错误信息改成中文" in package
    assert "探索：" in package
    assert "实现：" in package
    assert "不能拦住宿主直接改" in package
    assert "不要问 Kai" in package
    stored = criteria.read_text(encoding="utf-8")
    assert "状态：未确认" in stored
    assert "迁移时留下的说明" in stored
    assert "questions:" in stored
    frozen = criteria.read_bytes()
    _prompt(service, "other", folder, "改成另一件完全不同的事")
    assert criteria.read_bytes() == frozen
    other = service.bootstrap("codex:other", "codex", str(folder))["package"]
    assert "把解析器的错误信息改成中文" in other
    assert "改成另一件完全不同的事" not in other.split("建议默认", 1)[1].split("状态：未确认", 1)[0]

    facts = service.save("codex:ask", [
        {"op": "append", "section": "evidence", "items": [{"kind": "command", "text": "pytest -q\n2 failed"}]},
        {"op": "append", "section": "dead_end", "items": [{
            "attempt": "先读测试", "reason_kind": "证据不足", "conditions": "目标未确认", "strength": "暂缓",
        }]},
    ])
    assert facts["ok"] is True
    implemented = service.save("codex:ask", [
        {"op": "append", "section": "done", "items": [{"text": "已经改完解析器"}]},
        {"op": "project_prompt", "sections": {"最终目标": "我自己定的目标"}},
        {"op": "acceptance", "task": "先交一版", "verification": "还没确认目标",
         "risks": "目标未确认", "deliverables": "无", "conclusion": "CONDITIONAL PASS"},
    ])
    assert implemented["ok"] is False
    implemented_reasons = " ".join(item["reason"] for item in implemented["rejected"])
    assert "不能把实现写成已完成" in implemented_reasons
    assert "项目提示词" in implemented_reasons
    assert "验收不能代替" in implemented_reasons
    assert _load(service, "codex:ask")[0].items("已完成") == []

    blocked = service.save("codex:ask", [
        {"op": "replace", "section": "target", "text": "我自己写成了目标"},
        {"op": "set_status", "status": "已完成"},
        {"op": "acceptance", "task": "做完了", "verification": "看过代码", "risks": "没有还要人接受的风险",
         "deliverables": "一个提交", "conclusion": "PASS"},
    ])
    assert blocked["ok"] is False
    reasons = " ".join(item["reason"] for item in blocked["rejected"])
    assert "还没确认" in reasons
    assert "PASS" in reasons
    summary, _ = _load(service, "codex:ask")
    assert summary.status != "已完成"
    assert "最终验收" not in summary.extra

    missing_source = service.save("codex:ask", [_confirm("Q1", accept_default=True)])
    assert missing_source["ok"] is False
    assert any("原话" in item["reason"] for item in missing_source["rejected"])
    _prompt(service, "ask", folder, "同意 Q1 的建议默认")
    invented = service.save("codex:ask", [_confirm("Q1", accept_default=True, source="U1")])
    assert invented["ok"] is False
    assert any("明确同意" in item["reason"] for item in invented["rejected"])
    granted = service.save("codex:ask", [_confirm("Q1", accept_default=True, source="U2", grant="auth-required")])
    assert granted["ok"] is False
    assert any("权限" in item["reason"] for item in granted["rejected"])
    assert (_load(service, "codex:ask")[0].header.get("permission_grants") or ["auto"]) == ["auto"] or "auth-required" not in (
        _load(service, "codex:ask")[0].header.get("permission_grants") or [])
    first = service.save("codex:ask", [_confirm("Q1", accept_default=True, source="U2")])
    assert first["ok"] is True
    text = _load(service, "codex:ask")[0].items("决策")[0]["text"]
    assert "理由：这是要交给人用的结果" in text
    assert "取舍：先不扩大范围" in text
    assert "决定人：Kai" in text
    assert "来源：U2" in text
    confirmed = criteria.read_text(encoding="utf-8")
    assert "决定：按已说明的任务做完并可交接：把解析器的错误信息改成中文" in confirmed
    assert "状态：未确认" in confirmed
    assert service.save("codex:ask", [{"op": "set_status", "status": "已完成"}])["ok"] is False

    _prompt(service, "ask", folder, "完成标准定为：本地 tbrain check 退出码为 0，并且错误信息是中文。决定人 Kai")
    absent = service.save("codex:ask", [_confirm("Q2", decision="改成网页版", source="U3")])
    assert absent["ok"] is False
    assert any("原话" in item["reason"] for item in absent["rejected"])
    second = service.save("codex:ask", [_confirm(
        "Q2", decision="本地 tbrain check 退出码为 0，并且错误信息是中文", source="U3")])
    assert second["ok"] is True
    body = criteria.read_text(encoding="utf-8")
    assert "状态：未确认" not in body
    assert "理由：这是要交给人用的结果" in body
    assert "决定人：Kai" in body
    assert service.save("codex:ask", [
        {"op": "append", "section": "goal", "items": [{"text": "错误信息改成中文"}]},
        {"op": "set_status", "status": "已完成", "complete_goals": ["G1"]},
    ])["ok"]


def test_low_information_asks_and_a_clear_task_does_not(tmp_path, monkeypatch):
    service, _, vault = _service(tmp_path, monkeypatch)
    vague = vault.parent / "vague"
    vague.mkdir()
    context = _prompt(service, "vague", vague, "继续")
    text = json.dumps(context, ensure_ascii=False)
    assert "这次要做到什么才算完成" in text
    assert service.save("codex:vague", [{"op": "set_status", "status": "已完成"}])["ok"] is False
    clear = vault.parent / "clear-task"
    clear.mkdir()
    _prompt(service, "clear", clear, "把解析器错误改成中文")
    package = service.bootstrap("codex:clear", "codex", str(clear))["package"]
    assert "状态：未确认" not in package
    assert service.save("codex:clear", [{"op": "set_status", "status": "已完成"}])["ok"]


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
    assert service.save("codex:work", [
        {"op": "append", "section": "evidence", "items": [{"kind": "commit", "text": "4c8fa05"}]},
        {"op": "append", "section": "done", "items": [{"text": "先记下已完成的门禁 [证据:E1]"}]},
        {"op": "append", "section": "decision", "items": [{
            "decision": "热交接写在会话里",
            "reason": "恢复时先读 L0",
            "rationale": "不等冷蒸馏",
            "tradeoffs": "会话总结会变长",
            "decided_by": "Kai",
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
                   "决定人：Kai", "### 已完成", "先记下已完成的门禁", "### 已核实", "### 未核实",
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
        {"op": "replace", "section": "next_step", "text": "先把失败次数记下来"},
    ])
    assert noted["ok"] is True
    assert _load(service, "codex:work")[0].status == "已暂停"
    no_source = service.save("codex:work", [{
        "op": "continue", "route": "改为先补表格驱动测试再改解析",
        "why": "再试一次", "tradeoffs": "不换路", "decided_by": "Kai",
    }])
    assert no_source["ok"] is False
    assert any("source" in item["reason"] for item in no_source["rejected"])
    _prompt(service, "work", folder, route)
    same_route = service.save("codex:work", [{
        "op": "continue", "route": route, "why": "再试一次", "tradeoffs": "不换路",
        "decided_by": "Kai", "source": "U2",
    }])
    assert same_route["ok"] is False
    assert "不能当作换路" in same_route["rejected"][0]["reason"]

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

    _prompt(service, "work", folder, "改为先补表格驱动测试再改解析")
    continued = service.save("codex:work", [{
        "op": "continue", "route": "改为先补表格驱动测试再改解析",
        "why": "同一条 pytest 失败已经重复", "tradeoffs": "先不重写解析器",
        "decided_by": "Kai", "source": "U3",
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
    assert "决定人：Kai" in restored.items("决策")[-1]["text"]


def test_failed_pause_write_keeps_the_previous_session(tmp_path, monkeypatch):
    service, _, vault = _service(tmp_path, monkeypatch)
    folder = vault.parent / "disk"
    folder.mkdir()
    _prompt(service, "disk", folder, "核对写入失败不假装暂停")
    assert service.save("codex:disk", [{
        "op": "append", "section": "decision",
        "items": [{"decision": "先留下原决定", "reason": "写入失败不能丢",
                   "rationale": "原子写失败要保留原文", "tradeoffs": "这次暂停不会生效", "decided_by": "Kai"}],
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


def test_failed_question_write_stays_unresolved(tmp_path, monkeypatch):
    service, _, vault = _service(tmp_path, monkeypatch)
    folder = vault.parent / "criteria-disk"
    service.configure_project("写失败", [str(folder)])
    path = _criteria(vault, "写失败")
    original = path.read_bytes()
    real_write = service.store.write_text

    def fail_criteria(target, text, **kwargs):
        if Path(target).name == "完成标准.md":
            raise OSError("完成标准写不进去")
        return real_write(target, text, **kwargs)

    monkeypatch.setattr(service.store, "write_text", fail_criteria)
    _prompt(service, "ask", folder, "把目标问清楚")
    assert path.read_bytes() == original
    package = service.bootstrap("codex:ask", "codex", str(folder))["package"]
    assert "这个项目要做成什么样" in package
    monkeypatch.setattr(service.store, "write_text", real_write)
    assert service.save("codex:ask", [{"op": "set_status", "status": "已完成"}])["ok"] is False


def test_old_completion_and_old_session_stay_compatible(tmp_path, monkeypatch):
    service, _, vault = _service(tmp_path, monkeypatch)
    folder = vault.parent / "old"
    service.configure_project("旧项目", [str(folder)])
    path = _criteria(vault, "旧项目")
    path.write_text(
        "---\nfields:\n- goal\n- done\n- evidence\n- verify\n---\n\n"
        "## 目标\n迁移来的目标：本地记忆可以交接\n\n"
        "## 完成标准\n- [ ] 旧会话仍能保存\n\n"
        "## 证据\n沿用原来的证据格式\n\n"
        "## 验证\n沿用 tbrain check\n\n"
        "## 备注\n这是迁移时留下的说明\n",
        encoding="utf-8",
    )
    before = path.read_bytes()
    _prompt(service, "old", folder, "继续旧任务")
    package = service.bootstrap("codex:old", "codex", str(folder))["package"]
    assert path.read_bytes() == before
    assert "状态：未确认" not in package
    assert "不要再问一遍" in package
    assert service.save("codex:old", [
        {"op": "replace", "section": "next_step", "text": "旧会话照常保存下一步"},
    ])["ok"]
    summary, _ = _load(service, "codex:old")
    assert "stall" not in summary.header or not (summary.header.get("stall") or {}).get("paused")
    bare = vault.parent / "bare"
    bare.mkdir()
    _prompt(service, "bare", bare, "没有暂停字段的旧会话也能保存")
    assert service.save("codex:bare", [
        {"op": "replace", "section": "next_step", "text": "照常写下一步"},
    ])["ok"]
    assert _load(service, "codex:bare")[0].status == "进行中"


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
    SettingsStore(home).save({"vault_path": str(vault), "ntp_enabled": False, "timezone": "Asia/Shanghai"})
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
