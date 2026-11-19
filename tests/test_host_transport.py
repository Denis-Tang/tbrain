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
    brain.store.save_project('工程', [])
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
                "name": "save", "arguments": {"session_id": sid, "project": '工程' if number == 3 else '', "operations": [
                    {"op": "append", "section": "done", "items": [{"text": f"实际管道保存 {number}"}]}]}}})
            assert reply["id"] == number and not reply["result"].get("isError")
        assert len(flow.load_session(brain, sid)[0].items("已完成")) == 1
        note, = brain.index.sessions('工程', source_session_id=sid)
        assert flow.load_session(brain, note)[0].items("已完成")[0]['text'] == '实际管道保存 3'
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
  get: () => undefined,
  agents: {roots: () => roots},
  tokenMeter: {measure: () => ({totalTokens: tokens})},
  sessionProjections: {snapshot: () => ({values: {contextPressure: {contextWindow: 1000}}})},
  on: (name, handler) => { handlers[name] = handler; },
};
apply(ctx, {command: entry, home});
await handlers["agent/session-start"]({agent, source: "startup"});
const received = {role: "user", source: {kind: "user"}, content: [{type: "text", text: "验证 DSH 原生会话保存"}]};
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


@pytest.mark.skipif(os.name != 'nt' or not shutil.which('node'), reason='Windows DSH runtime')
def test_dsh_plugin_loads_and_captures_only_users_in_installed_runtime(brain, tmp_path):
    runtime = Path.home() / '.dsh/profiles/node_modules/@deepseek-ai'
    if not (runtime / 'dsh-agent-loop/lib/index.js').is_file():
        pytest.skip('DSH runtime is not installed')
    entry = tmp_path / 'runtime entry.cmd'
    entry.write_text(f'@echo off\n"{sys.executable}" -m tbrain %*\n', encoding='utf-8')
    driver = tmp_path / 'runtime.mjs'
    driver.write_text(r'''
import { pathToFileURL } from 'node:url';
const [runtime, plugin, entry, home, cwd] = process.argv.slice(2);
const load = name => import(pathToFileURL(`${runtime}/${name}/lib/index.js`));
const { Context } = await load('cordis');
const { createUserMessage } = await load('dsh-llm');
const ctx = new Context();
try {
  for (const name of ['dsh-agent', 'dsh-session', 'dsh-llm', 'dsh-system-prompt', 'dsh-tools', 'dsh-session-projection', 'dsh-token-meter']) {
    await ctx.plugin((await load(name)).default, {});
  }
  let startupIdentity = false;
  ctx.on('agent/pre-step', async (_, next) => {
    const decision = await next();
    startupIdentity ||= decision.messages.some(m => m.content.some(b => b.text?.includes('session_id: dsh:runtime-root')));
    return {kind: 'reject', reason: 'Stop before any model call'};
  });
  await ctx.plugin(await import(pathToFileURL(plugin)), {command: entry, home});
  await ctx.plugin((await load('dsh-agent-loop')).default, {agents: []});
  const handle = await ctx.agents.create({sessionId: 'runtime-root', meta: {cwd}});
  handle.agent.inject(createUserMessage({source: {kind: 'plugin', plugin: 'other-context'}, content: [{type: 'text', text: '插件背景不是用户指令'}]}));
  for (const text of ['真实DSH用户原话\n第二行保留', '第二条用户要求']) {
    handle.agent.followup(createUserMessage({source: {kind: 'user'}, content: [{type: 'text', text}]}));
    await handle.agent.whenIdle();
  }
  if (!startupIdentity) throw Error('native plugin did not inject the real session identity');
  await handle.dispose();
} finally {
  await ctx.fiber.dispose();
}
''', encoding='utf-8')
    plugin = Path(__file__).resolve().parents[1] / 'integrations/tbrain-dsh.mjs'
    result = subprocess.run([shutil.which('node'), str(driver), str(runtime), str(plugin), str(entry), str(brain.home), str(tmp_path)],
                            capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stderr
    summary, _ = flow.load_session(brain, 'dsh:runtime-root')
    assert [item['text'] for item in summary.users] == ['真实DSH用户原话\n第二行保留', '第二条用户要求']
    assert summary.raw_command == summary.users[0]['text']


@pytest.mark.skipif(os.name != 'nt' or not shutil.which('node'), reason='Windows Node adapter')
def test_pi_extension_tools_through_real_cli(brain, tmp_path):
    brain.store.save_project('Pi工具', [str(tmp_path)])
    brain.store.save_project('日志项目', [])
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
if (!tools.tbrain_save.description.includes('project_prompt') || !tools.tbrain_save.description.includes('rejected') ||
    !tools.tbrain_save.description.includes('supersede')) throw Error('missing save guidance');
let id = 'pi-first';
const ctx = {cwd, sessionManager: {getSessionId: () => id, getSessionFile: () => cwd + '/session.jsonl'},
  getContextUsage: () => ({tokens: 900, contextWindow: 1000})};
let ticks = 0;
const heartbeat = setInterval(() => ticks++, 10);
await handlers.input({text: '验证 pi 的真实保存链路'}, ctx);
clearInterval(heartbeat);
if (!ticks) throw Error('Tbrain CLI blocked the host event loop');
const startup = await handlers.before_agent_start();
if (!startup?.message?.content.includes('pi:pi-first')) throw Error('missing startup');
await handlers.session_before_compact({}, ctx);
await handlers.session_compact({reason: 'threshold', willRetry: false}, ctx);
const continuation = await handlers.agent_before_settle({outcome: 'completed'}, ctx);
if (!continuation?.continue) throw Error('missing post-compact continuation');
const afterCompact = await handlers.before_agent_start();
if (!afterCompact?.message?.content.includes('压缩前重新注入的用户指令') ||
    !afterCompact.message.content.includes('请在本轮结束前调用 save')) throw Error('missing precompact reminder');
await handlers.session_before_compact({}, ctx);
await handlers.session_compact({reason: 'manual', willRetry: false}, ctx);
await new Promise(resolve => setTimeout(resolve, 10));
if (!messages.some(message => message.content.includes('压缩已完成'))) throw Error('missing manual compact reminder');
await handlers.before_agent_start();
const saved = await tools.tbrain_save.execute('save', {operations: [{op: 'append', section: 'done',
  items: [{text: 'pi 保存完成'}]}, {op: 'append', section: 'decision', items: [{text: '超时固定为10秒 | 用户原要求'}]}]}, null, null, ctx);
if (!saved.details.ok) throw Error(JSON.stringify(saved.details));
await handlers.tool_result({toolName: 'bash', input: {command: 'pytest -q'}, details: {exitCode: 0}}, ctx);
const partial = await tools.tbrain_save.execute('partial', {operations: [
  {op: 'replace', section: 'next_step', text: '继续核对'}, {op: 'append', section: 'done', items: [{text: 'x'.repeat(201)}]}
]}, null, null, ctx);
if (partial.details.ok || !partial.details.rejected.length || !partial.details.written.next_step) throw Error('partial result hidden');
await tools.tbrain_save.execute('checkpoint', {operations: [{op: 'replace', section: 'next_step', text: '核对通过'}]}, null, null, ctx);
await handlers.agent_end({}, ctx);
if (!messages.some(message => message.content.includes('记录已保存'))) throw Error('missing context hint');
messages.length = 0;
await handlers.input({text: '第一轮'}, ctx);
await handlers.agent_end({}, ctx);
if (messages.some(message => message.content.includes('请调用 save'))) throw Error('reminded too early');
await handlers.input({text: '第二轮'}, ctx);
await handlers.agent_end({}, ctx);
await handlers.input({text: '第三轮'}, ctx);
await handlers.agent_end({}, ctx);
if (!messages.some(message => message.content.includes('请调用 save'))) throw Error('missing three-round reminder');
await tools.tbrain_save.execute('checkpoint', {operations: [{op: 'replace', section: 'next_step', text: '继续'}]}, null, null, ctx);
messages.length = 0;
await handlers.input({text: '修改文件'}, ctx);
await handlers.tool_result({toolName: 'edit', input: {}, isError: false}, ctx);
await handlers.agent_end({}, ctx);
if (!messages.some(message => message.content.includes('请调用 save'))) throw Error('missing edit reminder');
id = 'pi-second';
await handlers.input({text: '超时改为20秒，明确取代旧的10秒决定'}, ctx);
const resumed = await tools.tbrain_resume.execute('resume', {target: 'pi:pi-first'}, null, null, ctx);
if (!resumed.details.ok || !resumed.details.handoff.includes('pi 保存完成')) throw Error(JSON.stringify(resumed.details));
const updated = await tools.tbrain_save.execute('retire', {operations: [
  {op: 'append', section: 'decision', items: [{text: '超时固定为20秒 | 用户明确更新'}]},
  {op: 'set_status', status: '进行中', supersede: [`[[${resumed.details.resumed_from}]]#K1`]}
]}, null, null, ctx);
if (!updated.details.ok || updated.details.written.decisions.length !== 2) throw Error(JSON.stringify(updated.details));
const afterRetirement = await tools.tbrain_resume.execute('resume-again', {target: 'pi:pi-first'}, null, null, ctx);
if (afterRetirement.details.handoff.includes('超时固定为10秒')) throw Error('retired decision leaked into handoff');
await handlers.input({text: '本项目日志只保存在本地'}, ctx);
await handlers.tool_result({toolName: 'edit', input: {}, isError: false}, ctx);
const longTerm = await tools.tbrain_save_decision.execute('long-term', {
  text: '本项目日志只保存在本地', source: ['U2'], project: '日志项目'
}, null, null, ctx);
if (!longTerm.details.ok || longTerm.details.written.id !== 'DL-0001') throw Error(JSON.stringify(longTerm.details));
if (longTerm.details.written.why !== '') throw Error('invented decision reason');
if (longTerm.details.written.project !== '日志项目') throw Error('wrong decision project');
messages.length = 0;
await handlers.agent_end({}, ctx);
if (!messages.some(message => message.content.includes('请调用 tbrain_save'))) throw Error('long-term decision hid unsaved progress');
const routed = await tools.tbrain_save.execute('routed', {project: '日志项目', operations: [
  {op: 'replace', section: 'next_step', text: '复核本地日志'}
]}, null, null, ctx);
if (routed.details.session.project !== '日志项目') throw Error('wrong progress project');
console.log('OK');
''', encoding='utf-8')
    source = Path(__file__).resolve().parents[1] / 'integrations/pi/tbrain.ts'
    result = subprocess.run([shutil.which('node'), str(driver), str(source), str(entry), str(brain.home), str(tmp_path)],
                            capture_output=True, text=True, encoding='utf-8', timeout=40)
    assert result.returncode == 0, result.stderr
    summary, _ = flow.load_session(brain, 'pi:pi-first')
    assert summary.header['last_verify']['command'] == 'pytest -q'
    assert summary.header['host_record'].endswith('/session.jsonl')
    assert summary.all_items('决策')[0]['text'].endswith('（被取代）')
    assert not summary.active_decisions()
    assert len(brain.index.sessions()) == 3
    assert brain.index.sessions('日志项目')[0]['next_step'] == '复核本地日志'


@pytest.mark.skipif(os.name != 'nt' or not shutil.which('node'), reason='Windows Node adapter')
def test_pi_extension_input_keeps_only_user_sources(brain, tmp_path):
    entry = tmp_path / 'pi entry.cmd'
    entry.write_text(f'@echo off\n"{sys.executable}" -m tbrain %*\n', encoding='utf-8')
    driver = tmp_path / 'pi-input.mjs'
    driver.write_text(r'''
import { readFileSync } from 'node:fs';
import { stripTypeScriptTypes } from 'node:module';
const [source, entry, home, cwd] = process.argv.slice(2);
process.env.TBRAIN_ENTRY = entry;
process.env.TBRAIN_HOME = home;
let code = stripTypeScriptTypes(readFileSync(source, 'utf8'));
code = code.replace('import { Type } from "typebox";', 'const Type = new Proxy({}, { get: () => () => ({}) });');
const { default: extension } = await import('data:text/javascript;base64,' + Buffer.from(code).toString('base64'));
const handlers = {};
extension({ on: (name, fn) => handlers[name] = fn, registerTool: () => {} });
const ctx = { cwd, sessionManager: { getSessionId: () => 'input-source', getSessionFile: () => '' } };
await handlers.input({ text: '扩展注入的确认', source: 'extension' }, ctx);
if (await handlers.before_agent_start()) throw Error('extension input created a user session');
await handlers.input({ text: '用户交互原话', source: 'interactive' }, ctx);
if (!(await handlers.before_agent_start())?.message?.content.includes('pi:input-source')) throw Error('lock wait lost hook response');
await handlers.input({ text: '用户RPC原话', source: 'rpc' }, ctx);
await handlers.input({ text: '扩展再次注入', source: 'extension' }, ctx);
console.log('OK');
''', encoding='utf-8')
    source = Path(__file__).resolve().parents[1] / 'integrations/pi/tbrain.ts'
    locked = threading.Event()
    release = threading.Event()

    def hold_vault_lock():
        with brain.store.write_lock():
            locked.set()
            release.wait(9)

    holder = threading.Thread(target=hold_vault_lock)
    holder.start()
    assert locked.wait(3)
    try:
        result = subprocess.run([shutil.which('node'), str(driver), str(source), str(entry), str(brain.home), str(tmp_path)],
                                capture_output=True, text=True, encoding='utf-8', timeout=20)
    finally:
        release.set()
        holder.join(timeout=3)
    assert result.returncode == 0, result.stderr
    summary, _ = flow.load_session(brain, 'pi:input-source')
    assert [item['text'] for item in summary.users] == ['用户交互原话', '用户RPC原话']


@pytest.mark.skipif(not shutil.which('node'), reason='Node TypeScript text contract')
def test_save_descriptions_share_project_prompt_spec_without_running_hosts(tmp_path):
    import asyncio
    from tbrain import prompt
    from tbrain.mcp_server import create_server

    driver = tmp_path / 'pi-descriptions.mjs'
    driver.write_text(r'''
import { readFileSync } from 'node:fs';
import { stripTypeScriptTypes } from 'node:module';
let code = stripTypeScriptTypes(readFileSync(process.argv[2], 'utf8'));
code = code.replace('import { Type } from "typebox";', 'const Type = new Proxy({}, { get: () => () => ({}) });');
const { default: extension } = await import('data:text/javascript;base64,' + Buffer.from(code).toString('base64'));
const tools = {};
extension({ on: () => {}, registerTool: tool => tools[tool.name] = tool });
console.log(JSON.stringify({ save: tools.tbrain_save.description, search: tools.tbrain_search.description,
  decision: tools.tbrain_save_decision.description, read: tools.tbrain_read.description }));
''', encoding='utf-8')
    source = Path(__file__).resolve().parents[1] / 'integrations/pi/tbrain.ts'
    result = subprocess.run([shutil.which('node'), str(driver), str(source)],
                            capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stderr
    pi_descriptions = json.loads(result.stdout)
    mcp_tools = asyncio.run(create_server(service=object()).list_tools())
    mcp_descriptions = {tool.name: tool.description for tool in mcp_tools}
    assert set(mcp_descriptions) == {'bootstrap', 'search', 'read', 'save', 'save_decision', 'resume'}
    for description in (pi_descriptions['read'], mcp_descriptions['read']):
        assert 'next_start' in description and '不接续任务' in description
    decision_schema = next(tool.inputSchema for tool in mcp_tools if tool.name == 'save_decision')
    assert set(decision_schema['required']) == {'session_id', 'text', 'source'}
    assert decision_schema['properties']['source']['items']['type'] == 'string'
    for description in (pi_descriptions['decision'], mcp_descriptions['save_decision']):
        assert '长期决定' in description and 'DL-xxxx' in description and '未说明留空' in description
    assert prompt.PROJECT_PROMPT_SPEC in pi_descriptions['save']
    assert prompt.PROJECT_PROMPT_SPEC in mcp_descriptions['save']
    for description in (pi_descriptions['search'], mcp_descriptions['search']):
        assert '全局共享（读取非项目会话）' in description
        assert '开启' in description
        assert '公共池命中附sha，更新已有卡时作为env的base_sha。' in description
    for description in (pi_descriptions['save'], mcp_descriptions['save']):
        assert '"base_sha":{"当前阶段":"ab12cd34"}' in description
        assert 'head.text' in description and 'head.sha' in description


@pytest.mark.parametrize('host', ['codex', 'pi'])
def test_host_writes_require_read_bases_and_keep_conflict_heads(brain, tmp_path, host):
    import re
    from tbrain.store import public_card_sha, split_front_matter

    brain.configure_project('条件写入', [])
    sid = host + ':write-bases'
    package = brain.bootstrap(sid, host, str(tmp_path), project='条件写入')['package']
    bases = dict(re.findall(r'^## (.+?) 〔sha:([0-9a-f]{8})〕$', package, re.MULTILINE))
    assert len(bases) == 4
    missing = brain.save(sid, [{'op': 'project_prompt', 'sections': {'当前阶段': '不能覆盖空标题'}}])
    assert missing['rejected'][0]['reason'] == '缺少 base_sha'
    assert missing['rejected'][0]['head'] == {'sha': bases['当前阶段'], 'text': ''}
    first = brain.save(sid, [{'op': 'project_prompt', 'sections': {'当前阶段': '第一阶段：验证来源'},
                              'base_sha': {'当前阶段': bases['当前阶段']}}])
    assert first['written']['project_prompt'] == {'当前阶段': '第一阶段：验证来源'}
    second = brain.save(sid, [{'op': 'project_prompt',
                               'sections': {'当前阶段': '旧基准的另一份提议', '最终目标': '独立章节仍可更新'},
                               'base_sha': bases}])
    assert second['written']['project_prompt'] == {'最终目标': '独立章节仍可更新'}
    assert second['rejected'][0]['head']['text'] == '第一阶段：验证来源'

    assert brain.save(sid, [{'op': 'env', 'items': [{'type': '工具', 'name': '条件写入工具', 'body': '第一份正文'}]}])['ok']
    hit = next(hit for hit in brain.search('条件写入工具', sid)['results'] if hit['kind'] == 'public')
    path = brain.vault_root() / hit['path']
    assert hit['sha'] == public_card_sha(path.read_text(encoding='utf-8'))
    missing = brain.save(sid, [{'op': 'env', 'items': [{'type': '工具', 'name': '条件写入工具', 'body': '漏基准提议'}]}])
    assert missing['rejected'][0]['head']['sha'] == hit['sha']
    assert missing['rejected'][0]['reason'] == '缺少 base_sha'
    update = {'type': '工具', 'name': '条件写入工具', 'body': '第二份正文', 'base_sha': hit['sha']}
    assert brain.save(sid, [{'op': 'env', 'items': [update]}])['ok']
    assert 'base_sha' not in split_front_matter(path.read_text(encoding='utf-8'))[0]
    conflict = brain.save(sid, [{'op': 'env', 'items': [{**update, 'body': '旧基准卡片提议'}]}])
    assert conflict['rejected'][0]['conflict']
    assert '第二份正文' in conflict['rejected'][0]['head']['text']
    assert Path(conflict['rejected'][0]['conflict_path']).is_file()
