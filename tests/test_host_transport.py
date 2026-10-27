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
        [sys.executable, "-m", "tbrain", "--home", str(brain.home), "mcp", '--agent', 'claude-code'],
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
        reply = request({"id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "transport-test", "version": "1"}}})
        from tbrain import __version__
        assert reply["result"]["serverInfo"]["version"] == __version__
        request({"method": "notifications/initialized"})
        for number in (2, 3):
            reply = request({"id": number, "method": "tools/call", "params": {
                "name": "save", "arguments": {"session_id": sid, "operations": [
                    {"op": "append", "section": "done", "items": [{"text": f"实际管道保存 {number}"}]}]}}})
            assert reply["id"] == number and not reply["result"].get("isError")
        assert len(flow.load_session(brain, sid)[0].items("已完成")) == 2
        from tbrain.hosts import config
        config.clear_connect_state(brain.home, 'claude-code')
        reply = request({'id': 4, 'method': 'tools/call', 'params': {'name': 'search', 'arguments': {'query': '管道'}}})
        payload = reply['result'].get('structuredContent') or json.loads(reply['result']['content'][0]['text'])
        assert payload['ok'] is False and '已关闭' in payload['error']
        assert brain.hook('claude-code', {'hook_event_name': 'UserPromptSubmit', 'session_id': 'stdio-regression',
                                        'cwd': str(tmp_path), 'prompt': '关闭后不应记录'}) == {}
        config.write_connect_state(brain.home, 'claude-code', {})
        reply = request({'id': 5, 'method': 'tools/call', 'params': {'name': 'search', 'arguments': {'query': '管道'}}})
        payload = reply['result'].get('structuredContent') or json.loads(reply['result']['content'][0]['text'])
        assert payload['ok'] is True
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
  sessionProjections: {snapshot: () => ({values: {contextPressure: {contextWindow: 1000}}})},
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
await handlers["tools/post-execute"]({agent, name: "pwsh", arguments: {command: "pytest -q"}},
  {content: [{type: "text", text: "1 passed"}]}, async () => ({kind: "continue"}));
await handlers["tools/post-execute"]({agent, name: "pwsh", arguments: {command: "pytest -q"}},
  {isError: true, content: [{type: "text", text: "1 failed\n[exit code: 1]"}]}, async () => ({kind: "continue"}));
const checkpoint = spawnSync(python, ["-m", "tbrain", "--home", home, "tool", "save"], {
  input: JSON.stringify({session_id: "dsh:native-root", operations: [{op: "replace", section: "next_step", text: "修复失败测试"}]}),
  encoding: "utf8", windowsHide: true,
});
if (!JSON.parse(checkpoint.stdout).ok) throw Error("checkpoint failed");
await handlers["tools/post-execute"]({agent, name: "pwsh", arguments: {command: "pytest -q"}},
  {content: [{type: "text", text: "unknown exit status"}]}, async () => ({kind: "continue"}));
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
    assert summary.header['last_verify']['command'] == 'pytest -q'
    assert summary.header['last_verify']['exit_code'] == 1


@pytest.mark.skipif(os.name != 'nt' or not shutil.which('node'), reason='Windows Node adapter')
def test_pi_extension_tools_through_real_cli(brain, tmp_path):
    entry = tmp_path / 'pi entry.cmd'
    entry.write_text(f'@echo off\n"{sys.executable}" -m tbrain %*\n', encoding='utf-8')
    driver = tmp_path / 'pi-driver.mjs'
    driver.write_text(r'''
import { readFileSync } from 'node:fs';
import { stripTypeScriptTypes } from 'node:module';
const [source, entry, home, cwd] = process.argv.slice(2);
process.env.TBRAIN_ENTRY = entry;
process.env.TBRAIN_HOME = home;
let code = stripTypeScriptTypes(readFileSync(source, 'utf8'));
// 宿主负责 schema 校验；这里模拟注册接口，实际执行扩展与 CLI。
code = code.replace('import { Type } from "typebox";', 'const Type = new Proxy({}, { get: () => () => ({}) });');
const {default: extension} = await import('data:text/javascript;base64,' + Buffer.from(code).toString('base64'));
const handlers = {}, tools = {}, messages = [];
extension({on: (name, fn) => handlers[name] = fn, registerTool: tool => tools[tool.name] = tool,
  sendMessage: message => messages.push(message)});
if (!tools.tbrain_save.description.includes('project_prompt') || !tools.tbrain_save.description.includes('rejected')) throw Error('missing save guidance');
let id = 'pi-first';
const ctx = {cwd, sessionManager: {getSessionId: () => id, getSessionFile: () => cwd + '/session.jsonl'},
  getContextUsage: () => ({tokens: 900, contextWindow: 1000})};
await handlers.input({text: '验证 pi 的真实保存链路'}, ctx);
const startup = await handlers.before_agent_start();
if (!startup?.message?.content.includes('pi:pi-first')) throw Error('missing startup');
const saved = await tools.tbrain_save.execute('save', {operations: [{op: 'append', section: 'done',
  items: [{text: 'pi 保存完成'}]}]}, null, null, ctx);
if (!saved.details.ok) throw Error(JSON.stringify(saved.details));
await handlers.tool_result({toolName: 'bash', input: {command: 'pytest -q'}, details: {exitCode: 0}}, ctx);
const partial = await tools.tbrain_save.execute('partial', {operations: [
  {op: 'replace', section: 'next_step', text: '继续核对'}, {op: 'append', section: 'done', items: [{text: 'x'.repeat(201)}]}
]}, null, null, ctx);
if (partial.details.ok || !partial.details.rejected.length || !partial.details.written.next_step) throw Error('partial result hidden');
await tools.tbrain_save.execute('checkpoint', {operations: [{op: 'replace', section: 'next_step', text: '核对通过'}]}, null, null, ctx);
await handlers.agent_end({}, ctx);
if (!messages.some(message => message.content.includes('记录已保存'))) throw Error('missing context hint');
id = 'pi-second';
await handlers.input({text: '接着 pi 的工作继续'}, ctx);
const resumed = await tools.tbrain_resume.execute('resume', {target: 'pi:pi-first'}, null, null, ctx);
if (!resumed.details.ok || !resumed.details.handoff.includes('pi 保存完成')) throw Error(JSON.stringify(resumed.details));
console.log('OK');
''', encoding='utf-8')
    source = Path(__file__).resolve().parents[1] / 'integrations/pi/tbrain.ts'
    result = subprocess.run([shutil.which('node'), str(driver), str(source), str(entry), str(brain.home), str(tmp_path)],
                            capture_output=True, text=True, encoding='utf-8', timeout=40)
    assert result.returncode == 0, result.stderr
    summary, _ = flow.load_session(brain, 'pi:pi-first')
    assert summary.header['last_verify']['command'] == 'pytest -q'
    assert summary.header['host_record'].endswith('/session.jsonl')
    assert len(brain.index.sessions()) == 2
