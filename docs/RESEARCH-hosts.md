# 宿主插件机制调研（Tbrain 0.6.0 第 1 步）

> 调研日期：2026-10-24
> 范围：Claude Code、Codex（CLI + 桌面版）、DeepSeek Harness（DSH 桌面版）、pi，以及 0.5.2 现有接入/升级实现。
> 方法：官方文档 + 本机已安装包源码 + 隔离实测（临时 HOME/配置目录、自写假 API 与假 MCP stdio server），未使用真模型、未花 token。
> 隔离声明：未修改 `~/.claude`、`~/.claude.json`、`~/.codex`、`~/.pi`、`~/.dsh` 的真实配置；未运行旧 Shared Brain 的 desktop/mcp/hook；未写入旧安装目录、旧数据目录、旧 vault。原始实验产物在 `.research/`。
>
> 来源标记：**[实测]** = 本机隔离实验；**[文档]** = 官方文档（附 URL）；**[源码]** = 本机已安装包/源码内文件与行号；**[推断]** = 由证据推导、未直接验证。

---

## 0. 结论速览

### 0.1 四宿主对照

| 维度 | Claude Code 2.1.132 | Codex CLI 0.148.0 + Desktop | DSH 桌面 44.7.0（引擎 0.2.1-rc.3） | pi 0.84.2 |
|---|---|---|---|---|
| MCP 配置 | `~/.claude.json`（local/user scope）+ 项目 `.mcp.json`；`claude mcp` 命令 | `~/.codex/config.toml` `[mcp_servers.*]`（用户级）+ 项目 `.codex/config.toml`；Desktop 共用 `~/.codex` | `~/.dsh/profiles/<profile>/cordis.patch.yml` 插入 `@deepseek-ai/dsh-mcp-client` 条目；另有 home 级 `$DSH_HOME/cordis.patch.yml` | **无内置 MCP**；插件机制只有扩展（TS） |
| MCP 何时读配置 | 会话启动时读；运行中改文件不生效 | 进程启动时读；运行中不重载 | 配置编辑可 HMR 原地重连（无需重启进程） | N/A |
| hook 配置 | `~/.claude/settings.json` 等 settings 的 `hooks`（user/project/local/plugin） | `~/.codex/hooks.json` 或 `config.toml [hooks]`；**按 hash 信任** | 两条路：官方 Claude hooks 桥（npm `@deepseek-ai/dsh-hooks-claude-code`）或原生 cordis 插件 | `~/.pi/agent/extensions/*.ts`、settings `packages/extensions` |
| hook 生效时机 | 会话启动读；改完需新会话 | 启动读；**新增/改动 hook 需重新信任才执行** | 桥接配置**进程启动读一次**；原生插件随 HMR/重启 | `/reload` 热重载自动发现位置的扩展；settings 改动需重启 |
| MCP 进程生命周期 | 每会话 spawn 一份子进程，会话内复用，会话结束被杀 | 同左（实测 PID 每会话不同、退出即消失） | mcp-client 是 app 级单例插件，进程随宿主 app，不随会话 | 扩展进程内运行，无独立 MCP 进程 |
| resume 的 session_id | **沿用旧 ID**（resume/continue）；fork=新 ID（SessionStart 先报旧 ID 再切新） | **沿用旧 ID**，source=resume | 持久 session id 不变；`agent/created` 的 source 支持 resume（clear/compact 暂无发出方） | **沿用旧 ID**；但 `-p` 下 `session_start.reason` 仍是 startup |
| hook 能拿上下文长度/token | **不能**（payload 无 usage 字段；可解析 transcript_path） | **不能**（有 model 字段；可解析 transcript_path） | 原生插件**能**（`ctx.tokenMeter` + 投影）；Claude 桥 hook payload 无 token | **能**：`ctx.getContextUsage()` 返回 `{tokens, contextWindow, percent}` |
| Stop 阻止结束 | 可：`decision:block` + reason（实测有效） | 可：block + reason 会自动续跑 | 原生 `agent/turn-stopping` + `agent.steer()` 可续一步；桥接把 Stop block 映射为 steer | **无 Stop-block 等价事件** |

### 0.2 对本项目最关键的 8 条结论

1. **“固定入口 + 原地升级”成立**：四个宿主都是“启动时读配置/加载代码”。把 MCP/hook 入口写成稳定路径后，升级只替换程序文件即可；新会话用新版，旧会话仍连旧进程/旧扩展实例，需要重启会话或（Claude）`/mcp` Reconnect、（pi）`/reload`。这与第 3 步设计一致。
2. **session_id 可作跨会话主键**：Claude（resume/continue）、Codex（resume）、pi（`--session`/`-c`）实测都沿用旧 ID；DSH 持久会话 ID 不变。唯一坑：Claude `--fork-session` 时 SessionStart 先触发旧 ID、随后事件才是新 ID，需要在 UserPromptSubmit 后重绑。
3. **上下文长度不是四宿主都拿得到**：pi（`getContextUsage`）与 DSH 原生插件（`ctx.tokenMeter`）可以；Claude/Codex 的 hook payload 里没有 token 字段。原设计“Claude Code 和 DSH 显示换会话提醒”需要修正（见第 6 节 D4）。
4. **DSH 不必抛弃 Claude hook JSON**：官方有 `@deepseek-ai/dsh-hooks-claude-code@0.2.1-rc.3`（与本机 DSH 桌面引擎 0.2.1-rc.3 版本对齐），可把 Claude 风格 `hooks.json` 跑在 DSH 的拦截点上，支持 SessionStart / UserPromptSubmit / PreToolUse / PostToolUse / Stop / SubagentStart / SubagentStop，且能注入模型可见上下文。缺口是没有 PreCompact、SessionEnd 和上下文长度，需要原生小插件补齐（见第 6 节 D1）。
5. **Codex 的 hook 有 hash 信任机制**：新写/改动的 hook 在用户批准前**静默不执行**。`tbrain connect codex` 必须处理信任流程，`doctor` 要有“待用户信任”状态；不能默认用 `--dangerously-bypass-hook-trust`。这会影响第 3 步“doctor 全部绿色”的验收口径（见第 6 节 D2）。
6. **pi 没有内置 MCP**（官方 README 明确 “No MCP”）。第四宿主的接入不能复用 MCP 配置，只能写 TS 扩展（扩展可直接调用 Tbrain CLI，或自己内嵌 MCP 客户端）。需要你确认 pi 接入形态（见第 6 节 D3）。
7. **0.5.2 “升级后旧版本进程还在”的根因已定位**（见第 5.3 节）：MCP 子进程由宿主持有且 0.5.2 不替用户结束宿主进程；更新脚本等 90 秒等不到就放弃；宿主会话缓存工具列表；hook/MCP 返回不带版本号，用户无感；各宿主配置写死安装目录绝对路径。Tbrain 的固定入口 + 版本自检 + 进程登记退出 + doctor 正好逐条对应。
8. **真实接入状态与任务书描述有出入**：Claude Code 当前**没有** Shared Brain 的 MCP/hook 配置（只剩备份文件）；pi 没有 MCP、只有已禁用的旧 brain 遗留扩展；Codex 与 DSH 仍在接入（DSH 的 lifecycle 插件指向的文件已不存在，处于半失效状态）。第 6 步解绑时按实际状态处理。

