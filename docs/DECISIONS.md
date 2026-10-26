# 设计决策（DECISIONS）

> 记录设计稿未写到、或实现时按“最简单方式”定下的细节。后续每个阶段继续追加。
> 设计稿目录：`本地设计稿`。

## 0. 已确认的用户决定（2026-10-24）

| 编号 | 问题 | 决定 |
|---|---|---|
| P1 | 开工节奏 | 先只做第 1 步调研；调研汇报、确认后再进入第 2 步。 |
| P2 | 桌面端范围 | 外壳/风格/导航不变，页面换新内容：总观=会话列表，项目配置=项目名+工作目录增删，设置=时区/NTP/超时/全局提示词/版本；去掉知识统计、提交合并、维护、引导等旧功能。 |
| P3 | 项目与全局提示词维护 | 参照老版本方式：桌面页面维护项目路径与全局提示词（同时提供同名 CLI，便于脚本与验收）。 |
| P4 | “单实例” | 同版本允许多进程共存，只淘汰旧版本进程：PID/版本登记 + 锁文件，新版本启动时通知旧版本退出，超时强制结束；不造后台 daemon。 |
| P5 | 升级包来源 | 沿用 0.5.2 的 GitHub Releases 检查逻辑；验收用本地假包/本地 HTTP 源。 |
| P6 | 调研实测许可 | 允许用临时 HOME/配置目录启动宿主做隔离实测，不碰真实配置。 |
| P7 | 构建与文件改动 | 允许删改新仓库内文件、pip 装依赖、本地构建 exe、生成 0.6.1 测试包（仅升级模拟，不入主版本历史）。 |
| P8 | 新 vault 位置 | `%USERPROFILE%\Documents\TbrainVault`。 |
| P9 | 会话身份传递 | hook 注入会话身份，模型在 bootstrap/save/resume 参数里显式传 `session_id`；程序校验格式与归属，缺失/错误返回明确修复提示。 |
| D1 | DSH 接入形态 | 官方 Claude hooks 桥 + 原生小插件：桥负责共用 hooks.json（SessionStart/UserPromptSubmit/Stop/PostToolUse），原生插件补 PreCompact、SessionEnd、上下文长度/换会话提醒、工具结果。 |
| D2 | Codex hook 信任 | `doctor` 把“待用户信任”显示为黄色并给出宿主原生信任步骤；第 3 步验收口径改为“四宿主均指向固定入口且已连接，Codex 待信任除外”。不自动写 `trusted_hash`。 |
| D3 | pi 接入形态 | pi 扩展直接调用 Tbrain CLI 的 JSON 接口（bootstrap/search/save/resume），不引入 MCP；目标产品为本机 `@earendil-works/pi-coding-agent 0.84.2`。 |
| D4 | 换会话提醒范围 | 只在 DSH（原生插件）与 pi（扩展）显示；Claude/Codex 第一期不做（无 token 字段）。 |
| D5 | pi 的 Stop 提醒 | 降级为只提醒：`agent_end` 后注入一条“请 save”提示，不强制模型再走一步。 |
| D6 | 旧进程结束边界 | 新版本启动时通知旧版本进程自退，超时强制结束；doctor 与提示里说明旧宿主会话下一次工具调用会失败/自动重连。 |

## 1. 第 1 步（调研）定下的细节

