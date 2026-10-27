# Tbrain

给 coding agent 用的本地外挂大脑：记录会话、跨会话和跨 agent 接续任务。数据存成本地 Markdown，Obsidian 可以直接读。

Tbrain 解决的是 agent 换会话、换宿主之后忘记进度的问题：每轮对话的总结、项目提示词和可复用经验写进本地知识库，模型只提交增量，编号、校验、排序、去重由程序确定性完成，不调用大模型。面向本机使用 Claude Code、Codex、DSH、pi 的个人开发者。

## Features

- **四个 MCP 工具**：`bootstrap` 注入启动包，`search` 检索历史，`save` 保存增量，`resume` 接续旧会话。
- **四宿主接入**：Claude Code、Codex、DSH、pi；配置指向固定入口，升级不改宿主配置。
- **Markdown 是唯一事实源**：会话总结、项目提示词、公共池、索引都是 Obsidian 可读的本地文件；SQLite 只做检索。
- **中文与代码检索**：SQLite FTS + jieba 分词，支持按项目、会话、公共池分别检索。
- **桌面程序**：总观、项目配置、Agent 管理、设置四页；项目树与父子关系、项目提示词编辑、首次使用向导、升级与回退。
- **可解释的本地数据**：写入前比对指纹保护用户手改内容，失败条目进“待整理”，版本目录保留多份便于回退。

## Quick Start

### Requirements

- Windows 10/11 x64
- Microsoft Edge WebView2 Runtime
- 需要接入的 Agent（Claude Code / Codex / DSH / pi）已安装并登录；模型与 API 配置在各 Agent 内完成，Tbrain 不需要模型密钥

发布 ZIP 已包含 Python、.NET 与 Node 运行时，不需要安装开发工具。

### Installation

1. 解压 `tbrain-<版本>-windows-x64.zip`，双击目录里的 `tbrain.exe`。
2. 在首次设置页选择知识库文件夹、勾选要接入的 Agent，点击“开始使用”。程序安装到 `%LOCALAPPDATA%\Programs\Tbrain` 并创建开始菜单入口。
3. 按完成页提示重开 Agent：Claude 新开会话；DSH 完整退出后重开；pi 新开会话或 `/reload`；Codex 重开后在 `/hooks` 审核并信任 Tbrain Hooks。
4. 在“项目配置”添加项目及代码目录。不同 Agent 在同一目录工作时匹配到同一项目。

### Run

以后从 Windows 开始菜单打开 Tbrain；也可以在“Agent 管理”连接或断开 Agent。命令行入口固定为 `%LOCALAPPDATA%\Programs\Tbrain\bin\tbrain.cmd`：

```powershell
tbrain.cmd --version
tbrain.cmd doctor
```

源码方式运行见下方 Development。

## Usage

**桌面端**：总观看会话列表与全局提示词；项目配置管理主项目/子项目、工作目录与项目提示词；Agent 管理页每个宿主一个总开关；设置页改知识库、时区、校时、超时兜底与换会话提醒。

**Agent 内**：会话开始自动注入启动包（目标、进度、下一步、项目硬规则）；接续旧任务时调用 `resume`；需要保存时调用 `save`，只提交本轮变化。

目标或长期规则变化、阶段完成或切换、关键路径或命令变化、旧内容失效时，Agent 在同次任务的 `save` 中用 `project_prompt` 更新对应节；空白节只补已确认内容，无变化不改写。写作与更新规范由后端统一放进项目启动包，MCP 和 pi 都按同一规则维护。

`save` 的 `ok=true` 表示全部通过；`ok=false` 时，`written` 列出的内容仍已保存，只补交 `rejected` 中的失败部分。操作按顺序执行，先保存证据，再引用返回的 E 编号；正文连引用最多 200 字，超长退回，不截断。失败原文进入“待整理”也不算通过。宿主采集的测试结果不会被模型补报覆盖；保存后产生新测试结果会再次提醒补存。

**命令行**：

```powershell
tbrain.cmd project list
tbrain.cmd project add --name Tbrain --path D:\work\tbrain
tbrain.cmd project add --name 文档 --path D:\work\docs --parent Tbrain
tbrain.cmd prompt global
tbrain.cmd prompt project --name Tbrain
tbrain.cmd sessions --filter 进行中
```

MCP 工具的完整参数见 `docs/ARCHITECTURE.md`；宿主适配细节见 `docs/RESEARCH-hosts.md`。

## How It Works

1. 宿主 Hook 在会话开始、用户提交、工具调用、会话结束等时机把会话身份与用户指令交给 Tbrain。
2. `bootstrap` 返回启动包：项目提示词、上一轮目标与下一步、相关历史与可复用经验。
3. 模型在阶段结束时调用 `save`，只提交增量；程序校验条目格式、核对证据编号、写入 Markdown 并刷新索引。
4. 新会话需要继续旧任务时调用 `resume`：读取来源会话的保存点，返回交接包与当前会话 ID，来源总结保持不变。
5. 桌面程序通过 WebView2 读取同一套 Markdown 与 SQLite 索引，不另存一份数据。

会话身份按 `宿主名:宿主会话ID` 区分；同一 ID 恢复原总结。宿主恢复或压缩时明确提供旧 ID（`parent_session_id`），程序登记新旧映射；后续 hook 与工具调用都读取原总结。其余不同 ID（包括分支）各写自己的文件，开场文字相同也不会合并。换会话继续任务时显式调用 `resume`。

## Architecture