---

## 1. Claude Code 2.1.132

### 1.1 MCP / hook 配置位置、格式、作用域、生效时机

**MCP [文档]**（https://code.claude.com/docs/en/mcp.md，`## MCP installation scopes`）：

| Scope | 生效范围 | 存于 |
|---|---|---|
| local（默认） | 仅当前项目 | `~/.claude.json` 内该项目路径下的 `mcpServers` |
| project | 当前项目 | 项目根 `.mcp.json` |
| user | 所有项目 | `~/.claude.json` 顶层 `mcpServers` |

条目格式：`{"mcpServers":{"name":{"type":"stdio","command":"...","args":[...],"env":{...},"timeout":...}}}`，另支持 http/sse/ws。另有 `--mcp-config`、`--strict-mcp-config`、managed/plugin 来源；官方写入入口 `claude mcp add/list/get/remove`。**[文档+实测]**

**hooks [文档]**（https://code.claude.com/docs/en/hooks.md，`## Hook locations`）：位置包括 `~/.claude/settings.json`（用户级）、`.claude/settings.json`（项目级）、`.claude/settings.local.json`（项目私有）、managed policy、插件的 `hooks/hooks.json`、Skill/Subagent frontmatter；`--settings` 可一次性注入。结构是三层：event → matcher group → handlers；handler 支持 `type`（command/http/mcp_tool/prompt/agent）、`command`、`args`、`if`、`shell`、`timeout`（秒）、`async`、`onFailure`。**[文档]**

**Windows 坑 [实测]**：exec 形式（`command`=exe + `args` 数组）在 2.1.132 会退化为 shell 拼接且不引用带空格路径，报 `/usr/bin/bash: line 1: C:/Program: No such file or directory`；用带引号的 shell 形式，或加 `"shell":"powershell"` + `& "..."`。**Tbrain 安装 hook 时应统一用 `shell:"powershell"` + 全引号命令。**

**改配置何时生效 [实测]**：MCP 与 hooks 都在会话启动时读取，会话进行中改配置文件**不生效**。实验：单进程 `claude -p --input-format stream-json --output-format stream-json` 连续 3 轮，中途改 `--mcp-config` 与 hooks 文件；第 2、3 轮 `system/init` 的 `mcp_servers`/`tools_count` 不变，hook argv 仍是旧命令。要生效必须**新会话/重启**（`-p` 模式即重跑）。**[文档补充]** 交互式会话可用 `/reload-plugins` 让插件 MCP 立刻连接/断开，失败的 server 可用 `/mcp` Reconnect（2.1.284+ 支持 `/mcp reconnect all`）；`-p` 只能重跑。未发现对 MCP/hook 配置文件的自动热装载机制（`ConfigChange`/`FileChanged` 是通知外部，不是自动装载）。

### 1.2 MCP 进程缓存 / 升级后的旧进程

**[实测]** MCP stdio server 是宿主按会话 spawn 的子进程：会话启动即 spawn（早于首次模型调用）；同会话内多次调用复用同一 PID；跨会话 PID 不同；会话结束被宿主直接终止。**磁盘换新后**：老会话仍连旧进程（内存中旧代码），新会话读新文件、用新版。断旧进程的办法：交互式 `/mcp` Reconnect，或重启会话；`-p` 只能重跑。

→ 对 Tbrain：固定入口 + 原地替换对**新会话**立即生效；旧会话里的旧 MCP 进程只能靠版本自检提醒用户重启。

### 1.3 恢复会话时的 session_id

**[实测]**：

| 场景 | SessionStart `source` | hook/结果里的 session_id |
|---|---|---|
| 新会话 | `startup` | 新 UUID |
| `--resume <id>` | `resume` | **同一个旧 ID**（结果 JSON 也相同） |
| `--continue` | `resume` | **同一个旧 ID** |
| `--resume <id> --fork-session` | 新会话 | SessionStart **先报旧 ID**，随后 UserPromptSubmit/Stop/SessionEnd 为新 UUID；结果 JSON 为新 ID |

→ 用 session_id 做宿主会话主键可行；fork 场景需在 UserPromptSubmit 后按新 ID 重绑。

### 1.4 hook 输入能否拿上下文长度 / token

**[实测] 不能。** payload 字段：
- SessionStart：`session_id, transcript_path, cwd, hook_event_name, source`（本机 2.1.132 实测**没有** `model` 字段，文档说“可能有”）。
- UserPromptSubmit：`...+ permission_mode, prompt`
- Stop：`...+ permission_mode, stop_hook_active, last_assistant_message`
- SessionEnd：`...+ reason`

没有任何 token/context_window/usage 字段。旁证：只有 CLI `--output-format json` 的结果 JSON 里有 usage 与 `modelUsage.<model>.contextWindow`（不在 hook 流里）。**替代 [推断]**：hook 拿 `transcript_path` 自行解析 JSONL 的 assistant usage（transcript 异步写入、可能滞后当前轮）。→ Tbrain 若要在 Claude 上做“换会话提醒”，必须走 transcript 解析或 MCP 侧自采，成本与可靠性都差。

### 1.5 事件、注入能力、退出码、Stop 阻塞

**[文档]** 事件全表（hooks.md `Hook lifecycle`）：`SessionStart / Setup / UserPromptSubmit / UserPromptExpansion / PreToolUse / PermissionRequest / PermissionDenied / PostToolUse / PostToolUseFailure / PostToolBatch / Notification / MessageDisplay / SubagentStart / SubagentStop / TaskCreated / TaskCompleted / Stop / StopFailure / TeammateIdle / InstructionsLoaded / ConfigChange / CwdChanged / DirectoryAdded / FileChanged / WorktreeCreate / WorktreeRemove / PreCompact / PostCompact / PreModelSwitch / PostModelSwitch / Elicitation / ElicitationResult / SessionEnd`。
- matcher：SessionStart→`startup/resume/clear/compact/fork`；SessionEnd→`clear/resume/logout/prompt_input_exit/other`；PreCompact/PostCompact→`manual|auto`；Stop、UserPromptSubmit 无 matcher。
- 注入上下文：`hookSpecificOutput.additionalContext`（SessionStart/SubagentStart/UserPromptSubmit/UserPromptExpansion/PreToolUse/PostToolUse/PostToolUseFailure/PostToolBatch/Stop/SubagentStop/PostModelSwitch），字符串上限 10,000 字符，超出写文件仅给路径+2000 字预览。
- **Stop 可阻止结束 [实测]**：Stop hook 输出 `{"decision":"block","reason":"..."}` 后，同一次 run `num_turns` 1→2，第二次 Stop 的 `stop_hook_active=true`，模型按 reason 继续。Stop 也接受 `additionalContext`。
- 退出码：`exit 2` = 阻断（PreToolUse/UserPromptSubmit/Stop/SubagentStop/PreCompact 等可阻断；SessionStart/SessionEnd/PostToolUse 只能把 stderr 给用户/模型，不阻断）；`exit 0 + stdout JSON` = 结构化决策；其他退出码 = 非阻断错误。

