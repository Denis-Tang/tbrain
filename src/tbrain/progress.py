"""Goal clarification, no-progress pause, and the L0 hot handoff.

This sits on the existing bootstrap / save / resume path. It does not budget
tokens, money, or wall time, and it does not own the external agent process.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import handoff
from .check import find_configs

HANDOFF_SECTION = "热交接"
QUESTION_SECTION = "待确认"
QUESTION_MARKER = "<!-- tbrain:questions -->"
DEFAULT_UNPRODUCTIVE = 3
MAX_UNPRODUCTIVE = 20
LOG_LIMIT = 40
HANDOFF_PACKAGE_LIMIT = 3500

_STALL_KEYS = {
    "policy", "consecutive", "last_signature", "last_text", "paused", "reason",
    "paused_at", "log", "cleared_by", "streak_notes", "last_outcome",
    "paused_user_count", "proposed_route",
}
_UNPRODUCTIVE_EFFECTS = {"start", "repeat", "unproductive"}
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
UNDERSTANDING_FIELDS = (("goal", "任务目标"), ("scope", "本次范围与业务规则"),
                        ("deliverables", "交付结果"), ("done", "完成标准"))


def enabled(summary) -> bool:
    return bool(summary.header.get("task_mode", False))


def is_paused(summary) -> bool:
    stall = _stall(summary)
    return enabled(summary) and (bool(stall.get("paused")) or str(summary.header.get("status") or "") == "已暂停")


def approval_block(service, summary) -> str:
    """Why this session must not be marked done or PASS. Empty means it may."""
    if not enabled(summary):
        return ""
    if is_paused(summary):
        reason = str(_stall(summary).get("reason") or "连续没有新进展")
        return f"会话已暂停：{reason}。不能写成已完成或 PASS。"
    open_items = open_questions(service, summary)
    if open_items:
        names = "、".join(item["id"] for item in open_items)
        return f"任务目标理解还没确认（{names}）。不能写成已完成或 PASS。"
    return ""


def gate_gaps(summary) -> list[str]:
    """Wrap-up gaps carried on the session itself."""
    gaps = []
    if not enabled(summary):
        return gaps
    stall = _stall(summary)
    if is_paused(summary):
        reason = str(stall.get("reason") or "连续没有新进展")
        gaps.append(f"会话已暂停：{reason}。先用 continue 写明新路线和为什么，不能收尾。")
    open_items = [item for item in _header_questions(summary) if item.get("status") == "open"]
    if open_items:
        names = "、".join(item["id"] for item in open_items)
        gaps.append(f"任务目标理解还没确认（{names}）。不能当作已完成或 PASS。")
    return gaps


def start_confirmation(summary) -> None:
    """Open a session-local confirmation on its first real user message."""
    if not enabled(summary):
        return
    _remember_questions(summary, [{
        "id": "Q1", "field": "goal", "prompt": "请确认我对任务目标、业务规则和完成标准的理解是否准确。",
        "default": "", "status": "open",
    }])


def open_questions(service, summary) -> list[dict]:
    return [item for item in _header_questions(summary) if item.get("status") == "open"] if enabled(summary) else []


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
    return render_questions(items, heading="待确认的任务理解")


def turn_notice(service, summary) -> str:
    if is_paused(summary):
        return pause_instruction(summary)
    items = open_questions(service, summary)
    if not items:
        return ""
    return render_questions(items, heading="待确认的任务理解")


def pause_instruction(summary) -> str:
    reason = str(_stall(summary).get("reason") or "连续没有新进展")
    return (
        f"已暂停：{reason}\n"
        "可以继续保存证据、走不通的路、决定理由和下一步提议。这些不是继续实现。\n"
        "要解除暂停，先把下一步写成具体路线，再让用户在这次暂停之后同意这条路线。\n"
        "原话不必重复整句路线。暂停前的旧原话、以及原话里的片段，都不能代替这条路线。\n"
        "只填写决定人的名字不能解除暂停。Tbrain 不核验这个名字。\n"
        "Tbrain 会拒绝已完成和 PASS。Stop 不会再要求模型继续这一轮。\n"
        "Tbrain 不拥有外部 Agent 进程，不能把那个进程杀掉。"
    )


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


def apply_understanding(summary, operation: dict) -> str:
    """Record the Agent's restatement; a correction must cite the user message."""
    questions = _header_questions(summary)
    if not questions:
        raise ValueError("收到首条用户消息后才能复述任务目标")
    if questions[0].get("status") == "confirmed":
        source = _confirmation_source(summary, operation, who="")
        if int(source["id"][1:]) <= int(summary.header.get("understanding_users") or 0):
            raise ValueError("只有用户指出任务理解有误后，才能重新复述并确认")
    fields = []
    for key, label in UNDERSTANDING_FIELDS:
        value = " ".join(str(operation.get(key) or "").split())
        if not value or len(value) > 160:
            raise ValueError(f"{key}（{label}）须为 1～160 字")
        fields.append(f"{label}：{value}")
    text = "\n".join(fields)
    questions[0].update(default=text, status="open", answer="")
    summary.header["understanding_users"] = len(summary.users)
    summary.extra["任务理解"] = text + "\n状态：未确认\n"
    _remember_questions(summary, questions)
    return text


