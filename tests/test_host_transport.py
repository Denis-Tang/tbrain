"""真实管道与原生 DSH 事件；全部使用临时目录，不调用模型。"""

import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading

import pytest

from tbrain import session as flow
from tbrain.service import TbrainService


@pytest.fixture
def brain(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"))
    service = TbrainService(tmp_path / "home", vault=tmp_path / "vault")
    service.settings.update({"vault_path": str(tmp_path / "vault"), "ntp_enabled": False})
    return service


def test_mcp_save_returns_without_closing_stdin(brain, tmp_path):
    sid = "claude-code:stdio-regression"
    brain.bootstrap(sid, "claude-code", str(tmp_path))
    process = subprocess.Popen(
        [sys.executable, "-m", "tbrain", "--home", str(brain.home), "mcp"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8")
    replies = queue.Queue()

    def read():
        for line in process.stdout:
            replies.put(json.loads(line))

    threading.Thread(target=read, daemon=True).start()

    def request(message):
        process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
        process.stdin.flush()
        return replies.get(timeout=8) if "id" in message else None

    try:
        assert "result" in request({"id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "transport-test", "version": "1"}}})
        request({"method": "notifications/initialized"})
        for number in (2, 3):
            reply = request({"id": number, "method": "tools/call", "params": {
                "name": "save", "arguments": {"session_id": sid, "operations": [
                    {"op": "append", "section": "done", "items": [{"text": f"实际管道保存 {number}"}]}]}}})
            assert reply["id"] == number and not reply["result"].get("isError")
        assert len(flow.load_session(brain, sid)[0].items("已完成")) == 2
    finally:
        process.kill()
        process.wait(timeout=5)
        process.stdin.close()
        process.stdout.close()


@pytest.mark.skipif(os.name != "nt" or not shutil.which("node"), reason="Windows Node adapter")
def test_dsh_native_lifecycle_through_real_cli(brain, tmp_path):
    entry = tmp_path / "native entry.cmd"
    entry.write_text(f'@echo off\n"{sys.executable}" -m tbrain %*\n', encoding="utf-8")
    driver = tmp_path / "native.mjs"
    driver.write_text(r'''
import { apply } from "__PLUGIN__";
import { spawnSync } from "node:child_process";
const [entry, home, python, cwd] = process.argv.slice(2);
const handlers = {};
const injected = [], steered = [];
const session = {header: {id: "native-root", cwd}};
const agent = {session, inject: value => injected.push(value), steer: value => steered.push(value)};
const roots = [agent];
let tokens = 0;
const ctx = {
  agents: {roots: () => roots},
  tokenMeter: {measure: () => ({totalTokens: tokens})},
  sessionProjections: {stateOf: () => ({contextWindow: 1000})},
  on: (name, handler) => { handlers[name] = handler; },
};
apply(ctx, {command: entry, home});
await handlers["agent/created"]({agent, source: "startup"});
const received = {role: "user", source: {kind: "human"}, content: [{type: "text", text: "验证 DSH 原生会话保存"}]};
const enter = async () => ({kind: "enter", messages: []});
const first = await handlers["agent/pre-step"]({agent, messages: [received]}, enter);
if (!first.messages.some(m => m.content[0].text.includes("dsh:native-root"))) throw Error("missing startup identity");
await handlers["agent/turn-stopping"]({agent});
await handlers["agent/turn-stopping"]({agent});
if (steered.length !== 1) throw Error("save reminder must continue once");
const saved = spawnSync(python, ["-m", "tbrain", "--home", home, "tool", "save"], {
  input: JSON.stringify({session_id: "dsh:native-root", operations: [{op: "append", section: "done", items: [{text: "原生链路完成"}]}]}),
  encoding: "utf8", windowsHide: true,
});
if (saved.status !== 0 || !JSON.parse(saved.stdout).ok) throw Error("CLI save failed");
tokens = 900;
const after = await handlers["agent/pre-step"]({agent, messages: []}, enter);
if (!after.messages.some(m => m.content[0].text.includes("记录已保存"))) throw Error("missing context hint");
await handlers["session/event"](session, {type: "compaction/start"});
if (!injected.some(m => m.content[0].text.includes("压缩前"))) throw Error("missing precompact context");
roots.length = 0;
await handlers["agent/disposed"]({agent});
console.log(JSON.stringify({stop_reminders: steered.length, precompact: injected.length}));
'''.replace("__PLUGIN__", (Path(__file__).resolve().parents[1] / "integrations/tbrain-dsh.mjs").as_uri()), encoding="utf-8")
    result = subprocess.run([shutil.which("node"), str(driver), str(entry), str(brain.home), sys.executable, str(tmp_path)],
                            capture_output=True, text=True, encoding="utf-8", timeout=40)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["stop_reminders"] == 1
    summary, _ = flow.load_session(brain, "dsh:native-root")
    assert len(summary.users) == 1
    assert summary.status == "已中断"
    assert summary.items("已完成")[0]["text"] == "原生链路完成"
