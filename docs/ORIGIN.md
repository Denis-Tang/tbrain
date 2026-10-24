# 来源（ORIGIN）

- 重构起点：Shared Brain 0.5.2，仓库 `https://github.com/Denis-Tang/second-brain`，git 提交 **`3af5036`**（release: 发布 0.5.2 项目保存归属与桌面输入改进）。
- 复制日期：2026-10-24。
- 复制方式：按 `git ls-files` 复制受版本管理跟踪的文件（工作区中当时未提交的 README 改动也一并复制），未复制 `build/`、`dist/`、`.git/`、`.venv/`、`desktop/bin|obj/`、`__pycache__/`、`.pytest_cache/`、`src/*.egg-info/`。
- 未复制：旧仓库未跟踪的 `docs/`（发布宣传物料，不属于源码）。
- 改名范围：包名 `shared_brain` → `tbrain`、可执行文件 `shared-brain.exe` → `tbrain.exe`、MCP server 名 → `tbrain`、应用数据目录 `~/.shared-brain` → `~/.tbrain`、环境变量 `SHARED_BRAIN_HOME` → `TBRAIN_HOME`、桌面程序 `SharedBrain.Desktop` → `Tbrain.Desktop`、界面与文档名称 → Tbrain。历史调研文档 `docs/RESEARCH-hosts.md` 中保留旧名称，用于记录旧版真实状态与证据，不作产品标识。
- 本仓库为独立思考的 Tbrain 项目，0.6.0 起沿用 MIT 许可。
