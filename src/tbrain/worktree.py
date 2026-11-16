"""One git worktree per project.

The worktree lives beside the repository, not inside it:

    ../.tbrain-worktrees/<repo-directory>/<project>

``git worktree add`` refuses a path ignored by ``.gitignore``, and a worktree
inside the checkout shows up as untracked files. The sibling path avoids both.
"""

from __future__ import annotations

import os
import json
import re
import subprocess
from pathlib import Path

from . import naming
from .check import write_starter_config

CONVENTION = "../.tbrain-worktrees/<repo>/<project>"
_DIRNAME = ".tbrain-worktrees"
MAP_MAX_BYTES = 12 * 1024


def slug(name: str) -> str:
    text = naming.sanitize_filename(name).replace(" ", "-")
    return text or "project"


def convention_for(repo: Path, name: str) -> str:
    return f"../{_DIRNAME}/{Path(repo).name}/{slug(name)}"


def worktree_path(repo: Path, name: str) -> Path:
    root = Path(repo).resolve()
    return root.parent / _DIRNAME / root.name / slug(name)


def worktree_doc(name: str) -> str:
    text = slug(name)
    return (
        "# 工作区\n\n"
        "一个项目只使用一处 git 工作区。\n\n"
        f"约定：../.tbrain-worktrees/<仓库目录名>/{text}\n"
        f"分支：tbrain/{text}\n\n"
        "在这一处工作区里改代码。不要另开副本。\n"
    )


def prepare(name: str, paths: list[str]) -> dict:
    """Init git if needed and reserve the project's worktree. Never raises."""
    result = {
        "git_root": "",
        "worktree": "",
        "worktree_convention": CONVENTION,
        "worktree_branch": "",
        "worktree_status": "unbound",
        "worktree_error": "",
        "check_config": "",
    }
    if not paths:
        return result
    try:
        ensured = ensure(name, Path(str(paths[0])))
    except Exception as exc:
        result["worktree_status"] = "error"
        result["worktree_error"] = str(exc)
        return result
    result.update(ensured)
    return result


def ensure(name: str, folder: Path) -> dict:
    folder = Path(folder)
    if folder.exists() and not folder.is_dir():
        return _error("项目路径不是目录")
    folder.mkdir(parents=True, exist_ok=True)
    folder = folder.resolve()
    branch = f"tbrain/{slug(name)}"
    created = False
    toplevel = _toplevel(folder)
    if not toplevel:
        init = _git(folder, ["init"])
        if init.returncode != 0:
            return _error((init.stderr or init.stdout or "git init 失败").strip(), folder)
        created = True
        toplevel = _toplevel(folder) or folder
    root = Path(toplevel).resolve()
    config_path = write_starter_config(folder)
    doc_path = folder / ".tbrain" / "WORKTREE.md"
    if not doc_path.exists():
        doc_path.parent.mkdir(parents=True, exist_ok=True)
        doc_path.write_text(worktree_doc(name), encoding="utf-8")
    destination = worktree_path(root, name)
    if not _has_head(root) and _has_user_files(folder):
        return {
            "git_root": str(root),
            "worktree": str(folder),
            "worktree_convention": convention_for(root, name),
            "worktree_branch": branch,
            "worktree_status": "original",
            "worktree_error": (
                "项目文件还没有进入用户自己的提交，执行位置仍是原目录。"
                "没有自动提交这些文件，隔离还不完整。"
            ),
            "check_config": str(config_path),
            "worktree_limitation": "",
        }
    payload = {
        "git_root": str(root),
        "worktree": str(destination),
        "worktree_convention": convention_for(root, name),
        "worktree_branch": branch,
        "worktree_status": "skeleton",
        "worktree_error": "",
        "check_config": str(config_path),
    }
    if created and not _has_head(root):
        added = _commit_skeleton(root, [config_path, doc_path])
        if added.returncode != 0 or not _has_head(root):
            payload["worktree_error"] = (added.stderr or added.stdout or "无法创建初始提交").strip()
            destination.parent.mkdir(parents=True, exist_ok=True)
            _write_marker(destination, name)
            return payload
    if not _has_head(root):
        payload["worktree_error"] = "仓库还没有任何提交，工作区目录已预留。"
        destination.parent.mkdir(parents=True, exist_ok=True)
        _write_marker(destination, name)
        return payload
    if _registered(root, destination):
        payload["worktree_status"] = "worktree"
        payload["worktree_error"] = ""
        return _prefer_original_when_uncommitted(payload, folder, root)
    if not _clear_reserved(destination, payload):
        return payload
    destination.parent.mkdir(parents=True, exist_ok=True)
    if _branch_exists(root, branch):
        added = _git(root, ["worktree", "add", str(destination), branch])
    else:
        added = _git(root, ["worktree", "add", "-b", branch, str(destination), "HEAD"])
    if added.returncode == 0 and _registered(root, destination):
        payload["worktree_status"] = "worktree"
        payload["worktree_error"] = ""
        return _prefer_original_when_uncommitted(payload, folder, root)
    _write_marker(destination, name)
    payload["worktree_error"] = (added.stderr or added.stdout or "git worktree add 失败").strip()
    return payload


