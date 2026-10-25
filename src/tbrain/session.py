"""最小闭环的流程层：立项、启动包、hook 语义、save/search/resume。

本模块只依赖 service 暴露的存储/索引/时钟能力（见 docs/ARCHITECTURE.md），
不直接操作文件与数据库，便于单独测试与替换实现。
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
import json
import re
import subprocess
import time
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
    if not row and (identity_parts := identity.split_session_id(session_id)):
        link = service.index.get_session_link(*identity_parts)
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
    owners = summary.header.setdefault("user_owned_fields", [])
    for field, digest in summary.header.get("model_fingerprints", {}).items():
        if field in ("target", "next_step") and digest != handoff.fingerprint(getattr(summary, field)):
            if field not in owners:
                owners.append(field)
    return summary, row["abs_path"]


def write_session(service, summary: handoff.Summary, path: Path) -> None:
    for field in ("target", "next_step"):
        if field not in summary.header.get("user_owned_fields", []):
            summary.header.setdefault("model_fingerprints", {})[field] = handoff.fingerprint(getattr(summary, field))
    body = handoff.render(summary)
    service.store.write_text(path, body)
    service.remember_write(path, body)
    service.index.upsert_session(_row_from_summary(service, summary, path), body)


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
    low_info = naming.is_low_information(first_message)
    if low_info or not first_message.strip():
        name = naming.temp_name(date, sequence)
        provisional = True
    else:
        name = naming.session_name(first_message, project, history, date, sequence)
        provisional = False
    summary = handoff.new_summary(
        name=name, project=project, session_id=identity.build_session_id(host, host_session_id),
        agent=host, cwd=cwd, host_record=host_record, started_at=_utc_iso(now.utc),
        raw_command=first_message, provisional=provisional,
    )
    path = service.store.session_path(project or "独立", now.local, name)
    service.store.ensure_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_session(service, summary, path)
    service.index.register_link(host, host_session_id, summary.session_id, cwd,
                                identity.first_message_hash(first_message), summary.header["started_at"])
    service.index.log_access(summary.session_id, "bootstrap")
    return summary, path


def resolve_session(service, host: str, host_session_id: str, cwd: str,
                    first_message: str = "") -> tuple[handoff.Summary, Path, bool]:
    """按宿主身份找已有会话；新 ID 则用 cwd + 原命令完全相同匹配旧会话；都没有则新建。"""
    session_id = identity.build_session_id(host, host_session_id)
    row = _session_row(service, session_id)
    if row:
        summary, path = load_session(service, row)
        _mark_active(service, summary, path)
        return summary, path, False
    reused = identity.find_reused_session(service.index, cwd, first_message)
    if reused:
        row = _session_row(service, reused)
        if row:
            summary, path = load_session(service, row)
            service.index.register_link(host, host_session_id, summary.session_id, cwd,
                                        identity.first_message_hash(first_message), _utc_iso(service.now().utc))
            _mark_active(service, summary, path)
            return summary, path, False
    summary, path = create_summary(service, host=host, host_session_id=host_session_id, cwd=cwd,
                                   first_message=first_message)
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
    return prompt_mod.render(data.get("sections") or {}, data.get("user") or "").strip()


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
    rows = service.index.resume_view(project, child_projects, limit=limit, related_query=related_query)
    for row in rows:
        row["updated_display"] = _display(service, row.get("updated_at", ""))
    return rows


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
    footer = _time_block(service) + "\n\n" + _identity_block(service, summary)
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


def _stop_reminder(service, summary: handoff.Summary, path: Path, host: str) -> HookResult:
    summary.header["last_activity_at"] = _utc_iso(service.now().utc)
    write_session(service, summary, path)
    if host == "pi":
        hint = context_switch_hint(service, summary, None, None)
        message = "本轮尚未保存。请调用 save 写入完成、决策、证据与下一步。" + (f"\n{hint}" if hint else "")
        return HookResult(additional_context=message)
    return HookResult(decision="block",
                      reason="本轮还没有保存记录。请调用 save 提交增量（完成 D#、决策 K#、证据 E#、下一步），"
                             "再结束本轮。已保存则忽略本提醒。")


def handle_hook(service, event: HookEvent) -> HookResult:
    service.refresh_index()
    timeout_fallback(service)
    warning = _version_warning(service)
    host = event.host or "unknown"

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
        if len(summary.users) > int(summary.header.get("saved_users") or 0):
            return _stop_reminder(service, summary, path, host)
        summary.header["last_activity_at"] = _utc_iso(service.now().utc)
        write_session(service, summary, path)
        return HookResult()

    if event.kind == "pre-compact":
        summary, path = _load_or_none(service, host, event.host_session_id)
        if summary is None:
            return HookResult()
        users = summary.users[-5:]
        lines = ["## 压缩前重新注入的用户指令（防止压缩丢失约束）"]
        for item in users:
            lines.append(f"### {item['id']}\n{item.get('text', '')}")
        lines.append("请在本轮结束前调用 save 保存最新进展。")
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
        if source == "startup":
            return HookResult()
        existing = _load_or_none(service, host, event.host_session_id)
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
                                                     first_message=first_message)
            package = assemble_startup(service, summary, host, related_query=first_message)
            if warning:
                package = warning + "\n\n" + package
            service.index.log_access(summary.session_id, "bootstrap")
            return HookResult(additional_context=package)
        summary, path = existing
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
    if users and _same_text(users[-1].get("text", ""), text):
        return
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
    hours = int(service.settings.load().timeout_hours or 0)
    if hours <= 0:
        return 0
    changed = 0
    threshold = datetime.now(timezone.utc) - timedelta(hours=hours)
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
    if int(summary.header.get("saved_users") or 0) < len(summary.users):
        return ""
    settings = service.settings.load()
    if not getattr(settings, "reminder_context_switch", True):
        return ""
    if not tokens or not window:
        return ""
    ratio = float(tokens) / float(window)
    if ratio < 0.65:
        return ""
    savings = int(min(50, max(10, (ratio - 0.5) * 60)))
    return (f"记录已保存。当前会话上下文已用 {ratio * 100:.0f}%（约 {int(tokens)} tokens）。"
            f"建议新开会话继续，可省约 {savings}% 的后续上下文成本。")


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
    note = ""

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
                result = service.store.save_project_prompt(summary.project, sections or {})
                written["project_prompt"] = result.get("written") or {}
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
    if not rejected:
        summary.header["saved_users"] = len(summary.users)
    summary.header["git"] = service.git_info(str(summary.header.get("cwd") or ""))
    if last_test:
        summary.header["last_verify"] = {
            "command": str(last_test.get("command") or ""),
            "exit_code": last_test.get("exit_code"),
            "at": str(last_test.get("at") or now_iso),
        }
    write_session(service, summary, path)
    service.index.log_access(summary.session_id, "save")
    hint = context_switch_hint(service, summary, None, None)
    if hint:
        note = hint
    return {
        "ok": not rejected or any(written.values()),
        "written": written,
        "rejected": rejected,
        "warnings": warnings,
        "note": note,
        "user_modified": user_edited or bool(protected),
        "pending": pending_added,
        "current_time": _time_block(service),
        "session": {"session_id": summary.session_id, "name": summary.name, "project": summary.project,
                    "status": summary.status},
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
    if not text:
        rejected.append({"section": section, "reason": "text 不能为空", "example": _op_example("append")})
        return ""
    if len(text) > handoff.ITEM_MAX_CHARS:
        rejected.append({"section": section, "reason": f"单条不超过 {handoff.ITEM_MAX_CHARS} 字", "example": _op_example("append")})
        return ""
    if section == "决策" and "|" not in text:
        if item.get("decision") and item.get("reason"):
            text = f"{item['decision']} | {item['reason']}"
        else:
            rejected.append({"section": section, "reason": "决策格式：决策 | 理由",
                             "example": '{"text":"用 SQLite 只做索引 | Markdown 是唯一事实源"}'})
            return ""
    if section == "已完成":
        evidence = str(item.get("evidence") or "").strip()
        if evidence and f"[证据:{evidence}]" not in text:
            text = f"{text} [证据:{evidence}]"
    for candidate in summary.sections.get(section, []):
        if _same_text(candidate.get("text", ""), text):
            return f"{handoff.ITEM_PREFIXES[section]}{candidate['number']}"
    if section == "未完成目标":
        return handoff.add_goal(summary, text)
    return handoff.add_item(summary, section, text)


def _append_evidence(service, summary, item, rejected, warnings):
    text = str(item.get("text") or "").strip()
    kind = str(item.get("kind") or "").strip()
    if not text:
        rejected.append({"section": "证据", "reason": "证据内容不能为空",
                         "example": '{"kind":"file","text":"src/tbrain/vault.py:42"}'})
        return ""
    if kind and kind not in ("file", "command", "commit"):
        rejected.append({"section": "证据", "reason": "kind 只允许 file/command/commit", "example": '{"kind":"file","text":"..."}'})
        return ""
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
        if _same_text(candidate.get("text", ""), text):
            return f"E{candidate['number']}"
    return handoff.add_item(summary, "证据", text)


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
    text = f"{attempt} | {reason_kind} | {conditions} | {strength}"
    if item.get("evidence"):
        text += f" [证据:{item['evidence']}]"
    if len(text) > handoff.ITEM_MAX_CHARS:
        rejected.append({"section": "未走通的路", "reason": f"单条不超过 {handoff.ITEM_MAX_CHARS} 字", "example": "精简 attempt/conditions"})
        return ""
    for candidate in summary.sections.get("未走通的路", []):
        if _same_text(candidate.get("text", ""), text):
            return f"X{candidate['number']}"
    return handoff.add_item(summary, "未走通的路", text)


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
    card = service.store.save_public_card(kind, str(item.get("name")).strip(), item,
                                          source_session=f"[[{summary.name}]]")
    return card.get("id") or ""


# ---------------------------------------------------------------------------
# search / resume / bootstrap
# ---------------------------------------------------------------------------

def search(service, query: str, session_id: str = "", limit: int = 5) -> dict:
    service.refresh_index()
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
    return {"ok": True, "query": query, "results": unique, "current_time": _time_block(service)}


def resume(service, session_id: str, target: str) -> dict:
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
    return {"ok": True, "resumed_from": old.name, "session_id": current.session_id,
            "resumed_session_id": old.session_id,
            "handoff": "\n".join(lines), "current_time": _time_block(service)}


def bootstrap(service, session_id: str, agent: str, cwd: str, host_record: str = "",
              project: str | None = None) -> dict:
    host, host_session_id = identity.split_session_id(session_id) or (identity.normalize_host(agent or "unknown"), session_id)
    existing = _load_or_none(service, host, host_session_id)
    if existing[0] is None:
        summary, path = create_summary(service, host=host, host_session_id=host_session_id, cwd=cwd,
                                       host_record=host_record, first_message="",
                                       project_override=project)
    else:
        summary, path = existing
        if project is not None and project != "":
            if service.store.project(project):
                summary.header["project"] = project
                summary.header["project_link"] = f"[[{project}]]"
                write_session(service, summary, path)
    service.refresh_index()
    package = assemble_startup(service, summary, host,
                              related_query=summary.raw_command)
    service.index.log_access(summary.session_id, "bootstrap")
    return {
        "ok": True,
        "session_id": summary.session_id,
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
    remaining = []
    seen = set()
    for item in rejected:
        if item.get("reason", "").startswith("该会话总结已被用户手改"):
            remaining.append(item)
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
        if count < 3:
            remaining.append(item)
    rejected[:] = remaining
    payload[summary.session_id] = session_rejects
    _write_rejections(service, payload)
    return pending_added
