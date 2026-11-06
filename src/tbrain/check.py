"""Project-local checks. No GitHub CI required.

``tbrain check`` runs the commands in the project config, then the lints listed
there. A recurring mistake becomes a lint id; once the id is listed, the same
mistake fails this gate.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import yaml

CONFIG_NAMES = (Path(".tbrain") / "check.yaml", Path("tbrain.check.yaml"))
DEFAULT_LINTS = ("decision-missing-why",)
STARTER_CONFIG = """\
version: 1
# 本地检查：tbrain check 按顺序运行 checks，再跑 lints。
# 这是每天的门禁，不连接 GitHub。远程拉取请求仍用仓库自己的 CI。
checks: []
lints:
  - decision-missing-why
"""

# A decision has a why only when all three lines have text after the label.
_REASON_LABELS = {
    "rationale": ("理由：", "理由:", "Rationale:", "Rationale："),
    "tradeoffs": ("取舍：", "取舍:", "Tradeoffs:", "Tradeoff:", "Trade-offs:"),
    "decided_by": ("决定人：", "决定人:", "Decided by:", "Who:"),
}
_K_HEADING = re.compile(r"^###\s+K\d+\s*$")
_MARKER = "<!-- tbrain:decision -->"
_SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "dist", "build",
    ".tbrain-worktrees", "__pycache__",
}
_MAX_FILES = 200
_MAX_BYTES = 200_000

LINTS: dict = {}


def register_lint(lint_id: str, fn) -> None:
    """Add a mistake-class lint. ``fn(root) -> list[{path, line, message}]``."""
    LINTS[str(lint_id)] = fn


def has_decision_reasons(text: str) -> bool:
    """True when rationale, tradeoffs, and who decided are all non-empty."""
    lines = [line.strip().lstrip("-").strip() for line in str(text or "").splitlines()]

    def present(marks: tuple[str, ...]) -> bool:
        for line in lines:
            for mark in marks:
                if line.startswith(mark) and line[len(mark):].strip():
                    return True
        return False

    return all(present(marks) for marks in _REASON_LABELS.values())


def starter_config_path(folder: Path) -> Path:
    return Path(folder) / ".tbrain" / "check.yaml"


def write_starter_config(folder: Path) -> Path:
    path = starter_config_path(folder)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(STARTER_CONFIG, encoding="utf-8")
    return path


def find_config(start: Path) -> Path | None:
    current = Path(start).expanduser()
    try:
        current = current.resolve()
    except OSError:
        return None
    if current.is_file():
        return current
    if not current.is_dir():
        return None
    for _ in range(8):
        for name in CONFIG_NAMES:
            candidate = current / name
            if candidate.is_file():
                return candidate
        if (current / ".git").exists():
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def iter_markdown(root: Path):
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS]
        for name in filenames:
            if not name.endswith(".md"):
                continue
            path = Path(dirpath) / name
            try:
                if path.stat().st_size > _MAX_BYTES:
                    continue
            except OSError:
                continue
            yield path
            count += 1
            if count >= _MAX_FILES:
                return


def decision_missing_why(root: Path) -> list[dict]:
    """Fail when a recorded decision has no rationale, tradeoffs, and decider.

    Session-shaped blocks (``### K1``) and notes marked
    ``<!-- tbrain:decision -->`` are both checked. Other markdown is ignored.
    """
    findings = []
    base = Path(root)
    for path in iter_markdown(base):
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            continue
        findings.extend(_decision_findings(path, text, base))
    return findings


def _decision_findings(path: Path, text: str, root: Path) -> list[dict]:
    lines = text.splitlines()
    findings = []
    saw_decision = False
    index = 0
    while index < len(lines):
        if not _K_HEADING.match(lines[index].strip()):
            index += 1
            continue
        saw_decision = True
        start = index
        index += 1
        body: list[str] = []
        while index < len(lines) and not lines[index].startswith("#"):
            body.append(lines[index])
            index += 1
        if not has_decision_reasons("\n".join(body)):
            findings.append(_finding(path, root, start + 1,
                                     "决策缺少理由、取舍或决定人"))
    if saw_decision or _MARKER not in text:
        return findings
    if not has_decision_reasons(text):
        line_no = next((i for i, line in enumerate(lines, 1) if _MARKER in line), 1)
        findings.append(_finding(path, root, line_no, "决策缺少理由、取舍或决定人"))
    return findings


def _finding(path: Path, root: Path, line: int, message: str) -> dict:
    try:
        display = str(path.resolve().relative_to(root.resolve()))
    except (OSError, ValueError):
        display = str(path)
    return {"path": display, "line": line, "message": message}


register_lint("decision-missing-why", decision_missing_why)


def _enabled_lints(config: dict | None) -> list[str]:
    if not config or "lints" not in config:
        return list(DEFAULT_LINTS)
    raw = config.get("lints") or []
    if not isinstance(raw, list):
        return list(DEFAULT_LINTS)
    names = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            names.append(item.strip())
        elif isinstance(item, dict) and str(item.get("id") or "").strip():
            names.append(str(item["id"]).strip())
    return names


def _load_config(path: Path | None) -> tuple[dict | None, str]:
    if path is None:
        return None, ""
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    except (OSError, yaml.YAMLError) as exc:
        return None, f"检查配置无法读取：{exc}"
    if not isinstance(loaded, dict):
        return None, "检查配置必须是 YAML 对象"
    return loaded, ""


def _run_command(root: Path, name: str, command: str, timeout: int) -> dict:
    try:
        proc = subprocess.run(
            command,
            cwd=str(root),
            shell=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return {"name": name, "command": command, "ok": False, "exit_code": None,
                "error": f"超时（{timeout}s）", "tail": ""}
    except OSError as exc:
        return {"name": name, "command": command, "ok": False, "exit_code": None,
                "error": str(exc), "tail": ""}
    tail = ((proc.stdout or "") + (proc.stderr or ""))[-500:]
    return {"name": name, "command": command, "ok": proc.returncode == 0,
            "exit_code": proc.returncode, "error": "", "tail": tail}


def run_check(root: Path, *, config_path: Path | None = None) -> dict:
    """Run user commands and enabled lints. ``ok`` is false when any gate fails."""
    folder = Path(root).expanduser()
    try:
        folder = folder.resolve()
    except OSError:
        return {"ok": False, "root": str(root), "config": "", "checks": [], "lints": [],
                "error": "项目目录无法解析"}
    if not folder.is_dir():
        return {"ok": False, "root": str(folder), "config": "", "checks": [], "lints": [],
                "error": "项目目录不存在"}
    chosen = Path(config_path).expanduser() if config_path else find_config(folder)
    if config_path and chosen and not chosen.is_file():
        return {"ok": False, "root": str(folder), "config": str(chosen), "checks": [],
                "lints": [], "error": "找不到检查配置"}
    config, error = _load_config(chosen if chosen and chosen.is_file() else None)
    checks = []
    if config and not error:
        raw_checks = config.get("checks") or []
        if not isinstance(raw_checks, list):
            error = "checks 必须是数组"
            raw_checks = []
        for index, item in enumerate(raw_checks):
            if not isinstance(item, dict):
                checks.append({"name": f"check-{index + 1}", "command": "", "ok": False,
                               "exit_code": None, "error": "检查项必须是对象", "tail": ""})
                continue
            name = str(item.get("name") or f"check-{index + 1}")
            command = str(item.get("run") or item.get("command") or "").strip()
            if not command:
                checks.append({"name": name, "command": "", "ok": False, "exit_code": None,
                               "error": "缺少 run", "tail": ""})
                continue
            try:
                timeout = int(item.get("timeout") or 120)
            except (TypeError, ValueError):
                timeout = 120
            timeout = max(1, min(timeout, 600))
            checks.append(_run_command(folder, name, command, timeout))
    lint_rows = []
    if not error:
        for lint_id in _enabled_lints(config):
            fn = LINTS.get(lint_id)
            if fn is None:
                lint_rows.append({"id": lint_id, "ok": False, "findings": [
                    {"path": "", "line": 0, "message": f"未知 lint：{lint_id}"}]})
                continue
            try:
                findings = list(fn(folder) or [])
            except Exception as exc:  # a lint bug must fail the gate, not disappear
                findings = [{"path": "", "line": 0, "message": f"lint 运行失败：{exc}"}]
            lint_rows.append({"id": lint_id, "ok": not findings, "findings": findings})
    ok = not error and all(item.get("ok") for item in checks) and all(item.get("ok") for item in lint_rows)
    return {"ok": ok, "root": str(folder),
            "config": str(chosen) if chosen and chosen.is_file() else "",
            "checks": checks, "lints": lint_rows, "error": error}
