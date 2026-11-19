# Tbrain 宿主接入文件

本目录是 Tbrain 与四个宿主（Claude Code、Codex、DeepSeek Harness、pi coding agent）对接时用到的全部文件。正常使用不需要手工复制：安装后运行

```text
tbrain install                     # 在解压后的发布包目录运行；装到 %LOCALAPPDATA%\Programs\Tbrain（版本化目录）
tbrain connect claude-code         # 或 codex / dsh / pi
tbrain doctor                      # 逐项检查安装、进程与四宿主状态
```

程序会备份被修改的宿主配置（同目录 `<原文件名>.tbrain-<YYYYmmdd-HHMMSS>.bak`），并且只增删 Tbrain 自己的 hook / MCP / 插件条目，保留用户的其他内容。

## 固定入口（升级不改宿主配置）

所有宿主的命令都指向同一个稳定入口：

```text
<安装目录>\bin\tbrain.cmd
```

它读取同目录的 `current.txt`，执行 `versions\<版本>\tbrain.exe %*`。升级只切换 `current.txt`（并更新 `current` junction），因此**任何宿主的配置在升级后都不需要改**。

Hook 命令形如：

```text
"<安装目录>\bin\tbrain.cmd" hook --host <claude-code|codex|dsh|pi>
```

MCP 命令形如：

```text
"<安装目录>\bin\tbrain.cmd" --home "<应用数据目录>" mcp
```

`hooks.json` 是本目录的**模板**（占位符 `__TBRAIN_ENTRY__`）。`tbrain connect dsh` 会把具体内容写到 `<应用数据目录>\hooks.json`，作为历史兼容文件保留；DSH 0.6.1 不读取该文件；Claude Code 与 Codex 的 hook 由同一个生成器内联写入各自配置文件。

## 四宿主对照

### 新建项目联动

Tbrain 的桌面和 CLI 项目创建入口会读取 `connect-state.json`，向已接入的宿主登记项目，创建结果附在 `workspace_sync` 中，桌面保存提示显示各宿主结果。仅新建项目触发联动。

- Codex 使用 `app-server --stdio` 的 `project/list`、`project/create`，一个项目保留全部关联目录。需要 PATH 中的 Codex 支持这两个接口。同名且目录集合相同的项目直接复用。
- DSH 通过 Tbrain 原生插件调用 `workspaceController`，支持一个目录。更新接入插件后启动 DSH，插件在本机回环地址提供创建入口，端口保存在 Tbrain 应用目录的 `dsh-workspace-port`。已有目录登记直接复用。
- Pi Desktop 的已核查实现把项目根目录保存在界面 localStorage 中，缺少可用的外部项目登记接口，多目录项目也不受支持，因此显示未同步原因。

DSH 多目录、未启动或未更新插件，以及宿主接口失败时，Tbrain 项目仍保存，页面显示对应原因。联动只登记项目，目录应由用户事先准备。已有项目的修改、改名和删除不向宿主传播，失败任务不自动重试。

验收使用隔离的 Codex 数据目录及 DSH 插件 HTTP 链路。正在运行的宿主侧栏即时刷新需要安装后验收。

| 宿主 | 写入的配置 | 生效时机 | 备注 |
|---|---|---|---|
| Claude Code | `~/.claude/settings.json` 的 `hooks`（SessionStart / UserPromptSubmit / Stop / PreCompact / SessionEnd，`shell: powershell`）；`~/.claude.json` 顶层 `mcpServers.tbrain` | 新开会话 | Windows 上必须用全引号 PowerShell 形式，exec 形式会退化 |
| Codex | `~/.codex/hooks.json`（`command` + `commandWindows`，同样 5 个事件）；`~/.codex/config.toml` 的 `[mcp_servers.tbrain]` | 新进程；hook 还需用户信任 | **hook 未信任不会执行**：在 Codex 中执行 `/hooks` 审核并信任，`doctor` 会显示黄色“待信任” |
| DeepSeek Harness | profile patch 两项：`tbrain-mcp` 与 `tbrain-native`；原生插件复制到 `<应用数据目录>/integrations/` | 重开 DSH | 原生事件直接调用 CLI，无 Claude Hook 桥依赖 |
| pi coding agent | `~/.pi/agent/extensions/tbrain.ts` | 新 pi 会话或 `/reload` | pi 无内置 MCP；扩展通过固定入口调用 `hook` 与 `tool` |

