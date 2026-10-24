"""Versioned installation with a stable launcher entry.

Layout under the install root (``%LOCALAPPDATA%\\Programs\\Tbrain`` by default)::

    bin/tbrain.cmd          stable entry: reads current.txt -> versions/<ver>/tbrain.exe
    current.txt             the selected version (atomic write)
    current/                optional junction -> versions/<ver> (compat only)
    versions/<version>/     one complete package per version (tbrain.exe, desktop/, runtime/, _internal/)

Hosts only ever point at ``bin/tbrain.cmd``, so upgrading (or rolling back) never
requires editing a host configuration again.  Installation is copy -> staging
directory -> rename on the same volume, which keeps a half-copied version from
ever being selectable.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import paths

PACKAGE_ROOT = "tbrain"
REQUIRED_FILES = ("tbrain.exe", "desktop/Tbrain.Desktop.exe", "runtime/node.exe")
REQUIRED_DIRS = ("_internal",)
ENTRY_FILE = "tbrain.cmd"
_VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z._+-]*$")

# Pure cmd.exe, ASCII only.  ``%~dp0`` always ends with a backslash.
ENTRY_SCRIPT = """@echo off
setlocal
set "TBRAIN_VERSION="
if exist "%~dp0..\\current.txt" set /p TBRAIN_VERSION=<"%~dp0..\\current.txt"
if not defined TBRAIN_VERSION (
  echo Tbrain: current.txt is missing; please reinstall. 1>&2
  exit /b 1
)
"%~dp0..\\versions\\%TBRAIN_VERSION%\\tbrain.exe" %*
exit /b %ERRORLEVEL%
"""


class InstallError(RuntimeError):
    """A short message safe to show to the user."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _version_key(version: str) -> tuple:
    """Numeric sort key so 0.6.10 sorts after 0.6.9; invalid names sort last."""
    text = str(version or "").strip()
    match = re.fullmatch(r"(\d+(?:\.\d+)*)(?:[-+](.*))?", text)
    if not match:
        return ((), 0, text)
    numbers = tuple(int(part) for part in match.group(1).split("."))
    return (numbers, 1 if not match.group(2) else 0, text)


def _check_version(version: str) -> str:
    text = str(version or "").strip()
    if not text or not _VERSION_RE.fullmatch(text):
        raise InstallError(f"版本号不合法：{version!r}")
    return text


