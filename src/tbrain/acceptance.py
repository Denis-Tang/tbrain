"""A–E final acceptance stored on the session summary.

The section ``最终验收`` in the session markdown is the durable copy. Wrap-up
reads that section; a missing or invalid report blocks submission.
"""

from __future__ import annotations

import re

SECTION = "最终验收"
CONCLUSIONS = ("PASS", "CONDITIONAL PASS", "FAIL")
PENDING_LINE = "人工剩余风险接受：仍未完成"
SETTLED_LINE = "人工剩余风险接受：不适用"
STYLE_NOTE = "写法：用短句写明命令、路径、退出码，以及验过什么、没验什么。不要堆术语，也不要写「应该可以」。"

_HEADING = re.compile(r"^###\s+([A-E])\b")
_META_PREFIXES = (
    "Windows 安装包：",
    "Windows 真机验证：",
    "Windows 真机证据：",
    "Windows 缺口：",
    "人工剩余风险接受：",
)
_FIELDS = {
    "A": "task",
    "B": "verification",
    "C": "risks",
    "D": "deliverables",
    "E": "conclusion",
}
_LABELS = {
    "task": "A 任务状态",
    "verification": "B 验证与证据",
    "risks": "C 风险",
    "deliverables": "D 交付物",
    "conclusion": "E 结论",
}
_FIELD_LIMIT = 2000


def normalize(operation: dict, *, host_windows_proof: bool = False) -> dict:
    """Build a report dict from a save operation. Raises ValueError when it cannot be stored."""
    if not isinstance(operation, dict):
        raise ValueError("acceptance 必须是对象")
    fields = {
        "task": _clean(operation.get("task") or operation.get("a")),
        "verification": _clean(operation.get("verification") or operation.get("b")),
        "risks": _clean(operation.get("risks") or operation.get("c")),
        "deliverables": _clean(operation.get("deliverables") or operation.get("d")),
        "conclusion": _clean(operation.get("conclusion") or operation.get("e")),
        "windows_installable": _flag(operation.get("windows_installable")),
        "windows_verified": _flag(operation.get("windows_verified")),
        "windows_evidence": _clean(operation.get("windows_evidence")),
        "windows_gap": _clean(operation.get("windows_gap")),
    }
    problems = validate(fields, host_windows_proof=host_windows_proof)
    if problems:
        raise ValueError("；".join(problems))
    return fields


def render_report(fields: dict) -> str:
    verified = bool(fields.get("windows_verified")) and _names_windows_install_run(fields.get("windows_evidence") or "")
    residual = PENDING_LINE if fields.get("conclusion") == "CONDITIONAL PASS" else SETTLED_LINE
    lines = [
        "### A 任务状态",
        fields.get("task") or "",
        "",
        "### B 验证与证据",
        fields.get("verification") or "",
        "",
        "### C 风险",
        fields.get("risks") or "",
        "",
        "### D 交付物",
        fields.get("deliverables") or "",
        "",
        "### E 结论",
        fields.get("conclusion") or "",
        "",
        f"Windows 安装包：{'是' if fields.get('windows_installable') else '否'}",
        f"Windows 真机验证：{'已做' if verified else '未做'}",
        f"Windows 真机证据：{fields.get('windows_evidence') or ''}",
        f"Windows 缺口：{fields.get('windows_gap') or ''}",
        residual,
    ]
    return "\n".join(lines).strip() + "\n"