### 1.6 本机真实配置（只读）

- `~/.claude/settings.json`：15 个事件的 hooks 全是 **clawd-desk-pet / token-tracker** 集成（`C:\Program Files\nodejs\node.exe`、`clawd-hook.js`、`notify.cjs` 绝对路径；`shell:"powershell"`；PermissionRequest 是 http → `127.0.0.1:23333`）。**没有 shared-brain 条目**；只剩 `settings.json.shared-brain-20260928.bak` 等备份文件。
- `~/.claude.json`：顶层 `mcpServers` 为空，6 个 project 记录里也没有 mcpServers → **Claude Code 当前未接入旧 Shared Brain**。
- 写死绝对路径的项：node.exe、clawd-hook.js、notify.cjs。→ 这类条目升级 Tbrain 时必须“固定安装路径 + 原地替换”。

### 1.7 对 Tbrain 的注意点

- hook 命令用 PowerShell + 全引号；命令里不要依赖宿主 cwd 之外的环境变量。
- 用 SessionStart 的 `source` 区分 新会话/恢复/压缩后/清空后；`compact` 时重新注入启动包。
- fork 场景要处理“SessionStart 旧 ID → UserPromptSubmit 新 ID”的重绑。
- MCP/hook 都只在会话启动读；`connect` 后提示用户新开会话；`doctor` 无法从 Claude 侧读到运行中会话的真实版本，只能看配置与进程登记。

---

## 2. Codex CLI 0.148.0 + Codex Desktop

### 2.1 MCP / hook 配置位置、格式、生效时机

**hooks [文档]**（https://learn.chatgpt.com/docs/hooks.md；本机 `codex features list` 显示 `hooks stable true`）：
- 发现位置：`~/.codex/hooks.json`、`~/.codex/config.toml` 内联 `[hooks]`、`<repo>/.codex/hooks.json`、`<repo>/.codex/config.toml`、插件 bundle、managed `requirements.toml`。多层叠加，不覆盖。
- 格式：`{"hooks":{"Event":[{"matcher":"regex","hooks":[{"type":"command","command":"...","commandWindows":"...","timeout":秒,"async":true,"statusMessage":"...","additionalContextLimit":2500}]}]}}`；TOML 等价 `[[hooks.SessionStart]]`。**Windows 专用 `commandWindows` 字段**（本机真实配置正是 command + commandWindows 双写）。
- **信任机制（关键）[文档+实测]**：非 managed hook 必须 review+trust，信任记录在 hook 当前 hash 上（`config.toml [hooks.state.'...:session_start:0:0'] trusted_hash=sha256:...`）。新增/改动 hook 会被标记待审并**跳过执行**。实测：同一临时 hooks.json，带 `--dangerously-bypass-hook-trust` 4 个 hook 都写日志；不带时 hooks 完全没跑、日志文件都没生成，只有一条 timeout clamp 警告。
- **MCP [文档+实测]**：`[mcp_servers.<name>]` 在 `~/.codex/config.toml`（用户级）或项目 `.codex/config.toml`；`codex mcp add/list/get/remove`；字段 `command/args/env/env_vars/startup_timeout_sec/tool_timeout_sec/url`。
- 生效时机 [实测+推断]：每次进程启动读 config；MCP server 在会话启动时 spawn；运行中改配置无热装载。Desktop 是 MSIX 长驻 app-server，是否在“新线程”重读 config **未隔离验证**。

### 2.2 MCP 进程 / 升级后的旧进程

**[实测]** 与 Claude 同构：每个 session 起一份 MCP stdio 子进程（`initialize` 的 clientInfo 为 `codex-mcp-client`），session 结束消失，跨 session 不复用。磁盘换新 → 新 session 用新版，旧 session 旧进程；断旧进程 = 结束/重启会话（无 `/mcp` 式热重连文档）。

### 2.3 恢复会话时的 session_id

**[实测]** `codex exec resume <thread_id>`：`thread.started` 的 thread_id 与 SessionStart hook 的 session_id 都是原 ID，`source="resume"`；随后事件同 ID。**[文档]** source 取值 `startup|resume|clear|compact|fork`；fork 线程 source=`fork`。→ 直接做主键。

### 2.4 hook 输入能否拿上下文长度 / token

**[实测] 不能。** payload：SessionStart 多 `model`、`permission_mode`；UserPromptSubmit 多 `turn_id, prompt`；Stop 多 `turn_id, stop_hook_active, last_assistant_message`；SessionEnd 多 `reason`。无 usage/token 字段。`model` 字段可直接用。替代 [推断]：解析 transcript_path（rollout JSONL 有 token_count/usage）。

### 2.5 事件、注入能力、退出码、Stop 阻塞

- 事件：`SessionStart / SessionEnd / UserPromptSubmit / PreToolUse / PermissionRequest / PostToolUse / PreCompact / PostCompact / SubagentStart / SubagentStop / Stop / Interrupt`（另有文档级扩展事件）。matcher：SessionStart→source、SessionEnd→reason、Pre/PostToolUse→tool_name、Pre/PostCompact→manual|auto、Subagent→agent_type；UserPromptSubmit/Stop/Interrupt 忽略 matcher。
- 注入：plain stdout（SessionStart/UserPromptSubmit/SubagentStart 的纯文本作 developer context）、JSON `hookSpecificOutput.additionalContext`、`systemMessage`、`continue`、`stopReason`；`additionalContextLimit` 默认 ≈2500 tokens（超出 spill 到 `<temp>/hook_outputs/...`）。
- **Stop 可阻止结束**：`{"decision":"block","reason":"..."}` → Codex 不结束本轮，把 reason 当新的 user prompt 继续（官方明确 “automatically creates a new continuation prompt”）；`exit 2` + stderr 等价。`continue:false` 优先级更高。
- 退出码：0 无输出=继续；2+stderr=阻断（PreToolUse deny / Stop 继续 / UserPromptSubmit 拦截）；其他=失败但不阻断。超时：默认 600s；SessionEnd/Interrupt 默认 1s、上限 3s（实测 0.148.0 自动 clamp 到 3s 并告警）；`async:true` 后台执行不能阻断。

### 2.6 本机真实配置（只读）

