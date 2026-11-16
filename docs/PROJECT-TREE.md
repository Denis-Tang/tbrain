# Tbrain 项目树

已脱敏的本机 Git 快照：2026-11-16 10:30:48（Asia/Shanghai）。通过现有 map 功能生成；后续改动不会自动更新这份快照。

仓库：<REPO_ROOT>
基准：main；分支总数：20；worktree 总数：7
[项目说明](../README.md) · [地图用法](UPGRADE.md)

创建记录来自本机 reflog，可能缺失；缺失 1 条，时间与来源留空，不推断。
ahead/behind 相对 main；ahead > 0 只表示有独有提交，不表示分支目的或完成程度。
功能与决定原因以提交说明原文为准；没有记录的原因不推断。
项目树按分支分类，连线不表示分支祖先；提交关系见下方 Git 原生关系图。

## 项目树
```text
Tbrain
├─ codex/hindsight-memory @ a68c6a61
│  提交时间：2026-11-15T20:05:23+08:00；ahead 4 / behind 2
│  本机创建：2026-11-13T18:03:06+08:00；创建来源：HEAD
│  提交名称：wip: park Hindsight 1.2.1 memory experiment (0.7.4-beta.1+hindsight)
│  说明原文：User trial only; direction is local Markdown first. Not to be merged. 341 tests passed.
├─ wip/0.8.0-workbench @ 929f056b
│  提交时间：2026-11-15T20:05:16+08:00；ahead 29 / behind 2
│  本机创建：2026-11-15T20:05:16+08:00；创建来源：HEAD
│  提交名称：wip: park uncommitted 0.8.0 local candidate (workbench, attachments, dream, roles, maintenance, map, check editor)
│  说明原文：Parked as-is from the main working copy on 2026-11-15 so the baseline can return
│  to 0.7.8 (e080f0a). Not reviewed or merged; 526 tests passed on this tree before parking.
├─ codex/install-upgrade @ e080f0a4
│  提交时间：2026-11-13T09:01:54+08:00；ahead 28 / behind 2
│  本机创建：2026-11-13T09:34:06+08:00；创建来源：codex/0.7.8-public-backfill
│  提交名称：Record user-reported desktop GUI validation
│  说明原文：未记录
├─ codex/0.7.9-memory @ e080f0a4
│  提交时间：2026-11-13T09:01:54+08:00；ahead 28 / behind 2
│  本机创建：2026-11-15T20:05:23+08:00；创建来源：e080f0a
│  提交名称：Record user-reported desktop GUI validation
│  说明原文：未记录
├─ codex/0.7.8-public-backfill @ e080f0a4
│  提交时间：2026-11-13T09:01:54+08:00；ahead 28 / behind 2
│  本机创建：2026-11-13T01:00:09+08:00；创建来源：HEAD
│  提交名称：Record user-reported desktop GUI validation
│  说明原文：未记录
├─ codex/0.7.7-memory-writes-map @ 1e51b10d
│  提交时间：2026-11-13T00:49:49+08:00；ahead 22 / behind 2
│  本机创建：2026-11-12T23:56:47+08:00；创建来源：HEAD
│  提交名称：feat: add conditional memory writes and Git fact maps
│  说明原文：Require agent bases for existing sections and public cards while retaining user edit behavior. Preserve both conflict versions and isolate public drafts. Add bounded maps and readonly worktree audit. User U8 limits decision text plus reason to 200 characters. Full regression: 493 passed; no real Vault backfill or GUI verification.
├─ codex/0.7.6-startup-and-export @ d1bdb8b6
│  提交时间：2026-11-12T23:56:46+08:00；ahead 20 / behind 2
│  本机创建：2026-11-12T22:15:36+08:00；创建来源：HEAD
│  提交名称：docs: record R3 full regression and isolated Windows package checks
│  说明原文：未记录
├─ codex/0.7.5-memory-isolation @ d7c0a2c6
│  提交时间：2026-11-12T21:56:45+08:00；ahead 14 / behind 2
│  本机创建：2026-11-12T21:01:23+08:00；创建来源：HEAD
│  提交名称：test: retrieve basic records from a separate reading session
│  说明原文：未记录
├─ codex/0.7.4-memory-loop @ c1e13647
│  提交时间：2026-11-12T20:50:21+08:00；ahead 11 / behind 2
│  本机创建：2026-11-12T19:51:21+08:00；创建来源：v0.7.3
│  提交名称：fix: preserve project metadata and serialize memory writes
│  说明原文：未记录
├─ codex/explicit-task-confirmation @ 20448939
│  提交时间：2026-11-08T11:07:23+08:00；ahead 3 / behind 2
│  本机创建：2026-11-08T10:40:08+08:00；创建来源：codex/optional-task-mode
│  提交名称：fix: require explicit task confirmation and release 0.7.3
│  说明原文：Accept only a separate confirmation reply or a submitted native dialog choice. Add the Pi dialog and clarify extension refresh instructions. Remove task assent guessing and complete the decision template. Validated with 339 tests, four local lints, staged/package privacy scans, and an isolated Windows ZIP install with desktop startup.
├─ codex/optional-task-mode @ e9563b93
│  提交时间：2026-11-07T17:41:54+08:00；ahead 2 / behind 2
│  本机创建：2026-11-07T16:53:36+08:00；创建来源：v0.7.1
│  提交名称：release: publish 0.7.2 with optional task mode
│  说明原文：未记录
├─ codex/fix-project-save @ 2e5f8aef
│  提交时间：2026-11-07T15:11:00+08:00；ahead 6 / behind 0
│  本机创建：2026-11-07T14:47:35+08:00；创建来源：origin/cursor/goal-pause-handoff-42ee
│  提交名称：fix: keep project saves responsive and repair workflow regressions
│  说明原文：Run project saves in the background and poll their results so slow Git or host workspace operations do not block the desktop request loop. Stop recursively scanning project files just to determine whether the directory is populated.
│
│  Keep contracts on the active project directory, run inherited checks from their own configuration directory, preserve no-progress pauses across trailing completion notes, and recognize key-value test counts. Require continuation assent to refer to the proposed route.
│
│  Remove the always-failing Windows verifier and keyword-based certification. Record installation results explicitly as reporter declarations with evidence and remaining gaps. Fix Windows-specific regression tests and document the workflow.
│
│  Validation: 340 pytest cases passed on Windows; JavaScript syntax and git diff checks passed. An isolated native window stayed responsive during a simulated 108.3-second save and displayed the created project afterward.
├─ main @ 3952c802
│  提交时间：2026-11-06T04:44:06Z；ahead 0 / behind 0
│  本机创建：未记录；创建来源：未记录
│  提交名称：release: prepare 0.8.0-beta.1 experience build
│  说明原文：Point the single version source at 0.8.0-beta.1 so Dream L0/L1 can ship as a prerelease. Stable 0.7.1 stays the GitHub latest release.
├─ codex/attachments-pi-fresh2 @ a5214879
│  提交时间：2026-10-30T04:08:49+08:00；ahead 6 / behind 9
│  本机创建：2026-10-30T03:22:44+08:00；创建来源：HEAD
│  提交名称：docs: record completed experiment cleanup
│  说明原文：未记录
├─ codex/attachments-dsh-fresh1 @ 1d8fcea8
│  提交时间：2026-10-30T03:59:50+08:00；ahead 3 / behind 9
│  本机创建：2026-10-30T03:20:19+08:00；创建来源：HEAD
│  提交名称：wip: preserve bounded attachment UI experiment
│  说明原文：未记录
├─ codex/attachments-dsh @ 8386b7cc
│  提交时间：2026-10-30T03:51:20+08:00；ahead 4 / behind 9
│  本机创建：2026-10-30T02:02:42+08:00；创建来源：HEAD
│  提交名称：fix: integrate attachment body editing and project isolation
│  说明原文：未记录
├─ codex/attachments-dsh-fresh2 @ a9493eed
│  提交时间：2026-10-30T03:38:22+08:00；ahead 3 / behind 9
│  本机创建：2026-10-30T03:40:43+08:00；创建来源：HEAD
│  提交名称：feat: add scoped attachment search and startup cards
│  说明原文：未记录
├─ codex/memory-compare @ 74b73981
│  提交时间：2026-10-30T03:35:34+08:00；ahead 4 / behind 9
│  本机创建：2026-10-30T19:44:07+08:00；创建来源：HEAD
│  提交名称：fix: retain canonical attachment colors in metadata cards
│  说明原文：未记录
├─ codex/attachments-pi-fresh1 @ 5d922c54
│  提交时间：2026-10-30T03:29:11+08:00；ahead 2 / behind 9
│  本机创建：2026-10-30T02:29:21+08:00；创建来源：HEAD
│  提交名称：feat: preserve fresh-session attachment UI treatment
│  说明原文：未记录
└─ codex/attachments-pi @ 65f7248a
   提交时间：2026-10-30T03:29:11+08:00；ahead 4 / behind 9
   本机创建：2026-10-30T02:01:18+08:00；创建来源：HEAD
   提交名称：docs: complete attachment integration notes
   说明原文：未记录
```