def _write_atomic(path: Path, data: bytes | str) -> None:
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _remove_tree(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
        return
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# entry / current
# ---------------------------------------------------------------------------

def entry_command(root: Path | None = None) -> str:
    """Absolute path of the stable host-facing launcher."""
    return str(paths.bin_entry(Path(root) if root is not None else paths.install_root()))


def ensure_entry(root: Path) -> Path:
    entry = paths.bin_entry(root)
    text = ENTRY_SCRIPT.replace("\r\n", "\n").replace("\n", "\r\n")
    _write_atomic(entry, text.encode("ascii"))
    return entry


def current_version(root: Path | None = None) -> str:
    path = paths.current_file(Path(root) if root is not None else paths.install_root())
    try:
        text = path.read_text(encoding="utf-8-sig").strip().splitlines()[0].strip()
    except (OSError, IndexError):
        return ""
    return text


def installed_versions(root: Path | None = None) -> list[str]:
    base = Path(root) if root is not None else paths.install_root()
    folder = paths.versions_dir(base)
    try:
        candidates = [item.name for item in folder.iterdir() if item.is_dir()]
    except OSError:
        return []
    known = [name for name in candidates
             if not name.startswith(".") and paths.version_exe(base, name).is_file()]
    return sorted(known, key=_version_key)


def _package_root(source: Path) -> Path:
    """Accept either ``<dir>/tbrain`` (release zip) or a direct package directory."""
    source = Path(source)
    nested = source / PACKAGE_ROOT
    if (nested / "tbrain.exe").is_file():
        return nested
    if (source / "tbrain.exe").is_file():
        return source
    raise InstallError(f"安装包目录结构不正确：{source}")


def verify_source(source: Path) -> dict:
    """Check that a directory contains every file an installation needs."""
    app = _package_root(source)
    missing: list[str] = []
    for relative in REQUIRED_FILES:
        if not (app / relative).is_file():
            missing.append(relative)
    for relative in REQUIRED_DIRS:
        if not (app / relative).is_dir():
            missing.append(relative + "/")
    if missing:
        raise InstallError("安装包缺少必要文件：" + "、".join(missing))
    return {"ok": True, "path": str(app),
            "files": sum(1 for path in app.rglob("*") if path.is_file())}


def verify_layout(version: str | None = None, root: Path | None = None,
                  check_current: bool = True) -> dict:
    """Check one installed version (default: the selected one) on disk."""
    base = Path(root) if root is not None else paths.install_root()
    selected = str(version or current_version(base) or "")
    missing: list[str] = []
    if not selected:
        missing.append("current.txt")
        return {"ok": False, "version": "", "path": str(base), "missing": missing}
    folder = paths.version_dir(base, selected)
    if not folder.is_dir():
        missing.append(f"versions/{selected}/")
    else:
        for relative in REQUIRED_FILES:
            if not (folder / relative).is_file():
                missing.append(relative)
        for relative in REQUIRED_DIRS:
            if not (folder / relative).is_dir():
                missing.append(relative + "/")
    if check_current:
        entry = paths.bin_entry(base)
        if not entry.is_file():
            missing.append("bin/tbrain.cmd")
        if current_version(base) != selected:
            missing.append("current.txt(与安装版本不一致)")
    return {"ok": not missing, "version": selected, "path": str(folder), "missing": missing}


# ---------------------------------------------------------------------------
# junction
# ---------------------------------------------------------------------------

def _is_junction(path: Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    if path.is_symlink():
        return True
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        attrs = kernel32.GetFileAttributesW(str(path))
        if attrs == 0xFFFFFFFF:
            return False
        return bool(attrs & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
    except Exception:  # pragma: no cover - defensive
        return False


def _update_junction(root: Path, version: str) -> str:
    """Best-effort ``current`` junction; returns its state, never raises."""
    link = paths.current_dir(root)
    target = paths.version_dir(root, version)
    try:
        if link.is_symlink():
            link.unlink()
        elif link.exists():
            if _is_junction(link):
                os.rmdir(link)
            else:
                return "skipped-existing-directory"
    except OSError:
        return "skipped-in-use"
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return "skipped"
    if os.name == "nt":
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode == 0 and link.exists():
            return "junction"
        return "skipped"
    try:
        os.symlink(target, link, target_is_directory=True)
        return "symlink"
    except OSError:
        return "skipped"


# ---------------------------------------------------------------------------
# install / switch / prune / rollback / uninstall
# ---------------------------------------------------------------------------

def switch_current(version: str, root: Path | None = None) -> dict:
    """Select an installed version: atomic current.txt write plus junction update."""
    base = Path(root) if root is not None else paths.install_root()
    selected = _check_version(version)
    if not paths.version_exe(base, selected).is_file():
        raise InstallError(f"版本 {selected} 尚未安装")
    _write_atomic(paths.current_file(base), selected + "\n")
    junction = _update_junction(base, selected)
    verify = verify_layout(selected, base)
    return {"ok": verify["ok"], "version": selected, "entry": entry_command(base),
            "junction": junction, "missing": verify["missing"]}


def install_from_dir(source: Path, version: str, root: Path | None = None,
                     make_current: bool = True) -> dict:
    """Copy a package directory into ``versions/<version>`` and select it."""
    base = Path(root) if root is not None else paths.install_root()
    selected = _check_version(version)
    app = _package_root(Path(source))
    verify_source(app)
    target = paths.version_dir(base, selected)
    base.mkdir(parents=True, exist_ok=True)
    paths.versions_dir(base).mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".staging-{selected}-", dir=base))
    try:
        # Copy into the destination volume first; the final step is a rename.
        copy_root = staging / "package"
        shutil.copytree(app, copy_root)
        previous = target.with_name(f".old-{selected}-{os.getpid()}-{int(time.time() * 1000)}")
        replaced = target.exists()
        if replaced:
            os.rename(target, previous)
        try:
            os.rename(copy_root, target)
        except OSError:
            if replaced and previous.exists() and not target.exists():
                os.rename(previous, target)
            raise
        finally:
            _remove_tree(previous)
    except OSError as exc:
        raise InstallError(f"安装文件复制失败：{exc}") from None
    finally:
        _remove_tree(staging)
    entry = ensure_entry(base)
    verify = verify_layout(selected, base, check_current=False)
    result = {"ok": verify["ok"], "version": selected, "path": str(target),
              "entry": str(entry), "replaced": replaced, "switched": False,
              "junction": "", "missing": verify["missing"]}
    if make_current:
        switched = switch_current(selected, base)
        result.update({"switched": True, "junction": switched.get("junction", ""),
                       "ok": switched.get("ok", verify["ok"]),
                       "missing": switched.get("missing", verify["missing"])})
    return result


def install_from_package(source: Path, version: str, root: Path | None = None,
                         make_current: bool = True) -> dict:
    """Install from a package directory or a release ZIP."""
    source = Path(source)
    if source.is_dir():
        return install_from_dir(source, version, root=root, make_current=make_current)
    if not source.is_file():
        raise InstallError(f"找不到安装包：{source}")
    base = Path(root) if root is not None else paths.install_root()
    base.mkdir(parents=True, exist_ok=True)
    extract_root = Path(tempfile.mkdtemp(prefix=f".extract-{version}-", dir=base))
    try:
        extract_archive(source, extract_root)
        return install_from_dir(extract_root, version, root=base, make_current=make_current)
    finally:
        _remove_tree(extract_root)


def install(source: Path, version: str, root: Path | None = None) -> dict:
    """Alias used by docs/ARCHITECTURE.md: install a package and select it."""
    return install_from_package(source, version, root=root, make_current=True)


def stage(source: Path, version: str, root: Path | None = None) -> dict:
    """Copy a package into ``versions/<version>`` without selecting it."""
    return install_from_package(source, version, root=root, make_current=False)


def prune_versions(keep: int = 2, root: Path | None = None) -> dict:
    """Keep the current version and the newest ``keep - 1`` others."""
    base = Path(root) if root is not None else paths.install_root()
    keep = max(1, int(keep))
    current = current_version(base)
    versions = installed_versions(base)
    if not versions:
        return {"kept": [], "removed": []}
    ordered = sorted(versions, key=_version_key, reverse=True)
    keep_set: list[str] = []
    if current:
        keep_set.append(current)
    for name in ordered:
        if len(keep_set) >= keep:
            break
        if name not in keep_set:
            keep_set.append(name)
    removed: list[str] = []
    for name in versions:
        if name in keep_set:
            continue
        folder = paths.version_dir(base, name)
        _remove_tree(folder)
        if folder.exists():  # locked files: report instead of failing the upgrade
            continue
        removed.append(name)
    return {"kept": keep_set, "removed": removed}


def rollback(root: Path | None = None) -> dict:
    """Switch to the newest installed version that is not currently selected."""
    base = Path(root) if root is not None else paths.install_root()
    current = current_version(base)
    candidates = [name for name in installed_versions(base) if name != current]
    if not candidates:
        raise InstallError("没有可回退的版本")
    target = sorted(candidates, key=_version_key, reverse=True)[0]
    result = switch_current(target, base)
    result["previous"] = current
    result["message"] = f"已回退到 {target}"
    return result


def uninstall(root: Path | None = None) -> dict:
    """Remove the program files (the application data directory is left alone)."""
    base = Path(root) if root is not None else paths.install_root()
    removed: list[str] = []
    link = paths.current_dir(base)
    try:
        if link.is_symlink():
            link.unlink()
            removed.append(str(link))
        elif link.exists() and _is_junction(link):
            os.rmdir(link)
            removed.append(str(link))
    except OSError:
        pass
    for path in (paths.versions_dir(base), paths.current_file(base), paths.bin_dir(base)):
        if path.exists():
            _remove_tree(path)
            if not path.exists():
                removed.append(str(path))
    try:
        if base.exists() and not any(base.iterdir()):
            base.rmdir()
            removed.append(str(base))
    except OSError:
        pass
    return {"ok": True, "removed": removed, "root": str(base)}


# ---------------------------------------------------------------------------
# archives
# ---------------------------------------------------------------------------

def extract_archive(archive: Path, target: Path) -> int:
    """Extract a release ZIP into a fresh directory, refusing escaping entries."""
    archive = Path(archive)
    target = Path(target)
    if target.exists():
        _remove_tree(target)
    target.mkdir(parents=True, exist_ok=True)
    root = target.resolve()
    written = 0
    try:
        with zipfile.ZipFile(archive) as source:
            for member in source.infolist():
                if member.is_dir():
                    continue
                destination = (target / member.filename).resolve()
                if not destination.is_relative_to(root):
                    raise InstallError("压缩包里包含指向目录外的路径，已停止更新。")
                destination.parent.mkdir(parents=True, exist_ok=True)
                with source.open(member) as reader, destination.open("wb") as writer:
                    shutil.copyfileobj(reader, writer)
                written += 1
    except (zipfile.BadZipFile, OSError) as exc:
        raise InstallError(f"压缩包无法读取：{exc}") from None
    return written


def verify_archive(archive: Path) -> dict:
    """Check a release ZIP layout without extracting it to the installation."""
    try:
        with zipfile.ZipFile(archive) as source:
            names = source.namelist()
    except (OSError, zipfile.BadZipFile):
        raise InstallError("压缩包无法读取，可能不完整。") from None
    if not names:
        raise InstallError("压缩包是空的。")
    if {name.split("/", 1)[0] for name in names} != {PACKAGE_ROOT}:
        raise InstallError("压缩包结构与发布的包不一致。")
    for required in REQUIRED_FILES:
        if f"{PACKAGE_ROOT}/{required}" not in names:
            raise InstallError(f"压缩包缺少必要文件：{required}")
    if not any(name.startswith(f"{PACKAGE_ROOT}/_internal/") for name in names):
        raise InstallError(f"压缩包缺少 {PACKAGE_ROOT}/_internal 运行时目录。")
    return {"entries": len(names)}


def state_path(home: Path) -> Path:
    return paths.update_state_file(home)


def read_state(home: Path) -> dict:
    import json

    try:
        payload = json.loads(state_path(home).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_state(home: Path, **values) -> dict:
    import json

    path = state_path(home)
    record = {**read_state(home), **values, "at": _now()}
    _write_atomic(path, json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    return record


def pending_notice(home: Path) -> str:
    """A one-line report for an installation state that needs attention."""
    state = read_state(home)
    stage = str(state.get("stage") or "")
    if stage == "failed":
        return "上次升级失败并已回滚：" + str(state.get("message") or "原因未知")
    if stage == "replacing":
        return "上次升级未正常结束，请重新执行升级或检查安装目录。"
    return ""
