"""项目提示词写作规范的程序侧校验（确定性，不调用大模型）。

规范见 `项目提示词写作规范.md`：固定节、顺序、条数与字数上限、硬规则必须带
来源、关键路径需要检查存在性。程序只做检查；不通过时退回给模型并附示例。
"""

from __future__ import annotations

from pathlib import Path
import re

SECTION_ORDER = ("最终目标", "硬规则", "当前阶段", "关键路径和命令")

LIMITS = {
    "最终目标": {"lines": 3, "line_chars": 80},
    "硬规则": {"items": 12, "item_chars": 80},
    "当前阶段": {"lines": 5, "line_chars": 80},
    "关键路径和命令": {"items": 10, "item_chars": 100},
}
TOTAL_MAX_CHARS = 4000

USER_START = "<!-- owner:user -->"
USER_END = "<!-- /owner:user -->"
MODEL_START = "<!-- owner:model -->"
MODEL_END = "<!-- /owner:model -->"

MAINTENANCE_GUIDANCE = (
    "执行前核对项目提示词与相关文件；目标或长期规则变化、阶段完成或切换、关键路径或命令变化、"
    "旧内容失效时，在同次任务的 save 中提交 project_prompt 更新对应节，无变化不改写。"
    "空白节只补已确认内容，不推断用户偏好；保存后核对 written/rejected，只补交失败部分。"
)

PROJECT_PROMPT_SPEC = """\
项目提示词规范（project_prompt 操作）：
- 最终目标最多3行，每行80字；硬规则最多12条，每条80字；当前阶段最多5行，每行80字；关键路径和命令最多10条，每条100字；全文4000字以内。
- 硬规则每条带来源：（来源：[[会话名]]#U3 或 U3 / E4）；无来源不写入。目标和硬规则写长期约定；当前阶段写“阶段名：完成标准”，随阶段更新。
- 关键路径和命令写“用途：路径或命令”；命令须已验证。只写确认的事实，不写尝试经过、临时流水、相对时间词或评价；主语写全称。
- 只提交变化的节，整节替换；旧内容自动进入变更日志。用户补充区和用户手改内容不覆盖。
""" + MAINTENANCE_GUIDANCE

_SOURCE_RE = re.compile(r"（来源：\s*(.+?)）")
_WIKI_REF_RE = re.compile(r"\[\[([^\]|#]+)(?:\|([^\]]+))?\]\]#([UEDKXG]\d+)")
_LOCAL_REF_RE = re.compile(r"#?([UEDKXG]\d+)")
_PATH_LINE_RE = re.compile(r"^(?P<label>[^：:]{1,40})[：:]\s*(?P<value>.+)$")
_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_PRONOUN_START = ("它", "这", "该", "此", "上述")
_TIME_WORDS = ("最近", "刚才", "现在", "昨天")

EXAMPLE = {
    "最终目标": "3 行以内，每行 <=80 字：项目要达成什么、给谁用、完成标准。",
    "硬规则": "最多 12 条，每条 <=80 字，且必须带来源，例如：\n- 所有 Python 脚本用 `uv run` 执行（来源：[[重构索引 2026-10-24-1]]#U2）",
    "当前阶段": "格式：`阶段名：完成标准`，最多 5 行，每行 <=80 字。",
    "关键路径和命令": "格式：`用途：路径或命令`，最多 10 条，每条 <=100 字。例如：\n- 测试命令：.venv\\Scripts\\python.exe -m pytest -q",
}

def empty_sections() -> dict:
    return {name: "" for name in SECTION_ORDER}


def parse(text: str) -> dict:
    """解析项目提示词文件为 {user, sections}。容忍旧格式与缺失标记。"""
    text = (text or "").replace("\r\n", "\n")
    user = ""
    if USER_START in text and USER_END in text:
        start = text.find(USER_START) + len(USER_START)
        end = text.find(USER_END, start)
        user = text[start:end].strip("\n")
    model = text
    if MODEL_START in model and MODEL_END in model:
        start = model.find(MODEL_START) + len(MODEL_START)
        end = model.find(MODEL_END, start)
        model = text[start:end]
    sections = empty_sections()
    current = ""
    for line in model.split("\n"):
        if line.startswith("## "):
            current = line[3:].strip()
            continue
        if current in sections:
            sections[current] = (sections[current] + "\n" + line).strip("\n")
    return {"user": user, "sections": sections}


def render(sections: dict, user: str = "") -> str:
    body = [USER_START, "## 用户补充", user.strip("\n") if user.strip() else "", USER_END, "", MODEL_START]
    for name in SECTION_ORDER:
        body.append(f"## {name}")
        body.append(str(sections.get(name) or "").strip("\n"))
        body.append("")
    body.append(MODEL_END)
    return "\n".join(body).strip("\n") + "\n"


def _lines(text: str) -> list[str]:
    return [line.strip() for line in str(text or "").replace("\r\n", "\n").split("\n") if line.strip()]


def _items(text: str) -> list[str]:
    result = []
    for line in _lines(text):
        if line.startswith("- "):
            line = line[2:].strip()
        result.append(line)
    return result


