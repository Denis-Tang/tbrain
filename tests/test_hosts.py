"""Four host adapters, all isolated in temporary directories.

Every test sets ``CLAUDE_CONFIG_DIR`` / ``CODEX_HOME`` / ``DSH_HOME`` /
``PI_CODING_AGENT_DIR`` / ``TBRAIN_HOME`` / ``TBRAIN_INSTALL_ROOT`` to ``tmp_path``
sub-directories, so a real user configuration is never read or written.
"""

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tbrain import __version__, cli, hosts, installer, paths
from tbrain.hooks import HookResult
from tbrain.hosts import config
from tbrain.settings import SettingsStore

HOST = "claude-code"
_major, _minor, _patch = __version__.split(".")
UPGRADE_VERSION = f"{_major}.{_minor}.{int(_patch) + 1}"
LATER_VERSION = f"{_major}.{_minor}.{int(_patch) + 2}"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def make_package(base: Path, version: str = __version__) -> Path:
    app = base / f"package-{version}"
    (app / "desktop").mkdir(parents=True, exist_ok=True)
    (app / "runtime").mkdir(parents=True, exist_ok=True)
    (app / "_internal" / "tbrain").mkdir(parents=True, exist_ok=True)
    (app / "integrations").mkdir(parents=True, exist_ok=True)
    (app / "tbrain.exe").write_bytes(b"exe-" + version.encode())
    (app / "desktop" / "Tbrain.Desktop.exe").write_bytes(b"desktop")
    (app / "runtime" / "node.exe").write_bytes(b"node")
    (app / "_internal" / "tbrain" / "__init__.py").write_text(
        f'__version__ = "{version}"\n', encoding="utf-8")
    (app / "integrations" / "README.md").write_text("readme", encoding="utf-8")
    return app


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _all_commands(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _tbrain_handlers(payload: dict, event: str) -> list:
    hooks = payload.get("hooks") or {}
    groups = hooks.get(event) or []
    result = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        for handler in group.get("hooks") or []:
            text = str(handler.get("command") or "") + str(handler.get("commandWindows") or "")
            if "tbrain" in text.lower() and " hook --host " in text:
                result.append(handler)
    return result


@pytest.fixture
def iso(tmp_path, monkeypatch):
    """Isolated home/install/host-config roots plus pre-existing user configs."""
    home = tmp_path / "home"
    root = tmp_path / "install"
    claude = tmp_path / "claude"
    codex = tmp_path / "codex"
    dsh = tmp_path / "dsh"
    pi = tmp_path / "pi-agent"
    for name, value in (
        ("TBRAIN_HOME", home),
        ("TBRAIN_INSTALL_ROOT", root),
        ("CLAUDE_CONFIG_DIR", claude),
        ("CODEX_HOME", codex),
        ("DSH_HOME", dsh),
        ("PI_CODING_AGENT_DIR", pi),
    ):
        monkeypatch.setenv(name, str(value))

    installer.install_from_dir(make_package(tmp_path), __version__, root=root)
    entry = installer.entry_command(root)

    claude_settings = {
        "permissions": {"allow": ["Bash(ls)"]},
        "env": {"FOO": "bar"},
        "hooks": {
            "SessionStart": [{"matcher": "startup",
                              "hooks": [{"type": "command", "command": "echo user-start"}]}],
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "echo user-prompt"}]}],
        },
    }
    _write_json(claude / "settings.json", claude_settings)
    claude_mcp = {
        "numStartups": 3,
        "mcpServers": {"other": {"type": "stdio", "command": "other.exe", "args": ["--x"]}},
        "projects": {"C:\\work": {"allowedTools": []}},
    }
    _write_json(claude.parent / ".claude.json", claude_mcp)

    _write_json(codex / "hooks.json", {
        "hooks": {"SessionStart": [{"matcher": "startup", "hooks": [
            {"type": "command", "command": "echo codex-user",
             "commandWindows": "echo codex-user"}]}]},
    })
    (codex / "config.toml").write_text(
        "# keep this comment\n"
        'model = "gpt-test"\n'
        'approval_policy = "never"\n'
        "\n"
        "[mcp_servers.other]\n"
        "command = 'other.exe'\n"
        'args = ["--x"]\n'
        "\n"
        "[hooks.state.'C:\\\\Users\\\\none\\\\old\\\\hooks.json:stop:0:0']\n"
        'trusted_hash = "sha256:deadbeef"\n'
        "\n"
        "[projects.'C:\\\\work']\n"
        'trust_level = "trusted"\n',
        encoding="utf-8",
    )

    (dsh / "profiles" / "desktop").mkdir(parents=True, exist_ok=True)
    (dsh / "profiles" / "desktop" / "cordis.patch.yml").write_text(
        "# 用户自己的插件\n"
        "- insert:\n"
        "    - id: user-plugin\n"
        "      name: 'file:///C:/user/plugin.mjs'\n"
        "\n"
        "- replace:\n"
        "    - id: user-theme\n"
        "      name: 'file:///C:/user/theme.mjs'\n",
        encoding="utf-8",
    )
    _write_json(dsh / "profiles" / "desktop" / "package.json", {
        "name": "dsh-profile",
        "dependencies": {"@deepseek-ai/dsh-hooks-claude-code": "0.2.1-rc.3"},
    })
    _write_json(dsh / "profiles" / "desktop" / "node_modules" / "@deepseek-ai"
                / "dsh-hooks-claude-code" / "package.json",
                {"name": "@deepseek-ai/dsh-hooks-claude-code", "version": "0.2.1-rc.3"})

    _write_json(pi / "settings.json", {"packages": ["npm:pi-web"], "defaultModel": "test"})
    (pi / "extensions").mkdir(parents=True, exist_ok=True)
    (pi / "extensions" / "other.ts").write_text("// other", encoding="utf-8")

    settings = SettingsStore(home).load()
    return SimpleNamespace(
        tmp=tmp_path, home=home, root=root, claude=claude, codex=codex, dsh=dsh, pi=pi,
        claude_settings=claude / "settings.json", claude_mcp=claude.parent / ".claude.json",
        codex_hooks=codex / "hooks.json", codex_config=codex / "config.toml",
        dsh_patch=dsh / "profiles" / "desktop" / "cordis.patch.yml",
        dsh_profile=dsh / "profiles" / "desktop",
        pi_settings=pi / "settings.json", pi_extension=pi / "extensions" / "tbrain.ts",
        entry=entry, settings=settings,
    )


