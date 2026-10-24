# Tbrain

给 coding agent 用的本地外挂大脑：记录会话、跨会话和跨 agent 接续任务。数据存成本地 Markdown，Obsidian 可以直接读。

> **当前版本：0.6.0（第一期最小闭环已完成）。** Markdown 唯一事实源 + SQLite 索引、handoff/v1 会话总结、`bootstrap`/`search`/`save`/`resume` 四个 MCP 工具、四类 hook、版本化安装与固定入口、四宿主适配（`connect`/`disconnect`/`doctor`）、桌面三页均已实现；8 条验收场景与单元测试全部通过（`pytest -q` 223 passed），真实 Claude Code / Codex 端到端链路已验证。DSH 桥依赖需手动安装；pi 的扩展与 IPC 已就绪，真实模型链路待本机 provider 可用后验证。

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

构建产物：`dist/tbrain/`（可直接运行/分发）、`dist/tbrain-<版本>-windows-x64.zip` 与同名 `.sha256`。安装与四宿主接入见 `docs/UPGRADE.md`：

```powershell
dist\tbrain\tbrain.exe install                 # 安装到 %LOCALAPPDATA%\Programs\Tbrain
%LOCALAPPDATA%\Programs\Tbrain\bin\tbrain.cmd connect claude-code
%LOCALAPPDATA%\Programs\Tbrain\bin\tbrain.cmd connect codex
%LOCALAPPDATA%\Programs\Tbrain\bin\tbrain.cmd connect dsh
%LOCALAPPDATA%\Programs\Tbrain\bin\tbrain.cmd connect pi
%LOCALAPPDATA%\Programs\Tbrain\bin\tbrain.cmd doctor
```

开发期验证脚本（不会自动跑、真实宿主脚本会产生少量模型费用）：`scripts/verify/upgrade_sim.py`、`scripts/verify/real_hosts.py`。

## 文档

- `docs/ORIGIN.md`：来源与重构起点（second-brain 提交 `3af5036`）。
- `docs/RESEARCH-hosts.md`：四宿主插件机制调研（第 1 步，含实测与源码证据）。
- `docs/ARCHITECTURE.md`：实现架构与模块接口契约。
- `docs/DECISIONS.md`：设计稿未写到的细节与本项目自定的决定（含真实宿主验证结果）。
- `docs/UPGRADE.md`：版本管理、升级/回退、四宿主接入与 doctor 说明。
- `docs/MIGRATION-REPORT.md`：旧版数据迁移 dry-run 报告（迁移尚未执行）。

## 与 Shared Brain 0.5.2 的关系

Tbrain 是 Shared Brain 0.5.2 的重构版本，目标是更小的闭环、更稳的跨宿主接续与可解释的本地数据。旧版数据和旧的宿主接入在第 6 步迁移/解绑之前保持只读，不混用。

## 许可

MIT。见 `LICENSE` 与 `THIRD-PARTY-NOTICES.txt`。
