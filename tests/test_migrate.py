"""迁移 dry-run 报告测试：只读旧数据目录，不写任何旧/新文件。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tbrain.service import TbrainService


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
    (old_vault / "会话总结" / "项目A" / "2026" / "09").mkdir(parents=True)
    (old_vault / "会话总结" / "项目A" / "2026" / "09" / "a.md").write_text("# 会话", encoding="utf-8")
    (old_vault / "项目" / "项目A").mkdir(parents=True)
    (old_vault / "项目" / "项目A" / "项目.md").write_text("项目 A", encoding="utf-8")
    (old_vault / "知识").mkdir()
    (old_vault / "知识" / "全局提示词.md").write_text("- 用户偏好", encoding="utf-8")
    (old_home / "settings.json").write_text(json.dumps({"vault_path": str(old_vault)}),
                                            encoding="utf-8")
    db = old_home / "index-abc.sqlite3"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE notes (path TEXT, title TEXT, kind TEXT, body TEXT)")
    con.execute("CREATE TABLE observations (at TEXT, sessions INTEGER, memories INTEGER)")
    con.executemany("INSERT INTO notes VALUES (?,?,?,?)", [
        ("会话总结/项目A/a.md", "A", "session", "x"),
        ("会话总结/项目A/b.md", "B", "session", "x"),
        ("项目/项目A/项目.md", "项目A", "project", "x"),
        ("项目/独立项目/对象.md", "对象", "object", "x"),
        ("知识/k.md", "知识", "memory", "x"),
        ("技能/s.md", "技能", "skill", "x"),
        ("项目/决策.md", "决策", "decision", "x"),
    ])
    con.execute("INSERT INTO observations VALUES ('2026-09-28', 21, 52)")
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
    assert report["vault_files"]["会话总结"] == 1
    assert report["vault_files"]["项目卡"] == 1
    assert report["vault_files"]["全局提示词"] == 1
    assert report["note_kinds"] == {"session": 2, "project": 1, "object": 1, "memory": 1,
                                    "skill": 1, "decision": 1}
    assert report["old_index"]["counts"]["notes"] == 7
    plan = {item["source"]: item for item in report["import_plan"]}
    counts = {item["source"]: item["count"] for item in report["import_plan"]}
    assert counts["索引 notes(kind=session) / vault 会话总结"] == 2
    assert counts["索引 notes(kind=project) / vault 项目/*/项目.md"] == 1
    assert counts["索引 notes(kind=object)"] == 1
    assert report["summary"]["会话总结"] == 2
    assert report["summary"]["项目卡"] == 1
    assert report["summary"]["公共池对象"] == 1
    assert report["unmapped"]
    assert report["writes"] == "无（dry-run 只读）"
    after_old = {path: path.stat().st_mtime for path in old_vault.rglob("*") if path.is_file()}
    assert before_old == after_old
    assert not (service.vault_root() / "会话总结").exists()  # dry-run 不写新 vault


def test_dry_run_missing_old_home(tmp_path):
    service = TbrainService(tmp_path / "home", vault=tmp_path / "vault")
    report = service.migrate_dry_run(str(tmp_path / "missing"))
    assert report["old_home_exists"] is False
    assert report["errors"]
    assert report["import_plan"] == []