- `~/.codex/config.toml`：`features.hooks=true`；`[mcp_servers.shared_brain]`（旧安装绝对路径 + `--home %USERPROFILE%\.shared-brain mcp`）；`[mcp_servers.deepseek_harness]`；`[mcp_servers.node_repl]`；`notify` 绝对路径；`[hooks.state.*]` 4 条 trusted_hash（session_start/user_prompt_submit/subagent_start/stop）；`[desktop]`、`[plugins.*]` 等 Desktop 写入段。
- `~/.codex/hooks.json`：4 个事件，每 handler 是旧 Shared Brain 的 `native-hooks.mjs`（command + commandWindows 双写，timeout 5）。
- Desktop：MSIX `OpenAI.Codex_2p2nqsd0c76g0` 已安装；Desktop 把 `[desktop]` 配置写进同一个 `~/.codex/config.toml`，`CODEX_CLI_PATH` 指向 `%LOCALAPPDATA%\OpenAI\Codex\bin\<hash>\codex.exe` → 与 CLI 共用 `~/.codex`（同一 CODEX_HOME；配置/插件缓存/sessions 路径一致，**部分推断**）。

### 2.7 对 Tbrain 的注意点

- `connect codex` 写 hooks 后必须处理信任：提示/引导用户执行宿主原生信任（交互式 `/hooks`），或者 `doctor` 明确显示“待信任，hook 不会执行”。**不要默认带 bypass 参数。**
- 升级后 hooks.json 内容不变（固定入口），trusted_hash 才稳定；`connect` 只在首次写一次。若未来必须改 hook 命令内容，用户要重新信任——设计上应避免。
- Windows 必须写 `commandWindows`；Desktop 与 CLI 共用配置，`disconnect` 时不要只清 CLI 侧。

---

## 3. pi 0.84.2

> 本机安装：`@earendil-works/pi-coding-agent@0.84.2`（npm 全局 `pi` 命令）。任务书里写的是 `pi（badlogic/pi-mono）`，本机没有 badlogic/pi-mono 包；需要你确认目标就是这台机器上的 pi（见第 6 节 D3）。

### 3.1 MCP / hook（扩展）配置位置、格式

- **pi 无内置 MCP [源码]**：README `:498` “**No MCP.** Build CLI tools with READMEs ..., or build an extension that adds MCP support.”→ 不存在“pi 的 MCP 进程缓存”；若 Tbrain 要在 pi 上用 MCP，只能由扩展自己 spawn/管理。
- 扩展 = pi 的 hook 机制（无 shell hook JSON 子系统）[源码 docs/extensions.md]：
  - 全局：`~/.pi/agent/extensions/*.ts`、`~/.pi/agent/extensions/*/index.ts`；项目：`.pi/extensions/*.ts`（需项目受信任）。
  - settings `packages`（npm:/git:）与 `extensions`（本地路径）；`pi -e <path>` 显式加载。
  - 配置文件：`~/.pi/agent/settings.json`（全局）、`.pi/settings.json`（项目覆盖，深合并）；`PI_CODING_AGENT_DIR` 可整体替换配置目录（实测隔离即用此）。
  - API：`pi.on("<event>", handler)`；可注册 tool/command/provider/shortcut。
- 生效/重载 [源码]：`/reload` 或 `ctx.reload()` 对自动发现位置生效（`session_start` 以 `reason:"reload"` 重发）；`-e` 显式路径热重载不可靠；settings 改动需重启；`/trust` 明确“当前会话不重载”。

### 3.2 进程复用 / 升级后的旧进程

- MCP：N/A。
- 扩展：pi 每个会话把扩展载入自己的进程（in-process），没有扩展子进程缓存；每次启动 PID 不同，退出即销毁。升级 = 新 pi 进程加载新扩展文件；运行中的 pi 仍是旧版 → 重启或 `/reload`。

### 3.3 恢复会话时的 session_id

**[实测]** `ctx.sessionManager.getSessionId()`：新会话新 UUID；`--session <id>` 与 `-c` 都**沿用旧 UUID**；但 `-p` 模式下 `session_start.reason` 仍报 `"startup"`（不区分 resume，坑）。[文档] reason 取值 `startup|reload|new|resume|fork`；交互式 `/resume` 切换时 reason=resume 且带 previousSessionFile（本次未实测）。

### 3.4 上下文长度 / token

**[实测] 能，且信息最全**：`ctx.getContextUsage()` → `{tokens, contextWindow, percent}`：
- session_start（无内容）：`{tokens:0, contextWindow:128000, percent:0}`
- 一次真实后 agent_end：`{tokens:1002, contextWindow:128000, percent:0.78}`
- 另有 `message_end` 的 `event.message.usage`（输入/输出/cost 明细）、`session_compact` 的 `preparation.tokensBefore`。
→ 与任务书“pi 已知拿不到”的假设相反，**pi 是四宿主里上下文信息最全的**。

### 3.5 事件、注入能力、Stop 阻塞

- 生命周期：`session_start {reason, previousSessionFile?}`、`session_shutdown {reason: quit|reload|new|resume|fork}`、`resources_discover`、`project_trust`；会话切换：`session_before_switch`、`session_before_fork`、`session_before_compact`/`session_compact`、`session_before_tree`/`session_tree`、`session_info_changed`。
- Agent/轮次：`before_agent_start`（**可注入 message、改 system prompt**）、`agent_start`、`agent_end`、`agent_settled`、`turn_start`、`turn_end`。
- 消息/工具：`message_start/update/end`、`context`（**可修改 messages 注入**）、`tool_execution_start/update/end`、`tool_call`（可 `{block:true, reason}` 拦截）、`tool_result`、`user_bash`、`input`（可拦截用户输入）。
- Provider 层：`before_provider_headers`、`before_provider_request`、`after_provider_response`、`model_select`、`thinking_level_select`。
- **没有 Stop 等价物**：事件表里没有“本轮将结束、可阻止”的 hook（`agent_end`/`turn_end` 是通知型）。要实现“自动续跑”只能用扩展在 `agent_end` 后主动注入消息（可靠性待验证）。

### 3.6 本机真实配置（只读）

- `~/.pi/agent/settings.json`：`packages=["npm:pi-web-access","npm:pi-subagents"]`；`defaultProvider:"magpie"`、`defaultModel:"dsh-glm/glm-5.3-flash"`；`compaction{enabled,reserveTokens:16384,keepRecentTokens:20000}`；**`shellPath:"C:/Program Files/Git/bin/bash.exe"`（绝对路径）**。
- `~/.pi/agent/extensions/`：clawd-on-desk 与多个 pi-deck 扩展；`~/.pi/agent/disabled-legacy-brain/dsh-policy.ts`（旧 brain 遗留，已禁用）。
- 无 MCP 配置；旧 Shared Brain 在 pi 侧没有活跃条目。

### 3.7 对 Tbrain 的注意点

