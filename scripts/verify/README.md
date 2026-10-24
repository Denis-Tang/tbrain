# 开发期验证脚本

这些脚本**不属于 pytest**，需要手动运行；真实宿主脚本会调用真实模型、产生少量费用。

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

## 2. 真实宿主端到端（会花少量模型费用）

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