### DSH 说明

- `tbrain-native` 统一处理 `agent/session-start`、`agent/pre-step`、`agent/turn-stopping`、`session/event` 与 `agent/disposed`；仅记录根 Agent 的用户指令，子代理由根 Agent 汇总。
- 收尾未保存时用 `agent.steer()` 续跑一次，避免重复提醒循环；PreCompact 注入最近用户指令。
- 原生 CLI 响应使用 `additional_context`；MCP 提供六个工具，包括长期决定入口 `save_decision`，启动参数必须包含 `--home <目录> mcp`。
- `connect dsh` 仅替换 Tbrain 条目，原生插件放在固定应用数据目录；旧官方桥包不再加载，不自动卸载。

### Codex 信任

Codex 的 hook 按内容 hash 记录信任。Tbrain 的 hook 命令指向固定入口，内容在升级时保持不变，所以**只需信任一次**。程序不写 `trusted_hash`、不使用 `--dangerously-bypass-hook-trust`；`doctor` 在信任前显示黄色，并提示在 Codex 中执行 `/hooks`。

### pi 扩展

Pi 扩展异步等待固定入口的 Hook 和工具调用，避免 Tbrain 进程运行时阻塞 Pi 后台线程。Hook 等待 15 秒，长于 Tbrain 的 10 秒锁等待；工具调用仍等待 60 秒。


`pi/tbrain.ts` 是 pi 的 TypeScript 扩展：扩展在 `session_start` / `input` / `agent_end` / `session_shutdown` / `session_before_compact` 时调用 `tbrain hook --host pi`，并注册 `tbrain_bootstrap` / `tbrain_search` / `tbrain_read` / `tbrain_save` / `tbrain_save_decision` / `tbrain_resume` 六个工具（通过 `tbrain tool <name>` 传 JSON）。上下文用量来自 `ctx.getContextUsage()`。`edit` / `write` 修改文件、shell 运行测试或距上次保存达到三轮时，在下一轮提醒模型保存；压缩后保留压缩前提醒，并请求模型继续处理。成功调用 `tbrain_save` 后重置计数；保存长期决定不代表已保存工作进展，不重置提醒。重载扩展也会重置计数，shell 的其他文件修改不会被识别。

## 升级后旧进程

宿主自己缓存 MCP 子进程与 hook 命令：升级后**新会话**使用新版本；旧会话里的旧进程由新版本启动时的进程登记/退出请求处理（`~/.tbrain/processes/<pid>.json`，超时 `taskkill`）。升级后重开宿主或新开会话是最稳妥的做法。

## 手工检查

```text
tbrain doctor                      # 安装、进程、四宿主逐项红黄绿
tbrain connect <host>              # 重新写入并备份
tbrain disconnect <host>           # 只移除 Tbrain 条目
```

| 状态 | 含义 |
|---|---|
| 绿色 | 无需操作 |
| 黄色 | 待用户操作（未连接、Codex hook 待信任、DSH 桥/pi 扩展未安装、旧版本进程仍在运行） |
| 红色 | 需要修复（固定入口缺失、安装不完整、旧 Shared Brain 残留、DSH 原生插件缺失） |

## 文件清单