def _call(iso, action: str, host: str, **kwargs):
    getattr(hosts, action)  # validate the public surface exists
    return getattr(hosts, action)(host, iso.home, iso.root, iso.settings, **kwargs)


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------

def test_claude_connect_only_adds_tbrain_entries_and_backs_up(iso):
    settings_before = _read_json(iso.claude_settings)
    mcp_before = _read_json(iso.claude_mcp)

    result = _call(iso, "connect", HOST)
    assert result["ok"] is True
    assert result["entry"] == iso.entry
    assert result["backups"]  # both files were backed up
    assert any(path.startswith(str(iso.claude_settings)) for path in result["backups"])
    assert list(iso.claude.parent.glob(".claude.json.tbrain-*.bak"))
    assert list(iso.claude.glob("settings.json.tbrain-*.bak"))

    settings_after = _read_json(iso.claude_settings)
    # user content untouched
    assert settings_after["permissions"] == settings_before["permissions"]
    assert settings_after["env"] == settings_before["env"]
    assert "echo user-start" in _all_commands(settings_after["hooks"]["SessionStart"])
    assert "echo user-prompt" in _all_commands(settings_after["hooks"]["UserPromptSubmit"])
    # five events now carry exactly one Tbrain handler, pointing at the fixed entry
    for event in config.HOOK_EVENTS:
        handlers = _tbrain_handlers(settings_after, event)
        assert len(handlers) == 1, event
        handler = handlers[0]
        assert iso.entry in handler["command"]
        assert "--host claude-code" in handler["command"]
        assert handler["shell"] == "powershell"
        assert handler["command"].lstrip().startswith("& ")
        assert handler["timeout"] == 10

    mcp_after = _read_json(iso.claude_mcp)
    assert mcp_after["numStartups"] == 3
    assert mcp_after["projects"] == mcp_before["projects"]
    assert mcp_after["mcpServers"]["other"] == mcp_before["mcpServers"]["other"]
    tbrain_server = mcp_after["mcpServers"]["tbrain"]
    assert tbrain_server["command"] == iso.entry
    assert tbrain_server["args"] == ["--home", str(iso.home), "mcp", "--agent", "claude-code"]

    # idempotent: a second connect does not duplicate handlers
    _call(iso, "connect", HOST)
    again = _read_json(iso.claude_settings)
    assert len(_tbrain_handlers(again, "SessionStart")) == 1


