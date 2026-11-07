"""Goal clarification, no-progress pause, and the L0 hot handoff.

This sits on the existing bootstrap / save / resume path. It does not budget
tokens, money, or wall time, and it does not own the external agent process.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import handoff, naming
from .check import find_config
from .store import COMPLETION_FILE, render_note, split_front_matter

HANDOFF_SECTION = "热交接"
QUESTION_SECTION = "待确认"
CONFIRMED_SECTION = "已确认决定"
QUESTION_MARKER = "<!-- tbrain:questions -->"
DEFAULT_UNPRODUCTIVE = 3
MAX_UNPRODUCTIVE = 20
LOG_LIMIT = 40
HANDOFF_PACKAGE_LIMIT = 3500

_STALL_KEYS = {
    "policy", "consecutive", "last_signature", "last_text", "paused", "reason",
    "paused_at", "log", "cleared_by",
}
_PERSONAL_PATH = re.compile(
    r"[A-Za-z]:[\\/]Users[\\/][^\\/\s]+(?:[\\/][^\s\"']*)?"
    r"|/(?:home|Users)/[^/\s]+(?:/[^\s\"']*)?",
    re.IGNORECASE,
)
_SPECIFIC = re.compile(
    r"\b(test_[A-Za-z0-9_]+|[A-Za-z_][A-Za-z0-9_]*(?:Error|Exception)"
    r"|[A-Za-z0-9_./\\-]+\.(?:py|ts|tsx|js|md|yaml|yml))\b"
)
_FILLER = (
    "失败", "报错", "仍然", "还是", "再次", "同样", "没有过", "没过",
    "failed", "failure", "error", "again", "still", "same",
)
_GOAL_PROMPT = "这个项目要做成什么样？"
_DONE_PROMPT = "怎样才算做完，并且可以核对？"
_TASK_PROMPT = "这次要做到什么才算完成？"
_DONE_DEFAULT = "本地 tbrain check 退出码为 0，完成标准里的每一条都能用证据核对。"
_GOAL_DEFAULT = "做成可交接的本地结果：写清给谁用，做完可以离开。"
_TASK_DEFAULT = "做成一件可以核对的事：写清做完的标准，并用真实证据证明。"


def is_paused(summary) -> bool:
    stall = _stall(summary)
    return bool(stall.get("paused")) or str(summary.header.get("status") or "") == "已暂停"


def approval_block(service, summary) -> str:
    """Why this session must not be marked done or PASS. Empty means it may."""
    if is_paused(summary):
        reason = str(_stall(summary).get("reason") or "连续没有新进展")
        return f"会话已暂停：{reason}。不能写成已完成或 PASS。"
    open_items = open_questions(service, summary)
    if open_items:
        names = "、".join(item["id"] for item in open_items)
        return f"目标或完成标准还没确认（{names}）。不能写成已完成或 PASS。"
    return ""


def gate_gaps(summary) -> list[str]:
    """Wrap-up gaps carried on the session itself."""
    gaps = []
    stall = _stall(summary)
    if is_paused(summary):
        reason = str(stall.get("reason") or "连续没有新进展")
        gaps.append(f"会话已暂停：{reason}。先用 continue 写明新路线和为什么，不能收尾。")
    open_items = [item for item in _header_questions(summary) if item.get("status") == "open"]
    if open_items:
        names = "、".join(item["id"] for item in open_items)
        gaps.append(f"目标或完成标准还没确认（{names}）。不能当作已完成或 PASS。")
    return gaps


def sync(service, summary, task: str = "") -> bool:
    """Persist unresolved product questions once. Return False if that write failed.

    A failed write leaves the previous file in place and still treats the goal
    as unresolved. Clear goals already on disk are reused and not asked again.
    """
    task_text = str(task or summary.raw_command or "").strip()
    if summary.project:
        return _sync_project(service, summary, task_text)
    return _sync_independent(summary, task_text)


def open_questions(service, summary) -> list[dict]:
    if summary.project and service is not None:
        text = _read_completion(service, summary.project)
        if text:
            front, sections = _parse_completion(text)
            stored = _questions_from_front(front)
            if stored:
                return [item for item in stored if item.get("status") == "open"]
            if _needs_questions(sections):
                return _new_questions(summary.raw_command, sections)
            return []
    return [item for item in _header_questions(summary) if item.get("status") == "open"]


def lead_block(service, summary) -> str:
    """Text bootstrap and resume put first. Empty when nothing is pending."""
    if is_paused(summary):
        stored = str((summary.extra or {}).get(HANDOFF_SECTION) or "").strip()
        body = stored or render_hot_handoff(service, summary, reason=str(_stall(summary).get("reason") or ""))
        notice = pause_instruction(summary)
        return _clip(notice + "\n\n## " + HANDOFF_SECTION + "\n" + body.strip(), HANDOFF_PACKAGE_LIMIT)
    items = open_questions(service, summary)
    if not items:
        return ""
    return render_questions(items, heading="待确认的目标和完成标准")


def turn_notice(service, summary) -> str:
    if is_paused(summary):
        return pause_instruction(summary)
    items = open_questions(service, summary)
    if not items:
        return ""
    return render_questions(items, heading="待确认的目标和完成标准")


def pause_instruction(summary) -> str:
    reason = str(_stall(summary).get("reason") or "连续没有新进展")
    return (
        f"已暂停：{reason}\n"
        "不要再自主实现，也不要把同一条失败换个说法再试。\n"
        "Tbrain 会拒绝已完成和 PASS，并让收尾失败。Stop 不会再要求模型继续这一轮。\n"
        "Tbrain 不拥有外部 Agent 进程，不能把那个进程杀掉。\n"
        "要继续，调用 save 的 continue，写明新路线、理由、取舍和决定人。"
    )


def contract_extra(service, project: str) -> str:
    """Read-only addition to the work contract. Never writes."""
    if not project or service is None:
        return ""
    try:
        text = _read_completion(service, project)
    except Exception:
        return ""
    if not text:
        return ""
    front, sections = _parse_completion(text)
    stored = _questions_from_front(front)
    open_items = [item for item in stored if item.get("status") == "open"]
    if open_items:
        return "\n" + render_questions(open_items, heading="待确认的目标和完成标准")
    if stored and not open_items:
        return "\n目标和完成标准已经确认，不要再问一遍。\n"
    if _needs_questions(sections):
        return "\n目标和完成标准还没写清。先问具体问题并给出建议默认，不要直接做成已完成或 PASS。\n"
    if _section_clear(sections.get("目标", "")) and _section_clear(sections.get("完成标准", "")):
        return "\n目标和完成标准已经写清，不要再问一遍。\n"
    return ""


def normalize_attempt(operation: dict) -> dict:
    if not isinstance(operation, dict):
        raise ValueError("attempt 必须是对象")
    route = str(operation.get("route") or operation.get("attempt") or operation.get("text") or "").strip()
    evidence = str(operation.get("evidence") or "").strip()
    if not route and not evidence:
        raise ValueError("attempt 需要 route 或 evidence")
    productive = operation.get("productive")
    if isinstance(productive, str):
        productive = productive.strip().casefold() in {"1", "true", "yes", "y", "是"}
    return {"route": route, "evidence": evidence, "productive": bool(productive)}


def apply_confirm(service, summary, operation: dict) -> str:
    question_id = str(operation.get("question") or operation.get("id") or "").strip().upper()
    why = str(operation.get("why") or operation.get("rationale") or "").strip()
    tradeoffs = str(operation.get("tradeoffs") or operation.get("tradeoff") or "").strip()
    who = str(operation.get("decided_by") or operation.get("who") or "").strip()
    if not question_id:
        raise ValueError("confirm 需要 question，例如 Q1")
    if not why or not tradeoffs or not who:
        raise ValueError("确认决定要写理由、取舍、决定人")
    if not summary.project:
        questions = _header_questions(summary)
        path = None
        front, sections = {}, {}
        text = ""
    else:
        path = _completion_path(service, summary.project)
        text = service.store.read_text(path)
        front, sections = _parse_completion(text)
        questions = _questions_from_front(front)
        if not questions and _needs_questions(sections):
            questions = _new_questions(summary.raw_command, sections)
    found = next((item for item in questions if item["id"] == question_id), None)
    if found is None:
        known = "、".join(item["id"] for item in questions) or "无"
        raise ValueError(f"没有这个问题：{question_id}。当前是 {known}")
    if found.get("status") == "confirmed":
        raise ValueError(f"{question_id} 已经确认过，不要重复问")
    accept_default = _flag(operation.get("accept_default"))
    answer = str(operation.get("decision") or operation.get("answer") or "").strip()
    if accept_default:
        answer = str(found.get("default") or "").strip()
    if not answer:
        raise ValueError("要给出决定，或 accept_default=true 接受建议默认")
    found = dict(found)
    found.update(status="confirmed", answer=answer, why=why, tradeoffs=tradeoffs, decided_by=who)
    updated = [found if item["id"] == question_id else item for item in questions]
    item = _decision_item(f"确认{question_id}：{answer}", why, tradeoffs, who)
    decision_text = _decision_text(item)
    _check_decision_length(decision_text)
    if path is not None:
        front["questions"] = updated
        sections[found["field_heading"]] = _answer_body(found["field"], answer)
        sections[QUESTION_SECTION] = _question_section(updated)
        sections[CONFIRMED_SECTION] = _confirmed_section(updated)
        try:
            service.store.write_text(path, _render_completion(front, sections, text))
        except OSError as exc:
            raise ValueError("完成标准没有写入，确认没有生效，原来的待确认还在。") from exc
    elif not text:
        pass
    _remember_questions(summary, updated)
    if found["field"] == "goal" and "target" not in (summary.header.get("user_owned_fields") or []):
        summary.target = answer[: handoff.TARGET_MAX_CHARS]
    return _add_decision(summary, decision_text)


def apply_continue(service, summary, operation: dict, *, now: str) -> str:
    if not is_paused(summary):
        raise ValueError("还没有暂停，不用 continue")
    route = str(operation.get("route") or operation.get("text") or "").strip()
    why = str(operation.get("why") or operation.get("rationale") or "").strip()
    tradeoffs = str(operation.get("tradeoffs") or operation.get("tradeoff") or "").strip()
    who = str(operation.get("decided_by") or operation.get("who") or "").strip()
    if not route:
        raise ValueError("continue 需要新路线 route")
    if not why or not tradeoffs or not who:
        raise ValueError("继续要写理由、取舍、决定人")
    state = ensure_stall(summary, policy_limit(service, summary))
    previous = str(state.get("last_text") or "")
    if previous and same_failure(route, previous):
        raise ValueError("新路线和刚停下的失败是同一条，不能当作换路")
    item = _decision_item(f"换路继续：{route}", why, tradeoffs, who)
    decision_text = _decision_text(item)
    _check_decision_length(decision_text)
    cleared = int(state.get("consecutive") or 0)
    state["paused"] = False
    state["reason"] = ""
    state["consecutive"] = 0
    state["last_signature"] = ""
    state["last_text"] = ""
    state["cleared_by"] = {"route": _scrub(route, summary, service), "why": _scrub(why, summary, service),
                           "at": now, "previous_consecutive": cleared}
    _append_log(summary, state, {
        "at": now, "effect": "continue", "kind": "continue",
        "signature": signature(previous), "note": _scrub(route, summary, service),
        "why": _scrub(why, summary, service), "consecutive": 0, "cleared_consecutive": cleared,
    })
    summary.header["status"] = "进行中"
    summary.header["stall"] = state
    if "next_step" not in (summary.header.get("user_owned_fields") or []):
        summary.next_step = _clip(f"{route}。验证：留下和上次失败不同的证据。", handoff.NEXT_MAX_CHARS)
    previous_handoff = str(summary.extra.get(HANDOFF_SECTION) or "").rstrip()
    restored = (
        "\n\n### 已恢复\n"
        f"新路线：{_scrub(route, summary, service)}\n"
        f"理由：{_scrub(why, summary, service)}\n"
        f"取舍：{_scrub(tradeoffs, summary, service)}\n"
        f"决定人：{_scrub(who, summary, service)}\n"
        f"这不是静默清零。此前连续无进展次数：{cleared}。\n"
    )
    summary.extra[HANDOFF_SECTION] = (previous_handoff + restored).strip() + "\n"
    return _add_decision(summary, decision_text)


def observe_saved(service, summary, *, now: str, attempts: list[dict] | None = None, dead_ends: list[str] | None = None,
                  evidence: list[str] | None = None, done: list[str] | None = None, verify: dict | None = None,
                  ordered: list[dict] | None = None) -> bool:
    """Fold one save into the stall tracker. Return True when this save pauses."""
    if is_paused(summary):
        return False
    items: list[dict] = []
    if ordered is not None:
        items.extend(ordered)
    else:
        for attempt in attempts or []:
            text = " ".join(dict.fromkeys(part for part in (attempt.get("route"), attempt.get("evidence")) if part))
            items.append({"kind": "attempt", "text": text, "productive": bool(attempt.get("productive"))})
        for text in dead_ends or []:
            items.append({"kind": "dead_end", "text": text, "productive": False})
        for text in evidence or []:
            items.append({"kind": "evidence", "text": text, "productive": False})
        for text in done or []:
            items.append({"kind": "done", "text": text, "productive": True})
    if isinstance(verify, dict) and str(verify.get("command") or "").strip() and type(verify.get("exit_code")) is int:
        items.append({
            "kind": "verify",
            "text": f"{verify['command']} exit {verify['exit_code']}",
            "productive": False,
            "exit_code": verify["exit_code"],
        })
    if not items:
        return False
    state = ensure_stall(summary, policy_limit(service, summary))
    seen: set[str] = set()
    tripped = False
    for item in items:
        sig = signature(item["text"])
        if sig in seen:
            continue
        seen.add(sig)
        effect = _note(summary, state, item, now=now, service=service)
        if effect == "pause":
            tripped = True
            break
    summary.header["stall"] = state
    if tripped:
        reason = (
            f"连续 {state['consecutive']} 次没有新进展。"
            f"同一失败：{_clip(str(state.get('last_text') or ''), 120)}"
        )
        mark_paused(service, summary, reason, now=now)
    return tripped


def ensure_stall(summary, policy: int) -> dict:
    raw = summary.header.get("stall")
    state = dict(raw) if isinstance(raw, dict) else {}
    for key in list(state):
        if key not in _STALL_KEYS:
            state.pop(key, None)
    state.setdefault("consecutive", 0)
    state.setdefault("last_signature", "")
    state.setdefault("last_text", "")
    state.setdefault("paused", False)
    state.setdefault("reason", "")
    state.setdefault("paused_at", "")
    state.setdefault("log", [])
    if not isinstance(state["log"], list):
        state["log"] = []
    try:
        state["consecutive"] = int(state["consecutive"])
    except (TypeError, ValueError):
        state["consecutive"] = 0
    state["policy"] = policy
    summary.header["stall"] = state
    return state


def policy_limit(service, summary) -> int:
    roots: list[str] = []
    if service is not None and summary.project:
        card = service.store.project(summary.project) or {}
        if card.get("worktree"):
            roots.append(str(card["worktree"]))
        roots.extend(str(item) for item in (card.get("paths") or []))
    cwd = str(summary.header.get("cwd") or "")
    if cwd:
        roots.append(cwd)
    for root in roots:
        limit = _policy_in(root)
        if limit is not None:
            return limit
    return DEFAULT_UNPRODUCTIVE


def mark_paused(service, summary, reason: str, *, now: str) -> None:
    state = ensure_stall(summary, policy_limit(service, summary))
    state["paused"] = True
    state["reason"] = _scrub(reason, summary, service)
    state["paused_at"] = now
    summary.header["status"] = "已暂停"
    summary.header["stall"] = state
    summary.extra[HANDOFF_SECTION] = render_hot_handoff(service, summary, reason=state["reason"])
    if "next_step" not in (summary.header.get("user_owned_fields") or []):
        summary.next_step = "用 continue 写明新路线、理由、取舍和决定人。"


def render_hot_handoff(service, summary, *, reason: str) -> str:
    card = {}
    if service is not None and summary.project:
        try:
            card = service.store.project(summary.project) or {}
        except Exception:
            card = {}
    git = summary.header.get("git") or {}
    if not isinstance(git, dict):
        git = {}
    convention = str(card.get("worktree_convention") or "").strip() or "../.tbrain-worktrees/<仓库目录名>/<项目名>"
    branch = str(git.get("branch") or card.get("worktree_branch") or "").strip() or "（还没有分支记录）"
    head = str(git.get("head") or "").strip() or "（还没有 HEAD）"
    dirty = git.get("dirty")
    dirty_text = str(dirty) if isinstance(dirty, int) else "（未知）"
    questions = open_questions(service, summary) if service is not None else _header_questions(summary)
    questions = [item for item in questions if item.get("status", "open") == "open"]
    lines = [
        f"状态：已暂停",
        f"停顿原因：{_scrub(reason, summary, service)}",
        "恢复条件：用 continue 写下新路线、理由、取舍和决定人。沿同一失败再试不会解除暂停。",
        "",
        "### 目标",
        _scrub(summary.target or "（会话目标还没写）", summary, service),
        "",
        "### 未确认问题",
    ]
    if questions:
        for item in questions:
            lines.append(f"- {item['id']} {item['prompt']}")
            lines.append(f"  建议默认：{item['default']}")
            lines.append("  状态：未确认")
    else:
        lines.append("（没有未确认的目标或完成标准）")
    lines += ["", "### 已确认决定"]
    decisions = [item for item in summary.all_items("决策") if handoff.decision_active(item.get("text") or "")]
    lines += _bullet(summary, service, decisions, empty="（还没有确认过的决定）")
    lines += [
        "",
        "### 工作区",
        f"约定：{_scrub(convention, summary, service)}",
        f"分支：{_scrub(branch, summary, service)}",
        f"HEAD：{head}",
        f"改动文件数：{dirty_text}",
        "",
        "### 已完成",
    ]
    lines += _bullet(summary, service, summary.items("已完成"), empty="（还没有已完成项）")
    lines += ["", "### 尝试过的路和失败证据"]
    routes = list(summary.items("未走通的路"))
    routes += summary.items("证据")
    lines += _bullet(summary, service, routes, empty="（还没有记下失败路线）")
    stall = _stall(summary)
    for entry in stall.get("log") or []:
        if not isinstance(entry, dict):
            continue
        note = str(entry.get("note") or "").strip()
        if note:
            lines.append(f"- {entry.get('effect') or 'attempt'}：{_scrub(note, summary, service)}")
    verify = summary.header.get("last_verify") or {}
    if not isinstance(verify, dict):
        verify = {}
    command = _scrub(str(verify.get("command") or ""), summary, service)
    code = verify.get("exit_code")
    lines += ["", "### 已核实"]
    if code == 0 and command:
        lines.append(f"{command} 退出码 0")
    else:
        lines.append("（没有退出码为 0 的验证）")
    lines += ["", "### 未核实"]
    if command and code not in (0, None):
        lines.append(f"{command} 退出码 {code}")
    elif command and code is None:
        lines.append(f"{command} 还没有退出码")
    else:
        lines.append("（没有未核实的验证记录）")
    lines += [
        "",
        "### 下一步",
        "先回答未确认的问题，或用 continue 写明新路线、理由、取舍和决定人。",
        "不要沿同一条失败自动再试，也不要等 Dream / L1。",
    ]
    body = "\n".join(lines).strip() + "\n"
    return _scrub(body, summary, service)


def render_questions(items: list[dict], *, heading: str) -> str:
    return f"## {heading}\n" + question_lines(items)


def question_lines(items: list[dict]) -> str:
    lines = [
        "可以只读代码和测试，把问题问清楚。不要动手实现，也不要写成已完成或 PASS。",
        "仓库和测试能回答的技术问题自己定，不要问 Kai。",
    ]
    for item in items:
        lines.append(f"- {item['id']} {item['prompt']}")
        lines.append(f"  建议默认：{item['default']}")
        lines.append("  状态：未确认")
    return "\n".join(lines).strip() + "\n"


def same_failure(left: str, right: str) -> bool:
    if not str(left or "").strip() or not str(right or "").strip():
        return False
    anchors_left, anchors_right = specific_anchors(left), specific_anchors(right)
    if anchors_left and anchors_right and anchors_left == anchors_right:
        return True
    fuzzy_left, fuzzy_right = _fuzzy(left), _fuzzy(right)
    if fuzzy_left and fuzzy_left == fuzzy_right:
        return True
    if fuzzy_left and fuzzy_right and min(len(fuzzy_left), len(fuzzy_right)) >= 12 and (
            fuzzy_left in fuzzy_right or fuzzy_right in fuzzy_left):
        return True
    if fuzzy_left and fuzzy_right and _jaccard(_bigrams(fuzzy_left), _bigrams(fuzzy_right)) >= 0.72:
        return True
    return False


def signature(text: str) -> str:
    anchors = specific_anchors(text)
    if anchors:
        return "anchor:" + "|".join(sorted(anchors))
    return "text:" + _fuzzy(text)[:180]


def specific_anchors(text: str) -> frozenset[str]:
    found = set()
    for match in _SPECIFIC.finditer(str(text or "")):
        token = match.group(1).casefold().replace("\\", "/")
        if token in {"error", "exception"}:
            continue
        found.add(token)
    return frozenset(found)


def _note(summary, state: dict, item: dict, *, now: str, service) -> str:
    text = str(item.get("text") or "")
    kind = str(item.get("kind") or "attempt")
    previous = str(state.get("last_text") or "")
    same = bool(previous) and (signature(text) == state.get("last_signature") or same_failure(text, previous))
    exit_code = item.get("exit_code")
    claimed = bool(item.get("productive"))
    if exit_code == 0 or kind == "done" or (claimed and not same):
        state["consecutive"] = 0
        state["last_signature"] = ""
        state["last_text"] = ""
        effect = "progress"
    elif kind == "evidence" and not same:
        state["consecutive"] = 0
        state["last_signature"] = ""
        state["last_text"] = ""
        effect = "new-evidence"
    elif same:
        state["consecutive"] = int(state.get("consecutive") or 0) + 1
        state["last_signature"] = signature(text)
        state["last_text"] = _scrub(text, summary, service)[:240]
        effect = "repeat"
    else:
        state["consecutive"] = 1
        state["last_signature"] = signature(text)
        state["last_text"] = _scrub(text, summary, service)[:240]
        effect = "new-route" if previous else "start"
    _append_log(summary, state, {
        "at": now, "effect": effect, "kind": kind, "signature": signature(text),
        "note": _scrub(text, summary, service)[:240], "consecutive": int(state["consecutive"]),
    })
    policy = int(state.get("policy") or DEFAULT_UNPRODUCTIVE)
    if effect in {"repeat", "start", "new-route"} and int(state["consecutive"]) >= policy:
        return "pause"
    return effect


def _append_log(summary, state: dict, entry: dict) -> None:
    log = list(state.get("log") or [])
    log.append(entry)
    overflow = log[:-LOG_LIMIT]
    state["log"] = log[-LOG_LIMIT:]
    if not overflow:
        return
    existing = str(summary.extra.get("停顿记录") or "")
    lines = [line for line in existing.splitlines() if line.strip()]
    for item in overflow:
        lines.append(f"- {item.get('at', '')} {item.get('effect', '')} {item.get('note', '')}")
    summary.extra["停顿记录"] = "\n".join(lines).strip() + "\n"


def _policy_in(root: str) -> int | None:
    try:
        path = find_config(Path(root))
    except OSError:
        return None
    if path is None:
        return None
    try:
        import yaml
        loaded = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    except (OSError, Exception):
        return None
    if not isinstance(loaded, dict) or "stall" not in loaded:
        return None
    stall = loaded.get("stall") or {}
    if not isinstance(stall, dict):
        return None
    raw = stall.get("unproductive_attempts", DEFAULT_UNPRODUCTIVE)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_UNPRODUCTIVE
    if value < 1:
        return DEFAULT_UNPRODUCTIVE
    return min(value, MAX_UNPRODUCTIVE)


def _sync_project(service, summary, task: str) -> bool:
    path = _completion_path(service, summary.project)
    text = service.store.read_text(path)
    if not text:
        _remember_questions(summary, [])
        return True
    front, sections = _parse_completion(text)
    stored = _questions_from_front(front)
    if stored:
        _remember_questions(summary, stored)
        return True
    if not _needs_questions(sections):
        _remember_questions(summary, [])
        return True
    questions = _new_questions(task, sections)
    front["questions"] = questions
    sections[QUESTION_SECTION] = _question_section(questions)
    sections.setdefault(CONFIRMED_SECTION, _confirmed_section(questions))
    updated = _render_completion(front, sections, text)
    try:
        service.store.write_text(path, updated)
    except OSError:
        _remember_questions(summary, questions, persisted=False)
        return False
    _remember_questions(summary, questions, persisted=True)
    return True


def _sync_independent(summary, task: str) -> bool:
    if summary.target.strip() and _section_clear(summary.target):
        _remember_questions(summary, [item for item in _header_questions(summary) if item.get("status") == "open"])
        return True
    if not str(task or "").strip() or not naming.is_low_information(task):
        _remember_questions(summary, [])
        return True
    existing = _header_questions(summary)
    if existing:
        _remember_questions(summary, existing)
        return True
    default = _TASK_DEFAULT
    questions = [{
        "id": "Q1", "field": "goal", "field_heading": "目标", "prompt": _TASK_PROMPT,
        "default": default, "status": "open", "answer": "", "why": "", "tradeoffs": "", "decided_by": "",
    }]
    _remember_questions(summary, questions, persisted=True)
    return True


def _new_questions(task: str, sections: dict[str, str]) -> list[dict]:
    questions = []
    if not _section_clear(sections.get("目标", "")):
        questions.append({
            "id": "", "field": "goal", "field_heading": "目标", "prompt": _GOAL_PROMPT,
            "default": _goal_default(task), "status": "open", "answer": "", "why": "",
            "tradeoffs": "", "decided_by": "",
        })
    if not _section_clear(sections.get("完成标准", "")):
        questions.append({
            "id": "", "field": "done", "field_heading": "完成标准", "prompt": _DONE_PROMPT,
            "default": _DONE_DEFAULT, "status": "open", "answer": "", "why": "",
            "tradeoffs": "", "decided_by": "",
        })
    for index, item in enumerate(questions, start=1):
        item["id"] = f"Q{index}"
    return questions


def _needs_questions(sections: dict[str, str]) -> bool:
    return not _section_clear(sections.get("目标", "")) or not _section_clear(sections.get("完成标准", ""))


def _section_clear(body: str) -> bool:
    lines = []
    for line in str(body or "").splitlines():
        line = re.sub(r"^\s*[-*]\s*\[[ xX]\]\s*", "", line).strip()
        if line:
            lines.append(line)
    text = "\n".join(lines).strip()
    if not text:
        return False
    return "待填写" not in text


def _goal_default(task: str) -> str:
    text = " ".join(str(task or "").split())
    if text and not naming.is_low_information(text):
        clipped = text if len(text) <= 80 else text[:79] + "…"
        return f"按已说明的任务做完并可交接：{clipped}"
    return _GOAL_DEFAULT


def _questions_from_front(front: dict) -> list[dict]:
    raw = front.get("questions") or []
    if not isinstance(raw, list):
        return []
    questions = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        question_id = str(item.get("id") or "").strip().upper()
        if not question_id:
            continue
        field = str(item.get("field") or "goal").strip()
        questions.append({
            "id": question_id,
            "field": field,
            "field_heading": "完成标准" if field == "done" else "目标",
            "prompt": str(item.get("prompt") or "").strip(),
            "default": str(item.get("default") or "").strip(),
            "status": "confirmed" if str(item.get("status") or "") == "confirmed" else "open",
            "answer": str(item.get("answer") or "").strip(),
            "why": str(item.get("why") or "").strip(),
            "tradeoffs": str(item.get("tradeoffs") or "").strip(),
            "decided_by": str(item.get("decided_by") or "").strip(),
        })
    return questions


def _header_questions(summary) -> list[dict]:
    raw = summary.header.get("goal_questions") or []
    if not isinstance(raw, list):
        return []
    return _questions_from_front({"questions": raw})


def _remember_questions(summary, questions: list[dict], *, persisted: bool = True) -> None:
    summary.header["goal_questions"] = [
        {key: item.get(key, "") for key in (
            "id", "field", "prompt", "default", "status", "answer", "why", "tradeoffs", "decided_by")}
        for item in questions
    ]
    summary.header["goal_questions_persisted"] = bool(persisted)
    open_items = [item for item in questions if item.get("status") == "open"]
    if open_items:
        summary.extra[QUESTION_SECTION] = QUESTION_MARKER + "\n" + question_lines(open_items)
    elif str(summary.extra.get(QUESTION_SECTION) or "").startswith(QUESTION_MARKER):
        summary.extra.pop(QUESTION_SECTION, None)


def _parse_completion(text: str) -> tuple[dict, dict[str, str]]:
    front, body = split_front_matter(text)
    sections: dict[str, str] = {}
    order: list[str] = []
    current = ""
    bucket: list[str] = []

    def flush() -> None:
        if current:
            sections[current] = "\n".join(bucket).strip()
            if current not in order:
                order.append(current)

    for line in str(body or "").replace("\r\n", "\n").split("\n"):
        if line.startswith("## "):
            flush()
            current = line[3:].strip()
            bucket = []
            continue
        if current:
            bucket.append(line)
    flush()
    sections["__order__"] = "\n".join(order)
    return front, sections


def _render_completion(front: dict, sections: dict[str, str], original: str) -> str:
    order = [item for item in str(sections.get("__order__") or "").split("\n") if item]
    for name in ("目标", "完成标准", "证据", "验证", QUESTION_SECTION, CONFIRMED_SECTION):
        if name in sections and name not in order:
            order.append(name)
    for name in sections:
        if name != "__order__" and name not in order:
            order.append(name)
    body_parts = []
    for name in order:
        body_parts.append(f"## {name}\n{str(sections.get(name) or '').strip()}\n")
    data = {key: value for key, value in front.items() if key != "questions"}
    if front.get("questions"):
        data["questions"] = _public_questions(front["questions"])
    return render_note(data, "\n".join(body_parts).strip())


def _public_questions(questions: list[dict]) -> list[dict]:
    public = []
    for item in questions:
        public.append({
            "id": item["id"], "field": item["field"], "prompt": item["prompt"],
            "default": item["default"], "status": item["status"], "answer": item.get("answer") or "",
            "why": item.get("why") or "", "tradeoffs": item.get("tradeoffs") or "",
            "decided_by": item.get("decided_by") or "",
        })
    return public


def _question_section(questions: list[dict]) -> str:
    lines = []
    for item in questions:
        if item.get("status") == "confirmed":
            continue
        lines += [
            f"### {item['id']}",
            f"问题：{item['prompt']}",
            f"建议默认：{item['default']}",
            "状态：未确认",
            "",
        ]
    return "\n".join(lines).strip()


def _confirmed_section(questions: list[dict]) -> str:
    lines = []
    for item in questions:
        if item.get("status") != "confirmed":
            continue
        lines += [
            f"### {item['id']}",
            f"决定：{item.get('answer') or ''}",
            f"理由：{item.get('why') or ''}",
            f"取舍：{item.get('tradeoffs') or ''}",
            f"决定人：{item.get('decided_by') or ''}",
            "",
        ]
    return "\n".join(lines).strip()


def _answer_body(field: str, answer: str) -> str:
    if field == "done":
        text = answer.strip()
        if text.startswith("- ["):
            return text
        return f"- [ ] {text}"
    return answer.strip()


def _completion_path(service, project: str) -> Path:
    return service.store.project_dir(project) / COMPLETION_FILE


def _read_completion(service, project: str) -> str:
    if not project:
        return ""
    try:
        return service.store.read_text(_completion_path(service, project))
    except OSError:
        return ""


def _decision_item(decision: str, why: str, tradeoffs: str, who: str) -> dict:
    decision = " ".join(decision.split())
    why_line = " ".join(why.split())
    if len(decision) > 120:
        decision = decision[:119] + "…"
    if len(why_line) > 60:
        why_line = why_line[:59] + "…"
    return {"decision": decision, "reason": why_line, "rationale": why, "tradeoffs": tradeoffs, "decided_by": who}


def _decision_text(item: dict) -> str:
    lines = [f"{item['decision']} | {item['reason']}"]
    if item.get("rationale"):
        lines.append(f"理由：{item['rationale']}")
    if item.get("tradeoffs"):
        lines.append(f"取舍：{item['tradeoffs']}")
    if item.get("decided_by"):
        lines.append(f"决定人：{item['decided_by']}")
    return "\n".join(lines)


def _check_decision_length(text: str) -> None:
    primary = text.split("\n", 1)[0]
    if len(primary) > handoff.ITEM_MAX_CHARS or len(text) > 2000:
        raise ValueError("确认决定太长，请缩短理由后再提交")


def _add_decision(summary, text: str) -> str:
    for candidate in summary.all_items("决策"):
        if str(candidate.get("text") or "").strip() == text.strip():
            return candidate["id"]
    return handoff.add_item(summary, "决策", text)


def _bullet(summary, service, items: list[dict], *, empty: str) -> list[str]:
    if not items:
        return [empty]
    lines = []
    for item in items[-15:]:
        lines.append(f"- {item['id']} {_scrub(str(item.get('text') or ''), summary, service)}")
    if len(items) > 15:
        lines.append("- 更早的条目仍在本会话总结里。")
    return lines


def _scrub(text: str, summary, service) -> str:
    value = str(text or "")
    hides: list[str] = []
    cwd = str((summary.header.get("cwd") if summary is not None else "") or "")
    if cwd:
        hides.append(cwd)
    if service is not None and summary is not None and summary.project:
        try:
            card = service.store.project(summary.project) or {}
        except Exception:
            card = {}
        for key in ("worktree", "git_root"):
            if card.get(key):
                hides.append(str(card[key]))
        hides.extend(str(item) for item in (card.get("paths") or []) if item)
    try:
        home = str(Path.home())
        if home and home not in {"/", "."}:
            hides.append(home)
    except OSError:
        pass
    for item in sorted({hide for hide in hides if hide and len(hide) > 3}, key=len, reverse=True):
        value = value.replace(item, "工作区")
    return _PERSONAL_PATH.sub("~", value)


def _stall(summary) -> dict:
    raw = summary.header.get("stall")
    return raw if isinstance(raw, dict) else {}


def _flag(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "y", "是"}


def _fuzzy(text: str) -> str:
    raw = str(text or "").casefold().replace("\\", "/")
    raw = _PERSONAL_PATH.sub("", raw)
    raw = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", raw)
    for word in _FILLER:
        raw = raw.replace(word.casefold(), "")
    return raw


def _bigrams(text: str) -> set[str]:
    if len(text) < 2:
        return {text} if text else set()
    return {text[index:index + 2] for index in range(len(text) - 1)}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 1.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def _clip(text: str, limit: int) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"
