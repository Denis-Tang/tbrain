"""Host-neutral hook protocol shared by the four host adapters.

The adapters in ``tbrain.hosts`` normalize each host's raw hook payload into a
:class:`HookEvent`, call :func:`handle`, and render the :class:`HookResult` back
into the host's own JSON shape.  This module owns the semantics; adapters own
the wire formats.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class HookEvent:
    """One normalized host lifecycle event."""

    host: str
    kind: str
    host_session_id: str
    cwd: str = ""
    source: str = ""
    prompt: str = ""
    stop_hook_active: bool = False
    turn_id: str = ""
    context_tokens: int | None = None
    context_window: int | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class HookResult:
    """Neutral hook outcome; adapters translate it into host JSON."""

    additional_context: str = ""
    decision: str = ""
    reason: str = ""
    heartbeat: bool = True


def handle(event: HookEvent, service) -> HookResult:
    """Dispatch one hook event to the service layer."""
    from .session import handle_hook

    return handle_hook(service, event)
