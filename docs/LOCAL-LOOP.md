# 可选任务模式 | Local loop

在设置页开启任务模式后，下述流程适用于新建会话。默认关闭；旧会话和未记录模式的历史会话保持普通记忆模式。开关立即保存，不改变已创建会话的模式，也不批量初始化项目。

用户提出任务。Agent 先复述完整任务理解并等待用户确认，再在这个项目的一处工作区里观察、修改、验证，把证据和决定理由存下来。做完可以离开。地基级别改动及无法查清的关键业务缺口需要 用户 决定。

连续性仍是原来的 bootstrap / save / resume。0.7.2基于0.7.1，不包含Dream；核心路径不调用模型。

## 项目与工作区 | Project and worktree

任务模式开启时，`tbrain project add`（桌面里保存项目走同一条路径）会：

桌面保存通过后台任务执行；页面轮询结果，保存期间其他页面仍可响应，不受单次桥请求的 60 秒等待限制。宿主侧栏同步失败会作为单独结果显示，不撤销已经保存的项目。

1. 把第一个代码目录变成 git 仓库。已经是仓库则不重新初始化。
2. 使用固定约定：`../.tbrain-worktrees/<仓库目录名>/<项目名>`，分支 `tbrain/<项目名>`。能创建 git worktree 时状态是 `worktree`；仓库还没有提交时只预留目录，状态是 `skeleton`。
3. 写入完成标准模板 `项目/<项目名>/完成标准.md`。字段固定为 goal（目标）、done（完成标准）、evidence（证据）、verify（验证）。已有内容不会被覆盖。
4. 在代码目录写入 `.tbrain/check.yaml` 和 `.tbrain/WORKTREE.md`。新建的空仓库会把这两份文件放进初始提交，工作区里也能看到。

进程和端口级沙箱这轮不做。隔离方式就是：一个项目，一处工作区。

已有未提交文件时保留原目录，不自动提交或复制用户代码。启动包优先使用当前会话所属的项目目录，不把已经打开的工作副本切到旧工作树。工作树仍可作为单独的执行目录使用。

## 工作约定 | Work contract

bootstrap 启动包、resume 交接包、`bridge show` 的 `contract` 字段都带同一段白话约定：

- 只改这个项目的这一处工作区。
- 首条用户消息后先确认目标、范围、关键业务规则和验收标准；用户纠正整体理解时才重新确认。
- 先查已有规则。执行中发现新的关键业务缺口时集中补问，继续不依赖该决定的部分。
- 对着目标和完成标准做。
- 自己用 `tbrain check` 验证，并留下真实证据。
- 数据库迁移、登录和权限、对外协议、安装和升级路径需要 用户 决定；关键业务缺口按上述方式集中补问。命名、风格、小重构不要问。
- 不要用“要不要我继续”停下来。
- 决定要写为什么：理由、取舍、决定人。

## 决定理由 | Decision reasons

`save` 仍接受第一行 `决策 | 理由`。另外三行会写进同一条 K#，resume 原样带回：

```text
SQLite 只做索引 | Markdown 是唯一事实源
理由：索引可以删掉重建
取舍：多维护一个索引文件
决定人：用户
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

真机做过之后，把 `windows_verified` 设为 true，在 `windows_evidence` 里写明在哪台 Windows 上安装并运行了哪个文件，并清空已经解决的 `windows_gap`，结论才可以是 `PASS`。例如：「在 Windows 11 机器上安装 dist\\tbrain-setup.exe，并运行 tbrain.exe，窗口能打开。」记录标注为“报告者声明”，需要对照证据核对；Tbrain 不自动认证安装结果，也不根据关键词判断证据真假。

## 首条消息先确认任务理解 | Confirm the task once

新会话收到首条用户消息后，Agent 必须先复述完整任务理解，再等待用户明确确认。SessionStart 不提前发问。项目记忆或完成标准已经写清，也不能跳过本次确认；不同会话分别确认，不把一个会话的确认写进项目完成标准。会话总结只保存具体复述与待确认内容，通用流程提示由 Hook 提供，避免重复进入会话检索。

任务理解包含目标、范围、业务处理口径、信息保留要求、交付物和完成标准；不能只确认笼统目标就当业务规则齐全。确认的不是具体技术方案。统一使用下面四项；短句、主动句，一句只说一件事，同一概念使用同一名称。只写用户明确的边界，未知内容写“待确认”，不能把猜测当成事实。

```text
任务目标：用户希望达到什么结果。
本次范围与业务规则：这次包含哪些内容、处理口径和信息保留要求。
交付结果：最后交给用户什么。
完成标准：用户怎样判断任务做完。