1. **四宿主配置读取时机**：Claude/Codex/DSH 都是启动时读配置，运行中改文件不生效（DSH 的 MCP 配置编辑可 HMR 重连）；pi 扩展可 `/reload`。→ `connect` 之后提示用户新开会话。
2. **固定入口方案可行**：宿主配置里只写固定入口（`...\Programs\Tbrain\bin\tbrain.cmd`），升级只切 `current`，不改宿主配置。
3. **版本自检**：hook 与 MCP 工具返回都带版本号；hook 发现 MCP 版本与 `current` 不一致时注入“请重启 agent 加载新版本”。
4. **Codex `commandWindows`**：Windows 上必须写 `commandWindows`；hook 被改动后 trust hash 失效，因此 hooks.json 内容必须保持稳定（固定入口）。
5. **Claude hook 写法**：Windows 上用 `shell:"powershell"` + 全引号命令，避免 exec 形式退化。
6. **Claude fork 坑**：`--fork-session` 时 SessionStart 先报旧 ID、随后事件是新 ID；需要在 UserPromptSubmit 后重绑。
7. **DSH 桥版本对齐**：桥 `@deepseek-ai/dsh-hooks-claude-code` 必须与 DSH 引擎版本一致（当前桌面 0.2.1-rc.3），doctor 检查；configPath 在进程启动时读一次，hooks.json 内容保持稳定。
8. **DSH 原生插件事件名**：0.2.1-rc.3 用 `agent/created`（payload 带 `source`），不是旧版 `agent/session-start`；`clear/compact` 暂无发出方。
9. **DSH file 插件坑**：`D:\package.json` 带 BOM 会破坏 profile 插件兼容性预检；Tbrain 插件应作为包安装（或放在含合法 package.json 的目录）。
10. **pi 无 Stop-block**：用扩展在 `agent_end` 后注入提示；session id 稳定可作主键，但 `-p` 下 `reason` 仍报 startup。
11. **上下文长度能力**：DSH 原生插件（`ctx.tokenMeter`）与 pi（`getContextUsage`）可拿；Claude/Codex hook 拿不到。
12. **0.5.2 升级根因**：宿主 MCP 子进程长驻 + 0.5.2 不结束宿主进程 + 90 秒等待超时 + 宿主缓存工具列表 + 无版本自检 + 配置写死绝对路径。Tbrain 第 3 步逐条解决。

## 2. 第 2 步（建仓改名）定下的细节

1. **复制范围**：按 `git ls-files` 复制 second-brain 跟踪的 54 个文件（工作区已跟踪的 README 改动一并复制）；不复制 `build/`、`dist/`、`.git/`、`.venv/`、`desktop/bin|obj/`、`__pycache__/`、`.pytest_cache/`、`egg-info`、旧仓库未跟踪的 `docs/`。
2. **版本号单一来源**：`src/tbrain/__init__.py` 的 `__version__ = "0.6.0"`。`pyproject.toml` 用 `dynamic version = {attr = "tbrain.__version__"}`；`scripts/build.ps1` 解析该文件并 `-p:Version=$version` 注入桌面程序集；安装器/更新器/MCP/桌面显示都从 Python `__version__` 读。`.csproj` 不再写版本号。
3. **命名映射**：`shared_brain`→`tbrain`、`shared-brain`→`tbrain`、`SharedBrain`→`Tbrain`、`Shared Brain`→`Tbrain`、`SHARED_BRAIN_HOME`→`TBRAIN_HOME`、`~/.shared-brain`→`~/.tbrain`；MCP server 名 `tbrain`，工具名 `mcp__tbrain__*`；包目录 `src/tbrain`，桌面程序集 `Tbrain.Desktop`，可执行文件 `tbrain.exe`。
4. **历史文档保留旧名**：`docs/RESEARCH-hosts.md`（第 1 步调研证据）保留 Shared Brain/0.5.2 字样；`docs/ORIGIN.md` 记录来源。产品代码与界面不保留旧标识。
5. **发布仓库占位**：`update.py` 的 `REPOSITORY` 改为 `Denis-Tang/tbrain`（第 3 步前如未建仓，检查更新会按失败降级处理；第 3 步确认最终发布位置）。
6. **旧测试处理**：与第 4 步要重写的核心（`vault.py`/`service.py`/`model.py`/`maintenance.py` 及相关测试）在第 4~5 步整体重写；本阶段先原样保留并保证其余测试通过，第 4 步再替换。测试里出现的临时路径名 `shared brain.exe` 统一为 `tbrain.exe`。
7. **README**：重写为 Tbrain 0.6.0 简介（目标、数据目录、开发命令、文档索引、与 0.5.2 的关系），不再保留 0.5.2 的功能长文。
8. **git**：仅本地仓库，不设 remote、不 push；每个阶段一个中文 commit。

## 3. 待定 / 后续

- 发布仓库与资产命名（第 3 步确认）。
- 附件卡片读取格式与目录（第 4 步）。
- 项目登记文件格式（第 4 步）。
- 时间工具 NTP/HTTPS Date 实现细节（第 4 步）。
- 迁移映射与字段报告（第 6 步）。

## 4. 第 3~5 步实现决定（2026-10-24，用户授权“一直做到底、问题可暂放后确认”）

### 4.1 用户新增指示
- 一直做到能做完为止；不清楚的问题可先按最简单方式定下、记在此文件，等用户之后确认。
- 前端规范：`本地前端设计基本要求`（延续旧版设计语言/圆角/配色；所有动画非线性缓动；不擅自添加解释性语句）。
- 构建完新程序后必须做软件工程测试。
- 可以开 subagent 并行开发，避免 Lead 注意力分散。
- 真实宿主测试先测 Claude Code / Codex / pi（允许少量真实模型花费），DSH 之后单独验证。
- 第 6 步的不可逆操作（真实迁移、解绑旧版、改用户全局配置、停旧进程）仍等用户确认。

