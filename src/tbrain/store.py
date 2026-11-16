"""vault 文件读写：目录布局、原子写、区块所有权、项目、提示词、公共池与会话索引。

契约见 ``docs/ARCHITECTURE.md`` 第 5 节。要点：

- Markdown 是唯一数据源；程序只改自己区块（``<!-- owner:model -->``），用户区永不覆盖。
- 所有写操作原子：同目录临时文件 + ``os.replace``。
- 区块指纹 = 规范化后 sha256；程序记住上次写入指纹（``index.blocks`` 表），
  发现指纹不一致即视为用户手改，把该区块转为用户所有并返回 ``False``。
- 程序生成的会话索引文件带“本文件由程序生成”注释头。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import time
import uuid
from functools import wraps
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

import yaml
from filelock import FileLock

from . import clock, naming
from .index import Index


def serialized(method):
    @wraps(method)
    def run(self, *args, **kwargs):
        with self.write_lock():
            return method(self, *args, **kwargs)
    return run

# 目录与固定文件名
GLOBAL_PROMPT_FILE = "全局提示词.md"
GLOBAL_CHANGELOG_FILE = "全局提示词变更日志.md"
SESSIONS_DIR = "会话总结"
PROJECTS_DIR = "项目"
PUBLIC_DIR = "公共池"
INDEPENDENT_DIR = "独立会话池"
ATTACHMENTS_DIR = "附件"
INTERNAL_DIR = ".tbrain"
DELETED_PROJECTS_DIR = "已删除项目"
ARCHIVE_DIR = "归档"
SESSION_INDEX_FILE = "会话索引.md"
PROJECT_CARD_FILE = "项目卡.md"
PROJECT_PROMPT_FILE = "项目提示词.md"
PROJECT_CHANGELOG_FILE = "项目提示词变更日志.md"
INDEPENDENT_PROJECT = "独立"
INDEPENDENT_LABEL = "独立"

PUBLIC_KINDS = ("安装", "环境变量", "PATH", "代理", "配置", "工作区", "工具")
PUBLIC_YAML_FIELDS = (
    "id",
    "kind",
    "name",
    "version",
    "install",
    "package_id",
    "location",
    "scope",
    "uninstall",
    "verify",
    "source_session",
    "created_at",
    "updated_at",
    "state",
    "history",
)
ATTACHMENT_TYPES = ("skills", "插件", "工具", "程序", "模型", "提示词", "参考资料")
PROMPT_SECTIONS = ("最终目标", "硬规则", "当前阶段", "关键路径和命令")
PROMPT_HEADINGS = ("## 用户补充", "## 最终目标", "## 硬规则", "## 当前阶段", "## 关键路径和命令")

INDEX_COMMENT = "<!-- 本文件由程序生成，请勿手动编辑；重新生成会覆盖本文件。 -->"

PROJECT_PROMPT_TEMPLATE_MODEL = "\n\n".join(f"## {section}\n" for section in PROMPT_SECTIONS)
COMPLETION_FILE = "完成标准.md"
WORKTREE_NOTE_FILE = "工作区.md"
COMPLETION_FIELDS = ("goal", "done", "evidence", "verify")
COMPLETION_TEMPLATE = """\
---
fields:
  - goal
  - done
  - evidence
  - verify
---

## 目标
（待填写：这个项目要做成什么样）

## 完成标准
- [ ] （待填写：可以核对的完成条件）

## 证据
（待填写：用什么真实结果证明完成）

