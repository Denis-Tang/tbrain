# 隔离开发入口

本轮开发基座为 `v0.7.3`（`2044893`）。在当前工作树开发，不使用 PATH 上的裸 `tbrain`。
使用 PowerShell 7，无 Profile。真实 Vault、宿主配置和已安装目录不接受开发写入。

```powershell
py -3.11 -m venv "$env:TEMP\tbrain-dev-venv"
& "$env:TEMP\tbrain-dev-venv\Scripts\python.exe" -m pip install -e '.[dev]'
& ./scripts/dev/tb.ps1 -Target dev -CommandArgs @('version')
```

`tb.ps1` 为每次子进程设置临时 home、Vault、安装根和宿主目录，先检查版本和 `app_home`。
`installed` 仅用于合成数据复现；`rc` 需要已有的 TEMP/tbrain-rc-* 安装根。
JSON 输入使用 `-InputJson`，由包装脚本按 UTF-8 字节写入 stdin，不放进 CLI 的 JSON 参数。
输出为 JSON：`exit_code`、`stdout`、`stderr`、`log_path`。日志在 TEMP/tbrain-dev-log。

```powershell
& "$env:TEMP\tbrain-dev-venv\Scripts\python.exe" -I tests/fixtures/make_vault.py `
  --out "$env:TEMP\tbrain-example-fixture" --profile base
& ./scripts/dev/tb.ps1 -Target dev `
  -AppHome "$env:TEMP\tbrain-example-fixture\home" `
  -Vault "$env:TEMP\tbrain-example-fixture\vault" -CommandArgs @('sessions')
