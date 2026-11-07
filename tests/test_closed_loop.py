"""Closed-loop minimum cut: worktree, contract, reasons, wrap-up, local check."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from tbrain import cli
from tbrain.check import decision_missing_why, run_check
from tbrain.policy import classify, covers
from tbrain.service import TbrainService
from tbrain.settings import SettingsStore
from tbrain.worktree import slug


def _service(tmp_path):
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    service = TbrainService(home, vault=vault)
    service.settings.update({"vault_path": str(vault), "ntp_enabled": False, "timezone": "Asia/Shanghai"})
    return service, home, vault


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)


def _prompt(service, session, cwd, text, host="codex"):
    service.hook(host, {"hook_event_name": "UserPromptSubmit", "session_id": session,
                        "cwd": str(cwd), "prompt": text})


def _load(service, session_id):
    from tbrain import session as flow

    return flow.load_session(service, session_id)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TBRAIN_HOME", str(tmp_path / "home"))
    return _service(tmp_path)


def test_create_project_makes_git_worktree_and_completion_criteria(env):
    service, _, vault = env
    folder = vault.parent / "app"
    saved = service.configure_project("演示项目", [str(folder)])
    assert (folder / ".git").is_dir()
    assert saved["worktree_status"] == "worktree", saved.get("worktree_error")
    assert saved["worktree_convention"] == f"../.tbrain-worktrees/app/{slug('演示项目')}"
    assert saved["completion_fields"] == ["goal", "done", "evidence", "verify"]
    worktree = Path(saved["worktree"])
    assert worktree.is_dir()
    assert folder.resolve() not in worktree.resolve().parents
    assert _git(worktree, "rev-parse", "--is-inside-work-tree").stdout.strip() == "true"
    assert _git(worktree, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "tbrain/演示项目"
    assert "decision-missing-why" in (worktree / ".tbrain" / "check.yaml").read_text(encoding="utf-8")
    assert "一处 git 工作区" in (folder / ".tbrain" / "WORKTREE.md").read_text(encoding="utf-8")
    criteria = vault / "项目" / "演示项目" / "完成标准.md"
    text = criteria.read_text(encoding="utf-8")
    for field in ("goal", "done", "evidence", "verify"):
        assert field in text
    assert "待填写" in text
    head = _git(folder, "rev-parse", "HEAD").stdout.strip()
    criteria.write_text(text.replace("（待填写：这个项目要做成什么样）", "做成可交接的本地执行"), encoding="utf-8")
    again = service.configure_project("演示项目", [str(folder)])
    assert "做成可交接的本地执行" in criteria.read_text(encoding="utf-8")
    assert again["worktree"] == saved["worktree"]
    assert _git(folder, "rev-parse", "HEAD").stdout.strip() == head
    note = (vault / "项目" / "演示项目" / "工作区.md").read_text(encoding="utf-8")
    assert str(worktree) in note

    unbound = service.configure_project("只有标准", [])
    assert unbound["worktree_status"] == "unbound"
    assert (vault / "项目" / "只有标准" / "完成标准.md").is_file()


def test_bootstrap_resume_contract_and_decision_reasons(env):
    service, _, _ = env
    folder = env[2].parent / "repo"
    saved = service.configure_project("演示项目", [str(folder)])
    worktree = saved["worktree"]
    _prompt(service, "origin", worktree, "把本地检查做成每天的门禁")
    saved_decision = service.save("codex:origin", [{
        "op": "append", "section": "decision",
        "items": [{
            "decision": "本地检查做门禁",
            "reason": "不依赖 GitHub",
            "rationale": "同一类错误要在本机失败",
            "tradeoffs": "远程 CI 留到开拉取请求",
            "decided_by": "Kai",
        }],
    }])
    assert saved_decision["ok"] is True
    summary, path = _load(service, "codex:origin")
    text = summary.items("决策")[0]["text"]
    assert "本地检查做门禁 | 不依赖 GitHub" in text
    assert "理由：同一类错误要在本机失败" in text
    assert "取舍：远程 CI 留到开拉取请求" in text
    assert "决定人：Kai" in text
    stored = path.read_text(encoding="utf-8")
    assert "决定人：Kai" in stored

    package = service.bootstrap("codex:origin", "codex", worktree)["package"]
    for phrase in ("工作约定", "只改这个项目", "goal", "done", "evidence", "verify",
                   "真实证据", "Kai", "数据库迁移", "登录", "对外协议", "安装", "升级",
                   "小重构", "要不要我继续", "理由", "取舍", "决定人",
                   "推送分支", "拉取请求", "预览", "生产环境", "删除数据", "密钥",
                   "tbrain check", "small refactors", "CONDITIONAL PASS", "真实 Windows",
                   "pytest", "人工剩余风险接受仍未完成", "应该可以", "退出码"):
        assert phrase in package, phrase
    assert worktree in package
    assert "待填写" in package

    _prompt(service, "next", worktree, "接上一次的门禁决定")
    resumed = service.resume("codex:next", "codex:origin")
    assert resumed["ok"] is True
    assert "理由：同一类错误要在本机失败" in resumed["handoff"]
    assert "取舍：远程 CI 留到开拉取请求" in resumed["handoff"]
    assert "决定人：Kai" in resumed["handoff"]
    assert "工作约定" in resumed["handoff"]
    pack = service.handoff_pack("演示项目")
    assert "工作约定" in pack["contract"]
    assert any("决定人：Kai" in item["text"] for item in pack["items"])


def _clear_criteria(vault, project):
    path = vault / "项目" / project / "完成标准.md"
    text = path.read_text(encoding="utf-8")
    text = text.replace("（待填写：这个项目要做成什么样）", "已有目标：把这一轮做完并可交接")
    text = text.replace("- [ ] （待填写：可以核对的完成条件）", "- [ ] 证据、本地检查和决定理由都齐")
    path.write_text(text, encoding="utf-8")


def test_wrap_up_blocks_missing_gates_and_stop_stays_quiet_while_in_progress(env):
    service, _, vault = env
    folder = vault.parent / "wrap"
    service.configure_project("收尾项目", [str(folder)])
    _clear_criteria(vault, "收尾项目")
    _prompt(service, "open", folder, "先做一轮，先不交")
    assert service.save("codex:open", [
        {"op": "append", "section": "decision", "items": [{"text": "先记一笔 | 还没写原因"}]},
        {"op": "replace", "section": "next_step", "text": "继续改完再验证"},
    ])["ok"]
    assert service.hook("codex", {"hook_event_name": "Stop", "session_id": "open", "cwd": str(folder)}) == {}
    blocked = service.hook("codex", {"hook_event_name": "WrapUp", "session_id": "open", "cwd": str(folder)})
    assert blocked["decision"] == "block"
    assert "证据" in blocked["reason"]
    assert "本地检查" in blocked["reason"]
    assert "决定人" in blocked["reason"]

    _prompt(service, "submit", folder, "这一轮要交出去")
    assert service.save("codex:submit", [
        {"op": "append", "section": "goal", "items": [{"text": "做完这一轮"}]},
        {"op": "append", "section": "decision", "items": [{"text": "先记一笔 | 还没写原因"}]},
        {"op": "permission", "action": "delete production data"},
        {"op": "set_status", "status": "已完成", "complete_goals": ["G1"]},
    ])["ok"]
    stop = service.hook("codex", {"hook_event_name": "Stop", "session_id": "submit", "cwd": str(folder)})
    assert stop["decision"] == "block"
    for phrase in ("证据", "本地检查", "决定人", "权限不够", "auth-required"):
        assert phrase in stop["reason"], phrase

    _prompt(service, "done", folder, "补齐证据后再交")
    assert service.save("codex:done", [
        {"op": "append", "section": "evidence", "items": [{"kind": "commit", "text": "4c8fa05"}]},
        {"op": "append", "section": "done", "items": [{"text": "写了本地门禁 [证据:E1]"}]},
        {"op": "append", "section": "goal", "items": [{"text": "门禁能拦住缺理由的决定"}]},
        {"op": "append", "section": "decision", "items": [{
            "decision": "本地检查做门禁",
            "reason": "不依赖 GitHub",
            "rationale": "同一类错误要在本机失败",
            "tradeoffs": "远程 CI 留到开拉取请求",
            "decided_by": "Kai",
        }]},
        {"op": "permission", "action": "read tests"},
    ])["ok"]
    service.hook("codex", {"hook_event_name": "ToolResult", "session_id": "done", "cwd": str(folder),
                          "last_test": {"command": "tbrain check", "exit_code": 0}})
    assert service.save("codex:done", [
        {"op": "replace", "section": "next_step", "text": "核对本地检查已经通过"},
        {"op": "set_status", "status": "已完成", "complete_goals": ["G1"]},
    ])["ok"]
    early = service.hook("codex", {"hook_event_name": "Stop", "session_id": "done", "cwd": str(folder)})
    assert early["decision"] == "block"
    assert "最终验收" in early["reason"]
    assert "应该可以" in early["reason"]
    assert "退出码" in early["reason"]
    assert service.save("codex:done", [_pass_report()])["ok"]
    assert service.hook("codex", {"hook_event_name": "Stop", "session_id": "done", "cwd": str(folder)}) == {}
    assert service.hook("codex", {"hook_event_name": "WrapUp", "session_id": "done", "cwd": str(folder)}) == {}
    _, path = _load(service, "codex:done")
    stored = path.read_text(encoding="utf-8")
    for heading in ("### A 任务状态", "### B 验证与证据", "### C 风险", "### D 交付物", "### E 结论"):
        assert heading in stored
    assert "\nPASS\n" in stored


def _pass_report(**overrides):
    report = {
        "op": "acceptance",
        "task": "本地门禁已接到收尾",
        "verification": "tbrain check 退出码 0，证据 E1",
        "risks": "没有剩下要 Kai 接受的风险",
        "deliverables": "commit 4c8fa05；在项目目录运行 tbrain check",
        "conclusion": "PASS",
    }
    report.update(overrides)
    return report


def _ready_for_acceptance(service, folder, session):
    service.configure_project(session, [str(folder)])
    _clear_criteria(service.vault_root(), session)
    _prompt(service, session, folder, "准备提交最终验收")
    sid = f"codex:{session}"
    assert service.save(sid, [
        {"op": "append", "section": "evidence", "items": [{"kind": "commit", "text": "4c8fa05"}]},
        {"op": "append", "section": "done", "items": [{"text": "写了验收 [证据:E1]"}]},
        {"op": "append", "section": "goal", "items": [{"text": "交出 A-E"}]},
        {"op": "append", "section": "decision", "items": [{
            "decision": "用 A-E 收尾",
            "reason": "结论只能三选一",
            "rationale": "避免含糊的完成声明",
            "tradeoffs": "多写一段验收",
            "decided_by": "Kai",
        }]},
        {"op": "permission", "action": "read tests"},
    ])["ok"]
    service.hook("codex", {"hook_event_name": "ToolResult", "session_id": session, "cwd": str(folder),
                          "last_test": {"command": "tbrain check", "exit_code": 0}})
    assert service.save(sid, [
        {"op": "replace", "section": "next_step", "text": "提交最终验收"},
        {"op": "set_status", "status": "已完成", "complete_goals": ["G1"]},
    ])["ok"]
    return sid


def test_wrap_up_acceptance_report_blocks_until_valid_and_stores_it(env):
    service, _, vault = env
    folder = vault.parent / "accept"
    sid = _ready_for_acceptance(service, folder, "验收")
    stop = {"hook_event_name": "Stop", "session_id": "验收", "cwd": str(folder)}
    assert "最终验收" in service.hook("codex", stop)["reason"]

    missing = service.save(sid, [_pass_report(deliverables="")])
    assert missing["ok"] is False
    assert any("D 交付物" in item["reason"] for item in missing["rejected"])
    bad = service.save(sid, [_pass_report(conclusion="MAYBE")])
    assert bad["ok"] is False
    assert any("PASS" in item["reason"] for item in bad["rejected"])

    conditional = _pass_report(
        conclusion="CONDITIONAL PASS",
        risks="安装路径还没在 Windows 上看过",
        windows_installable=True,
        windows_gap="没有在 Windows 机器上安装并运行该产物",
    )
    assert service.save(sid, [conditional])["ok"]
    _, path = _load(service, sid)
    stored = path.read_text(encoding="utf-8")
    assert "人工剩余风险接受：仍未完成" in stored
    assert "Windows 缺口：没有在 Windows 机器上安装并运行该产物" in stored
    assert service.hook("codex", stop) == {}
    _prompt(service, "验收续", folder, "读回验收结论")
    resumed = service.resume("codex:验收续", sid)
    assert "CONDITIONAL PASS" in resumed["handoff"]
    assert "人工剩余风险接受：仍未完成" in resumed["handoff"]

    path.write_text(stored.replace("人工剩余风险接受：仍未完成", "人工剩余风险接受：不适用"), encoding="utf-8")
    stripped = service.hook("codex", stop)
    assert stripped["decision"] == "block"
    assert "人工剩余风险接受" in stripped["reason"]


def test_windows_installable_cannot_pass_without_a_real_windows_run(env):
    service, _, vault = env
    folder = vault.parent / "win"
    sid = _ready_for_acceptance(service, folder, "安装包")
    stop = {"hook_event_name": "Stop", "session_id": "安装包", "cwd": str(folder)}
    pytest_only = _pass_report(
        verification="Linux 上 pytest -q 已通过",
        windows_installable=True,
        windows_verified=True,
        windows_evidence="pytest -q",
        windows_gap="只跑了 Linux pytest",
    )
    rejected = service.save(sid, [pytest_only])
    assert rejected["ok"] is False
    assert any("不能是 PASS" in item["reason"] or "pytest" in item["reason"] for item in rejected["rejected"])
    assert service.hook("codex", stop)["decision"] == "block"

    bare_pass = _pass_report(windows_installable=True, conclusion="PASS")
    assert service.save(sid, [bare_pass])["ok"] is False
    named = _pass_report(
        conclusion="FAIL",
        risks="安装包没有在 Windows 上打开过",
        windows_installable=True,
        windows_gap="没有在 Windows 机器上安装并运行该产物",
    )
    assert service.save(sid, [named])["ok"]
    assert service.hook("codex", stop) == {}

    prose = _pass_report(
        conclusion="PASS",
        verification="Windows 真机安装并运行过安装包；Linux pytest 只是附加检查",
        windows_installable=True,
        windows_verified=True,
        windows_evidence="在 Windows 机器上安装 tbrain-setup 并运行 tbrain.exe",
    )
    prose_saved = service.save(sid, [prose])
    assert prose_saved["ok"] is False
    assert any("宿主" in item["reason"] for item in prose_saved["rejected"])
    _, path = _load(service, sid)
    assert "\nPASS\n" not in path.read_text(encoding="utf-8")
    assert service.hook("codex", {"hook_event_name": "WrapUp", "session_id": "安装包", "cwd": str(folder)}) == {}


def test_local_check_fails_missing_why_then_passes_when_reasons_exist(tmp_path):
    root = tmp_path / "notes"
    root.mkdir()
    note = root / "NOTES.md"
    note.write_text("### K1\n改用工作区\n", encoding="utf-8")
    config = root / ".tbrain"
    config.mkdir()
    (config / "check.yaml").write_text(yaml.safe_dump({
        "version": 1,
        "checks": [{"name": "noop", "run": "sh -c 'exit 0'"}],
        "lints": ["decision-missing-why"],
    }, allow_unicode=True, sort_keys=False), encoding="utf-8")
    failed = run_check(root)
    assert failed["ok"] is False
    assert failed["checks"][0]["ok"] is True
    assert failed["lints"][0]["id"] == "decision-missing-why"
    assert failed["lints"][0]["findings"]
    assert "理由" in failed["lints"][0]["findings"][0]["message"]

    note.write_text(
        "### K1\n改用工作区 | 一个项目一处工作区\n"
        "理由：避免同时改两份代码\n取舍：多一个目录，换来隔离\n决定人：Kai\n",
        encoding="utf-8",
    )
    assert run_check(root)["ok"] is True

    marker = tmp_path / "marker"
    marker.mkdir()
    (marker / "choice.md").write_text("<!-- tbrain:decision -->\n改用工作区\n", encoding="utf-8")
    marked = decision_missing_why(marker)
    assert marked and marked[0]["line"] == 1
    (marker / "choice.md").write_text(
        "<!-- tbrain:decision -->\n理由：只留一处工作区\n取舍：不在主目录里堆副本\n决定人：Kai\n",
        encoding="utf-8",
    )
    assert decision_missing_why(marker) == []

    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "tbrain.check.yaml").write_text(yaml.safe_dump({
        "version": 1,
        "checks": [{"name": "bad", "run": "sh -c 'exit 2'"}],
        "lints": [],
    }, allow_unicode=True, sort_keys=False), encoding="utf-8")
    command_failed = run_check(bad)
    assert command_failed["ok"] is False
    assert command_failed["checks"][0]["exit_code"] == 2
    (bad / "tbrain.check.yaml").write_text("version: 1\nchecks: []\nlints:\n  - not-a-lint\n", encoding="utf-8")
    unknown = run_check(bad)
    assert unknown["ok"] is False
    assert "未知 lint" in unknown["lints"][0]["findings"][0]["message"]


def test_permission_tiers_cover_only_what_was_granted():
    assert classify("read tests") == "auto"
    assert classify("push branch") == "conditional"
    assert classify("delete production data") == "auth-required"
    assert covers(["auto"], "auto")
    assert not covers(["auto"], "conditional")
    assert not covers(["conditional"], "auth-required")
    assert covers(["auth-required"], "conditional")


def test_cli_project_add_and_check_exit_code(env, capsys):
    service, home, vault = env
    folder = vault.parent / "cli-app"
    code = cli.main(["project", "add", "--name", "命令行项目", "--path", str(folder), "--home", str(home)])
    captured = capsys.readouterr()
    assert code == 0, captured.out
    payload = json.loads(captured.out)
    assert payload["worktree_status"] == "worktree"
    assert (folder / ".git").is_dir()
    assert SettingsStore(home).load().vault_path

    note = folder / "NOTES.md"
    note.write_text("### K1\n改用工作区\n", encoding="utf-8")
    failed = cli.main(["check", "--path", str(folder), "--home", str(home)])
    failed_out = capsys.readouterr().out
    assert failed == 1
    assert json.loads(failed_out)["ok"] is False
    note.write_text(
        "### K1\n改用工作区 | 一个项目一处工作区\n"
        "理由：避免同时改两份代码\n取舍：多一个目录\n决定人：Kai\n",
        encoding="utf-8",
    )
    assert cli.main(["check", "--path", str(folder), "--home", str(home)]) == 0
    assert service.store.project("命令行项目")["name"] == "命令行项目"