## 验证
（待填写：本地检查命令，例如 tbrain check）
"""
GLOBAL_PROMPT_TEMPLATE = {
    "user": "",
    "model": "（第一期不启用模型区）",
}

_SECTION_EXAMPLES = {
    "最终目标": "最终目标（≤3 行）：\n完成 Tbrain 0.6.0 最小闭环，四宿主可接入。",
    "硬规则": "硬规则（≤12 条，每条带来源，如 `#U3`）：\n- 写入只允许原子写 [[会话名]]#U3",
    "当前阶段": "当前阶段（≤5 行）：\n第 4 步：存储/索引层实现中。",
    "关键路径和命令": "关键路径和命令（≤10 条）：\n- `pytest tests/ -q` — 全量测试",
}

_BLOCK_RE = re.compile(
    r"<!--\s*owner:\s*(?P<owner>[A-Za-z0-9_-]+)\s*-->"
    r"(?P<body>.*?)"
    r"<!--\s*/owner:\s*(?P=owner)\s*-->",
    re.S,
)
_BLOCK_ORDER = ("user", "model")

_COMMENT_HEADER = INDEX_COMMENT + "\n\n"


# ---------------------------------------------------------------------------
# Markdown / YAML 小工具


def split_front_matter(text: str) -> tuple[dict, str]:
    """拆分 YAML 文件头与正文；没有文件头时返回 ({}, 原文)。"""
    value = text or ""
    lines = value.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, value
    closing = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            closing = index
            break
    if closing is None:
        return {}, value
    header_text = "\n".join(lines[1:closing])
    body = "\n".join(lines[closing + 1:])
    try:
        data = yaml.safe_load(header_text) if header_text.strip() else {}
    except yaml.YAMLError:
        return {}, value
    if not isinstance(data, dict):
        data = {}
    return data, body


def _worktree_note(name: str, data: dict) -> str:
    status = str(data.get("worktree_status") or "")
    error = str(data.get("worktree_error") or "").strip()
    lines = [
        INDEX_COMMENT,
        "",
        f"# {name} 的工作区",
        "",
        "一个项目只使用一处 git 工作区。",
        "",
        f"- 约定：{data.get('worktree_convention') or ''}",
        f"- 代码目录：{data.get('git_root') or ''}",
        f"- 工作区：{data.get('worktree') or ''}",
        f"- 分支：{data.get('worktree_branch') or ''}",
        f"- 状态：{status}",
        f"- 本地检查：{data.get('check_config') or ''}",
    ]
    if error:
        lines.append(f"- 说明：{error}")
    lines.append("")
    return "\n".join(lines)


def render_note(data: dict, body: str = "") -> str:
    """YAML 文件头 + 正文；中文可读（allow_unicode），键序保持传入顺序。"""
    header = yaml.safe_dump(
        data,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).strip()
    clean_body = str(body or "").strip("\n")
    if clean_body:
        return f"---\n{header}\n---\n\n{clean_body}\n"
    return f"---\n{header}\n---\n"


def parse_blocks(text: str) -> dict[str, dict]:
    """解析 ``<!-- owner:x -->`` 区块，返回 {block: {owner, body}}（保持出现顺序）。"""
    blocks: dict[str, dict] = {}
    for match in _BLOCK_RE.finditer(text or ""):
        owner = match.group("owner")
        blocks[owner] = {
            "owner": owner,
            "body": match.group("body").strip("\n"),
        }
    return blocks


def render_blocks(blocks: dict[str, dict]) -> str:
    """按 user/model 在前、其余按插入顺序渲染区块文件。"""
    names = [name for name in _BLOCK_ORDER if name in blocks]
    names.extend(name for name in blocks if name not in names)
    parts: list[str] = []
    for name in names:
        body = str(blocks[name].get("body") or "").strip("\n")
        parts.append(f"<!-- owner:{name} -->\n{body}\n<!-- /owner:{name} -->")
    return "\n\n".join(parts) + "\n"


def block_fingerprint(text: str) -> str:
    """规范化后 sha256：统一换行、去行尾空白、去首尾空行。"""
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    value = "\n".join(line.rstrip() for line in value.split("\n"))
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()


def public_card_sha(text: str) -> str:
    data, body = split_front_matter(text)
    data.pop('model_fingerprint', None)
    return block_fingerprint(render_note(data, body))[:8]


def _wikilink_pattern(name: str) -> re.Pattern[str]:
    return re.compile(r"\[\[" + re.escape(name) + r"(?=[#|\]])")


def replace_wikilink(text: str, old: str, new: str) -> str:
    """只替换精确的 ``[[旧名]]`` / ``[[旧名|别名]]`` / ``[[旧名#标题]]``。"""
    if not old or old == new:
        return text
    return _wikilink_pattern(old).sub("[[" + str(new), text)


def _escape_table_cell(value: Any) -> str:
    text = str(value if value is not None else "")
    text = text.replace("|", "\\|").replace("\r", " ").replace("\n", " ")
    return text.strip()


# ---------------------------------------------------------------------------
# Store


class Store:
    """vault 的读写门面。``vault`` 是用户知识库，``app_home`` 存索引。"""

    def __init__(self, vault: Path, app_home: Path, *, now_fn=None):
        self.vault = Path(vault)
        self.app_home = Path(app_home)
        self.index = Index(self.app_home, now_fn=now_fn)
        self._now_fn = now_fn
        self._write_lock = FileLock((self.vault / INTERNAL_DIR / 'write.lock').resolve(),
                                    timeout=10, is_singleton=True, preserve_lock_file=True)

    def write_lock(self):
        """跨宿主串行化 Markdown 读改写；同线程的嵌套保存复用锁。"""
        Path(self._write_lock.lock_file).parent.mkdir(parents=True, exist_ok=True)
        return self._write_lock

    # -- 时间 ---------------------------------------------------------------

    def _now(self) -> datetime:
        if self._now_fn is not None:
            value = self._now_fn()
            if isinstance(value, datetime):
                return value if value.tzinfo else value.replace(tzinfo=clock.UTC)
            from datetime import timezone

            return datetime.fromtimestamp(float(value), timezone.utc)
        return clock.utc_now(self.app_home)

    def _stamp(self) -> str:
        return clock.iso_utc(self._now())

    # -- 目录与路径 ---------------------------------------------------------

    def _project_folder(self, project_name: str) -> str:
        name = str(project_name or "").strip()
        if not name or name == INDEPENDENT_LABEL:
            return INDEPENDENT_LABEL
        return naming.sanitize_filename(name)

    def global_prompt_path(self) -> Path:
        return self.vault / GLOBAL_PROMPT_FILE

    def global_changelog_path(self) -> Path:
        return self.vault / GLOBAL_CHANGELOG_FILE

    def project_dir(self, project_name: str) -> Path:
        return self.vault / PROJECTS_DIR / self._project_folder(project_name)

    def project_card_path(self, project_name: str) -> Path:
        return self.project_dir(project_name) / PROJECT_CARD_FILE

    def project_prompt_path(self, project_name: str) -> Path:
        return self.project_dir(project_name) / PROJECT_PROMPT_FILE

    def project_changelog_path(self, project_name: str) -> Path:
        return self.project_dir(project_name) / PROJECT_CHANGELOG_FILE

    def project_index_path(self, project_name: str) -> Path:
        return self.project_dir(project_name) / SESSION_INDEX_FILE

    def independent_index_path(self) -> Path:
        return self.vault / INDEPENDENT_DIR / SESSION_INDEX_FILE

    def session_dir(self, project_name: str, when: datetime) -> Path:
        folder = self._project_folder(project_name)
        stamp = when if isinstance(when, datetime) else clock.parse_utc(str(when))
        return (
            self.vault
            / SESSIONS_DIR
            / folder
            / f"{stamp.year:04d}"
            / f"{stamp.month:02d}"
        )

    def session_path(self, project_name: str, when: datetime, name: str) -> Path:
        directory = self.session_dir(project_name, when)
        directory.mkdir(parents=True, exist_ok=True)
        filename = naming.sanitize_filename(name)
        if filename.lower().endswith(".md"):
            filename = filename[:-3].rstrip(" .")
        return directory / f"{filename}.md"

    def ensure_layout(self) -> None:
        """建目录、默认全局提示词、独立会话索引，并初始化索引库。

        多进程（MCP / hook / 桌面）可能同时首次启动：已存在的文件/目录一律视为成功，
        并发创建时其它进程已写好同内容文件也不报错（见 :meth:`write_text`）。
        """
        for relative in (
            SESSIONS_DIR,
            PROJECTS_DIR,
            PUBLIC_DIR,
            INDEPENDENT_DIR,
            ATTACHMENTS_DIR,
            INTERNAL_DIR,
            f"{PUBLIC_DIR}/{ARCHIVE_DIR}",
        ):
            (self.vault / relative).mkdir(parents=True, exist_ok=True)
        try:
            self._ensure_block_file(
                self.global_prompt_path(),
                {block: {"owner": owner, "body": body} for block, (owner, body) in (
                    ("user", ("user", GLOBAL_PROMPT_TEMPLATE["user"])),
                    ("model", ("model", GLOBAL_PROMPT_TEMPLATE["model"])),
                )},
            )
        except OSError:
            if not self.global_prompt_path().exists():
                raise
        if not self.independent_index_path().exists():
            try:
                self.write_independent_index([])
            except OSError:
                if not self.independent_index_path().exists():
                    raise
        with self.index.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('vault_path', ?)",
                (str(self.vault),),
            )

    # -- 原子读写 -----------------------------------------------------------

    def write_text(self, path: Path, text: str, *, fsync: bool = True, create: bool = False) -> None:
        """同目录临时文件原子发布（UTF-8 无 BOM，``\\n``）。

        ``create=True`` 用硬链接创建新文件，撞名抛 FileExistsError；默认替换已有文件。

        Windows 上两个进程同时替换同一目标文件会短暂返回 ``PermissionError``
        （目标正被另一进程打开/替换），这里做有限重试；若目标内容已等于本次要写的
        内容（另一进程已写成相同结果），直接视为成功，避免并发初始化报错。
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        content = str(text)
        tmp = target.parent / f".{target.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                if fsync:
                    os.fsync(handle.fileno())
            if create:
                # 链接原子发布完整内容；目标已存在时失败，不能覆盖另一个会话。
                os.link(tmp, target)
                return
            last_error: OSError | None = None
            for attempt in range(6):
                try:
                    os.replace(tmp, target)
                    return
                except OSError as exc:  # PermissionError（WinError 5/32）等瞬时错误
                    last_error = exc
                    try:
                        if target.exists() and target.read_text(encoding="utf-8-sig") == content:
                            # 并发写入同内容：另一进程已落盘，本次视为成功。
                            try:
                                tmp.unlink()
                            except OSError:
                                pass
                            return
                    except OSError:
                        pass
                    time.sleep(0.05 * (attempt + 1))
            raise last_error if last_error is not None else OSError("原子写失败")
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

    def read_text(self, path: Path) -> str:
        """读 UTF-8 文本（容忍 BOM）；文件不存在返回空串。"""
        target = Path(path)
        try:
            return target.read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return ""

    def _read_optional(self, path: Path) -> str:
        return self.read_text(path)

    def _rel(self, path: Path) -> str:
        """vault 内相对路径（posix 形式），作为 blocks/files 表的键。"""
        target = Path(path)
        try:
            return target.resolve().relative_to(self.vault.resolve()).as_posix()
        except (ValueError, OSError):
            return str(target)

    # -- 区块所有权 ---------------------------------------------------------

    def block_fingerprint(self, text: str) -> str:
        return block_fingerprint(text)

    def block_state(self, path: Path, block: str) -> dict:
        """区块状态：owner 以 index 记录为准（手改后转为 user），指纹取文件当前内容。"""
        path = Path(path)
        blocks = parse_blocks(self._read_optional(path))
        current = blocks.get(str(block))
        if current is None:
            return {"owner": "", "fingerprint": ""}
        stored = self.index.block_state(self._rel(path), str(block))
        owner = str((stored or {}).get("owner") or current.get("owner") or "")
        return {
            "owner": owner,
            "fingerprint": self.block_fingerprint(current["body"]),
        }

    def _ensure_block_file(self, path: Path, blocks: dict[str, dict]) -> bool:
        """文件不存在时写模板并登记各区块指纹；已存在返回 False。

        并发首次启动时另一进程可能刚好创建了同名文件：只要最终文件内容是同一模板
        （或已存在）就继续，不抛 FileExistsError / PermissionError。
        """
        path = Path(path)
        if path.exists():
            return False
        text = render_blocks(blocks)
        try:
            self.write_text(path, text)
        except OSError:
            if path.exists():
                return False
            raise
        # 只有确认内容就是本模板时才登记指纹，避免把并发写坏的/别人的文件当成模板。
        if self.read_text(path) != text:
            return False
        relative = self._rel(path)
        for name, payload in blocks.items():
            self.index.set_block_state(
                relative, name, payload.get("owner", name), self.block_fingerprint(payload.get("body", ""))
            )
        return True

    @serialized
    def set_block(self, path: Path, block: str, owner: str, text: str) -> bool:
        """只写程序区块；返回 False 表示该区块已归用户所有（不覆盖，文件也不改）。"""
        path = Path(path)
        file_text = self.read_text(path)
        blocks = parse_blocks(file_text)
        if not blocks and file_text.strip():
            return False  # 无区块标记的文件视为用户文件
        block = str(block)
        body = str(text or "").strip("\n")
        relative = self._rel(path)
        stored = self.index.block_state(relative, block)
        stored_owner = str((stored or {}).get("owner") or "")
        stored_fingerprint = str((stored or {}).get("fingerprint") or "")
        current = blocks.get(block)
        if current is not None:
            if str(current.get("owner") or "") == "user" or stored_owner == "user":
                return False
            current_fingerprint = self.block_fingerprint(current.get("body") or "")
            if stored_fingerprint and stored_fingerprint != current_fingerprint:
                # 用户手改过模型区：所有权转为 user（只改索引，不动用户内容）。
                self.index.set_block_state(relative, block, "user", current_fingerprint)
                return False
        blocks[block] = {"owner": str(owner or block), "body": body}
        self.write_text(path, render_blocks(blocks))
        self.index.set_block_state(relative, block, str(owner or block), self.block_fingerprint(body))
        return True

    # -- 全局提示词 ---------------------------------------------------------

    def global_prompt(self) -> str:
        """只返回用户手写区（模型区第一期不启用）。"""
        text = self.read_text(self.global_prompt_path())
        blocks = parse_blocks(text)
        if "user" in blocks:
            return str(blocks["user"].get("body") or "").strip()
        return text.strip()

    @serialized
    def save_global_prompt(self, text: str) -> dict:
        """用户显式保存全局提示词：写【用户区】，model 区原样不动（Lead 决定 2026-10-24）。

        程序自身从不自动改这个文件；只有用户通过桌面/CLI 显式保存才写。
        """
        path = self.global_prompt_path()
        if not path.exists():
            self.ensure_layout()
        file_text = self.read_text(path)
        blocks = parse_blocks(file_text)
        if not blocks:
            # 用户手写的无区块文件：整篇视作用户区，补齐固定格式，不丢内容。
            blocks = {
                "user": {"owner": "user", "body": file_text.strip("\n")},
                "model": {"owner": "model", "body": GLOBAL_PROMPT_TEMPLATE["model"]},
            }
        old_user = str((blocks.get("user") or {}).get("body") or "")
        model_before = str((blocks.get("model") or {}).get("body") or "")
        blocks["user"] = {"owner": "user", "body": str(text or "").strip("\n")}
        blocks.setdefault("model", {"owner": "model", "body": GLOBAL_PROMPT_TEMPLATE["model"]})
        self.write_text(path, render_blocks(blocks))
        self.index.set_block_state(
            self._rel(path), "user", "user", self.block_fingerprint(blocks["user"]["body"])
        )
        model_after = str(parse_blocks(self.read_text(path)).get("model", {}).get("body") or "")
        if model_after != model_before:  # 防御：model 区绝不该被改
            raise RuntimeError("内部错误：保存全局提示词时改动了 model 区")
        if old_user != blocks["user"]["body"]:
            self.append_changelog(
                self.global_changelog_path(),
                "用户区更新（用户手动保存）",
                old_user,
            )
        return {"ok": True, "written": True}

    # -- 项目 --------------------------------------------------------------

    def projects(self) -> list[dict]:
        base = self.vault / PROJECTS_DIR
        if not base.exists():
            return []
        result: list[dict] = []
        for card in sorted(base.glob(f"*/{PROJECT_CARD_FILE}")):
            data, _body = split_front_matter(self.read_text(card))
            name = str(data.get("name") or card.parent.name).strip()
            if not name:
                continue
            result.append(self._project_payload(name, data, card))
        result.sort(key=lambda item: item["name"])
        return result

    def project(self, name: str) -> dict | None:
        value = str(name or "").strip()
        if not value:
            return None
        card = self.project_card_path(value)
        if not card.exists():
            return None
        data, _body = split_front_matter(self.read_text(card))
        if str(data.get('name') or card.parent.name).strip() != value:
            return None
        return self._project_payload(value, data, card)

    def _project_payload(self, name: str, data: dict, card: Path) -> dict:
        paths = data.get("paths") or []
        if isinstance(paths, str):
            paths = [paths]
        paths = [str(item) for item in paths if str(item).strip()]
        fields = data.get("completion_fields") or []
        if isinstance(fields, str):
            fields = [fields]
        return {
            "name": name,
            "paths": paths,
            "parent": str(data.get("parent") or ""),
            "allow_child": bool(data.get("allow_child", False)),
            "read_standalone": data.get("read_standalone") is True,
            "project_id": str(data.get("project_id") or "") or None,
            "created_at": str(data.get("created_at") or ""),
            "session_count": len(self._index_rows(self.project_index_path(name))),
            "path": str(card),
            "git_root": str(data.get("git_root") or ""),
            "worktree": str(data.get("worktree") or ""),
            "worktree_convention": str(data.get("worktree_convention") or ""),
            "worktree_branch": str(data.get("worktree_branch") or ""),
            "worktree_status": str(data.get("worktree_status") or ""),
            "worktree_error": str(data.get("worktree_error") or ""),
            "worktree_limitation": str(data.get("worktree_limitation") or ""),
            "check_config": str(data.get("check_config") or ""),
            "completion_criteria": str(data.get("completion_criteria") or ""),
            "completion_fields": [str(item) for item in fields if str(item).strip()],
        }

    def save_project(
        self,
        name: str,
        paths: Sequence[str],
        parent: str = "",
        allow_child: bool = False,
        rename_from: str = "",
    ) -> dict:
        target_name = self._validate_project_name(name)
        with self.write_lock():
            if self.project_card_path(target_name).exists() and self.project(target_name) is None:
                raise ValueError(f'项目目录已属于其他项目：{self.project_dir(target_name)}')
            rename_from = str(rename_from or "").strip()
            if rename_from and rename_from != target_name:
                if self.project(rename_from) is None:
                    raise ValueError(f'项目不存在：{rename_from}')
                self._rename_project(rename_from, target_name)
            path_values = [self._normalize_path(item) for item in (paths or []) if str(item).strip()]
            parent_name = str(parent or "").strip()
            if parent_name and parent_name == target_name:
                raise ValueError("父项目不能是项目自身")
            if parent_name and self.project(parent_name) is None:
                raise ValueError(f"父项目不存在：{parent_name}")
            ancestors = {target_name}
            ancestor = parent_name
            while ancestor:
                if ancestor in ancestors:
                    raise ValueError('父项目关系不能形成循环')
                ancestors.add(ancestor)
                ancestor = (self.project(ancestor) or {}).get('parent', '')
            card = self.project_card_path(target_name)
            is_new = not card.exists()
            existing_data, existing_body = split_front_matter(self.read_text(card))
            created_at = str(existing_data.get("created_at") or "") or self._stamp()
            data = {
                **existing_data,
                "name": target_name,
                "paths": path_values,
                "parent": parent_name,
                "allow_child": bool(allow_child),
                "created_at": created_at,
            }
            if is_new:
                data["project_id"] = 'p-' + secrets.token_hex(6)
            self.write_text(card, render_note(data, existing_body))
            self._ensure_project_files(target_name)
            return self.project(target_name)

    @serialized
    def set_project_read_standalone(self, name: str, enabled: bool) -> dict:
        """Only controls active access to non-project sessions; public knowledge is unchanged."""
        if not isinstance(enabled, bool):
            raise ValueError('read_standalone 必须是布尔值')
        project = self.project(name)
        if project is None:
            raise ValueError(f'项目不存在：{name}')
        card = Path(project['path'])
        data, body = split_front_matter(self.read_text(card))
        data['read_standalone'] = enabled
        self.write_text(card, render_note(data, body))
        return self.project(name)

    def project_ids(self, *, apply: bool = False, backup_dir: str = '', assignments=None) -> dict:
        """Preview missing IDs; apply the same explicit assignments after a directory backup."""
        requested = {}
        if apply:
            if not backup_dir or not isinstance(assignments, list):
                raise ValueError('应用项目 ID 需要预览清单和备份目录')
            for item in assignments:
                if not isinstance(item, dict):
                    raise ValueError('ID 清单条目必须包含 name 和 project_id')
                name = str(item.get('name') or '').strip()
                project_id = str(item.get('project_id') or '')
                if not name or not re.fullmatch(r'p-[0-9a-f]{12}', project_id) or name in requested:
                    raise ValueError('ID 清单包含无效或重复条目')
                requested[name] = project_id
            if len(set(requested.values())) != len(requested):
                raise ValueError('ID 清单不能给不同项目分配相同 project_id')
            owners = {project['project_id']: project['name'] for project in self.projects() if project['project_id']}
            if any(value in owners and owners[value] != name for name, value in requested.items()):
                raise ValueError('ID 清单中的 project_id 已属于其他项目')
        with self.write_lock() if apply else nullcontext():
            projects, planned, edits = [], [], []
            known = set()
            for project in self.projects():
                name, current = project['name'], project['project_id']
                known.add(name)
                if current:
                    if name in requested and requested[name] != current:
                        raise ValueError('不能改写项目已有的 project_id')
                    projects.append({'name': name, 'project_id': current, 'existing': True, 'source': 'card'})
                    continue
                recovered = set()
                for filename in ('地图.md', '决策日志.md'):
                    data, _ = split_front_matter(self.read_text(self.project_dir(name) / filename))
                    value = str(data.get('project_id') or '')
                    if re.fullmatch(r'p-[0-9a-f]{12}', value):
                        recovered.add(value)
                if len(recovered) > 1:
                    raise ValueError('项目地图与决策日志的 project_id 不一致')
                prior_id = next(iter(recovered), '')
                value = requested.get(name) if apply else prior_id or 'p-' + secrets.token_hex(6)
                if prior_id and value and value != prior_id:
                    raise ValueError('预览清单与既有项目产物的 project_id 不一致')
                projects.append({'name': name, 'project_id': value, 'existing': False,
                                 'source': 'recovered' if prior_id else 'generated' if value else 'unassigned'})
                if not value:
                    continue
                planned.append({'name': name, 'project_id': value})
                if apply:
                    path = Path(project['path'])
                    raw = path.read_bytes()
                    opening = re.match(rb'(?:\xef\xbb\xbf)?---[ \t]*(\r?\n)', raw)
                    if opening is None or not split_front_matter(raw.decode('utf-8-sig'))[0]:
                        raise ValueError('项目卡缺少有效的 YAML 文件头')
                    line = f'project_id: {value}'.encode() + opening[1]
                    edits.append((path, (raw[:opening.end()] + line + raw[opening.end():]).decode('utf-8')))
            if requested.keys() - known:
                raise ValueError('预览清单中有不存在的项目')
            backup = ''
            if apply and edits:
                destination = Path(backup_dir).expanduser().resolve()
                if destination.is_relative_to(self.vault.resolve()):
                    raise ValueError('备份目录不能位于知识库内')
                backup_path = destination / PROJECTS_DIR
                shutil.copytree(self.vault / PROJECTS_DIR, backup_path)
                backup = str(backup_path)
                for path, text in edits:
                    self.write_text(path, text)
                for project in projects:
                    project['existing'] = bool(project['project_id'])
            return {'ok': True, 'projects': projects, 'assignments': planned, 'applied': apply,
                    'updated': len(edits), 'backup': backup}

    @serialized
    def record_execution(self, name: str, setup: dict) -> dict:
        """Record the worktree and write the completion-criteria template once."""
        value = str(name or "").strip()
        if self.project(value) is None:
            raise ValueError(f"项目不存在：{value}")
        card = self.project_card_path(value)
        data, body = split_front_matter(self.read_text(card))
        for key in ("git_root", "worktree", "worktree_convention", "worktree_branch",
                    "worktree_status", "check_config"):
            data[key] = str((setup or {}).get(key) or "")
        error = str((setup or {}).get("worktree_error") or "").strip()
        if error:
            data["worktree_error"] = error
        else:
            data.pop("worktree_error", None)
        limitation = str((setup or {}).get("worktree_limitation") or "").strip()
        if limitation:
            data["worktree_limitation"] = limitation
        else:
            data.pop("worktree_limitation", None)
        data["completion_criteria"] = COMPLETION_FILE
        data["completion_fields"] = list(COMPLETION_FIELDS)
        self.write_text(card, render_note(data, body))
        criteria = self.project_dir(value) / COMPLETION_FILE
        if not criteria.exists():
            self.write_text(criteria, COMPLETION_TEMPLATE)
        self.write_text(self.project_dir(value) / WORKTREE_NOTE_FILE, _worktree_note(value, data))
        return self.project(value) or {}

    def _ensure_project_files(self, name: str) -> None:
        self._ensure_block_file(
            self.project_prompt_path(name),
            {
                "user": {"owner": "user", "body": "## 用户补充\n"},
                "model": {"owner": "model", "body": PROJECT_PROMPT_TEMPLATE_MODEL},
            },
        )
        index_path = self.project_index_path(name)
        if not index_path.exists():
            try:
                self.write_project_index(name, self._index_rows(index_path))
            except OSError:
                if not index_path.exists():
                    raise

    @serialized
    def delete_project(self, name: str) -> dict:
        value = str(name or "").strip()
        project = self.project(value)
        if project is None:
            raise ValueError(f"项目不存在：{value}")
        stamp = re.sub(r"[^0-9A-Za-z]", "", self._stamp())[:15]
        archive_root = self.vault / INTERNAL_DIR / DELETED_PROJECTS_DIR
        archive_root.mkdir(parents=True, exist_ok=True)
        target = archive_root / f"{naming.sanitize_filename(value)}-{stamp}"
        suffix = 2
        while target.exists():
            target = archive_root / f"{naming.sanitize_filename(value)}-{stamp}-{suffix}"
            suffix += 1
        source = self.project_dir(value)
        archived = False
        if source.exists():
            shutil.move(str(source), str(target))
            archived = True
        # 子项目解除父项目引用
        for child in self.projects():
            if child.get("parent") == value:
                card = self.project_card_path(child["name"])
                data, body = split_front_matter(self.read_text(card))
                data["parent"] = ""
                self.write_text(card, render_note(data, body))
        sessions_kept = 0
        sessions_root = self.vault / SESSIONS_DIR / self._project_folder(value)
        if sessions_root.exists():
            sessions_kept = len(list(sessions_root.rglob("*.md")))
        return {
            "ok": True,
            "name": value,
            "archived_to": str(target) if archived else "",
            "sessions_kept": sessions_kept,
        }

    @serialized
    def _rename_project(self, old_name: str, new_name: str) -> dict:
        old = str(old_name or "").strip()
        new = self._validate_project_name(new_name)
        if not old or old == new:
            return {"renamed": False}
        if self.project(new) is not None:
            raise ValueError(f"目标项目已存在：{new}")
        old_dir = self.project_dir(old)
        new_dir = self.project_dir(new)
        if old_dir.exists():
            new_dir.parent.mkdir(parents=True, exist_ok=True)
            if new_dir.exists():
                raise ValueError(f"目标项目目录已存在：{new_dir}")
            shutil.move(str(old_dir), str(new_dir))
        moved_sessions = 0
        old_sessions = self.vault / SESSIONS_DIR / self._project_folder(old)
        new_sessions = self.vault / SESSIONS_DIR / self._project_folder(new)
        if old_sessions.exists():
            new_sessions.parent.mkdir(parents=True, exist_ok=True)
            if new_sessions.exists():
                raise ValueError(f"目标项目会话目录已存在：{new_sessions}")
            shutil.move(str(old_sessions), str(new_sessions))
            rows = self._index_rows(self.project_index_path(new))
            self.write_project_index(new, rows)
        for session_file in sorted((self.vault / SESSIONS_DIR).rglob('*.md')):
            data, _ = split_front_matter(self.read_text(session_file))
            if data.get('project') == old:
                self._set_front_matter_field(session_file, 'project', new)
                moved_sessions += 1
        for attachment in sorted((self.vault / ATTACHMENTS_DIR).rglob('*.md')):
            data, body = split_front_matter(self.read_text(attachment))
            projects = data.get('projects')
            if isinstance(projects, list) and old in projects:
                data['projects'] = [new if item == old else item for item in projects]
                self.write_text(attachment, render_note(data, body))
            elif projects == old:
                data['projects'] = new
                self.write_text(attachment, render_note(data, body))
        # 子项目改父项目名
        for child in self.projects():
            if child.get("parent") == old:
                card = self.project_card_path(child["name"])
                data, body = split_front_matter(self.read_text(card))
                data["parent"] = new
                self.write_text(card, render_note(data, body))
        return {"renamed": True, "from": old, "to": new, "sessions": moved_sessions}

    def _validate_project_name(self, name: str) -> str:
        value = str(name or "").strip()
        if not value:
            raise ValueError("项目名不能为空")
        if value == '__standalone__':
            raise ValueError('项目名不能使用保留名 __standalone__')
        if len(value) > 80:
            raise ValueError("项目名不能超过 80 个字符")
        if value in (".", ".."):
            raise ValueError("项目名不合法")
        for ch in '\\/:*?"<>|':
            if ch in value:
                raise ValueError('项目名不能包含 \\ / : * ? " < > |')
        if value != value.strip(". "):
            raise ValueError("项目名不能以空格或点开头/结尾")
        return value

    @staticmethod
    def _normalize_path(value: str) -> str:
        text = os.path.expanduser(str(value).strip().replace("/", os.sep))
        text = os.path.normpath(text)
        return text

    def match_project(self, cwd: str) -> str:
        """最长路径匹配；Windows 下大小写与 ``/``、``\\`` 不敏感；无匹配返回 ""。"""
        key = _path_key(cwd)
        if not key:
            return ""
        best_name = ""
        best_length = -1
        for project in self.projects():
            candidates = list(project.get("paths") or [])
            if project.get("worktree_status") == "worktree" and project.get("worktree"):
                candidates.append(project["worktree"])
            for path in candidates:
                candidate = _path_key(path)
                if not candidate:
                    continue
                if _path_within(key, candidate) and len(candidate) > best_length:
                    best_name = project["name"]
                    best_length = len(candidate)
        return best_name

    # -- 提示词 ------------------------------------------------------------

    def project_prompt(self, name: str) -> dict:
        value = str(name or "").strip()
        if self.project(value) is None:
            raise ValueError(f"项目不存在：{value}")
        path = self.project_prompt_path(value)
        text = self.read_text(path)
        blocks = parse_blocks(text)
        model_body = str((blocks.get("model") or {}).get("body") or "")
        user_body = str((blocks.get("user") or {}).get("body") or "")
        if not blocks and text.strip():
            model_body = text
        sections = _parse_sections(model_body)
        user = user_body.strip()
        user = re.sub(r"^##\s*用户补充\s*", "", user).strip()
        return {"sections": sections, "user": user}

    @serialized
    def save_project_prompt(self, name: str, sections: dict, *, summary=None,
                            base_sha: dict | None = None, session_id: str = '') -> dict:
        results = {section: 'rejected' for section in sections}
        try:
            value = str(name or '').strip()
            if self.project(value) is None:
                raise ValueError(f'项目不存在：{value}')
            accepted, rejected, warnings = self._validate_prompt_sections(value, sections, summary)
            path = self.project_prompt_path(value)
            original = self.read_text(path)
            front, original_body = split_front_matter(original)
            blocks = parse_blocks(original)
            current = self.project_prompt(value)
            old_sections = current['sections']
            fingerprints = front.get('model_fingerprints') or {}
            protected = set(front.get('user_owned_sections') or [])
            model = blocks.get('model', {})
            model_body = model.get('body', '') if blocks else original_body
            existing_sections = {re.sub(r'\s*〔sha:[^〕]*〕\s*$', '', line[3:].strip())
                                 for line in model_body.splitlines() if line.startswith('## ')}
            state = self.index.block_state(self._rel(path), 'model') or {}
            for section, content in old_sections.items():
                if section in fingerprints and fingerprints[section] != block_fingerprint(content):
                    protected.add(section)
                elif not fingerprints and content and (model.get('owner') == 'user' or
                        state.get('fingerprint') != block_fingerprint(model.get('body', ''))):
                    protected.add(section)
            pending = ''
            for section in list(accepted):
                if section in protected:
                    proposed = accepted.pop(section)
                    pending = self.record_conflict(value, f'{value} 项目提示词「{section}」已被用户修改，待用户确认', proposed)
                    rejected.append({'section': section, 'reason': pending, 'example': _SECTION_EXAMPLES[section]})
                elif base_sha is not None and section in base_sha and section in existing_sections:
                    base = base_sha[section]
                    head = {'sha': block_fingerprint(old_sections[section])[:8], 'text': old_sections[section]}
                    if not base or base[:8] != head['sha']:
                        proposed = accepted.pop(section)
                        conflict = bool(base)
                        reason = '写入冲突' if conflict else '缺少 base_sha'
                        if conflict:
                            pending = self.record_conflict(value, f'写入冲突：{value} 项目提示词「{section}」',
                                                           f'当前生效版本：\n{head["text"]}\n\n己方草稿：\n{proposed}')
                            results[section] = 'conflict'
                        rejected.append({'section': section, 'reason': reason, 'conflict': conflict,
                                         'base': base, 'head': head})
            merged = {**old_sections, **accepted}
            front['user_owned_sections'] = sorted(protected)
            front['model_fingerprints'] = {section: fingerprints.get(section, block_fingerprint(old_sections[section]))
                                          if section in protected else block_fingerprint(content)
                                          for section, content in merged.items()}
            model_body = _replace_sections(model_body, accepted)
            if accepted or protected:
                rendered_model = render_blocks({'model': {'owner': 'model', 'body': model_body}}).rstrip('\n')
                if 'model' in blocks:
                    body = _BLOCK_RE.sub(lambda match: rendered_model if match['owner'] == 'model' else match[0],
                                         original_body)
                elif blocks:
                    body = original_body.rstrip() + '\n\n' + rendered_model
                else:
                    body = rendered_model
                self.write_text(path, render_note(front, body))
                self.index.set_block_state(self._rel(path), 'model', 'user' if protected else 'model',
                                           block_fingerprint(model_body))
            changed = [section for section in accepted if accepted[section] != old_sections[section]]
            if changed:
                self.append_changelog(self.project_changelog_path(value), '替换项目提示词：' + '、'.join(changed),
                                      '\n\n'.join(f'## {section}\n{old_sections[section]}' for section in changed))
            results.update({section: 'ok' for section in accepted})
            return {'written': accepted, 'rejected': rejected, 'warnings': warnings, 'pending_confirm': pending}
        finally:
            for section, result in results.items():
                self._log_w3_write(session_id or getattr(summary, 'session_id', ''), f'{name}/{section}',
                                   bool(base_sha and base_sha.get(section)), result)

    def _validate_prompt_sections(self, project_name: str, sections: dict, summary=None):
        from . import prompt
        if summary and isinstance(sections, dict) and isinstance(sections.get('硬规则'), str):
            def qualify(match):
                parts = re.split(r'(\[\[[^\]]+\]\]#[UEDKXG]\d+)', match[1])
                for index in range(0, len(parts), 2):
                    parts[index] = re.sub(r'#?\b([UEDKXG]\d+)\b',
                                          lambda ref: f'[[{summary.name}]]#{ref[1]}', parts[index])
                return '（来源：' + ''.join(parts) + '）'
            sections = {**sections, '硬规则': prompt._SOURCE_RE.sub(qualify, sections['硬规则'])}
        result = prompt.validate(sections, resolve_source=self._prompt_resolver(project_name, summary),
                                 path_exists=_path_exists)
        return result['written'], result['rejected'], result['warnings']

    def _prompt_resolver(self, project_name: str, summary=None):
        resolve = _rule_resolver(self.vault, project_name, summary, rows=self.index.sessions())
        return lambda name, reference: resolve(name, reference)['state'] == 'active'

    def classify_project_rules(self, project_name: str) -> dict[str, list[str]]:
        from .prompt import classify_rules
        resolve = _rule_resolver(self.vault, project_name, rows=self.index.sessions())
        return classify_rules(self.project_prompt(project_name)['sections']['硬规则'], resolve)

    def record_conflict(self, project: str, title: str, proposed: str) -> str:
        path = (self.project_dir(project) if project else self.vault / PUBLIC_DIR) / '待用户确认.md'
        self.append_changelog(path, title, proposed)
        return title

    def pending_confirmations(self, project: str) -> str:
        files = [self.vault / PUBLIC_DIR / '待用户确认.md']
        if project:
            files.append(self.project_dir(project) / '待用户确认.md')
        contents = [self._read_optional(path).strip() for path in files]
        return '\n\n'.join(['## 待用户确认'] + [text for text in contents if text]) if any(contents) else ''

    # -- 公共池 ------------------------------------------------------------

    def handoff_items(self, project_name: str | list[str], limit: int = 3, *,
                      decision_projects: list[str] | None = None) -> list[dict]:
        """有效历史决定，供 bootstrap 与 CLI 交接；会话结束不代表决定失效。

        传入项目列表时用于项目树（根项目 + 子项目），每条结果带 ``project``，
        便于跨实例/Router 判断决定属于谁。
        """
        from . import handoff

        limit = max(1, min(int(limit or 3), 10))
        projects = decision_projects if decision_projects is not None else (
            [project_name] if isinstance(project_name, str) else project_name)
        decisions = [entry for project in projects for entry in reversed(self.project_decisions(project))
                     if entry['active']]
        decisions.sort(key=lambda entry: entry['at'], reverse=True)
        items: list[dict] = []
        for decision in decisions[:limit]:
            row = self.index.session(decision.get('session_id', '')) or {}
            items.append({
                'session': row.get('name') or decision.get('session_id', ''),
                'id': decision['id'], 'text': decision['text'], 'at': decision['at'],
                'source': f"[[{Path(decision['path']).with_suffix('').as_posix()}]]#{decision['id']}",
                'project': decision['project'], 'verified': False, 'git_head': '',
            })
        rows = self.index.sessions(project_name)
        rows.sort(key=lambda row: (str(row.get("updated_at") or ""), float(row.get("file_mtime") or 0)),
                  reverse=True)
        for row in rows:
            if len(items) >= limit:
                break
            path = self.vault / str(row.get("path") or "")
            try:
                summary = handoff.parse(self.read_text(path))
            except OSError:
                continue
            decisions = summary.active_decisions()
            if not decisions:
                continue
            name = summary.name or str(row.get("name") or "")
            try:
                verify = json.loads(str(row.get("last_verify") or "{}"))
            except ValueError:
                verify = {}
            for decision in reversed(decisions):
                if len(items) >= limit:
                    break
                items.append({
                    "session": name,
                    "id": decision.get("id", ""),
                    "text": str(decision.get("text") or ""),
                    "at": str(row.get("updated_at") or row.get("last_activity_at") or ""),
                    "source": f"[[{name}]]#{decision['id']}",
                    "project": str(row.get("project") or ""),
                    "verified": bool(verify) and verify.get("exit_code") == 0,
                    "git_head": str(row.get("git_head") or ""),
                })
        return items

    def _public_kind_dir(self, kind: str) -> Path:
        return self.vault / PUBLIC_DIR / str(kind)

    def _public_card_path(self, kind: str, name: str) -> Path:
        return self._public_kind_dir(kind) / (naming.sanitize_filename(name) + ".md")

    def public_cards(self, kind: str | None = None) -> list[dict]:
        base = self.vault / PUBLIC_DIR
        if not base.exists():
            return []
        kinds = [str(kind)] if kind else sorted(
            [child.name for child in base.iterdir() if child.is_dir() and child.name not in {ARCHIVE_DIR, '冲突'}]
        )
        cards: list[dict] = []
        for kind_name in kinds:
            if kind_name == '冲突':
                continue
            directory = base / kind_name
            if not directory.is_dir():
                continue
            for card_path in sorted(directory.glob("*.md")):
                data, body = split_front_matter(self.read_text(card_path))
                card = dict(data)
                card.setdefault("kind", kind_name)
                card.setdefault("name", card_path.stem)
                card.setdefault("state", "生效")
                card["kind"] = str(card.get("kind") or kind_name)
                card["name"] = str(card.get("name") or card_path.stem)
                card["id"] = str(card.get("id") or f"{card['kind']}-{card['name']}")
                card["path"] = str(card_path)
                card["body"] = str(body or "").strip()
                card["archived"] = False
                cards.append(card)
        cards.sort(key=lambda item: (str(item.get("kind") or ""), str(item.get("name") or "")))
        return cards

    @serialized
    def _log_w3_write(self, session_id: str, target: str, has_base: bool, result: str) -> None:
        try:
            with (self.app_home / 'w3-writes.jsonl').open('a', encoding='utf-8') as handle:
                handle.write(json.dumps({'at': self._stamp(), 'session_id': session_id, 'target': target,
                                         'has_base': has_base, 'result': result}, ensure_ascii=False) + '\n')
        except OSError:
            pass

    @serialized
    def save_public_card(
        self,
        kind: str,
        name: str,
        meta: dict | None = None,
        source_session: str = '',
        *,
        base_sha: str | None = None,
        session_id: str = '',
    ) -> dict:
        """同对象新卡生效；旧卡先写归档副本，现行路径始终存在。"""
        kind_value = str(kind or '').strip()
        name_value = str(name or '').strip()
        meta = dict(meta or {})
        base_sha = meta.pop('base_sha', base_sha)
        result = 'rejected'
        is_replacement = False
        try:
            if kind_value not in PUBLIC_KINDS:
                raise ValueError('公共池类型必须是：' + '、'.join(PUBLIC_KINDS))
            if not name_value:
                raise ValueError('公共池卡片名称不能为空')
            stamp = self._stamp()
            replaced = None
            archived_path = ''
            existing = [card for card in self.public_cards(kind_value)
                        if str(card.get('name') or '').strip().casefold() == name_value.casefold()
                        or str(card.get('id') or '') == f'{kind_value}-{name_value}']
            if existing:
                is_replacement = True
                old = existing[0]
                old_path = Path(old['path'])
                old_text = self.read_text(old_path)
                old_data, old_body = split_front_matter(old_text)
                fingerprint = old_data.pop('model_fingerprint', '')
                current_fingerprint = block_fingerprint(render_note(old_data, old_body))
                if old_data.get('owner') == 'user' or fingerprint != current_fingerprint:
                    message = self.record_conflict('', f'公共池 {kind_value}/{name_value} 已被用户修改或缺少原写入指纹，待用户确认',
                                                   render_note(meta, str(meta.get('body') or '')))
                    raise ValueError(message)
                if base_sha is not None and (not base_sha or base_sha[:8] != current_fingerprint[:8]):
                    conflict = bool(base_sha)
                    rejection = {'id': str(old['id']), 'path': str(old_path), 'base': base_sha,
                                 'reason': '写入冲突' if conflict else '缺少 base_sha', 'conflict': conflict,
                                 'head': {'sha': current_fingerprint[:8], 'text': old_text}}
                    if conflict:
                        conflict_dir = self.vault / PUBLIC_DIR / '冲突'
                        conflict_name = naming.sanitize_filename(f'{stamp}-{kind_value}-{name_value}')
                        conflict_path = conflict_dir / f'{conflict_name}.md'
                        suffix = 2
                        while conflict_path.exists():
                            conflict_path = conflict_dir / f'{conflict_name}-{suffix}.md'
                            suffix += 1
                        draft = render_note({key: value for key, value in meta.items() if key != 'body'},
                                            str(meta.get('body') or ''))
                        self.write_text(conflict_path, f'# 写入冲突：{kind_value}/{name_value}\n\n'
                                        f'## 当前生效版本\n\n{old_text}\n\n## 己方草稿\n\n{draft}')
                        rejection['conflict_path'] = str(conflict_path)
                        result = 'conflict'
                    return rejection
                old_data['state'] = '已被取代'
                old_data['updated_at'] = stamp
                history = old_data.get('history') or []
                if not isinstance(history, list):
                    history = []
                history.append({'at': stamp, 'action': '已被取代', 'note': f'新卡 {kind_value}-{name_value} 生效'})
                old_data['history'] = history
                archive_dir = self._public_kind_dir(kind_value) / ARCHIVE_DIR
                archive_path = archive_dir / old_path.name
                suffix = 2
                while archive_path.exists():
                    archive_path = archive_dir / f'{old_path.stem}-{suffix}{old_path.suffix}'
                    suffix += 1
                self.write_text(archive_path, render_note(old_data, old_body))
                replaced = {'id': str(old.get('id') or ''), 'path': str(old_path)}
                archived_path = str(archive_path)
                card_path = old_path
            else:
                card_path = self._public_card_path(kind_value, name_value)
                suffix = 2
                while card_path.exists():
                    card_path = self._public_kind_dir(kind_value) / f'{naming.sanitize_filename(name_value)}-{suffix}.md'
                    suffix += 1
            created_at = str(meta.get('created_at') or '') or str((existing[0] if existing else {}).get('created_at') or '') or stamp
            data: dict[str, Any] = {
                'id': f'{kind_value}-{name_value}', 'kind': kind_value, 'name': name_value,
                'version': str(meta.get('version') or ''), 'install': str(meta.get('install') or ''),
                'package_id': str(meta.get('package_id') or ''), 'location': str(meta.get('location') or ''),
                'scope': str(meta.get('scope') or ''), 'uninstall': str(meta.get('uninstall') or ''),
                'verify': str(meta.get('verify') or ''),
                'source_session': str(meta.get('source_session') or source_session or ''),
                'created_at': created_at, 'updated_at': stamp, 'state': '生效',
                'history': [{'at': stamp, 'action': '创建' if not existing else '取代旧卡',
                             'note': str(meta.get('note') or '')}],
            }
            for key, value in meta.items():
                if key not in ('body', 'note') and key not in data and key not in PUBLIC_YAML_FIELDS:
                    data[key] = value
            body = str(meta.get('body') or '')
            data['owner'] = 'model'
            data.pop('model_fingerprint', None)
            data['model_fingerprint'] = block_fingerprint(render_note(data, body))
            self.write_text(card_path, render_note(data, body))
            if replaced:
                self.index.upsert_public({'id': str(old.get('id') or f'{kind_value}-{name_value}'),
                                          'kind': kind_value, 'name': str(old.get('name') or name_value),
                                          'state': '已被取代', 'path': archived_path, 'updated_at': stamp}, old_body)
            self.index.upsert_public({'id': data['id'], 'kind': kind_value, 'name': name_value,
                                      'state': '生效', 'path': str(card_path), 'updated_at': stamp}, body)
            result = 'ok'
            return {'id': str(data['id']), 'path': str(card_path), 'state': '生效',
                    'replaced': replaced, 'archived_path': archived_path}
        finally:
            if is_replacement:
                self._log_w3_write(session_id, f'公共池/{kind_value}/{name_value}', bool(base_sha), result)

    def public_l1(self, max_lines: int = 40) -> str:
        """公共池 L1 索引（程序生成，≤40 行）：每类一行 + 热点事实。"""
        cards = [card for card in self.public_cards() if card.get('state') == '生效']
        by_kind: dict[str, list[dict]] = {}
        for card in cards:
            by_kind.setdefault(str(card.get("kind") or ""), []).append(card)
        lines = ["# 公共池（程序生成）"]
        ordered_kinds = [kind for kind in PUBLIC_KINDS if kind in by_kind]
        ordered_kinds.extend(kind for kind in sorted(by_kind) if kind not in ordered_kinds)
        for kind_name in ordered_kinds:
            entries = by_kind[kind_name]
            latest = max((str(entry.get("updated_at") or "") for entry in entries), default="")
            lines.append(f"- {kind_name}：{len(entries)} 条，最近变更 {latest[:10] or '—'}")
        lines.append("")
        lines.append("## 热点事实")
        recent = sorted(
            cards,
            key=lambda item: (str(item.get("updated_at") or ""), str(item.get("name") or "")),
            reverse=True,
        )
        for card in recent[:8]:
            detail = (
                card.get("version")
                or card.get("location")
                or card.get("scope")
                or _one_line(str(card.get("body") or ""))
            )
            suffix = f"：{detail}" if detail else ""
            lines.append(f"- {card.get('kind')}·{card.get('name')}{suffix}")
        if len(lines) > max_lines:
            lines = lines[: max(0, max_lines - 1)] + ["（更多内容请调用 search 查询）"]
        return "\n".join(lines)

    # -- 附件（只读） -------------------------------------------------------

    def attachments(self, project_name: str = "") -> list[dict]:
        base = self.vault / ATTACHMENTS_DIR
        if not base.exists():
            return []
        project = str(project_name or "").strip()
        identities = {project} if project else set()
        if project:
            record = self.project(project) or next(
                (item for item in self.projects() if item.get('project_id') == project), None)
            if record:
                identities.add(record['name'])
                if record.get('project_id'):
                    identities.add(record['project_id'])
        cards: list[dict] = []
        for directory in sorted(child for child in base.iterdir() if child.is_dir()):
            for card_path in sorted(directory.glob("*.md")):
                data, body = split_front_matter(self.read_text(card_path))
                card_type = str(data.get("type") or directory.name)
                projects = data.get("projects") or []
                if isinstance(projects, str):
                    projects = [projects]
                projects = [str(item) for item in projects]
                if projects and not identities.intersection(projects):
                    continue
                cards.append(
                    {
                        "name": str(data.get("name") or card_path.stem),
                        "type": card_type,
                        "path": str(data.get("path") or ""),
                        "note": str(data.get("note") or ""),
                        "group": str(data.get("group") or ""),
                        "projects": projects,
                        "card_path": str(card_path),
                        "body": str(body or "").strip(),
                    }
                )
        cards.sort(key=lambda item: (item["type"], item["name"]))
        return cards

    # -- 会话索引（程序生成） -----------------------------------------------

    def write_project_index(self, project_name: str, rows: Sequence[dict]) -> Path:
        path = self.project_index_path(project_name)
        rows = list(rows or [])
        lines = [_COMMENT_HEADER, f"# {project_name} 会话索引", "", _render_index_table(rows)]
        self.write_text(path, "\n".join(lines).rstrip() + "\n")
        return path

    def write_independent_index(self, rows: Sequence[dict]) -> Path:
        path = self.independent_index_path()
        rows = list(rows or [])
        lines = [_COMMENT_HEADER, "# 独立会话池 会话索引", ""]
        groups: dict[str, list[dict]] = {}
        order: list[str] = []
        for row in rows:
            group = str(row.get("cwd") or "").strip() or "未记录 cwd"
            if group not in groups:
                groups[group] = []
                order.append(group)
            groups[group].append(row)
        for group in order:
            lines.extend([f"## {group}", "", _render_index_table(groups[group]), ""])
        if not rows:
            lines.append("（暂无独立会话）")
        self.write_text(path, "\n".join(lines).rstrip() + "\n")
        return path

    def _index_rows(self, path: Path) -> list[dict]:
        """从程序生成的索引文件读回表格行（用于 rename 与项目会话数）。"""
        text = self.read_text(Path(path))
        rows: list[dict] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("|"):
                continue
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if len(cells) < 2:
                continue
            link = re.match(r"\[\[([^\]|#]+)(?:[#|][^\]]*)?\]\]", cells[0])
            if not link:
                continue
            rows.append(
                {
                    "name": link.group(1),
                    "status": cells[1] if len(cells) > 1 else "",
                    "agent": cells[2] if len(cells) > 2 else "",
                    "updated_at": cells[3] if len(cells) > 3 else "",
                    "open_goals": cells[4] if len(cells) > 4 else "",
                    "next_step": cells[5] if len(cells) > 5 else "",
                }
            )
        return rows

    # -- 重命名与链接 -------------------------------------------------------

    @serialized
    def rename_session(self, old_path: Path, new_name: str) -> Path:
        """移动文件 + 同步索引/其他总结中的 ``[[旧名]]`` + 更新 blocks 表 path。"""
        old = Path(old_path)
        if not old.exists():
            raise FileNotFoundError(f"会话文件不存在：{old}")
        old_name = old.stem
        if naming.sanitize_filename(new_name).lower().endswith(".md"):
            new_stem = naming.sanitize_filename(new_name)[:-3].rstrip(" .")
        else:
            new_stem = naming.sanitize_filename(new_name)
        if not new_stem or new_stem == old_name:
            return old
        new_path = old.parent / f"{new_stem}.md"
        if new_path.exists():
            raise ValueError(f"同名会话已存在：{new_stem}")
        original_text = self.read_text(old)
        front, body = split_front_matter(original_text)
        project = str(front.get("project") or "")
        aliases = front.get('aliases') or []
        if isinstance(aliases, str):
            aliases = [aliases]
        previous_name = str(front.get('name') or old_name)
        front['aliases'] = list(dict.fromkeys([name for name in aliases if name != previous_name] + [previous_name]))[-10:]
        front['name'] = new_stem
        front['provisional_name'] = False
        os.replace(str(old), str(new_path))
        old_rel = self._rel(old)
        new_rel = self._rel(new_path)
        self.index.move_block_path(old_rel, new_rel)
        self.write_text(new_path, render_note(front, body))
        targets: list[Path] = [new_path]
        sessions_root = self.vault / SESSIONS_DIR
        if sessions_root.exists():
            targets.extend(sorted(sessions_root.rglob("*.md")))
        for index_path in (self.project_index_path(project), self.independent_index_path()):
            if index_path.exists():
                targets.append(index_path)
        seen: set[str] = set()
        for target in targets:
            key = str(target.resolve()) if target.exists() else str(target)
            if key in seen:
                continue
            seen.add(key)
            text = self.read_text(target)
            replaced = replace_wikilink(text, old_name, new_stem)
            if replaced != text:
                self.write_text(target, replaced)
        return new_path

    def _set_front_matter_field(self, path: Path, key: str, value: Any) -> None:
        text = self.read_text(Path(path))
        data, body = split_front_matter(text)
        if not data and not text.lstrip().startswith("---"):
            return
        data[key] = value
        self.write_text(Path(path), render_note(data, body))

    # -- 变更日志 -----------------------------------------------------------

    @serialized
    def write_memory_map(self, project: str, text: str) -> Path:
        if self.project(project) is None:
            raise ValueError(f'项目不存在：{project}')
        path = self.project_dir(project) / '地图.md'
        self.write_text(path, text)
        return path

    def project_decisions(self, project: str) -> list[dict]:
        path = self.project_dir(project) / '决策日志.md'
        entries = []
        for match in re.finditer(r'^## (DL-\d+) — ([^\n]+)\n(.*?)(?=^## DL-\d+ — |\Z)',
                                 self.read_text(path), re.MULTILINE | re.DOTALL):
            entry = {'id': match[1], 'at': match[2], 'project': project, 'path': self._rel(path)}
            field = None
            for line in match[3].splitlines():
                value = re.match(r'^([a-z_]+): ?(.*)$', line)
                if value:
                    field = value[1]
                    entry[field] = value[2]
                elif field and line.startswith('  '):
                    entry[field] += '\n' + line[2:]
            entry['supersedes'] = str(entry.get('supersedes') or '').split(', ') if entry.get('supersedes') else []
            entries.append(entry)
        retired = {identifier for entry in entries for identifier in entry['supersedes']}
        for entry in entries:
            entry['active'] = entry['id'] not in retired
        return entries

    @serialized
    def append_decision_log(self, project: str, text: str, why: str, decided_by: str,
                            session_id: str, source: str = '', supersedes: list[str] | None = None) -> dict:
        card = self.project(project)
        if card is None:
            raise ValueError(f'项目不存在：{project}')
        text, why = str(text or '').strip(), str(why or '').strip()
        if not text or len(text) + len(why) > 200:
            raise ValueError('决策正文必填，与理由合计不超过 200 字；未说明理由时留空')
        path = self.project_dir(project) / '决策日志.md'
        existing = self.read_text(path)
        supersedes = supersedes or []
        identifiers = {entry['id'] for entry in self.project_decisions(project)}
        if any(identifier not in identifiers for identifier in supersedes):
            raise ValueError('supersedes 必须引用当前项目已有的 DL-xxxx 决定')
        number = max((int(value) for value in re.findall(r'^## DL-(\d+) —', existing, re.MULTILINE)), default=0) + 1
        entry = {'id': f'DL-{number:04d}', 'at': self._stamp(), 'session_id': session_id,
                 'text': text, 'why': why, 'decided_by': decided_by}
        if source:
            entry['source'] = source
        if supersedes:
            entry['supersedes'] = ', '.join(supersedes)
        if not existing.strip():
            existing = render_note({'project_id': card.get('project_id') or '未回填'},
                                   '# 决策日志\n\n程序只追加，用户不手改；删除文件后编号重新开始。')
        fields = [f'{key}: ' + '\n  '.join(str(value).splitlines())
                  for key, value in entry.items() if key not in ('id', 'at')]
        self.write_text(path, existing.rstrip() + f'\n\n## {entry["id"]} — {entry["at"]}\n\n' + '\n'.join(fields) + '\n')
        return {**entry, 'path': str(path)}

    def append_changelog(self, path: Path, title: str, old: str) -> None:
        """追加一条变更记录（含旧内容，便于还原）；整体仍走原子写。"""
        path = Path(path)
        existing = self.read_text(path)
        if not existing.strip():
            existing = "# 变更日志\n"
        lines = [existing.rstrip(), "", f"## {self._stamp()} — {str(title).strip()}"]
        old_text = str(old or "").strip()
        if old_text:
            lines.append("旧内容：")
            lines.append("")
            lines.extend("> " + line if line else ">" for line in old_text.splitlines())
        else:
            lines.append("旧内容：（无）")
        lines.append("")
        self.write_text(path, "\n".join(lines).rstrip() + "\n")


# ---------------------------------------------------------------------------
# 模块级辅助


def _rule_resolver(vault: Path, project: str, summary=None, *, rows=None):
    from . import handoff
    vault = Path(vault).resolve()
    parsed = {}
    if rows is None:
        rows = []
        for path in sorted((vault / SESSIONS_DIR).rglob('*.md')):
            source = handoff.parse(path.read_text(encoding='utf-8-sig'))
            if source.session_id:
                parsed[str(path)] = source
                rows.append({'path': str(path), 'session_id': source.session_id,
                             'name': source.name, 'project': source.project})
    else:
        rows = [dict(row) for row in rows]
    if summary is not None:
        rows = [row for row in rows if row['session_id'] != summary.session_id]
        rows.append({'path': '', 'session_id': summary.session_id,
                     'name': summary.name, 'project': summary.project})

    def load(row):
        if summary is not None and row['session_id'] == summary.session_id:
            return summary
        path = vault / row['path']
        key = str(path)
        if key not in parsed:
            try:
                parsed[key] = handoff.parse(path.read_text(encoding='utf-8-sig'))
            except FileNotFoundError:
                parsed[key] = None
        return parsed[key]

    def result(source, name, reference):
        item = None
        if source is not None:
            if reference.startswith('U'):
                item = next((item for item in source.users if item['id'] == reference), None)
            else:
                section = next((key for key, value in handoff.ITEM_PREFIXES.items() if reference.startswith(value)), None)
                if section:
                    item = next((item for item in source.all_items(section) if item['id'] == reference), None)
        state = handoff.decision_state(item['text']) if item and reference.startswith('K') else 'active' if item else 'unresolved'
        return {'state': state, 'text': item['text'] if item else '',
                'session': source.name if source else name, 'reference': reference}

    def alias_matches(row, name):
        source = load(row)
        aliases = (source.header.get('aliases') or []) if source else []
        return name == aliases if isinstance(aliases, str) else name in aliases

    def resolve(name, reference):
        if not name:
            return result(summary, name, reference)
        local = [row for row in rows if row['project'] == project]
        for candidates, aliases in ((local, False), (local, True), (rows, False), (rows, True)):
            matches = [row for row in candidates if (alias_matches(row, name) if aliases else row['name'] == name)]
            if matches:
                return result(load(matches[0]) if len(matches) == 1 else None, name, reference)
        return result(None, name, reference)
    return resolve


def export_project(vault: Path, project_name: str) -> str:
    """Read a portable Markdown fragment without constructing a store, index, or service."""
    from . import prompt
    from .check import find_configs
    vault = Path(vault).resolve()
    project_name = str(project_name or '').strip()
    if not project_name:
        raise ValueError('导出需要项目名')

    def project_files(name):
        directory = vault / PROJECTS_DIR / naming.sanitize_filename(name)
        card = directory / PROJECT_CARD_FILE
        if not card.is_file():
            raise ValueError(f'项目不存在：{name}')
        data, _ = split_front_matter(card.read_text(encoding='utf-8-sig'))
        text = (directory / PROJECT_PROMPT_FILE).read_text(encoding='utf-8-sig')
        front, body = split_front_matter(text)
        blocks = parse_blocks(body)
        model = str((blocks.get('model') or {}).get('body') or '') if blocks else body
        parsed = {'sections': _parse_sections(model),
                  'user': str((blocks.get('user') or {}).get('body') or '')}
        if front.get('body_encoding') == 'escaped-headings':
            parsed['sections'] = {key: re.sub(r'(?m)^\\(?=## |###\s+[UDKXEG]\d+\s*$)', '', value)
                                  for key, value in parsed['sections'].items()}
            parsed['user'] = re.sub(r'(?m)^\\(?=## |###\s+[UDKXEG]\d+\s*$)', '', parsed['user'])
        return data, parsed

    def plain(text):
        text = prompt._WIKI_REF_RE.sub(lambda match: f'{match[1]} {match[3]}', str(text))
        text = re.sub(r'\[\[([^\]|]+)(?:\|([^\]]+))?\]\]', lambda match: match[2] or match[1], text)
        text = prompt._SOURCE_RE.sub(lambda match: f'（来源说明：{match[1]}）', text)
        return re.sub(r'\s*〔sha:[^〕]*〕', '', text)

    card, data = project_files(project_name)
    parts = [f'# {project_name} 项目说明']
    if data['user'].strip():
        user = re.sub(r'^##\s*用户补充\s*', '', data['user']).strip()
        parts.append('## 用户补充\n' + plain(user))
    for section in ('最终目标', '当前阶段', '关键路径和命令'):
        if data['sections'][section]:
            parts.append(f'## {section}\n' + plain(data['sections'][section]))
    pending, inactive = [], []
    name, ancestors = project_name, set()
    while name:
        if name in ancestors:
            raise ValueError('项目卡父级关系形成循环，请修正项目卡')
        ancestors.add(name)
        current_card, current = (card, data) if name == project_name else project_files(name)
        groups = prompt.classify_rules(current['sections']['硬规则'], _rule_resolver(vault, name))
        active = [plain(line) for line in groups['active']]
        active.extend(plain(line) + '（来源待核对）' for line in groups['unresolved'])
        if active:
            heading = '有效硬规则' if name == project_name else f'继承硬规则（来源项目：{name}）'
            parts.append('## ' + heading + '\n' + '\n'.join(active))
        prefix = '' if name == project_name else f'（来源项目：{name}）'
        pending.extend(plain(line) + prefix for line in groups['pending'])
        inactive.extend(plain(line) + prefix for line in groups['inactive'])
        name = str(current_card.get('parent') or '')
    attachment_names = []
    identities = {project_name, str(card.get('project_id') or '')} - {''}
    for path in sorted((vault / ATTACHMENTS_DIR).glob('*/*.md')):
        attachment, _ = split_front_matter(path.read_text(encoding='utf-8-sig'))
        projects = attachment.get('projects') or []
        projects = [projects] if isinstance(projects, str) else projects
        if not projects or identities.intersection(projects):
            attachment_names.append('- ' + str(attachment.get('name') or path.stem))
    if attachment_names:
        parts.append('## 附件\n' + '\n'.join(attachment_names))
    configs = [Path(card['check_config'])] if card.get('check_config') else []
    if not configs:
        roots = card.get('paths') or []
        roots = [roots] if isinstance(roots, str) else roots
        for root in roots:
            configs.extend(path for path in find_configs(Path(root)) if path not in configs)
    commands = []
    for path in configs:
        config = yaml.safe_load(path.read_text(encoding='utf-8-sig')) or {}
        cwd = path.parent.parent if path.parent.name == '.tbrain' else path.parent
        for item in config.get('checks') or []:
            command = str(item.get('run') or item.get('command') or '').strip()
            if command:
                commands.append(f'- 在 `{cwd}` 运行：`{command}`')
    if commands:
        parts.append('## 验证命令\n' + '\n'.join(commands))
    if pending:
        parts.append('## 来源待确认规则\n以下原文保留，尚不作为有效硬规则。\n' + '\n'.join(pending))
    if inactive:
        parts.append('## 已失效规则（仅供参考）\n' + '\n'.join(inactive))
    return '\n\n'.join(parts).rstrip() + '\n'


def _parse_sections(model_body: str) -> dict[str, str]:
    sections = {section: "" for section in PROMPT_SECTIONS}
    current: str | None = None
    buffer: list[str] = []
    for line in str(model_body or "").splitlines():
        if line.startswith("## "):
            if current in sections:
                sections[current] = "\n".join(buffer).strip()
            heading = re.sub(r'\s*〔sha:[^〕]*〕\s*$', '', line[3:].strip())
            current = heading if heading in sections else None
            buffer = []
            continue
        if current is not None:
            buffer.append(line)
    if current in sections:
        sections[current] = "\n".join(buffer).strip()
    return sections


def _replace_sections(body: str, sections: dict[str, str]) -> str:
    remaining = dict(sections)

    def replace(match):
        heading = re.sub(r'\s*〔sha:[^〕]*〕\s*$', '', match[0].split('\n', 1)[0][3:].strip())
        if heading not in remaining:
            return match[0]
        return f'## {heading}\n{remaining.pop(heading)}'.rstrip() + '\n\n'

    body = re.sub(r'^## [^\n]*(?:\n|$).*?(?=^## |\Z)', replace, body, flags=re.M | re.S)
    for heading, value in remaining.items():
        body = body.rstrip() + f'\n\n## {heading}\n{value}'
    return body.strip('\n')


def _render_index_table(rows: Sequence[dict]) -> str:
    lines = [
        "| 名称 | 状态 | agent | 更新时间 | 未完成目标 | 下一步 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        name = str(row.get("name") or "")
        lines.append(
            "| [[{}]] | {} | {} | {} | {} | {} |".format(
                _escape_table_cell(name),
                _escape_table_cell(row.get("status")),
                _escape_table_cell(row.get("agent")),
                _escape_table_cell(row.get("updated_at") or row.get("updated")),
                _escape_table_cell(row.get("open_goals")),
                _escape_table_cell(row.get("next_step")),
            )
        )
    return "\n".join(lines)


def _path_exists(value: str) -> bool:
    try:
        return os.path.exists(os.path.expanduser(str(value or "").strip().strip("`")))
    except (OSError, ValueError):
        return False


def _one_line(text: str, limit: int = 60) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value[:limit]


def _path_key(value: str) -> str:
    text = os.path.expanduser(str(value or "").strip().replace("/", os.sep))
    if not text:
        return ""
    text = os.path.normcase(os.path.normpath(text))
    if len(text) > 3:
        text = text.rstrip(os.sep)
    return text


def _path_within(child: str, parent: str) -> bool:
    if not child or not parent:
        return False
    if child == parent:
        return True
    if parent.endswith(os.sep):
        return child.startswith(parent)
    return child.startswith(parent + os.sep)
