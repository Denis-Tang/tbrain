"""最小闭环的流程层：立项、启动包、hook 语义、save/search/resume。

本模块只依赖 service 暴露的存储/索引/时钟能力（见 docs/ARCHITECTURE.md），
不直接操作文件与数据库，便于单独测试与替换实现。
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
import copy
import json
import re
import sys
import platform
from importlib import metadata
from pathlib import Path
from zoneinfo import ZoneInfo

from . import clock, handoff, identity, naming, prompt as prompt_mod
from .contract import render_contract
from .hooks import HookEvent, HookResult
from .policy import TIERS, classify
from . import progress

HOST_BUDGETS = {"claude-code": 9000, "codex": 8000, "dsh": 12000, "pi": 12000, "unknown": 8000}
STATUS_PRIORITY = {"已中断": 0, "已暂停": 0, "进行中": 1, "受阻": 2, "待验证": 3, "已完成": 4, "已放弃": 5}


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
    # 否定词、数字和代码中的空格都可能改变含义；只合并完全相同的正文。
    a, b = str(left or "").strip(), str(right or "").strip()
    return bool(a) and a == b


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
    snapshot_path = folder / (revision + ".md")
    if snapshot_path.is_file():
        return
    try:
        service.store.write_text(snapshot_path, body, create=True)
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
        summary.header["task_mode"] = service.settings.load().task_mode
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
    renamed_header = handoff.parse(service.store.read_text(new_path)).header
    if "aliases" in renamed_header:
        summary.header["aliases"] = renamed_header["aliases"]
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


def _project_prompt_parts(service, project: str) -> list[tuple[str, str]]:
    if not project:
        return []
    data = service.store.project_prompt(project)
    shas = {name: service.store.block_fingerprint(data['sections'].get(name, ''))[:8]
            for name in prompt_mod.SECTION_ORDER}
    def rules_for(name):
        groups = service.store.classify_project_rules(name)
        rules = groups['active'] + [line + '（来源待核对）' for line in groups['unresolved']]
        extras = []
        if groups['pending']:
            extras.append(f"## 来源待确认规则：{name}\n来源尚未确认，不作为有效硬规则；原文保留。\n" +
                          '\n'.join(groups['pending']))
        if groups['unresolved']:
            extras.append(f"来源待核对：{len(groups['unresolved'])} 条（{name}，规则原文已保留，请核对来源后使用）")
        if groups['inactive']:
            extras.append(f"已排除失效硬规则：{len(groups['inactive'])} 条（{name}，原文保留在项目提示词中）")
        return '\n'.join(rules), extras
    sections = dict(data.get('sections') or {})
    sections['硬规则'], extra_rules = rules_for(project)
    parents = []
    ancestors = {project}
    parent = (service.store.project(project) or {}).get("parent")
    while parent:
        if parent in ancestors:
            raise ValueError('项目卡父级关系形成循环，请修正项目卡')
        ancestors.add(parent)
        rules, parent_extras = rules_for(parent)
        inherited = []
        if rules:
            inherited.append((f"继承 {parent} 的硬规则", f"## 继承 {parent} 的硬规则\n{rules}"))
        parents[0:0] = inherited
        extra_rules.extend(parent_extras)
        parent = (service.store.project(parent) or {}).get("parent")
    parts = [(name, f"## {name} 〔sha:{shas[name]}〕\n{sections.get(name, '')}") for name in prompt_mod.SECTION_ORDER]
    parts[1:1] = parents
    parts += [(f"规则来源说明{n + 1}", text if text.startswith('## ') else '## 规则来源说明\n' + text)
              for n, text in enumerate(extra_rules)]
    parts.append(("项目提示词规范", "## 项目提示词维护\n项目提示词规范见save的project_prompt说明；核对written/rejected，保留用户区。\n原文：" +
                  str(service.store.project_prompt_path(project))))
    user_lines = str(data.get('user') or '').splitlines()
    if user_lines and user_lines[0] == '## 用户补充':
        user_lines = user_lines[1:]
    if user_lines:
        parts.append(("用户补充", '## 用户补充\n' + '\n'.join(user_lines)))
    return parts


def _project_prompt_block(service, project: str) -> str:
    return '\n\n'.join(text for _, text in _project_prompt_parts(service, project))


def _resume_rows(service, summary: handoff.Summary, related_query: str, limit: int = 5) -> list[dict]:
    project = summary.project
    projects, _ = service.visible_projects_for_project(project)
    child_projects = [name for name in projects if name != project]
    rows = service.index.resume_view(project, child_projects, limit=50, related_query=related_query,
                                     cwd=str(summary.header.get("cwd") or "") if not project else "")
    rows = [row for row in rows if row['session_id'] != summary.session_id]
    if not rows:
        rows = [row for row in service.index.resume_view(project, child_projects, limit=50,
                cwd=str(summary.header.get('cwd') or '') if not project else '') if row['session_id'] != summary.session_id]
    service.index.set_meta('resume_view:' + summary.session_id, json.dumps({
        'at': _utc_iso(clock.utc_now()), 'cwd': summary.header.get('cwd', '') if not project else '',
        'rows': [{key: row[key] for key in ('session_id', 'status', 'updated_at', 'cwd', 'name')} for row in rows]}, ensure_ascii=False))
    for row in rows:
        row["updated_display"] = _display(service, row.get("updated_at", ""))
    return rows[:limit]


def _time_block(service) -> str:
    now = service.now()
    offset = now.offset_seconds
    calibrate = "已校准" if now.calibrated else "未校准"
    sign = "+" if offset >= 0 else ""
    return f"## 当前时间\n{now.utc.strftime('%Y-%m-%d %H:%M:%S')} UTC（{now.local.strftime('%Y-%m-%d %H:%M')}，{now.timezone}，{calibrate}，偏差 {sign}{offset:.0f}s）"


def _identity_block(service, summary: handoff.Summary) -> str:
    return (f"## 当前会话\nsession_id: {summary.session_id}\n"
            f"之后调用 search/save/save_decision/resume 时原样传入该 session_id。" +
            (f"\n当前用户原话编号：{summary.users[-1]['id']}" if summary.users else ''))


def assemble_startup(service, summary: handoff.Summary, host: str, *,
                     resume: bool = False, related_query: str = "") -> str:
    budget = HOST_BUDGETS.get(host, HOST_BUDGETS["unknown"])
    lead = progress.lead_block(service, summary)
    contract = render_contract(service, summary.project, cwd=str(summary.header.get("cwd") or ""),
                               task_mode=progress.enabled(summary))
    mode = ("## 工作模式\n任务模式：已开启（本会话）。" if progress.enabled(summary) else
            "## 工作模式\n普通记忆模式：本会话不附加任务确认、停滞暂停或固定收尾要求；用户指令和项目业务规则始终有效。")
    blocks = [("任务提示", lead, None, True), ("工作约定", contract, None, True),
              ("工作模式", mode, None, True), ("当前时间", _time_block(service), None, True),
              ("当前会话", _identity_block(service, summary), None, True)]
    inline = []
    lead_lines = lead.splitlines()
    if summary.target and '任务目标：' + summary.target not in lead_lines:
        inline.append('目标：' + summary.target)
    if summary.next_step and '具体下一步：' + summary.next_step not in lead_lines:
        inline.append('下一步：' + summary.next_step)
    if inline:
        blocks.append(('当前目标与下一步', '## 当前进度\n' + '\n'.join(inline), None, True))
    omitted_details = []
    indexes = []
    vault = service.vault_root()
    current_row = service.index.session(summary.session_id)
    current_path = vault / current_row['path']

    def detail(name, path, count, text):
        indexes.append((name + '索引', f'## {name}\n{count}条；{path}', None, True))
        if text:
            omitted_details.append((name + '正文', len(text)))

    if summary.project:
        card = service.store.project(summary.project) or {}
        note = str(card.get('worktree_error') or card.get('worktree_limitation') or '').strip()
        if note:
            path = service.store.project_card_path(summary.project)
            blocks.append(('工作区说明索引', f'## 工作区说明\n1条；{path}', None, True))
            omitted_details.append(('工作区说明正文', len(note)))
    project_parts = _project_prompt_parts(service, summary.project)
    user = [part for part in project_parts if part[0] == '用户补充']
    project_indexes = []
    project_remaining = 2650
    for name, text in project_parts:
        if name == '用户补充':
            continue
        primary = name in ('最终目标', '硬规则') or name.startswith('继承 ')
        limit = None if primary else max(0, project_remaining)
        project_indexes.append(len(blocks))
        business_section = name in prompt_mod.SECTION_ORDER or name.startswith('继承 ')
        blocks.append((name, text, limit, not business_section))
        project_remaining -= min(len(text), len(text) if limit is None else limit) + 2
    project_prompt_index = next((index for index in project_indexes
                                 if blocks[index][0] == '项目提示词规范'), None)
    recent = _recent_users_block(summary, max_chars=sum(len(item.get('text', '')) for item in summary.users[-5:]) + 1000) if resume else ''
    recent_index = len(blocks)
    blocks.append(('最近用户指令', recent, 400, False))
    if resume:
        progress_text = '\n'.join(str(item.get('text') or '') for item in summary.items('已完成') + summary.goals())
        pending_goals = sum(not item.get('done') for item in summary.goals())
        detail(f'本会话已保存进度（未完成{pending_goals}条）', current_path,
               len(summary.items('已完成')) + len(summary.goals()), progress_text)
        detail('完整用户原话', current_path, len(summary.users), ''.join(item.get('text', '') for item in summary.users))
    if summary.project:
        scope, _ = service.visible_projects_for_project(summary.project)
        for project in service.decision_projects(summary.project):
            decisions = [entry for entry in service.store.project_decisions(project) if entry['active']]
            if decisions:
                path = service.store.project_dir(project) / '决策日志.md'
                detail('项目长期决定：' + project, path, len(decisions), '\n'.join(entry['text'] for entry in decisions))
                text = '\n'.join(f"{entry['id']} {entry['text']}" for entry in decisions)
                blocks.append(('项目长期决定：' + project, f'## 项目长期决定：{project}\n{text}', 800, False))
        historical = service.index.sessions(scope)
        historical.sort(key=lambda row: str(row.get('updated_at') or ''), reverse=True)
        found = 0
        for row in historical:
            path = vault / row['path']
            old = handoff.parse(service.store.read_text(path))
            decisions = old.active_decisions()
            if decisions:
                detail('待接续决定：' + old.name, path, len(decisions), '\n'.join(item['text'] for item in decisions))
                found += 1
                if found == 3:
                    break
    project_conflicts = 0
    for path in [vault / '公共池' / '待用户确认.md'] + ([service.store.project_dir(summary.project) / '待用户确认.md'] if summary.project else []):
        if path.is_file():
            text = service.store.read_text(path).strip()
            if text:
                is_public = path == vault / '公共池' / '待用户确认.md'
                count = sum(line.startswith('## ') for line in text.splitlines()) or 1
                detail(('公共池' if is_public else '本项目') + '待用户确认', path, count, text)
                if not is_public:
                    project_conflicts = sum(line.startswith('## ') and '写入冲突' in line
                                            for line in text.splitlines())
    cards = [card for card in service.store.public_cards() if card.get('state') == '生效']
    if cards:
        detail('公共池有效卡片', vault / '公共池', len(cards), service.store.public_l1())
    attachments = service.store.attachments(summary.project)
    if attachments:
        detail('本项目附件卡片', vault / '附件', len(attachments), '\n'.join(card['body'] for card in attachments))
    for row in _resume_rows(service, summary, related_query):
        detail('接续推荐：' + row['name'], vault / row['path'], 1, str(row.get('next_step') or ''))
    detail_indexes = []
    index_remaining = 220
    for name, text, _, whole in indexes:
        limit = len(text) if len(text) <= index_remaining else 0
        detail_indexes.append(len(blocks))
        blocks.append((name, text, limit, whole))
        index_remaining -= limit
    global_index = len(blocks)
    blocks.append(('全局提示词', _global_prompt_block(service), 200, False))
    pointer_indexes = []
    public_conflicts = sum(1 for path in (vault / '公共池' / '冲突').glob('*.md')
                           if path.is_file() and path.stat().st_size)
    if public_conflicts or project_conflicts:
        public_label = f'公共池{public_conflicts}条' + ('（公共池/冲突）' if public_conflicts else '')
        project_label = f'本项目{project_conflicts}条' + ('（项目目录下待用户确认.md）' if project_conflicts else '')
        pointer_indexes.append(len(blocks))
        blocks.append(('写入冲突计数', f'写入冲突：{public_label}；{project_label}', None, True))
    map_index = None
    if summary.project:
        path = service.store.project_dir(summary.project) / '地图.md'
        if path.is_file() and path.stat().st_size:
            map_index = len(blocks)
            pointer_indexes.append(map_index)
            blocks.append(('记忆地图指针', '记忆地图：地图.md（与项目提示词原文同目录，按需读）', None, True))
    blocks.extend((name, text, None, False) for name, text in user)
    kept = [''] * len(blocks)

    def render():
        omissions = omitted_details + [(name, len(text) - len(value)) for (name, text, _, _), value in zip(blocks, kept)
                                       if len(text) > len(value)]
        parts = [text for text in kept if text]
        if omissions:
            parts.append('## 启动包省略清单\n' + '\n'.join(f'- {name}：省略{count}字' for name, count in omissions))
        return '\n\n'.join(parts)

    def allocate(index, limit=None):
        _, text, initial, whole = blocks[index]
        if index == map_index and kept[project_prompt_index] != blocks[project_prompt_index][1]:
            kept[index] = ''
            return
        candidate = text[:limit if limit is not None else initial]
        kept[index] = candidate
        if len(render()) <= budget:
            return
        kept[index] = ''
        if whole:
            return
        low, high = 0, len(candidate)
        while low < high:
            mid = (low + high + 1) // 2
            kept[index] = candidate[:mid]
            if len(render()) <= budget:
                low = mid
            else:
                high = mid - 1
        kept[index] = candidate[:low]

    allocate(recent_index)
    for index, (name, _, _, _) in enumerate(blocks):
        if name != '用户补充' and index != recent_index:
            allocate(index)
    # 初始份额不足时先让出低优先内容，补齐项目四节，再恢复索引和全局。
    low_indexes = detail_indexes + [global_index] + pointer_indexes
    for index in low_indexes:
        kept[index] = ''
    for index in project_indexes:
        allocate(index, len(blocks[index][1]))
    for index in low_indexes:
        allocate(index)
    for index in detail_indexes:
        allocate(index, len(blocks[index][1]))
    # 首份400字之后，把实际余量先还给最近指令；用户补充最后分配。
    allocate(recent_index, budget // 4)
    for index, (name, _, _, _) in enumerate(blocks):
        if name == '用户补充':
            allocate(index)
    return render()


# ---------------------------------------------------------------------------
# Hook 语义
# ---------------------------------------------------------------------------

def _version_warning(service) -> str:
    try:
        return service.version_warning()
    except Exception:
        return ""


def _recent_users_block(summary, title: str = "最近 5 条用户指令（防止压缩丢失约束）", *, max_chars: int = 3000) -> str:
    """Last five user instructions, replayed around compaction (deterministic)."""
    lines = [f"## {title}"]
    for item in reversed(summary.users[-5:]):
        lines.append(f"### {item['id']}\n{item.get('text', '')}")
    return "\n".join(lines)[:max_chars] if len(lines) > 1 else ""


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


def _wrap_up_hook(service, summary, path, host: str) -> HookResult:
    from .wrapup import gaps, message

    if not (progress.enabled(summary) if summary is not None else service.settings.load().task_mode):
        return HookResult()
    if summary is None:
        text = "收尾未通过：没有会话记录，不能核对证据、本地检查、决定理由和权限。"
        return HookResult(decision="block", reason=text,
                          additional_context=text if host == "pi" else "")
    found = gaps(summary)
    summary.header["last_activity_at"] = _utc_iso(service.now().utc)
    if path is not None:
        write_session(service, summary, path)
    if not found:
        return HookResult()
    text = message(found)
    return HookResult(decision="block", reason=text,
                      additional_context=text if host == "pi" else "")


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
                                              "at": _utc_iso(service.now().utc), "source": "tool",
                                              "platform": platform.system()}
            progress.observe_saved(service, summary, attempts=[], dead_ends=[], evidence=[], done=[],
                                   verify=result, now=summary.header["last_verify"]["at"])
            write_session(service, summary, path)
            if progress.is_paused(summary):
                return HookResult(additional_context=progress.pause_instruction(summary))
        return HookResult()

    if event.kind == "session-end":
        summary, path = _load_or_none(service, host, event.host_session_id)
        if summary is not None:
            if summary.status in ("进行中", "受阻", "待验证") and not progress.is_paused(summary):
                summary.header["status"] = "已中断"
            summary.header["last_activity_at"] = _utc_iso(service.now().utc)
            write_session(service, summary, path)
        return HookResult()

    if event.kind == "wrap-up":
        summary, path = _load_or_none(service, host, event.host_session_id)
        return _wrap_up_hook(service, summary, path, host)

    if event.kind == "stop":
        if event.stop_hook_active:
            return HookResult(decision="", reason="")
        summary, path = _load_or_none(service, host, event.host_session_id)
        if summary is None:
            return HookResult()
        if progress.is_paused(summary):
            # Block on Stop asks the host to take another autonomous turn. A pause
            # must end the turn instead, and keep refusing completion on save.
            summary.header["last_activity_at"] = _utc_iso(service.now().utc)
            write_session(service, summary, path)
            return HookResult(additional_context=progress.pause_instruction(summary))
        if _needs_save(summary):
            return _stop_reminder(service, summary, path, host)
        if summary.status == "已完成" or event.extra.get("wrap_up"):
            return _wrap_up_hook(service, summary, path, host)
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
            parent = event.extra.get("parent_session_id", "")
            # 分支必须有自己的身份，不能沿用旧版本猜出的别名。
            if not service.index.session(identity.build_session_id(host, event.host_session_id)):
                child, child_path = create_summary(service, host=host, host_session_id=event.host_session_id,
                                                  cwd=event.cwd, host_record=event.extra.get("host_record", ""))
                if not child.project and parent:
                    parent_row = _session_row(service, parent)
                    if parent_row and parent_row.get("project"):
                        child.header["project"] = parent_row["project"]
                        child.header["project_link"] = f"[[{parent_row['project']}]]"
                        write_session(service, child, child_path)
                        service.sync_session_index("")
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
                aliases = summary.header.setdefault("session_aliases", [])
                alias = identity.build_session_id(host, event.host_session_id)
                if alias not in aliases:
                    aliases.append(alias)
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
            progress.start_confirmation(summary)
            write_session(service, summary, path)
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
            summary.sections["用户指令"] = [{"number": 1, "text": first_message}]
            summary.header["last_activity_at"] = _utc_iso(service.now().utc)
            summary.header["updated_at"] = summary.header["last_activity_at"]
            if summary.header.get("provisional_name"):
                date = service.now().local.strftime("%Y-%m-%d")
                sequence = next_sequence(service, summary.project, date)
                name = naming.session_name(first_message, summary.project,
                                          _history_names(service, summary.project), date, sequence)
                path = rename_session(service, summary, path, name)
            progress.start_confirmation(summary)
            write_session(service, summary, path)
            package = assemble_startup(service, summary, host, related_query=first_message)
            if warning:
                package = warning + "\n\n" + package
            return HookResult(additional_context=package)
        if summary.header.get("branch_user_count") == len(summary.users):
            progress.start_confirmation(summary)
        record_user(service, summary, path, first_message)
        notice = progress.turn_notice(service, summary)
        reference = f"当前用户原话编号：{summary.users[-1]['id']}"
        return HookResult(additional_context='\n'.join(part for part in (notice, reference) if part))

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
        with service.store.write_lock():
            if not path.is_file():
                continue
            summary = handoff.parse(service.store.read_text(path))
            moment = _parse_utc(summary.header.get("last_activity_at"))
            if summary.status != "进行中" or moment is None or moment >= threshold:
                continue
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


def save_decision(service, session_id: str, text: str, source: list[str], why: str = '',
                  supersedes: list[str] | None = None) -> dict:
    row = _session_row(service, session_id)
    if not row:
        raise ValueError('会话不存在：请先调用 bootstrap')
    summary, path = load_session(service, row)
    if not summary.project:
        raise ValueError('长期决定需要归属项目；请先明确当前会话的项目')
    if not isinstance(source, list) or not source or any(not isinstance(value, str) for value in source):
        raise ValueError('source 必须是用户原话 U# 的非空数组')
    identifiers = {entry['id'] for entry in summary.users}
    if any(identifier not in identifiers for identifier in source):
        raise ValueError('source 必须引用当前会话已有的用户原话 U#')
    if supersedes is not None and (not isinstance(supersedes, list) or
                                   any(not isinstance(value, str) for value in supersedes)):
        raise ValueError('supersedes 必须是当前项目 DL-xxxx 编号数组')
    written = service.store.append_decision_log(summary.project, text, why, '用户',
                                                summary.session_id, ', '.join(source), supersedes)
    saved = int(summary.header.get('saved_users') or 0)
    while f'U{saved + 1}' in source:
        saved += 1
    summary.header['saved_users'] = saved
    write_session(service, summary, path)
    return {'ok': True, 'written': written, 'current_time': _time_block(service)}


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
                     "goals": [], "target": "", "next_step": "", "status": "", "env": [],
                     "project_prompt": {}, "permissions": [], "acceptance": {},
                     "attempts": [], "confirms": [], "continues": [], "understanding": ""}
    observed: list[dict] = []
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
            if op in {"permission", "acceptance", "understanding", "confirm", "continue", "attempt"} and not progress.enabled(summary):
                raise ValueError("本会话未开启任务模式；设置中的开关仅影响新会话")
            if progress.is_paused(summary) and op not in {"confirm", "continue", "append", "replace"}:
                raise ValueError("已暂停。可以保存证据和理由；解除暂停需要用户原话里的新路线。")
            if op == "append":
                section_key = str(operation.get("section") or "").strip()
                section = SECTION_ALIASES.get(section_key)
                if not section:
                    raise ValueError("section 只允许 done/decision/dead_end/evidence/goal")
                items = operation.get("items")
                if not isinstance(items, list) or not items:
                    raise ValueError("items 必须是非空数组")
                if progress.is_paused(summary) and section not in {"证据", "决策", "未走通的路"}:
                    raise ValueError("已暂停。可以保存证据、走不通的路和决定理由，不能把实现写成已完成。")
                if section == "已完成" and progress.approval_block(service, summary):
                    raise ValueError(
                        "任务目标理解还没确认，不能把实现写成已完成。"
                        "先复述任务目标并等待用户确认，不要继续执行任务。"
                        "Tbrain 不能拦住宿主直接改文件。")
                for item in items:
                    item_start = len(rejected)
                    appended = _append_item(service, summary, section, item, rejected, warnings)
                    for error in rejected[item_start:]:
                        error["operation"] = {**operation, "items": [item]}
                    if appended:
                        written_key = {"已完成": "done", "决策": "decisions", "未走通的路": "dead_ends",
                                       "证据": "evidence", "未完成目标": "goals"}[section]
                        written[written_key].append(appended)
                        if section == "未走通的路":
                            observed.append({"kind": "dead_end", "productive": False,
                                             "text": str(item.get("attempt") or item.get("text") or "")})
                        elif section == "证据":
                            observed.append({"kind": "evidence", "productive": False,
                                             "text": str(item.get("text") or "")})
                        elif section == "已完成":
                            observed.append({"kind": "done", "productive": True,
                                             "text": str(item.get("text") or "")})
            elif op == "replace":
                section = str(operation.get("section") or "")
                text = str(operation.get("text") or "").strip()
                if progress.is_paused(summary) and section != "next_step":
                    raise ValueError("已暂停。可以记下下一步提议，不能改写目标。")
                if section in protected:
                    summary.conflicts.append(
                        f"- 用户手改过本会话总结，{section} 的模型更新未覆盖（{_utc_iso(service.now().utc)}），待用户确认")
                    rejected.append({"op": "replace", "section": section,
                                     "reason": "该会话总结已被用户手改，程序不覆盖；内容保留在“待用户确认”",
                                     "example": "请手动合并；确认恢复模型维护后，移除文件头 user_owned_fields 中的对应字段"})
                    continue
                if section == "target":
                    if progress.approval_block(service, summary):
                        raise ValueError("任务目标理解还没确认。先用 understanding 保存复述，用户确认后再用 confirm 记录。")
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
                    if progress.is_paused(summary):
                        stall = summary.header.setdefault("stall", {})
                        if isinstance(stall, dict):
                            stall["proposed_route"] = text
                else:
                    raise ValueError("replace.section 只允许 target / next_step")
            elif op == "set_status":
                status = str(operation.get("status") or "")
                if status not in handoff.STATUSES:
                    raise ValueError("status 只允许：" + " / ".join(handoff.STATUSES))
                if status in {"已中断", "已暂停"}:
                    raise ValueError(f"{status}只由程序写入")
                if progress.is_paused(summary):
                    raise ValueError("已暂停。要继续先 continue，不能直接改状态。")
                if status == "已完成" and progress.approval_block(service, summary):
                    raise ValueError(progress.approval_block(service, summary))
                goal_ids = operation.get('complete_goals') or []
                if not isinstance(goal_ids, list) or any(not isinstance(value, str) for value in goal_ids):
                    raise ValueError('complete_goals 必须是目标编号数组')
                completing = {value.upper() for value in goal_ids}
                unknown = completing - {item['id'] for item in summary.goals()}
                if unknown:
                    raise ValueError('目标编号不存在：' + '、'.join(sorted(unknown)))
                remaining = [item["id"] for item in summary.goals()
                             if not item.get("done") and item["id"] not in completing]
                if status == "已完成" and remaining:
                    raise ValueError("仍有未完成目标：" + "、".join(remaining) + "；完成后通过 complete_goals 指定编号")
                superseded = _supersede_decisions(service, summary, operation.get("supersede") or [])
                summary.header["status"] = status
                written["status"] = status
                done_ids = handoff.complete_goals(summary, operation.get("complete_goals") or [])
                if done_ids:
                    written["goals"].extend(done_ids)
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
            elif op == "permission":
                recorded = _record_permission(summary, operation)
                written["permissions"].append(recorded)
            elif op == "acceptance":
                written["acceptance"] = _save_acceptance(service, summary, operation)
            elif op == "understanding":
                written["understanding"] = progress.apply_understanding(summary, operation)
            elif op == "confirm":
                written["confirms"].append(progress.apply_confirm(service, summary, operation))
            elif op == "continue":
                written["continues"].append(progress.apply_continue(
                    service, summary, operation, now=_utc_iso(service.now().utc)))
            elif op == "attempt":
                normalized = progress.normalize_attempt(operation)
                written["attempts"].append(normalized)
                observed.append({
                    "kind": "attempt",
                    "productive": normalized["productive"],
                    "text": " ".join(dict.fromkeys(
                        part for part in (normalized["route"], normalized["evidence"]) if part)),
                })
            elif op == "project_prompt":
                if progress.approval_block(service, summary):
                    raise ValueError(
                        "任务目标理解还没确认，不能改项目提示词把实现当成已经批准。"
                        "Tbrain 不能拦住宿主直接改文件。")
                sections = operation.get("sections")
                if not summary.project:
                    raise ValueError("独立会话没有项目提示词")
                if not isinstance(sections, dict):
                    raise ValueError('project_prompt.sections 必须是对象')
                base = operation.get('base_sha', {})
                if not isinstance(base, dict) or any(not isinstance(value, str) for value in base.values()):
                    raise ValueError('project_prompt.base_sha 必须是章节名到sha字符串的对象')
                base = {name: base.get(name, '') for name in sections}
                result = service.store.save_project_prompt(summary.project, sections, summary=summary,
                                                           base_sha=base, session_id=summary.session_id)
                written["project_prompt"].update(result.get("written") or {})
                rejected.extend(result.get("rejected") or [])
                warnings.extend(result.get("warnings") or [])
            else:
                raise ValueError("op 只允许 append/replace/set_status/env/project_prompt/permission/acceptance/understanding/confirm/continue/attempt")
        except (ValueError, OSError) as exc:
            rejected.append({"op": op, "reason": str(exc),
                             "example": _op_example(op)})
        for error in rejected[reject_start:]:
            error.setdefault("operation", operation)

    now = service.now()
    now_iso = _utc_iso(now.utc)
    summary.header["updated_at"] = now_iso
    summary.header["last_activity_at"] = now_iso
    summary.header["git"] = service.git_info(str(summary.header.get("cwd") or ""))
    applied_verify = None
    if last_test is not None:
        if not isinstance(last_test, dict) or not isinstance(last_test.get("command"), str) or type(last_test.get("exit_code")) is not int:
            rejected.append({"op": "last_test", "operation": {"last_test": last_test},
                             "reason": "last_test 必须包含 command 和整数 exit_code"})
        elif summary.header.get("last_verify", {}).get("source") == "tool":
            if any(last_test[key] != summary.header["last_verify"].get(key) for key in ("command", "exit_code")):
                rejected.append({"op": "last_test", "operation": {"last_test": last_test},
                                 "reason": "last_test 不能覆盖宿主采集的测试结果；请省略补报并以实际执行结果为准。"})
        else:
            summary.header["last_verify"] = {"command": last_test["command"], "exit_code": last_test["exit_code"],
                                              "at": now_iso, "source": "model"}
            applied_verify = last_test
    progress.observe_saved(service, summary, ordered=observed, verify=applied_verify, now=now_iso)
    if not rejected:
        summary.header["saved_users"] = len(summary.users)
        summary.header["saved_verify_at"] = summary.header.get("last_verify", {}).get("at")
    pending_added = _record_rejections(service, summary, rejected)
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
        "env": '{"op":"env","items":[{"type":"安装","name":"jieba","version":"0.42.1","base_sha":"ab12cd34","state":"生效"}]}',
        "project_prompt": '{"op":"project_prompt","sections":{"当前阶段":"阶段名：完成标准"},"base_sha":{"当前阶段":"ab12cd34"}}',
        "permission": '{"op":"permission","action":"push branch"}',
        "understanding": '{"op":"understanding","goal":"用户需要的结果","scope":"本次范围","deliverables":"交付结果","done":"可核对的完成标准"}',
        "confirm": '{"op":"confirm","question":"Q1","source":"U2","why":"用户确认任务目标理解","tradeoffs":"先不扩大范围","decided_by":"用户"}',
        "continue": '{"op":"continue","route":"改为先补失败用例再改解析","why":"同一失败已重复","tradeoffs":"先不重写解析器","decided_by":"用户"}',
        "attempt": '{"op":"attempt","route":"pytest -q","evidence":"AssertionError test_parse","productive":false}',
        "acceptance": ('{"op":"acceptance","task":"本地检查已接上，这一轮可以提交",'
                       '"verification":"在项目目录运行 tbrain check，退出码 0。证据 E1。没有在 Windows 上安装",'
                       '"risks":"没有还要人接受的风险",'
                       '"deliverables":"commit 4c8fa05。在该目录运行 tbrain check","conclusion":"PASS"}'),
    }.get(op, "")


def _supersede_decisions(service, summary, references):
    if not isinstance(references, list) or any(not isinstance(ref, str) for ref in references):
        raise ValueError('supersede 必须是决策编号数组，可用 K1 或 [[会话名]]#K1')
    groups = {}
    for reference in references:
        match = re.fullmatch(r'(?:\[\[(.+?)\]\]#)?(K\d+)', reference.strip(), re.I)
        if not match:
            raise ValueError('supersede 使用 K1 或 [[会话名]]#K1')
        name, identifier = match[1], match[2].upper()
        if not name or name == summary.name:
            old, path = summary, None
        elif name in groups:
            old, path, _ = groups[name]
        else:
            rows = [row for row in service.index.sessions(summary.project) if row['name'] == name]
            if len(rows) != 1:
                raise ValueError(f'当前项目找不到唯一来源会话：{name}')
            old, path = load_session(service, rows[0]['session_id'])
        if identifier not in {item['id'] for item in old.all_items('决策')}:
            raise ValueError(f'决策编号不存在：{name or summary.name}#{identifier}')
        groups.setdefault(name or summary.name, (old, path, []))[2].append(identifier)
    changed = []
    for old, path, identifiers in groups.values():
        marked = handoff.supersede_decisions(old, identifiers)
        if path and marked:
            handoff.touch(old, _utc_iso(service.now().utc))
            write_session(service, old, path)
        changed.extend(f'[[{old.name}]]#{identifier}' if path else identifier for identifier in marked)
    return changed


def _save_acceptance(service, summary, operation: dict) -> dict:
    from .acceptance import apply_report, normalize

    fields = normalize(operation)
    blocked = progress.approval_block(service, summary)
    if blocked:
        raise ValueError(blocked + " 验收不能代替目标确认。")
    apply_report(summary, fields)
    return {"conclusion": fields["conclusion"], "windows_installable": fields["windows_installable"],
            "windows_verified": fields["windows_verified"]}


def _format_decision(item: dict) -> str:
    """Keep the first line as 决策 | 理由 and store why on the following lines."""
    text = str(item.get("text") or "").strip()
    decision = str(item.get("decision") or "").strip()
    reason = str(item.get("reason") or "").strip()
    rationale = str(item.get("rationale") or "").strip()
    tradeoffs = str(item.get("tradeoffs") or item.get("tradeoff") or "").strip()
    who = str(item.get("decided_by") or item.get("who") or "").strip()
    if "|" not in text and decision and (reason or rationale):
        text = f"{decision} | {reason or rationale}"
    lines = [text] if text else []
    if rationale and "理由：" not in text and "理由:" not in text and "Rationale:" not in text:
        lines.append(f"理由：{rationale}")
    if tradeoffs and "取舍：" not in text and "取舍:" not in text and "Tradeoffs:" not in text:
        lines.append(f"取舍：{tradeoffs}")
    if who and "决定人：" not in text and "决定人:" not in text and "Decided by:" not in text:
        lines.append(f"决定人：{who}")
    return "\n".join(line for line in lines if line).strip()


def _record_permission(summary, operation: dict) -> dict:
    grants = summary.header.get("permission_grants")
    if not isinstance(grants, list):
        grants = ["auto"]
    elif "auto" not in grants:
        grants = ["auto", *grants]
    grant = str(operation.get("grant") or "").strip()
    action = str(operation.get("action") or "").strip()
    if not grant and not action:
        raise ValueError("permission 需要 grant 或 action")
    if grant:
        if grant not in TIERS:
            raise ValueError("grant 只允许 auto / conditional / auth-required")
        if grant not in grants:
            grants.append(grant)
    tier = str(operation.get("tier") or "").strip()
    recorded = {"grant": grant}
    if action:
        if tier and tier not in TIERS:
            raise ValueError("tier 只允许 auto / conditional / auth-required")
        tier = tier or classify(action)
        actions = summary.header.get("permission_actions")
        if not isinstance(actions, list):
            actions = []
        entry = {"action": action, "tier": tier}
        actions.append(entry)
        summary.header["permission_actions"] = actions
        recorded.update(entry)
    summary.header["permission_grants"] = grants
    return recorded


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
    text = _format_decision(item) if section == "决策" else str(item.get("text") or "").strip()
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
    primary = text.split("\n", 1)[0] if section == "决策" else text
    if len(primary) > handoff.ITEM_MAX_CHARS or (section == "决策" and len(text) > 2000):
        limit = "全文不超过 2000 字" if section == "决策" and len(primary) <= handoff.ITEM_MAX_CHARS else f"正文连同证据引用不超过 {handoff.ITEM_MAX_CHARS} 字"
        rejected.append({"section": section, "reason": limit, "example": _op_example("append")})
        return ""
    for candidate in summary.all_items(section):
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
    meta = dict(item)
    base_sha = meta.pop('base_sha', '')
    if not isinstance(base_sha, str):
        rejected.append({'op': 'env', 'reason': 'base_sha 必须是sha字符串', 'example': _op_example('env')})
        return ''
    try:
        card = service.store.save_public_card(kind, str(item.get("name")).strip(), meta,
                                              source_session=f"[[{summary.name}]]", base_sha=base_sha,
                                              session_id=summary.session_id)
    except ValueError as exc:
        rejected.append({'op': 'env', 'reason': str(exc), 'example': _op_example('env')})
        return ''
    if card.get('reason'):
        rejected.append({'op': 'env', **card})
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
    projects, include_standalone = service.visible_projects_for_project(project)
    terms = [term.casefold() for term in service.index.bigram_terms(query)]
    decision_hits = []
    for name in service.decision_projects(project):
        for entry in reversed(service.store.project_decisions(name)):
            content = entry['text'] + '\n' + entry.get('why', '')
            if any(term in content.casefold() for term in terms):
                label = '' if entry['active'] else '[已被替代] '
                decision_hits.append({**entry, 'kind': 'decision', 'snippet': label + content})
    decision_hits.sort(key=lambda item: (item['active'], item['at']), reverse=True)
    decision_hits = decision_hits[:min(3, limit)]
    available = limit - len(decision_hits)
    current_id = row["session_id"] if row else ""
    project_hits = [{**item, "kind": "session", "source": "项目"}
                    for item in service.index.search_sessions(query, projects, limit + 1)
                    if item["session_id"] != current_id] if projects else []
    public_hits = [{**item, "kind": "public", "source": "公共池"}
                   for item in service.index.search_public(query, limit)]
    standalone_hits = [{**item, "kind": "session", "source": "独立会话池"}
                       for item in service.index.search_sessions(query, [""], limit + 1)
                       if item["session_id"] != current_id] if include_standalone else []
    if project:
        project_count = min(3, len(project_hits), available)
        public_count = min(1, len(public_hits), available - project_count)
        remaining = available - project_count - public_count
        extra = min(remaining, len(project_hits) - project_count)
        project_count += extra
        remaining -= extra
        public_count += min(remaining, len(public_hits) - public_count)
        remaining = available - project_count - public_count
        unique = decision_hits + project_hits[:project_count] + public_hits[:public_count] + standalone_hits[:min(1, remaining)]
    else:
        unique = (public_hits + standalone_hits)[:limit]
    for item in unique:
        if item['kind'] == 'decision':
            continue
        if item['kind'] == 'public':
            from .store import public_card_sha
            item['sha'] = public_card_sha(service.store.read_text(service.vault_root() / item['path']))
            continue
        summary = handoff.parse(service.store.read_text(service.vault_root() / item["path"]))
        candidates = [f"目标：{summary.target}", f"下一步：{summary.next_step}"]
        candidates.extend(f"{entry['id']} {entry['text']}" for entry in summary.users)
        for section in handoff.ITEM_PREFIXES:
            for entry in summary.all_items(section):
                label = "[历史记录，未生效或已失效] " if section == '决策' and not handoff.decision_active(entry['text']) else ""
                candidates.append(f"{label}{entry['id']} {entry['text']}")
        matches = sorted(candidates, key=lambda text: sum(term in text.casefold() for term in terms), reverse=True)
        item["snippet"] = _clip("\n".join(text for text in matches[:3] if any(term in text.casefold() for term in terms)), 500)
    payload = {"ok": True, "query": query, "results": unique, "current_time": _time_block(service)}
    return payload


def resume(service, session_id: str, target: str, *, fork_user_count=None, fork_revision: str = "", fork: bool = False) -> dict:
    row = _session_row(service, session_id)
    if not row:
        return {"ok": False, "error": "当前会话不存在；请先让 hook 立项或调用 bootstrap"}
    current, current_path = load_session(service, row)
    target = str(target or "").strip().strip("[]")
    if not target:
        return {"ok": False, "error": "请提供要接续的会话名或 session_id"}
    target_row = _session_row(service, target)
    projects, include_standalone = service.visible_projects_for_project(current.project)
    if not target_row:
        matches = [item for item in service.index.sessions() if item.get("name") == target
                   and (item.get("project") in projects or (not item.get("project") and include_standalone))]
        if len(matches) > 1:
            return {"ok": False, "error": "找到多个可见同名会话，请使用 session_id",
                    "candidates": [{key: item[key] for key in ("session_id", "name", "project")} for item in matches]}
        target_row = _session_row(service, matches[0]["session_id"]) if matches else None
    if not target_row:
        return {"ok": False, "error": f"找不到要接续的会话：{target}"}
    if target_row.get("project") not in projects and not (not target_row.get("project") and include_standalone):
        return {"ok": False, "error": "目标会话不在当前可见范围"}
    if target_row["session_id"] == current.session_id:
        return {"ok": False, "error": "不能接续当前会话本身"}
    old, _ = load_session(service, target_row)
    if old.project not in projects and not (not old.project and include_standalone):
        return {"ok": False, "error": "目标会话不在当前可见范围"}
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
        current.extra.pop("任务理解", None)
        current.extra.pop(progress.QUESTION_SECTION, None)
        for key in ("last_verify", "git", "dead_end_conditions", "user_owned_fields", "model_fingerprints", "stall"):
            if key in old.header and (key != "stall" or progress.enabled(current)):
                current.header[key] = copy.deepcopy(old.header[key])
        if progress.enabled(current) and progress.is_paused(old):
            current.header["status"] = "已暂停"
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
        items = old.active_decisions() if title == '决策' else old.all_items(title)
        lines += [f"### {title}"] + [f"{item['id']} {item['text']}" for item in items] + [""]
    for project in service.decision_projects(old.project):
        decisions = [entry for entry in service.store.project_decisions(project) if entry['active']]
        if decisions:
            lines += [f"### 项目长期决定：{project}"] + [f"{entry['id']} {entry['text']}" for entry in decisions] + [""]
    if old.extra.get("旧总结原文（迁移）"):
        lines += ["### 旧总结原文（迁移）", old.extra["旧总结原文（迁移）"], ""]
    acceptance_text = str(old.extra.get("最终验收") or "").strip()
    if acceptance_text:
        lines += ["### 最终验收", acceptance_text, ""]
    handoff_text = "\n".join(lines)
    if progress.enabled(current) and progress.is_paused(old):
        current.header["status"] = "已暂停"
        current.header["stall"] = copy.deepcopy(old.header.get("stall") or {})
        if old.extra.get(progress.HANDOFF_SECTION):
            current.extra[progress.HANDOFF_SECTION] = old.extra[progress.HANDOFF_SECTION]
    # 当前会话写“接续自”
    if current.name != old.name:
        current.header["resumed_from"] = f"[[{old.name}]]"
        if current.header.get("provisional_name"):
            date = service.now().local.strftime("%Y-%m-%d")
            seq = next_sequence(service, current.project, date)
            current_path = rename_session(service, current, current_path, naming.resume_name(old.name, date, seq))
        write_session(service, current, current_path)
    lead = progress.lead_block(service, current)
    contract = render_contract(service, current.project or old.project,
                               cwd=str(current.header.get("cwd") or old.header.get("cwd") or ""),
                               task_mode=progress.enabled(current))
    handoff_text = "\n\n".join(part for part in (lead, contract, handoff_text) if part)
    service.index.log_access(old.session_id, "resume")
    ranking = service.index.record_resume_feedback(current.session_id, current.project, old.session_id)
    return {"ok": True, "resumed_from": old.name, "session_id": current.session_id,
            "resumed_session_id": old.session_id,
            "handoff": handoff_text, "current_time": _time_block(service), "ranking": ranking}


def bootstrap(service, session_id: str, agent: str, cwd: str, host_record: str = "",
              project: str | None = None) -> dict:
    service.refresh_index()
    timeout_fallback(service)
    host, host_session_id = identity.split_session_id(session_id) or (identity.normalize_host(agent or "unknown"), session_id)
    existing = _load_or_none(service, host, host_session_id)
    if existing[0] is None:
        if project and not service.store.project(project):
            return {"ok": False, "error": "项目不存在"}
        summary, path = create_summary(service, host=host, host_session_id=host_session_id, cwd=cwd,
                                       host_record=host_record, first_message="",
                                       project_override=project)
        old_project = summary.project
    else:
        summary, path = existing
        old_project = summary.project
        _mark_active(service, summary, path)
        if project is not None and project != "":
            if service.store.project(project):
                summary.header["project"] = project
                summary.header["project_link"] = f"[[{project}]]"
    write_session(service, summary, path)
    if old_project != summary.project:
        service.sync_session_index(old_project)
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