def validate(sections: dict, *, resolve_source=None, path_exists=None) -> dict:
    """校验模型提交的节；返回 {written, rejected, warnings, cleaned}。

    ``written`` 为写入的节内容（被整节退回的节不写入，保留旧值由调用方决定）；
    ``rejected`` 每项含 section / reason / example；``warnings`` 为写入但提醒的问题。
    """
    rejected: list[dict] = []
    warnings: list[str] = []
    written: dict = {}
    cleaned: dict = {name: "" for name in SECTION_ORDER}

    if not isinstance(sections, dict):
        return {"written": {}, "rejected": [{"section": "", "reason": "sections 必须是对象", "example": ""}],
                "warnings": [], "cleaned": cleaned}
    unknown = [name for name in sections if name not in SECTION_ORDER]
    if unknown:
        rejected.append({"section": ",".join(unknown), "reason": "只允许 4 个固定节", "example": "见规范第 2 节"})

    total_chars = 0
    for name in SECTION_ORDER:
        if name not in sections:
            continue
        raw = sections.get(name)
        if not isinstance(raw, str):
            rejected.append({"section": name, "reason": "节内容必须是字符串", "example": EXAMPLE[name]})
            continue
        text = raw.strip("\n")
        limit = LIMITS[name]
        if name in ("最终目标", "当前阶段"):
            lines = _lines(text)
            if len(lines) > limit["lines"]:
                rejected.append({"section": name, "reason": f"不超过 {limit['lines']} 行（当前 {len(lines)} 行）", "example": EXAMPLE[name]})
                continue
            too_long = [line for line in lines if len(line) > limit["line_chars"]]
            if too_long:
                rejected.append({"section": name, "reason": f"每行不超过 {limit['line_chars']} 字（当前最长 {max(len(line) for line in too_long)} 字）", "example": EXAMPLE[name]})
                continue
            for line in lines:
                _warn_style(line, name, warnings)
            cleaned[name] = "\n".join(lines)
        else:
            items = _items(text)
            if len(items) > limit["items"]:
                rejected.append({"section": name, "reason": f"不超过 {limit['items']} 条（当前 {len(items)} 条），请合并或删减，超出 {len(items) - limit['items']} 条", "example": EXAMPLE[name]})
                continue
            if name == "硬规则":
                kept: list[str] = []
                for index, item in enumerate(items, start=1):
                    if not _SOURCE_RE.search(item):
                        rejected.append({"section": name, "reason": f"第 {index} 条缺少来源（来源：…）", "example": EXAMPLE[name]})
                        continue
                    if len(item) > limit["item_chars"]:
                        rejected.append({"section": name, "reason": f"第 {index} 条超过 {limit['item_chars']} 字", "example": EXAMPLE[name]})
                        continue
                    if resolve_source is not None and not _resolve_sources(item, resolve_source):
                        rejected.append({"section": name, "reason": f"第 {index} 条引用的会话/编号不存在", "example": EXAMPLE[name]})
                        continue
                    kept.append(item)
                    _warn_style(item, name, warnings)
                items = kept
            else:
                for index, item in enumerate(items, start=1):
                    if len(item) > limit["item_chars"]:
                        rejected.append({"section": name, "reason": f"第 {index} 条超过 {limit['item_chars']} 字", "example": EXAMPLE[name]})
                        continue
                    if path_exists is not None:
                        _check_path(item, path_exists, warnings)
                    _warn_style(item, name, warnings)
                kept = [item for item in items if len(item) <= limit["item_chars"]]
                if len(kept) != len(items):
                    items = kept
            cleaned[name] = "\n".join(f"- {item}" for item in items)
        total_chars += len(cleaned[name])
        if cleaned[name] or name in sections:
            written[name] = cleaned[name]

    if total_chars > TOTAL_MAX_CHARS:
        rejected.append({"section": "全文", "reason": f"全文控制在 {TOTAL_MAX_CHARS} 字以内（当前 {total_chars} 字）", "example": "删减过程性内容"})
        return {"written": {}, "rejected": rejected, "warnings": warnings, "cleaned": cleaned}

    if rejected:
        # 只退回有问题的节：其余合格节照常写入
        bad = {item["section"] for item in rejected}
        written = {name: value for name, value in written.items() if name not in bad}
        for name in bad:
            cleaned[name] = ""
    return {"written": written, "rejected": rejected, "warnings": warnings, "cleaned": cleaned}


def _resolve_sources(item: str, resolve_source) -> bool:
    match = _SOURCE_RE.search(item)
    if not match:
        return False
    sources = match.group(1)
    refs = _WIKI_REF_RE.findall(sources)
    if refs:
        return all(bool(resolve_source(name.strip(), ref)) for name, _alias, ref in refs)
    local = _LOCAL_REF_RE.findall(sources)
    return bool(local) and all(bool(resolve_source("", ref)) for ref in local)


def _check_path(item: str, path_exists, warnings: list[str]) -> None:
    match = _PATH_LINE_RE.match(item)
    if not match:
        return
    value = match.group("value").strip().strip("`")
    candidate = value.split("（", 1)[0].strip()
    if " " in candidate and not candidate.startswith('"'):
        candidate = candidate.split(" ", 1)[0]
    looks_like_path = _DRIVE_PATH_RE.match(candidate) or candidate.startswith("\\\\") or (
        ("/" in candidate or "\\" in candidate) and "." in candidate.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    )
    if looks_like_path and not path_exists(candidate):
        warnings.append(f"{match.group('label')}：路径未找到 {candidate}")


def _warn_style(item: str, section: str, warnings: list[str]) -> None:
    if item.startswith(_PRONOUN_START):
        warnings.append(f"{section}：条目以代词开头，建议写明对象（它/这/该/此/上述）")
    if any(word in item for word in _TIME_WORDS):
        warnings.append(f"{section}：含相对时间词，建议改用编号或阶段名")


def project_prompt_path(vault: Path, project: str) -> Path:
    return Path(vault) / "项目" / project / "项目提示词.md"