def apply_confirm(service, summary, operation: dict) -> str:
    question_id = str(operation.get("question") or operation.get("id") or "").strip().upper()
    why = str(operation.get("why") or operation.get("rationale") or "").strip()
    tradeoffs = str(operation.get("tradeoffs") or operation.get("tradeoff") or "").strip()
    who = str(operation.get("decided_by") or operation.get("who") or "").strip()
    if operation.get("grant") or operation.get("permission") or operation.get("tier"):
        raise ValueError("确认目标不附带权限。权限仍按 permission 单独记录，默认只有读代码、日志和测试。")
    if not why or not tradeoffs or not who:
        raise ValueError("确认决定要写理由、取舍、决定人")
    source = _confirmation_source(summary, operation, who=who)
    questions = _header_questions(summary)
    found = next((item for item in questions if item["id"] == question_id), None)
    if found is None:
        raise ValueError(f"没有这个问题：{question_id}。当前是 Q1 任务理解确认")
    if found.get("status") == "confirmed":
        raise ValueError(f"{question_id} 已经确认过，不要重复问")
    answer = str(found.get("default") or "")
    if not answer:
        raise ValueError("请先用 understanding 保存任务目标复述，并向用户说明后等待确认")
    if int(source["id"][1:]) <= int(summary.header["understanding_users"]):
        raise ValueError("确认必须来自任务复述之后的用户原话")
    if not _assents_to_question(source["text"], question_id, answer):
        raise ValueError("这句原话没有确认这个具体问题的任务理解")
    goal = answer.splitlines()[0].removeprefix("任务目标：")
    found.update(status="confirmed", answer=answer, why=why, tradeoffs=tradeoffs, decided_by=who)
    summary.header["understanding_users"] = len(summary.users)
    summary.extra["任务理解"] = answer + f"\n确认来源：{source['id']}\n"
    _remember_questions(summary, questions)
    if "target" not in (summary.header.get("user_owned_fields") or []):
        summary.target = goal
    return question_id