def test_claude_disconnect_restores_user_content(iso):
    before = _read_json(iso.claude_settings)
    before_mcp = _read_json(iso.claude_mcp)
    _call(iso, "connect", HOST)
    result = _call(iso, "disconnect", HOST)
    assert result["ok"] is True

    after = _read_json(iso.claude_settings)
    assert after["permissions"] == before["permissions"]
    assert after["env"] == before["env"]
    assert after.get("hooks", {}).get("SessionStart", []) == before["hooks"]["SessionStart"]
    after_mcp = _read_json(iso.claude_mcp)
    assert "tbrain" not in after_mcp.get("mcpServers", {})
    assert after_mcp["mcpServers"]["other"] == before_mcp["mcpServers"]["other"]
    assert after_mcp["projects"] == before_mcp["projects"]


def test_claude_doctor_states(iso):
    item = hosts.doctor(HOST, iso.home, iso.root, iso.settings)
    assert item["id"] == HOST and item["host"] == HOST
    assert item["status"] == "yellow"  # not connected yet

    _call(iso, "connect", HOST)
    item = hosts.doctor(HOST, iso.home, iso.root, iso.settings)
    assert item["status"] == "green", item

    # a host configured against a missing fixed entry is red
    (iso.root / "bin" / "tbrain.cmd").unlink()
    item = hosts.doctor(HOST, iso.home, iso.root, iso.settings)
    assert item["status"] == "red"
    assert "固定入口" in item["detail"]


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------

def _add_codex_trust(config_path: Path, hooks_path: Path) -> None:
    text = config_path.read_text(encoding="utf-8")
    blocks = []
    for event in ("session_start", "user_prompt_submit", "stop", "pre_compact", "session_end"):
        blocks.append(f"[hooks.state.'{hooks_path}:{event}:0:0']\n"
                      f'trusted_hash = "sha256:{event}-hash"\n')
    config_path.write_text(text.rstrip("\n") + "\n\n" + "\n".join(blocks), encoding="utf-8")


def test_codex_connect_writes_hooks_and_mcp_toml(iso):
    before_text = iso.codex_config.read_text(encoding="utf-8")
    result = _call(iso, "connect", "codex")
    assert result["ok"] is True
    assert result["hook_trust"] == "pending"
    assert any("信任" in warning for warning in result["warnings"])

    hooks = _read_json(iso.codex_hooks)
    assert "echo codex-user" in _all_commands(hooks)
    for event in config.HOOK_EVENTS:
        handlers = _tbrain_handlers(hooks, event)
        assert len(handlers) == 1, event
        handler = handlers[0]
        assert iso.entry in handler["command"]
        assert handler["commandWindows"].startswith("& ")
        assert "--host codex" in handler["commandWindows"]
        assert handler["timeout"] == (3 if event == "SessionEnd" else 10)

    after_text = iso.codex_config.read_text(encoding="utf-8")
    assert "# keep this comment" in after_text
    assert 'model = "gpt-test"' in after_text
    assert "[mcp_servers.other]" in after_text
    assert "[mcp_servers.tbrain]" in after_text
    assert iso.entry in after_text
    assert "[hooks.state.'C:\\\\Users\\\\none\\\\old\\\\hooks.json:stop:0:0']" in after_text
    assert config.TOML_MARK_START in after_text
    assert list(iso.codex.glob("config.toml.tbrain-*.bak"))
    assert list(iso.codex.glob("hooks.json.tbrain-*.bak"))

    # hooks.json must stay stable across reconnects (Codex trust is content-hash based)
    first = iso.codex_hooks.read_text(encoding="utf-8")
    _call(iso, "connect", "codex")
    assert iso.codex_hooks.read_text(encoding="utf-8") == first


