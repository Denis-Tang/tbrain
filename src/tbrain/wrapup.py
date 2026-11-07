"""Wrap-up gate: evidence, local check, decision reasons, permission tier, A–E acceptance."""

from __future__ import annotations

import re

from . import acceptance, handoff
from .check import has_decision_reasons
from .policy import TIERS, TIER_TEXT, classify, covers

_LOCAL_CHECK = re.compile(
    r"(?:^|[\s\"'`/\\])tbrain(?:\.cmd)?\s+check(?:\s|$)"
    r"|python(?:\d+(?:\.\d+)?)?\s+-m\s+tbrain\s+check(?:\s|$)"
)


def local_check_recorded(summary) -> tuple[bool, str]:
    verify = summary.header.get("last_verify") or {}
    command = str(verify.get("command") or "")
    code = verify.get("exit_code")
    if not _LOCAL_CHECK.search(command):
        return False, "没有跑本地检查：请运行 tbrain check，并把命令和退出码记到验证结果。"
    if code != 0:
        return False, f"本地检查未通过（退出码 {code}）。"
    return True, ""


def gaps(summary) -> list[str]:
    """Missing wrap-up requirements. Empty means the session may be submitted."""
    missing: list[str] = []
    if not summary.items("证据"):
        missing.append("缺少证据：要有至少一条真实证据（文件:行号、命令和输出，或 commit 号）。")
    for item in summary.items("已完成"):
        if "[证据:" not in str(item.get("text") or ""):
            missing.append(f"{item['id']} 没有引用证据。")
    for item in summary.all_items("决策"):
        text = str(item.get("text") or "")
        if not handoff.decision_active(text):
            continue
        if not has_decision_reasons(text):
            missing.append(f"{item['id']} 没有写全决定理由（理由、取舍、决定人）。")
    ok, detail = local_check_recorded(summary)
    if not ok:
        missing.append(detail)
    grants = summary.header.get("permission_grants") or ["auto"]
    if not isinstance(grants, list):
        missing.append("权限记录无法读取。")
        grants = ["auto"]
    actions = summary.header.get("permission_actions") or []
    if not isinstance(actions, list):
        missing.append("权限记录无法读取。")
        actions = []
    for action in actions:
        if not isinstance(action, dict):
            missing.append("权限记录无法读取。")
            continue
        name = str(action.get("action") or "").strip()
        tier = str(action.get("tier") or "").strip() or classify(name)
        if tier not in TIERS:
            missing.append(f"权限级别无法识别：{name or tier}")
            continue
        if not covers(grants, tier):
            allowed = "、".join(str(item) for item in grants) or "无"
            meaning = TIER_TEXT.get(tier, "").rstrip("。")
            detail = f"{tier}（{meaning}）" if meaning else tier
            missing.append(f"权限不够：{name or tier} 需要 {detail}，当前允许 {allowed}。")
    try:
        saved_users = int(summary.header.get("saved_users") or 0)
    except (TypeError, ValueError):
        saved_users = 0
    if len(summary.users) > saved_users:
        missing.append("还有未保存的用户指令，先 save。")
    missing.extend(acceptance.gaps(summary))
    from .progress import gate_gaps

    missing.extend(gate_gaps(summary))
    return missing


def message(gaps_found: list[str]) -> str:
    return "收尾未通过：\n" + "\n".join(f"- {item}" for item in gaps_found)