def apply_continue(service, summary, operation: dict, *, now: str) -> str:
    if not is_paused(summary):
        raise ValueError("还没有暂停，不用 continue")
    why = str(operation.get("why") or operation.get("rationale") or "").strip()
    tradeoffs = str(operation.get("tradeoffs") or operation.get("tradeoff") or "").strip()
    who = str(operation.get("decided_by") or operation.get("who") or "").strip()
    if not why or not tradeoffs or not who:
        raise ValueError("继续要写理由、取舍、决定人")
    state = ensure_stall(summary, policy_limit(service, summary))
    proposal = str(state.get("proposed_route") or "").strip()
    if not proposal:
        raise ValueError("还没有记下具体的新路线。请先把下一步写成要走的路线，再请用户在这次暂停之后明确同意。不要猜。")
    source = _confirmation_source(summary, operation, who=who)
    if int(source["id"][1:]) <= int(state.get("paused_user_count") or 0):
        raise ValueError("这句原话在本次暂停之前，不能用来解除暂停")
    if not _assents_to_proposal(source["text"], proposal):
        raise ValueError("这句原话没有同意已记下的新路线")
    supplied = str(operation.get("route") or operation.get("text") or "").strip()
    route = supplied or proposal
    if route != proposal:
        raise ValueError("继续的路线必须是已记下的下一步，不能用原话里的片段代替")
    previous = str(state.get("last_text") or "")
    if _matches_streak(state, route):
        raise ValueError("新路线仍是没有实质进展的尝试，不能当作换路")
    item = _decision_item(f"换路继续：{route}", why, tradeoffs, who, source=source["id"])
    decision_text = _decision_text(item)
    _check_decision_length(decision_text)
    cleared = int(state.get("consecutive") or 0)
    state["paused"] = False
    state["reason"] = ""
    state["consecutive"] = 0
    state["last_signature"] = ""
    state["last_text"] = ""
    state["streak_notes"] = []
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
    if not enabled(summary) or is_paused(summary):
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
    for item in items:
        sig = signature(item["text"]) + "|" + str(item.get("exit_code") if item.get("exit_code") is not None else "")
        if sig in seen:
            continue
        seen.add(sig)
        _note(summary, state, item, now=now, service=service)
    end_paused = int(state["consecutive"]) >= int(state["policy"])
    summary.header["stall"] = state
    if end_paused:
        reason = (
            f"连续 {state['consecutive']} 次没有实质进展。"
            f"最近一次：{_clip(str(state.get('last_text') or ''), 120)}"
        )
        mark_paused(service, summary, reason, now=now)
    return end_paused


def ensure_stall(summary, policy: int) -> dict:
    raw = summary.header.get("stall")
    state = dict(raw) if isinstance(raw, dict) else {}
    for key in list(state):
        if key not in _STALL_KEYS:
            state.pop(key, None)
    state.setdefault("consecutive", 0)
    state.setdefault("last_signature", "")
    state.setdefault("last_text", "")
    state.setdefault("last_outcome", "")
    state.setdefault("paused", False)
    state.setdefault("paused_user_count", 0)
    state.setdefault("proposed_route", "")
    state.setdefault("reason", "")
    state.setdefault("paused_at", "")
    state.setdefault("log", [])
    state.setdefault("streak_notes", [])
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
        roots.extend(str(item) for item in (card.get("paths") or []))
        if card.get("worktree"):
            roots.append(str(card["worktree"]))
    cwd = str(summary.header.get("cwd") or "")
    if cwd:
        roots.append(cwd)
    configs = []
    for root in roots:
        try:
            configs.extend(find_configs(Path(root)))
        except OSError:
            continue
    limit = _stall_from_configs(configs)
    return limit if limit is not None else DEFAULT_UNPRODUCTIVE