- pi 接入必须是 TS 扩展（放 `integrations/pi/`），无法复用 MCP JSON。
- 扩展可直接调用 Tbrain CLI/本地 HTTP，实现 bootstrap/search/save/resume 的等价能力；或者扩展内嵌 MCP 客户端再连 Tbrain MCP。前者更简单、少一层进程。
- `-p` 模式拿不到 `reason:resume`，但 session id 稳定，可凭 ID 接续。
- 无 Stop-block：save 提醒只能退化为“本轮结束后注入一条提示”，或由扩展在 `agent_end` 时 steer。

---

## 4. DeepSeek Harness（本机桌面 44.7.0 / 引擎 0.2.1-rc.3）

> 版本说明：安装目录 `version` 文件为 `44.7.0`（桌面 app 版本）；`resources/app.asar` 内 `@deepseek-ai/dsh-desktop-runtime` 及全部引擎包为 **0.2.1-rc.3**；npm 全局 `dsh` CLI 为 0.1.0-rc.8（旧版，仅用于对照）。**Tbrain 对接目标是桌面引擎 0.2.1-rc.3。**

### 4.1 MCP 配置位置、格式、生效时机

- 位置：`~/.dsh/profiles/desktop/cordis.patch.yml`（profile 用户 patch 层）。DSH 的 MCP 不是独立的 `mcp.json`，而是“每个 server 一个插件实例”：

```yaml
- insert:
    - id: tbrain-mcp
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: tbrain
        transport: stdio
        command: C:\...\Tbrain\bin\tbrain.cmd
        args: []
```

- 加载顺序 [源码 README]：bundle patch → profile `cordis.patch.yml` → home 级 `$DSH_HOME/cordis.patch.yml`。工具名 `mcp__tbrain__<tool>`。
- **生效时机**：`dsh-mcp-client` README 明确 “HMR 支持热替换：编辑配置项会触发断开 + 重新连接，无需重启进程；serverName 不变时生成完全相同的工具名”；profile `package.json` 里本机配置 `dsh.profile.patchReload: "live"`。→ **DSH 是四宿主里唯一改配置可热生效的**（原生插件文件改动也可 HMR 重载）。
- 断线/崩溃：supervisor 指数退避重连（默认 500ms→30s 上限、10 次预算），恢复后重新发现工具；连接中断期间旧工具仍列出但调用失败。

### 4.2 hook 的两条可行路线

#### (a) 官方 Claude Code hooks 桥（可与 Claude/Codex 共用同一份 hooks.json）

- 包：`@deepseek-ai/dsh-hooks-claude-code`，npm 上最新 `next` 为 **0.2.1-rc.3**（与本机桌面引擎版本对齐）。官方描述：“在 agent 运行期间执行你现有 Claude Code hooks.json 或 settings 文件中的 command 钩子”。
- 配置：在 profile patch 插入插件，`configPath` 指向 hooks.json/settings 文件；`pluginRoot`、`projectDir`、`defaultTimeoutMs` 可选：

```yaml
- insert:
    - id: tbrain-cc-hooks
      name: '@deepseek-ai/dsh-hooks-claude-code'
      config:
        configPath: %USERPROFILE%\.tbrain\hooks.json
```

- 支持事件：`SessionStart`、`UserPromptSubmit`、`PreToolUse`、`PostToolUse`、`Stop`、`SubagentStart`、`SubagentStop`。映射：SessionStart→`agent/created`（需等待的注入）、UserPromptSubmit/PreToolUse→waterfall 可拒绝、PostToolUse→`tools/post-execute`、Stop→`agent/turn-stopping` + `steer()` 强制再走一步。
- 注入模型上下文：**可以**。SessionStart/UserPromptSubmit/PostToolUse/SubagentStart 的 hook 输出 `hookSpecificOutput.additionalContext`（或纯文本）会变成本会话模型可见的上下文消息（带来源归因）；Stop 的 block reason 作为 steering 进入下一步。**这是“additionalContext 能否传回模型”的答案：能。**
- hook payload：由桥按 Claude 形状构造，含 `session_id`、字面 `cwd`、`hook_event_name`，以及各事件字段（SessionStart 带 `source`、UserPromptSubmit 带 `prompt` 等）；`transcript_path` 恒为 `''`（持久化 seam 不暴露产物路径）。
- 限制：配置在**进程启动时读取一次**，改 hooks.json 需重启 DSH 或 HMR 重载插件；只支持 command hook；不支持 PreCompact/SessionEnd（DSH 的拦截点里没有对应 pre-compact 事件，SessionEnd 也没有 Claude 形状的事件）；hook payload 里没有 token/上下文长度。
- 版本兼容：桥 0.2.1-rc.3 的 peerDependencies 精确要求 `@deepseek-ai/dsh-* 0.2.1-rc.3`，与本机桌面引擎一致；DSH 升级后需要同步升级桥（doctor 要检查）。

#### (b) DSH 原生插件（cordis 插件，mjs/包）

可用拦截点（来自 `resources/app.asar` 内 `@deepseek-ai/dsh-tool-cordis/lib/types/api-catalog.js`，引擎 0.2.1-rc.3）：

| 事件 | 模式 | 用途 |
|---|---|---|
| `agent/created` | serial | payload `{agent, source: 'startup'\|'resume'\|'clear'\|'compact', signal?}`；**session-start 来源在 0.2.1 并入 created**；`clear`/`compact` 值保留但暂无发出方 |
| `agent/pre-step` | waterfall | payload `{agent, messages, turn, step, signal}`；调用 `next()` 后返回 `{kind:'enter', messages:[...]}` 可**替换进入本步的消息** → 注入启动包/记录用户指令 |
| `agent/turn-stopping` | serial | 轮次将结束时运行；监听器 `agent.steer(message)` 可让模型再走一步 → 等价 Stop block 提醒 |
| `agent/disposed` / `session/disposed` | emit | 会话结束/释放 → 等价 SessionEnd（标“已中断”） |
| `session/event` | emit | 追加事件流：`user/message`、`turn/end`、`compaction/start`、`compaction/end` 等 → 可做 PreCompact 提醒 |
| `tools/post-execute` | waterfall | 观察工具结果（测试命令/退出码） → 自动补“最近验证” |
| `agent/request` | waterfall | 替换模型调用配置（不需要） |

原生插件能力：
- `agent.inject(message)`：把上下文排入下一 step（不唤醒驱动器）；`agent/followup/steer` 见 dsh-agent README。
- 会话 ID：`agent.session.header.id`（格式 `session-<uuid>`）；header keys 含 `version,id,createdAt,cwd,isSeeded`。
- **上下文长度/ token**：`ctx.tokenMeter.measure(session)` 返回 `{totalTokens, surfaceTokens, logRevision, baseline, surfaceDeltaTokens, nodes}`（插件需 `inject: ["tokenMeter"]`）；另有 `contextPressure` 投影（`pressureTokens/projectedTokens/contextWindow`）与 `ctx.llm.resolveModelInfo().context` 拿模型容量。→ **换会话提醒在 DSH 上可做。**