def _error(message: str, folder: Path | None = None) -> dict:
    return {
        "git_root": str(folder or ""),
        "worktree": "",
        "worktree_convention": CONVENTION,
        "worktree_branch": "",
        "worktree_status": "error",
        "worktree_error": message,
        "check_config": "",
    }


def _clear_reserved(destination: Path, payload: dict) -> bool:
    """Remove a previous skeleton so git can create the worktree. Keep other files."""
    if not destination.exists():
        return True
    entries = list(destination.iterdir())
    if entries and {item.name for item in entries} != {"WORKTREE.md"}:
        payload["worktree_error"] = "工作区路径已有其他文件，未覆盖。"
        return False
    for item in entries:
        item.unlink()
    destination.rmdir()
    return True


def _write_marker(destination: Path, name: str) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    marker = destination / "WORKTREE.md"
    if not marker.exists():
        marker.write_text(worktree_doc(name), encoding="utf-8")


def _commit_skeleton(root: Path, files: list[Path]) -> subprocess.CompletedProcess:
    relative = []
    for path in files:
        if path.is_file():
            relative.append(path.resolve().relative_to(root.resolve()).as_posix())
    if not relative:
        return _git(root, ["commit", "--allow-empty", "-m", "tbrain: project worktree skeleton"])
    _git(root, ["add", "--", *relative])
    return _git(root, ["commit", "-m", "tbrain: project worktree skeleton"])


def _prefer_original_when_uncommitted(payload: dict, folder: Path, root: Path) -> dict:
    """Dirty or untracked project files stay the execution location.

    A warning on an empty worktree still sends the agent to the stale tree.
    Do not copy or commit those files.
    """
    pending = _uncommitted(root)
    if not pending:
        payload.pop("worktree_limitation", None)
        return payload
    payload["worktree_status"] = "original"
    payload["worktree"] = str(folder)
    payload["worktree_error"] = (
        "有未提交的改动或未跟踪文件，执行位置仍是原目录。"
        "没有复制或提交这些文件，隔离还不完整。"
    )
    payload["worktree_limitation"] = "未提交的文件没有复制到工作区，仍只在原目录：" + "、".join(pending[:8])
    return payload


def _has_user_files(folder: Path) -> bool:
    return any(item.name not in {".git", ".tbrain"} for item in folder.iterdir())


def _uncommitted(root: Path) -> list[str]:
    proc = _git(root, ["status", "--porcelain"])
    if proc.returncode != 0:
        return []
    names = []
    for line in proc.stdout.splitlines():
        text = line[3:].strip() if len(line) > 3 else ""
        if not text or text.startswith(".tbrain/"):
            continue
        names.append(text.split(" -> ")[-1])
    return names


def _has_head(root: Path) -> bool:
    return _git(root, ["rev-parse", "--verify", "HEAD"]).returncode == 0


def _toplevel(folder: Path) -> str:
    proc = _git(folder, ["rev-parse", "--show-toplevel"])
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def _branch_exists(root: Path, branch: str) -> bool:
    return _git(root, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]).returncode == 0


def _registered(root: Path, destination: Path) -> bool:
    proc = _git(root, ["-c", "core.quotepath=false", "worktree", "list", "--porcelain"])
    if proc.returncode != 0:
        return False
    try:
        target = destination.resolve()
    except OSError:
        target = destination
    for line in (proc.stdout or "").splitlines():
        if not line.startswith("worktree "):
            continue
        raw = line[len("worktree "):].strip()
        try:
            if Path(raw).resolve() == target:
                return True
        except OSError:
            continue
    return False


def _read_git(repo: Path, args: list[str]) -> str:
    result = _git(repo, args)
    if result.returncode:
        raise ValueError((result.stderr or result.stdout).strip())
    return result.stdout