def mark_paused(service, summary, reason: str, *, now: str) -> None:
    state = ensure_stall(summary, policy_limit(service, summary))
    state["paused"] = True
    state["reason"] = _scrub(reason, summary, service)
    state["paused_at"] = now
    state["paused_user_count"] = len(summary.users)
    state["proposed_route"] = ""
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
        "收到首条用户消息后，先确认完整任务理解：目标、范围、关键业务规则、交付物和完成标准，再进入执行。",
        "确认的是用户需要的结果及必须遵守的业务口径，不是具体技术方案；已有项目记忆不能跳过首轮确认。",
        "先查用户指令、项目文件和已有记录；已确认规则直接沿用，不把自己能查清的技术问题交回用户。",
        "按下面四项回复用户；先用 save 的 understanding 操作保存同一份复述，再等待用户回复：",
        "任务目标：用户希望达到什么结果。",
        "本次范围与业务规则：这次包含哪些内容、必须遵守的处理口径和信息保留要求；只写用户明确的边界。",
        "交付结果：最后交给用户什么。",
        "完成标准：用户怎样判断任务做完。未知内容写“待确认”，不要自行补成事实。",
        "末尾请求确认完整任务理解；已知的关键业务缺口放在同一轮集中询问，不能只确认笼统目标就当规则齐全。",
        "借鉴 ASD-STE100：短句、主动句、一句一件事；同一概念用同一名称，使用通俗中文。",
        "已经保存复述时，先处理用户回复：明确确认就调用 confirm，不要再次要求确认；用户纠正时才修改复述再等待。",
        "确认后不因新消息、压缩、接续或项目模板缺项重复确认整个任务。",
        "只有用户指出整体理解有误时，才重做 understanding 并引用 source；新业务缺口不重启整项任务确认。",
        "执行中发现会改变业务结果、现有资料又不能确定的新缺口，集中列出问题及影响范围，只暂停依赖该决定的部分。",
        "处理口径、信息保留、含糊规则冲突、任务边界、验收及取舍都属于业务缺口；明确的新要求直接遵守。",
        "业务缺口用 save append goal 记录；用户明确回答后保存决定和原话来源，再用 set_status 的 complete_goals 关闭对应 G#。",
        "未回答的问题保留，不反复询问已回答的问题；未知数据事实按已定口径留空或进核实清单，不逐条打断开发任务。",
        "任务确认不等于规则已经齐全；不得猜定缺失口径或将待确认部分写成已完成。不依赖缺口的工作继续推进。",
        "未确认时不执行任务；可以保存复述和已有记录。已完成、项目提示词和验收会被拒绝。",
        "Tbrain 只拦这些保存和 hook，不能拦住宿主直接改文件。",
    ]
    for item in items:
        lines.append(f"- {item['id']} {item['prompt']}")
        if item.get("default"):
            lines.append(item["default"])
        lines.append("状态：未确认")
    return "\n".join(lines).strip() + "\n"


_FAIL_COUNT = re.compile(r"(\d+)\s*(?:failed|failures|failure|失败)|(?:failures?|失败)\s*[:=]\s*(\d+)", re.IGNORECASE)
_PASS_COUNT = re.compile(r"(\d+)\s*(?:passed|passing|通过)|(?:passed|passing|通过)\s*[:=]\s*(\d+)", re.IGNORECASE)


def result_counts(text: str) -> tuple[str, str] | None:
    """Failing and passing counts only. Elapsed time and other digits are ignored."""
    raw = str(text or "")
    failed = [left or right for left, right in _FAIL_COUNT.findall(raw)]
    passed = [left or right for left, right in _PASS_COUNT.findall(raw)]
    if not failed and not passed:
        return None
    return (",".join(failed), ",".join(passed))


def outcome_key(text: str, explicit: str = "") -> str:
    """Identity of an execution result.

    An explicit outcome wins. Otherwise only ``N failed`` / ``N passed`` counts.
    ``10 failed, took 12s`` is the same result as ``10 failed``.
    """
    given = " ".join(str(explicit or "").casefold().split())
    if given:
        return "explicit:" + given
    counts = result_counts(text)
    if not counts:
        return ""
    return f"fail:{counts[0]}|pass:{counts[1]}"


def same_failure(left: str, right: str) -> bool:
    if not str(left or "").strip() or not str(right or "").strip():
        return False
    anchors_left, anchors_right = specific_anchors(left), specific_anchors(right)
    if anchors_left and anchors_right and anchors_left == anchors_right:
        return result_counts(left) == result_counts(right)
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
    base = "anchor:" + "|".join(sorted(anchors)) if anchors else "text:" + _fuzzy(text)[:180]
    outcome = outcome_key(text)
    return base + ("|" + outcome if outcome else "")


def specific_anchors(text: str) -> frozenset[str]:
    found = set()
    for match in _SPECIFIC.finditer(str(text or "")):
        token = match.group(1).casefold().replace("\\", "/")
        if token in {"error", "exception"}:
            continue
        found.add(token)
    return frozenset(found)