**[实测]（隔离 CLI 环境，假 OpenAI 兼容 server）**：
- DSH CLI 0.1.0-rc.8：`agent/session-start`（source=startup）里 `agent.inject` 的内容出现在模型请求 messages 中；`agent/pre-step` 返回替换后的 messages 也能注入（marker 出现在请求里）；`ctx.tokenMeter.measure` 可调用（初始 `{totalTokens:0,surfaceTokens:0}`）；`session/event` 能看到 `user/message`。
- DSH CLI 0.2.1-rc.3：同一套原生插件改用 `agent/created(source)` 后运行成功（`source=startup`、`inject-ok`、pre-step、session/event 全部观测到；注入 marker 进入模型请求）。**这是 Tbrain 原生插件的事件名依据。**
- **Claude 桥端到端未完成实测**：在 headless profile 里插入桥插件后它未激活（headless 组合缺少/重复注册 `shell`、`workingDirectory` 等同名服务，且插入动作排在 headless-runner 之后）；桌面 profile 具备完整服务，但改真实 profile 需要你批准。桥的能力结论来自其官方 README + 包源码（`apply` 里 `agent.inject(context)`、`agent/pre-step` 折叠 messages）；标记为 **[文档+源码]**，未实测注入。

### 4.3 MCP 进程 / 升级后的旧进程

- mcp-client 是 **app 级单例插件**（一个 server 一个实例），进程随 DSH app 生命周期，不随会话；断线自动重连；配置编辑原地重连。
- 升级 Tbrain 后：DSH app 仍连着旧 `tbrain.exe` 子进程（旧代码）→ 要么改插件配置触发原地重连（会 spawn 新文件），要么重启 DSH。→ 版本自检 + 配置里固定入口缺一不可。

### 4.4 恢复会话时的 session_id

- 持久 session id 不变（会话日志按 id 持久化；`agent/created` 的 `source` 区分 startup/resume）。
- `SessionStartSource = 'startup' | 'resume' | 'clear' | 'compact'`（源码类型）；本机桌面 README 明确 `'clear'/'compact'` “已保留但尚无发出方”，待驱动子系统落地。
- 实测（headless）只观察到 `startup`；resume 场景在桌面 GUI 中恢复同一会话应得到同一 session id（源码层面 `resumeSessionId` 复用会话）。

### 4.5 本机真实配置状态（只读）

`~/.dsh/profiles/desktop/cordis.patch.yml` 第 250~264 行：

```yaml
- insert:
    - id: shared-brain-mcp
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: shared_brain
        transport: stdio
        command: %LOCALAPPDATA%\Programs\Shared Brain\shared-brain.exe
        args: [--home, %USERPROFILE%\.shared-brain, mcp]
    - id: shared-brain-lifecycle
      name: file:///%LOCALAPPDATA%/Programs/DeepSeek%20Harness/agent-mcp/shared-brain-lifecycle.mjs
      config:
        command: ...shared-brain.exe
        home: %USERPROFILE%\.shared-brain
```

- `shared-brain-mcp` 仍在（旧 MCP 会被加载）。
- `shared-brain-lifecycle` 指向的 `...\DeepSeek Harness\agent-mcp\shared-brain-lifecycle.mjs` **在当前安装目录中不存在**（`agent-mcp` 目录不存在）→ 该插件条目加载失败/被跳过，旧 DSH 接入处于半失效状态。第 6 步解绑时应一并清理（需你批准）。
- DSH 的真实配置里没有 Tbrain 任何条目；没有 `$DSH_HOME/cordis.patch.yml`（home 级 patch 文件不存在）。

### 4.6 其他坑（实测）

- **`D:\package.json` 带 BOM**（内容为 `{"name":"demo-target-repo"...}`）会破坏 DSH 的 profile 插件兼容性预检：任何位于 D: 盘、向上找不到更近 package.json 的 file 插件，会报 `its declared dsh requirements cannot be validated: Unexpected token '﻿'` 并被禁用。**给 file 插件目录放一个合法 package.json 即可绕过**；Tbrain 的插件应作为 npm/包或放在含 package.json 的目录中安装。
- `dsh plugin --profile <name> add <pkg>` 走 pnpm，需要可用的 workspace；在隔离环境里因路径错误失败（不影响桌面真实使用，但 `connect dsh` 不要依赖它，直接写 patch + 安装包目录更稳）。
- DSH 的 Claude 桥 `configPath` 是进程启动读一次 → Tbrain 的 hooks.json 内容必须稳定（固定入口），避免升级后要求用户重启 DSH 才能改 hook 命令。

### 4.7 对 Tbrain 的接入建议（待你拍板，见 D1）

推荐 **桥 + 原生小插件** 两件套：
1. 桥（`dsh-hooks-claude-code`）读取与其他宿主共用的 `~/.tbrain/hooks.json`，覆盖 SessionStart / UserPromptSubmit / Stop / PostToolUse（三家共用一份 JSON 的设计目标在 DSH 上实现）。
2. 原生小插件（`tbrain-dsh.mjs`）只补 DSH 独有能力：PreCompact 提醒（`session/event` 的 `compaction/start`）、SessionEnd 标中断（`agent/disposed`/`session/disposed`）、上下文长度与换会话提醒（`tokenMeter`）、工具结果抓取（`tools/post-execute`）。
3. `connect dsh` 写 profile patch（固定入口 + 桥 configPath + 原生插件 file URL/包），先备份；`doctor` 检查引擎版本 0.2.1-rc.3、桥版本、插件是否加载、旧 shared-brain 条目残留。

备选：只装桥（最简单，但缺 PreCompact/SessionEnd/上下文提醒）；或只装原生插件（能力最全，但 DSH 不与其他宿主共用 hooks.json）。

---

## 5. 旧版 0.5.2 的接入与升级（现状与“旧进程”根因）

### 5.1 现在怎么接入宿主

0.5.2 **没有自动配置器**（README：“当前仍使用接入提示词，尚未提供自动修改宿主配置的安装器”）。流程是：桌面生成一段“接入提示词”（`onboarding.build_prompt` / `service.agent_prompt`），交给 agent 或用户手工写入各宿主：

- MCP 配置由 `service.mcp_config()` 生成：`command = 当前运行的 shared-brain.exe 绝对路径`，`args = [--home, <home>, mcp]`。
- hook 统一入口：同一 exe 的 `hook` 子命令；Codex/Claude 通过 `integrations/native-hooks.mjs` 包装（JSON stdin/stdout）；DSH 用 `integrations/deepseek-harness.mjs`（原生 cordis 插件，`file://` 路径写进 profile patch）；旧 Oh-DSH 用 `oh-dsh.mjs`；pi 按 PI-Desktop 设置页手工加 MCP（0.5.2 文档称 PI-Desktop 0.15.10 验证过，与现在本机 `@earendil-works/pi-coding-agent` 不是同一产品）。
- 各宿主配置里写的都是**安装目录绝对路径**（Codex config.toml/hooks.json、Claude ~/.claude.json、DSH profile patch、pi 设置页）。

