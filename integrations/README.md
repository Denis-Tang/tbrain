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

`pi/tbrain.ts` 是 pi 的 TypeScript 扩展：扩展在 `session_start` / `input` / `agent_end` / `session_shutdown` / `session_before_compact` 时调用 `tbrain hook --host pi`，并注册 `tbrain_bootstrap` / `tbrain_search` / `tbrain_save` / `tbrain_resume` 四个工具（通过 `tbrain tool <name>` 传 JSON）。上下文用量来自 `ctx.getContextUsage()`；pi 没有 Stop-block 等价事件，因此“请保存”提醒降级为下一轮提示（不强制续跑）。

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

所有适配只保存“作为模型可读的会话记忆”所需的信息：不读取宿主完整会话记录、不收集工具原始输出、不写入任何凭据。
