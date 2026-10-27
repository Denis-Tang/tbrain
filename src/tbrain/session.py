"""最小闭环的流程层：立项、启动包、hook 语义、save/search/resume。

本模块只依赖 service 暴露的存储/索引/时钟能力（见 docs/ARCHITECTURE.md），
不直接操作文件与数据库，便于单独测试与替换实现。
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
import json
import re
import sys
import platform
from importlib import metadata
from pathlib import Path
from zoneinfo import ZoneInfo

from . import handoff, identity, naming, prompt as prompt_mod
from .hooks import HookEvent, HookResult

HOST_BUDGETS = {"claude-code": 9000, "codex": 8000, "dsh": 12000, "pi": 12000, "unknown": 8000}
STATUS_PRIORITY = {"已中断": 0, "进行中": 1, "受阻": 2, "待验证": 3, "已完成": 4, "已放弃": 5}


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _utc_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _display(service, iso_text: str) -> str:
    """按用户时区显示 ISO 时间（失败时原样返回）。"""
    try:
        moment = datetime.strptime(str(iso_text), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        zone = ZoneInfo(service.settings.load().timezone)
        return moment.astimezone(zone).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(iso_text or "")


def _days_since(iso_text: str) -> float:
    try:
        moment = datetime.strptime(str(iso_text), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - moment).total_seconds() / 86400.0)
    except Exception:
        return 9999.0


def _clip(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _same_text(left: str, right: str) -> bool:
    import difflib

    def norm(value: str) -> str:
        return re.sub(r"\s+", "", str(value or "")).strip("。；，,.;")

    a, b = norm(left), norm(right)
    if not a or not b:
        return False
    if a == b:
        return True
    if min(len(a), len(b)) < 30:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.92


# ---------------------------------------------------------------------------
# 会话定位 / 创建
# ---------------------------------------------------------------------------

def _session_row(service, session_id: str) -> dict | None:
    service.refresh_index()
    row = service.index.session(session_id)
    if not row:
        parts = identity.split_session_id(session_id)
        link = service.index.get_session_link(*parts) if parts else None
        if link:
            row = service.index.session(link["session_id"])
    if not row:
        return None
    path = service.vault_root() / row["path"]
    if not path.is_file():
        return None
    return {**row, "abs_path": path}


def load_session(service, session_id_or_row) -> tuple[handoff.Summary, Path] | None:
    row = session_id_or_row if isinstance(session_id_or_row, dict) else _session_row(service, str(session_id_or_row))
    if not row:
        return None
    text = service.store.read_text(row["abs_path"])
    summary = handoff.parse(text)
    _refresh_dead_ends(service, summary)
    owners = summary.header.setdefault("user_owned_fields", [])
    for field, digest in summary.header.get("model_fingerprints", {}).items():
        if field in ("target", "next_step") and digest != handoff.fingerprint(getattr(summary, field)):
            if field not in owners:
                owners.append(field)
    return summary, row["abs_path"]


def write_session(service, summary: handoff.Summary, path: Path, *, create: bool = False) -> None:
    _refresh_dead_ends(service, summary)
    for field in ("target", "next_step"):
        if field not in summary.header.get("user_owned_fields", []):
            summary.header.setdefault("model_fingerprints", {})[field] = handoff.fingerprint(getattr(summary, field))
    snapshot = handoff.parse(handoff.render(summary))
    for key in ("updated_at", "last_activity_at", "revision"):
        snapshot.header.pop(key, None)
    revision = f"U{len(summary.users)}-{handoff.fingerprint(handoff.render(snapshot))[:16]}"
    summary.header["revision"] = revision
    body = handoff.render(summary)
    service.store.write_text(path, body, create=create)
    service.remember_write(path, body)
    service.index.upsert_session(_row_from_summary(service, summary, path), body)
    service.sync_session_index(summary.project)
    folder = service.vault_root() / ".tbrain" / "会话版本" / handoff.fingerprint(summary.session_id)
    try:
        service.store.write_text(folder / (revision + ".md"), body, create=True)
    except FileExistsError:
        pass  # 心跳没有改变交接内容时复用同一个版本。


def _row_from_summary(service, summary: handoff.Summary, path: Path) -> dict:
    try:
        relative = path.resolve().relative_to(service.vault_root().resolve()).as_posix()
    except ValueError:
        relative = path.as_posix()
    goals = summary.goals()
    return {
        "session_id": summary.session_id,
        "project": summary.project,
        "name": summary.name,
        "path": relative,
        "status": summary.status,
        "agent": str(summary.header.get("agent") or ""),
        "cwd": str(summary.header.get("cwd") or ""),
        "started_at": str(summary.header.get("started_at") or ""),
        "updated_at": str(summary.header.get("updated_at") or ""),
        "last_activity_at": str(summary.header.get("last_activity_at") or ""),
        "next_step": summary.next_step,
        "open_goals": sum(1 for item in goals if not item.get("done")),
        "git_branch": (summary.header.get("git") or {}).get("branch", ""),
        "git_head": (summary.header.get("git") or {}).get("head", ""),
        "git_dirty": int((summary.header.get("git") or {}).get("dirty") or 0),
        "last_verify": json.dumps(summary.header.get("last_verify") or {}, ensure_ascii=False),
        "file_hash": handoff.fingerprint(handoff.render(summary)),
        "file_mtime": path.stat().st_mtime if path.exists() else 0.0,
    }


def next_sequence(service, project: str, date: str) -> int:
    used = 0
    for row in service.index.sessions(project):
        name = str(row.get("name") or "")
        match = re.search(rf"{re.escape(date)}-(\d+)$", name)
        if match:
            used = max(used, int(match.group(1)))
    return used + 1


def _history_names(service, project: str) -> list[str]:
    return [str(row.get("name") or "") for row in service.index.sessions(project)]


def _project_dir(service, project: str) -> Path:
    if not project:
        return service.vault_root() / "独立会话池"
    return service.vault_root() / "项目" / project


def create_summary(service, *, host: str, host_session_id: str, cwd: str,
                   host_record: str = "", first_message: str = "",
                   project_override: str | None = None) -> tuple[handoff.Summary, Path]:
    now = service.now()
    date = now.local.strftime("%Y-%m-%d")
    project = project_override if project_override is not None else service.store.match_project(cwd)
    history = _history_names(service, project)
    sequence = next_sequence(service, project, date)
    provisional = naming.is_low_information(first_message) or not first_message.strip()
    service.store.ensure_layout()
    while True:
        name = (naming.temp_name(date, sequence) if provisional else
                naming.session_name(first_message, project, history, date, sequence))
        summary = handoff.new_summary(
            name=name, project=project, session_id=identity.build_session_id(host, host_session_id),
            agent=host, cwd=cwd, host_record=host_record, started_at=_utc_iso(now.utc),
            raw_command=first_message, provisional=provisional,
        )
        path = service.store.session_path(project or "独立", now.local, name)
        try:
            write_session(service, summary, path, create=True)
            break
        except FileExistsError:
            sequence += 1
    service.index.register_link(host, host_session_id, summary.session_id, cwd,
                                identity.first_message_hash(first_message), summary.header["started_at"])
    service.index.log_access(summary.session_id, "bootstrap")
    return summary, path


def resolve_session(service, host: str, host_session_id: str, cwd: str,
                    first_message: str = "", host_record: str = "") -> tuple[handoff.Summary, Path, bool]:
    """按宿主 ID 或明确登记的映射复用，不用开场文字猜测会话身份。"""
    session_id = identity.build_session_id(host, host_session_id)
    row = _session_row(service, session_id)
    if row:
        summary, path = load_session(service, row)
        _mark_active(service, summary, path)
        return summary, path, False
    summary, path = create_summary(service, host=host, host_session_id=host_session_id, cwd=cwd,
                                   first_message=first_message, host_record=host_record)
    return summary, path, True


def _mark_active(service, summary: handoff.Summary, path: Path) -> None:
    now = _utc_iso(service.now().utc)
    changed = False
    if summary.status == "已中断":
        summary.header["status"] = "进行中"
        changed = True
    summary.header["last_activity_at"] = now
    if changed:
        write_session(service, summary, path)


def rename_session(service, summary: handoff.Summary, path: Path, new_name: str) -> Path:
    old_name = summary.name
    if not new_name or new_name == old_name:
        return path
    new_path = path.with_name(naming.sanitize_filename(new_name) + ".md")
    if new_path.exists():
        new_path = path.with_name(naming.sanitize_filename(new_name) + f" {_utc_iso(service.now().utc)[:10]}.md")
    new_path = service.store.rename_session(path, new_path.name)
    summary.header["name"] = new_path.stem
    summary.header["provisional_name"] = False
    try:
        relative = new_path.resolve().relative_to(service.vault_root().resolve()).as_posix()
    except ValueError:
        relative = new_path.as_posix()
    service.index.remove_session(summary.session_id)
    service.index.upsert_session({**_row_from_summary(service, summary, new_path), "path": relative},
                                 handoff.render(summary))
    return new_path


# ---------------------------------------------------------------------------
# 启动包
# ---------------------------------------------------------------------------

def _global_prompt_block(service) -> str:
    text = service.store.global_prompt()
    if not text:
        return ""
    if prompt_mod.USER_START in text and prompt_mod.USER_END in text:
        start = text.find(prompt_mod.USER_START) + len(prompt_mod.USER_START)
        end = text.find(prompt_mod.USER_END, start)
        return text[start:end].strip("\n")
    return text.strip()


def _project_prompt_block(service, project: str) -> str:
    if not project:
        return ""
    data = service.store.project_prompt(project)
    parents = []
    ancestors = {project}
    parent = (service.store.project(project) or {}).get("parent")
    while parent:
        if parent in ancestors:
            raise ValueError('项目卡父级关系形成循环，请修正项目卡')
        ancestors.add(parent)
        rules = service.store.project_prompt(parent)["sections"].get("硬规则", "")
        if rules:
            parents.insert(0, f"## 继承 {parent} 的硬规则\n{rules}")
        parent = (service.store.project(parent) or {}).get("parent")
    return "\n\n".join([prompt_mod.PROJECT_PROMPT_SPEC] + parents +
                       [prompt_mod.render(data.get("sections") or {}, data.get("user") or "").strip()])


def _attachment_block(service, project: str) -> str:
    cards = service.store.attachments(project)
    if not cards:
        return ""
    lines = ["## 附件卡片（需要时按路径直接读取，不必再检索）"]
    for card in cards:
        note = f"；备注：{card.get('note')}" if card.get("note") else ""
        lines.append(f"- {card.get('name')}（{card.get('type')}）：{card.get('path')}{note}")
    return "\n".join(lines)


def _resume_rows(service, summary: handoff.Summary, related_query: str, limit: int = 5) -> list[dict]:
    project = summary.project
    child_projects = service.child_projects(project)
    rows = service.index.resume_view(project, child_projects, limit=50, related_query=related_query,
                                     cwd=str(summary.header.get("cwd") or "") if not project else "")
    rows = [row for row in rows if row['session_id'] != summary.session_id]
    if not rows:
        rows = [row for row in service.index.resume_view(project, child_projects, limit=50,
                cwd=str(summary.header.get('cwd') or '') if not project else '') if row['session_id'] != summary.session_id]
    service.index.set_meta('resume_view:' + summary.session_id, json.dumps({
        'at': _utc_iso(service.now().utc), 'cwd': summary.header.get('cwd', '') if not project else '',
        'rows': [{key: row[key] for key in ('session_id', 'status', 'updated_at', 'cwd', 'name')} for row in rows]}, ensure_ascii=False))
    for row in rows:
        row["updated_display"] = _display(service, row.get("updated_at", ""))
    return rows[:limit]


def _resume_block(service, rows: list[dict], root_project: str) -> str:
    if not rows:
        return ""
    lines = ["## 接续视图（与原命令/状态/新近排序，前 5 条）"]
    for row in rows:
        source = ""
        if root_project and row.get("project") and row.get("project") != root_project:
            source = f"（子项目 {row.get('project')}）"
        lines.append(f"- {row.get('name')} / {row.get('status')} / {row.get('agent')} / "
                     f"{row.get('updated_display')} / 下一步：{row.get('next_step') or '（未写）'}{source}")
    return "\n".join(lines)


def _public_l1_block(service) -> str:
    text = service.store.public_l1()
    return f"## 公共池索引\n{text}" if text else ""


def _time_block(service) -> str:
    now = service.now()
    offset = now.offset_seconds
    calibrate = "已校准" if now.calibrated else "未校准"
    sign = "+" if offset >= 0 else ""
    return f"## 当前时间\n{now.utc.strftime('%Y-%m-%d %H:%M:%S')} UTC（{now.local.strftime('%Y-%m-%d %H:%M')}，{now.timezone}，{calibrate}，偏差 {sign}{offset:.0f}s）"


def _identity_block(service, summary: handoff.Summary) -> str:
    return (f"## 当前会话\nsession_id: {summary.session_id}\n"
            f"之后调用 search/save/resume 时原样传入该 session_id。")


def assemble_startup(service, summary: handoff.Summary, host: str, *,
                     resume: bool = False, related_query: str = "") -> str:
    blocks: list[tuple[str, str]] = []
    blocks.append(("", _global_prompt_block(service)))
    blocks.append(("", _project_prompt_block(service, summary.project)))
    blocks.append(("", _public_l1_block(service)))
    blocks.append(("", _attachment_block(service, summary.project)))
    rows = _resume_rows(service, summary, related_query)
    blocks.append(("", _resume_block(service, rows, summary.project)))
    recent = _recent_users_block(summary) if resume else ""
    if recent:
        recent = recent[-HOST_BUDGETS.get(host, 8000) // 3:]
    progress = ''
    if resume:
        progress = '\n'.join(['## 本会话已保存进度', '原始命令：' + _clip(summary.raw_command, 1000),
                              '目标：' + summary.target, '下一步：' + summary.next_step] +
                             [item['id'] + ' ' + item['text'] for item in summary.items('已完成')[-5:]])
        progress = _clip(progress, HOST_BUDGETS.get(host, 8000) // 4)
    pending = service.store.pending_confirmations(summary.project)
    footer = "\n\n".join(part for part in (progress, recent, _clip(pending, 1500), _time_block(service),
                                           _identity_block(service, summary)) if part)
    budget = HOST_BUDGETS.get(host, HOST_BUDGETS["unknown"]) - len(footer) - 80
    parts: list[str] = []
    used = 0
    trimmed = False
    for _, text in blocks:
        if not text:
            continue
        remaining = budget - used
        if remaining <= 0:
            trimmed = True
            break
        if len(text) > remaining:
            parts.append(text[: max(0, remaining - 40)].rstrip())
            trimmed = True
            used = budget
            break
        parts.append(text)
        used += len(text) + 2
    package = "\n\n".join(parts)
    if trimmed:
        package += "\n\n（启动包已按宿主上限裁剪；更多内容可调用 bootstrap / search 获取。）"
    return package + "\n\n" + footer


# ---------------------------------------------------------------------------
# Hook 语义
# ---------------------------------------------------------------------------

def _version_warning(service) -> str:
    try:
        return service.version_warning()
    except Exception:
        return ""


def _recent_users_block(summary, title: str = "最近 5 条用户指令（防止压缩丢失约束）") -> str:
    """Last five user instructions, replayed around compaction (deterministic)."""
    lines = [f"## {title}"]
    for item in summary.users[-5:]:
        lines.append(f"### {item['id']}\n{item.get('text', '')}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _stop_reminder(service, summary: handoff.Summary, path: Path, host: str) -> HookResult:
    summary.header["last_activity_at"] = _utc_iso(service.now().utc)
    write_session(service, summary, path)
    message = ("有尚未总结的用户指令或新测试结果。请调用 save 提交本轮增量；无需凑完成项。"
               + (prompt_mod.MAINTENANCE_GUIDANCE if summary.project else "") +
               "先写证据再引用返回编号；有 rejected 时只补交失败部分，待整理不代表通过校验。")
    if host == "pi":
        return HookResult(additional_context=message)
    return HookResult(decision="block",
                      reason=message)


def _needs_save(summary: handoff.Summary) -> bool:
    verification = summary.header.get("last_verify") or {}
    return (len(summary.users) > int(summary.header.get("saved_users") or 0) or
            (verification.get("source") == "tool" and
             verification.get("at") != summary.header.get("saved_verify_at")))


def handle_hook(service, event: HookEvent) -> HookResult:
    service.refresh_index()
    timeout_fallback(service)
    warning = _version_warning(service)
    host = event.host or "unknown"

    if event.kind == "tool-result":
        summary, path = _load_or_none(service, host, event.host_session_id)
        if summary is not None and event.extra.get("last_test"):
            result = event.extra["last_test"]
            if not isinstance(result, dict) or not isinstance(result.get('command'), str) or not isinstance(result.get('exit_code'), int):
                return HookResult(additional_context='工具结果缺少 command 或整数 exit_code，未记录验证结果。')
            summary.header["last_verify"] = {"command": str(result["command"]),
                                              "exit_code": result["exit_code"],
                                              "at": _utc_iso(service.now().utc), "source": "tool"}
            write_session(service, summary, path)
        return HookResult()

    if event.kind == "session-end":
        summary, path = _load_or_none(service, host, event.host_session_id)
        if summary is not None:
            if summary.status in ("进行中", "受阻", "待验证"):
                summary.header["status"] = "已中断"
            summary.header["last_activity_at"] = _utc_iso(service.now().utc)
            write_session(service, summary, path)
        return HookResult()

    if event.kind == "stop":
        if event.stop_hook_active:
            return HookResult(decision="", reason="")
        summary, path = _load_or_none(service, host, event.host_session_id)
        if summary is None:
            return HookResult()
        if _needs_save(summary):
            return _stop_reminder(service, summary, path, host)
        summary.header["last_activity_at"] = _utc_iso(service.now().utc)
        write_session(service, summary, path)
        return HookResult()

    if event.kind == "pre-compact":
        summary, path = _load_or_none(service, host, event.host_session_id)
        if summary is None:
            return HookResult()
        lines = [_recent_users_block(summary, "压缩前重新注入的用户指令（防止压缩丢失约束）"),
                 "请在本轮结束前调用 save 保存最新进展。"]
        summary.header["last_activity_at"] = _utc_iso(service.now().utc)
        write_session(service, summary, path)
        return HookResult(additional_context=(warning + "\n\n" if warning else "") + "\n".join(lines))

    if event.kind == "context":
        summary, path = _load_or_none(service, host, event.host_session_id)
        if summary is None:
            return HookResult()
        hint = context_switch_hint(service, summary, event.context_tokens, event.context_window)
        summary.header["last_activity_at"] = _utc_iso(service.now().utc)
        write_session(service, summary, path)
        return HookResult(additional_context=hint)

    if event.kind == "session-start":
        source = (event.source or "startup").lower()
        if source == "fork":
            # 分支必须有自己的身份，不能沿用旧版本猜出的别名。
            if not service.index.session(identity.build_session_id(host, event.host_session_id)):
                create_summary(service, host=host, host_session_id=event.host_session_id, cwd=event.cwd,
                               host_record=event.extra.get("host_record", ""))
            parent = event.extra.get("parent_session_id", "")
            if parent and parent != identity.build_session_id(host, event.host_session_id):
                current = bootstrap(service, identity.build_session_id(host, event.host_session_id), host,
                                    event.cwd, event.extra.get("host_record", ""))
                result = resume(service, current["session_id"], parent,
                                fork_user_count=event.extra.get("fork_user_count"),
                                fork_revision=event.extra.get("fork_revision", ""), fork=True)
                return HookResult(additional_context=(result.get("handoff") or result.get("error", "")) +
                                   f"\n\n当前会话 session_id: {current['session_id']}")
            return HookResult()
        if source == "startup":
            return HookResult()
        existing = _load_or_none(service, host, event.host_session_id)
        parent = event.extra.get("parent_session_id", "")
        if existing[0] is None and source in ("resume", "compact") and parent:
            row = _session_row(service, parent)
            if row and row["agent"] == host:
                summary, path = load_session(service, row)
                service.index.register_link(host, event.host_session_id, summary.session_id, event.cwd,
                                            identity.first_message_hash(summary.raw_command))
                existing = summary, path
        if existing[0] is None:
            # 未知会话（例如首次被 Tbrain 接管的历史会话）：等第一条用户消息再立项。
            return HookResult()
        summary, path = existing
        _mark_active(service, summary, path)
        related = " ".join([summary.raw_command] + handoff.last_users(summary, 3)).strip()
        package = assemble_startup(service, summary, host, resume=True, related_query=related)
        summary.header["last_activity_at"] = _utc_iso(service.now().utc)
        write_session(service, summary, path)
        return HookResult(additional_context=(warning + "\n\n" if warning else "") + package)

    if event.kind == "user-prompt":
        first_message = event.prompt or ""
        existing = _load_or_none(service, host, event.host_session_id)
        if existing[0] is None:
            summary, path, created = resolve_session(service, host, event.host_session_id, event.cwd,
                                                     first_message=first_message,
                                                     host_record=event.extra.get("host_record", ""))
            package = assemble_startup(service, summary, host, related_query=first_message)
            if warning:
                package = warning + "\n\n" + package
            service.index.log_access(summary.session_id, "bootstrap")
            return HookResult(additional_context=package)
        summary, path = existing
        if event.extra.get("host_record"):
            summary.header["host_record"] = event.extra["host_record"]
        if not summary.raw_command.strip():
            # 无 hook 宿主先用 bootstrap 建了空总结：第一条消息补成原始命令与 U1。
            summary.raw_command = first_message
            summary.sections["用户指令"] = [{"number": 1, "text": handoff._clip_user(first_message)}]
            summary.header["last_activity_at"] = _utc_iso(service.now().utc)
            summary.header["updated_at"] = summary.header["last_activity_at"]
            if summary.header.get("provisional_name"):
                date = service.now().local.strftime("%Y-%m-%d")
                sequence = next_sequence(service, summary.project, date)
                name = naming.session_name(first_message, summary.project,
                                          _history_names(service, summary.project), date, sequence)
                path = rename_session(service, summary, path, name)
            write_session(service, summary, path)
            package = assemble_startup(service, summary, host, related_query=first_message)
            if warning:
                package = warning + "\n\n" + package
            return HookResult(additional_context=package)
        record_user(service, summary, path, first_message)
        return HookResult()

    return HookResult()


def _load_or_none(service, host: str, host_session_id: str):
    session_id = identity.build_session_id(host, host_session_id)
    row = _session_row(service, session_id)
    if not row:
        return None, None
    summary, path = load_session(service, row)
    return summary, path


def record_user(service, summary: handoff.Summary, path: Path, text: str) -> None:
    if not text or not text.strip():
        return
    now = _utc_iso(service.now().utc)
    users = summary.sections.setdefault("用户指令", [])
    handoff.add_user(summary, text)
    summary.header["last_activity_at"] = now
    summary.header["updated_at"] = now
    provisional = bool(summary.header.get("provisional_name"))
    if provisional and len(users) >= 2:
        date = service.now().local.strftime("%Y-%m-%d")
        seq = next_sequence(service, summary.project, date)
        new_name = naming.session_name(text, summary.project, _history_names(service, summary.project), date, seq)
        path = rename_session(service, summary, path, new_name)
    write_session(service, summary, path)


def timeout_fallback(service) -> int:
    hours = float(service.settings.load().timeout_hours or 0)
    if hours <= 0:
        return 0
    changed = 0
    threshold = service.now().utc - timedelta(hours=hours)
    for row in service.index.sessions(status=["进行中"]):
        moment = _parse_utc(row.get("last_activity_at"))
        if moment is None or moment >= threshold:
            continue
        path = service.vault_root() / row["path"]
        if not path.is_file():
            continue
        summary = handoff.parse(service.store.read_text(path))
        summary.header["status"] = "已中断"
        write_session(service, summary, path)
        changed += 1
    return changed


def _parse_utc(value) -> datetime | None:
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def context_switch_hint(service, summary: handoff.Summary, tokens, window) -> str:
    if _needs_save(summary):
        return ""
    settings = service.settings.load()
    if not getattr(settings, "reminder_context_switch", True):
        return ""
    if not tokens or not window:
        return ""
    ratio = float(tokens) / float(window)
    if ratio < 0.65:
        return ""
    return (f"记录已保存。当前会话上下文已用 {ratio * 100:.0f}%（约 {int(tokens)} tokens）。"
            "建议新开会话接续，以减少后续携带的上下文。费用节省未估算。")


# ---------------------------------------------------------------------------
# save
# ---------------------------------------------------------------------------

SECTION_ALIASES = {
    "done": "已完成", "completed": "已完成",
    "decision": "决策", "decisions": "决策",
    "dead_end": "未走通的路", "dead_end_path": "未走通的路", "x": "未走通的路",
    "evidence": "证据", "e": "证据",
    "goal": "未完成目标", "goal_open": "未完成目标",
}


def save(service, session_id: str, operations: list[dict], last_test: dict | None = None) -> dict:
    row = _session_row(service, session_id)
    if not row:
        return {"ok": False, "error": "会话不存在：请先让 hook 完成立项，或调用 bootstrap(session_id=...)",
                "current_time": _time_block(service)}
    summary, path = load_session(service, row)
    raw_text = service.store.read_text(path)
    program_hash = service.program_written_hash(path)
    user_edited = bool(program_hash) and handoff.fingerprint(raw_text) != program_hash
    if user_edited and not summary.header.get("model_fingerprints"):
        summary.header["user_owned_fields"] = ["target", "next_step"]
    protected = summary.header.get("user_owned_fields", [])
    written: dict = {"users": [], "done": [], "decisions": [], "dead_ends": [], "evidence": [],
                     "goals": [], "target": "", "next_step": "", "status": "", "env": [], "project_prompt": {}}
    rejected: list[dict] = []
    warnings: list[str] = []

    if not isinstance(operations, list) or not operations:
        return {"ok": False, "error": "operations 必须是非空数组", "current_time": _time_block(service)}

    for operation in operations:
        reject_start = len(rejected)
        if not isinstance(operation, dict):
            rejected.append({"op": str(operation), "operation": operation, "reason": "操作必须是对象", "example": '{"op":"append","section":"done","items":[{"text":"..."}]}'})
            continue
        op = str(operation.get("op") or "")
        try:
            if op == "append":
                section_key = str(operation.get("section") or "").strip()
                section = SECTION_ALIASES.get(section_key)
                if not section:
                    raise ValueError("section 只允许 done/decision/dead_end/evidence/goal")
                items = operation.get("items")
                if not isinstance(items, list) or not items:
                    raise ValueError("items 必须是非空数组")
                for item in items:
                    item_start = len(rejected)
                    appended = _append_item(service, summary, section, item, rejected, warnings)
                    for error in rejected[item_start:]:
                        error["operation"] = {**operation, "items": [item]}
                    if appended:
                        written_key = {"已完成": "done", "决策": "decisions", "未走通的路": "dead_ends",
                                       "证据": "evidence", "未完成目标": "goals"}[section]
                        written[written_key].append(appended)
            elif op == "replace":
                section = str(operation.get("section") or "")
                text = str(operation.get("text") or "").strip()
                if section in protected:
                    summary.conflicts.append(
                        f"- 用户手改过本会话总结，{section} 的模型更新未覆盖（{_utc_iso(service.now().utc)}），待用户确认")
                    rejected.append({"op": "replace", "section": section,
                                     "reason": "该会话总结已被用户手改，程序不覆盖；内容保留在“待用户确认”",
                                     "example": "请手动合并；确认恢复模型维护后，移除文件头 user_owned_fields 中的对应字段"})
                    continue
                if section == "target":
                    if len(text) > handoff.TARGET_MAX_CHARS:
                        raise ValueError(f"目标不超过 {handoff.TARGET_MAX_CHARS} 字")
                    summary.target = text
                    written["target"] = text
                elif section == "next_step":
                    if not text:
                        raise ValueError("下一步不能为空（只能 1 条，写成“动作 + 怎么验证”）")
                    if len(text) > handoff.NEXT_MAX_CHARS:
                        raise ValueError(f"下一步不超过 {handoff.NEXT_MAX_CHARS} 字")
                    summary.next_step = text
                    written["next_step"] = text
                else:
                    raise ValueError("replace.section 只允许 target / next_step")
            elif op == "set_status":
                status = str(operation.get("status") or "")
                if status not in handoff.STATUSES:
                    raise ValueError("status 只允许：" + " / ".join(handoff.STATUSES))
                if status == "已中断":
                    raise ValueError("已中断只由程序写入")
                completing = {str(value).upper() for value in operation.get("complete_goals") or []}
                remaining = [item["id"] for item in summary.goals()
                             if not item.get("done") and item["id"] not in completing]
                if status == "已完成" and remaining:
                    raise ValueError("仍有未完成目标：" + "、".join(remaining) + "；完成后通过 complete_goals 指定编号")
                summary.header["status"] = status
                written["status"] = status
                done_ids = handoff.complete_goals(summary, operation.get("complete_goals") or [])
                if done_ids:
                    written["goals"].extend(done_ids)
                superseded = handoff.supersede_decisions(summary, operation.get("supersede") or [])
                if superseded:
                    written["decisions"].extend(superseded)
            elif op == "env":
                items = operation.get("items")
                if not isinstance(items, list) or not items:
                    raise ValueError("env.items 必须是非空数组")
                for item in items:
                    item_start = len(rejected)
                    card = _save_env(service, summary, item, rejected)
                    for error in rejected[item_start:]:
                        error["operation"] = {**operation, "items": [item]}
                    if card:
                        written["env"].append(card)
            elif op == "project_prompt":
                sections = operation.get("sections")
                if not summary.project:
                    raise ValueError("独立会话没有项目提示词")
                result = service.store.save_project_prompt(summary.project, sections or {}, summary=summary)
                written["project_prompt"].update(result.get("written") or {})
                rejected.extend(result.get("rejected") or [])
                warnings.extend(result.get("warnings") or [])
            else:
                raise ValueError("op 只允许 append/replace/set_status/env/project_prompt")
        except ValueError as exc:
            rejected.append({"op": op, "reason": str(exc),
                             "example": _op_example(op)})
        for error in rejected[reject_start:]:
            error.setdefault("operation", operation)

    pending_added = _record_rejections(service, summary, rejected)
    now = service.now()
    now_iso = _utc_iso(now.utc)
    summary.header["updated_at"] = now_iso
    summary.header["last_activity_at"] = now_iso
    summary.header["git"] = service.git_info(str(summary.header.get("cwd") or ""))
    if last_test is not None:
        if not isinstance(last_test, dict) or not isinstance(last_test.get("command"), str) or type(last_test.get("exit_code")) is not int:
            rejected.append({"op": "last_test", "reason": "last_test 必须包含 command 和整数 exit_code"})
        elif summary.header.get("last_verify", {}).get("source") == "tool":
            if any(last_test[key] != summary.header["last_verify"].get(key) for key in ("command", "exit_code")):
                rejected.append({"op": "last_test", "reason": "last_test 不能覆盖宿主采集的测试结果；请省略补报并以实际执行结果为准。"})
        else:
            summary.header["last_verify"] = {"command": last_test["command"], "exit_code": last_test["exit_code"],
                                              "at": now_iso, "source": "model"}
    if not rejected:
        summary.header["saved_users"] = len(summary.users)
        summary.header["saved_verify_at"] = summary.header.get("last_verify", {}).get("at")
    write_session(service, summary, path)
    _log_agent_changes(service, handoff.parse(raw_text), summary)
    service.index.log_access(summary.session_id, "save")
    return {
        "ok": not rejected,
        "written": written,
        "rejected": rejected,
        "warnings": warnings,
        "user_modified": user_edited or bool(protected),
        "pending": pending_added,
        "current_time": _time_block(service),
        "session": {"session_id": summary.session_id, "name": summary.name, "project": summary.project,
                    "status": summary.status},
        "revision": summary.header.get("revision", ""),
    }


def _op_example(op: str) -> str:
    return {
        "append": '{"op":"append","section":"done","items":[{"text":"改了 vault.py 的索引事务 [证据:E1]"}]}',
        "replace": '{"op":"replace","section":"next_step","text":"修完中文检索后跑 pytest -q 验证"}',
        "set_status": '{"op":"set_status","status":"已完成","complete_goals":["G1"]}',
        "env": '{"op":"env","items":[{"type":"安装","name":"jieba","version":"0.42.1","location":"venv","state":"生效"}]}',
        "project_prompt": '{"op":"project_prompt","sections":{"最终目标":"...","硬规则":"- ...（来源：U1）"}}',
    }.get(op, "")


def _append_item(service, summary, section, item, rejected, warnings):
    if isinstance(item, str):
        item = {"text": item}
    if not isinstance(item, dict):
        rejected.append({"section": section, "reason": "条目必须是对象", "example": _op_example("append")})
        return ""
    if section == "证据":
        return _append_evidence(service, summary, item, rejected, warnings)
    if section == "未走通的路":
        return _append_dead_end(service, summary, item, rejected)
    text = str(item.get("text") or "").strip()
    if section == "决策" and "|" not in text and item.get("decision") and item.get("reason"):
        text = f"{item['decision']} | {item['reason']}"
    if not text:
        rejected.append({"section": section, "reason": "text 不能为空", "example": _op_example("append")})
        return ""
    if section == "决策" and "|" not in text:
        rejected.append({"section": section, "reason": "决策格式：决策 | 理由",
                         "example": '{"text":"用 SQLite 只做索引 | Markdown 是唯一事实源"}'})
        return ""
    if section == "已完成":
        evidence = str(item.get("evidence") or "").strip()
        if evidence and f"[证据:{evidence}]" not in text:
            text = f"{text} [证据:{evidence}]"
    references = re.findall(r"\[证据:([^\]]*)\]", text)
    if text.count("[证据:") != len(references) or any(
            not re.fullmatch(r"E\d+", reference) or not summary.has_reference(reference) for reference in references):
        rejected.append({"section": section, "reason": "证据引用无效或不存在；先保存证据并使用返回的 E#", "example": _op_example("append")})
        return ""
    if len(text) > handoff.ITEM_MAX_CHARS:
        rejected.append({"section": section, "reason": f"正文连同证据引用不超过 {handoff.ITEM_MAX_CHARS} 字", "example": _op_example("append")})
        return ""
    for candidate in summary.sections.get(section, []):
        if _same_text(candidate.get("text", ""), text):
            return f"{handoff.ITEM_PREFIXES[section]}{candidate['number']}"
    if section == "未完成目标":
        if summary.status == "已完成":
            summary.header["status"] = "进行中"
        return handoff.add_goal(summary, text)
    return handoff.add_item(summary, section, text)


def _append_evidence(service, summary, item, rejected, warnings):
    text = str(item.get("text") or "").strip()
    kind = str(item.get("kind") or "").strip()
    if not text:
        rejected.append({"section": "证据", "reason": "证据内容不能为空",
                         "example": '{"kind":"file","text":"src/tbrain/vault.py:42"}'})
        return ""
    if kind not in ("file", "command", "commit"):
        rejected.append({"section": "证据", "reason": "kind 只允许 file/command/commit", "example": '{"kind":"file","text":"..."}'})
        return ""
    if len(text) > handoff.ITEM_MAX_CHARS:
        rejected.append({'section': '证据', 'reason': '单条证据不超过 200 字，不能静默截断',
                         'example': '{"kind":"file","text":"src/example.py:1"}'})
        return ''
    if kind == "file":
        match = re.match(r"^(.+?):(\d+)$", text)
        if not match:
            rejected.append({"section": "证据", "reason": "文件证据格式：文件:行号", "example": '{"kind":"file","text":"src/tbrain/vault.py:42"}'})
            return ""
        candidate = Path(match.group(1))
        if not candidate.is_absolute():
            candidate = Path(str(summary.header.get("cwd") or "")) / candidate
        if not candidate.is_file():
            warnings.append(f"证据文件未找到：{match.group(1)}（已写入，请自行核对）")
    elif kind == "commit":
        if not re.fullmatch(r"[0-9a-fA-F]{7,40}", text):
            rejected.append({"section": "证据", "reason": "commit 证据只允许 commit 号", "example": '{"kind":"commit","text":"3af5036"}'})
            return ""
    elif kind == "command":
        if text.count("\n") > 2:
            rejected.append({"section": "证据", "reason": "命令+输出最多 3 行", "example": '{"kind":"command","text":"pytest -q\\n130 passed"}'})
            return ""
    for candidate in summary.sections.get("证据", []):
        if candidate.get("text", "").strip() == text:
            return f"E{candidate['number']}"
    return handoff.add_item(summary, "证据", text)


def _dead_end_context(service, conditions: str, command: str, summary, cards=None) -> dict:
    cards = service.store.public_cards() if cards is None else cards
    packages = {}
    for name in set(re.findall(r'[A-Za-z][A-Za-z0-9_.-]*', conditions)):
        try:
            packages[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
    return {'platform': platform.system() + ' ' + platform.release(), 'python': sys.version.split()[0],
            'packages': packages,
            'dependencies': {card['name']: [card.get('version', ''), card.get('location', '')]
                             for card in cards if card['name'].casefold() in conditions.casefold()},
            'command_passed': bool(command and summary.header.get('last_verify', {}).get('command') == command
                                   and summary.header['last_verify'].get('exit_code') == 0)}


def _refresh_dead_ends(service, summary) -> None:
    if not summary.header.get('dead_end_conditions'):
        return
    cards = service.store.public_cards()
    for item in summary.items('未走通的路'):
        saved = summary.header.get('dead_end_conditions', {}).get(item['id'])
        if saved and saved['baseline'] != _dead_end_context(service, saved['conditions'], saved['command'], summary, cards):
            source = next(row for row in summary.sections['未走通的路'] if row['number'] == item['number'])
            source['text'] = re.sub(r'\| (已排除|暂缓)', '| 可重试', source['text'])


def _append_dead_end(service, summary, item, rejected):
    attempt = str(item.get("attempt") or item.get("text") or "").strip()
    reason_kind = str(item.get("reason_kind") or "").strip()
    conditions = str(item.get("conditions") or "").strip()
    strength = str(item.get("strength") or "").strip()
    if not attempt:
        rejected.append({"section": "未走通的路", "reason": "缺少 attempt", "example": '{"attempt":"...","reason_kind":"环境/权限限制","conditions":"Windows 10","strength":"暂缓"}'})
        return ""
    if reason_kind not in handoff.REASON_KINDS:
        rejected.append({"section": "未走通的路", "reason": "reason_kind 只允许：" + " / ".join(handoff.REASON_KINDS),
                         "example": '{"attempt":"...","reason_kind":"环境/权限限制","conditions":"...","strength":"暂缓"}'})
        return ""
    if not conditions:
        rejected.append({"section": "未走通的路", "reason": "conditions 必填（版本/平台/配置）", "example": '{"attempt":"...","reason_kind":"时间不够","conditions":"Windows 10 + Python 3.11","strength":"暂缓"}'})
        return ""
    if strength not in handoff.STRENGTHS:
        rejected.append({"section": "未走通的路", "reason": "strength 只允许：已排除 / 暂缓", "example": '{"attempt":"...","strength":"暂缓"}'})
        return ""
    if strength == "已排除" and not str(item.get("evidence") or "").strip():
        rejected.append({"section": "未走通的路", "reason": "结论为“已排除”必须附证据 E#", "example": '{"attempt":"...","strength":"已排除","evidence":"E2"}'})
        return ""
    if item.get("evidence") and (not re.fullmatch(r"E\d+", str(item["evidence"])) or
                                 not summary.has_reference(str(item["evidence"]))):
        rejected.append({"section": "未走通的路", "reason": "证据 E# 不存在，请先保存证据并使用返回编号",
                         "example": '{"evidence":"E1"}'})
        return ""
    text = f"{attempt} | {reason_kind} | {conditions} | {strength}"
    if item.get("evidence"):
        text += f" [证据:{item['evidence']}]"
    if len(text) > handoff.ITEM_MAX_CHARS:
        rejected.append({"section": "未走通的路", "reason": f"单条不超过 {handoff.ITEM_MAX_CHARS} 字", "example": "精简 attempt/conditions"})
        return ""
    for candidate in summary.sections.get("未走通的路", []):
        if _same_text(candidate.get("text", ""), text):
            return f"X{candidate['number']}"
    number = handoff.add_item(summary, "未走通的路", text)
    command = str(item.get('command') or attempt)
    summary.header.setdefault('dead_end_conditions', {})[number] = {
        'conditions': conditions, 'command': command,
        'baseline': _dead_end_context(service, conditions, command, summary)}
    return number


def _save_env(service, summary, item, rejected):
    if not isinstance(item, dict):
        rejected.append({"op": "env", "reason": "env 条目必须是对象", "example": _op_example("env")})
        return ""
    required = ("type", "name")
    missing = [key for key in required if not str(item.get(key) or "").strip()]
    if missing:
        rejected.append({"op": "env", "reason": "缺少字段：" + ",".join(missing), "example": _op_example("env")})
        return ""
    kind = str(item.get("type") or "").strip()
    if kind not in ("安装", "环境变量", "PATH", "代理", "配置", "工作区", "工具"):
        rejected.append({"op": "env", "reason": "type 只允许：安装/环境变量/PATH/代理/配置/工作区/工具", "example": _op_example("env")})
        return ""
    try:
        card = service.store.save_public_card(kind, str(item.get("name")).strip(), item,
                                              source_session=f"[[{summary.name}]]")
    except ValueError as exc:
        rejected.append({'op': 'env', 'reason': str(exc), 'example': _op_example('env')})
        return ''
    return card.get("id") or ""


# ---------------------------------------------------------------------------
# search / resume / bootstrap
# ---------------------------------------------------------------------------

def search(service, query: str, session_id: str = "", limit: int = 5) -> dict:
    service.refresh_index()
    timeout_fallback(service)
    query = str(query or "").strip()
    if not query:
        return {"ok": False, "error": "query 不能为空", "results": []}
    limit = max(1, min(int(limit or 5), 5))
    row = _session_row(service, session_id) if session_id else None
    project = str(row.get("project") or "") if row else ""
    projects = [project] if project else []
    if project:
        projects += service.child_projects(project)
    results: list[dict] = []
    for item in service.index.search_sessions(query, projects, limit):
        results.append({**item, "kind": "session", "source": "项目"})
    for item in service.index.search_public(query, limit):
        results.append({**item, "kind": "public", "source": "公共池"})
    for item in service.index.search_sessions(query, [""], limit):
        results.append({**item, "kind": "session", "source": "独立会话池"})
    seen = set()
    unique = []
    for item in results:
        key = item.get("session_id") or item.get("id") or item.get("path")
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    unique = unique[:limit]
    terms = [term.casefold() for term in service.index.bigram_terms(query)]
    for item in unique:
        if item["kind"] != "session":
            continue
        summary = handoff.parse(service.store.read_text(service.vault_root() / item["path"]))
        candidates = [f"目标：{summary.target}", f"下一步：{summary.next_step}"]
        candidates.extend(f"{entry['id']} {entry['text']}" for entry in summary.users)
        for section in handoff.ITEM_PREFIXES:
            candidates.extend(f"{entry['id']} {entry['text']}" for entry in summary.items(section))
        matches = sorted(candidates, key=lambda text: sum(term in text.casefold() for term in terms), reverse=True)
        item["snippet"] = _clip("\n".join(text for text in matches[:3] if any(term in text.casefold() for term in terms)), 500)
    return {"ok": True, "query": query, "results": unique, "current_time": _time_block(service)}


def resume(service, session_id: str, target: str, *, fork_user_count=None, fork_revision: str = "", fork: bool = False) -> dict:
    row = _session_row(service, session_id)
    if not row:
        return {"ok": False, "error": "当前会话不存在；请先让 hook 立项或调用 bootstrap"}
    current, current_path = load_session(service, row)
    target = str(target or "").strip().strip("[]")
    if not target:
        return {"ok": False, "error": "请提供要接续的会话名或 session_id"}
    target_row = _session_row(service, target)
    if not target_row:
        matches = [item for item in service.index.sessions() if item.get("name") == target]
        target_row = _session_row(service, matches[0]["session_id"]) if matches else None
    if not target_row:
        return {"ok": False, "error": f"找不到要接续的会话：{target}"}
    if target_row["session_id"] == current.session_id:
        return {"ok": False, "error": "不能接续当前会话本身"}
    old, _ = load_session(service, target_row)
    if fork or fork_user_count is not None or fork_revision:
        if current.target or current.next_step or any(current.sections.get(section) for section in handoff.ITEM_PREFIXES):
            return {'ok': False, 'error': '当前会话已有保存进度，不能用历史分支替换；请在新会话中选定分叉点'}
        folder = service.vault_root() / ".tbrain" / "会话版本" / handoff.fingerprint(old.session_id)
        versions = list(folder.glob("*.md"))
        if fork_revision:
            versions = [path for path in versions if path.stem == fork_revision]
        elif fork_user_count is not None:
            if not isinstance(fork_user_count, int) or fork_user_count < 0:
                return {"ok": False, "error": "fork_user_count 必须是非负整数"}
            versions = [path for path in versions if path.name.startswith(f"U{fork_user_count}-")]
        else:
            return {"ok": False, "error": "分支需提供 fork_revision 或 fork_user_count；不能用父会话最新进度代替分叉点"}
        if not versions:
            return {"ok": False, "error": "没有对应分叉点的保存版本，请回查宿主原文；当前会话仍独立保存"}
        version = max(versions, key=lambda path: path.stat().st_mtime_ns)
        old = handoff.parse(service.store.read_text(version))
        for section in old.sections:
            inherited = [dict(item) for item in old.sections[section]]
            if section == "用户指令":
                inherited += [{"number": len(inherited) + i + 1, "text": item["text"]}
                              for i, item in enumerate(current.users)]
            current.sections[section] = inherited
        current.raw_command = current.raw_command or old.raw_command
        current.target, current.next_step = old.target, old.next_step
        current.archive = list(old.archive)
        current.extra = dict(old.extra)
        for key in ("last_verify", "git", "dead_end_conditions"):
            if key in old.header:
                current.header[key] = old.header[key]
        current.header.update(branched_from=f"[[{old.name}]]", branch_revision=version.stem,
                              branch_user_count=len(old.users))
    users = old.users[-3:]
    done = [f"{item['id']} {item.get('text', '')}" for item in old.items("已完成")]
    evidence = [f"{item['id']} {item.get('text', '')}" for item in old.items("证据")]
    goals = [f"{item['id']} {item.get('text', '')}" for item in old.goals() if not item.get("done")]
    lines = [f"## 接续自 [[{old.name}]]", "", "### 原始命令", old.raw_command or "（空）", ""]
    lines += ["### 目标", old.target or "（未写）", "", "### 未完成目标"] + (goals or ["（无）"]) + [""]
    lines += ["### 下一步", old.next_step or "（未写）", "", "### 最近用户指令"]
    lines += ([f"- {item['id']}: {item.get('text', '')}" for item in users] or ["（无）"]) + [""]
    lines += ["### 已完成"] + (done[-10:] or ["（无）"]) + [""]
    lines += ["### 证据"] + (evidence[-10:] or ["（无）"]) + [""]
    for title in ("决策", "未走通的路"):
        lines += [f"### {title}"] + [f"{item['id']} {item['text']}" for item in old.items(title)] + [""]
    if old.extra.get("旧总结原文（迁移）"):
        lines += ["### 旧总结原文（迁移）", old.extra["旧总结原文（迁移）"], ""]
    # 当前会话写“接续自”
    if current.name != old.name:
        current.header["resumed_from"] = f"[[{old.name}]]"
        if current.header.get("provisional_name"):
            date = service.now().local.strftime("%Y-%m-%d")
            seq = next_sequence(service, current.project, date)
            current_path = rename_session(service, current, current_path, naming.resume_name(old.name, date, seq))
        write_session(service, current, current_path)
    service.index.log_access(old.session_id, "resume")
    ranking = service.index.record_resume_feedback(current.session_id, current.project, old.session_id)
    return {"ok": True, "resumed_from": old.name, "session_id": current.session_id,
            "resumed_session_id": old.session_id,
            "handoff": "\n".join(lines), "current_time": _time_block(service), "ranking": ranking}


def bootstrap(service, session_id: str, agent: str, cwd: str, host_record: str = "",
              project: str | None = None) -> dict:
    service.refresh_index()
    timeout_fallback(service)
    host, host_session_id = identity.split_session_id(session_id) or (identity.normalize_host(agent or "unknown"), session_id)
    existing = _load_or_none(service, host, host_session_id)
    if existing[0] is None:
        summary, path = create_summary(service, host=host, host_session_id=host_session_id, cwd=cwd,
                                       host_record=host_record, first_message="",
                                       project_override=project)
    else:
        summary, path = existing
        _mark_active(service, summary, path)
        if project is not None and project != "":
            if service.store.project(project):
                summary.header["project"] = project
                summary.header["project_link"] = f"[[{project}]]"
                write_session(service, summary, path)
    service.refresh_index()
    package = assemble_startup(service, summary, host,
                              resume=bool(summary.users), related_query=summary.raw_command)
    service.index.log_access(summary.session_id, "bootstrap")
    return {
        "ok": True,
        "session_id": summary.session_id,
        "revision": summary.header.get("revision", ""),
        "session": {"name": summary.name, "project": summary.project, "status": summary.status},
        "project": summary.project,
        "project_directory": str(_project_dir(service, summary.project)) if summary.project else "",
        "package": package,
        "tbrain_version": service.version(),
        "current_time": _time_block(service),
    }


def session_detail(service, session_id: str) -> dict:
    row = _session_row(service, session_id)
    if not row:
        return {"ok": False, "error": "会话不存在"}
    summary, path = load_session(service, row)
    return {"ok": True, "session": row, "markdown": handoff.render(summary), "path": str(path)}

def _rejection_file(service) -> Path:
    return service.home / "rejections.json"


def _log_agent_changes(service, before, summary) -> None:
    """只记录保存成功的已有字段变更；新增条目由会话版本保留。"""
    previous, current = (
        {"target": item.target, "next_step": item.next_step, "status": item.status,
         **{entry["id"]: entry for section in ("决策", "未完成目标") for entry in item.items(section)}}
        for item in (before, summary)
    )
    changes = [field for field in previous if field in current and previous[field] != current[field]]
    if not changes:
        return
    try:
        with (service.home / "agent-changes.jsonl").open("a", encoding="utf-8") as handle:
            for field in changes:
                record = {"at": _utc_iso(service.now().utc), "session_id": summary.session_id,
                          "revision": summary.header["revision"],
                          "op": "replace" if field in ("target", "next_step") else "set_status",
                          "field": field, "old": previous[field], "new": current[field]}
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _read_rejections(service) -> dict:
    try:
        payload = json.loads(_rejection_file(service).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_rejections(service, payload: dict) -> None:
    path = _rejection_file(service)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("rejections.json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _record_rejections(service, summary: handoff.Summary, rejected: list[dict]) -> list[str]:
    """同一不合格操作重试 2 次后，把原文写进“待整理”并标未校验。"""
    if not any(item.get("reason") for item in rejected):
        return []
    payload = _read_rejections(service)
    session_rejects = payload.get(summary.session_id, {})
    pending_added: list[str] = []
    seen = set()
    for item in rejected:
        if item.get("reason", "").startswith("该会话总结已被用户手改"):
            continue
        raw = item["operation"]
        signature = json.dumps(raw, ensure_ascii=False, sort_keys=True)
        count = int(session_rejects.get(signature) or 0)
        if signature not in seen:
            count += 1
            session_rejects[signature] = count
            if count == 3:
                line = f"- 未校验：{signature}（{_utc_iso(service.now().utc)}，多次不合格，原文保留）"
                summary.pending.append(line)
                pending_added.append(line)
            seen.add(signature)
    payload[summary.session_id] = session_rejects
    _write_rejections(service, payload)
    return pending_added