### 5.2 升级流程（update.py + installer.py + detached 脚本）

1. `update.py` 查 GitHub Releases（`Denis-Tang/second-brain` latest，匿名 API，6 小时缓存，60 秒内不重复），返回版本/资产 URL/SHA256。
2. 下载 ZIP 到应用数据目录，校验 SHA256 后落盘（记录 `updates/<version>/download.json`）；此阶段不碰安装目录。
3. 桌面点“立即更新并重启”→ `service.apply_update` → `installer.apply`：
   - 先 `foreign_processes()`（`tasklist` 过滤 `shared-brain.exe`，排除自己）——**只要有任何 shared-brain.exe 在跑就直接拒绝**，返回消息“请先退出 Codex / Claude Code / DeepSeek Harness 里的 shared_brain 连接（或重启宿主）后重试”。
   - 无占用则解压到同卷 staging（`.<安装名>-staging-<版本>`），生成 `apply-update.ps1` 并以 detached 方式启动。
4. PowerShell 脚本：等待所有 `shared-brain` / `SharedBrain.Desktop` 进程退出（最多 90 秒）→ 不够就抛错放弃（未改动任何文件）→ 旧安装目录改名为 `<安装名>.bak-<当前版本>` → staging 就位 → 运行 `--version` 自检 → 启动新版本 → 成功后删备份；任何失败回滚；状态写 `update-state.json`。
5. 源码运行时（非打包）不支持自动替换，只能把“更新提示词”交给 agent 手工完成（GitHub 下载 → 校验 → 备份目录 → 覆盖回原路径 → 验证版本）。

### 5.3 “升级后旧版本进程还在跑”的具体原因（证据链）

1. **MCP server 是宿主持有的长驻子进程**：Claude/Codex 每个会话 spawn 一份，会话不结束进程不死（实测）；DSH 是 app 级单例，只要 DSH 不重启就一直连着旧 exe。0.5.2 的更新流程**明确不替用户结束宿主进程**（`installer.py` 顶部注释：“processes belonging to a host are never terminated for the user”）。
2. **更新脚本等不到进程退出就放弃**：`installer.py` 的 `apply()` 先检测占用，有占用直接 blocked；即使绕过，PS 脚本只等 90 秒（`WAIT_SECONDS = 90`），宿主不重启 → 进程不死 → 脚本放弃并报“程序仍在运行，已放弃替换”。
3. **宿主缓存工具列表**：README 更新章节明说“更新后请重启 Codex / Claude Code / DeepSeek Harness 的会话或宿主：MCP 客户端在会话开始时缓存工具列表，不重连就不会使用新的工具定义。”旧会话继续用旧进程/旧工具定义。
4. **没有版本自检**：hook 适配脚本与 MCP 工具返回都不带版本号（全文 grep 无版本注入；只有 `--version` 和桌面左下角显示）；用户/程序无法发现“hook 是新版、MCP 是旧版”。
5. **配置写死绝对路径**：所有宿主配置写的是安装目录绝对路径，只有“覆盖回原目录”才不需要改配置；换目录安装/迁移则旧配置仍指向旧路径，形成“两套安装并存”。
6. **Windows 文件锁**：运行中的 exe 无法替换，必须让进程退出或整目录改名后替换；0.5.2 用“改名旧目录 + 新目录就位”绕过锁，但旧进程仍从旧 inode 运行，直到宿主重启。

→ Tbrain 第 3 步的“固定入口 + `versions/` + `current` 切换 + 进程登记/通知旧版本退出 + hook/MCP 返回版本号 + 版本自检提醒 + doctor 检查残留”正好覆盖以上 6 条，方向可行。

### 5.4 四宿主当前真实接入状态（2026-10-24 只读检查）

| 宿主 | 旧 Shared Brain 状态 |
|---|---|
| Claude Code | **未接入**：`~/.claude/settings.json` 只有 clawd-desk-pet/token-tracker hooks；`~/.claude.json` 的 mcpServers 为空；只剩 `.bak` 备份文件 |
| Codex（CLI+Desktop） | **仍接入**：config.toml 有 `[mcp_servers.shared_brain]`；hooks.json 有 4 个旧 hook；config.toml 有 4 条 trusted_hash |
| DSH | **半接入**：profile patch 有 `shared-brain-mcp`（生效）+ `shared-brain-lifecycle`（指向的文件已不存在） |
| pi | **未接入**：无 MCP；`~/.pi/agent/disabled-legacy-brain/dsh-policy.ts` 是旧遗留但已禁用 |

第 6 步解绑时按此状态处理，不要假设“四个宿主都还接着”。

---

## 6. 对第 3/4 步设计的影响与需要你拍板的问题

### D1. DSH 接入形态（影响第 3 步 `connect dsh`、第 4 步 hook）

- 设计稿：“Claude Code、Codex、DSH 三家尽量共用同一份 hook JSON”。
- 现实：DSH 没有内置 Claude hook 解析器，但官方桥 `@deepseek-ai/dsh-hooks-claude-code@0.2.1-rc.3` 可以跑 Claude 风格 hooks.json，覆盖 SessionStart/UserPromptSubmit/PreToolUse/PostToolUse/Stop/SubagentStart/SubagentStop；**不支持 PreCompact/SessionEnd，也没有 token/上下文长度**。
- 选项：
  - A. 只装桥：三家共用 hooks.json，DSH 缺 PreCompact/SessionEnd/换会话提醒（用超时兜底 + 心跳代偿）。
  - B. **桥 + 原生小插件（推荐）**：共用 hooks.json 覆盖通用事件，原生插件补 DSH 独有能力（PreCompact、SessionEnd、tokenMeter、工具结果）。
  - C. 只装原生插件：能力最全，但 DSH 不共用 hooks.json，`connect dsh` 要多装一个文件。

### D2. Codex hook 信任与“doctor 全绿”验收口径

- 现实：Codex 新 hook 必须用户批准（或 `--dangerously-bypass-hook-trust`）才会执行；信任按 hook hash 记录。
- 设计稿第 3 步验收：“`doctor` 全部显示绿色”。
- 选项：
  - A. `connect codex` 写入后，`doctor` 把“未信任”显示为**黄色/待操作**，并给出宿主原生信任步骤；验收改为“四宿主均指向固定入口且已连接，Codex 待用户信任除外”。
  - B. 程序自动写入 `trusted_hash` 替用户完成信任（不建议：绕过宿主安全机制，且 hash 随内容变化）。
  - C. 验收时用 `--dangerously-bypass-hook-trust` 启动 Codex 跑通，仅作为测试手段，不算产品行为。

