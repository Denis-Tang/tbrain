# Tbrain

给 coding agent 用的本地外挂大脑：记录会话、跨会话和跨 agent 接续任务。数据存成本地 Markdown，Obsidian 可以直接读。

> **当前版本：0.6.0（施工中）。** 本仓库处于从 Shared Brain 0.5.2 重构为 Tbrain 的第 2 步：已完成建仓与全量改名，核心（Markdown 会话总结、handoff/v1、SQLite 索引、bootstrap/search/save/resume 四工具、四宿主 hook 与版本管理）按 `docs/` 里的设计稿在后续步骤重写。

## 目标（0.6.0 第一期）

- **Markdown 唯一事实源**：会话总结、项目提示词、公共池、会话索引全部是 Obsidian 可读的本地文件；SQLite 只做索引。
- **最小闭环**：4 个 MCP 工具（`bootstrap` / `search` / `save` / `resume`）+ 4 类 hook；模型只提交增量，编号、校验、排序、注入、防覆盖由程序确定性完成，不调用大模型。
- **跨宿主**：Claude Code、Codex（CLI + 桌面版）、DeepSeek Harness、pi 四宿主接入；程序升级后不会再连到旧版本进程。
- **准确交接**：会话总结遵循 handoff/v1，支持“接续自”、证据、未走通的路、下一步与状态。

第一期**不包含**：维护轮、错误库、向量检索、附件注册界面、子项目开关界面（后端保留读取接口）。

## 数据目录

- 应用数据目录：`~/.tbrain`（`TBRAIN_HOME` 可覆盖），保存设置、索引、进程登记、更新状态。
- 知识库（vault）：用户自定义路径，当前默认 `%USERPROFILE%\Documents\TbrainVault`（可在设置中修改），由程序创建：
  `会话总结/`、`项目/`、`公共池/`、`独立会话池/`。

## 开发

要求：Windows 10/11 x64、Python 3.11+、.NET 8 SDK（桌面程序）、Node.js（Hook 适配脚本；分发包会内置）。

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m pytest -q
```

运行：

```powershell
.venv\Scripts\python.exe -m tbrain desktop   # 桌面程序（WebView2）
.venv\Scripts\python.exe -m tbrain mcp       # stdio MCP 服务
.venv\Scripts\python.exe -m tbrain --version
```

构建分发包（桌面 + PyInstaller + Node 运行时）：

```powershell
pwsh scripts/build.ps1
```

版本号只在 `src/tbrain/__init__.py` 的 `__version__` 写一处；`pyproject.toml` 动态读取，桌面构建由 `scripts/build.ps1` 注入。

## 文档

- `docs/ORIGIN.md`：来源与重构起点（second-brain 提交 `3af5036`）。
- `docs/RESEARCH-hosts.md`：四宿主插件机制调研（第 1 步，含实测与源码证据）。
- `docs/DECISIONS.md`：设计稿未写到的细节与本项目自定的决定（持续更新）。
- `docs/UPGRADE.md`：版本管理与升级流程（第 3 步产出）。

## 与 Shared Brain 0.5.2 的关系

Tbrain 是 Shared Brain 0.5.2 的重构版本，目标是更小的闭环、更稳的跨宿主接续与可解释的本地数据。旧版数据和旧的宿主接入在第 6 步迁移/解绑之前保持只读，不混用。

## 许可

MIT。见 `LICENSE` 与 `THIRD-PARTY-NOTICES.txt`。
