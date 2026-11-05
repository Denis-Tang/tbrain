# Tbrain

> 给编程 Agent 用的本地记忆工具。把要求、决定和进度存成 Markdown，换会话或换 Agent 时接着做。

[**下载 0.7.0**](https://github.com/Denis-Tang/tbrain/releases/tag/v0.7.0) · [**使用文档**](#usage--使用指南) · [**实测结果**](docs/BENCHMARK.md) · [**问题反馈**](https://github.com/Denis-Tang/tbrain/issues)

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

### 长任务实测

2026 年 11 月的隔离评测比较了 DSH、Pi Desktop 各自的原生记忆、Tbrain 实验兼容版和 Basic Memory。每组一条五阶段工程链，包含五次新会话检查；模型为 `deepseek-v4.1-flash`，medium thinking。

| 宿主 | 原生记忆检查 | Tbrain 检查 | Basic Memory 检查 | Tbrain 接续答题输入相对原生 |
|---|---:|---:|---:|---:|
| DSH | 69/69 | 67/69 | 67/69 | 减少 26.2% |
| Pi Desktop | 68/69 | 69/69 | 66/69 | 减少 20.8% |

DSH 的五次接续答题总输入从 8,426,194 降至 6,214,688 Token，统一价格估算费用减少 28.0%，同时少通过两次检查。Pi 的估算费用减少 17.1%，但累计答题耗时更长：1,627.728 秒，原生为 688.903 秒，且有一次控制器干预。

这些数字来自实验版 `0.7.0-beta.1+lab.1`，不是 0.7.0 的重新横评。0.7.0 纳入了该实验版的快照兼容修复。每个条件只有一条独立链，检查包含重复事实；结果不能推广为普遍准确率、完整开发任务的费用节省或整体提速。原话采集和旧决定退役还有缺口，见 [评测方法、完整对照与失分说明](docs/BENCHMARK.md)。

## Features | 核心功能

### 1. 保存进度，按需接续

通过四个工具工作：`bootstrap` 返回启动包，`search` 检索历史，`save` 保存增量，`resume` 读取旧会话交接包。会话原话、完成项、决定、证据与未完成目标分开保存。

### 2. 让项目规则跟着工作目录走

为项目关联一个或多个目录，维护目标、硬规则、当前阶段和常用命令。新建项目可向已接入的 Codex、DSH 登记；Codex 保留多目录，DSH 支持单目录。Pi Desktop 暂无可用的外部项目登记接口，具体条件见 [宿主接入说明](integrations/README.md#新建项目联动)。

### 3. 在本地查看和核对记忆

Markdown 是事实源，SQLite 是可重建的检索索引。检索支持中文分词和代码标识符；归档条目仍可查找。明确标为被取代、已废止或待确认的决定不进入有效决定交接，但旧决定的识别与退役仍依赖 Agent 正确维护。

### 4. 桌面管理与版本切换

在桌面管理知识库、项目和 Agent 接入，检查更新并回退已安装版本。宿主使用固定命令入口，版本切换后按提示重开宿主。

## Quick Start | 快速开始

### 环境要求

- Windows 10/11 x64。
- Microsoft Edge WebView2 Runtime。
- 已安装并配置好至少一个支持的 Agent。模型及 API 配置在 Agent 内完成。

发布包包含 Python、.NET 和 Node.js 运行时，无需安装开发工具。

### 安装与配置

1. 下载 [0.7.0 发布页](https://github.com/Denis-Tang/tbrain/releases/tag/v0.7.0) 中的 `tbrain-0.7.0-windows-x64.zip` 并解压。
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
& "$env:LOCALAPPDATA\Programs\Tbrain\bin\tbrain.cmd" upgrade --from .\tbrain-0.7.0-windows-x64.zip
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

构建 0.7.0 分发包：

```powershell
pwsh -NoProfile -File scripts/build.ps1 -OutputDirectory dist/0.7.0
```

版本号统一定义在 `src/tbrain/__init__.py`。构建产出可运行目录、ZIP、同名 `.sha256` 和 `latest.json`；Release 上传后三项。0.7.0 的本地全量回归为 **292 项通过**，其中一项覆盖已有历史快照重复保存的兼容问题。真实宿主验证需单独执行，不能用单元测试替代，见 [验证说明](https://github.com/Denis-Tang/tbrain/blob/main/scripts/verify/README.md)。

## Roadmap | 开发计划

- **0.7.0 已包含：** 记忆保存与检索、跨会话接续、有效决定筛选、历史版本、项目多目录、Codex/DSH 新建项目联动、Pi 异步扩展及快照兼容修复。
- **后续优先核验：** DSH 会话归属与原话采集、旧决定和阶段授权退役、项目侧栏实际刷新行为。
- **后续评测：** 增加独立任务链和任务主题，补测跨宿主接力。具体时间尚未确定。

## FAQ | 常见问题

**会自动清理错误或过时的记忆吗？**

不会自动判断所有冲突。程序过滤明确标为失效的决定，Agent 仍需核对来源并维护状态。实测中存在旧决定未退役、阶段授权延迟清理的问题。

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