### 4.2 并行分工与写入范围
- A `core-store`：`clock.py`、`naming.py`、`store.py`、`index.py` + 各自测试（task-2）。
- B `hosts-versioning`：`paths.py`、`settings.py`、`proc.py`、`installer.py`、`update.py`、`hosts/`、`cli.py`、`integrations/`、`scripts/build.ps1`、宿主/安装测试、`docs/UPGRADE.md`（task-3）。
- C `frontend`：`web/index.html`、`app.js`、`bridge.js`、`style.css`（task-4）。
- Lead：`handoff.py`、`identity.py`、`hooks.py`、`session.py`、`service.py`、`prompt.py`、`mcp_server.py`、`desktop.py`、验收/集成测试、构建与真实宿主测试。
- 接口契约固定为 `docs/ARCHITECTURE.md`；跨人接口变更先发消息。

### 4.3 会话身份与 hook 注入
- 会话 ID 形如 `<宿主名>:<宿主会话ID>`（宿主名 claude-code/codex/dsh/pi；别名做归一化）。
- 启动注入包与每次 bootstrap 返回都带 `## 当前会话 session_id: …`，模型之后调用 search/save/resume 必须原样传；缺失/未知时返回明确修复提示。
- 还原会话拿到新 ID 时：同一 cwd 下“原始命令 == 首条用户消息”完全相同（sha256 规范化比对）视为同一会话，写 session_links 记录新旧映射。

### 4.4 hook 生成方式
- `app_home/hooks.json` 只给 DSH 桥（`configPath` 指向它，命令 `tbrain hook --host dsh`）。
- Claude `~/.claude/settings.json` 与 Codex `~/.codex/hooks.json` 用同一生成器内联，各自 `--host`；事件 SessionStart/UserPromptSubmit/Stop/PreCompact/SessionEnd。
- 所有命令指向固定入口 `...\Programs\Tbrain\bin\tbrain.cmd`；Windows 上加引号/`shell:"powershell"`（Claude）与 `commandWindows`（Codex）。
- 共用是“同一份事件/命令生成逻辑”，不要求物理同一文件（宿主格式限制）。

### 4.5 保存语义
- 操作：append（done/decision/dead_end/evidence/goal）、replace（target/next_step）、set_status、env、project_prompt；逐条校验、部分接受、退回附示例。
- 单条 ≤200 字；target ≤200；next_step ≤150 且只能 1 条；D#/K# 各 30 条后移入归档。
- 去重：规范化空白后相同或相似度 ≥0.92 视为同一条，返回已有编号。
- 重试：同一不合格操作按会话累计 2 次后，原文写入“待整理”并标“未校验”，不再打回。
- 自动补齐：时间、git 分支/HEAD/改动文件数；最近验证只在宿主/调用方提供 `last_test` 时写入（Claude/Codex hook 拿不到工具结果，DSH 原生插件可抓）。
- 用户手改保护：用索引里的文件哈希与当前文件比较；发现用户改过时，replace 类（target/next_step/项目提示词/全局提示词/公共池卡片）不覆盖，生成“待用户确认”；append 类只追加不改已有内容。
- 独立会话 project 用空字符串，文件落 `会话总结/独立/YYYY/MM/`。

### 4.6 时间 / 命名 / 上下文提醒
- 时间：UTC 存储；显示时区取设置（默认系统时区，回退 Asia/Shanghai）；SNTP 1.5s（多服务器轮询）+ HTTPS Date 3s 兜底；缓存 6 小时；关闭或失败标“未校准”。
- 命名：`<提炼名> YYYY-MM-DD-N`；低信息首条消息用 `未命名 YYYY-MM-DD-N`；resume 未接续时用第二条消息定名，接续后改 `<旧名> 续 YYYY-MM-DD-N`；用户可手动改名并同步 wikilink。
- 宿主注入预算：claude-code 9000、codex 8000、dsh 12000、pi 12000 字符；超限裁剪并提示可调用 bootstrap/search。
- 换会话提醒：仅 DSH（原生 tokenMeter）与 pi（getContextUsage），阈值 65% 上下文占用，估算节省 10%~50%，可在设置关闭。