def test_codex_doctor_pending_then_trusted(iso):
    _call(iso, "connect", "codex")
    item = hosts.doctor("codex", iso.home, iso.root, iso.settings)
    assert item["status"] == "yellow", item
    assert "信任" in item["detail"]

    # Simulate the user approving hooks in Codex: config.toml gains trust records
    # *after* connect, so its mtime is newer than the recorded connect time.
    _add_codex_trust(iso.codex_config, iso.codex_hooks)
    state = config.read_connect_state(iso.home)["codex"]
    stamp = int(state["configured_at_ns"]) + 5_000_000_000
    os.utime(iso.codex_config, ns=(stamp, stamp))
    item = hosts.doctor("codex", iso.home, iso.root, iso.settings)
    assert item["status"] == "green", item

    # hooks.json changing afterwards invalidates the recorded trust again
    hooks = _read_json(iso.codex_hooks)
    hooks["hooks"]["SessionStart"][0]["hooks"].append({"type": "command", "command": "echo extra"})
    _write_json(iso.codex_hooks, hooks)
    item = hosts.doctor("codex", iso.home, iso.root, iso.settings)
    assert item["status"] == "yellow"


def test_codex_disconnect_removes_trust_and_keeps_user_toml(iso):
    _call(iso, "connect", "codex")
    _add_codex_trust(iso.codex_config, iso.codex_hooks)
    result = _call(iso, "disconnect", "codex")
    assert result["ok"] is True

    hooks = _read_json(iso.codex_hooks)
    assert "echo codex-user" in _all_commands(hooks)
    for event in config.HOOK_EVENTS:
        assert _tbrain_handlers(hooks, event) == []

    text = iso.codex_config.read_text(encoding="utf-8")
    assert "[mcp_servers.tbrain]" not in text
    assert "[mcp_servers.other]" in text
    assert "# keep this comment" in text
    # our trust records go away with the hooks, unrelated trust records stay
    assert str(iso.codex_hooks) not in text
    assert "[hooks.state.'C:\\\\Users\\\\none\\\\old\\\\hooks.json:stop:0:0']" in text
    assert "[projects.'C:\\\\work']" in text


# ---------------------------------------------------------------------------
# DeepSeek Harness
# ---------------------------------------------------------------------------

def test_dsh_connect_patch_and_native_plugin(iso):
    result = _call(iso, "connect", "dsh")
    assert result["ok"] is True
    assert result["profile"] == "desktop"

    text = iso.dsh_patch.read_text(encoding="utf-8")
    assert "用户自己的插件" in text
    assert "user-plugin" in text
    assert "user-theme" in text
    assert config.YAML_MARK_START in text
    assert list(iso.dsh_profile.glob("cordis.patch.yml.tbrain-*.bak"))

    data = yaml.safe_load(text)
    assert isinstance(data, list)
    ids = [entry.get("id") for block in data if isinstance(block, dict)
           for entry in (block.get("insert") or []) if isinstance(entry, dict)]
    assert "tbrain-mcp" in ids and "tbrain-native" in ids
    assert "tbrain-cc-hooks" not in ids
    assert "user-plugin" in ids

    entries = {entry["id"]: entry for block in data if isinstance(block, dict)
               for entry in (block.get("insert") or []) if isinstance(entry, dict)}
    assert entries["tbrain-mcp"]["name"] == "@deepseek-ai/dsh-mcp-client"
    assert entries["tbrain-mcp"]["config"]["command"] == iso.entry
    assert entries["tbrain-mcp"]["config"]["args"] == ["--home", str(iso.home), "mcp", "--agent", "dsh"]
    assert entries["tbrain-native"]["name"].startswith("file:///")
    assert "tbrain-dsh.mjs" in entries["tbrain-native"]["name"]
    assert entries["tbrain-native"]["config"]["command"] == iso.entry
    assert entries["tbrain-native"]["config"]["home"] == str(iso.home)

    native = paths.integrations_dir(iso.home) / "tbrain-dsh.mjs"
    assert native.is_file()
    assert (paths.integrations_dir(iso.home) / "package.json").is_file()

    item = hosts.doctor("dsh", iso.home, iso.root, iso.settings)
    assert item["status"] == "green", item