| 文件 | 用途 |
|---|---|
| `hooks.json` | DSH 桥使用的 Claude 风格 hook 模板（`__TBRAIN_ENTRY__` 占位） |
| `tbrain-dsh.mjs` | DSH 原生 cordis 插件（PreCompact / SessionEnd / 上下文提醒） |
| `pi/tbrain.ts` | pi 扩展（生命周期 hook + 六个工具） |

会话记忆适配保存模型接续所需的信息，项目联动另向宿主登记项目名称和工作目录。适配不读取宿主完整会话记录、不收集工具原始输出、不写入任何凭据。

## Agent 调试与工作流插件

桌面后端和窗口共用 `DesktopAPI`。用独立的 `--home` 和测试知识库调试，可直接传目录路径。每行输入一条 UTF-8 JSON 请求，每行输出同一 `id` 的结果。`methods` 列出本版本的方法和参数。

```powershell
'{"id":1,"method":"methods"}' | .\tbrain.exe --home D:\TbrainDev\home desktop --stdio
```

保持同一进程打开，依次发送以下请求。后台保存返回 `running: true` 时，用同一进程轮询 `project_save_status`，直至 `running: false`，再检查结果。`save_settings` 的 `vault_path` 指向测试库。

```jsonl
{"id":1,"method":"save_settings","args":[{"vault_path":"D:/TbrainDev/vault","ntp_enabled":false}]}
{"id":2,"method":"project_save","args":["样例",["D:/TbrainDev/work"]]}
{"id":3,"method":"project_save_status"}
{"id":4,"method":"projects"}
{"id":5,"method":"project_memory","args":["样例"]}
```

用 `tool` 复现单次记忆调用，用 `hook --host codex` 等入口发送宿主事件，用 `check --path <项目目录>` 运行项目检查。索引错误记录在应用数据目录的 `tbrain.log`，公共卡及项目提示词写入记录见同目录的 `w3-writes.jsonl`。在 PowerShell 里可用 `Get-Content -LiteralPath <日志路径> -Tail 40`。标准输入输出模式启动独立后端实例，后台任务状态属于该实例。页面渲染和系统选择框属于窗口宿主。

验证真实页面时，可把解压包的窗口宿主接到同一个测试后端。以下 Python 示例在 Windows 上运行，工作目录为解压后的 `tbrain` 目录，`home` 为前例中已配置测试库的目录。调试端口仅在这个窗口进程启用。

```python
import os
import subprocess
from pathlib import Path

pkg = Path.cwd()
home = Path("D:/TbrainDev/home")
env = {**os.environ, "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": "--remote-debugging-port=9222"}
with subprocess.Popen([pkg / "tbrain.exe", "--home", home, "desktop", "--stdio"],
                      stdin=subprocess.PIPE, stdout=subprocess.PIPE) as api:
    subprocess.run([pkg / "desktop/Tbrain.Desktop.exe", pkg / "_internal/tbrain/web", home / "webview"],
                   stdin=api.stdout, stdout=api.stdin, env=env, check=True)
    api.stdin.close()
```

另一个测试进程使用 Playwright 的 `chromium.connect_over_cdp("http://127.0.0.1:9222")` 连接此窗口，即可按 DOM 操作、检查页面错误、断言内容或截图。测试结束后退出托盘中的测试窗口。

工作流插件是一个本地 Python 文件。给运行 Hook 的进程设置环境变量 `TBRAIN_WORKFLOW`，值为该文件的绝对路径。插件在首次宿主事件时载入，在普通记忆处理完成后、宿主响应生成前收到规范化的 `HookEvent`、`HookResult` 和当前 `TbrainService`，返回 `HookResult`。例如以下插件在用户消息后附加一条工作提示。

```python
def on_hook(event, result, service):
    if event.kind == "user-prompt":
        result.additional_context += "\n请核对本项目的验收说明。"
    return result
```

清除变量并重启对应进程即可恢复纯记忆流程。仅通过 MCP 工具调用的客户端可自行用六个记忆工具组织工作流。历史会话、任务文本和用户原话仍可用 `read` 查阅。