### 4.7 版本与发布占位
- 固定入口 `bin\tbrain.cmd` + `current.txt`（+ current junction 尽力而为）；`versions/<ver>/` 保留最近 2 个。
- 版本自检：hook/MCP 返回带版本；发现登记进程版本≠`__version__` 或 current≠运行版本时注入重启提醒。
- GitHub Releases 仓库暂定 `Denis-Tang/tbrain`（第 6 步前如未建仓则检查更新失败降级，不影响本地功能）。
- 升级模拟：从同一源码复制到临时目录、只改 `__init__.__version__` 与安装包名生成 0.6.1 测试包，仅用于 `tests/test_installer.py` 的临时目录验收。

### 4.8 真实宿主端到端测试（第 5 步后）
- 隔离配置：Claude `CLAUDE_CONFIG_DIR`+`--settings/--mcp-config`；Codex `CODEX_HOME`（测试时允许 `--dangerously-bypass-hook-trust`，仅测试）；pi `PI_CODING_AGENT_DIR`；均用临时 HOME，不碰真实配置。
- 真实模型调用控制在小 prompt（ping/简单任务），单宿主 ≤ 数轮；DSH 用桌面/隔离 profile 单独验证。
- 真实花费仅用于验证 hook→bootstrap→save→resume 链路，不做长对话。

## 5. 第 3~5 步验收结果（2026-10-24）

### 5.1 自动化测试
- `pytest -q`：**223 passed**（含 8 条验收场景、存储/索引/时间/命名/流程/save/宿主/安装/更新/桌面 API/迁移 dry-run）。
- 8 条验收场景在 `tests/acceptance/`，用伪造宿主 hook 输入驱动，不调用模型；scenario4 连续多次运行稳定（多进程并发写同一项目）。
- 构建分发包后：`dist/tbrain` 与 zip + `.sha256` 生成成功；`tbrain.exe --version` = 0.6.0。

### 5.2 真实分发包升级模拟（通过）
`scripts/verify/upgrade_sim.py`：临时目录安装 0.6.0 → 启动 0.6.0 的 MCP 长驻进程 → 从 0.6.0 固定入口升级到 0.6.1（真实构建的两个包）。结果：
- current 从 0.6.0 → 0.6.1，`versions/` 保留 0.6.0、0.6.1；
- 旧 0.6.0 MCP 进程被通知退出（进程表只剩新版本）；
- 四个伪造宿主的配置文件升级前后 SHA256 完全一致；
- 新入口 `doctor`：install/processes/claude-code/pi 绿，codex 黄（待用户 /hooks 信任），dsh 黄（桥依赖未安装），无红色项。

### 5.3 真实宿主端到端（Claude Code / Codex 通过，pi 待 provider）
- Claude Code 2.1.132：临时 `CLAUDE_CONFIG_DIR` + `--settings`/`--mcp-config`（不改真实配置），真实模型一轮。结果：UserPromptSubmit hook 立项并注入启动包，模型调用 MCP `save`，会话总结含 D#「E2E Claude 真实链路验证通过」与下一步；约 $0.19。
- Codex CLI 0.148.0：临时 `CODEX_HOME`（机械复制 auth.json 用于登录，测后删除），`--dangerously-bypass-hook-trust` 仅测试。结果：SessionStart/UserPromptSubmit/Stop hook 全部执行，MCP `tbrain/save` 调用成功，记录 D#「E2E Codex 真实链路验证通过」；约 41.6k tokens。**Codex 能直接 spawn 固定入口 `.cmd` 的 MCP**（B 之前担心的风险已排除）。
- pi 0.84.2：hook/tool IPC 链路通过（`hook --host pi`、`tool save`、`tool search` 用固定入口 stdin JSON 实测）。真实模型一轮被本机 provider 阻断：默认 provider `magpie` 指向本机 `127.0.0.1:3425` 未运行，`deepseek` provider 无 API key；待 provider 可用后执行 `scripts/verify/real_hosts.py pi` 复验。
- DSH：真实桌面未改配置（按用户要求之后单独验证）；用临时 `DSH_HOME` + 伪造 profile 验证 `connect dsh`（写入 3 项 patch + 原生插件 + hooks.json，保留用户内容）、`disconnect dsh`（完全移除、用户内容保留）、`doctor` 输出（桥依赖缺失为黄色）。DSH 桥的 hooks.json 命令执行 shell 形式仍需真机确认。