请在确认弹窗中提交“确认”；没有弹窗时，请单独回复：确认。确认后我再开始执行。
```

写法借鉴 [Karpathy 的建议](https://x.com/karpathy/status/2105819303471976479)及 [ASD-STE100 官方说明](https://www.asd-ste100.org/about_STE.html)中的简明、统一用词原则。上述四项是本项目采用的中文模板。

Agent 先用 `save` 保存同一份复述，再回复用户等待确认：

```json
{"op":"understanding","goal":"把错误提示改成中文","scope":"解析器错误提示，保留原有错误含义","deliverables":"修改后的代码","done":"原有功能不变，错误提示使用中文"}
```

四个字段各为 1～160 字。保存并展示复述后，支持原生确认弹窗的 Agent 必须优先弹窗。只有用户主动提交“确认”选项才开工；默认选中、取消、关闭、未提交或自由文本补充都不算确认。Pi 交互模式的扩展会在保存复述成功后自动展示原生选择框，选择“确认”才记录下面的操作：

```json
{"op":"confirm","question":"Q1","dialog_answer":"确认","why":"用户主动提交弹窗确认","tradeoffs":"仅执行弹窗展示的任务理解","decided_by":"用户"}
```

`dialog_answer` 只记录原生弹窗实际提交的确认选项，不由 Agent 猜测。确认来源会记为“弹窗”，不伪造用户消息 U#。其他宿主由 Agent 调用其原生提问工具，并按同一规则记录结果。

没有确认弹窗时，Agent 必须结束本轮回复，等待后续用户单独回复“确认”，再用 `confirm` 引用该 U#。去除首尾空白后必须完全等于“确认”；“对”“可以”“开始吧”“确认你的任务理解”“确认。”以及长消息中的确认字样均不接受。不做近义句或关键词意图识别。

```json
{"op":"confirm","question":"Q1","source":"U2","why":"用户确认任务目标理解","tradeoffs":"先不扩大范围","decided_by":"用户"}
```

用户补充或更正信息时，Agent 纳入信息，更新并展示 `understanding`，再按上述方式等待确认。同一消息含更正及“确认”也不能直接开工。确认前不要重存未变化的复述；每次保存复述后都要重新等待确认。来源过早或原话不符时，错误会提示下一步，不要重复提交同一失败的确认。确认后，后续用户消息、压缩和同会话恢复都不重复确认整个任务，项目模板缺项也不触发新的目标反问。只有用户指出整体理解有误时，Agent 才重新复述整个任务；此时 `understanding` 的 `source` 引用该用户原话。

未确认时，Agent 不进入后续执行，可以保存复述和已有记录。Tbrain 会拒绝保存已完成项、更新项目提示词、标为已完成和提交验收。Tbrain 能约束这些保存及 Hook，不能阻止外部宿主直接改文件。确认本身不提升权限。

### 新业务缺口集中补问

先查用户指令、项目文件和已有记录，已明确的规则直接沿用。查完仍有会改变业务结果、必须由用户决定的缺口时，集中询问问题及影响范围，不重新启动整项任务确认。处理口径、信息保留、含糊规则冲突、任务范围、验收与取舍都可能属于这种缺口。已明确的新要求直接执行，技术问题自行查清。

复用 `save` 的未完成目标 G# 记录缺口，不增加工具或审批步骤：

```json
{"op":"append","section":"goal","items":[{"text":"业务缺口：同义条目以什么条件合并？影响：合并逻辑。"},{"text":"业务缺口：原文与来源须保留哪些信息？影响：内容导出。"}]}
```

不依赖答案的工作继续推进；依赖答案的部分暂不执行，不能猜定口径。未答问题保留，未完成目标会阻止会话直接标为已完成。bootstrap 和 resume 带回尚未完成的目标。用户明确回答后，保存决定及用户原话来源，再关闭对应 G#；不额外要求用户确认自己的回答：

```json
{"op":"set_status","status":"进行中","complete_goals":["G1","G2"]}
```

已知关键问题尽量集中在首轮问清；执行中出现新的关键缺口，只补问新问题，不反复询问已答内容。未知数据事实按已有口径留空或进入核实清单，不逐条打断开发会话。产品内的业务核实流程不受开发 Agent 首轮确认次数限制。

## 没有实质进展就暂停 | Pause when attempts add no evidence

一次尝试记在 save 的 `attempt`、走不通的路，或一次非 0 的验证里。默认连续 3 次没有实质进展就暂停。换一条失败、或把失败换个说法，只要没有新证据，都继续累加，不能靠 A→B→A 避开暂停。这个次数写在 `.tbrain/check.yaml`：

```yaml
stall:
  unproductive_attempts: 3
```

这里只数尝试。不看 token、费用或时长。措辞变化不是进展。一条和已有失败不同的新证据可以重新计数，即使测试仍未通过。退出码 0 也算实质进展。

暂停后会话状态是已暂停，原因写在会话总结里。之后的 save 会拒绝继续改实现。Stop 不再要求模型再跑一轮。收尾不能通过。Tbrain 只守住保存、启动包和 hook；它不拥有外部 Agent 进程，也不能把那个进程杀掉。

要继续必须显式换路，并留下为什么。这不是把计数悄悄归零：

```json
{"op":"continue","route":"改为先补失败用例","why":"同一失败已重复","tradeoffs":"先不重写解析器","decided_by":"用户"}
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
