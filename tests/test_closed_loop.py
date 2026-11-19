"""Closed-loop minimum cut: worktree, contract, reasons, wrap-up, local check."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from tbrain import cli
from tbrain.check import decision_missing_why, run_check
from tbrain.service import TbrainService
from tbrain.settings import SettingsStore


def _service(tmp_path):
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    service = TbrainService(home, vault=vault)
    service.settings.update({"vault_path": str(vault), "ntp_enabled": False, "timezone": "Asia/Shanghai"})
    return service, home, vault


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TBRAIN_HOME", str(tmp_path / "home"))
    return _service(tmp_path)


def test_local_check_skips_local_test_data_but_checks_project_notes(tmp_path):
    scratch = tmp_path / '.t'
    scratch.mkdir()
    (scratch / 'fixture.md').write_text('### K1\n测试夹具缺少理由\n', encoding='utf-8')
    assert run_check(tmp_path)['ok']
    (tmp_path / 'decision.md').write_text('### K1\n真实项目笔记缺少理由\n', encoding='utf-8')
    assert not run_check(tmp_path)['ok']


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
        "理由：避免同时改两份代码\n取舍：多一个目录，换来隔离\n决定人：用户\n",
        encoding="utf-8",
    )
    assert run_check(root)["ok"] is True

    marker = tmp_path / "marker"
    marker.mkdir()
    (marker / "choice.md").write_text("<!-- tbrain:decision -->\n改用工作区\n", encoding="utf-8")
    marked = decision_missing_why(marker)
    assert marked and marked[0]["line"] == 1
    (marker / "choice.md").write_text(
        "<!-- tbrain:decision -->\n理由：只留一处工作区\n取舍：不在主目录里堆副本\n决定人：用户\n",
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


def test_cli_project_add_and_check_exit_code(env, capsys):
    service, home, vault = env
    folder = vault.parent / "cli-app"
    folder.mkdir()
    code = cli.main(["project", "add", "--name", "命令行项目", "--path", str(folder), "--home", str(home)])
    captured = capsys.readouterr()
    assert code == 0, captured.out
    payload = json.loads(captured.out)
    assert payload["paths"] == [str(folder)]
    assert not (folder / ".git").exists()
    assert SettingsStore(home).load().vault_path

    note = folder / "NOTES.md"
    note.write_text("### K1\n改用工作区\n", encoding="utf-8")
    failed = cli.main(["check", "--path", str(folder), "--home", str(home)])
    failed_out = capsys.readouterr().out
    assert failed == 1
    assert json.loads(failed_out)["ok"] is False
    note.write_text(
        "### K1\n改用工作区 | 一个项目一处工作区\n"
        "理由：避免同时改两份代码\n取舍：多一个目录\n决定人：用户\n",
        encoding="utf-8",
    )
    assert cli.main(["check", "--path", str(folder), "--home", str(home)]) == 0
    assert service.store.project("命令行项目")["name"] == "命令行项目"


@pytest.mark.parametrize("option", ["on", "off", None])
def test_cli_project_set_only_changes_explicit_switch(env, capsys, option):
    service, home, _ = env
    service.configure_project("命令行项目", [], allow_child=True)
    service.store.set_project_read_standalone("命令行项目", option != "on")
    before = service.store.project("命令行项目")
    card = service.store.project_card_path("命令行项目")
    before_bytes = card.read_bytes()
    arguments = ["project", "set", "--name", "命令行项目", "--home", str(home)]
    if option is not None:
        arguments += ["--read-standalone", option]
    assert cli.main(arguments) == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved["ok"] is True
    after = service.store.project("命令行项目")
    assert after["read_standalone"] is (option != "off")
    assert {key: value for key, value in after.items() if key != "read_standalone"} == {
        key: value for key, value in before.items() if key != "read_standalone"
    }
    if option is None:
        assert card.read_bytes() == before_bytes
    assert cli.main(["project", "set", "--name", "不存在", "--home", str(home)]) == 1
    assert "项目不存在" in json.loads(capsys.readouterr().out)["error"]


def test_cli_project_ids_applies_the_preview_and_keeps_original_bytes(env, tmp_path, capsys):
    service, home, _ = env
    card = service.store.project_card_path("旧项目")
    card.parent.mkdir(parents=True)
    before = b"\xef\xbb\xbf" + "---\r\nname: 旧项目\r\npaths: []\r\nx_unknown: keep\r\n---\r\n\r\n保留原文。\r\n".encode("utf-8")
    card.write_bytes(before)
    assert cli.main(["project", "ids", "--preview", "--home", str(home)]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert card.read_bytes() == before
    assert len(preview["assignments"]) == 1
    approved_id = preview["assignments"][0]["project_id"]
    preview_file = tmp_path / "ids-preview.json"
    preview_file.write_text(json.dumps(preview, ensure_ascii=False), encoding="utf-8")
    assert cli.main(["project", "ids", "--apply", str(preview_file), "--backup", str(tmp_path / "backup"),
                     "--home", str(home)]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["updated"] == 1
    assert service.store.project("旧项目")["project_id"] == approved_id
    assert any(path.read_bytes() == before for path in Path(applied["backup"]).rglob("项目卡.md"))
    restored = b"".join(line for line in card.read_bytes().splitlines(keepends=True)
                        if not line.startswith(b"project_id:"))
    assert restored == before


@pytest.fixture()
def cold_export(tmp_path):
    home, vault = tmp_path / "cold-home", tmp_path / "cold-vault"
    home.mkdir()
    project = vault / "项目" / "导出项目"
    project.mkdir(parents=True)
    (home / "settings.json").write_text(json.dumps({"vault_path": str(vault)}), encoding="utf-8")
    (project / "项目卡.md").write_text("---\nname: 导出项目\npaths: []\n---\n", encoding="utf-8")
    (project / "项目提示词.md").write_text(
        "<!-- owner:user -->\n用户补充说明。\n<!-- /owner:user -->\n"
        "<!-- owner:model -->\n## 最终目标\n保留 Tbrain 和 session_id 两个业务术语。\n"
        "## 硬规则\n- 导出使用 UTF-8 无 BOM（来源：U1）\n"
        "## 当前阶段\n验证只读导出。\n## 关键路径和命令\n- 验证命令：python -m pytest -q\n"
        "<!-- /owner:model -->\n", encoding="utf-8")
    return home, vault


@pytest.mark.parametrize("to_file", [False, True])
def test_cli_export_is_cold_and_writes_plain_utf8(cold_export, tmp_path, capsys, monkeypatch, to_file):
    home, vault = cold_export
    monkeypatch.setattr(cli, "_service", lambda *_args: pytest.fail("export must not initialize a service"))
    before = {path.relative_to(vault): path.read_bytes() for path in vault.rglob("*") if path.is_file()}
    output = tmp_path / "AGENTS.md"
    arguments = ["export", "--project", "导出项目", "--home", str(home)]
    if to_file:
        arguments += ["--out", str(output)]
    assert cli.main(arguments) == 0
    printed = capsys.readouterr().out
    if to_file:
        assert json.loads(printed)["path"] == str(output)
        assert not output.read_bytes().startswith(b"\xef\xbb\xbf")
        text = output.read_text(encoding="utf-8")
    else:
        text = printed
        assert not text.startswith(("{", '"'))
    assert "Tbrain 和 session_id" in text
    assert "UTF-8 无 BOM" in text
    assert "owner:" not in text
    assert {path.relative_to(vault): path.read_bytes() for path in vault.rglob("*") if path.is_file()} == before
    assert {path.name for path in home.iterdir()} == {"settings.json"}


@pytest.mark.parametrize("case,message", [("no-settings", "知识库路径"), ("missing-project", "项目"),
                                         ("vault-output", "知识库外")])
def test_cli_export_rejects_missing_input_and_vault_output(cold_export, capsys, case, message):
    home, vault = cold_export
    card = vault / "项目" / "导出项目" / "项目提示词.md"
    before = card.read_bytes()
    arguments = ["export", "--project", "导出项目", "--home", str(home)]
    if case == "no-settings":
        (home / "settings.json").write_text("{}", encoding="utf-8")
    elif case == "missing-project":
        arguments[2] = "不存在的项目"
    else:
        arguments += ["--out", str(card)]
    assert cli.main(arguments) == 1
    assert message in json.loads(capsys.readouterr().out)["error"]
    assert card.read_bytes() == before
