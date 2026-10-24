"""第 5 步 8 条验收场景的共享夹具：用伪造的宿主 hook 输入驱动真实服务。

不调用真实大模型，也不启动真实宿主：每个宿主只提供其 hook 输入 JSON 形状。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tbrain.service import TbrainService


class HostDriver:
    """把一行“用户消息/生命周期事件”伪装成各宿主真实的 hook 输入。"""

    def __init__(self, brain, host: str):
        self.brain = brain
        self.host = host
        self.service = brain.service

    # -- 宿主原始 payload ---------------------------------------------------
    def _raw(self, session: str, cwd: Path, event: str, **fields) -> dict:
        # pi 扩展（integrations/pi/tbrain.ts）发送与 Claude 相同的 hook_event_name 形状。
        payload = {"hook_event_name": event, "session_id": session, "cwd": str(cwd)}
        payload.update(fields)
        return payload

    def call(self, session: str, cwd: Path, event: str, **fields) -> dict:
        return self.service.hook(self.host, self._raw(session, cwd, event, **fields))

    # -- 便捷动作 -----------------------------------------------------------
    def first_message(self, session: str, cwd: Path, message: str) -> str:
        output = self.call(session, cwd, "UserPromptSubmit", prompt=message)
        flat = json.dumps(output, ensure_ascii=False)
        return _context_of(output, flat)

    def message(self, session: str, cwd: Path, message: str) -> dict:
        return self.call(session, cwd, "UserPromptSubmit", prompt=message)

    def start(self, session: str, cwd: Path, source: str = "startup") -> str:
        output = self.call(session, cwd, "SessionStart", source=source)
        return _context_of(output, json.dumps(output, ensure_ascii=False))

    def stop(self, session: str, cwd: Path, active: bool = False) -> dict:
        return self.call(session, cwd, "Stop", stop_hook_active=active)

    def precompact(self, session: str, cwd: Path) -> str:
        output = self.call(session, cwd, "PreCompact")
        return _context_of(output, json.dumps(output, ensure_ascii=False))

    def end(self, session: str, cwd: Path) -> dict:
        return self.call(session, cwd, "SessionEnd", reason="other")

    def context(self, session: str, cwd: Path, tokens: int, window: int) -> str:
        output = self.call(session, cwd, "Context", context_tokens=tokens, context_window=window)
        return _context_of(output, json.dumps(output, ensure_ascii=False))


def _context_of(output: dict, flat: str) -> str:
    if not isinstance(output, dict):
        return ""
    if "hookSpecificOutput" in output:
        return str(output["hookSpecificOutput"].get("additionalContext") or "")
    return str(output.get("additional_context") or output.get("additionalContext") or "")


class Brain:
    def __init__(self, service: TbrainService, vault: Path, root: Path):
        self.service = service
        self.vault = vault
        self.root = root

    def host(self, host: str) -> HostDriver:
        return HostDriver(self, host)

    def workdir(self, name: str) -> Path:
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def name_of(self, session_id: str) -> str:
        self.service.refresh_index()
        row = self.service.index.session(session_id)
        return str(row["name"]) if row else ""

    def row(self, session_id: str):
        self.service.refresh_index()
        return self.service.index.session(session_id)


@pytest.fixture()
def brain(tmp_path, monkeypatch):
    home = tmp_path / "home"
    vault = tmp_path / "vault"
    root = tmp_path / "workspace"
    vault.mkdir()
    root.mkdir()
    monkeypatch.setenv("TBRAIN_HOME", str(home))
    service = TbrainService(home, vault=vault)
    service.settings.update({"vault_path": str(vault), "timezone": "Asia/Shanghai",
                             "ntp_enabled": False, "timeout_hours": 6})
    service.reset_vault()
    return Brain(service, vault, root)