def git_facts(folder: Path) -> dict:
    """Read branch and registered-worktree facts without preparing a workspace."""
    root = _toplevel(Path(folder).expanduser())
    if not root:
        raise ValueError("指定路径不是 Git 仓库")
    repo = Path(root)
    if not _branch_exists(repo, "main"):
        raise ValueError("仓库缺少 main 分支，无法比较独有提交")

    def comparison(revision):
        behind, ahead = map(int, _read_git(repo, ["rev-list", "--left-right", "--count", "main..." + revision]).split())
        return ahead, behind

    branches = []
    records = _read_git(repo, ["for-each-ref", "--format=%(refname:short)%00%(objectname)%00%(committerdate:iso-strict)%00%(committerdate:unix)%00%(contents:subject)%00%(contents:body)%00",
                              "refs/heads"])
    for record in records.removesuffix("\0\n").split("\0\n"):
        name, head, committed, timestamp, subject, body = record.split("\0")
        ahead, behind = comparison(head)
        creation = [item.split("\0", 1) for item in _read_git(repo, ["reflog", "show", "--date=iso-strict",
                    "--format=%gD%x00%gs", name]).splitlines() if "\0branch: Created from " in item]
        created, source = "", ""
        if creation:
            selector, message = creation[-1]
            created = selector.rsplit("@{", 1)[-1].removesuffix("}")
            source = message.removeprefix("branch: Created from ")
        branches.append(dict(name=name, head=head, committed=committed, timestamp=int(timestamp), ahead=ahead, behind=behind,
                             created=created, source=source, subject=subject, body=body.strip()))
    worktrees = []
    for block in _read_git(repo, ["worktree", "list", "--porcelain", "-z"]).strip("\0\n").split("\0\0"):
        fields = dict(item.split(" ", 1) if " " in item else (item, "") for item in block.split("\0"))
        head = fields["HEAD"]
        ahead, behind = comparison(head)
        committed, timestamp = _read_git(repo, ["show", "-s", "--format=%cI%x00%ct", head]).strip().split("\0")
        status = _read_git(Path(fields["worktree"]), ["status", "--porcelain"])
        worktrees.append(dict(path=fields["worktree"], head=head, branch=fields.get("branch", "").removeprefix("refs/heads/"),
                              detached="detached" in fields, ahead=ahead, behind=behind,
                              committed=committed, timestamp=int(timestamp), dirty=len(status.splitlines())))
    return {"repo": str(repo), "base": "main", "branches": branches, "worktrees": worktrees}


