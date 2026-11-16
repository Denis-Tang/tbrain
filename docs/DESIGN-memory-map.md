# 1D：条件写入与事实地图

基座：R3源码af5fa97，478项全量通过，隔离Windows CLI通过；GUI未验证。
本轮只做规划12的1D。用户U8决定：决策日志text与why合计最多200字，二者必填。

## 采用的材料与边界

| 原文 | 采用 | 不采用 |
|---|---|---|
| Vault `Lib/记忆机制/___Dreaming__.md` §6，133–153行 | 写前核对读取版本、保留来源 | 把演讲整理稿当API规范 |
| Vault `memory-architecture-design-zh.md` §5，742–890行 | 比较旧/新版与来源 | staging与多套评审状态文件；文中claim_id实际存run_id |
| [Anthropic memory update](https://platform.claude.com/docs/en/api/beta/memory_stores/memories/update)，184–218行 | 内容条件写入 | 原样照搬协议；其hash是原始UTF-8字节、条件可省略、失败409 |
| [lattice ADR-0004](https://github.com/alexnodeland/lattice/blob/0eec80cdf7cd2e0f215cd4d435a5a1d17a6d9152/docs/artifactr/adr/0004-optimistic-concurrency-and-revisions.md)，13–16行 | base与冲突head | changes_since、修订库、rebase、CRDT；第3行已说明后续收窄 |
| Vault `Lib/个人生产力的培养方法/Poteto相关/原始资料/Poteto的组织管理方法.md`，74–100行 | 地图帮助发现入口 | 无来源的功能含义与目的 |
| [knowledge-wiki](https://github.com/anthropics/claude-quickstarts/blob/main/managed-agents/knowledge-wiki/README.md)，7/39–40/93行及agent prompts | 来源、冲突、按需索引 | 整套多agent wiki；示例忙页5%丢行不是Tbrain冲突率 |
| [Karpathy wiki gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)，27–49/75行 | 区分原文与派生视图、轻目录、追加日志 | 自动归纳、wiki引擎、RAG、自动ingest/lint |

Tbrain沿用block_fingerprint的规范化算法，取8位；If-Match/412/428只是语义类比，不新增HTTP协议。
1000对固定种子的随机文本校准未出现8位前缀相等；这不是无碰撞证明。

## 写入暴露面

独立只读脚本窗口内4451个托管文件及app_home三份授权文件哈希不变。
389个替换标题展开为535个章节事件、59个项目×章节目标。
相邻替换间隔≤5分钟/30分钟/2小时分别43/215/339，窗口内全部事件对为44/292/882。
日志没有会话ID及读取基准，不能判定不同agent基于同一版本，所以只是暴露面，不能写成冲突数。
公共池8条取代记录去重为4次取代、4个目标，没有同一目标的两次取代，间隔分布无样本。
agent-changes的882条只有target/next_step/status/G#/K#，不含W3目标；rejections是校验拒绝，也不是冲突日志。
真正冲突率尚无证据。只实施章节/整卡条件写入与两版冲突记录，不自动合并、不引入修订系统。
新增w3-writes.jsonl仅记固定五键，供用户使用后再决定是否需要更复杂处理。

聚合数据：TEMP/tbrain-dev-work/stage1d-readonly-stats-20261112-235251.json。

## 公共池缺指纹根因调查

7种正式目录递归共301张，含归档4张；非归档297张。全部缺指纹293、缺owner292；非归档二者均缺289。
286张带程序固定迁移标记，全部缺owner/指纹；台账object_hashes也是286项，数量相符。
台账实际在app_home/migration-ledger.json，规划所写Vault/.tbrain路径不存在。
台账hash针对旧路径、正文和元数据，不能拿来和现卡字节直接比较；未读旧对象，逐对象对应尚未证明。
其余6张缺owner卡无迁移标记，来源仍未证实。292张的created_at都早于e3ec157，
但迁移保留旧created_at，时间本身不能证明写入版本。
单对象合成迁移对照已完成：v0.7.3写owner与有效指纹，e3ec157父提交3c758d1（源码0.6.2）不写两字段；
两者均带相同固定迁移标记，旧输入均未变。历史writer可以直接产生这类缺失，当前迁移器没有复现。
286张现卡符合历史迁移形态，但未逐对象追溯；另外6张来源仍未证实。不据此自动回填。
293与292差的1张为安装类归档卡，owner为字符串，指纹键缺失，history为列表。
源码save_public_card先pop旧指纹再写归档，能够直接产生此形态；不回填归档卡。
1D只调查，不回填任何真实卡；回填属于1E，仍需预览、用户批准和用户指定备份目录。

## 地图形态与校准

只从git/项目卡读取：main对比、独有提交、最后提交、本机reflog创建记录、worktree、任务/标准链接。
缺失reflog留空；时间不表示失效；不推断创建者、分支目的、功能作用。
读取复用worktree.py的_git/_toplevel/_has_head/_branch_exists与registered读取方式，不调用prepare/ensure。
地图用事实表格与简短汇总flowchart；图的连线表达事实分类，不把分类当分支血缘。

| 合成分支数 | 独有提交分支 | 原UTF-8字节 | 12KiB后字节 | 展示/折叠最旧 |
|---:|---:|---:|---:|---:|
| 30 | 24 | 4141 | 4141 | 24/0 |
| 100 | 80 | 10477 | 10477 | 80/0 |
| 300 | 240 | 28559 | 12208 | 95/145 |

每库3个worktree、1条缺失reflog，git branch/worktree/status前后哈希不变。
仅当前混合中文名字与原型分布，不证明任意命名下都能展示同样条数；最终按字节限制折叠。
30分支三种Mermaid已用本地现有运行库和全新TEMP浏览器profile渲染，root已目检：
gitGraph适宽后高2419像素、纵向冗长；timeline适宽高110像素，文字难辨；汇总flowchart清楚。
据此采用12KiB上限及表格+汇总图。校准文件在TEMP/tbrain-map-calibration-c33d618d。
Obsidian实际渲染与真实宿主新会话定位仍由用户验证，不以本地渲染代替。

## 实施边界

W1沿用；W2只加只追加决策日志底层，不加agent操作；W3在现有方法与工具接线；W4/Dream不做。
地图与audit共享必要的git事实读取，不引入可插拔框架。audit只列清单，不清理工作树/分支。
v0.7.3哨兵探测339/339通过（229.213秒）；真实仓库branch/worktree/status哈希不变，
已检查范围内哨兵外新增.tbrain-worktrees目录0。本轮未复现污染，不改旧测试或prepare路径。
生产renderer在30/100/300分支为5149/12184/12187字节，展示24/72/72条独有提交分支，
折叠最旧0/8/168，三库git输出不变，相关测试4/4通过。
真实仓库只读验收：38分支、40worktree，地图8633字节，两次生成相同，git输出哈希不变。
真实36项目卡独立路径匹配到1个worktree，仅是本次路径对应数，不是创建者证明；4473托管文件窗口哈希不变。
验证目录TEMP/tbrain-r4-probes-04c9e658，地图写在该目录合成Vault，不写真实Vault。
