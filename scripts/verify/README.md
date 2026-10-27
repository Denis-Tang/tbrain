# 开发期验证脚本

这些脚本**不属于 pytest**，需要手动运行；真实宿主脚本会调用真实模型、产生少量费用。

## 当前源码宿主验收（2026-10-27）

Codex/pi 先通过临时入口验证当前源码；随后按用户要求，Claude 使用日常登录和真实 Tbrain 知识库补测，DSH 使用实际运行的 Desktop 与已配置 MCP 桥接直接派单。Claude 的 MCP 启动参数仅在本次命令指向当前源码，不改用户配置；DSH Desktop 使用现有 Tbrain 0.6.4 接入。

| 宿主 | 真实模型结果 | 已核对的磁盘结果 |
|---|---|---|
| Codex CLI 0.148.0 | 新建、恢复、分支均通过；使用 ChatGPT 登录的 gpt-5.6-luna | 恢复沿用同一 ID、U1→U2；分支新 ID 独立保存；父总结逐字节不变 |
| pi 1.4.0 | 新建、恢复、分支均通过；openai-codex/gpt-6-luna | 恢复 U1→U2、目标更新；分支产生独立总结，父总结目标保留 |
| Claude Code 2.1.132 | 日常配置下新建、续接、分支均通过，haiku | 新建 U1/D1；续接同 ID 达 U2/D2；分支新 ID、独立 D1，父总结目标与进度保留 |
| DSH Desktop 0.2.1-rc.3 | MCP submit_task / continue_task 两轮通过 | 原生工具调用 seq19/20、seq32/33；两次 Tbrain save 均 ok=true；真实总结累计 U2/D2，目标更新为 DSH-DESKTOP-DIRECT-CONTINUE |

Codex 使用临时 CODEX_HOME，通过硬链接让宿主自行使用已有登录，不读取或复制认证内容；测试后删除临时引用。临时配置只给隔离 MCP 服务的 save 设置 `approval_mode="approve"`，其余 shell 保持只读；否则 exec 的 never 策略会拒绝 MCP 写入。测试使用已核对的临时 hooks，并仅在本次命令启用 hook 信任跳过参数，不修改日常信任状态。

已报告费用：Claude 全部补测累计 **0.214755 美元**（其中日常配置三轮为 0.144072 美元），pi 三轮估算 **0.00129186 美元**，合计约 **0.21605 美元**。Codex 使用现有 ChatGPT 订阅额度，DSH Desktop 未返回美元费用；这不是完整账单核对。用户授权上限为 1 美元，未充值或开通新计费服务。

范围限制：已验证上述四宿主的实际工具保存链路；DSH 是 Desktop MCP 派单，不是之前失败的旧 npm CLI。DSH Desktop 分支、真实压缩和宿主更换 ID 的恢复事件仍未覆盖；源码与日常安装版接入的证据分别列明，不能写成候选安装包已通过全部宿主回放。

MCP 接入修复：`deepseek_harness` 配置仍在，但安装目录 `agent-mcp/` 已缺失，stdio 握手直接关闭。已从 2026-10-20 原接入记录恢复 `bridge.mjs` 与 `stdio.mjs`，保留原本机认证和任务归属机制；创建新的运行认证与任务索引，未读取或打印认证值。原丢失任务索引未恢复，Harness 原生会话不改。Desktop profile 只做停用/启用以重新加载，修改前已生成 `.bak`；Codex 配置不变，账户、模型和权限不变。

闪窗修复：`service.py` 的 `git_info()` 已为 3 次 Git 调用设置 `CREATE_NO_WINDOW`。源码从无控制台进程实测可见终端窗口从 3 个降到 0，分支、提交号和工作区改动数仍正常读取。

0.6.5 候选包：`dist/0.6.5/tbrain-0.6.5-windows-x64.zip`，177,180,890 字节；SHA256 为 `89c2fe0e3066b1b873dd29bd7b672181d73f1794c66c88ae4ede3030c0d9c1dd`。全量测试 **267 passed in 55.53s**；包内 EXE 返回版本 0.6.5，标准 MCP 握手及 bootstrap/search/save/resume 四工具发现通过，必要文件、ZIP 结构与校验文件核对通过。候选验收时尚未安装或发布，日常安装为 0.6.4；包内窗口行为未单独实测。后续发布状态以 GitHub Releases 为准。

包冒烟影响：候选 MCP 在日常 home 启动，触发现有新版淘汰旧版进程逻辑，导致原桌面和本聊天 MCP 退出。已重新启动原安装的 0.6.4 桌面，`current.txt` 仍为 0.6.4；本聊天的原 MCP 连接已关闭，收尾保存使用单次标准客户端连接原安装版完成。

---

## 1. 升级模拟（不需要模型、不碰真实安装/宿主配置）

```powershell
# 先生成两个测试包（在仓库根目录）
pwsh scripts/build.ps1                       # 产出 dist/tbrain-0.6.0-... 并复制到 .research/packages/tbrain-0.6.0
# 构建 0.6.1 测试包：临时把 src/tbrain/__init__.py 版本改成 0.6.1，重新 build.ps1，
# 复制 dist/tbrain 到 .research/packages/tbrain-0.6.1，然后把版本改回 0.6.0。
.venv\Scripts\python.exe scripts/verify/upgrade_sim.py
```

脚本在 `.research/upgrade-sim/` 里安装 0.6.0 → 启动长驻 MCP 进程 → 用 0.6.0 的固定入口升级到 0.6.1，验证：

- `current.txt` 切到 0.6.1，`versions/` 保留最近 2 个版本；
- 旧版本 MCP 进程被通知/强制结束；
- 四个伪造宿主的配置文件内容哈希不变（升级不改宿主配置）；
- 新入口 `doctor` 无红色项。

## 2. 历史脚本：真实宿主端到端（会花少量模型费用）

注意：`real_hosts.py` 仍固定安装 0.6.0，且包含认证文件复制路径；不能直接用它声称本轮源码已经验收。以下保留历史使用方式。

```powershell
.venv\Scripts\python.exe scripts/verify/real_hosts.py claude
.venv\Scripts\python.exe scripts/verify/real_hosts.py codex
.venv\Scripts\python.exe scripts/verify/real_hosts.py pi
```

安全边界：

- Tbrain 安装、应用数据、vault 全部在 `.research/e2e/<host>/` 临时目录；
- Claude：用 `CLAUDE_CONFIG_DIR` 生成临时 hooks/MCP 配置，再以 `--settings` / `--mcp-config` 运行，不改真实配置；
- Codex：临时 `CODEX_HOME`，只机械复制 `auth.json` 用于登录（测试后删除整个临时目录），`--dangerously-bypass-hook-trust` 仅用于测试；
- pi：优先用 `-e` 显式加载临时扩展，不复制 pi 配置/凭据；
- 不读取、不打印任何凭据内容。

已验证结果（2026-10-24）：

| 宿主 | 结果 |
|---|---|
| Claude Code 2.1.132 | 通过：hook 立项 + MCP `save` 写入 D#/下一步；约 $0.19 |
| Codex CLI 0.148.0 | 通过：SessionStart/UserPromptSubmit/Stop hook + MCP save；约 41.6k tokens |
| pi 0.84.2 | hook/tool IPC 通过；真实模型链路被本机 provider（magpie 本地代理未运行）阻断，待 provider 可用后重跑 |
