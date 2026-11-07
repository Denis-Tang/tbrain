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
    service.settings.update({"vault_path": str(vault), "ntp_enabled": False, "timezone": "Asia/Shanghai", "task_mode": True})
    return service


def _prompt(service, session, cwd, text):
    return service.hook("codex", {
        "hook_event_name": "UserPromptSubmit", "session_id": session,
        "cwd": str(cwd), "prompt": text,
    })


def _load(service, session_id):
    from tbrain import session as flow

    return flow.load_session(service, session_id)


def _clear(service, project):
    path = service.vault_root() / "项目" / project / "完成标准.md"
    path.write_text(path.read_text(encoding="utf-8")
                    .replace("（待填写：这个项目要做成什么样）", "已有明确目标")
                    .replace("- [ ] （待填写：可以核对的完成条件）", "- [ ] 本地检查能核对"),
                    encoding="utf-8")


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
    saved = service.configure_project("未入库", [str(app)])
    assert (app / "app.py").read_text(encoding="utf-8") == "SENTINEL = 1\n"
    assert saved["worktree_status"] == "original"
    package = service.bootstrap("codex:nongit", "codex", str(app), project="未入库")["package"]
    assert str(app.resolve()) in package or str(app) in package
    assert "没有自动提交" in package
    assert "隔离还不完整" in package

    unborn = tmp_path / "unborn"
    unborn.mkdir()
    (unborn / "app.py").write_text("SENTINEL = 2\n", encoding="utf-8")
    service2 = _service(tmp_path, "unborn")
    import subprocess
    subprocess.run(["git", "-C", str(unborn), "init"], check=True, capture_output=True)
    saved2 = service2.configure_project("未提交", [str(unborn)])
    assert saved2["worktree_status"] == "original"
    assert (unborn / "app.py").read_text(encoding="utf-8") == "SENTINEL = 2\n"
    head = subprocess.run(["git", "-C", str(unborn), "rev-parse", "HEAD"], capture_output=True, text=True)
    assert head.returncode != 0

    committed = tmp_path / "committed"
    committed.mkdir()
    (committed / "app.py").write_text("SENTINEL = 3\n", encoding="utf-8")
    git = ["git", "-c", "user.name=Tbrain", "-c", "user.email=tbrain@localhost", "-c", "commit.gpgsign=false"]
    subprocess.run([*git, "-C", str(committed), "init"], check=True, capture_output=True)
    subprocess.run([*git, "-C", str(committed), "add", "app.py"], check=True, capture_output=True)
    subprocess.run([*git, "-C", str(committed), "commit", "-m", "app"], check=True, capture_output=True)
    service3 = _service(tmp_path, "committed-home")
    saved3 = service3.configure_project("已提交", [str(committed)])
    assert saved3["worktree_status"] == "worktree"
    assert (Path(saved3["worktree"]) / "app.py").read_text(encoding="utf-8") == "SENTINEL = 3\n"
    (committed / "app.py").write_text("SENTINEL = 4\n", encoding="utf-8")
    (committed / "notes.txt").write_text("local only\n", encoding="utf-8")
    dirty = service3.configure_project("已提交", [str(committed)])
    card = service3.store.project("已提交")
    assert dirty["worktree_status"] == "original"
    assert Path(card["worktree"]).resolve() == committed.resolve()
    assert "未提交的文件没有复制" in card["worktree_limitation"]
    assert (committed / "app.py").read_text(encoding="utf-8") == "SENTINEL = 4\n"
    assert (committed / "notes.txt").read_text(encoding="utf-8") == "local only\n"
    package = service3.bootstrap("codex:committed", "codex", str(committed), project="已提交")["package"]
    assert str(committed.resolve()) in package
    listed = subprocess.run(
        ["git", "-C", str(committed), "worktree", "list", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout
    isolated = [
        Path(line.split(" ", 1)[1])
        for line in listed.splitlines()
        if line.startswith("worktree ") and Path(line.split(" ", 1)[1]).resolve() != committed.resolve()
    ]
    assert isolated
    assert (isolated[0] / "app.py").read_text(encoding="utf-8") == "SENTINEL = 3\n"
    assert not (isolated[0] / "notes.txt").exists()


def test_changed_failure_counts_are_not_one_stall_and_a_batch_keeps_later_success(tmp_path):
    service = _service(tmp_path)
    folder = tmp_path / "app"
    folder.mkdir()
    _prompt(service, "stall", folder, "同一测试的失败次数在下降")
    route = "pytest tests/test_app.py::test_parse -q"
    for count in (10, 5, 1):
        saved = service.save("codex:stall", [{
            "op": "attempt", "route": route, "evidence": f"{count} failed", "productive": True,
        }])
        assert saved["ok"] is True
        assert _load(service, "codex:stall")[0].status != "已暂停"
    summary = _load(service, "codex:stall")[0]
    assert [item["effect"] for item in summary.header["stall"]["log"]][0] == "start"
    assert "outcome" in [item["effect"] for item in summary.header["stall"]["log"]]

    _prompt(service, "key-values", folder, "失败数以键值形式记录")
    for count in (10, 5, 1):
        assert service.save("codex:key-values", [{
            "op": "append", "section": "evidence",
            "items": [{"kind": "command", "text": f"pytest test_parser.py failures={count}"}],
        }])["ok"]
    assert _load(service, "codex:key-values")[0].status != "已暂停"

    _prompt(service, "same", folder, "同一结果换个说法")
    assert service.save("codex:same", [{
        "op": "attempt", "route": route, "evidence": "10 failed", "productive": True,
    }])["ok"]
    assert service.save("codex:same", [{
        "op": "attempt", "route": route, "evidence": "10 tests failed", "productive": True,
    }])["ok"]
    assert service.save("codex:same", [{
        "op": "attempt", "route": route, "evidence": "10 failed", "productive": True,
    }])["ok"]
    assert _load(service, "codex:same")[0].status == "已暂停"

    _prompt(service, "batch", folder, "同一批三次尝试之后测试通过")
    saved = service.save("codex:batch", [
        {"op": "attempt", "route": "rewrite parser from scratch", "evidence": "still red alpha"},
        {"op": "attempt", "route": "swap tokenizer library", "evidence": "still red beta"},
        {"op": "attempt", "route": "delete cache directory", "evidence": "still red gamma"},
        {"op": "append", "section": "evidence", "items": [{"kind": "command", "text": "pytest -q\n1 passed"}]},
    ], last_test={"command": "pytest -q", "exit_code": 0})
    summary = _load(service, "codex:batch")[0]
    assert saved["ok"] is True
    assert summary.status != "已暂停"
    assert summary.header["last_verify"]["exit_code"] == 0
    assert summary.items("证据")[-1]["text"] == "pytest -q\n1 passed"
    assert "progress" in [item["effect"] for item in summary.header["stall"]["log"]]

    _prompt(service, "done-tail", folder, "完成项不能抵消三次无进展")
    service.save("codex:done-tail", [
        {"op": "attempt", "route": "alpha route"},
        {"op": "attempt", "route": "beta route"},
        {"op": "attempt", "route": "gamma route"},
        {"op": "append", "section": "done", "items": [{"text": "记下之前的工作"}]},
    ])
    assert _load(service, "codex:done-tail")[0].status == "已暂停"


def test_assent_does_not_require_the_users_name_in_the_sentence(tmp_path):
    service = _service(tmp_path)
    folder = tmp_path / "app"
    folder.mkdir()
    service.configure_project("确认项目", [str(folder)])
    _prompt(service, "ask", folder, "说明本次任务")
    assert service.save("codex:ask", [{"op": "understanding", "goal": "完成本次任务",
        "scope": "本次需求", "deliverables": "任务结果", "done": "有证据可核对"}])["ok"]
    _prompt(service, "ask", folder, "同意 Q1 的建议默认")
    missing = service.save("codex:ask", [{
        "op": "confirm", "question": "Q1",
        "why": "按建议做", "tradeoffs": "先不扩大", "decided_by": "用户",
    }])
    assert missing["ok"] is False
    unknown = service.save("codex:ask", [{
        "op": "confirm", "question": "Q1", "source": "U9",
        "why": "按建议做", "tradeoffs": "先不扩大", "decided_by": "用户",
    }])
    assert unknown["ok"] is False and any("U9" in item["reason"] for item in unknown["rejected"])
    accepted = service.save("codex:ask", [{
        "op": "confirm", "question": "Q1", "source": "U2",
        "why": "按建议做", "tradeoffs": "先不扩大", "decided_by": "用户",
    }])
    assert accepted["ok"] is True
    summary = _load(service, "codex:ask")[0]
    assert "确认来源：U2" in summary.extra["任务理解"]
    assert summary.header["goal_questions"][0]["decided_by"] == "用户"
    assert "用户" not in "同意 Q1 的建议默认"
    grants = _load(service, "codex:ask")[0].header.get("permission_grants") or ["auto"]
    assert "auth-required" not in grants


def test_pause_keeps_evidence_and_continue_needs_the_users_route(tmp_path):
    service = _service(tmp_path)
    folder = tmp_path / "app"
    folder.mkdir()
    _clear_ready = False
    _prompt(service, "pause", folder, "先看三次没有证据的尝试")
    assert not _clear_ready
    saved = service.save("codex:pause", [
        {"op": "attempt", "route": "rewrite parser from scratch", "evidence": "still red alpha"},
        {"op": "attempt", "route": "swap tokenizer library", "evidence": "still red beta"},
        {"op": "attempt", "route": "delete cache directory", "evidence": "still red gamma"},
    ])
    assert saved["ok"] is True
    assert _load(service, "codex:pause")[0].status == "已暂停"
    noted = service.save("codex:pause", [
        {"op": "append", "section": "evidence", "items": [{"kind": "command", "text": "pytest -q\n1 passed"}]},
        {"op": "replace", "section": "next_step", "text": "改为先补表格驱动测试再改解析"},
    ])
    assert noted["ok"] is True
    summary = _load(service, "codex:pause")[0]
    assert summary.status == "已暂停"
    assert summary.items("证据")[-1]["text"] == "pytest -q\n1 passed"
    assert summary.header["stall"]["proposed_route"] == "改为先补表格驱动测试再改解析"
    no_source = service.save("codex:pause", [{
        "op": "continue", "route": "改为先补表格驱动测试再改解析",
        "why": "三次都没有新证据", "tradeoffs": "先不重写", "decided_by": "用户",
    }])
    assert no_source["ok"] is False
    bad_source = service.save("codex:pause", [{
        "op": "continue", "route": "改为先补表格驱动测试再改解析", "source": "U9",
        "why": "三次都没有新证据", "tradeoffs": "先不重写", "decided_by": "用户",
    }])
    assert bad_source["ok"] is False
    old_source = service.save("codex:pause", [{
        "op": "continue", "route": "改为先补表格驱动测试再改解析", "source": "U1",
        "why": "三次都没有新证据", "tradeoffs": "先不重写", "decided_by": "用户",
    }])
    assert old_source["ok"] is False
    assert "暂停之前" in old_source["rejected"][0]["reason"]
    _prompt(service, "pause", folder, "同意把按钮改成蓝色")
    unrelated = service.save("codex:pause", [{
        "op": "continue", "source": "U2", "why": "按钮颜色与路线无关",
        "tradeoffs": "保持暂停", "decided_by": "用户",
    }])
    assert unrelated["ok"] is False
    _prompt(service, "pause", folder, "继续按建议的新路线做")
    fragment = service.save("codex:pause", [{
        "op": "continue", "route": "新路线", "source": "U3",
        "why": "三次都没有新证据", "tradeoffs": "先不重写", "decided_by": "用户",
    }])
    assert fragment["ok"] is False
    assert "已记下的下一步" in fragment["rejected"][0]["reason"]
    continued = service.save("codex:pause", [{
        "op": "continue", "route": "改为先补表格驱动测试再改解析", "source": "U3",
        "why": "三次都没有新证据", "tradeoffs": "先不重写", "decided_by": "用户",
    }])
    assert continued["ok"] is True
    assert _load(service, "codex:pause")[0].status == "进行中"
    _prompt(service, "next", folder, "接手")
    resumed = service.resume("codex:next", "codex:pause")
    assert resumed["ok"] is True
    assert "pytest -q" in resumed["handoff"]


def test_windows_acceptance_records_a_report_without_claiming_automatic_verification(tmp_path):
    service = _service(tmp_path)
    folder = tmp_path / "app"
    folder.mkdir()
    service.configure_project("验收", [str(folder)])
    _clear(service, "验收")
    _prompt(service, "accept", folder, "准备交最终验收")
    assert service.save("codex:accept", [{"op": "understanding", "goal": "完成本次任务",
        "scope": "本次需求", "deliverables": "任务结果", "done": "有证据可核对"}])["ok"]
    _prompt(service, "accept", folder, "确认任务理解")
    assert service.save("codex:accept", [{"op": "confirm", "question": "Q1", "source": "U2",
        "why": "目标已确认", "tradeoffs": "不扩大范围", "decided_by": "用户"}])["ok"]
    report = {
        "op": "acceptance",
        "task": "这一轮不是安装包发布",
        "verification": "Linux 上记录了 tbrain check 退出码 0。证据 E1。",
        "risks": "没有另外要人接受的产品风险",
        "deliverables": "检查配置。运行 tbrain check。",
        "conclusion": "PASS",
        "windows_installable": True,
        "windows_verified": False,
        "windows_evidence": "未做安装运行验证",
        "windows_gap": "尚未安装",
    }
    saved = service.save("codex:accept", [
        {"op": "append", "section": "evidence", "items": [{"kind": "command", "text": "tbrain check\nexit 0"}]},
        report,
    ], last_test={"command": "tbrain check", "exit_code": 0})
    assert saved["ok"] is False
    assert any("不能是 PASS" in item["reason"] for item in saved["rejected"])
    hooked = service.hook("codex", {
        "hook_event_name": "ToolResult", "session_id": "accept", "cwd": str(folder),
        "last_test": {"command": "tbrain check", "exit_code": 0},
    })
    assert hooked == {} or "decision" not in hooked or hooked.get("decision") != "block"
    summary = _load(service, "codex:accept")[0]
    assert summary.header["last_verify"]["source"] == "tool"
    import platform
    assert summary.header["last_verify"]["platform"] == platform.system()
    still = service.save("codex:accept", [report])
    assert still["ok"] is False
    recorded = service.save("codex:accept", [{
        **report, "windows_verified": True, "windows_gap": "",
        "windows_evidence": "在 Windows 上安装候选包并打开窗口，记录见 E1",
    }])
    assert recorded["ok"]
    text = _load(service, "codex:accept")[0].extra["最终验收"]
    assert "已做（报告者声明）" in text


def test_dirty_committed_repo_contract_uses_the_original_files(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text("version-one\n", encoding="utf-8")
    git = ["git", "-c", "user.name=Tbrain", "-c", "user.email=tbrain@localhost", "-c", "commit.gpgsign=false"]
    subprocess.run([*git, "-C", str(root), "init"], check=True, capture_output=True)
    subprocess.run([*git, "-C", str(root), "add", "README.md"], check=True, capture_output=True)
    subprocess.run([*git, "-C", str(root), "commit", "-m", "readme"], check=True, capture_output=True)
    service = _service(tmp_path, "dirty")
    clean = service.configure_project("改过的仓库", [str(root)])
    assert clean["worktree_status"] == "worktree"
    (root / "README.md").write_text("version-two\n", encoding="utf-8")
    (root / "app.py").write_text("print('working')\n", encoding="utf-8")
    package = service.bootstrap("codex:before-reconfigure", "codex", str(root), project="改过的仓库")["package"]
    work_line = next(line for line in package.splitlines() if line.startswith("工作区："))
    assert Path(work_line.split("：", 1)[1]).resolve() == root.resolve()
    again = service.configure_project("改过的仓库", [str(root)])
    assert again["worktree_status"] == "original"
    package = service.bootstrap("codex:dirty", "codex", str(root), project="改过的仓库")["package"]
    work_line = next(line for line in package.splitlines() if line.startswith("工作区："))
    prescribed = Path(work_line.split("：", 1)[1].strip())
    assert prescribed.resolve() == root.resolve()
    assert (prescribed / "README.md").read_text(encoding="utf-8") == "version-two\n"
    assert (prescribed / "app.py").read_text(encoding="utf-8") == "print('working')\n"


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


def test_elapsed_seconds_do_not_count_as_a_new_failure_outcome(tmp_path):
    service = _service(tmp_path)
    folder = tmp_path / "app"
    folder.mkdir()
    _prompt(service, "time", folder, "同一失败次数多了耗时")
    route = "pytest tests/test_app.py::test_parse -q"
    for evidence in ("10 failed", "10 failed, took 12s", "10 failed"):
        saved = service.save("codex:time", [{
            "op": "attempt", "route": route, "evidence": evidence, "productive": False,
        }])
        assert saved["ok"] is True
    summary = _load(service, "codex:time")[0]
    assert summary.status == "已暂停"
    assert summary.header["stall"]["consecutive"] == 3


def test_unrelated_agreement_does_not_accept_another_question(tmp_path):
    service = _service(tmp_path)
    folder = tmp_path / "app"
    folder.mkdir()
    service.configure_project("按钮", [str(folder)])
    _prompt(service, "agree", folder, "同意把按钮改成蓝色")
    assert service.save("codex:agree", [{"op": "understanding", "goal": "完成本次任务",
        "scope": "本次需求", "deliverables": "任务结果", "done": "有证据可核对"}])["ok"]
    _prompt(service, "agree", folder, "同意把按钮改成蓝色")
    package = service.bootstrap("codex:agree", "codex", str(folder), project="按钮")["package"]
    assert "决定人只是记录" in package
    rejected = service.save("codex:agree", [{
        "op": "confirm", "question": "Q1",
        "source": "U2", "why": "按钮颜色不是这个问题", "tradeoffs": "不能借用同意两个字",
        "decided_by": "用户",
    }])
    assert rejected["ok"] is False
    assert any("这个具体问题" in item["reason"] for item in rejected["rejected"])
    _prompt(service, "agree", folder, "不同意 Q1 的建议默认")
    refused = service.save("codex:agree", [{
        "op": "confirm", "question": "Q1",
        "source": "U3", "why": "这句是否定", "tradeoffs": "不能把否定当成同意",
        "decided_by": "用户",
    }])
    assert refused["ok"] is False
    _prompt(service, "agree", folder, "同意 Q1 的建议默认")
    accepted = service.save("codex:agree", [{
        "op": "confirm", "question": "Q1",
        "source": "U4", "why": "这句原话指向 Q1", "tradeoffs": "不另写答案",
        "decided_by": "用户",
    }])
    assert accepted["ok"] is True


def test_user_check_file_stall_threshold_wins_over_generated_file(tmp_path):
    service = _service(tmp_path)
    folder = tmp_path / "app"
    folder.mkdir()
    service.configure_project("阈值", [str(folder)])
    (folder / ".tbrain" / "check.yaml").write_text(
        "stall:\n  unproductive_attempts: 3\nchecks: []\n", encoding="utf-8")
    (folder / "tbrain.check.yaml").write_text(
        "stall:\n  unproductive_attempts: 9\nchecks: []\n", encoding="utf-8")
    _prompt(service, "limit", folder, "用户文件里的阈值应该生效")
    route = "same failing command"
    for _ in range(4):
        assert service.save("codex:limit", [{
            "op": "attempt", "route": route, "evidence": "still red", "productive": False,
        }])["ok"]
    assert _load(service, "codex:limit")[0].status != "已暂停"