### 5.4 真实配置影响核查
- Claude Code：真实 `~/.claude/settings.json`（15 个事件）与 `~/.claude.json`（mcpServers 空）核查无 Tbrain 残留。
- Codex / DSH / pi：真实配置中 `tbrain` 引用计数均为 0；旧 Shared Brain 条目仍在（按计划第 6 步解绑前保持只读）。
- 事故与恢复：并行开发者 B 在集成冒烟时一次未隔离 `CLAUDE_CONFIG_DIR`，曾写入真实 Claude 配置；其用 connect 前的 `.bak` 恢复，Lead 独立核查（TBRAIN 引用 0、hooks 15 事件、JSON 合法、mcpServers 空）确认已恢复。

### 5.5 遗留 / 待用户决定
- 第 6 步 6.2 正式迁移（到 `%USERPROFILE%\Documents\TbrainVault`）尚未执行，dry-run 报告见 `docs/MIGRATION-REPORT.md`。
- 6.3 解绑旧版与四宿主真实接入（改用户全局配置、停旧进程）需用户确认后执行。
- DSH 真实桌面验证（需安装桥依赖或在真实 profile 上操作）待用户安排。
- pi 真实模型链路待本机 provider 可用。
- 发布仓库 `Denis-Tang/tbrain` 尚未创建，`check_latest` 目前按失败降级；创建 Release 时资产需带 SHA256（update 校验）。


## 6. 0.6.1 适配修订

- 用户确认 DSH 改为统一原生插件，保留 MCP 四工具；移除 Tbrain 配置中的官方 Claude Hook 桥，不卸载已有依赖包。
- 用户最初授权安装与配置，后续改为先完成配置前准备：不切换 current，不更新真实宿主配置，尤其不配置当前 Codex。真实模型验收总费用不超过 10 元。
- Git 采集子进程使用空 stdin，避免继承 MCP 管道；真实 stdio 回归验证连续两次 save。
- 未解决的拒绝不推进 saved_users；重试上限后只保留实际失败条目的原文，不再退回。
- 会话目标/下一步的指纹与用户所有权写入 Markdown 头，跨 Hook 写入和进程重启保留；resume 返回当前会话 ID 与独立的来源 ID，迁移原文进入交接包。
- 正式数据迁移已执行：256 份旧总结、22 个项目卡、286 个公共池对象；此前第 5.5 节“尚未迁移”是迁移前记录。

### 0.6.1 已完成验证

- 全量测试：232 passed；包括真实 stdio 连续保存、原生 DSH 事件到实际 CLI、外部文件刷新和项目父子关系。
- 0.6.1 安装包连续 save：1.47 秒、1.34 秒，输入管道保持打开。
- Claude IDE 2.1.310 引擎：真实保存与新会话 resume → save 均成功，来源总结字节未变；两次模型费用合计 $0.265912。测试使用独立 Tbrain home/vault 和安装目录，通过宿主正常认证读取用户设置，不读取或复制凭据。
- 较早的 0.6.1 候选已复制到版本目录但未切换，后续界面修订仅更新分发包；current 保持 0.6.0。新版正式安装配置与 DSH 真实对话验收留待用户后续安排。
- 总观元数据单次读取、状态本地筛选；全部按项目默认折叠，展开分批 30 条。1000 条合成会话热刷新由约 2038ms 降至 153ms；此数字为本机样本，不代表所有知识库。
- 项目卡片进入二级页，父项目默认折叠以直接展示子项目；窄布局保留编辑和删除操作，统一留白与对齐。

### 2026-10-25 用户确认后切换

- 用户已保存工作并明确允许切换及重开；最终候选包已安装并切换 current 至 0.6.1，Claude IDE 已重载，DSH 和 Tbrain 桌面已重开。
- Claude/DSH 配置已更新，修改前生成同目录 `.bak`；其他 Claude 设置、MCP 条目及 DSH 插件配置比对一致。Codex 的 config.toml、hooks.json 未改，当前会话的旧 MCP 进程保留。
- DSH 桌面两轮真实 DeepSeek Flash 验收：原生 Hook 建档 → bootstrap → save（13 秒），新会话 resume → save（9 秒）；两次保存各写入 1 条，无拒绝或警告，来源总结字节校验不变。与此前 Claude 真实验收一起覆盖两宿主的保存及接续。

## 7. 0.6.2 首次使用

