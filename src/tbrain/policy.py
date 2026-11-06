"""Permission tiers for a project worktree.

Rules come first. This module does not sandbox processes or ports; the wrap-up
hook only checks actions the session recorded against the tiers below.
"""

from __future__ import annotations

TIERS = ("auto", "conditional", "auth-required")

# Higher tiers include the ones below them.
_RANK = {"auto": 1, "conditional": 2, "auth-required": 3}

# Plain-language meaning. Hooks and the work contract quote these, not internal names.
TIER_TEXT = {
    "auto": "读这个工作区里的代码、日志和测试可以直接做。",
    "conditional": "推送分支、开拉取请求、开预览，要先被允许。",
    "auth-required": "生产环境、删除数据、碰密钥，必须明确允许。",
}

_AUTH_MARKERS = (
    "production",
    "prod database",
    "生产环境",
    "生产库",
    "删除数据",
    "delete data",
    "drop table",
    "密钥",
    "secret",
    "凭据",
)
_CONDITIONAL_MARKERS = (
    "git push",
    "push branch",
    "pull request",
    "拉取请求",
    "推送分支",
    "开预览",
    "preview deploy",
    "preview",
)


def classify(action: str) -> str:
    """Map a recorded action to the tier it needs. Unknown actions stay auto."""
    text = str(action or "").casefold()
    if any(marker.casefold() in text for marker in _AUTH_MARKERS):
        return "auth-required"
    if any(marker.casefold() in text for marker in _CONDITIONAL_MARKERS):
        return "conditional"
    return "auto"


def covers(grants: list[str] | tuple[str, ...] | None, tier: str) -> bool:
    """True when the highest granted tier is enough for ``tier``."""
    needed = _RANK.get(str(tier or ""), 0)
    if needed == 0:
        return False
    held = 0
    for grant in grants or []:
        held = max(held, _RANK.get(str(grant or ""), 0))
    return held >= needed
