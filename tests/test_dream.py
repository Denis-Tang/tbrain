"""Dream L1：状态路由、字符预算，以及夹具库上的一次冷蒸馏。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tbrain import cli, clock, dream, handoff, session as flow
from tbrain.service import TbrainService


FIXED = clock.Now(
    utc=datetime(2026, 11, 6, 3, 45, tzinfo=timezone.utc),
    local=datetime(2026, 11, 6, 11, 45),
    timezone="Asia/Shanghai",
    calibrated=False,
    source="system",
    offset_seconds=0.0,
)


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
    service.configure_project("示例", [str(work)])
    _clear_criteria(vault, "示例")
    monkeypatch.setattr(service, "now", lambda: FIXED)
    return service, vault, work, home


def _clear_criteria(vault, project, goal="冷热记忆分开，热交接仍可用", done="冷蒸馏不改热会话"):
    path = Path(vault) / "项目" / project / "完成标准.md"
    text = path.read_text(encoding="utf-8")
    text = text.replace("（待填写：这个项目要做成什么样）", goal)
    text = text.replace("- [ ] （待填写：可以核对的完成条件）", f"- [ ] {done}")
    path.write_text(text, encoding="utf-8")


def _prompt(service, session, work, text):
    service.hook("codex", {"hook_event_name": "UserPromptSubmit", "session_id": session,
                           "cwd": str(work), "prompt": text})


def _session_bytes(vault: Path) -> dict[str, bytes]:
    root = vault / "会话总结"
    return {path.relative_to(vault).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*.md"))}


def test_clip_snippet_keeps_whole_lines_inside_budget():
    text = "\n".join(f"行{i:02d} " + ("字" * 20) for i in range(30))
    clipped = dream.clip_snippet(text, 100)
    assert len(clipped) <= 100
    assert clipped
    assert dream.clip_snippet(text, 0) == ""
    assert dream.clip_snippet("短", 10) == "短"
    assert len(dream.clip_snippet("超过预算的一整行", 4)) <= 4


def test_status_routing_ignores_anything_but_ready(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    assert dream.read_status(vault)["state"] == "pending"
    assert dream.read_status(vault)["present"] is False
    assert dream.bootstrap_snippet(vault, room=8000) == ""

    for state in ("pending", "running"):
        dream.write_status(vault, state=state, updated_at="2026-11-06T03:45:00Z")
        assert dream.bootstrap_snippet(vault, room=8000) == ""

    (vault / ".tbrain" / "dream-status.json").write_text('{"state": "later"}', encoding="utf-8")
    assert dream.read_status(vault)["state"] == "pending"
    assert dream.bootstrap_snippet(vault, room=8000) == ""

    dream.write_status(vault, state="ready", updated_at="2026-11-06T03:45:00Z", selected=1)
    assert dream.bootstrap_snippet(vault, room=8000) == ""
    index = vault / ".tbrain" / "dream" / "_index.md"
    index.parent.mkdir(parents=True, exist_ok=True)
    bullets = "\n".join(f"- [[topic-{i}]] — 第 {i} 条索引 " + ("详" * 40) for i in range(20))
    index.write_text("---\nname: _index\n---\n\n# Dream L1\n\n" + bullets + "\n", encoding="utf-8")
    snippet = dream.bootstrap_snippet(vault, room=8000)
    assert snippet.startswith("## Dream L1")
    assert len(snippet) <= dream.SNIPPET_BUDGET
    assert snippet.count("[[") <= dream.SNIPPET_MAX_LINES
    assert dream.bootstrap_snippet(vault, room=dream.SNIPPET_MIN_ROOM - 1) == ""
    tight = dream.bootstrap_snippet(vault, room=80)
    assert tight
    assert len(tight) <= 78


def test_bootstrap_unchanged_until_dream_is_ready(env):
    service, vault, work, _home = env
    _prompt(service, "open", work, "还在进行的会话")
    service.save("codex:open", [{"op": "append", "section": "decision",
                                 "items": [{"text": "进行中专有词 | 不应进入冷蒸馏"}]}])
    _prompt(service, "reader", work, "读取启动包")
    baseline = service.bootstrap("codex:reader", "codex", str(work))["package"]
    assert "## Dream L1" not in baseline
    assert service.bootstrap("codex:reader", "codex", str(work))["package"] == baseline

    dream.write_status(vault, state="pending", updated_at="2026-11-06T03:45:00Z")
    assert service.bootstrap("codex:reader", "codex", str(work))["package"] == baseline
    dream.write_status(vault, state="running", updated_at="2026-11-06T03:45:00Z")
    assert service.bootstrap("codex:reader", "codex", str(work))["package"] == baseline
    dream.status_path(vault).unlink()
    assert service.bootstrap("codex:reader", "codex", str(work))["package"] == baseline
    assert "dream" not in service.handoff_pack("示例")


def test_fixture_dream_run_reaches_ready_and_budgets_snippet(env):
    service, vault, work, home = env
    _prompt(service, "done", work, "收尾存储约定")
    saved = service.save("codex:done", [
        {"op": "replace", "section": "target", "text": "把冷热记忆分开并保持热交接可用"},
        {"op": "append", "section": "decision", "items": [
            {"text": "保持本地存储 | 用户要求"},
            {"text": "测试必须设置 DATA_DIR | 早期约束"},
            {"text": "调用口令 token=abcdEFGH1234 | 只留在热会话"},
        ]},
    ])
    assert saved["ok"]
    assert service.save("codex:done", [{"op": "set_status", "status": "已完成", "supersede": ["K2"]}])["ok"]
    summary, path = flow.load_session(service, "codex:done")
    summary.pending.append("未校验：把夜间失败的命令原文留下")
    flow.write_session(service, summary, path)

    _prompt(service, "cut", work, "会话中断前的目标")
    assert service.save("codex:cut", [{"op": "append", "section": "goal",
                                       "items": [{"text": "把中断目标写进冷层"}]}])["ok"]
    cut, cut_path = flow.load_session(service, "codex:cut")
    cut.header["status"] = "已中断"
    flow.write_session(service, cut, cut_path)

    _prompt(service, "open", work, "还在进行的会话")
    service.save("codex:open", [{"op": "append", "section": "decision",
                                 "items": [{"text": "进行中专有词 | 不应进入冷蒸馏"}]}])
    other_work = work.parent / "other-work"
    other_work.mkdir()
    service.configure_project("别的", [str(other_work)])
    _clear_criteria(vault, "别的", "另一个项目自己的目标", "只蒸馏自己的会话")
    _prompt(service, "other", other_work, "另一个项目")
    service.save("codex:other", [{"op": "append", "section": "decision",
                                  "items": [{"text": "其他项目专有词 | 这次不蒸馏"}]}])
    service.save("codex:other", [{"op": "set_status", "status": "已完成"}])

    _prompt(service, "reader", work, "读取启动包")
    before = service.bootstrap("codex:reader", "codex", str(work))["package"]
    assert service.bootstrap("codex:reader", "codex", str(work))["package"] == before
    resumed = service.resume("codex:reader", "codex:done")
    assert "### Dream L1" not in resumed["handoff"]
    assert "DATA_DIR" not in resumed["handoff"]

    filtered = service.dream_run(project="示例")
    assert filtered["ok"] is True and filtered["state"] == "ready"
    wiki = "\n".join(path.read_text(encoding="utf-8") for path in (vault / ".tbrain" / "dream").glob("*.md"))
    assert "其他项目专有词" not in wiki
    assert "进行中专有词" not in wiki

    bodies = _session_bytes(vault)
    result = service.dream_run()
    assert result["ok"] is True
    assert result["state"] == "ready"
    assert result["selected"] == 3
    assert dream.read_status(vault)["state"] == "ready"
    assert _session_bytes(vault) == bodies
    assert "abcdEFGH1234" in path.read_text(encoding="utf-8")

    project_file = vault / ".tbrain" / "dream" / "proj-示例.md"
    text = project_file.read_text(encoding="utf-8")
    active, retired = text.split("## 已退役", 1)
    assert "保持本地存储" in active
    assert "DATA_DIR" not in active
    assert "DATA_DIR" in retired
    assert "abcdEFGH1234" not in text
    assert "[redacted]" in text
    assert "把中断目标写进冷层" in text
    pending = (vault / ".tbrain" / "dream" / "pending.md").read_text(encoding="utf-8")
    assert "把夜间失败的命令原文留下" in pending
    assert "未校验" in pending
    dream_tree = "\n".join(item.read_text(encoding="utf-8")
                           for item in (vault / ".tbrain" / "dream").rglob("*") if item.is_file())
    assert "进行中专有词" not in dream_tree
    manifest = json.loads((vault / ".tbrain" / "dream" / "_manifest.json").read_text(encoding="utf-8"))
    ids = {item["session_id"] for item in manifest["sessions"]}
    assert "codex:done" in ids and "codex:cut" in ids and "codex:other" in ids
    assert "codex:open" not in ids and "codex:reader" not in ids

    package = service.bootstrap("codex:reader", "codex", str(work))["package"]
    assert package.startswith(before)
    snippet = package[len(before):].lstrip("\n")
    assert snippet.startswith("## Dream L1")
    assert len(snippet) <= dream.SNIPPET_BUDGET
    assert "DATA_DIR" not in snippet
    assert "abcdEFGH1234" not in snippet
    assert len(package) <= flow.HOST_BUDGETS["codex"]
    assert "[[proj-示例]]" in snippet
    pack = service.handoff_pack("示例")
    assert pack["dream"].startswith("## Dream L1")
    assert len(pack["dream"]) <= dream.SNIPPET_BUDGET

    found = service.search("保持本地存储", "codex:reader")
    assert found["results"]
    assert any(item["path"].endswith("proj-示例.md") for item in found["dream"])
    secret = service.search("abcdEFGH1234", "codex:reader")
    assert secret["results"]
    assert "dream" not in secret

    again = service.resume("codex:reader", "codex:done")
    assert "### Dream L1" in again["handoff"]
    assert again["handoff"].index("### 决策") < again["handoff"].index("### Dream L1")
    bodies = _session_bytes(vault)

    ops = {
        "ops": [
            {"op": "upsert", "slug": "old-data-dir", "description": "旧测试方法",
             "body": "需要 DATA_DIR 才能跑测试。"},
            {"op": "upsert", "slug": "test-runner", "description": "当前如何跑测试",
             "body": "当前版本直接 pytest，不再需要 DATA_DIR。\n见 [[proj-示例]]。",
             "sources": ["[[收尾]]"]},
            {"op": "retire", "slug": "old-data-dir", "replaced_by": "test-runner",
             "note": "后一会话取消了该环境变量"},
            {"op": "digest_pending", "into": "test-runner",
             "text": "未校验：把夜间失败的命令原文留下"},
        ]
    }
    ops_path = home / "dream-ops.json"
    ops_path.write_text(json.dumps(ops, ensure_ascii=False), encoding="utf-8")
    applied = service.dream_run(ops_path=str(ops_path))
    assert applied["ops_applied"] == 4
    assert dream.read_status(vault)["state"] == "ready"
    assert _session_bytes(vault) == bodies
    runner = (vault / ".tbrain" / "dream" / "test-runner.md").read_text(encoding="utf-8")
    assert "直接 pytest" in runner
    assert "把夜间失败的命令原文留下" in runner
    stub = (vault / ".tbrain" / "dream" / "old-data-dir.md").read_text(encoding="utf-8")
    assert "MOVED TO [[test-runner]]" in stub
    index = (vault / ".tbrain" / "dream" / "_index.md").read_text(encoding="utf-8")
    assert "[[test-runner]]" in index
    assert "[[old-data-dir]]" not in index
    assert not (vault / ".tbrain" / "dream" / "pending.md").exists()

    replayed = service.dream_run()
    assert replayed["ops_applied"] == 4
    assert "直接 pytest" in (vault / ".tbrain" / "dream" / "test-runner.md").read_text(encoding="utf-8")
    assert _session_bytes(vault) == bodies

    bad = home / "bad-ops.json"
    bad.write_text('{"ops": [{"op": "imagine"}]}', encoding="utf-8")
    with pytest.raises(dream.DreamError):
        service.dream_run(ops_path=str(bad))
    assert dream.read_status(vault)["state"] == "ready"
    assert "直接 pytest" in (vault / ".tbrain" / "dream" / "test-runner.md").read_text(encoding="utf-8")

    assert cli.main(["--home", str(home), "dream", "status"]) == 0
    assert cli.main(["--home", str(home), "dream", "run"]) == 0
    assert dream.read_status(vault)["state"] == "ready"
    assert _session_bytes(vault) == bodies

    lock = vault / ".tbrain" / "dream.lock"
    lock.write_text("busy\n", encoding="utf-8")
    with pytest.raises(dream.DreamBusy):
        service.dream_run()
    assert lock.is_file()
    assert dream.read_status(vault)["state"] == "ready"


def test_superseded_marker_matches_handoff():
    assert handoff.decision_active("保持本地存储 | 用户要求")
    assert not handoff.decision_active("测试必须设置 DATA_DIR | 早期约束（被取代）")
