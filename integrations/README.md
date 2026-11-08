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

- `tbrain-native` 统一处理 `agent/created`、`agent/pre-step`、`agent/turn-stopping`、`session/event` 与 `agent/disposed`；仅记录根 Agent 的用户指令，子代理由根 Agent 汇总。
- 收尾未保存时用 `agent.steer()` 续跑一次，避免重复提醒循环；PreCompact 注入最近用户指令。
- 原生 CLI 响应使用 `additional_context`；MCP 仍提供四个工具，启动参数必须包含 `--home <目录> mcp`。
- `connect dsh` 仅替换 Tbrain 条目，原生插件放在固定应用数据目录；旧官方桥包不再加载，不自动卸载。

### Codex 信任

Codex 的 hook 按内容 hash 记录信任。Tbrain 的 hook 命令指向固定入口，内容在升级时保持不变，所以**只需信任一次**。程序不写 `trusted_hash`、不使用 `--dangerously-bypass-hook-trust`；`doctor` 在信任前显示黄色，并提示在 Codex 中执行 `/hooks`。

### pi 扩展

Pi 扩展异步等待固定入口的 Hook 和工具调用，避免 Tbrain 进程运行时阻塞 Pi 后台线程；返回顺序和原有超时保持不变。

任务模式下，Pi 扩展成功保存 `understanding` 后，在 `ctx.hasUI` 为真时用原生选择框展示完整复述，选项为“继续补充”和“确认”。用户提交“确认”后才保存 `confirm`；取消或选择继续补充时停止执行。没有交互界面时，模型必须结束回复，等待用户单独回复“确认”。其他宿主优先使用各自的原生确认工具，规则与记录方式见 [LOCAL-LOOP](../docs/LOCAL-LOOP.md#首条消息先确认任务理解--confirm-the-task-once)。

`pi/tbrain.ts` 是 pi 的 TypeScript 扩展：扩展在 `session_start` / `input` / `agent_end` / `session_shutdown` / `session_before_compact` 时调用 `tbrain hook --host pi`，并注册 `tbrain_bootstrap` / `tbrain_search` / `tbrain_save` / `tbrain_resume` 四个工具（通过 `tbrain tool <name>` 传 JSON）。上下文用量来自 `ctx.getContextUsage()`。`edit` / `write` 修改文件、shell 运行测试或距上次保存达到三轮时，在下一轮提醒模型保存；压缩后保留压缩前提醒，并请求模型继续处理。成功保存后重置计数；重载扩展也会重置计数，shell 的其他文件修改不会被识别。

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
| `pi/tbrain.ts` | pi 扩展（生命周期 hook + 四个工具） |

会话记忆适配保存模型接续所需的信息，项目联动另向宿主登记项目名称和工作目录。适配不读取宿主完整会话记录、不收集工具原始输出、不写入任何凭据。
