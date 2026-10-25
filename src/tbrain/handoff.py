"""handoff/v1 会话总结的解析、渲染与条目操作。

Markdown 是唯一事实源：本模块只负责把 Summary 与文件文本互相转换，不负责
文件写入（store.py）或索引（index.py）。所有条目编号由程序分配，模型只提交
增量内容。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import re

import yaml

STATUSES = ("进行中", "受阻", "待验证", "已完成", "已放弃", "已中断")
REASON_KINDS = ("环境/权限限制", "时间不够", "证据不足", "已证实不可行")
STRENGTHS = ("已排除", "暂缓")

ITEM_MAX_CHARS = 200
TARGET_MAX_CHARS = 200
NEXT_MAX_CHARS = 150
USER_MAX_CHARS = 2000
ARCHIVE_THRESHOLD = 30

HEADER_FIELDS = (
    "name", "project", "project_link", "session_id", "agent", "cwd", "host_record",
    "started_at", "updated_at", "last_activity_at", "resumed_from",
    "git", "last_verify", "status", "saved_users", "provisional_name",
)

SECTIONS = (
    "原始命令", "用户指令", "目标", "已完成", "决策", "未走通的路", "证据",
    "未完成目标", "下一步", "待整理", "待用户确认", "归档",
)

ITEM_PREFIXES = {"已完成": "D", "决策": "K", "未走通的路": "X", "证据": "E", "未完成目标": "G"}

_ITEM_RE = re.compile(r"^###\s+([UDKXEG])(\d+)\s*$")
_ARCHIVE_RE = re.compile(r"^-\s+([UDKXEG])(\d+)\s+(.*)$")


@dataclass
class Summary:
    header: dict = field(default_factory=dict)
    raw_command: str = ""
    target: str = ""
    next_step: str = ""
    sections: dict = field(default_factory=dict)  # name -> list[dict] 或 list[str]
    pending: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    archive: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)  # 未知/保留的二级标题 -> 原文（迁移与手工内容不丢失）

    # 便捷访问器 ----------------------------------------------------------
    @property
    def name(self) -> str:
        return str(self.header.get("name") or "")

    @property
    def project(self) -> str:
        return str(self.header.get("project") or "")

    @property
    def session_id(self) -> str:
        return str(self.header.get("session_id") or "")

    @property
    def status(self) -> str:
        return str(self.header.get("status") or "进行中")

    @property
    def users(self) -> list[dict]:
        return [{**item, "id": f"U{item['number']}"} for item in self.sections.get("用户指令", [])]

    def items(self, section: str) -> list[dict]:
        return [{**item, "id": f"{ITEM_PREFIXES[section]}{item['number']}"}
                for item in self.sections.get(section, [])]

    def goals(self) -> list[dict]:
        return [{**item, "id": f"G{item['number']}"} for item in self.sections.get("未完成目标", [])]


def _now_iso(value: datetime | str | None) -> str:
    if isinstance(value, datetime):
        return value.astimezone().astimezone(tz=None).isoformat() if value.tzinfo else value.isoformat()
    return str(value or "")


def new_summary(*, name: str, project: str, session_id: str, agent: str, cwd: str,
                host_record: str, started_at: str, raw_command: str,
                status: str = "进行中", provisional: bool = False) -> Summary:
    summary = Summary()
    summary.header = {
        "name": name,
        "project": project,
        "project_link": f"[[{project}]]" if project else "",
        "session_id": session_id,
        "agent": agent,
        "cwd": cwd,
        "host_record": host_record,
        "started_at": started_at,
        "updated_at": started_at,
        "last_activity_at": started_at,
        "resumed_from": "",
        "git": {"branch": "", "head": "", "dirty": 0},
        "last_verify": {"command": "", "exit_code": None, "at": ""},
        "status": status,
        "saved_users": 0,
        "provisional_name": bool(provisional),
    }
    summary.raw_command = raw_command
    summary.sections = {
        "用户指令": ([{"number": 1, "text": _clip_user(raw_command)}] if str(raw_command or "").strip() else []),
        "已完成": [], "决策": [], "未走通的路": [], "证据": [], "未完成目标": [],
    }
    return summary


def _clip_user(text: str) -> str:
    text = str(text or "")
    if len(text) <= USER_MAX_CHARS:
        return text
    return text[:USER_MAX_CHARS] + f"\n（已截断，原文位置：宿主会话记录；共 {len(text)} 字）"


def render(summary: Summary) -> str:
    header = {key: summary.header.get(key) for key in HEADER_FIELDS if key != "provisional_name"}
    header.update({key: value for key, value in summary.header.items() if key not in header})
    header["provisional_name"] = bool(summary.header.get("provisional_name", False))
    front = yaml.safe_dump(header, allow_unicode=True, sort_keys=False, default_flow_style=False).strip()
    lines = ["---", front, "---", "", "## 原始命令", summary.raw_command or "", ""]

    lines += ["## 用户指令"]
    for item in summary.sections.get("用户指令", []):
        lines += [f"### U{item['number']}", item.get("text", ""), ""]

    lines += ["## 目标", summary.target or "", ""]

    for section in ("已完成", "决策", "未走通的路", "证据", "未完成目标"):
        prefix = ITEM_PREFIXES[section]
        lines += [f"## {section}"]
        for item in summary.sections.get(section, []):
            lines.append(f"### {prefix}{item['number']}")
            if section == "未完成目标":
                lines.append(("- [x] " if item.get("done") else "- [ ] ") + item.get("text", ""))
            else:
                lines.append(item.get("text", ""))
            lines.append("")

    lines += ["## 下一步", summary.next_step or "", ""]
    lines += ["## 待整理"] + [(item if item.startswith("- ") else f"- {item}") for item in summary.pending] + [""]
    lines += ["## 待用户确认"] + [(item if item.startswith("- ") else f"- {item}") for item in summary.conflicts] + [""]
    lines += ["## 归档"] + summary.archive + [""]
    for name, body in summary.extra.items():
        if name in SECTIONS:
            continue
        lines += [f"## {name}", str(body or ""), ""]
    return "\n".join(lines).rstrip() + "\n"


def parse(text: str) -> Summary:
    text = text.replace("\r\n", "\n").replace("\r\r\n", "\n")
    header: dict = {}
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            front = text[4:end]
            try:
                loaded = yaml.safe_load(front) or {}
                if isinstance(loaded, dict):
                    header = {key: loaded.get(key) for key in HEADER_FIELDS if key in loaded}
                    for key, value in loaded.items():
                        header.setdefault(key, value)
            except yaml.YAMLError:
                header = {}
            body = text[end + 4:]

    summary = Summary(header=header)
    summary.sections = {"用户指令": [], "已完成": [], "决策": [], "未走通的路": [], "证据": [], "未完成目标": []}
    current = ""
    current_unknown = ""
    current_item: dict | None = None
    for line in body.split("\n"):
        if line.startswith("## "):
            current = line[3:].strip()
            current_item = None
            current_unknown = current if current not in SECTIONS else ""
            if current_unknown:
                summary.extra.setdefault(current_unknown, "")
            continue
        if current_unknown:
            existing = summary.extra.get(current_unknown, "")
            summary.extra[current_unknown] = (existing + "\n" + line) if existing else line
            continue
        match = _ITEM_RE.match(line)
        if match and current:
            prefix, number = match.group(1), int(match.group(2))
            if current == "归档":
                current_item = None
                continue
            current_item = {"number": number, "text": "", "prefix": prefix}
            summary.sections[current].append(current_item)
            continue
        archive_match = _ARCHIVE_RE.match(line)
        if archive_match and current == "归档":
            summary.archive.append(line)
            current_item = None
            continue
        if current_item is not None and current:
            current_item["text"] = (current_item["text"] + "\n" + line).strip("\n")
            continue
        if not current:
            continue
        if current == "原始命令":
            summary.raw_command = (summary.raw_command + "\n" + line).lstrip("\n")
        elif current == "目标":
            summary.target = (summary.target + "\n" + line).lstrip("\n")
        elif current == "下一步":
            summary.next_step = (summary.next_step + "\n" + line).lstrip("\n")
        elif current == "待整理" and line.strip():
            summary.pending.append(line.strip() if line.strip().startswith("- ") else f"- {line.strip()}")
        elif current == "待用户确认" and line.strip():
            summary.conflicts.append(line.strip() if line.strip().startswith("- ") else f"- {line.strip()}")
        elif current == "归档" and line.strip():
            summary.archive.append(line)

    for item in summary.sections.get("未完成目标", []):
        text_value = item.get("text", "")
        if text_value.startswith("- [x]"):
            item["done"] = True
            item["text"] = text_value[5:].strip()
        elif text_value.startswith("- [ ]"):
            item["done"] = False
            item["text"] = text_value[5:].strip()

    # 去掉尾部空行/空段
    summary.raw_command = summary.raw_command.rstrip()
    summary.target = summary.target.strip()
    summary.next_step = summary.next_step.strip()
    for section, items in summary.sections.items():
        for item in items:
            item["text"] = str(item.get("text", "")).strip()
    return summary


def next_number(summary: Summary, section: str) -> int:
    numbers = [int(item["number"]) for item in summary.sections.get(section, [])]
    archive_prefix = ITEM_PREFIXES.get(section)
    for line in summary.archive:
        match = _ARCHIVE_RE.match(line)
        if match and match.group(1) == archive_prefix:
            numbers.append(int(match.group(2)))
    return (max(numbers) + 1) if numbers else 1


def add_user(summary: Summary, text: str) -> str:
    number = max((int(item["number"]) for item in summary.sections.get("用户指令", [])), default=0) + 1
    summary.sections.setdefault("用户指令", []).append({"number": number, "text": _clip_user(text)})
    return f"U{number}"


def add_item(summary: Summary, section: str, text: str) -> str:
    prefix = ITEM_PREFIXES[section]
    number = next_number(summary, section)
    summary.sections.setdefault(section, []).append({"number": number, "text": text[:ITEM_MAX_CHARS]})
    _archive_overflow(summary, section)
    return f"{prefix}{number}"


def add_goal(summary: Summary, text: str) -> str:
    number = next_number(summary, "未完成目标")
    summary.sections.setdefault("未完成目标", []).append({"number": number, "text": text[:ITEM_MAX_CHARS], "done": False})
    return f"G{number}"


def _archive_overflow(summary: Summary, section: str) -> None:
    if section not in ("已完成", "决策"):
        return
    items = summary.sections.get(section, [])
    if len(items) <= ARCHIVE_THRESHOLD:
        return
    prefix = ITEM_PREFIXES[section]
    moved = items[:-ARCHIVE_THRESHOLD]
    summary.sections[section] = items[-ARCHIVE_THRESHOLD:]
    for item in moved:
        summary.archive.append(f"- {prefix}{item['number']} {item.get('text', '')}".strip())


def complete_goals(summary: Summary, ids: list[str]) -> list[str]:
    wanted = {str(item).strip().upper() for item in ids or []}
    done: list[str] = []
    for item in summary.sections.get("未完成目标", []):
        identifier = f"G{item['number']}"
        if identifier in wanted:
            item["done"] = True
            done.append(identifier)
    return done


def supersede_decisions(summary: Summary, ids: list[str]) -> list[str]:
    wanted = {str(item).strip().upper() for item in ids or []}
    marked: list[str] = []
    for item in summary.sections.get("决策", []):
        identifier = f"K{item['number']}"
        if identifier in wanted and "被取代" not in item.get("text", ""):
            item["text"] = f"{item['text']}（被取代）"
            marked.append(identifier)
    return marked


def fingerprint(text: str) -> str:
    normalized = "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n")).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def touch(summary: Summary, when: str) -> None:
    summary.header["updated_at"] = when
    summary.header["last_activity_at"] = when


def has_open_goals(summary: Summary) -> bool:
    return any(not item.get("done") for item in summary.sections.get("未完成目标", []))


def last_users(summary: Summary, count: int = 3) -> list[str]:
    return [item.get("text", "") for item in summary.sections.get("用户指令", [])[-count:]]
