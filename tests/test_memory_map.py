import hashlib
import json
from pathlib import Path

import pytest

from tbrain import cli, worktree
from tbrain.service import TbrainService
from tbrain.store import render_note, split_front_matter


def git(repo, *args):
    result = worktree._git(repo, list(args))
    assert result.returncode == 0, result.stderr
    return result.stdout


def repository(tmp_path, linked):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "--initial-branch=main")
    git(repo, "commit", "--allow-empty", "-m", "main")
    git(repo, "checkout", "-b", "feature/合成")
    git(repo, "commit", "--allow-empty", "-m", "own work", "-m", "理由：保留用户原话\n功能：完整记录多行输入")
    git(repo, "checkout", "main")
    git(repo, "branch", "tbrain/合成占位", "main")
    git(repo, "branch", "other/empty", "main")
    git(repo, "worktree", "add", str(linked), "feature/合成")
    git(repo, "worktree", "add", "--detach", str(tmp_path / "detached"), "main")
    git(repo, "reflog", "expire", "--expire=now", "refs/heads/feature/合成")
    return repo


def hashes(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("legacy", [False, True])
def test_map_cli_is_bounded_deterministic_and_preserves_git(tmp_path, monkeypatch, capsys, legacy):
    repo = repository(tmp_path, tmp_path / "linked")
    service = TbrainService(tmp_path / "home", vault=tmp_path / "vault")
    service.settings.save({"vault_path": str(service.vault_root()), "ntp_enabled": False, "task_mode": False})
    service.store.save_project("地图项目", [] if legacy else [str(repo)])
    service.store.record_execution("地图项目", {"worktree_status": "worktree", "worktree": str(tmp_path / "linked")})
    card = service.store.project_card_path("地图项目")
    data, body = split_front_matter(card.read_text(encoding="utf-8"))
    if legacy:
        data.pop("project_id")
        card.write_text(render_note(data, body), encoding="utf-8")
    original_paths = data["paths"]

    def forbidden(*args, **kwargs):
        raise AssertionError("map must not prepare or initialize a workspace")

    monkeypatch.setattr(worktree, "prepare", forbidden)
    monkeypatch.setattr(worktree, "ensure", forbidden)
    before = [git(repo, *args) for args in (("branch", "--list"), ("worktree", "list", "--porcelain"), ("status", "--porcelain"))]
    arguments = ["--home", str(service.home), "map", "--project", "地图项目"]
    if legacy:
        arguments += ["--repo", str(repo)]
    assert cli.main(arguments) == 0
    result = json.loads(capsys.readouterr().out)
    output = Path(result["path"])
    text = output.read_text(encoding="utf-8")
    assert result["bytes"] == len(text.encode("utf-8")) <= 12288
    assert "分支总数：4；worktree 总数：3" in text
    assert "tbrain/*：折叠 1 条" in text
    assert "feature/合成 @ " in text and "ahead 1 / behind 0" in text
    assert "本机创建：未记录；创建来源：未记录" in text
    assert "理由：保留用户原话" in text and "功能：完整记录多行输入" in text
    assert "提交名称：own work" in text and "main @ " in text and "other/empty @ " in text
    assert "程序生成物" in text and "连线不表示分支祖先" in text
    assert "未提交项" in text and "## 提交关系" in text
    assert ('project_id: "未回填"' if legacy else data["project_id"]) in text
    assert "[完成标准](<完成标准.md>)" in text
    assert cli.main(arguments) == 0
    capsys.readouterr()
    assert output.read_text(encoding="utf-8") == text
    after = [git(repo, *args) for args in (("branch", "--list"), ("worktree", "list", "--porcelain"), ("status", "--porcelain"))]
    assert before == after
    assert service.store.project("地图项目")["paths"] == original_paths
    service.store.save_project("新地图项目", original_paths, rename_from="地图项目")
    assert (service.store.project_dir("新地图项目") / "地图.md").read_text(encoding="utf-8") == text


@pytest.mark.parametrize("kind,relative", [("codex", ".codex/worktrees/合成"), ("Tbrain", ".tbrain-worktrees/repo/合成")])
def test_worktree_audit_cli_reads_only_and_marks_heuristics(tmp_path, monkeypatch, capsys, kind, relative):
    linked = tmp_path / relative
    repo = repository(tmp_path, linked)
    home, vault = tmp_path / "home", tmp_path / "vault"
    home.mkdir()
    card = vault / "项目" / "匹配项目" / "项目卡.md"
    card.parent.mkdir(parents=True)
    card.write_text(render_note({"name": "匹配项目", "worktree": str(linked), "paths": []}), encoding="utf-8")
    (home / "settings.json").write_text(json.dumps({"vault_path": str(vault)}), encoding="utf-8")
    before = hashes(vault), hashes(home)

    def forbidden(*args, **kwargs):
        raise AssertionError("audit must not construct a service")

    monkeypatch.setattr(cli, "_service", forbidden)
    assert cli.main(["--home", str(home), "worktree", "audit", "--repo", str(repo)]) == 0
    result = json.loads(capsys.readouterr().out)
    row = next(row for row in result["worktrees"] if Path(row["path"]).resolve() == linked.resolve())
    assert row["creator_guess"] == kind and row["has_unique_commits"] is True
    assert row["projects"] == ["匹配项目"] and row["fixture_name_hint"] is True
    assert len(result["worktrees"]) == 3
    assert "不是创建证明" in result["heuristic"] and "清理" in result["heuristic"]
    assert (hashes(vault), hashes(home)) == before