```

夹具输出目录必须为空或不存在。`base` 覆盖项目隔离，`n1` 生成8个预算组合，`n3` 覆盖改名引用。
N1 的 F-B 按明确的输入定义添加5条800字指令，共12条消息；不沿用旧提示词的“共7条”。

测试也经包装脚本的 `check --config <临时YAML>` 执行。检查命令要显式设置工作树目录：
0.7.3 的 `check` 在配置文件所在目录运行命令，`--path` 只决定检查对象。
原有测试基线见 TEMP/tbrain-dev-log/baseline.xml；本轮新增版本用例不计入原有基线。
验收约定见 `docs/FREEZE-stage0.md`、`docs/FREEZE-baseline.md`。

阶段0结果：原有339项为338通过、1个跨进程写锁失败；版本修复后全量367项通过。
版本用例改前17失败/11通过，改后28项通过。安装版和源码的N1/N3结果一致：
N1复现，Codex任务模式F-A为0/4完整节；N3部分复现，改名后硬规则3条降为1条。
基线四节均有正文的11个项目中，Codex完整7个、pi完整11个；全空18个项目另列。
真实Vault在基线窗口4386文件哈希无变化。完整报告位于规划3/snapshots，未修改真实配置。

R1（0.7.4，c1e1364）：全量376项通过，C10改前5/5失败、改后20/20通过；
20轮压力覆盖2000轮项目/save并发、2000轮锁顺序，全部通过。
候选包：`%TEMP%/tbrain-r1-build-c1e1364-62f9449c/output`（不可变提交导出构建）。
隔离升级与回退：`%TEMP%/tbrain-r1-verify-20261112-123813-d86e9c52`，CLI验证通过。
用户U4最初选择GUI稍后验证；U10已报告“GUI已经验证”，G5桌面GUI待验项按用户报告关闭。

产品口径：共享开关仅允许主动搜索/手动续接非项目会话；公共池待确认提议在启动包中仅显示数量和路径。
真实安装和桌面 GUI 不由源码测试替代。真实 Vault 的ID、公共池指纹回填仍要在预览后单独批准。

R2（0.7.5）：阶段最终全量419项通过。
R3来源修复与只读导出提交为 `2859e19`；预算冻结提交为 `71d8681`。
R3预算源码0.7.6提交为 `af5fa97`；最终全量478项通过（454.63秒），30个合成包守住宿主预算且四节完整。
候选包从该提交构建：`%TEMP%/tbrain-r3-build-5df480c6/output/tbrain-0.7.6-windows-x64.zip`。
隔离验证目录：`%TEMP%/tbrain-r3-verify-f7659542`；16次CLI退出0，含R2升级、两宿主包、只读导出和回退。
导出窗口16个Vault文件哈希不变，测试淘汰进程0；该隔离脚本未运行桌面GUI，真实安装仍为0.7.3。
本轮合成项目导出经既有隔离 Pi 脚本读答3/3，仅作辅助证据；实际 Pi 扩展未由本轮同步或验证。

`vault_stats.py <Vault目录>` 直接读取 Markdown，输出四节、用户区、全局及待确认的字符数，
P50/P90采用 nearest rank（`ceil(p*n)-1`），空值与非空值分组；同时统计七种正式公共池卡分类，
归档、冲突等目录不计作正式卡。前后托管文件哈希只输出差异数，不输出正文、项目名、会话名或文件清单。
脚本不构造服务、Store或索引，不写输入Vault；不统计依赖索引的接续与热点事实，也不展开继承规则。
经 `tb.ps1` 的临时检查配置运行，例如检查命令先 `os.chdir` 到工作树，再执行：

```powershell
& "$env:TEMP\tbrain-dev-venv\Scripts\python.exe" -I scripts/dev/vault_stats.py <合成Vault目录>
```

真实36份项目提示词已完成一次只读统计，本轮不重复运行。
一个TEMP合成夹具的14项检查通过，11个输入文件的内容和mtime无变化。
统计解析复用Store的纯函数，覆盖 `owner: user` 空格标记，不实例化Store。

R4（0.7.7，1e51b10）：阶段全量493项通过，隔离0.7.6→0.7.7→回退的44条命令全部退出0，
其中包含Git命令；验证目录为 `%TEMP%/tbrain-r4-verify-2aef1b34`。该脚本未验证真实安装或桌面GUI。

`public_backfill.py` 是1E独立工具，不接入产品CLI。当前只授权开发、合成验证和真实只读预览，
真实备份/回填/回滚须用户另行批准并指定Vault外备份目录；本轮未执行真实写入。

```powershell
python -I scripts/dev/public_backfill.py preview --vault <Vault目录>
python -I scripts/dev/public_backfill.py apply --vault <Vault目录> --approved <批准JSON> --backup-dir <Vault外目录> --app-home <TEMP空目录>
python -I scripts/dev/public_backfill.py rollback --vault <Vault目录> --manifest <结果JSON> --app-home <TEMP空目录>
```

上述开发调用同样经 `tb.ps1 check --config <临时YAML>` 执行。预览默认写入TEMP/r4b-preview新目录，
`preview.json`含逐卡相对路径/hash/类别和元数据，`preview.md`便于阅读，均无正文。
θ默认未复核、mtime默认不可用，缺指纹归D；只有本批事实获复核后才显式传`--theta`、
`--mtime-usable`和`--theta-source`，不把候选320秒或集中比例当通用判据。
批准JSON格式为`{"selected":[{"path":"公共池/配置/卡.md","hash":"当前SHA256","category":"D","add_owner":false}],"approval":"原话备注","reason_report":"根因报告备注"}`，类别仅C或D。
selected默认空，不按类别自动选择D；备注不作身份或批准验证。add_owner仅补缺省owner，不覆盖已有值。
apply锁内完整备份公共池到新子目录并核对文件数/哈希，逐卡复核后只插入字段行，保留BOM/换行/原字节，
每张卡先把前后hash和插入行原子写入结果清单，再改卡；重复运行跳过有效指纹。
正常rollback只撤本次字段，后续改过的卡跳过。
灾难恢复仅由用户明确选择并先关闭宿主停写后人工执行整目录复制；这会丢失回填后的更新，工具不自动执行。

R4b（0.7.8，2b70e13）：回填工具定向6项通过，路径修复后最终全量500项通过（437.91秒）。
最终真实只读预览在 `%TEMP%/r4b-preview/final-script-20261113-012538-f7685f64`：
A8/B0/C0/D289，4482个托管文件内容和mtime不变，选择清单为空。真实回填未执行；GUI用户报告见下文。
最终包在 `%TEMP%/tbrain-r4b-final-build-9baf8db3/output`；隔离验收在 `%TEMP%/tbrain-r4b-final-verify-a4d1118a`，
44条命令退出0（29次Tbrain、15次Git），覆盖0.7.7→0.7.8→回退及锁标记保留。
阶段末原始M1为两宿主32/36，原值保留；用户U9确认保留来源标记并采用有效内容验收，
有效规则78/78、其余三节各36/36，M1产品验收通过。用户U9同时决定36卡ID本轮不回填，D289全部不动。
用户U10报告GUI已验证，G5据此关闭，未另提供包版本和操作明细；不扩展到所有版本或Obsidian验证。
真实回填不执行，原预览保留；M4仍289次受阻，历史数据维护缺口保留，阶段1整体未完成。