### D3. pi 接入形态与目标产品确认

- 现实：本机 `pi` = `@earendil-works/pi-coding-agent@0.84.2`，**无内置 MCP**；有完整 TS 扩展 API 与 `getContextUsage()`。
- 选项：
  - A. **pi 扩展直接调用 Tbrain CLI**（`tbrain bootstrap/search/save/resume` 的 JSON 接口），不引入 MCP；hook 用扩展事件实现（推荐，简单可靠）。
  - B. pi 扩展内嵌 MCP 客户端连 Tbrain 的 stdio MCP（多一层进程，扩展要自己管生命周期）。
  - C. 只支持旧 PI-Desktop（0.5.2 文档里的产品）而不支持本机 pi coding-agent。
- 另需确认：任务书里的 `pi（badlogic/pi-mono）` 是否就是指本机这个 `@earendil-works/pi-coding-agent`？

### D4. 换会话提醒的宿主范围（设计稿第 4 步/决策 14）

- 现实：Claude/Codex hook 拿不到 token；pi 与 DSH 原生插件能拿；DSH 桥也拿不到（需原生插件）。
- 建议：第一期只在 **DSH（原生插件）与 pi（扩展）** 显示；Claude/Codex 不做（或第二期用 transcript 解析补）。要你确认这个修正。

### D5. Stop 提醒在 pi 上的降级

- pi 没有 Stop-block 等价事件。第一期的 pi 适配只能“agent_end 后注入一条提示”，无法像 Claude/Codex 那样强制模型再走一步。
- 选项：A. 接受降级（只提醒不强制）；B. 研究扩展注入消息让模型继续（成本高、可靠性未知）；C. pi 第一期只做 bootstrap/search/save，不做自动 save 提醒。

### D6. 旧版本进程的结束边界

- 设计稿：“新版本启动时如果发现旧版本进程还在，就先通知旧进程退出，等待超时后强制结束。”
- 现实：Claude/Codex 的 MCP 子进程由宿主持有。允许新版本 Tbrain 通知/杀死旧版本 `tbrain.exe` 后，旧会话的下一次工具调用会失败/自动重连，用户需重启会话；这符合设计预期，但需要在 `doctor`/提示里说明。请确认此行为可接受（尤其“强制结束”）。

---

## 7. 未验证 / 存疑清单

**Claude Code**
- `SessionStart` 的 `model` 字段实测缺席（文档说可能带），未确认出现条件。
- 交互式 TUI 下新增 MCP/hook 是否同样需要重启（只测了 `-p`/stream-json 单进程）；`/mcp` Reconnect 与 `/reload-plugins` 行为仅查文档未实测。
- 用 `transcript_path` 解析 token 的可行性与滞后程度未验证。

**Codex**
- Desktop（app-server 常驻）在“新建线程”时是否重读 config.toml/hooks.json，未隔离验证。
- 未 trusted hook 在交互式 TUI 中的提示形态（exec 模式静默跳过是实测）。
- `SessionStart source=fork/clear/compact` 未实测（仅文档）。

**DSH**
- 官方 Claude 桥的端到端注入未实测（headless 组合服务不匹配；桌面 profile 改动需你批准）。
- `agent/created` 的 `source=resume` 未实测（桌面 GUI 恢复会话）；`clear/compact` 无发出方（官方 README 明说）。
- `ctx.tokenMeter` 在真实桌面组合里取 `contextWindow` 的路径（`ctx.llm.resolveModelInfo()`）未验证（隔离 CLI 返回 null）。
- `dsh plugin --profile add` 的 pnpm 流程在真实 profile 未验证（隔离环境失败）。

**pi**
- 交互式 `/resume` 的 `session_start.reason` 是否为 `resume`（`-p` 下实测为 `startup`）；`/reload` 热重载未实测。
- “无内置 MCP”来自官方 README，不是运行时反向验证。
- 本机 `@earendil-works/pi-coding-agent` 与任务书 `badlogic/pi-mono` 的对应关系待确认。

**0.5.2 升级**
- 未实际执行升级流程（只读代码/README/安装脚本），根因为代码与文档证据推导；第 3 步模拟升级时会实测验证。

---

## 8. 来源索引

### 本机只读证据

- `%USERPROFILE%\.claude\settings.json`、`~/.claude.json`（Claude hooks/MCP）
- `%USERPROFILE%\.codex\config.toml`、`~/.codex/hooks.json`（Codex MCP/hooks/trust）
- `%USERPROFILE%\.dsh\profiles\desktop\cordis.patch.yml`、`package.json`（DSH 旧接入与配置）
- `%LOCALAPPDATA%\Programs\DeepSeek Harness\version`（44.7.0）、`resources/app.asar`（引擎 0.2.1-rc.3 源码）
- `~/.pi/agent/settings.json`、`~/.pi/agent/extensions/`（pi）
- 0.5.2 源码：`src/shared_brain/installer.py`、`update.py`、`service.py`、`onboarding.py`、`desktop.py`、`scripts/build.ps1`、`integrations/README.md`、`integrations/native-hooks.mjs`、`integrations/deepseek-harness.mjs`

### 官方文档

- Claude Code MCP：https://code.claude.com/docs/en/mcp.md
- Claude Code hooks：https://code.claude.com/docs/en/hooks.md
- Codex hooks：https://learn.chatgpt.com/docs/hooks.md
- pi 扩展：本机 `@earendil-works/pi-coding-agent` 包内 `docs/extensions.md`（README “No MCP” 在 `:498`）
- DSH MCP 客户端：`@deepseek-ai/dsh-mcp-client/README.zh.md`（npm 包）
- DSH Claude 桥：`@deepseek-ai/dsh-hooks-claude-code`（npm，0.2.1-rc.3）
- DSH Codex 桥：`@deepseek-ai/dsh-hooks-codex`（npm，0.2.1-rc.3）
- DSH 原生事件：`resources/app.asar` 内 `@deepseek-ai/dsh-tool-cordis/lib/types/api-catalog.js`、`dsh-agent/README.zh.md`、`dsh-token-meter/README.zh.md`

### 实验产物（`.research\`，供复查）

- `probe-cc-codex-pi.md`：三宿主 Q1~Q6 逐条证据笔记（Claude/Codex/pi 隔离实验日志索引）
- `probe-cc-codex-pi-raw/`：假 API、假 MCP、hooks/MCP/responses 原始日志
- `dsh-asar/`：从桌面 asar 抽出的 `api-catalog.js`、`dsh-agent-loop.js`、`dsh-mcp-client`/`token-meter` README
- `dsh-probe/`：DSH 原生插件与假 LLM 实验（probe.mjs、probe021.mjs、mock-llm.py、cc-hook.mjs、hooks.json、patch*.yml、日志）
- `dsh-home*/`、`dsh021/`：隔离 DSH_HOME 与 npm 安装的 DSH 0.2.1-rc.3（临时，不入库）
