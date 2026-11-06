# 本地闭环 | Local loop

人写下目标和完成标准。Agent 在这个项目的一处工作区里自己观察、修改、验证，再把证据和决定理由存下来。做完可以离开。只有地基级别的改动才打断 Kai。

连续性仍是原来的 bootstrap / save / resume。这里不替换成更激进的上下文压缩，也不改 Dream：核心路径不调用模型。

## 项目与工作区 | Project and worktree

`tbrain project add`（桌面里保存项目走同一条路径）会：

1. 把第一个代码目录变成 git 仓库。已经是仓库则不重新初始化。
2. 使用固定约定：`../.tbrain-worktrees/<仓库目录名>/<项目名>`，分支 `tbrain/<项目名>`。能创建 git worktree 时状态是 `worktree`；仓库还没有提交时只预留目录，状态是 `skeleton`。
3. 写入完成标准模板 `项目/<项目名>/完成标准.md`。字段固定为 goal（目标）、done（完成标准）、evidence（证据）、verify（验证）。已有内容不会被覆盖。
4. 在代码目录写入 `.tbrain/check.yaml` 和 `.tbrain/WORKTREE.md`。新建的空仓库会把这两份文件放进初始提交，工作区里也能看到。

进程和端口级沙箱这轮不做。隔离方式就是：一个项目，一处工作区。

## 工作约定 | Work contract

bootstrap 启动包、resume 交接包、`bridge show` 的 `contract` 字段都带同一段白话约定：

- 只改这个项目的这一处工作区。
- 对着目标和完成标准做。
- 自己用 `tbrain check` 验证，并留下真实证据。
- 只有数据库迁移、登录和权限、对外协议、安装和升级路径才打断 Kai。命名、风格、小重构不要问。
- 不要用“要不要我继续”停下来。
- 决定要写为什么：理由、取舍、决定人。

## 决定理由 | Decision reasons

`save` 仍接受第一行 `决策 | 理由`。另外三行会写进同一条 K#，resume 原样带回：

```text
SQLite 只做索引 | Markdown 是唯一事实源
理由：索引可以删掉重建
取舍：多维护一个索引文件
决定人：Kai
```

也可以用字段提交：`decision`、`reason`、`rationale`、`tradeoffs`、`decided_by`。

## 收尾 | Wrap-up

宿主事件 `WrapUp` 会检查下面四项，缺任何一项就 `decision=block`，原因写在 `reason` 里。会话状态已经是「已完成」时，原来的 `Stop` 也会做同样的检查。进行中的 `Stop` 仍只提醒尚未 save 的内容。

- 至少一条证据；已完成项要引用证据编号。
- 验证结果里有一次退出码为 0 的 `tbrain check`。
- 每条仍有效的决定都写了理由、取舍、决定人。
- 记录过的动作没有超出已允许的权限级别。

## 权限 | Permission tiers

| 级别 | 可以直接做的事 |
|---|---|
| auto | 读这个工作区里的代码、日志和测试 |
| conditional | 推送分支、开拉取请求、开预览 |
| auth-required | 生产环境、删除数据、碰密钥 |

默认只有 auto。更高的级别要在 `save` 里写入，例如 `{"op":"permission","grant":"conditional"}`。动作用 `{"op":"permission","action":"push branch"}` 记下，收尾时按动作需要的级别核对。没有单独的进程沙箱。

## 本地检查 | Local check

```powershell
tbrain check --path D:\work\app
```

配置可以是 `.tbrain/check.yaml` 或 `tbrain.check.yaml`：

```yaml
version: 1
checks:
  - name: unit
    run: python -m pytest -q
lints:
  - decision-missing-why
```

`checks` 是你自己的命令。`lints` 不写时会跑默认的 `decision-missing-why`。未知的 lint id 算失败，不会被悄悄跳过。退出码 0 表示全部通过。

## 错了就加一条 lint | Mistake, then lint, then the local gate

同一类错误再出现时，加一条 lint，并把它写进项目的 `lints`，让 `tbrain check` 下次失败。

1. 在 `src/tbrain/check.py` 写函数，扫描项目目录，返回 `[{path, line, message}]`。
2. 调用 `register_lint("你的-id", 函数)`。
3. 把这个 id 放进 `.tbrain/check.yaml` 的 `lints`。
4. `tbrain check` 在这类错误还在时失败。

示例就是 `decision-missing-why`。`### K1` 或带 `<!-- tbrain:decision -->` 的笔记如果没有理由、取舍、决定人，本地检查失败。补全这三行之后，同一条 lint 通过。GitHub 上的 CI 留给以后的远程拉取请求，不代替这个本地门禁。
