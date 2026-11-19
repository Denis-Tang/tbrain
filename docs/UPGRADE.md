# Tbrain 升级与宿主接入说明

本文说明 Tbrain 的版本化安装、固定入口、进程淘汰、升级/回退流程，以及四个宿主（Claude Code、Codex、DeepSeek Harness、pi）的接入与 `doctor` 检查。

0.6.3 新用户可解压后双击 `tbrain.exe`，在首次设置页选择知识库和 Agent，点击“开始使用”。安装完成后从 Windows 开始菜单启动；下文命令用于手动安装、管理与排查。Agent 需要提前安装和登录；未勾选的宿主不写配置。

## 0.7.11 按事项保存与接续

从 0.7.10 升级时，使用固定入口 `%LOCALAPPDATA%\Programs\Tbrain\bin\tbrain.cmd upgrade --from <新版ZIP路径>`。旧版经 `current\tbrain.exe` 切换目录时可能报告编码加载失败。可用固定入口运行 `version` 查看实际选中的版本。

同一聊天可用 `save` 和 `save_decision` 的 `project` 参数把进度和长期决定分别保存到已有项目。省略参数时沿用会话所属项目。目标项目可以搜索、读回和接续自己的记录。

这一版移除了内置任务模式。旧设置中的 `task_mode` 不再启用确认、暂停或收尾，已有 Markdown、Git 工作树和项目目录保留。调试入口为 `desktop --stdio`。自定义工作流可通过 `TBRAIN_WORKFLOW` 接收宿主事件。Pi 接入需更新扩展并重载，MCP 宿主需重开会话。完整示例见[接入指南](../integrations/README.md#agent-调试与工作流插件)。

## 0.7.10 的记忆工具拆分与修复

新增 `save_decision`，专门保存当前项目的用户长期决定，关联用户原话，支持明确替代旧决定、父项目继承及搜索和交接读取。临时要求、工作进展和技术决定仍使用 `save`。

本版修复项目提示词保存丢失自定义内容、引用标题误变用户原话、Pi 扩展消息来源混淆、Hook 等待过短、分叉丢失手改保护及项目名碰撞。任务模式功能保持原状。

公开文档已替换本机路径；开发基线脚本要求显式传入 `--vault`，快照默认写入当前目录的 `snapshots/`。本地 `.t/` 测试数据不纳入仓库检查或分发。

升级后重新打开 MCP 宿主会话。Pi 用户重新运行 `tbrain connect pi` 并新开会话或 `/reload`，才能看到新增工具。发布清单和安装包版本均为 `0.7.10`。

## 从 0.7.3 首次升级到 0.7.4 及后续版本

这次升级由旧版程序执行。先在宿主中保存并关闭使用 Tbrain 的会话，再关闭 Tbrain 桌面，确认旧进程退出后，通过固定入口执行 `install --from <新版包> --version <新版版本号>`。
不要使用旧版桌面一键更新或 `upgrade`：旧排序可能把保留的预发布版当成最新版本，清理掉0.7.3；`install` 不执行版本清理，但仍会淘汰旧进程，所以也需要先关闭会话。

0.7.4修复项目卡保存丢字段、并发写入与设置保存，并修正版本排序。稳定版不自动升级或回退到预发布；清理只计算稳定版名额，预发布与不可解析目录保留。现存 `0.8.0-beta.1` 不会自动删除。

Codex的MCP为常驻进程，升级后重新打开会话；pi下一次CLI调用使用新版程序。若pi扩展文件本身变化，再同步扩展并执行 `/reload`。
候选包的桌面GUI与真实环境升级须单独验证；源码测试通过不代表这些步骤已完成。

## 0.7.5 的项目读取范围和稳定 ID

项目默认只主动检索本项目、允许读取的子项目及公共池，不再默认读取非项目会话。
“全局共享（读取非项目会话）”只允许主动检索和手动续接非项目会话，不把这些会话加入启动推荐。
公共池和全局提示词始终可读。非项目会话不读取项目会话或项目附件。

开关也可通过 `project set --name <项目> --read-standalone on|off` 修改；未传开关时不改卡片。
新项目自动获得稳定 `project_id`，改名时保留。已有项目读取时不自动回填：
先用 `project ids --preview` 保存预览JSON，确认清单后再用
`project ids --apply <同一JSON文件> --backup <知识库外的备份目录>` 应用，原卡仅新增ID一行。

回退到0.7.4时，项目新增字段仍会保留。原版0.7.3能读新卡，但编辑项目会丢掉新增字段；
因此降到原版0.7.3后不要编辑项目卡。再次回填会优先从地图、决策日志恢复原ID，找不到才分配。

## 0.7.6 的启动包、引用与只读导出

启动包按最终完整字符串控制总量：Codex最多8000字符，Pi最多12000字符。
任务契约采用中文单语；项目目标和有效硬规则优先，用户补充最后分配。
交接决定、进度明细、待确认提议、附件与公共池摘要改为数量和来源路径索引；目标、下一步和最近指令仍内联。
省略清单列出块名与省略字符数，完整内容保留在来源文件。暂停提示保留具体下一步与热交接路径。

会话改名时，旧名写入该会话的 `aliases`，去重保留最近10个；项目提示词不改写。
Tbrain按本项目真名、本项目别名、全库真名、全库别名解析来源；同层重名时保留规则并标“来源待核对”。
Obsidian中的 `[[旧名]]` 仍可能显示未解析，Tbrain的来源解析与启动包不受影响。
来源尚未确认的规则单列“来源待确认规则”；来源末尾明确失效的规则不作为有效要求，原记录保留。

`export --project <项目名> [--out <知识库外文件>]` 直接读取项目文件，默认输出Markdown，文件使用UTF-8无BOM。
导出包含四节业务内容、继承规则、附件名称和验证命令的工作目录；不会执行命令或刷新索引。
失效规则只列在末尾“已失效规则（仅供参考）”，失联来源保留并提示核对。

随包Pi扩展已补齐项目提示词规范。升级后需由用户运行 `tbrain connect pi`，再新开会话或执行 `/reload`。
本轮隔离验证不改真实Pi扩展或宿主配置；真实宿主上的新版说明尚未验证。

## 0.7.7 的条件写入与事实地图

Agent修改已有项目章节时，须回传启动包标题中的 `sha` 到该节的 `base_sha`，空标题也算已有章节。
取代公共池卡时，先通过search取得卡片 `sha`，作为env条目的 `base_sha`。
缺少基准或基准已变化会返回当前 `head`；先核对内容，再仅重交失败节或卡。
用户手改仍优先保护。桌面和CLI用户编辑保留原行为。

项目写入冲突的两版保留在本项目待用户确认；公共卡的两版保留在 `公共池/冲突/`，处理后由用户移走或删除。
公共冲突文件不会进入检索、公共池摘要或失败路线的环境比较；启动包只列冲突数及读取位置。

`map --project <项目名> [--repo <仓库>]` 生成项目目录下的 `地图.md`，只展示Git和项目卡事实。
`--repo`只读取仓库，不注册项目路径。地图最多12KiB，超限折叠最旧条目，不据时间判条目失效。
`worktree audit [--repo <仓库>]` 只列清单，创建者和夹具名标记是启发式，不作为清理依据。
地图的Obsidian渲染及真实宿主新会话定位仍需用户验证。

当前开发版将地图改成文字项目树：列出分支的提交名称、时间、本机创建来源和提交说明原文；工作树列出未提交项数。
未记录的决定原因不推断。分类树不表示祖先关系；下方使用 Git 原生关系图，最多展示最近30次提交。12KiB上限仍保留，折叠数在地图中列出。

Pi用户升级后需重新运行 `tbrain connect pi`，再新开会话或 `/reload`，使工具说明包含本版写入协议。
本轮未同步真实扩展，未回填真实项目ID或公共池指纹。

## 当前开发版的宿主记忆修复

DSH 原生插件改用当前 Cordis 支持的依赖数组，通过 `agent/session-start` 获取启动来源，只把来源为 `user` 的消息记为用户原话。
Pi 的保存工具说明补上 `set_status.supersede`：用户明确替代旧决定时，保存新决定并标记旧决定被取代；保留旧原文，不按时间或相似度自动作废。
源码验证使用隔离目录。日常安装和宿主配置不会随源码修改更新；应用新版插件仍需安装对应版本并重新接入宿主。
本地回归：`.venv\Scripts\python.exe -m pytest tests/test_host_transport.py tests/test_memory_map.py tests/test_workspace_sync.py -q`。
DSH 运行时用例在本机已安装 `.dsh/profiles/node_modules` 时执行；没有运行时则跳过。该用例在模型调用前停止，不能代替模型使用效果或桌面安装验收。

安装暂存目录复制完成后，Windows 上短暂占用包内程序文件会使目录重命名报 `WinError 5`；直接占用目录也可能报 `WinError 32`。
开发版仅对这两个错误在发布暂存目录时最多尝试4次，间隔50毫秒。其他错误立即返回；持续占用仍报错，并沿用原有旧版本恢复逻辑。
隔离复现中，1300次安装出现10次短暂拒绝，等待约11–14毫秒后成功；未捕获到具体占用进程，不能归因给某个杀毒软件或编辑器。

## 0.7.8 的公共池回填准备

独立脚本 `scripts/dev/public_backfill.py` 提供只读预览、按指定卡片回填和正常回滚，使用方法见 `scripts/dev/README.md`。
它不接入产品CLI，不自动修复旧卡。真实执行前须用户批准具体清单并指定Vault外备份目录。
回填会把当前内容登记为模型内容，后续Agent取代可能归档原卡；时间戳只作线索，无法排除曾经手改。
正常回滚只撤本次新增字段，跳过回填后的修改。回填后首次正常使用由现有增量索引刷新卡片。

写锁保留已有锁文件的字节，最低依赖为 `filelock>=3.32.7`。本轮真实回填未执行，真实安装仍为0.7.3。

## 1. 版本化安装布局

默认安装到 `%LOCALAPPDATA%\Programs\Tbrain`（可在设置里改 `install_root`，或用环境变量 `TBRAIN_INSTALL_ROOT` 覆盖，测试/便携场景使用）：

```text
%LOCALAPPDATA%\Programs\Tbrain\
  bin\tbrain.cmd            # 稳定入口：读 current.txt → versions\<ver>\tbrain.exe %*
  current.txt               # 当前版本号（原子写）
  current\                  # junction → versions\<ver>；快捷方式经它跟随升级
  versions\0.6.0\...        # 每个版本一份完整程序
  versions\0.6.1\...
```

一个版本的目录必须包含：`tbrain.exe`、`desktop/Tbrain.Desktop.exe`、`runtime/node.exe`、`_internal/`（PyInstaller 运行时）。安装器 `installer.verify_layout()` 会逐项检查。

应用数据（设置、索引、进程登记、升级缓存）在 `~/.tbrain`（`TBRAIN_HOME` 可覆盖），升级不会删除它。

首次安装：把发布 zip 解压后，在该目录运行 `tbrain.exe install`（安装到 `%LOCALAPPDATA%\Programs\Tbrain`），或者
`tbrain install --from <目录或 zip>`。安装完成后固定入口为 `<安装目录>\bin\tbrain.cmd`。

## 2. 固定入口：升级不改宿主配置

宿主配置里只写固定的 `bin\tbrain.cmd`：

```text
"<安装目录>\bin\tbrain.cmd" hook --host <host> [--home "<应用数据目录>"]
"<安装目录>\bin\tbrain.cmd" --home "<应用数据目录>" mcp --agent <host>
```

入口脚本是纯 ASCII 的 cmd 批处理：读取同目录 `..\current.txt`，然后执行 `versions\<当前版本>\tbrain.exe %*`。升级只做两件事：

1. 把新版本解压到 `versions\<新版本>`；
2. 原子改写 `current.txt`（并尽力更新 `current` junction）。

因此 **Claude Code / Codex / DSH 的 hook 与 MCP 配置不需要重新写**，新会话或 DSH 的 MCP 热重连即使用新版程序。Pi 扩展通过固定入口调用新版程序；若扩展本身有更新，需重新运行 `tbrain connect pi` 复制新版扩展，再新开会话或执行 `/reload`。**0.7.3 的自动确认弹窗需要这一步。**

桌面、开始菜单、任务栏固定的 Tbrain 快捷方式都指向 `<安装目录>\current\...`（junction），所以同一个图标在升级或回退后自动启动新版本。早期版本创建的、仍指向 `versions\<版本>\...` 的快捷方式，会在下次切版本时自动改写到 `current`（原快捷方式保留一份 `.bak`）；快捷方式里没有参数时，窗口宿主也会读 `current.txt` 启动当前版本。

## 3. 进程登记与旧版本淘汰

`~/.tbrain/processes/<pid>.json` 记录长驻进程：`pid`、`version`、`role`、`exe`、`started_at`、`updated_at`。desktop 与 MCP 进程启动时登记，退出时删除；新版本只淘汰版本号更旧的进程，旧版不会反过来结束新版。新版本会：

1. 找到登记的旧版本进程，写 `processes/<pid>.exit`（内容是退出请求，含 requester／当前版本）；
2. 旧进程的后台 watcher 线程发现 `.exit` 后优雅退出（MCP/desktop 由调用方注册回调）；
3. 等待超时（默认 5 秒）仍未退出的，用 `taskkill /PID <pid> /T /F` 强制结束；
4. 清理陈旧的登记与退出请求文件。

**触发点（启动即淘汰）**：

| 时机 | 行为 | 说明 |
|---|---|---|
| 桌面程序启动 | 先按 `current.txt` 加载的版本同步淘汰（最多等 3 秒），再判断单实例、再开窗口 | 旧版窗口被关闭后，新窗口才会出现；重申请单实例锁避免“淘汰了旧窗口却没有新窗口” |
| MCP server 启动 | 登记后开一个 daemon 线程淘汰，不阻塞 stdio 握手 | 新版本一被宿主拉起来就清理旧版本，不拖慢 `initialize` |
| `tbrain install` | 装完并切换 `current` 后淘汰其他版本 | 覆盖“直接装包、没走 upgrade”的情况 |
| `tbrain upgrade` | 安装 → 淘汰 → 切换 `current` | 见第 4 节 |
| `tbrain evict [--timeout]` | 手动触发 | 排障用；`doctor` 的进程项会指出还有哪些旧版本在跑 |

边界：Claude/Codex 的 MCP 子进程由宿主持有，被结束后旧会话下一次工具调用会失败或由宿主自动重连；重开宿主/会话最稳妥。这也正是需要“启动即淘汰”的原因——旧进程不可能自己发现被替换。

### 3.1 参考的现有做法

- **固定入口（shim）**：rustup / asdf / nvm / `pyenv` 的 shim 每次都按 `current` 解析真实程序。Tbrain 的 `bin\tbrain.cmd` 就是这个模式，短命进程（hook、CLI）永远拿到新版本；但长驻守护进程不受益，必须额外清理。
- **版本目录 + 薄启动器（Squirrel.Windows、Discord、Slack）**：安装在 `app-<版本>` 目录，更新器换链接；新版本第一次启动时先让旧实例退出，再删旧目录。Tbrain 用 `versions/<版本>` + `current.txt` 实现同一思路，淘汰逻辑放在“新版本启动时”。
- **单实例锁（Electron `requestSingleInstanceLock`、Chrome、Windows 命名互斥体）**：单实例锁通常不带版本号，所以新旧版本会争同一把锁。Tbrain 沿用 `Local\Tbrain.Desktop` 互斥体，但先淘汰旧版本、再重新申请锁，避免旧版本“占着锁不放”。
- **守护进程版本握手（gpg-agent、tmux、docker daemon）**：客户端连上后先问版本，不匹配就让 daemon 退出或重启。MCP 做不到——host 不替我们重启 server，server 也不能杀死宿主，所以只能在“新版本被启动”这一刻清理，并由 hook 的版本提醒告诉用户重开会话。
- **Windows 安装器的常见组合**：进程登记 + 优雅退出信号（窗口消息/`.exit` 文件/命名事件）+ 超时强杀（`taskkill /T /F`）。Tbrain 用的是 `.exit` 文件（跨进程、可审计、失败可查）+ `taskkill`。

## 4. 升级流程

`tbrain upgrade` / 点击桌面左下角版本号检查、选择“升级”（也可取消）：

1. **检查**：读取 `https://github.com/Denis-Tang/tbrain/releases/latest/download/latest.json`；不调用 GitHub REST API，不占用匿名 API 查询额度。结果缓存 6 小时，手动检查间隔至少 1 分钟；失败显示简短原因；
2. **下载**：后台流式写入 `~/.tbrain/updates/<版本>/`，界面每半秒读取进度，显示百分比、下载量和速度。下载中断或取消后保留 `.part` 文件，再次升级通过 HTTP Range 续传；服务器返回完整文件时重新写入。网络超时按等待数据的时长计算，完整下载不受界面60秒等待限制；
3. **校验**：检查压缩包结构（`tbrain/` 根、必需文件、`_internal/`）；
4. **安装**：解压并复制到 `versions\<新版本>`（同卷先 staging 再 rename，半拷贝的目录永远不会被选中）；
5. **停旧进程**：见第 3 节；
6. **切版本**：原子写 `current.txt` + 更新 junction，并把仍指向 `versions\<版本>\...` 的桌面、开始菜单、任务栏快捷方式改写到 `current`；
7. **跑 doctor**：把检查结果附在升级结果里（doctor 失败不会撤销已完成的升级）；
8. **保留版本**：`prune_versions(keep=2)` 保留当前稳定版和最新稳定版，再按新到旧补满稳定版名额；预发布与不可解析目录不占名额，也不自动删除。

下载结束后校验完整文件的 SHA256 和压缩包结构，再进入安装。安装开始后取消按钮禁用，完成后桌面自动重启。已下载完整但尚未安装的包可以复用。

桌面启动后在后台检查更新，此后每6小时检查一次。发现新版时左下角版本号旁显示小蓝点，鼠标悬停可查看新版本号，点击后仍由用户选择升级。升级会就地更新桌面、开始菜单（含子文件夹）与任务栏已有快捷方式的目标、工作目录和图标路径，保留快捷方式名称与启动参数。

0.7.1 启动时也会修正已有快捷方式，用于补齐旧更新器留下的图标及工作目录迁移。

从本地包升级（测试、内网分发）：

```text
tbrain upgrade --from D:\path\tbrain-0.6.1-windows-x64.zip
tbrain upgrade --from D:\path\dist\tbrain --version 0.6.1
tbrain upgrade --from https://example.com/tbrain.zip --version 0.6.1
```

`--version` 省略时安装器会尝试从包内的 `tbrain/__init__.py` 读取版本号。

失败处理：升级状态写入 `~/.tbrain/update-state.json`。如果切换版本后才失败，会尽力切回原版本；升级包校验失败会在写入前停止。

## 5. 回退与卸载

```text
tbrain rollback            # 切到已安装的另一个（较新）版本
tbrain uninstall           # 删除程序文件，保留 ~/.tbrain 与知识库
```

回退只是再次改写 `current.txt`：宿主配置不变，新会话使用回退后的版本。`uninstall` 删除 `versions/`、`bin/`、`current.txt`、`current` junction；用户数据目录不动。卸载前建议先在各宿主执行 `tbrain disconnect <host>`，避免宿主继续调用已删除的入口。

## 6. 四宿主接入

```text
tbrain connect claude-code
tbrain connect codex
tbrain connect dsh [--profile desktop]
tbrain connect pi
tbrain disconnect <host>
tbrain doctor
```

连接前会备份被修改的文件为同目录 `<原文件名>.tbrain-<YYYYmmdd-HHMMSS>.bak`；只增删 Tbrain 自己的条目，保留用户其他配置。

| 宿主 | 写入内容 | 生效 |
|---|---|---|
| Claude Code | `~/.claude/settings.json` hooks（5 事件，`shell: powershell` 全引号命令）；`~/.claude.json` 用户级 `mcpServers.tbrain` | 新会话 |
| Codex | `~/.codex/hooks.json`（command + commandWindows）；`~/.codex/config.toml` 的 `[mcp_servers.tbrain]` | 新进程；hook 需 `/hooks` 信任 |
| DeepSeek Harness | profile patch 的 `tbrain-mcp` / `tbrain-native` 两项；原生插件复制到 `<app_home>/integrations/` | MCP 热重连；原生插件重开 DSH |
| pi | `~/.pi/agent/extensions/tbrain.ts`（无 MCP） | 新 pi 会话或 `/reload` |

### Codex hook 信任

Codex 对非 managed hook 采用 hash 信任，未信任的 hook 会静默跳过。Tbrain 的命令指向固定入口，升级不改变 hook 内容，因此信任一次即可。程序**不写** `trusted_hash`、**不使用** `--dangerously-bypass-hook-trust`；`doctor` 在信任前显示黄色“待信任”，并提示在 Codex 中执行 `/hooks` 审核。

### DSH 原生接入

0.6.1 使用单一原生插件处理生命周期，不再加载 `tbrain-cc-hooks`。重新运行 `connect dsh` 会移除旧 Tbrain 条目并写入两项配置，保留其他插件与注释；已有桥依赖包不卸载。

MCP 参数为 `--home <应用数据目录> mcp`。原生插件通过固定入口调用 CLI，读取 `additional_context`，收尾未保存时只续跑一次；压缩前注入最近用户指令。上下文占用从宿主 tokenMeter/contextPressure 读取，仅在本轮已保存时建议换会话。

`doctor` 检查固定入口、启动参数和原生插件配置，不把“配置完整”当成真实宿主执行证明。升级后需在 DSH 新会话验证建档、save 与 resume。

## 7. doctor 检查项

`tbrain doctor` 输出 `{"ok": bool, "items": [{"id", "host", "status", "detail"}]}`，至少包含：

| id | 绿色 | 黄色 | 红色 |
|---|---|---|---|
| `install` | 版本目录完整、`current.txt` 与固定入口一致 | 尚未安装（源码运行） | 固定入口缺失、`current.txt` 缺失、当前版本文件不完整 |
| `processes` | 无进程或均为当前版本 | 有旧版本进程仍在运行 | — |
| `claude-code` | hooks + MCP 均指向固定入口 | 未接入 / MCP 条目不一致 | 固定入口缺失 |
| `codex` | 已接入且 hook 已信任 | 未接入 / **hook 待信任** / 信任记录可能过期 | 固定入口缺失 |
| `dsh` | MCP 与原生插件两项配置完整，启动参数正确 | 未接入 / 配置不完整 | **旧 Shared Brain 条目残留** / 原生插件缺失 |
| `pi` | 扩展已安装 | 未接入 | 固定入口缺失 |

## 8. 常见问题

- **升级后旧会话还在用旧版本？** 宿主按会话缓存 MCP 进程与 hook；新会话生效。重开宿主最稳。旧版本进程会被新版本通知退出/强制结束（见第 3 节）。
- **Claude/Codex 配置里有旧 Shared Brain 条目？** 第 6 步迁移/解绑流程统一处理；`doctor` 会把 DSH 的旧 `shared-brain-mcp` / `shared-brain-lifecycle` 残留标红。
- **想换安装目录？** 在设置里改 `install_root` 后重新 `tbrain install`，再对每个宿主重新 `connect`（宿主配置写的是安装目录绝对路径）。
- **发布资产要求？** `scripts/build.ps1` 生成 ZIP、`.sha256` 和 `latest.json`，每个正式 Release 都要上传三份附件。`latest.json` 包含版本号和安装包的名称、固定下载地址、大小、SHA256；可填写 `notes` 作为更新说明。程序读取清单中的摘要校验下载内容；`.sha256` 供人工核对。不要仅上传安装包却遗漏清单。
- **安装包里的版本号从哪来？** 构建时 `scripts/build.ps1` 把 `src/tbrain/__init__.py` 的版本写入包内 `VERSION` 文件（PyInstaller 目录里没有可读的 `tbrain/__init__.py`），`tbrain install/upgrade` 从该文件推断版本；源头仍是 `__version__`。
- **升级包如何生成？** `pwsh -File scripts/build.ps1`，产物 `dist/tbrain` 直接匹配 `installer.REQUIRED_FILES`；版本号只从 `src/tbrain/__init__.py` 读取。


## 0.6.3：Agent 管理与更新生效

安装后在左侧“Agent 管理”开启 Claude Code、Codex、DSH、pi 各自的总开关；展开详情查看工具、Hook、配置路径和加载步骤。未接入的 Agent 不写配置，其他插件和 MCP 条目保留。pi 的工具与事件由扩展提供，不写 MCP 配置。

| Agent | 开启后的操作 |
|---|---|
| Claude Code | 登录模型服务，重开会话；必要时在 `/mcp` 重连 |
| Codex | 重开 Codex，在 `/hooks` 审核并信任 Tbrain；首次工具调用按原生提示批准 |
| DSH | 完整退出后重开；Tbrain 默认管理 desktop profile，其他 profile 用现有 `connect dsh --profile <name>` |
| pi | 新开会话或 `/reload`，先在 pi 内完成模型认证 |

关闭开关会移除 Tbrain 配置，并在应用数据中记录禁用。已经启动的新版 MCP 即使仍被宿主缓存，也会拒绝工具读写；缓存 Hook 不再记录消息。工具名称从宿主列表消失仍需重开或重连。此控制要求实际运行 0.6.3；升级前已经缓存的旧代码需先结束。

桌面点击安装更新后，0.6.3 会先返回结果，再退出旧原生窗口、释放单实例占用，启动目标版本。旧进程无法结束时不切换 current。回退需要显式执行 rollback；启动时的进程淘汰仅针对更旧版本。

版本目录默认保留两份便于回退，知识库和应用数据保留。下列实测记录对应历史版本，当前发布的更新清单地址见第 4 节。


### 隔离实测结果（2026-10-25）

使用真实构建包测试两条升级路径：0.6.2→0.6.3 的本地 HTTP 升级，以及在 0.6.3 桌面点击“检查更新／升级”到仅用于验证的 0.6.4。桌面完成下载后自动退出并打开新版本；旧桌面、原生窗口和 MCP PID 存活数为 0，四宿主配置与固定入口内容不变。0.6.4 测试包不发布，交付候选仍为 0.6.3。

在升级后的实际窗口逐个关闭、重新开启四个 Agent：关闭时三个已连接 MCP 的无 session_id 检索均被拒绝，pi 工具被禁用，四个 Hook 均未写入消息；重新开启后原 MCP 连接恢复，pi 工具恢复，四个 Hook 各生成一份独立会话总结。

Codex CLI 另以隔离 CODEX_HOME 验证：未信任时不生成总结；单次测试参数 `--dangerously-bypass-hook-trust` 放行后原生 Hook 捕获一条原始指令。此测试没有登录，模型请求返回 401，不代表已验证“用户在 /hooks 信任后，Hook 与模型联合运行”；生产接入仍需用户在 /hooks 审核，不自动写信任记录。

四个 Agent 的实际模型保存与新会话接续已分别通过；Claude/Codex 的模型测试关闭全局 Hook 以隔离真实知识库。自动回归 249 项通过，含 8 个设计验收场景。公开 Release 下载链路仍待发布资产后验证。


### 直接双击新包时的启动顺序

正式安装仍为 0.6.2 时，直接双击 0.6.3 分发包曾导致新窗口退出：新窗口结束旧 MCP 后，Agent 按仍指向 0.6.2 的固定入口重连；0.6.2 的淘汰逻辑会反过来结束新窗口。新版桌面在登记进程和淘汰之前，先把新包安装到已有安装目录并切换固定入口，宿主重连因此启动新版。未安装过时仍进入首次设置，不会自动降级已有的更高版本。Agent 配置和知识库保持原位。

修复验证：隔离环境重现旧入口为 0.6.2 时新窗口退出；修复后固定入口为 0.6.3，模拟 Agent 自动重连启动 0.6.3 MCP，新窗口保持运行；全量 251 项通过。实际用户目录启动也已核对到 0.6.3 窗口。


### 0.6.4：Codex SessionEnd 超时

Codex 的 SessionEnd 最多允许 3 秒；Tbrain 从 0.6.4 起为该事件写入 `timeout: 3`，其他已接入事件仍为 10 秒。旧配置的 `clamping SessionEnd hook timeout to 3s` 表示 Codex 自动压短了超时，不代表 MCP 失效。修改现有配置前保留 `.bak`；改动 Hook 后在 `/hooks` 重新审核信任。依据：https://developers.openai.com/codex/hooks/ 。
