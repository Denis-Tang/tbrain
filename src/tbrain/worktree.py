"""One git worktree per project.

The worktree lives beside the repository, not inside it:

    ../.tbrain-worktrees/<repo-directory>/<project>

``git worktree add`` refuses a path ignored by ``.gitignore``, and a worktree
inside the checkout shows up as untracked files. The sibling path avoids both.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import naming
from .check import write_starter_config

CONVENTION = "../.tbrain-worktrees/<repo>/<project>"
_DIRNAME = ".tbrain-worktrees"


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
        return payload
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
        return payload
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
