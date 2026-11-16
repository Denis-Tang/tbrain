"""Host adapters: connect / disconnect / doctor / normalize / render.

``hosts.normalize(host, raw)`` turns a host hook payload into the neutral
:class:`tbrain.hooks.HookEvent`; ``hosts.render(host, result, event)`` turns the
neutral :class:`tbrain.hooks.HookResult` back into the dict the host expects on
stdout.  ``connect``/``disconnect``/``doctor`` only ever touch Tbrain's own
entries and back up every modified file.
"""

from __future__ import annotations

from pathlib import Path
import subprocess

from . import claude, codex, config, dsh, pi
from .base import (HOST_EVENTS, HOST_SPECS, HostSpec, entry_for, hook_command,
                   host_spec, normalize, render)
from .config import (HOST_ALIASES, SUPPORTED_HOSTS, HostError, canonical_host,
                     integrations_source)
from .doctor import doctor, doctor_all

HOST_MODULES = {"claude-code": claude, "codex": codex, "dsh": dsh, "pi": pi}


def list_hosts() -> list[dict]:
    return [{"key": spec.key, "display": spec.display,
             "supports_context": spec.supports_context, "notes": spec.notes}
            for spec in HOST_SPECS.values()]


def project_roots(project: dict) -> list[str]:
    """User directories only. The managed worktree is execution context, not a second root."""
    worktree = str(project.get("worktree") or "").strip()
    separate = str(project.get("worktree_status") or "") == "worktree"
    roots = []
    for item in project.get("paths") or []:
        text = str(item).strip()
        if not text:
            continue
        if separate and worktree:
            try:
                if Path(text).expanduser().resolve() == Path(worktree).expanduser().resolve():
                    continue
            except OSError:
                if text == worktree:
                    continue
        roots.append(text)
    return roots


def create_workspaces(project: dict, app_home: Path) -> list[str]:
    results = []
    dispatched = {**project, "paths": project_roots(project)}
    for host, connection in config.read_connect_state(app_home).items():
        if not connection.get("enabled") or host not in ("codex", "dsh", "pi"):
            continue
        try:
            if host == "pi":
                detail = "未同步：Pi Desktop 暂无项目登记接口，且不支持多目录项目"
            elif not dispatched["paths"]:
                detail = "未同步：项目未关联工作目录"
            elif host == "codex":
                detail = codex.create_workspace(dispatched)
            else:
                detail = dsh.create_workspace(dispatched, app_home)
        except (OSError, ValueError, KeyError, HostError, subprocess.SubprocessError) as error:
            detail = f"同步失败：{error}"
        results.append(f"{HOST_SPECS[host].display}：{detail}")
    return results


def connect(host: str, app_home: Path | None = None, install_root: Path | None = None,
            settings=None, profile: str | None = None) -> dict:
    key = canonical_host(host)
    module = HOST_MODULES[key]
    if key == "dsh":
        return module.connect(key, app_home, install_root, settings, profile=profile)
    return module.connect(key, app_home, install_root, settings)


def disconnect(host: str, app_home: Path | None = None, install_root: Path | None = None,
               settings=None, profile: str | None = None) -> dict:
    key = canonical_host(host)
    module = HOST_MODULES[key]
    if key == "dsh":
        return module.disconnect(key, app_home, install_root, settings, profile=profile)
    return module.disconnect(key, app_home, install_root, settings)


def doctor_all_hosts(app_home: Path | None = None, install_root: Path | None = None,
                     settings=None, profile: str | None = None) -> dict:
    return doctor_all(app_home, install_root, settings, profile)


__all__ = [
    "HOST_EVENTS",
    "HOST_SPECS",
    "HOST_ALIASES",
    "SUPPORTED_HOSTS",
    "HostError",
    "HostSpec",
    "canonical_host",
    "connect",
    "disconnect",
    "doctor",
    "doctor_all",
    "doctor_all_hosts",
    "entry_for",
    "hook_command",
    "host_spec",
    "integrations_source",
    "list_hosts",
    "normalize",
    "render",
]