def test_dsh_doctor_yellow_before_connect_and_red_for_old_residue(iso):
    item = hosts.doctor("dsh", iso.home, iso.root, iso.settings)
    assert item["status"] == "yellow"

    _call(iso, "connect", "dsh")
    text = iso.dsh_patch.read_text(encoding="utf-8")
    text += "\n- insert:\n    - id: shared-brain-mcp\n      name: '@deepseek-ai/dsh-mcp-client'\n"
    iso.dsh_patch.write_text(text, encoding="utf-8")
    item = hosts.doctor("dsh", iso.home, iso.root, iso.settings)
    assert item["status"] == "red"
    assert "Shared Brain" in item["detail"]


def test_dsh_reconnect_preserves_user_plugin_inside_old_markers(iso):
    _call(iso, "connect", "dsh")
    text = iso.dsh_patch.read_text(encoding="utf-8")
    user = "- id: user-added-later\n  name: user-plugin\n  config:\n    value: !!js '() => 42'\n"
    text = text.replace(config.YAML_MARK_END, user + config.YAML_MARK_END)
    text = text.replace(f'args: {json.dumps(["--home", str(iso.home), "mcp", "--agent", "dsh"])}', 'args: []')
    iso.dsh_patch.write_text(text, encoding="utf-8")
    assert hosts.doctor("dsh", iso.home, iso.root, iso.settings)["status"] == "yellow"
    _call(iso, "connect", "dsh")
    assert user in iso.dsh_patch.read_text(encoding="utf-8")
    assert hosts.doctor("dsh", iso.home, iso.root, iso.settings)["status"] == "green"
    _call(iso, "disconnect", "dsh")
    assert user in iso.dsh_patch.read_text(encoding="utf-8")


def test_dsh_disconnect_removes_only_tbrain_entries(iso):
    _call(iso, "connect", "dsh")
    result = _call(iso, "disconnect", "dsh")
    assert result["ok"] is True
    text = iso.dsh_patch.read_text(encoding="utf-8")
    assert "user-plugin" in text and "user-theme" in text
    assert "tbrain-mcp" not in text
    assert config.YAML_MARK_START not in text
    assert not (paths.integrations_dir(iso.home) / "tbrain-dsh.mjs").exists()
    # the user's YAML is still valid after removal
    assert isinstance(yaml.safe_load(text), list)


# ---------------------------------------------------------------------------
# pi
# ---------------------------------------------------------------------------

def test_pi_connect_installs_extension_without_touching_settings(iso):
    before = iso.pi_settings.read_bytes()
    result = _call(iso, "connect", "pi")
    assert result["ok"] is True
    assert iso.pi_extension.is_file()
    assert "Tbrain pi extension" in iso.pi_extension.read_text(encoding="utf-8")
    assert iso.pi_settings.read_bytes() == before  # pi settings are never modified
    assert (iso.pi / "extensions" / "other.ts").is_file()

    item = hosts.doctor("pi", iso.home, iso.root, iso.settings)
    assert item["status"] == "green", item

    _call(iso, "disconnect", "pi")
    assert not iso.pi_extension.exists()
    assert iso.pi_settings.read_bytes() == before
    item = hosts.doctor("pi", iso.home, iso.root, iso.settings)
    assert item["status"] == "yellow"


