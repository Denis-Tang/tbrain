"""Dream L1：同一知识库里的冷蒸馏。

热交接（L0）仍由 bootstrap / save / resume / search 写入 ``会话总结/``。
本模块只做三件本地的事，不调用模型，也不依赖外部记忆服务：

1. 把 ``.tbrain/dream-status.json`` 标成 ``pending`` / ``running`` / ``ready``。
2. 在 ``.tbrain/dream/`` 写 wiki（``_index.md`` + 主题文件）。
3. 当且仅当状态为 ``ready`` 时，给启动包拼一段有字符预算的索引行。

显式 ``ops`` 由外部分析器（Reason / Codex / Claude 等）事先写好，这里只按
结构落地。会话总结正文不会被删除或改写。
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import handoff, naming
from .store import render_note, split_front_matter

SCHEMA = 1
STATUS_FILE = "dream-status.json"
DREAM_DIRNAME = "dream"
SNIPPET_BUDGET = 480
SNIPPET_MAX_LINES = 8
SNIPPET_MIN_ROOM = 64
LOCK_STALE_SECONDS = 15 * 60
FINISHED_STATUSES = ("已完成", "已中断", "已放弃")
OPS = ("upsert", "retire", "digest_pending")

_SECRET_RE = re.compile(
    r"(?i)(?:sk-[A-Za-z0-9_\-]{8,}|ghp_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}"
    r"|(?:api[_-]?key|access[_-]?token|token|password|secret)\s*[:=]\s*\S+)"
)
_SLUG_RE = re.compile(r"^[^\\/:*?\"<>|\s.][^\\/:*?\"<>|\s]*$")
_STUB_RE = re.compile(r"^MOVED TO \[\[[^\]\n]+\]\]\s*$")


class DreamError(ValueError):
    """调用方可以修正的 Dream 错误（操作不合法、知识库不可用）。"""


class DreamBusy(DreamError):
    """已有一次 Dream 在写盘；热路径不应等待它。"""


def status_path(vault: Path) -> Path:
    return Path(vault) / ".tbrain" / STATUS_FILE


def dream_dir(vault: Path) -> Path:
    return Path(vault) / ".tbrain" / DREAM_DIRNAME


def read_status(vault: Path) -> dict:
    """读路由文件。缺失、损坏或未知 state 都当成 ``pending``（启动包不附加 L1）。"""
    path = status_path(vault)
    base = {
        "schema": SCHEMA,
        "state": "pending",
        "present": False,
        "updated_at": "",
        "run_id": "",
        "selected": 0,
        "index": ".tbrain/dream/_index.md",
        "error": "",
    }
    if not path.is_file():
        return base
    base["present"] = True
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        base["error"] = "dream-status.json 无法读取"
        return base
    if not isinstance(payload, dict):
        base["error"] = "dream-status.json 不是对象"
        return base
    state = str(payload.get("state") or "")
    if state not in ("pending", "running", "ready"):
        base["error"] = "未知 state"
        state = "pending"
    else:
        base["error"] = str(payload.get("error") or "")
    try:
        selected = int(payload.get("selected") or 0)
    except (TypeError, ValueError):
        selected = 0
    base.update(
        state=state,
        updated_at=str(payload.get("updated_at") or ""),
        run_id=str(payload.get("run_id") or ""),
        selected=selected,
    )
    return base


def status_view(vault: Path) -> dict:
    data = read_status(vault)
    data.update(
        ok=True,
        snippet_budget=SNIPPET_BUDGET,
        status_path=".tbrain/dream-status.json",
        dream_dir=".tbrain/dream/",
    )
    return data


def write_status(vault: Path, *, state: str, updated_at: str, run_id: str = "",
                 selected: int = 0, error: str = "") -> None:
    if state not in ("pending", "running", "ready"):
        raise DreamError(f"非法 state：{state}")
    payload = {
        "schema": SCHEMA,
        "state": state,
        "updated_at": updated_at,
        "run_id": run_id,
        "selected": int(selected),
        "index": ".tbrain/dream/_index.md",
        "error": error,
    }
    _atomic_write(status_path(vault), json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def clip_snippet(text: str, budget: int) -> str:
    """按字符预算从末尾丢掉整行；结果长度不超过 budget。"""
    body = str(text or "").strip()
    if budget <= 0 or not body:
        return ""
    if len(body) <= budget:
        return body
    kept: list[str] = []
    used = 0
    for line in body.splitlines():
        extra = len(line) if not kept else len(line) + 1
        if used + extra > budget:
            break
        kept.append(line)
        used += extra
    if not kept:
        if budget == 1:
            return body[:1]
        return body[: budget - 1] + "…"
    clipped = "\n".join(kept)
    if len(clipped) == len(body):
        return clipped
    marker = "…"
    if used + 1 + len(marker) <= budget:
        return clipped + "\n" + marker
    if len(clipped) > budget:
        return clipped[: budget - 1] + "…" if budget > 1 else clipped[:budget]
    return clipped


def bootstrap_snippet(vault: Path, *, room: int) -> str:
    """启动包后缀。非 ready、没有索引行、或剩余空间不够时返回空字符串。"""
    if room < SNIPPET_MIN_ROOM:
        return ""
    if read_status(vault).get("state") != "ready":
        return ""
    index = dream_dir(vault) / "_index.md"
    if not index.is_file():
        return ""
    try:
        text = index.read_text(encoding="utf-8")
    except OSError:
        return ""
    bullets = [line for line in text.splitlines() if line.startswith("- [[")]
    if not bullets:
        return ""
    lines = ["## Dream L1", "索引 .tbrain/dream/_index.md（细节不在启动包内）"]
    lines.extend(bullets[:SNIPPET_MAX_LINES])
    budget = min(SNIPPET_BUDGET, room - 2)
    return clip_snippet("\n".join(lines), budget)


def search_topics(vault: Path, query: str, limit: int = 3) -> list[dict]:
    """在已 ready 的主题文件里做子串匹配。不读模型，也不改启动包。"""
    if read_status(vault).get("state") != "ready":
        return []
    needle = str(query or "").strip().casefold()
    if not needle:
        return []
    root = dream_dir(vault)
    if not root.is_dir():
        return []
    hits: list[dict] = []
    for path in sorted(root.glob("*.md")):
        if path.name.startswith("_"):
            continue
        try:
            data, body = split_front_matter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if _is_stub(body):
            continue
        description = str(data.get("description") or "")
        if needle not in body.casefold() and needle not in description.casefold():
            continue
        hits.append({
            "kind": "dream",
            "slug": path.stem,
            "path": f".tbrain/dream/{path.name}",
            "snippet": _matching_line(body, needle)[:500],
        })
        if len(hits) >= max(1, limit):
            break
    return hits


def resume_pointer(vault: Path, session_id: str) -> str:
    """被冷蒸馏过的会话，在 resume 交接包末尾加一行指针。"""
    if read_status(vault).get("state") != "ready":
        return ""
    manifest = _read_json(dream_dir(vault) / "_manifest.json")
    sessions = manifest.get("sessions") if isinstance(manifest, dict) else None
    if not isinstance(sessions, list):
        return ""
    if not any(isinstance(item, dict) and item.get("session_id") == session_id for item in sessions):
        return ""
    return "### Dream L1\n该会话已冷蒸馏，详见 .tbrain/dream/_index.md。上文交接包仍是热记忆。"


def run(service, *, ops_path: str = "", project: str = "") -> dict:
    """选出已结束会话，写 L1 wiki，最后把状态改成 ready。不改写 L0 正文。"""
    vault = Path(service.vault_root())
    if not str(vault) or not vault.is_dir():
        raise DreamError("请先在设置中选择知识库路径")
    _acquire(vault)
    try:
        return _run_locked(service, vault, ops_path=ops_path, project=str(project or "").strip())
    finally:
        _release(vault)


def _run_locked(service, vault: Path, *, ops_path: str, project: str) -> dict:
    previous = _status_bytes(vault)
    stamp = _stamp(service)
    run_id = stamp.replace("-", "").replace(":", "")
    mutated = False
    try:
        ops = _resolve_ops(vault, ops_path)
        write_status(vault, state="running", updated_at=stamp, run_id=run_id, error="")
        mutated = True
        selected, skipped_open, unreadable = _collect(service, project)
        _write_wiki(vault, selected, project=project, stamp=stamp, ops=ops)
        topics = _index_slugs(vault)
        write_status(
            vault, state="ready", updated_at=stamp, run_id=run_id,
            selected=len(selected), error="",
        )
        return {
            "ok": True,
            "state": "ready",
            "run_id": run_id,
            "selected": len(selected),
            "skipped_open": skipped_open,
            "unreadable": unreadable,
            "topics": topics,
            "ops_applied": len(ops),
            "index": ".tbrain/dream/_index.md",
            "status_path": ".tbrain/dream-status.json",
            "error": "",
        }
    except Exception as exc:
        if not mutated and previous is not None:
            _restore_status(vault, previous)
        else:
            message = str(exc) or exc.__class__.__name__
            try:
                write_status(vault, state="pending", updated_at=_stamp(service), run_id=run_id, error=message)
            except Exception:
                pass
        if isinstance(exc, DreamError):
            raise
        raise DreamError(str(exc) or exc.__class__.__name__) from exc


def _collect(service, project: str) -> tuple[list[dict], int, int]:
    service.refresh_index()
    chosen: list[dict] = []
    skipped_open = 0
    unreadable = 0
    rows = service.index.sessions()
    rows.sort(key=lambda row: (str(row.get("updated_at") or ""), str(row.get("session_id") or "")))
    for row in rows:
        name = str(row.get("project") or "")
        if project and name != project:
            continue
        status = str(row.get("status") or "")
        if status not in FINISHED_STATUSES:
            skipped_open += 1
            continue
        summary = _load_summary(service, row)
        if summary is None:
            unreadable += 1
            continue
        chosen.append({
            "session_id": summary.session_id or str(row.get("session_id") or ""),
            "name": summary.name or str(row.get("name") or ""),
            "project": summary.project or name,
            "status": summary.status or status,
            "path": str(row.get("path") or ""),
            "updated": str(summary.header.get("updated_at") or row.get("updated_at") or "")[:10],
            "summary": summary,
        })
    return chosen, skipped_open, unreadable


def _write_wiki(vault: Path, selected: list[dict], *, project: str, stamp: str, ops: list[dict]) -> None:
    root = dream_dir(vault)
    root.mkdir(parents=True, exist_ok=True)
    grouped: dict[str, list[dict]] = {}
    order: list[str] = []
    pending_lines: list[str] = []
    for item in selected:
        key = (item["project"] or "").strip() or "独立"
        grouped.setdefault(key, []).append(item)
        if key not in order:
            order.append(key)
        pending_lines.extend(_pending_lines(item))
    written: set[str] = set()
    for key in order:
        slug, text = _render_project(key, grouped[key], stamp)
        if text is None:
            continue
        _atomic_write(root / f"{slug}.md", text)
        written.add(slug)
    if pending_lines or (project and (root / "pending.md").is_file()):
        pending_text = _render_pending(vault, pending_lines, stamp, project=project, selected=selected)
        if pending_text:
            _atomic_write(root / "pending.md", pending_text)
            written.add("pending")
    _purge_local(vault, written, only_slugs=_filter_slugs(project, written) if project else None)
    touched: set[str] = set()
    for op in ops:
        _apply_op(vault, op, stamp)
        touched.update(_op_slugs(op))
    _purge_ops(vault, touched)
    _write_manifest(vault, selected, stamp)
    _write_log(vault, selected, stamp)
    _atomic_write(
        root / "_ops.json",
        json.dumps({"schema": SCHEMA, "ops": ops}, ensure_ascii=False, indent=2) + "\n",
    )
    _write_index(vault, stamp)


def _render_project(project: str, items: list[dict], stamp: str) -> tuple[str, str | None]:
    slug = _project_slug(project)
    current: list[str] = []
    retired: list[str] = []
    goals: list[str] = []
    dead: list[str] = []
    targets: list[str] = []
    for item in items:
        summary = item["summary"]
        cite = f"[[{item['name']}]]" if item["name"] else item["session_id"]
        when = item["updated"]
        target = _redact(_one_line(summary.target))
        if target:
            targets.append(f"- {target}（来源 {cite}，{item['status']}，{when}）")
        for decision in summary.all_items("决策"):
            text = _redact(_one_line(decision.get("text") or ""))
            if not text:
                continue
            line = (f"- {decision['id']} {text}"
                    f"（来源 {cite}#{decision['id']}，{item['status']}，{when}）")
            if handoff.decision_active(decision.get("text") or ""):
                current.append(line)
            else:
                retired.append(line)
        if item["status"] == "已中断":
            for goal in summary.goals():
                if goal.get("done"):
                    continue
                text = _redact(_one_line(goal.get("text") or ""))
                if text:
                    goals.append(f"- {goal['id']} {text}（来源 {cite}#{goal['id']}，已中断，{when}）")
        for stopped in summary.all_items("未走通的路"):
            text = _redact(_one_line(stopped.get("text") or ""))
            if text:
                dead.append(f"- {stopped['id']} {text}（来源 {cite}#{stopped['id']}，{when}）")
    if not any((current, retired, goals, dead, targets)):
        return slug, None
    parts = ["## 目标摘录", *targets, ""] if targets else []
    parts += ["## 仍有效", *(current or ["（无）"]), ""]
    if retired:
        parts += ["## 已退役", "冷层记录。原会话总结未改写，热交接仍忽略明确失效的决定。", *retired, ""]
    if goals:
        parts += ["## 中断时未完成", "热交接仍以该会话的 resume 为准。", *goals, ""]
    if dead:
        parts += ["## 未走通", *dead, ""]
    description = f"{project}：{len(current)} 条仍有效决定，{len(retired)} 条已在冷层退役"
    meta = {
        "name": slug,
        "description": description,
        "generator": "local",
        "updated_at": stamp,
        "sources": [f"[[{item['name']}]]" for item in items if item["name"]],
    }
    return slug, render_note(meta, "\n".join(parts).strip())


def _pending_lines(item: dict) -> list[str]:
    lines = []
    cite = f"[[{item['name']}]]" if item["name"] else item["session_id"]
    for raw in item["summary"].pending:
        text = _redact(_one_line(raw))
        if text.startswith("- "):
            text = text[2:].strip()
        if text:
            lines.append(f"- {cite} {text}")
    return lines


def _render_pending(vault: Path, lines: list[str], stamp: str, *, project: str, selected: list[dict]) -> str:
    if project:
        path = dream_dir(vault) / "pending.md"
        kept: list[str] = []
        if path.is_file():
            try:
                _meta, body = split_front_matter(path.read_text(encoding="utf-8"))
            except OSError:
                body = ""
            names = {f"[[{item['name']}]]" for item in selected if item["name"]}
            for line in body.splitlines():
                if line.startswith("- ") and any(name in line for name in names):
                    continue
                if line.startswith("#") or not line.strip():
                    continue
                if line.startswith("- "):
                    kept.append(line)
        lines = kept + lines
    if not lines:
        return ""
    body = "\n".join([
        "## 未校验",
        "这些文字来自会话「待整理」，尚未经过校验，不能当作规则。",
        *lines,
    ])
    meta = {
        "name": "pending",
        "description": f"待整理 {len(lines)} 条，仍未校验",
        "generator": "local",
        "updated_at": stamp,
    }
    return render_note(meta, body)


def _apply_op(vault: Path, op: dict, stamp: str) -> None:
    kind = str(op.get("op") or "")
    if kind == "upsert":
        _apply_upsert(vault, op, stamp)
    elif kind == "retire":
        _apply_retire(vault, op, stamp)
    elif kind == "digest_pending":
        _apply_digest(vault, op, stamp)
    else:
        raise DreamError(f"未知操作：{kind}")


def _apply_upsert(vault: Path, op: dict, stamp: str) -> None:
    slug = _check_slug(op.get("slug"))
    description = _one_line(op.get("description") or slug)[:200]
    body = _redact(str(op.get("body") or "").strip())
    sources = op.get("sources") or []
    if isinstance(sources, str):
        sources = [sources]
    if not isinstance(sources, list):
        raise DreamError(f"upsert {slug} 的 sources 必须是数组")
    meta = {
        "name": slug,
        "description": description,
        "generator": "ops",
        "updated_at": stamp,
        "sources": [str(item) for item in sources],
    }
    _atomic_write(dream_dir(vault) / f"{slug}.md", render_note(meta, body))


def _apply_retire(vault: Path, op: dict, stamp: str) -> None:
    slug = _check_slug(op.get("slug"))
    path = dream_dir(vault) / f"{slug}.md"
    if not path.is_file():
        raise DreamError(f"retire 找不到主题：{slug}")
    replaced = str(op.get("replaced_by") or "").strip()
    note = _redact(_one_line(op.get("note") or ""))
    if replaced:
        replaced = _check_slug(replaced)
        body = f"MOVED TO [[{replaced}]]"
        description = "已退役"
    else:
        body = f"（已退役）{note}".strip()
        description = f"已退役：{note}" if note else "已退役"
    meta = {
        "name": slug,
        "description": description[:200],
        "generator": "ops",
        "updated_at": stamp,
        "metadata": {"note": note, "replaced_by": replaced},
    }
    _atomic_write(path, render_note(meta, body))


def _apply_digest(vault: Path, op: dict, stamp: str) -> None:
    slug = _check_slug(op.get("into"))
    text = _redact(_one_line(op.get("text") or ""))
    if not text:
        raise DreamError("digest_pending 缺少 text")
    pending_path = dream_dir(vault) / "pending.md"
    if pending_path.is_file():
        data, body = split_front_matter(pending_path.read_text(encoding="utf-8"))
        kept = [line for line in body.splitlines() if text not in line]
        data["updated_at"] = stamp
        data.setdefault("generator", "local")
        remaining = [line for line in kept if line.startswith("- ")]
        if remaining:
            data["description"] = f"待整理 {len(remaining)} 条，仍未校验"
            _atomic_write(pending_path, render_note(data, "\n".join(kept).strip()))
        elif str(data.get("generator") or "local") == "local":
            pending_path.unlink()
    topic = dream_dir(vault) / f"{slug}.md"
    addition = f"## 已消化的待整理\n- {text}（由显式操作并入，原会话待整理未删除）"
    if topic.is_file():
        data, body = split_front_matter(topic.read_text(encoding="utf-8"))
        if text not in body:
            body = body.rstrip() + "\n\n" + addition
        data["updated_at"] = stamp
        _atomic_write(topic, render_note(data, body))
    else:
        meta = {
            "name": slug,
            "description": _one_line(op.get("description") or "由待整理并入的事实")[:200],
            "generator": "ops",
            "updated_at": stamp,
        }
        _atomic_write(topic, render_note(meta, addition))


def _write_index(vault: Path, stamp: str) -> None:
    root = dream_dir(vault)
    grouped = {"项目": [], "待整理": [], "主题": []}
    for path in sorted(root.glob("*.md")):
        if path.name.startswith("_"):
            continue
        try:
            data, body = split_front_matter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if _is_stub(body):
            continue
        slug = path.stem
        description = _one_line(data.get("description") or slug)
        grouped[_domain(slug)].append(f"- [[{slug}]] — {description}")
    lines = ["# Dream L1", "", "冷蒸馏目录。热交接仍以会话总结为准；本目录不回写那些文件。", ""]
    for domain in ("项目", "待整理", "主题"):
        if not grouped[domain]:
            continue
        lines += [f"## {domain}", "", *grouped[domain], ""]
    if not any(grouped.values()):
        lines.append("（暂无主题）")
    meta = {
        "name": "_index",
        "description": "Dream L1 目录。启动包只引用其中的索引行。",
        "generator": "local",
        "updated_at": stamp,
    }
    _atomic_write(root / "_index.md", render_note(meta, "\n".join(lines).strip()))


def _write_manifest(vault: Path, selected: list[dict], stamp: str) -> None:
    payload = {
        "schema": SCHEMA,
        "updated_at": stamp,
        "sessions": [
            {
                "session_id": item["session_id"],
                "name": item["name"],
                "project": item["project"],
                "status": item["status"],
                "path": item["path"],
            }
            for item in selected
        ],
    }
    _atomic_write(
        dream_dir(vault) / "_manifest.json",
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


def _write_log(vault: Path, selected: list[dict], stamp: str) -> None:
    lines = [
        "# Dream 运行记录",
        "",
        "本文件只作指针。会话总结正文没有在这次运行里被删除或改写。",
        "",
        f"- at: {stamp}",
        f"- selected: {len(selected)}",
    ]
    for item in selected:
        lines.append(f"- [[{item['name']}]]（{item['status']}）`{item['path']}`")
    meta = {"name": "_log", "description": "最近一次 Dream 指针", "generator": "local", "updated_at": stamp}
    _atomic_write(dream_dir(vault) / "_log.md", render_note(meta, "\n".join(lines)))


def _resolve_ops(vault: Path, ops_path: str) -> list[dict]:
    if ops_path:
        file = Path(ops_path).expanduser()
        if not file.is_file():
            raise DreamError(f"找不到操作文件：{file}")
        try:
            raw = json.loads(file.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise DreamError(f"操作文件不是 JSON：{file}") from exc
        ops = _ops_list(raw)
    else:
        stored = _read_json(dream_dir(vault) / "_ops.json")
        ops = _ops_list(stored) if stored else []
    _validate_ops(ops)
    return ops


def _ops_list(raw) -> list[dict]:
    if raw is None:
        return []
    if isinstance(raw, list):
        ops = raw
    elif isinstance(raw, dict):
        ops = raw.get("ops") if "ops" in raw else []
    else:
        raise DreamError("ops 须为对象或数组")
    if not isinstance(ops, list):
        raise DreamError("ops 须为数组")
    return ops


def _validate_ops(ops: list[dict]) -> None:
    for index, op in enumerate(ops, 1):
        if not isinstance(op, dict):
            raise DreamError(f"第 {index} 条操作不是对象")
        kind = str(op.get("op") or "")
        if kind not in OPS:
            raise DreamError(f"第 {index} 条操作未知：{kind or '（空）'}")
        if kind == "upsert":
            _check_slug(op.get("slug"))
            if not str(op.get("description") or "").strip():
                raise DreamError(f"第 {index} 条 upsert 缺少 description")
            if not str(op.get("body") or "").strip():
                raise DreamError(f"第 {index} 条 upsert 缺少 body")
        elif kind == "retire":
            _check_slug(op.get("slug"))
            if str(op.get("replaced_by") or "").strip():
                _check_slug(op.get("replaced_by"))
        elif kind == "digest_pending":
            _check_slug(op.get("into"))
            if not str(op.get("text") or "").strip():
                raise DreamError(f"第 {index} 条 digest_pending 缺少 text")


def _purge_local(vault: Path, keep: set[str], *, only_slugs: set[str] | None) -> None:
    root = dream_dir(vault)
    if not root.is_dir():
        return
    for path in root.glob("*.md"):
        if path.name.startswith("_"):
            continue
        slug = path.stem
        if only_slugs is not None and slug not in only_slugs:
            continue
        if slug in keep:
            continue
        try:
            data, _body = split_front_matter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if str(data.get("generator") or "local") == "local":
            path.unlink()


def _filter_slugs(project: str, written: set[str]) -> set[str]:
    """限定清理范围：本次项目文件、待整理，以及这次确实写下的本地文件。"""
    return {_project_slug(project), "pending", *written}


def _op_slugs(op: dict) -> set[str]:
    kind = str(op.get("op") or "")
    if kind == "upsert":
        return {_check_slug(op.get("slug"))}
    if kind == "retire":
        return {_check_slug(op.get("slug"))}
    if kind == "digest_pending":
        return {_check_slug(op.get("into"))}
    return set()


def _purge_ops(vault: Path, keep: set[str]) -> None:
    """`--ops` 会替换上一份操作。不再被这些操作拥有的主题文件删掉。"""
    root = dream_dir(vault)
    if not root.is_dir():
        return
    for path in root.glob("*.md"):
        if path.name.startswith("_") or path.stem in keep:
            continue
        try:
            data, _body = split_front_matter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if str(data.get("generator") or "") == "ops":
            path.unlink()


def _index_slugs(vault: Path) -> list[str]:
    index = dream_dir(vault) / "_index.md"
    if not index.is_file():
        return []
    try:
        text = index.read_text(encoding="utf-8")
    except OSError:
        return []
    found = []
    for line in text.splitlines():
        if not line.startswith("- [["):
            continue
        end = line.find("]]")
        if end > 4:
            found.append(line[4:end])
    return found


def _load_summary(service, row: dict):
    raw = Path(str(row.get("path") or ""))
    path = raw if raw.is_absolute() else Path(service.vault_root()) / raw
    if not path.is_file():
        return None
    try:
        return handoff.parse(service.store.read_text(path))
    except Exception:
        return None


def _project_slug(project: str) -> str:
    label = (project or "").strip() or "独立"
    return "proj-" + naming.sanitize_filename(label)


def _domain(slug: str) -> str:
    if slug == "pending":
        return "待整理"
    if slug.startswith("proj-"):
        return "项目"
    return "主题"


def _check_slug(value) -> str:
    slug = str(value or "").strip()
    if not slug or slug.startswith((".", "_")) or not _SLUG_RE.match(slug) or ".." in slug:
        raise DreamError(f"非法主题名：{slug or '（空）'}")
    if slug in {"_index", "_log", "_manifest", "_ops"}:
        raise DreamError(f"非法主题名：{slug}")
    return slug


def _is_stub(body: str) -> bool:
    return bool(_STUB_RE.match(str(body or "").strip()))


def _one_line(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _redact(text: str) -> str:
    return _SECRET_RE.sub("[redacted]", text)


def _matching_line(body: str, needle: str) -> str:
    for line in body.splitlines():
        if needle in line.casefold():
            return line.strip()
    stripped = body.strip()
    return stripped.splitlines()[0] if stripped else ""


def _stamp(service) -> str:
    from datetime import datetime, timezone

    try:
        moment = service.now().utc.astimezone(timezone.utc)
    except Exception:
        moment = datetime.now(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_json(path: Path):
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _status_bytes(vault: Path) -> bytes | None:
    path = status_path(vault)
    try:
        return path.read_bytes() if path.is_file() else None
    except OSError:
        return None


def _restore_status(vault: Path, payload: bytes) -> None:
    path = status_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def _acquire(vault: Path) -> None:
    root = Path(vault) / ".tbrain"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "dream.lock"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            age = 0
        if age < LOCK_STALE_SECONDS:
            raise DreamBusy("Dream 正在运行，启动包不会等待它")
        try:
            path.unlink()
        except OSError as exc:
            raise DreamBusy("Dream 正在运行，启动包不会等待它") from exc
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    try:
        os.write(fd, f"{os.getpid()}\n".encode())
    finally:
        os.close(fd)


def _release(vault: Path) -> None:
    path = Path(vault) / ".tbrain" / "dream.lock"
    try:
        path.unlink()
    except OSError:
        pass
