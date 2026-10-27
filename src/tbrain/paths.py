"""Module-level path helpers shared by installer, processes and hosts.

`app_home` is the application data directory; `install_root` is where versioned
installations live.  Both are overridable through environment variables so tests
and isolated experiments never touch a real user installation.

Resolution order for the install root:

1. ``settings.install_root`` (the value the user configured in the app);
2. ``TBRAIN_INSTALL_ROOT`` (used by tests / portable installs);
3. ``%LOCALAPPDATA%\\Programs\\Tbrain`` on Windows (or the equivalent under the
   user's home directory on other platforms).
"""

from __future__ import annotations

import os
from pathlib import Path


def app_home(home: Path | None = None) -> Path:
    """The application data directory (~/.tbrain unless overridden)."""
    if home is not None:
        return Path(home).expanduser().resolve()
    configured = os.environ.get("TBRAIN_HOME", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".tbrain").resolve()


def default_install_root() -> Path:
    """Where the versioned program files are installed by default."""
    local = os.environ.get("LOCALAPPDATA", "").strip()
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / "Programs" / "Tbrain"


def install_root(home: Path | None = None, settings=None) -> Path:
    """Resolve the installation directory (settings > env > platform default)."""
    if settings is not None:
        configured = getattr(settings, "install_root", "") or ""
        if str(configured).strip():
            return Path(str(configured)).expanduser().resolve()
    env = os.environ.get("TBRAIN_INSTALL_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return default_install_root().resolve()


def versions_dir(root: Path) -> Path:
    return Path(root) / "versions"


def version_dir(root: Path, version: str) -> Path:
    return versions_dir(root) / str(version)


def version_exe(root: Path, version: str) -> Path:
    return version_dir(root, version) / "tbrain.exe"


def current_file(root: Path) -> Path:
    return Path(root) / "current.txt"


def current_dir(root: Path) -> Path:
    return Path(root) / "current"


def bin_dir(root: Path) -> Path:
    return Path(root) / "bin"


def bin_entry(root: Path) -> Path:
    """The stable launcher hosts point at; it reads ``current.txt``."""
    return bin_dir(root) / "tbrain.cmd"


def processes_dir(home: Path | None = None) -> Path:
    return app_home(home) / "processes"


def process_file(pid: int, home: Path | None = None) -> Path:
    return processes_dir(home) / f"{int(pid)}.json"


def exit_request_file(pid: int, home: Path | None = None) -> Path:
    return processes_dir(home) / f"{int(pid)}.exit"


def integrations_dir(home: Path | None = None) -> Path:
    """Stable copy of the runtime integration files (DSH native plugin)."""
    return app_home(home) / "integrations"


def update_state_file(home: Path | None = None) -> Path:
    return app_home(home) / "update-state.json"