无独有提交的 tbrain/*：折叠 0 条。
按最后提交时间折叠最旧分支：0 条；时间不表示失效。

## 已登记工作树
| 路径 | 分支或 detached | HEAD | 提交时间 | 未提交项 |
|---|---|---|---|---:|
| <USER_HOME>/.codex/worktrees/hindsight-memory/Tbrain | codex/hindsight-memory | a68c6a61 | 2026-11-15T20:05:23+08:00 | 0 |
| <REPO_ROOT> | codex/0.7.9-memory | e080f0a4 | 2026-11-13T09:01:54+08:00 | 7 |
| <USER_HOME>/.codex/worktrees/b7b1/Tbrain | codex/0.7.8-public-backfill | e080f0a4 | 2026-11-13T09:01:54+08:00 | 0 |
| <USER_HOME>/.codex/worktrees/f603/Tbrain | codex/explicit-task-confirmation | 20448939 | 2026-11-08T11:07:23+08:00 | 0 |
| <REPO_PARENT>/Tbrain-optional-task-mode | codex/optional-task-mode | e9563b93 | 2026-11-07T17:41:54+08:00 | 0 |
| <REPO_PARENT>/Tbrain-fix-project-save | codex/fix-project-save | 2e5f8aef | 2026-11-07T15:11:00+08:00 | 15 |
| <USER_HOME>/.codex/worktrees/memory-compare/Tbrain | codex/memory-compare | 74b73981 | 2026-10-30T03:35:34+08:00 | 4 |

按 HEAD 提交时间折叠最旧工作树：0 条。

## 提交关系
Git 原生关系图，最多展示最近 30 次提交；下图行数：22。
```text
* a68c6a6 2026-11-15T20:05:23+08:00 wip: park Hindsight 1.2.1 memory experiment (0.7.4-beta.1+hindsight) (codex/hindsight-memory)
| * 929f056 2026-11-15T20:05:16+08:00 wip: park uncommitted 0.8.0 local candidate (workbench, attachments, dream, roles, maintenance, map, check editor) (wip/0.8.0-workbench)
| * e080f0a 2026-11-13T09:01:54+08:00 Record user-reported desktop GUI validation (HEAD -> codex/0.7.9-memory, codex/install-upgrade, codex/0.7.8-public-backfill)
| * 7d64b81 2026-11-13T08:24:07+08:00 Apply user-approved M1 acceptance and defer real backfills
| * 6c7f54c 2026-11-13T01:47:07+08:00 Document final 0.7.8 validation and remaining stage-one decisions
| * 2b70e13 2026-11-13T01:42:59+08:00 Keep resolved backfill paths within public cards and record stage-one metrics
| * 2daf9e9 2026-11-13T01:28:21+08:00 Prepare 0.7.8 explicit public-card backfill and preserve lock bytes
| * d5ef679 2026-11-13T01:00:10+08:00 docs: freeze public backfill boundaries with real writes pending
| * 1e51b10 2026-11-13T00:49:49+08:00 feat: add conditional memory writes and Git fact maps (codex/0.7.7-memory-writes-map)
| * 88d4d9e 2026-11-13T00:05:31+08:00 docs: freeze minimal conditional writes and measured memory map v0
| * d1bdb8b 2026-11-12T23:56:46+08:00 docs: record R3 full regression and isolated Windows package checks (codex/0.7.6-startup-and-export)
| * af5fa97 2026-11-12T23:43:57+08:00 fix: budget complete startup packages and use the reviewed Chinese contract
| * 87f4af3 2026-11-12T23:19:02+08:00 dev: add aggregate readonly vault sizing script
| * 71d8681 2026-11-12T23:01:50+08:00 docs: freeze Chinese startup contract and reviewed initial budgets
| * 2859e19 2026-11-12T22:51:26+08:00 fix: preserve renamed rule sources and add readonly project export
| * 17c4c5c 2026-11-12T22:15:37+08:00 FREEZE-1C: pin rule lifecycle and read-only export semantics
| * d7c0a2c 2026-11-12T21:56:45+08:00 test: retrieve basic records from a separate reading session (codex/0.7.5-memory-isolation)
| * 9e474c9 2026-11-12T21:38:15+08:00 feat: isolate project reads and assign stable project identity
| * 83d6df2 2026-11-12T21:01:24+08:00 FREEZE-1B: pin inbound sharing and stable project identity
| * c1e1364 2026-11-12T20:50:21+08:00 fix: preserve project metadata and serialize memory writes (codex/0.7.4-memory-loop)
| * bf2fc11 2026-11-12T20:32:10+08:00 FREEZE-1A: pin write integrity cases and measured lock limits
| * e935673 2026-11-12T20:32:09+08:00 chore: measure stage zero memory continuity baseline
```