def test_pi_extension_uses_fixed_entry_commands(iso):
    _call(iso, "connect", "pi")
    text = iso.pi_extension.read_text(encoding="utf-8")
    assert "tbrain.cmd" in text
    assert "hook --host pi" in text or '"hook", "--host", HOST' in text or '"--host", "pi"' in text
    assert "tool" in text and "registerTool" in text
    assert "getContextUsage" in text


# ---------------------------------------------------------------------------
# normalize / render
# ---------------------------------------------------------------------------

CLAUDE_START = {"session_id": "uuid-1", "transcript_path": "C:\\t.jsonl", "cwd": "D:\\work",
                "hook_event_name": "SessionStart", "source": "resume"}
CODEX_PROMPT = {"session_id": "thread-1", "turn_id": "turn-9", "cwd": "D:\\work",
                "hook_event_name": "UserPromptSubmit", "model": "gpt-fake",
                "permission_mode": "bypassPermissions", "prompt": "ping"}
DSH_STOP = {"session_id": "session-x", "cwd": "D:\\work",
            "hook_event_name": "Stop", "stop_hook_active": False}
PI_START = {"hook_event_name": "SessionStart", "session_id": "pi-1", "cwd": "D:\\w",
            "source": "resume", "context_tokens": 1000, "context_window": 128000}


def test_normalize_maps_each_host_payload():
    event = hosts.normalize("claude", CLAUDE_START)
    assert (event.host, event.kind, event.host_session_id, event.cwd, event.source) == (
        "claude-code", "session-start", "uuid-1", "D:\\work", "resume")

    event = hosts.normalize("codex", CODEX_PROMPT)
    assert event.host == "codex"
    assert event.kind == "user-prompt"
    assert event.prompt == "ping"
    assert event.turn_id == "turn-9"

    event = hosts.normalize("dsh", DSH_STOP)
    assert event.host == "dsh"
    assert event.kind == "stop"
    assert event.stop_hook_active is False

    event = hosts.normalize("pi", PI_START)
    assert event.host == "pi"
    assert event.kind == "session-start"
    assert event.source == "resume"
    assert (event.context_tokens, event.context_window) == (1000, 128000)

    unknown = hosts.normalize("claude", {"session_id": "x", "hook_event_name": "Notification"})
    assert unknown.kind == ""