```
src/tbrain/            Python 后端、MCP 服务与 CLI
  clock.py naming.py   时间基准、会话命名
  store.py index.py    Markdown 读写、区块所有权、SQLite 索引与检索
  session.py handoff.py identity.py hooks.py    bootstrap/save/resume、交接包、hook 语义
  prompt.py service.py  提示词校验、服务层
  mcp_server.py desktop.py  MCP 入口、桌面后端 API
  installer.py update.py proc.py paths.py  安装、升级、进程登记、路径
  hosts/ integrations/  四宿主配置写入与插件
  web/                 WebView2 前端（HTML/JS/CSS）
desktop/               WPF + WebView2 桌面壳
scripts/               构建、升级模拟与真实宿主验证脚本
tests/                 单元测试与 8 条设计验收场景
docs/                  调研、架构、决策、升级、迁移文档
```

技术栈：Python 3.11+、`mcp`、SQLite FTS5、jieba、httpx、keyring、PyYAML；桌面壳为 .NET 8 WPF + WebView2；Node.js 用于部分宿主的 Hook 适配（分发包内置）。

数据目录：

- 应用数据：`%USERPROFILE%\.tbrain`（`TBRAIN_HOME` 可覆盖），存设置、索引、进程登记、更新状态。
- 知识库（vault）：安装时选择，默认建议用户文档目录下的 `TbrainVault`，由程序创建 `会话总结/`、`项目/`、`公共池/`、`独立会话池/`、`附件/`。

## Configuration

`%USERPROFILE%\.tbrain\settings.json`：

| 字段 | 说明 |
|---|---|
| `vault_path` | 知识库目录 |
| `timezone` | IANA 时区；留空表示跟随系统 |
| `ntp_enabled` | 联网校时（NTP，失败回退 HTTPS Date） |
| `timeout_hours` | 会话超时兜底 |
| `reminder_context_switch` | 换会话提醒（仅 DSH、pi） |
| `install_root` | 安装目录，默认 `%LOCALAPPDATA%\Programs\Tbrain` |

环境变量 `TBRAIN_HOME` 可覆盖应用数据目录。程序只改自己的区块；用户手改过的目标/下一步会归用户维护，其余字段仍可保存。

## Advanced Usage

- **接入或断开宿主**：`tbrain.cmd connect claude-code|codex|dsh|pi` / `disconnect <host>`；修改配置前生成同目录 `.bak`。Codex 的 Hook 需要在 `/hooks` 里审核信任。
- **升级与回退**：点击桌面左下角版本号检查更新，发现新版后选择升级或取消；更新读取 Release 附件 `latest.json`，不使用 GitHub 查询 API。也可 `tbrain.cmd upgrade --from <包目录或 zip>`，`tbrain.cmd rollback` 切回上一个已安装版本。版本目录默认保留两份。
- **升级版本核对**：更新检查以目标安装目录 `current.txt` 选中的版本为准；没有安装记录时才使用运行中程序的版本。升级后用 `tbrain.cmd version` 的 `installed_version` 或 `current.txt` 核对实际版本；退出码 0 本身不表示发生了升级。
- **迁移旧数据**：`tbrain.cmd migrate --old-home <旧应用数据目录> --dry-run` 先看报告，确认后加 `--execute`。旧数据只读，不删除。
- **手动检查**：`tbrain.cmd doctor` 检查安装、版本、进程与四宿主配置状态；真实执行仍以宿主新会话为准。
- **验证脚本**：`scripts/verify/upgrade_sim.py`（隔离目录里的真实升级模拟）、`scripts/verify/real_hosts.py`（真实宿主端到端，会产生少量模型费用）。

## Development

要求 Windows 10/11 x64、Python 3.11+、.NET 8 SDK（桌面壳）、Node.js（Hook 适配，分发包会内置）。

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m tbrain desktop
```

构建分发包：

```powershell
pwsh scripts/build.ps1
```

产物为 `dist/tbrain/`（可直接运行）、`dist/tbrain-<版本>-windows-x64.zip`、同名 `.sha256` 和更新清单 `dist/latest.json`；发布时需上传三份附件。版本号只在 `src/tbrain/__init__.py` 的 `__version__` 写一处，`pyproject.toml` 与桌面程序集都从它读取。

## Roadmap

已完成（0.6.x）：四宿主接入与固定入口、版本目录与升级/回退、首次使用向导、Agent 管理、项目树与项目提示词、跨会话接续与证据校验。

接下来：公开更新源的全链路验证、附件注册界面、维护轮与错误库；第一期不包含向量检索与子项目开关界面。

## FAQ / Troubleshooting

- **双击后没窗口或白屏**：确认已安装 Microsoft Edge WebView2 Runtime；应用数据与日志在 `%USERPROFILE%\.tbrain`。
- **Codex 的 Hook 没执行**：非 managed hook 需要手动信任，在 Codex 里执行 `/hooks` 审核；Tbrain 不自动写 `trusted_hash`。
- **升级后工具没变化**：宿主可能缓存了旧 MCP 进程，重开 Agent，或运行 `tbrain.cmd evict`。
- **同版本不会提示更新**：应用内更新只安装更高版本；修复同版本的安装请用 `tbrain.cmd install --from <包目录>`。
- **知识库换到别的目录**：在设置里改 `vault_path` 后保存并重启；旧目录不自动搬迁，可用 `migrate` 或手动复制。

## License

Apache License 2.0，见 [LICENSE](LICENSE)。第三方组件许可见 [THIRD-PARTY-NOTICES.txt](THIRD-PARTY-NOTICES.txt)。
