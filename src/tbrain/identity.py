"""会话身份：`宿主名:宿主会话ID`。不同 ID 的新会话与分支分别保存。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib

KNOWN_HOSTS = ("claude-code", "codex", "dsh", "pi", "dsh-native", "unknown")

_ALIASES = {
    "claude": "claude-code",
    "claude code": "claude-code",
    "claudecode": "claude-code",
    "codex-cli": "codex",
    "deepseek-harness": "dsh",
    "oh-dsh": "dsh",
}


@dataclass
class Identity:
    host: str
    host_session_id: str

    @property
    def session_id(self) -> str:
        return build_session_id(self.host, self.host_session_id)


def normalize_host(value: str) -> str:
    text = str(value or "").strip().lower()
    text = _ALIASES.get(text, text)
    return text if text in KNOWN_HOSTS else "unknown"


def build_session_id(host: str, host_session_id: str) -> str:
    return f"{normalize_host(host)}:{str(host_session_id or '').strip()}"


def split_session_id(session_id: str) -> tuple[str, str] | None:
    text = str(session_id or "").strip()
    if ":" not in text:
        return None
    host, _, host_session_id = text.partition(":")
    if not host_session_id:
        return None
    return normalize_host(host), host_session_id


def first_message_hash(text: str) -> str:
    normalized = "\n".join(line.rstrip() for line in str(text or "").replace("\r\n", "\n").split("\n")).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def register(index, identity: Identity, session_id: str, cwd: str, first_message: str, now_iso: str) -> None:
    index.register_link(identity.host, identity.host_session_id, session_id, cwd,
                        first_message_hash(first_message), now_iso)
