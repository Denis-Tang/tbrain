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

宿主事件 `WrapUp` 会检查下面各项，缺任何一项就 `decision=block`，原因写在 `reason` 里。会话状态已经是「已完成」时，原来的 `Stop` 也会做同样的检查。进行中的 `Stop` 仍只提醒尚未 save 的内容。

- 至少一条证据；已完成项要引用证据编号。
- 验证结果里有一次退出码为 0 的 `tbrain check`。
- 每条仍有效的决定都写了理由、取舍、决定人。
- 记录过的动作没有超出已允许的权限级别。
- 会话总结里有一份 A–E 最终验收。结论只能是 `PASS`、`CONDITIONAL PASS` 或 `FAIL`。

验收写在该会话总结的 `## 最终验收`，resume 会带上这一节。

写法保持白话，该有的细节不能省。每句都要能核对：写上命令、路径、退出码，以及验过什么、没验什么。不要堆术语，也不要写「应该可以」「大体没问题」。

```json
{"op":"acceptance","task":"本地检查已接上，这一轮可以提交。","verification":"在 D:\\work\\app 运行 tbrain check，退出码 0。证据 E1 是 commit 4c8fa05。没有在 Windows 上安装。","risks":"没有还要人接受的风险。","deliverables":"commit 4c8fa05。在 D:\\work\\app 运行 tbrain check。","conclusion":"PASS"}
```

| 字段 | 写什么 |
|---|---|
| A 任务状态 | 一两句说清做到哪了。例：本地检查已接上，这一轮可以提交。 |
| B 验证与证据 | 命令、目录、退出码、证据编号。没做的也写上。例：在 D:\work\app 运行 tbrain check，退出码 0。证据 E1。没有在 Windows 上安装。 |
| C 风险 | 还没消掉的具体事情。没有就写「没有还要人接受的风险」。 |
| D 交付物 | commit 号、产物路径、怎么跑。例：commit 4c8fa05。在 D:\work\app 运行 tbrain check。 |
| E 结论 | 只能是 PASS、CONDITIONAL PASS、FAIL |

`CONDITIONAL PASS` 会在同一节写下「人工剩余风险接受：仍未完成」。这句话还在，就不能当成已经通过。

Tbrain 桌面或 Windows 安装包还要多记一项。证据里必须有一次真实 Windows 机器上的安装并运行，不能只拿 Linux 上的 pytest 充当。没做这次验证时，结论不能是 `PASS`：用 `CONDITIONAL PASS` 或 `FAIL`，并写上缺口。

```json
{"op":"acceptance","task":"安装包还不能交给 Windows 用户。","verification":"在 Linux 上运行 pytest -q，退出码 0。没有在 Windows 机器上安装或打开安装包。","risks":"安装和启动还没在 Windows 上看过。","deliverables":"dist\\tbrain-setup.exe。在 Windows 上安装并打开 tbrain.exe。","conclusion":"CONDITIONAL PASS","windows_installable":true,"windows_gap":"没有在 Windows 机器上安装并运行 dist\\tbrain-setup.exe"}
```

真机做过之后，把 `windows_verified` 设为 true，并在 `windows_evidence` 里写明在哪台 Windows 上安装并运行了哪个文件，结论才可以是 `PASS`。例如：「在 Windows 11 机器上安装 dist\\tbrain-setup.exe，并运行 tbrain.exe，窗口能打开。」

## 目标没写清就先问 | Ask before an unclear goal

项目的 `完成标准.md` 里，目标或完成标准还是「待填写」时，bootstrap 会给出具体问题和建议默认，并把未确认项写回这个文件。已经写清的目标不会每次任务都再问一遍。

可以只读代码和测试，把问题问清楚。仓库和测试能回答的技术问题自己定。没确认之前，不能把会话标成已完成，也不能把最终验收写成 PASS。收尾也会拦住。

确认用 save：

```json
{"op":"confirm","question":"Q1","accept_default":true,"why":"同意建议默认","tradeoffs":"先不扩大范围","decided_by":"Kai"}
```

理由、取舍、决定人都会留下。旧的完成标准如果目标段和完成标准段已经有正文、又没有未确认问题，就当作已经定过，不再生成新问题。

## 没有进展就暂停 | Pause when the same failure repeats

一次尝试记在 save 的 `attempt`、走不通的路，或一次非 0 的验证里。默认连续 3 次同一失败就暂停。这个次数写在 `.tbrain/check.yaml`：

```yaml
stall:
  unproductive_attempts: 3
```

这里只数尝试。不看 token、费用或时长。把同一失败换个说法，或原样再跑一次，不会把计数清零。一条和上次不同的新证据可以算进展，即使测试仍未通过。

暂停后会话状态是已暂停，原因写在会话总结里。之后的 save 会拒绝继续改实现。Stop 不再要求模型再跑一轮。收尾不能通过。Tbrain 只守住保存、启动包和 hook；它不拥有外部 Agent 进程，也不能把那个进程杀掉。

要继续必须显式换路，并留下为什么。这不是把计数悄悄归零：

```json
{"op":"continue","route":"改为先补失败用例","why":"同一失败已重复","tradeoffs":"先不重写解析器","decided_by":"Kai"}
```

新路线如果还是刚停下的那条失败，不会被接受。

## 暂停时写下热交接 | Hot handoff on pause

暂停当时就把 L0 热交接写进该会话总结的「热交接」。里面有目标、未确认问题、已确认决定和理由、工作区约定与分支、已完成、试过的路和失败证据、已核实和未核实的验证、停顿原因、下一步和恢复条件。工作区只写约定路径，不写本机用户目录。

bootstrap 和 resume 先读这段。暂停和未确认都会留着，不会改成已完成，也不会自动再跑一轮。不等 Dream，也不读 L1 才允许恢复。会话文件仍用原来的原子写；写入失败时磁盘上的原文保持不动，不能假装已经暂停。

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