- 复用现有安装器与宿主适配器：首次双击选择知识库和 Agent，复制到用户安装目录并写入开始菜单入口；不重启其他应用、不淘汰当前 Codex 的 MCP。
- 未设置知识库时状态为未就绪，首次进入设置页，不读取会话或联网校时；已有用户直接进入总观。默认建议用户文档目录下的 TbrainVault。
- PATH、Claude VS Code 扩展及标准 DSH 安装目录用于检测宿主，Codex 桌面补查当前用户 Store 安装；用户未选择或未检测到的宿主不写配置。
- 完成页保留各宿主的重开／信任提示，只代表安装和配置结果；真实执行需要新会话验证。配置修改前保留 `.bak`。
- 移除已不使用的 DSH Claude Hook 桥文件生成与模板，DSH 保持统一原生插件。
- 验收：234 项自动化测试通过；隔离目录完成真实 EXE 首启安装、四宿主配置、开始菜单重开与连续 MCP 保存（1.34 秒、1.26 秒）。原生文件夹对话框已验证打开及取消，选择回填因自动化控件定位问题未完成；完整路径输入可用。测试后恢复本机 0.6.1，未修改真实 Agent 配置。

### 0.6.2 稳定化修订（2026-10-25，用户要求）

- 用户确认 0.6.2 的首次使用向导、宿主面板与项目二级页属于后来追加的范围，保留。
- 修复 Claude Code 的 hook 输出：只有 SessionStart / UserPromptSubmit / Stop / SubagentStop / PreToolUse / PostToolUse / PostToolBatch / PostToolUseFailure / SubagentStart / UserPromptExpansion / PostModelSwitch 接受 `hookSpecificOutput.additionalContext`（docs/RESEARCH-hosts.md §1.5）。此前 PreCompact 返回 `hookSpecificOutput`，被宿主判为非法输出并整条丢弃，压缩前的用户指令也一起丢了。
- Claude Code 压缩前的提醒改由压缩后的 `SessionStart(source=compact)` 补发：启动包之后追加“最近 5 条用户指令（压缩后补发）”，内容取自程序逐条记录的 U#，不依赖模型。其他宿主（DSH 原生插件、Codex、pi）仍在压缩前注入，行为不变。
- 启动即淘汰：MCP server 登记后开 daemon 线程执行 `proc.evict_old_versions`，不阻塞 stdio 握手；桌面程序在单实例判断前同步淘汰（最多 3 秒），旧窗口退出后重新申请 `Local\Tbrain.Desktop` 互斥体，避免“旧窗口没了、新窗口也没开”。
- `tbrain install` 在切换 `current` 后淘汰其他版本；新增 `tbrain evict [--timeout]` 手动触发。参考做法（固定入口 shim、Squirrel 版本目录、Electron 单实例锁、gpg-agent 版本握手）记录在 docs/UPGRADE.md §3.1。
- 测试：238 项通过；新增 PreCompact/SessionEnd 不产生 `hookSpecificOutput`、压缩后补发指令、后台淘汰成功与异常吞掉四类用例。

## 8. 项目界面重做（仿 Shared Brain 0.5.2，2026-10-26）

参考 `https://github.com/Denis-Tang/second-brain/releases/tag/v0.5.2` 的源码前端（`src/shared_brain/web/`），只作为界面与交互参照，不搬旧包。

- 项目页只列主项目；前端读取 `projects` 接口后按 `created_at` 倒序排序（同一时间按名称稳定排序），新建项目在最上面。后端 `store.projects()`、接口与 Markdown 文件格式都不变，排序只在前端做。
- 带子项目的卡片点击打开独立悬浮层 `tree-dialog`：根项目在左、直接子项目在右，SVG 折线从根卡片右缘连到各子卡片左缘；子项目若还有下级，点击该卡片把树根切换成它继续展开，避免深层项目不可达。
- 卡片结构沿用 0.5.2：`编辑` + `···` 菜单（新建子项目/复制名称/删除项目），右键卡片也打开同一菜单；项目页和悬浮层各一个“简洁/详细”开关，展开状态按知识库写入 localStorage。
- Tbrain 独有项保留：项目提示词移到独立对话框编辑；“允许读取子项目”开关在编辑对话框和卡片详情都可切换，详情里的切换直接调 `project_save` 保存。
- 新增/编辑项目对话框去掉父项目下拉：子项目从卡片菜单“新建子项目”进入，对话框顶部显示“主项目：X”；工作目录仍可手动输入，也可用“添加文件夹”选择。
- 本次只改前端（`app.js`、`index.html`、`style.css`）：排序、悬浮树、菜单、对话框都在页面侧完成；后端 Python 不动，以后改这些界面只需替换 web 资源，不用重打 PyInstaller 包。