def test_render_produces_host_shapes():
    context = HookResult(additional_context="请保存")
    block = HookResult(decision="block", reason="本轮尚未 save")

    claude = hosts.render("claude-code", context, "UserPromptSubmit")
    assert claude["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert claude["hookSpecificOutput"]["additionalContext"] == "请保存"
    assert hosts.render("claude-code", block, "Stop") == {"decision": "block", "reason": "本轮尚未 save"}
    assert hosts.render("claude-code", HookResult(), "Stop") == {}
    # Claude Code rejects hookSpecificOutput for PreCompact/SessionEnd; the same
    # text is replayed by the SessionStart(compact) handler instead.
    assert hosts.render("claude-code", context, "PreCompact") == {}
    assert hosts.render("claude-code", context, "SessionEnd") == {}
    assert hosts.render("claude-code", context, "SessionStart")["hookSpecificOutput"]["additionalContext"]

    assert hosts.render("dsh", context, "SessionStart")["additional_context"] == "请保存"
    assert hosts.render("codex", block, "Stop")["decision"] == "block"

    pi_context = hosts.render("pi", context, "UserPromptSubmit")
    assert pi_context == {"additional_context": "请保存", "decision": "", "reason": ""}
    pi_block = hosts.render("pi", block, "Stop")
    assert pi_block["decision"] == "block"
    assert pi_block["reason"] == "本轮尚未 save"


def test_normalize_accepts_aliases_and_rejects_unknown_host():
    assert hosts.normalize("claude", CLAUDE_START).host == "claude-code"
    with pytest.raises(config.HostError):
        hosts.normalize("vim", {})
    assert ["claude-code", "codex", "dsh", "pi"] == [item["key"] for item in hosts.list_hosts()]


# ---------------------------------------------------------------------------
# doctor_all
# ---------------------------------------------------------------------------

def test_doctor_all_structure_and_statuses(iso):
    report = hosts.doctor_all(iso.home, iso.root, iso.settings)
    assert report["ok"] is True  # only yellow items, nothing broken
    items = {item["id"]: item for item in report["items"]}
    assert {"install", "processes", "claude-code", "codex", "dsh", "pi"} <= set(items)
    for item in report["items"]:
        assert item["host"] == item["id"]
        assert item["status"] in {"green", "yellow", "red"}
        assert isinstance(item["detail"], str) and item["detail"]
    assert items["install"]["status"] == "green"
    assert items["processes"]["status"] == "green"
    for host in ("claude-code", "codex", "dsh", "pi"):
        assert items[host]["status"] == "yellow"

    for host in ("claude-code", "codex", "dsh", "pi"):
        _call(iso, "connect", host)
    report = hosts.doctor_all(iso.home, iso.root, iso.settings)
    items = {item["id"]: item for item in report["items"]}
    assert items["claude-code"]["status"] == "green"
    assert items["codex"]["status"] == "yellow"  # trust pending (by design)
    assert items["dsh"]["status"] == "green"
    assert items["pi"]["status"] == "green"


def test_doctor_red_when_current_version_mismatch(iso):
    # current.txt pointing at a version whose files are gone is a broken install
    paths.current_file(iso.root).write_text("9.9.9\n", encoding="utf-8")
    item = hosts.doctor("claude-code", iso.home, iso.root, iso.settings)  # any host item
    assert item["status"] in {"red", "yellow"}  # host item itself may be yellow
    report = hosts.doctor_all(iso.home, iso.root, iso.settings)
    install_item = next(item for item in report["items"] if item["id"] == "install")
    assert install_item["status"] == "red"
    assert report["ok"] is False


# ---------------------------------------------------------------------------
# CLI integration (service-independent commands only)
# ---------------------------------------------------------------------------

def test_cli_isolated_install_connect_doctor_upgrade_uninstall(iso, capsys, tmp_path):
    # --version
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--version"])
    assert exit_info.value.code == 0
    assert __version__ in capsys.readouterr().out

    # install a second version (0.6.1) through the CLI -- directory package
    package_061 = make_package(tmp_path / "upgrade-src", UPGRADE_VERSION)
    assert cli.main(["install", "--from", str(package_061), "--version", UPGRADE_VERSION]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert installer.current_version(iso.root) == UPGRADE_VERSION
    assert json.loads((iso.home / "settings.json").read_text(encoding="utf-8"))["install_root"] == str(iso.root)

    # connect / doctor / disconnect through the CLI
    assert cli.main(["connect", "claude-code"]) == 0
    connected = json.loads(capsys.readouterr().out)
    assert connected["ok"] is True
    assert cli.main(["doctor"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert {item["id"] for item in report["items"]} >= {"install", "processes", "claude-code", "codex", "dsh", "pi"}
    assert cli.main(["disconnect", "claude-code"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    # upgrade through the CLI -- local package, no network
    package_062 = make_package(tmp_path / "upgrade-src-2", LATER_VERSION)
    assert cli.main(["upgrade", "--from", str(package_062), "--version", LATER_VERSION]) == 0
    upgraded = json.loads(capsys.readouterr().out)
    assert upgraded["ok"] is True and upgraded["to"] == LATER_VERSION
    assert installer.current_version(iso.root) == LATER_VERSION

    # uninstall removes program files only
    assert cli.main(["uninstall"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert not (iso.root / "versions").exists()
    assert (iso.home / "settings.json").is_file()