def parse_report(text: str) -> dict | None:
    raw = str(text or "").replace("\r\n", "\n").strip()
    if not raw:
        return None
    buckets = {key: [] for key in "ABCDE"}
    meta: list[str] = []
    current = ""
    for line in raw.split("\n"):
        match = _HEADING.match(line.strip())
        if match:
            current = match.group(1)
            continue
        if any(line.startswith(prefix) for prefix in _META_PREFIXES):
            current = ""
            meta.append(line.strip())
            continue
        if current:
            buckets[current].append(line)
    if not any(buckets.values()):
        return None
    conclusion = ""
    for line in buckets["E"]:
        if line.strip():
            conclusion = line.strip()
            break
    evidence = _meta(meta, "Windows 真机证据：")
    verified_flag = _meta(meta, "Windows 真机验证：") == "已做"
    return {
        "task": "\n".join(buckets["A"]).strip(),
        "verification": "\n".join(buckets["B"]).strip(),
        "risks": "\n".join(buckets["C"]).strip(),
        "deliverables": "\n".join(buckets["D"]).strip(),
        "conclusion": conclusion,
        "windows_installable": _meta(meta, "Windows 安装包：") == "是",
        "windows_verified": verified_flag and _names_windows_install_run(evidence),
        "windows_evidence": evidence,
        "windows_gap": _meta(meta, "Windows 缺口："),
        "residual_pending": PENDING_LINE in raw,
    }


def host_windows_proof(summary) -> bool:
    """No installable verifier is available.

    A host event with exit code 0 is not proof that this build was installed and
    started, even if the record says Windows. ``whoami``, ``echo``, ``pytest``,
    and an old timestamp do not identify the artifact. A model-supplied platform
    is not a trusted runner. This returns False until a real verifier exists.
    Hand-built headers in tests are simulations, not Windows runs.
    """
    del summary
    return False


def validate(fields: dict, *, host_windows_proof: bool = False) -> list[str]:
    """Return human-readable problems. Empty means the report may be stored and can pass wrap-up."""
    problems: list[str] = []
    for key, label in _LABELS.items():
        value = str(fields.get(key) or "").strip()
        if not value:
            problems.append(f"{label}为空")
        elif len(value) > _FIELD_LIMIT:
            problems.append(f"{label}超过 {_FIELD_LIMIT} 字")
    conclusion = str(fields.get("conclusion") or "").strip()
    if conclusion and conclusion not in CONCLUSIONS:
        problems.append("E 结论只能是 PASS、CONDITIONAL PASS 或 FAIL")
    if conclusion == "CONDITIONAL PASS" and not fields.get("residual_pending", True):
        problems.append("CONDITIONAL PASS 必须写明人工剩余风险接受仍未完成")
    if fields.get("windows_installable") and not host_windows_proof:
        if conclusion == "PASS":
            problems.append(
                "当前没有能核对安装包的检查器。宿主上的任意成功命令，即使平台写成 Windows，"
                "也不能当作本版本已安装并启动的证据。模型填写的 windows_verified 不能代替，结论不能是 PASS"
            )
        if not str(fields.get("windows_gap") or "").strip():
            problems.append("Windows 真机验证未做，必须写明缺口")
    return problems


def gaps(summary) -> list[str]:
    text = str((getattr(summary, "extra", None) or {}).get(SECTION) or "").strip()
    if not text:
        return ["缺少 A–E 最终验收报告（A 任务状态、B 验证与证据、C 风险、D 交付物、E 结论）。", STYLE_NOTE]
    parsed = parse_report(text)
    if not parsed:
        return ["最终验收不是 A–E 结构。", STYLE_NOTE]
    problems = validate(parsed, host_windows_proof=host_windows_proof(summary))
    if problems:
        problems.append(STYLE_NOTE)
    return problems


def apply_report(summary, fields: dict) -> None:
    summary.extra[SECTION] = render_report(fields)


def _windows_verified(fields: dict) -> bool:
    if not fields.get("windows_verified"):
        return False
    return _names_windows_install_run(str(fields.get("windows_evidence") or ""))


def _names_windows_install_run(text: str) -> bool:
    raw = str(text or "")
    lowered = raw.casefold()
    has_windows = "windows" in lowered
    has_install = "安装" in raw or "install" in lowered
    has_run = "运行" in raw or re.search(r"\brun\b", lowered) is not None
    return bool(raw.strip()) and has_windows and has_install and has_run


def _clean(value) -> str:
    return str(value or "").strip()


def _flag(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "y", "是"}


def _meta(lines: list[str], prefix: str) -> str:
    for line in lines:
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return ""