def _note(summary, state: dict, item: dict, *, now: str, service) -> str:
    """Count attempts without substantive progress. A new failure wording still counts.

    Only a successful verification, or evidence that is not a reword of this
    streak, resets the counter. Claiming ``productive`` does not.
    """
    text = str(item.get("text") or "")
    kind = str(item.get("kind") or "attempt")
    exit_code = item.get("exit_code")
    note = _scrub(text, summary, service)[:240]
    cur_outcome = outcome_key(text, str(item.get("outcome_key") or ""))
    prev_outcome = str(state.get("last_outcome") or "")
    route_same = bool(specific_anchors(text)) and specific_anchors(text) == specific_anchors(str(state.get("last_text") or ""))
    if kind == "done":
        _append_log(summary, state, {
            "at": now, "effect": "noted", "kind": kind, "signature": signature(text),
            "note": note, "consecutive": int(state.get("consecutive") or 0),
        })
        return "noted"
    same = _matches_streak(state, text)
    outcome_changed = bool(
        kind in {"attempt", "verify"} and route_same
        and result_counts(text) and result_counts(str(state.get("last_text") or ""))
        and result_counts(text) != result_counts(str(state.get("last_text") or ""))
    )
    if exit_code == 0 or (kind == "evidence" and not same) or outcome_changed:
        state["consecutive"] = 0
        state["last_signature"] = signature(text)
        state["last_text"] = note
        state["last_outcome"] = cur_outcome
        state["streak_notes"] = []
        effect = "progress" if exit_code == 0 else ("outcome" if outcome_changed else "new-evidence")
    else:
        state["consecutive"] = int(state.get("consecutive") or 0) + 1
        state["last_signature"] = signature(text)
        state["last_text"] = note
        state["last_outcome"] = cur_outcome or prev_outcome
        streak = [str(item) for item in (state.get("streak_notes") or []) if str(item).strip()]
        streak.append(note)
        state["streak_notes"] = streak[-20:]
        effect = "repeat" if same else ("start" if state["consecutive"] == 1 else "unproductive")
    _append_log(summary, state, {
        "at": now, "effect": effect, "kind": kind, "signature": signature(text),
        "note": note, "consecutive": int(state["consecutive"]),
    })
    policy = int(state.get("policy") or DEFAULT_UNPRODUCTIVE)
    if effect in _UNPRODUCTIVE_EFFECTS and int(state["consecutive"]) >= policy:
        return "pause"
    return effect


def _matches_streak(state: dict, text: str) -> bool:
    if not str(text or "").strip():
        return False
    notes = [str(state.get("last_text") or "")]
    notes.extend(str(item) for item in (state.get("streak_notes") or []))
    for note in notes:
        if note and (signature(text) == signature(note) or same_failure(text, note)):
            return True
    for entry in state.get("log") or []:
        if not isinstance(entry, dict) or entry.get("effect") not in _UNPRODUCTIVE_EFFECTS:
            continue
        note = str(entry.get("note") or "")
        if note and (signature(text) == str(entry.get("signature") or "") or same_failure(text, note)):
            return True
    return False


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


def _stall_value(path: Path) -> int | None:
    try:
        import yaml
        loaded = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    except (OSError, Exception):
        return None
    if not isinstance(loaded, dict) or "stall" not in loaded:
        return None
    stall = loaded.get("stall") or {}
    if not isinstance(stall, dict) or "unproductive_attempts" not in stall:
        return None
    try:
        value = int(stall.get("unproductive_attempts"))
    except (TypeError, ValueError):
        return None
    if value < 1:
        return DEFAULT_UNPRODUCTIVE
    return min(value, MAX_UNPRODUCTIVE)


def _policy_in(root: str) -> int | None:
    """Stall limit for one directory. A user check file wins over the generated file."""
    try:
        return _stall_from_configs(find_configs(Path(root)))
    except OSError:
        return None


