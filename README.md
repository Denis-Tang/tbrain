# Tbrain

给 coding agent 用的本地外挂大脑：记录会话、跨会话和跨 agent 接续任务。数据存成本地 Markdown，Obsidian 可以直接读。

> **当前版本：0.6.2。** 新用户双击后可在界面内安装、选择知识库并接入 Agent；已有用户直接进入总观。包含 0.6.1 的 Claude 保存与 DSH 原生插件修复。

## 首次使用（Windows）

要求 Windows 10/11 x64 和 Microsoft Edge WebView2 Runtime。分发包已包含 Python、.NET 和 Node 运行时，无需安装开发工具。

1. 先安装并登录需要使用的 Claude Code／Claude IDE、Codex、DSH 或 pi；模型和 API 配置在各 Agent 中完成，Tbrain 不需要模型密钥。
2. 解压发布 ZIP，双击里面的 `tbrain.exe`。
3. 选择知识库文件夹，勾选要接入的 Agent，点击“开始使用”。程序安装到当前用户目录，并添加开始菜单入口；未检测到的 Agent 可在安装后点击“刷新”。可以先不接入 Agent。
4. 根据完成页提示重开 Agent。Claude 新开会话；DSH 完整退出后重开；pi 新开会话或 `/reload`；Codex 重开后在 `/hooks` 中审核并信任 Tbrain Hooks。
5. 在“项目配置”添加项目及代码目录。不同 Agent 在同一目录工作时匹配到同一项目；换会话时保存，再用 `resume` 接续。

以后从 Windows 开始菜单打开 Tbrain；也可以在“设置 → 宿主接入”连接或断开 Agent。配置修改前生成同目录 `.bak`，保留其他条目。完成页表示配置已写入，实际运行需在 Agent 新会话中验证。

首次界面未出现时，已有知识库设置会继续沿用；无需重新初始化。自定义安装的命令行 Agent 需要将启动命令加入 PATH。DSH 检测桌面版默认安装位置，自定义位置可用下方命令手动接入。

## 目标（0.6.0 第一期）

- **Markdown 唯一事实源**：会话总结、项目提示词、公共池、会话索引全部是 Obsidian 可读的本地文件；SQLite 只做索引。
- **最小闭环**：4 个 MCP 工具（`bootstrap` / `search` / `save` / `resume`）+ 4 类 hook；模型只提交增量，编号、校验、排序、注入、防覆盖由程序确定性完成，不调用大模型。
- **跨宿主**：Claude Code、Codex（CLI + 桌面版）、DeepSeek Harness、pi 四宿主接入；程序升级后不会再连到旧版本进程。
- **准确交接**：会话总结遵循 handoff/v1，支持“接续自”、证据、未走通的路、下一步与状态。

第一期**不包含**：维护轮、错误库、向量检索、附件注册界面、子项目开关界面（后端保留读取接口）。

## 数据目录

- 应用数据目录：`~/.tbrain`（`TBRAIN_HOME` 可覆盖），保存设置、索引、进程登记、更新状态。
- 知识库（vault）：首次使用时选择，默认建议用户文档目录下的 `TbrainVault`，可在设置中修改，由程序创建：
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
- `docs/MIGRATION-REPORT.md`：迁移前的 dry-run 方案；正式迁移结果见新 vault 内的迁移报告。

## 与 Shared Brain 0.5.2 的关系

Tbrain 是 Shared Brain 0.5.2 的重构版本，目标是更小的闭环、更稳的跨宿主接续与可解释的本地数据。旧版数据和旧的宿主接入在第 6 步迁移/解绑之前保持只读，不混用。

## 许可

MIT。见 `LICENSE` 与 `THIRD-PARTY-NOTICES.txt`。

## 0.6.1 开发与接续

- 保存失败的条目仍会触发收尾提醒；同一条第二次重试仍不合格时，原文进入“待整理”，不再退回。
- 用户手改过的目标或下一步会持续归用户维护；其他字段仍可保存。确认恢复模型维护后，可移除总结文件头 `user_owned_fields` 中的对应字段。
- `resume` 返回当前会话的 `session_id`，来源会话放在 `resumed_session_id`；旧版迁移正文随交接包返回。
- DSH 更新后运行 `tbrain connect dsh`，重开 DSH；Claude 重新打开会话或重连 MCP。`doctor` 检查配置，真实执行由宿主新会话验证。
- 总观状态改为点击展开的菜单；会话元数据读取后在本地筛选，“全部”按项目默认折叠，展开后每次加载 30 条。外部文件变更通过“刷新”读取。
- 项目首页展示主项目与子项目摘要，点击进入二级页查看子项目及提示词；设置时区支持“跟随系统”。
- 回归验证：`.venv\Scripts\python.exe -m pytest -q`。`tests/test_host_transport.py` 覆盖保持 stdin 打开的 MCP 连续保存和 DSH 原生事件到真实 CLI 的链路，不调用模型。
