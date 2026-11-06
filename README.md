# Tbrain

> 给编程 Agent 用的本地记忆工具。把要求、决定和进度存成 Markdown，换会话或换 Agent 时接着做。

[**下载 0.7.1**](https://github.com/Denis-Tang/tbrain/releases/tag/v0.7.1) · [**使用文档**](#usage--使用指南) · [**实验与结果**](#experiments--长任务评测) · [**问题反馈**](https://github.com/Denis-Tang/tbrain/issues)

## Overview | 产品介绍

Tbrain 面向在 Windows 上使用 Claude Code、Codex、DeepSeek Harness（DSH）或 Pi 的你。它把聊天里的项目要求、已做工作、证据和下一步写进本地知识库，让新会话有依据地继续任务。

记忆采用 Markdown，能用 Obsidian 或文本编辑器查看和修改。Tbrain 的存储、校验与检索不调用大模型，也不需要模型密钥；整理和读取记忆仍会消耗 Agent 的上下文与模型用量。

### 为什么需要它？

- **换会话丢进度：** 把目标、决定、未完成事项和下一步放进交接包，减少重新翻聊天的工作。
- **换 Agent 重复解释：** 让同一项目目录匹配同一份项目规则和历史。实际接续仍需 Agent 调用工具。
- **记忆难以核对：** 保留来源编号、用户原话和历史版本，让你能检查记住了什么、依据是什么。

## Preview | 产品预览

桌面提供总观、项目配置、Agent 管理和设置四页。一次接续的流程是：

```text
在项目目录开始会话 → 读取项目规则与近期进度
完成一段工作       → save 保存本轮变化
开启新会话         → resume 读取旧任务交接包 → 继续执行
```

## Experiments | 长任务评测

这轮正式实验于北京时间 2026 年 11 月 4 日至 5 日运行，围绕三个问题展开。切换到新会话后，Agent 能否恢复要求与工程状态，能否正确处理撤销和更新的决定，以及接续答题需要多少输入、费用和时间。实验使用 DSH、Pi Desktop 两款桌面宿主，每款分别运行原生记忆、Tbrain、Basic Memory 三种条件，共六条工程链，每条件一条。

### Agent、模型与运行配置

| 项目 | 实验设置 |
|---|---|
| DSH Desktop | ASAR 包版本 `0.2.1-rc.5`，Windows 文件版本 `0.2.1.0` |
| Pi Desktop | `0.8.2` |
| Agent 模型 | 两款宿主均使用 `deepseek-v4.1-flash`，经本机同一 `lite-relay` 路由调用 |
| 推理设置 | DSH `reasoningEffort=medium`，Pi `defaultThinkingLevel=medium` |
| 客户端模型声明 | 两款均配置 `contextWindow=1000000`，Pi 另配置 `maxTokens=16384`。声明值未独立验证为上游实际有效上限 |
| Tbrain | `0.7.0-beta.1+lab.1`，实验兼容版 |
| Basic Memory | `1.0.0`，本地模式，各组独立笔记库，通过 stdio MCP 接入 |
| 系统与隔离 | Windows，Sandboxie-Plus `1.18.8`，六组各自拥有应用箱、工作副本、配置、会话历史和记忆目录 |
| 任务控制与评分 | Codex 调度和复核。工程执行、模型调用与会话保存由实际 DSH、Pi 桌面运行时完成 |

原 Beta `0.7.0-beta.1` 在 Sandboxie 内重复保存已有快照时失败。正式六组中的 Tbrain 条件使用修复该问题的 `+lab.1`。发布版 `0.7.0` 随后纳入兼容修复，另做回归和分发验证。下面的横评数字对应实验兼容版。

### 额外插件、工具与权限

DSH 三组都加载了实验调度插件 `tbrain-bench-desktop-driver`。插件通过桌面 `sessionController` 创建会话、发送任务和记录事件，并设置 `danger-full-access` 权限。为解决箱内渲染与重复权限限制，三组统一关闭 DSH 内部重复 ACL 限制，使用 `--no-sandbox` 等 Electron 启动参数，外层 Sandboxie 保留。Pi 三组使用桌面自身的 Agent API 调度和原生 PowerShell 工具。这些条件会影响运行行为与耗时。

| 宿主与条件 | 额外记忆接入 | 会话关联方式 |
|---|---|---|
| DSH 原生 | 使用宿主历史与本组工程笔记 | 宿主自身记录 |
| DSH + Tbrain | 通用 MCP 接入 Tbrain 四工具 | 模型手动传入宿主会话 ID |
| DSH + Basic Memory | 通过 `@deepseek-ai/dsh-mcp-client` 连接本地 Basic Memory | 本组专用 MCP 项目与笔记库 |
| Pi 原生 | 使用宿主历史与本组工程笔记 | 宿主自身记录 |
| Pi + Tbrain | 加载随被测包提供的 `tbrain.ts` 扩展，扩展调用 Tbrain CLI 四工具 | 扩展读取 `sessionManager.getSessionId()` 并绑定真实 ID |
| Pi + Basic Memory | 通过桌面 MCP 配置直接暴露 Basic Memory 工具 | 本组专用 MCP 项目与笔记库 |

六组都获得相同项目背景、`AGENTS.md` 和项目内的 DocETL 技能材料，首阶段均要求阅读 `source/.agents/skills/docetl/SKILL.md`，允许网络调研、读取自己的历史及编写工程笔记。各组的记忆工具不同，输入提示中的条件说明也相应不同。实验没有逐次统计所有技能的实际使用情况。

**配置记录仍有缺口。** 已核对的准备脚本与实验目录能够确认上表接入。完整宿主内置插件清单、各内置组件版本、逐请求采样参数和完整系统提示词未冻结为可公开复核的快照。控制器 Codex 的版本、模型与推理档位也未在评测报告中固定记录，评分独立性与完整复现因此受限。

### 工程任务与检查方式

任务对象是一个 Windows 酒店知识库转换程序，各组从相同材料和空任务记忆开始，按相同阶段要求推进。

1. 调研现有方法，审计源码并离线复现缺陷。
2. 对已证实问题做最小修复和回归，同时处理交付代号、输出目录等要求变更。
3. 简化离线导出流程，核对原文、来源及生成内容的对应关系。
4. 进行真实模型转换对照，比较候选流程并决定是否保留。
5. 冻结方案，使用独立材料核验并整理源码与 Windows 包。

每条链计划使用六个宿主会话，切换五次。控制器主动创建新会话，每次先发只读问题检查记忆，再在同一新会话中推进下一阶段，最后一次只读检查最终状态。六链从首个工程发令到最后探针发令跨越 **2.275–4.865 小时**，其中包含调度与等待。

- **核心记忆检查**每链共 69 次，包括 59 次已知事实恢复和 10 次无答案识别。同一事实可能在不同会话重复出现。
- **严格工程接续**有四个可执行切换点，需要正确恢复实际代码、测试、当前方案与剩余工作，并完成随后指定的工程动作。
- **最终状态恢复**单独记录最后一次检查。旧决定退役、原话采集与来源纠错补测也分别核对。
- **计分**要求一项中的全部子条件成立。额外断言与原始材料矛盾也会失分。Codex 对照消息、源码、工具记录和产物评分，未进行外部人工盲标。

### 六组结果

以下用量、费用和时间均累计自**五次新会话接续答题**，从发令统计到首份完整答卷，失败检查点也保留。总输入包括缓存输入。费用按人民币每百万 Token 的未缓存输入 2 元、缓存输入 0.04 元、输出 8 元统一估算，输出已包含推理 Token，未核对供应商账单。

| 宿主与条件 | 核心检查 | 严格工程接续 | 总输入 Token | 估算费用（元） | 累计答题秒 |
|---|---:|---:|---:|---:|---:|
| DSH 原生 | 69/69 | 4/4 | 8,426,194 | 2.139394 | 1,234.077 |
| DSH + Tbrain | 67/69 | 3/4 | 6,214,688 | 1.540952 | 1,035.149 |
| DSH + Basic Memory | 67/69 | 4/4 | 9,054,575 | 1.927965 | 1,175.738 |
| Pi 原生 | 68/69 | 4/4 | 3,572,804 | 1.043492 | 688.903 |
| Pi + Tbrain | 69/69 | 4/4 | 2,829,381 | 0.864804 | 1,627.728 |
| Pi + Basic Memory | 66/69 | 4/4 | 2,651,872 | 0.874814 | 543.690 |

DSH 的 Tbrain 条件相对原生总输入减少 **26.2%**、统一价格估算费用减少 **28.0%**，同时核心检查少通过两项、严格工程接续少通过一项。Pi 的 Tbrain 条件总输入减少 **20.8%**、估算费用减少 **17.1%**，核心检查多通过一项，但累计答题时间明显增加。Pi 的 Basic Memory 条件具有本轮更低的输入与耗时，核心检查为 66/69。

工程接续通过的点也可能接受过恢复协助。四个点中，可计为无控制器恢复协助通过的数量依次为 DSH 原生 **4/4**、Tbrain **3/4**、Basic Memory **3/4**，Pi 原生 **3/4**、Tbrain **3/4**、Basic Memory **4/4**。Pi Tbrain 第三次答题包含控制器中止越界 Shell 的干预，该次 **870.970 秒**全部计入累计时间。

### 接入缺口与结论范围

答对事实之外，本轮还观察到以下问题。

- DSH Tbrain 第四阶段用到了明确禁止用于调参的同源材料，后续对来源的同一误判在两个检查点重复，造成两项核心失分和一个工程接续点失败。Pi Basic Memory 对同一撤销日期的错误重复三次。
- DSH Tbrain 的 `bootstrap` 传入 ID 匹配真实宿主的为 **3/8**。`save` 的 ID 匹配为 **12/24**、整次无拒绝为 **15/24**，两项同时满足为 **8/24**。Pi 的扩展绑定 ID，`bootstrap` 为 **3/3**、`save` 为 **28/28**，但 `save` 整次无拒绝为 **13/28**。ID 关联和保存接受情况分别计量，结果还受到宿主适配方式影响。
- Tbrain 用户原话全文匹配在桌面 API 路径下，DSH 为 **0/10**、Pi 为 **10/10**。真实 GUI 各补测一条，分别为 **0/1**、**1/1**。GUI 样本很小。
- Pi Tbrain 能恢复更新后的值，但两条旧决定的原记录在阶段二和阶段五均未正式退役，检查为 **0/2**。DSH 的旧阶段禁令也有延迟清理。

本轮每条件只有一条独立链，69 次检查包含重复事实，运行顺序未完整随机化，机器非独占，上游缓存未重置。工程方案、工具选择、输出长度、网络等待和控制器干预均有差异，现有数据不足以把输入与耗时差异单独归因于记忆机制。

因此，本轮支持报告这些具体条件下的事实恢复、接续与答题成本观测。普遍准确率、完整开发任务的费用节省、跨宿主接力效果、原生上下文压缩成功率和统计显著性仍需新的实验。公开 CSV 可用于复算汇总，完整独立复现还需要未公开的原始任务材料与运行记录。更多计量定义、六组明细和失分依据见 [评测方法](docs/BENCHMARK.md) 与 [汇总数据](docs/benchmark-summary.csv)。

## Features | 核心功能

### 1. 保存进度，按需接续

通过四个工具工作：`bootstrap` 返回启动包，`search` 检索历史，`save` 保存增量，`resume` 读取旧会话交接包。会话原话、完成项、决定、证据与未完成目标分开保存。

### 2. 让项目规则跟着工作目录走

为项目关联一个或多个目录，维护目标、硬规则、当前阶段和常用命令。新建项目可向已接入的 Codex、DSH 登记；Codex 保留多目录，DSH 支持单目录。Pi Desktop 暂无可用的外部项目登记接口，具体条件见 [宿主接入说明](integrations/README.md#新建项目联动)。

### 3. 在本地查看和核对记忆

Markdown 是事实源，SQLite 是可重建的检索索引。检索支持中文分词和代码标识符；归档条目仍可查找。明确标为被取代、已废止或待确认的决定不进入有效决定交接，但旧决定的识别与退役仍依赖 Agent 正确维护。

### 4. 桌面管理与版本切换

在桌面管理知识库、项目和 Agent 接入，后台检查新版并在版本号旁显示小蓝点。升级时显示下载进度，支持取消和断点续传，完成后自动重启，并复用已有快捷方式。宿主使用固定命令入口，版本切换后按提示重开宿主，也可回退已安装版本。

## Quick Start | 快速开始

### 环境要求

- Windows 10/11 x64。
- Microsoft Edge WebView2 Runtime。
- 已安装并配置好至少一个支持的 Agent。模型及 API 配置在 Agent 内完成。

发布包包含 Python、.NET 和 Node.js 运行时，无需安装开发工具。

### 安装与配置

1. 下载 [0.7.1 发布页](https://github.com/Denis-Tang/tbrain/releases/tag/v0.7.1) 中的 `tbrain-0.7.1-windows-x64.zip` 并解压。
2. 双击解压目录里的 `tbrain.exe`，选择知识库文件夹，勾选要接入的 Agent，点击“开始使用”。
3. 按完成页提示重开 Agent。Codex 还需在 `/hooks` 中审核并信任 Tbrain Hook；Pi 新开会话或执行 `/reload`；DSH 完整退出后重开。
4. 在“项目配置”添加项目及代码目录，让会话匹配到对应项目。

### 启动与升级

从 Windows 开始菜单打开 Tbrain。命令行固定入口为：

```powershell
& "$env:LOCALAPPDATA\Programs\Tbrain\bin\tbrain.cmd" version
```

已有安装可点击桌面左下角版本号检查更新，或用固定入口从本地 ZIP 升级：

```powershell
& "$env:LOCALAPPDATA\Programs\Tbrain\bin\tbrain.cmd" upgrade --from .\tbrain-0.7.1-windows-x64.zip
```

升级后重开 Agent，核对 `version` 输出中的 `installed_version`。升级与回退细节见 [升级文档](docs/UPGRADE.md)。

## Usage | 使用指南

### 基础使用

1. 在已配置的项目目录开始会话，检查启动包中的项目名、规则和进度。
2. 完成阶段工作后，让 Agent 调用 `save`，保存本轮变化及依据。
3. 在新会话继续旧任务时，让 Agent 调用 `resume`，核对交接包后继续。

例如，向 Agent 发出：

```text
把这次完成的修改、测试结果和下一步保存到 Tbrain。
```

接续时给出旧会话名：

```text
用 Tbrain resume 接续“旧会话名”，先核对未完成目标，再继续。
```

`save` 返回 `ok=true` 表示全部通过。返回 `ok=false` 时，`written` 中的内容仍已保存，让 Agent 只补交 `rejected` 中失败的部分。进入“待整理”不等于保存验收通过。

### 进阶使用

在固定入口所在目录运行以下命令；示例目录需替换成你的代码目录：

```powershell
.\tbrain.cmd project add --name 示例项目 --path D:\work\app --path D:\work\docs
.\tbrain.cmd sessions --filter 进行中
.\tbrain.cmd bridge show --project 示例项目
```

`bridge show` 列出项目内近期有效决定，便于接手前核对。项目目标、规则或阶段发生变化时，让 Agent 在 `save` 中提交对应的 `project_prompt` 节；没有变化的节不重写。

新建项目时，第一个代码目录会初始化为 git 仓库（已是仓库则沿用），并固定一处工作区：`../.tbrain-worktrees/<仓库目录名>/<项目名>`。同时写入完成标准模板，字段是 goal、done、evidence、verify。

每天的本地门禁用 `tbrain check`。它只跑项目里的 `.tbrain/check.yaml`，不连接 GitHub；远程拉取请求仍用仓库自己的 CI。说明见 [本地闭环](docs/LOCAL-LOOP.md)。

```powershell
.\tbrain.cmd check --path D:\work\app
```

冷蒸馏不进启动路径。会话已经结束或中断后，可以在本地把它们收成 wiki；没跑过 Dream 时，启动包与原来相同：

```powershell
.\tbrain.cmd dream status
.\tbrain.cmd dream run
```

说明见 [Dream](docs/DREAM.md)。

MCP 指 [Model Context Protocol](https://modelcontextprotocol.io/)，用于 Agent 调用外部工具。四个工具的参数见 [技术架构](docs/ARCHITECTURE.md)，各宿主生效条件见 [接入说明](integrations/README.md)。

## Architecture | 技术架构

| 层级 | 技术与职责 |
|---|---|
| 桌面 | .NET 8 WPF + WebView2，展示 HTML/CSS/JavaScript 界面 |
| 后端 | Python 3.11+，提供 CLI、MCP 和桌面 API |
| 存储 | Markdown 保存事实，SQLite FTS5 + jieba 提供检索 |
| Agent 接入 | Claude Code、Codex 的 Hook/MCP，DSH 原生插件/MCP，Pi 扩展 |
| 分发 | PyInstaller 目录包、内置桌面与 Node.js 运行时、ZIP 更新包 |

宿主事件或工具调用进入同一后端，后端写入 Markdown 并刷新索引。新会话通过启动包和 `resume` 读取已保存的信息。Tbrain 不会搬迁完整聊天上下文，也不能保证 Agent 自动记住所有内容。

## Project Structure | 项目结构

```text
src/tbrain/       后端、CLI、MCP、宿主适配与网页界面
desktop/         WPF + WebView2 桌面壳
integrations/    DSH 插件、Pi 扩展与接入说明
tests/           单元测试、集成测试与验收场景
scripts/         构建与验证脚本
docs/            架构、升级、评测与历史决策
README.md        项目介绍与上手指南
LICENSE          Apache-2.0 许可证
```

## Configuration | 配置说明

在桌面设置页修改配置，文件位于 `%USERPROFILE%\.tbrain\settings.json`。

| 配置项 | 作用 | 是否必填 |
|---|---|---|
| `vault_path` | 本地知识库目录 | 首次设置时选择 |
| `timezone` | IANA 时区，留空跟随系统 | 否 |
| `ntp_enabled` | 联网校时 | 否 |
| `timeout_hours` | 会话超时处理的小时数 | 否 |
| `reminder_context_switch` | DSH、Pi 的换会话提醒 | 否 |
| `install_root` | 程序安装目录 | 否 |

`TBRAIN_HOME` 可指定独立应用数据目录。程序默认安装到 `%LOCALAPPDATA%\Programs\Tbrain`；知识库由你选择。切换知识库路径不会自动搬迁旧文件。

## Development | 开发指南

准备 Python 3.11+。构建 Windows 分发包还需要 .NET 8 SDK、Node.js 和 PyInstaller。

```powershell
git clone https://github.com/Denis-Tang/tbrain.git
cd tbrain
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev,build]"
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m tbrain desktop
```

构建 0.7.1 分发包：

```powershell
pwsh -NoProfile -File scripts/build.ps1 -OutputDirectory dist/0.7.1
```

版本号统一定义在 `src/tbrain/__init__.py`。构建产出可运行目录、ZIP、同名 `.sha256` 和 `latest.json`，Release 上传后三项。0.7.1 的本地全量回归为 **301 项通过**，覆盖更新下载、取消续传、后台检查与旧快捷方式修正等行为。真实宿主验证需单独执行，见 [验证说明](https://github.com/Denis-Tang/tbrain/blob/main/scripts/verify/README.md)。

## Roadmap | 开发计划

- **0.7.1 已包含**后台更新检查与版本提示、下载进度和断点续传、取消下载、安装后重启、旧快捷方式复用，以及补全配置和方法的实验说明。
- **0.7.0 已包含：** 记忆保存与检索、跨会话接续、有效决定筛选、历史版本、项目多目录、Codex/DSH 新建项目联动、Pi 异步扩展及快照兼容修复。
- **后续优先核验：** DSH 会话归属与原话采集、旧决定和阶段授权退役、项目侧栏实际刷新行为。
- **后续评测：** 增加独立任务链和任务主题，补测跨宿主接力。具体时间尚未确定。

## FAQ | 常见问题

**会自动清理错误或过时的记忆吗？**

热路径不会自动判断所有冲突。`bootstrap` / `save` / `resume` 仍只过滤已经明确标成失效的决定，Agent 还要核对来源并维护状态。`tbrain dream run` 在同一知识库另写一份冷蒸馏，把这些失效决定记成退役，并把「待整理」收成待核验条目；它不改写会话总结正文。见 [Dream](docs/DREAM.md)。实测中存在旧决定未退役、阶段授权延迟清理的问题。

**四个 Agent 的接入效果一样吗？**

不一样。实测中 Pi 扩展绑定了真实会话 ID；DSH 通用 MCP 路径存在模型手填 ID 错误，真实 GUI 原话采集补测也未通过。详细分母与限制见 [评测说明](docs/BENCHMARK.md#接入与记忆维护限制)。

**双击后没有窗口或出现白屏怎么办？**

确认已安装 WebView2 Runtime。运行 `tbrain.cmd doctor` 检查安装与接入状态；Codex Hook 需在 `/hooks` 信任。升级后工具仍为旧版时重开 Agent。

**支持 macOS 或 Linux 吗？**

当前发布包仅支持 Windows x64。其他平台的桌面运行与接入没有完成验收。

## Contributing | 参与贡献

通过 [Issue](https://github.com/Denis-Tang/tbrain/issues) 反馈问题，附上版本、宿主和复现步骤；提交 Pull Request 前运行相关测试。请从日志和样本中移除密钥、个人路径与私有项目内容。

## License | 开源协议

采用 [Apache License 2.0](LICENSE)。第三方组件许可见 [THIRD-PARTY-NOTICES.txt](THIRD-PARTY-NOTICES.txt)。

## Acknowledgements | 致谢

感谢 [Model Context Protocol](https://modelcontextprotocol.io/)、[jieba](https://github.com/fxsjy/jieba)、[PyInstaller](https://github.com/pyinstaller/pyinstaller)、[WebView2](https://developer.microsoft.com/microsoft-edge/webview2/) 等开源项目与平台。评测对照使用了 [Basic Memory](https://github.com/basicmachines-co/basic-memory)，维度参考 [LongMemEval](https://arxiv.org/abs/2410.10813)；本项目没有运行 LongMemEval 标准榜单测试。