def render_memory_map(project: dict, repo: Path | None = None) -> str:
    paths = project.get("paths") or []
    if isinstance(paths, str):
        paths = [paths]
    source = repo or project.get("git_root") or (paths[0] if paths else "")
    if not source:
        raise ValueError("项目没有 Git 仓库路径，请指定 --repo")
    facts = git_facts(Path(source))
    branches = facts["branches"]
    shown = sorted((row for row in branches if row["ahead"] or not row["name"].startswith("tbrain/")),
                   key=lambda row: (row["timestamp"], row["name"]), reverse=True)
    worktrees = sorted(facts["worktrees"], key=lambda row: (row["timestamp"], row["path"]), reverse=True)
    grouped = sum(not row["ahead"] and row["name"].startswith("tbrain/") for row in branches)
    missing = sum(not row["created"] for row in branches)
    criteria = project.get("completion_criteria") or ""

    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ")

    history = _read_git(Path(facts['repo']), ["log", "--graph", "--all", "--max-count=30", "--format=%h %cI %s%d"]).splitlines()

    def render(visible, trees, commits):
        lines = ["---", "project_id: " + json.dumps(project.get("project_id") or "未回填", ensure_ascii=False), "---",
                 "<!-- 程序生成物；每次重新生成，用户不手改 -->", "# 记忆地图", "",
                 "仓库：" + cell(facts["repo"]), f"基准：main；分支总数：{len(branches)}；worktree 总数：{len(worktrees)}",
                 "[项目任务](项目提示词.md) · " + (f"[完成标准](<{criteria}>)" if criteria else "完成标准：未配置"),
                 "项目卡工作区状态：" + cell(project.get("worktree_status") or "") + "；路径：" + cell(project.get("worktree") or ""), "",
                 f"创建记录来自本机 reflog，可能缺失；缺失 {missing} 条，时间与来源留空，不推断。",
                 "ahead/behind 相对 main；ahead > 0 只表示有独有提交，不表示分支目的或完成程度。",
                 "功能与决定原因以提交说明原文为准；没有记录的原因不推断。",
                 "项目树按分支分类，连线不表示分支祖先；提交关系见下方 Git 原生关系图。", "",
                 "## 项目树", "```text", cell(Path(facts['repo']).name)]
        for index, row in enumerate(shown[:visible]):
            last = index == visible - 1
            prefix = "   " if last else "│  "
            lines += [("└─ " if last else "├─ ") + row['name'] + " @ " + row['head'][:8],
                      prefix + f"提交时间：{row['committed']}；ahead {row['ahead']} / behind {row['behind']}",
                      prefix + f"本机创建：{row['created'] or '未记录'}；创建来源：{row['source'] or '未记录'}",
                      prefix + "提交名称：" + row['subject'],
                      prefix + "说明原文：" + (row['body'].replace('\n', '\n' + prefix) or '未记录')]
        lines += ["```"]
        lines += ["", f"无独有提交的 tbrain/*：折叠 {grouped} 条。",
                  f"按最后提交时间折叠最旧分支：{len(shown) - visible} 条；时间不表示失效。", "",
                  "## 已登记工作树", "| 路径 | 分支或 detached | HEAD | 提交时间 | 未提交项 |", "|---|---|---|---|---:|"]
        lines += [f"| {cell(row['path'])} | {cell(row['branch'] or 'detached')} | {row['head'][:8]} | {row['committed']} | {row['dirty']} |"
                  for row in worktrees[:trees]]
        lines += ["", f"按 HEAD 提交时间折叠最旧工作树：{len(worktrees) - trees} 条。", "",
                  "## 提交关系", "Git 原生关系图，最多展示最近 30 次提交；下图行数：" + str(commits) + "。",
                  "```text", *history[:commits], "```", ""]
        return "\n".join(lines)

    visible, trees, commits = len(shown), len(worktrees), len(history)
    text = render(visible, trees, commits)
    while len(text.encode("utf-8")) > MAP_MAX_BYTES and (visible or trees or commits):
        if commits:
            commits -= 1
        elif visible:
            visible -= 1
        else:
            trees -= 1
        text = render(visible, trees, commits)
    if len(text.encode("utf-8")) > MAP_MAX_BYTES:
        raise ValueError("地图固定项目元数据已超过 12KiB")
    return text


def audit(folder: Path, projects: list[dict]) -> dict:
    facts = git_facts(folder)
    rows = []
    for tree in facts["worktrees"]:
        path, branch = Path(tree["path"]), tree["branch"]
        normalized = path.as_posix().lower()
        creator = ("codex" if "/.codex/worktrees/" in normalized or branch.startswith("codex/") else
                   "Tbrain" if "/.tbrain-worktrees/" in normalized or branch.startswith("tbrain/") else "其他")
        matched = []
        for project in projects:
            paths = project.get("paths") or []
            if isinstance(paths, str):
                paths = [paths]
            if any(Path(str(item)).expanduser().resolve() == path.resolve() for item in [project.get("worktree"), *paths] if item):
                matched.append(project["name"])
        fixture = bool(re.search(r"(?:^|[/_. -])(?:test|tests|fixture|synthetic|demo|pytest)(?:[-/_. ]|$)|测试|示例|合成",
                                 path.as_posix() + "/" + branch, re.IGNORECASE))
        rows.append({"path": str(path), "branch": branch, "detached": tree["detached"], "creator_guess": creator,
                     "has_unique_commits": tree["ahead"] > 0, "projects": matched, "fixture_name_hint": fixture})
    return {"ok": True, "repo": facts["repo"], "base": "main", "worktrees": rows,
            "heuristic": "创建者与夹具名只按路径和分支前缀推断，不是创建证明，不作为清理依据。"}


def _git(cwd: Path, args: list[str]) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX",
                "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_OBJECT_DIRECTORY"):
        env.pop(key, None)
    env["GIT_AUTHOR_NAME"] = "Tbrain"
    env["GIT_AUTHOR_EMAIL"] = "tbrain@localhost"
    env["GIT_COMMITTER_NAME"] = "Tbrain"
    env["GIT_COMMITTER_EMAIL"] = "tbrain@localhost"
    return subprocess.run(
        ["git", "-c", "safe.directory=*", "-c", "user.name=Tbrain",
         "-c", "user.email=tbrain@localhost", "-c", "commit.gpgsign=false", *args],
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
