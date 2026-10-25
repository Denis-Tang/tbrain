# Tbrain 升级与宿主接入说明

本文说明 Tbrain 的版本化安装、固定入口、进程淘汰、升级/回退流程，以及四个宿主（Claude Code、Codex、DeepSeek Harness、pi）的接入与 `doctor` 检查。

0.6.2 新用户可解压后双击 `tbrain.exe`，在首次设置页选择知识库和 Agent，点击“开始使用”。安装完成后从 Windows 开始菜单启动；下文命令用于手动安装、管理与排查。Agent 需要提前安装和登录；未勾选的宿主不写配置。

## 1. 版本化安装布局

默认安装到 `%LOCALAPPDATA%\Programs\Tbrain`（可在设置里改 `install_root`，或用环境变量 `TBRAIN_INSTALL_ROOT` 覆盖，测试/便携场景使用）：

```text
%LOCALAPPDATA%\Programs\Tbrain\
  bin\tbrain.cmd            # 稳定入口：读 current.txt → versions\<ver>\tbrain.exe %*
  current.txt               # 当前版本号（原子写）
  current\                  # 可选 junction → versions\<ver>（兼容旧式引用；创建失败会跳过）
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
"<安装目录>\bin\tbrain.cmd" --home "<应用数据目录>" mcp
```

入口脚本是纯 ASCII 的 cmd 批处理：读取同目录 `..\current.txt`，然后执行 `versions\<当前版本>\tbrain.exe %*`。升级只做两件事：

1. 把新版本解压到 `versions\<新版本>`；
2. 原子改写 `current.txt`（并尽力更新 `current` junction）。

因此 **Claude Code / Codex / DSH 的 hook 与 MCP 配置、pi 的扩展文件都不需要重新写**，新会话（或 DSH 的 MCP 热重连、pi 的 `/reload`）即使用新版本。

## 3. 进程登记与旧版本淘汰

`~/.tbrain/processes/<pid>.json` 记录长驻进程：`pid`、`version`、`role`、`exe`、`started_at`、`updated_at`。desktop 与 MCP 进程启动时登记，退出时删除；升级后新版本会：

1. 找到登记的旧版本进程，写 `processes/<pid>.exit`（内容是退出请求，含 requester／当前版本）；
2. 旧进程的后台 watcher 线程发现 `.exit` 后优雅退出（MCP/desktop 由调用方注册回调）；
3. 等待超时（默认 5 秒）仍未退出的，用 `taskkill /PID <pid> /T /F` 强制结束；
4. 清理陈旧的登记与退出请求文件。

边界：Claude/Codex 的 MCP 子进程由宿主持有，被结束后旧会话下一次工具调用会失败或由宿主自动重连；重开宿主/会话最稳妥。doctor 会把“旧版本进程仍在运行”显示为黄色提醒。

## 4. 升级流程

`tbrain upgrade` / 桌面“检查更新并升级”：

1. **检查**：GitHub Releases（`Denis-Tang/tbrain`，匿名 API，结果缓存 6 小时，失败/限流降级为结构化结果，不抛异常）；
2. **下载**：流式写入 `~/.tbrain/updates/<版本>/`，校验 SHA256；下载期间不碰安装目录；
3. **校验**：检查压缩包结构（`tbrain/` 根、必需文件、`_internal/`）；
4. **安装**：解压并复制到 `versions\<新版本>`（同卷先 staging 再 rename，半拷贝的目录永远不会被选中）；
5. **停旧进程**：见第 3 节；
6. **切版本**：原子写 `current.txt` + 更新 junction；
7. **跑 doctor**：把检查结果附在升级结果里（doctor 失败不会撤销已完成的升级）；
8. **保留版本**：`prune_versions(keep=2)` 保留当前与最近一个版本，删除更旧的。

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
- **发布资产要求？** `update` 只接受带 SHA256 摘要的 GitHub Release 资产（资产元数据 `digest` 字段，或发布时附带 `.sha256` 校验文件供人工核对）；没有摘要的包会被拒绝下载。
- **安装包里的版本号从哪来？** 构建时 `scripts/build.ps1` 把 `src/tbrain/__init__.py` 的版本写入包内 `VERSION` 文件（PyInstaller 目录里没有可读的 `tbrain/__init__.py`），`tbrain install/upgrade` 从该文件推断版本；源头仍是 `__version__`。
- **升级包如何生成？** `pwsh -File scripts/build.ps1`，产物 `dist/tbrain` 直接匹配 `installer.REQUIRED_FILES`；版本号只从 `src/tbrain/__init__.py` 读取。
