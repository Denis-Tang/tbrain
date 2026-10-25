"""迁移报告与正式迁移测试：旧数据只读，新 vault 写入 + 幂等。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tbrain.service import TbrainService

SESSION_A = """---
kind: session
title: 只读调研 Tbrain 迁移
session_id: 09f13e57-aac8-4378-b88b-e92945e26b00
agent: Claude
project: proj-1
shared_brain: true
updated: '2026-09-29T16:21:50+00:00'
created: '2026-09-29T15:54:04+00:00'
---

用户要求评估迁移方案。结论：优先 Markdown，旧知识不迁移。
"""

SESSION_B = """---
kind: session
title: 独立会话：整理迁移报告
session_id: 01a0dc3d-9b4d-7241-8363-f70c5552d14e
agent: Codex
project: ''
created: '2026-09-28T03:29:27+00:00'
updated: '2026-09-28T03:29:27+00:00'
---

独立会话正文，保留原文。
"""

PROJECT = """---
kind: project
title: 项目A
project: proj-1
project_id: proj-1
parent_project_id: ''
status: active
goal: ''
created: '2026-09-01T00:00:00+00:00'
updated: '2026-09-01T00:00:00+00:00'
---

项目 A 备注。
"""

OBJECT_META = {
    "kind": "object", "title": "安装 Node.js", "project": "",
    "location": "C:\\Program Files\\nodejs", "current_state": "Node 24 已安装",
    "evidence": "node --version 通过", "verified": True,
    "updated": "2026-10-02T03:08:27+00:00", "created": "2026-09-28T00:00:00+00:00",
}


@pytest.fixture()
def old_data(tmp_path, monkeypatch):
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    work = tmp_path / "work"
    vault.mkdir()
    work.mkdir()
    monkeypatch.setenv("TBRAIN_HOME", str(home))
    monkeypatch.setenv("TBRAIN_INSTALL_ROOT", str(tmp_path / "install"))
    service = TbrainService(home, vault=vault)
    service.settings.update({"vault_path": str(vault), "ntp_enabled": False})
    service.reset_vault()

    old_home = tmp_path / "old"
    old_home.mkdir()
    old_vault = tmp_path / "old-vault"
    sessions = old_vault / "会话总结"
    (sessions / "Claude" / "2026" / "09").mkdir(parents=True)
    (sessions / "Codex" / "2026" / "09").mkdir(parents=True)
    (sessions / "Claude" / "2026" / "09" / "s1.md").write_text(SESSION_A, encoding="utf-8")
    (sessions / "Codex" / "2026" / "09" / "s2.md").write_text(SESSION_B, encoding="utf-8")
    project_dir = old_vault / "项目" / "项目A"
    project_dir.mkdir(parents=True)
    (project_dir / "项目.md").write_text(PROJECT, encoding="utf-8")
    knowledge = old_vault / "知识"
    knowledge.mkdir()
    (knowledge / "全局提示词.md").write_text("- 用户偏好：中文、先测试\n", encoding="utf-8")
    (old_home / "settings.json").write_text(json.dumps({"vault_path": str(old_vault)}),
                                            encoding="utf-8")
    db = old_home / "index-abc.sqlite3"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE notes (path TEXT, title TEXT, kind TEXT, project TEXT, body TEXT, metadata TEXT)")
    con.execute("CREATE TABLE observations (at TEXT, sessions INTEGER, memories INTEGER)")
    con.execute("CREATE TABLE state (key TEXT, value TEXT)")
    con.executemany("INSERT INTO notes VALUES (?,?,?,?,?,?)", [
        ("会话总结/Claude/2026/09/s1.md", "只读调研 Tbrain 迁移", "session", "proj-1", "x", "{}"),
        ("会话总结/Codex/2026/09/s2.md", "独立会话：整理迁移报告", "session", "", "x", "{}"),
        ("项目/项目A/项目.md", "项目A", "project", "proj-1", "x", "{}"),
        ("对象/安装 Node.md", "安装 Node.js", "object", "",
         "### 2026-10-02 · 安装 | - 安装 Node 24", json.dumps(OBJECT_META, ensure_ascii=False)),
        ("知识/k.md", "知识", "memory", "", "x", "{}"),
        ("技能/s.md", "技能", "skill", "", "x", "{}"),
        ("项目/决策.md", "决策", "decision", "proj-1", "x", "{}"),
    ])
    con.execute("INSERT INTO observations VALUES ('2026-09-28', 21, 52)")
    con.execute("INSERT INTO state VALUES ('project_paths', ?)",
                (json.dumps({"proj-1": ["D:\\work\\project-a"]}),))
    con.commit()
    con.close()
    return service, old_home, old_vault


def test_dry_run_reports_counts_and_plans_without_writing(old_data):
    service, old_home, old_vault = old_data
    before_old = {path: path.stat().st_mtime for path in old_vault.rglob("*") if path.is_file()}
    report = service.migrate_dry_run(str(old_home))
    assert report["mode"] == "dry-run"
    assert report["old_home_exists"] is True
    assert report["old_vault"] == str(old_vault)
    assert report["vault_files"]["会话总结"] == 2
    assert report["vault_files"]["项目卡"] == 1
    assert report["vault_files"]["全局提示词"] == 1
    assert report["note_kinds"]["session"] == 2
    assert report["note_kinds"]["object"] == 1
    assert report["old_index"]["counts"]["notes"] == 7
    counts = {item["source"]: item["count"] for item in report["import_plan"]}
    assert counts["索引 notes(kind=session) / vault 会话总结"] == 2
    assert counts["索引 notes(kind=project) / vault 项目/*/项目.md"] == 1
    assert counts["索引 notes(kind=object)"] == 1
    assert report["summary"]["会话总结"] == 2
    assert report["unmapped"]
    assert report["writes"] == "无（dry-run 只读）"
    after_old = {path: path.stat().st_mtime for path in old_vault.rglob("*") if path.is_file()}
    assert before_old == after_old
    assert not (service.vault_root() / "会话总结").exists()


def test_plan_reads_file_level_counts(old_data):
    service, old_home, _ = old_data
    from tbrain import migrate

    report = migrate.plan(old_home, service.vault_root())
    assert report["counts"]["sessions"] == 2
    assert report["counts"]["projects"] == 1
    assert report["counts"]["objects"] == 1
    assert report["counts"]["global_prompt"] is True
    assert "工具" in report["public_object_kinds"] or "安装" in report["public_object_kinds"]
    assert report["old_vault"] == str(old_home.parent / "old-vault")


def test_execute_imports_and_is_idempotent(old_data):
    service, old_home, old_vault = old_data
    new_vault = Path(service.home).parent / "new-vault"
    before_old = {path: path.stat().st_mtime for path in old_vault.rglob("*") if path.is_file()}

    report = service.migrate(str(old_home), execute=True, new_vault=new_vault)
    assert report["mode"] == "execute", report
    assert report["failed"] == []
    assert report["imported"]["sessions"] == 2
    assert report["imported"]["projects"] == 1
    assert report["imported"]["objects"] == 1
    assert report["imported"]["global_prompt"] == 1
    assert Path(report["report_path"]).is_file()

    sessions = list((new_vault / "会话总结").rglob("*.md"))
    assert len(sessions) == 2
    joined = "\n".join(path.read_text(encoding="utf-8") for path in sessions)
    assert "旧总结原文（迁移）" in joined
    assert "status: 已完成" in joined.replace("\r\n", "\n")
    assert "只读调研 Tbrain 迁移" in joined
    project_text = (new_vault / "项目" / "项目A" / "项目卡.md").read_text(encoding="utf-8")
    assert "D:\\work\\project-a" in project_text.replace("/", "\\")
    cards = list((new_vault / "公共池").rglob("*.md"))
    assert len(cards) == 1
    assert "安装 Node" in cards[0].read_text(encoding="utf-8")
    assert "Node 24 已安装" in cards[0].read_text(encoding="utf-8")
    assert "用户偏好：中文" in (new_vault / "全局提示词.md").read_text(encoding="utf-8")

    # 第二次、第三次执行：全部跳过，不重复归档
    service.migrate(str(old_home), execute=True, new_vault=new_vault)
    again = service.migrate(str(old_home), execute=True, new_vault=new_vault)
    assert again["imported"]["sessions"] == 0
    assert again["imported"]["objects"] == 0
    assert len(list((new_vault / "公共池").rglob("*.md"))) == 1
    after_old = {path: path.stat().st_mtime for path in old_vault.rglob("*") if path.is_file()}
    assert before_old == after_old


def test_dry_run_missing_old_home(tmp_path):
    service = TbrainService(tmp_path / "home", vault=tmp_path / "vault")
    report = service.migrate_dry_run(str(tmp_path / "missing"))
    assert report["old_home_exists"] is False
    assert report["errors"]
    assert report["import_plan"] == []


def test_execute_refuses_old_vault_as_target(old_data):
    service, old_home, old_vault = old_data
    report = service.migrate(str(old_home), execute=True, new_vault=old_vault)
    assert report["failed"]
    assert "不能与旧 vault 相同" in " ".join(str(item) for item in report["failed"])
