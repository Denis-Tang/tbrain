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
import os
import re
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

import yaml

from . import clock, naming
from .index import Index

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
        return self._project_payload(value, data, card)

    def _project_payload(self, name: str, data: dict, card: Path) -> dict:
        paths = data.get("paths") or []
        if isinstance(paths, str):
            paths = [paths]
        paths = [str(item) for item in paths if str(item).strip()]
        return {
            "name": name,
            "paths": paths,
            "parent": str(data.get("parent") or ""),
            "allow_child": bool(data.get("allow_child", False)),
            "created_at": str(data.get("created_at") or ""),
            "session_count": len(self._index_rows(self.project_index_path(name))),
            "path": str(card),
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
        rename_from = str(rename_from or "").strip()
        if rename_from and rename_from != target_name:
            self._rename_project(rename_from, target_name)
            _ = self.project(target_name)  # 重新读取改名后的卡片（若有）
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
        existing_data, existing_body = split_front_matter(self.read_text(card))
        created_at = str(existing_data.get("created_at") or "") or self._stamp()
        data = {
            "name": target_name,
            "paths": path_values,
            "parent": parent_name,
            "allow_child": bool(allow_child),
            "created_at": created_at,
        }
        self.write_text(card, render_note(data, existing_body))
        self._ensure_project_files(target_name)
        return self.project(target_name) or {
            "name": target_name,
            "paths": path_values,
            "parent": parent_name,
            "allow_child": bool(allow_child),
            "created_at": created_at,
            "session_count": 0,
            "path": str(card),
        }

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
            for session_file in sorted(new_sessions.rglob("*.md")):
                self._set_front_matter_field(session_file, "project", new)
                moved_sessions += 1
            rows = self._index_rows(self.project_index_path(new))
            self.write_project_index(new, rows)
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
            for path in project.get("paths") or []:
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

    def save_project_prompt(self, name: str, sections: dict, *, summary=None) -> dict:
        value = str(name or '').strip()
        if self.project(value) is None:
            raise ValueError(f'项目不存在：{value}')
        accepted, rejected, warnings = self._validate_prompt_sections(value, sections, summary)
        path = self.project_prompt_path(value)
        original = self.read_text(path)
        front, _ = split_front_matter(original)
        blocks = parse_blocks(original)
        current = self.project_prompt(value)
        old_sections = current['sections']
        fingerprints = front.get('model_fingerprints') or {}
        protected = set(front.get('user_owned_sections') or [])
        model = blocks.get('model', {})
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
        merged = {**old_sections, **accepted}
        front['user_owned_sections'] = sorted(protected)
        front['model_fingerprints'] = {section: fingerprints.get(section, block_fingerprint(old_sections[section]))
                                      if section in protected else block_fingerprint(content)
                                      for section, content in merged.items()}
        blocks['model'] = {'owner': 'model', 'body': _render_sections(merged)}
        if accepted or protected:
            self.write_text(path, render_note(front, render_blocks(blocks)))
            self.index.set_block_state(self._rel(path), 'model', 'user' if protected else 'model',
                                       block_fingerprint(blocks['model']['body']))
        changed = [section for section in accepted if accepted[section] != old_sections[section]]
        if changed:
            self.append_changelog(self.project_changelog_path(value), '替换项目提示词：' + '、'.join(changed),
                                  '\n\n'.join(f'## {section}\n{old_sections[section]}' for section in changed))
        return {'written': accepted, 'rejected': rejected, 'warnings': warnings, 'pending_confirm': pending}

    def _validate_prompt_sections(self, project_name: str, sections: dict, summary=None):
        from . import prompt
        result = prompt.validate(sections, resolve_source=self._prompt_resolver(project_name, summary),
                                 path_exists=_path_exists)
        return result['written'], result['rejected'], result['warnings']

    def _prompt_resolver(self, project_name: str, summary=None):
        from . import handoff
        def resolve(session_name: str, reference: str) -> bool:
            if not session_name or (summary is not None and session_name == summary.name):
                return summary is not None and summary.has_reference(reference)
            matches = [row for row in self.index.sessions() if row['name'] == session_name]
            if len(matches) != 1:
                return False
            return handoff.parse(self.read_text(self.vault / matches[0]['path'])).has_reference(reference)
        return resolve

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

    def _public_kind_dir(self, kind: str) -> Path:
        return self.vault / PUBLIC_DIR / str(kind)

    def _public_card_path(self, kind: str, name: str) -> Path:
        return self._public_kind_dir(kind) / (naming.sanitize_filename(name) + ".md")

    def public_cards(self, kind: str | None = None) -> list[dict]:
        base = self.vault / PUBLIC_DIR
        if not base.exists():
            return []
        kinds = [str(kind)] if kind else sorted(
            [child.name for child in base.iterdir() if child.is_dir() and child.name != ARCHIVE_DIR]
        )
        cards: list[dict] = []
        for kind_name in kinds:
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

    def save_public_card(
        self,
        kind: str,
        name: str,
        meta: dict | None = None,
        source_session: str = "",
    ) -> dict:
        """同对象（kind + name）新卡生效；旧卡标 ``已被取代`` 移入 ``归档/``。"""
        kind_value = str(kind or "").strip()
        if kind_value not in PUBLIC_KINDS:
            raise ValueError("公共池类型必须是：" + "、".join(PUBLIC_KINDS))
        name_value = str(name or "").strip()
        if not name_value:
            raise ValueError("公共池卡片名称不能为空")
        meta = dict(meta or {})
        stamp = self._stamp()
        replaced: dict | None = None
        archived_path = ""
        existing = [
            card
            for card in self.public_cards(kind_value)
            if str(card.get("name") or "").strip().casefold() == name_value.casefold()
            or str(card.get("id") or "") == f"{kind_value}-{name_value}"
        ]
        if existing:
            old = existing[0]
            old_path = Path(str(old["path"]))
            old_data, old_body = split_front_matter(self.read_text(old_path))
            fingerprint = old_data.pop("model_fingerprint", "")
            if old_data.get("owner") == "user" or fingerprint != block_fingerprint(render_note(old_data, old_body)):
                message = self.record_conflict("", f"公共池 {kind_value}/{name_value} 已被用户修改或缺少原写入指纹，待用户确认",
                                               render_note(meta, str(meta.get("body") or "")))
                raise ValueError(message)
            old_data["state"] = "已被取代"
            old_data["updated_at"] = stamp
            history = old_data.get("history") or []
            if not isinstance(history, list):
                history = []
            history.append(
                {
                    "at": stamp,
                    "action": "已被取代",
                    "note": f"新卡 {kind_value}-{name_value} 生效",
                }
            )
            old_data["history"] = history
            archive_dir = self._public_kind_dir(kind_value) / ARCHIVE_DIR
            archive_dir.mkdir(parents=True, exist_ok=True)
            archive_path = archive_dir / old_path.name
            suffix = 2
            while archive_path.exists():
                archive_path = archive_dir / f"{old_path.stem}-{suffix}{old_path.suffix}"
                suffix += 1
            os.replace(str(old_path), str(archive_path))
            self.write_text(archive_path, render_note(old_data, old_body))
            replaced = {"id": str(old.get("id") or ""), "path": str(old_path)}
            archived_path = str(archive_path)
            self.index.upsert_public(
                {
                    "id": str(old.get("id") or f"{kind_value}-{name_value}"),
                    "kind": kind_value,
                    "name": str(old.get("name") or name_value),
                    "state": "已被取代",
                    "path": str(archive_path),
                    "updated_at": stamp,
                },
                old_body,
            )
        card_path = self._public_card_path(kind_value, name_value)
        if card_path.exists():
            suffix = 2
            while card_path.exists():
                card_path = self._public_kind_dir(kind_value) / (
                    f"{naming.sanitize_filename(name_value)}-{suffix}.md"
                )
                suffix += 1
        created_at = str(meta.get("created_at") or "") or str((existing[0] if existing else {}).get("created_at") or "") or stamp
        data: dict[str, Any] = {
            "id": f"{kind_value}-{name_value}",
            "kind": kind_value,
            "name": name_value,
            "version": str(meta.get("version") or ""),
            "install": str(meta.get("install") or ""),
            "package_id": str(meta.get("package_id") or ""),
            "location": str(meta.get("location") or ""),
            "scope": str(meta.get("scope") or ""),
            "uninstall": str(meta.get("uninstall") or ""),
            "verify": str(meta.get("verify") or ""),
            "source_session": str(meta.get("source_session") or source_session or ""),
            "created_at": created_at,
            "updated_at": stamp,
            "state": "生效",
            "history": [
                {
                    "at": stamp,
                    "action": "创建" if not existing else "取代旧卡",
                    "note": str(meta.get("note") or ""),
                }
            ],
        }
        for key, value in meta.items():
            if key in ("body", "note") or key in data or key in PUBLIC_YAML_FIELDS:
                continue
            data[key] = value
        body = str(meta.get("body") or "")
        data["owner"] = "model"
        data.pop("model_fingerprint", None)
        data["model_fingerprint"] = block_fingerprint(render_note(data, body))
        self.write_text(card_path, render_note(data, body))
        self.index.upsert_public(
            {
                "id": data["id"],
                "kind": kind_value,
                "name": name_value,
                "state": "生效",
                "path": str(card_path),
                "updated_at": stamp,
            },
            body,
        )
        return {
            "id": str(data["id"]),
            "path": str(card_path),
            "state": "生效",
            "replaced": replaced,
            "archived_path": archived_path,
        }

    def public_l1(self, max_lines: int = 40) -> str:
        """公共池 L1 索引（程序生成，≤40 行）：每类一行 + 热点事实。"""
        cards = self.public_cards()
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
        projects = self.projects()
        if projects:
            lines.append("")
            lines.append("## 工作区路径")
            for project in projects:
                paths = project.get("paths") or []
                if paths:
                    lines.append(f"- {project['name']}：{paths[0]}")
        if len(lines) > max_lines:
            lines = lines[: max(0, max_lines - 1)] + ["（更多内容请调用 search 查询）"]
        return "\n".join(lines)

    # -- 附件（只读） -------------------------------------------------------

    def attachments(self, project_name: str = "") -> list[dict]:
        base = self.vault / ATTACHMENTS_DIR
        if not base.exists():
            return []
        project = str(project_name or "").strip()
        cards: list[dict] = []
        for directory in sorted(child for child in base.iterdir() if child.is_dir()):
            for card_path in sorted(directory.glob("*.md")):
                data, body = split_front_matter(self.read_text(card_path))
                card_type = str(data.get("type") or directory.name)
                projects = data.get("projects") or []
                if isinstance(projects, str):
                    projects = [projects]
                projects = [str(item) for item in projects]
                if project and projects and project not in projects:
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
        project = str(split_front_matter(original_text)[0].get("project") or "")
        os.replace(str(old), str(new_path))
        old_rel = self._rel(old)
        new_rel = self._rel(new_path)
        self.index.move_block_path(old_rel, new_rel)
        # 只更新文件头的 name 字段，正文内容不动
        self._set_front_matter_field(new_path, "name", new_stem)
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


def _block_body(text: str, block: str) -> str:
    blocks = parse_blocks(text)
    return str((blocks.get(block) or {}).get("body") or "")


def _parse_sections(model_body: str) -> dict[str, str]:
    sections = {section: "" for section in PROMPT_SECTIONS}
    current: str | None = None
    buffer: list[str] = []
    for line in str(model_body or "").splitlines():
        if line.startswith("## "):
            if current in sections:
                sections[current] = "\n".join(buffer).strip()
            heading = line[3:].strip()
            current = heading if heading in sections else None
            buffer = []
            continue
        if current is not None:
            buffer.append(line)
    if current in sections:
        sections[current] = "\n".join(buffer).strip()
    return sections


def _render_sections(sections: dict[str, str]) -> str:
    parts: list[str] = []
    for section in PROMPT_SECTIONS:
        value = str(sections.get(section) or "").strip()
        parts.append(f"## {section}\n{value}".rstrip())
    return "\n\n".join(parts)


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