def _stall_from_configs(paths: list[Path]) -> int | None:
    """A user ``tbrain.check.yaml`` stall value wins over ``.tbrain/check.yaml``."""
    user_value = None
    managed_value = None
    seen = set()
    for path in paths:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key in seen:
            continue
        seen.add(key)
        value = _stall_value(path)
        if value is None:
            continue
        if path.name == "tbrain.check.yaml":
            if user_value is None:
                user_value = value
        elif managed_value is None:
            managed_value = value
    if user_value is not None:
        return user_value
    return managed_value


def _header_questions(summary) -> list[dict]:
    return [dict(item) for item in summary.header.get("goal_questions") or []]


def _remember_questions(summary, questions: list[dict]) -> None:
    summary.header["goal_questions"] = questions
    open_items = [item for item in questions if item.get("status") == "open"]
    if open_items:
        summary.extra[QUESTION_SECTION] = QUESTION_MARKER + "\n" + "\n".join(
            f"{item['id']} {item['prompt']}\n{item.get('default') or ''}\n状态：未确认"
            for item in open_items)
    elif str(summary.extra.get(QUESTION_SECTION) or "").startswith(QUESTION_MARKER):
        summary.extra.pop(QUESTION_SECTION, None)


def _decision_item(decision: str, why: str, tradeoffs: str, who: str, source: str = "") -> dict:
    decision = " ".join(decision.split())
    why_line = " ".join(why.split())
    if len(decision) > 120:
        decision = decision[:119] + "…"
    if len(why_line) > 60:
        why_line = why_line[:59] + "…"
    item = {"decision": decision, "reason": why_line, "rationale": why, "tradeoffs": tradeoffs, "decided_by": who}
    if source:
        item["source"] = source
    return item


def _decision_text(item: dict) -> str:
    lines = [f"{item['decision']} | {item['reason']}"]
    if item.get("rationale"):
        lines.append(f"理由：{item['rationale']}")
    if item.get("tradeoffs"):
        lines.append(f"取舍：{item['tradeoffs']}")
    if item.get("decided_by"):
        lines.append(f"决定人：{item['decided_by']}")
    if item.get("source"):
        lines.append(f"来源：{item['source']}")
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


def _confirmation_source(summary, operation: dict, *, who: str) -> dict:
    source = str(operation.get("source") or "").strip().upper()
    match = re.fullmatch(r"U(\d+)", source)
    if not match:
        raise ValueError("需要 source，指向用户原话编号，例如 U2。不能由保存操作自行认定")
    number = int(match.group(1))
    found = next((item for item in summary.sections.get("用户指令", []) if int(item.get("number") or 0) == number), None)
    if found is None:
        raise ValueError(f"找不到用户原话 {source}")
    text = str(found.get("text") or "").strip()
    if not text:
        raise ValueError("这句用户原话不能当作确认")
    return {"id": source, "text": text, "recorded_name": who}


def _positive_assent(text: str, marks: tuple[str, ...]) -> bool:
    raw = str(text or "")
    if re.search(r"不(同意|接受|按默认|用默认|继续|确认|准确|正确|对)|理解有误", raw):
        return False
    folded = raw.casefold()
    return any(mark.casefold() in folded or mark in raw for mark in marks)


def _assents_to_question(text: str, question_id: str, default: str) -> bool:
    raw = str(text or "").strip().rstrip("。.!！")
    if raw.casefold() in {"对", "是", "是的", "确认", "同意", "可以", "继续", "没问题", "yes", "ok", "correct"}:
        return True
    return (_positive_assent(raw, ("确认", "同意", "接受", "准确", "正确", "对的", "理解对了", "accept"))
            and (question_id in raw.upper() or default in raw
                 or any(word in raw for word in ("理解", "复述", "任务目标"))))


def _assents_to_proposal(text: str, proposal: str) -> bool:
    if not _positive_assent(text, ("继续", "同意", "按建议", "就按", "accept")):
        return False
    return proposal in text or any(reference in text for reference in ("新路线", "这条路线", "下一步", "建议"))


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
