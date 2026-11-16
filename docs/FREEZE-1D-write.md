# 1D验收约定

基座af5fa97/d1bdb8b，R3全量478通过；先冻结本文件，再实施R4。
设计与只读统计见DESIGN-memory-map.md；U8覆盖原规划解释：text+why合计≤200字。
真实Vault/安装/宿主配置不写；不push、不发布，不删除任何既有工作树或分支。

## 条件写入

- Store.save_project_prompt新增可选base_sha映射，章节是最小单位。默认用户编辑仍按旧行为。
- 只有映射中出现的章节比较前缀8位；相符正常写，不符不写该节，其余节照常写。
- 项目冲突进入本项目待用户确认，两版完整保留；rejected带section/reason/conflict/base/head(sha,text)。
- 受保护内容先按旧机制处理，再判base；不能用base_sha覆盖用户手改。
- agent project_prompt更新已有章节必须带base_sha，漏传返回缺少base_sha与当前head，内容不变。
  已存在的空标题也算已有章节；真正缺失的章节可新建。桌面/CLI用户编辑不受此入口要求影响。
- 启动包四节sha来自未过滤原文，每节15字符，共≤60；继承父规则不加本项目可写sha。
- 公共池agent取代已有卡必须带base_sha；新卡可不带。base_sha不进入卡头。
- 公共卡sha与现有指纹校验同算法。search最多5个公共池命中现读算sha，不改索引结构。
- 公共冲突两版只进公共池/冲突/<程序ID>.md，不进公共池待用户确认正文。
  public_cards/public_l1/search/死路环境对比不包含冲突目录。
- 公共卡取代先写归档副本，再写同目录非md临时文件，再os.replace现行卡，最后索引。
  注入归档后失败及replace失败时，旧/新现行卡仍存在；refresh后可检索。
- 新写入方法自身@serialized；W3日志在锁内只追加app_home/w3-writes.jsonl。
  项目按每目标保存记行；公共池仅已有卡的取代尝试记行，新建不记。
  每行键恰为at/session_id/target/has_base/result，不记正文或base；结果ok/conflict/rejected。
  日志IO失败按原有日志方式跳过，不加诊断类别。
- MCP和随包Pi说明同步base_sha及head重交；真实Pi由用户connect/reload，本轮不代改。

## 决策日志底层

- 路径项目/<名>/决策日志.md，头含project_id或未回填，注明程序只追加、删文件后重新编号。
- Store.append_decision_log自身序列化，原子写；DL-0001起，锁内取最大号加1，不复用。
- 字段id/at/session_id/text/why/decided_by/source；text、why均必填且合计≤200字，source可选。
- 不进启动包，不新增agent入口、分支目的日志或不可变修订库。

## 直接验收

| 检查 | 标准 |
|---|---|
| bootstrap sha→两会话先后同节写 | 从实际包解析sha；第二次冲突，两版保留，现行是首次内容 |
| agent漏base | 章节/整卡不覆盖且给head；新资源和用户入口保持约定 |
| 独立节并发 | 20线程、5项目×4节全部成功且互不覆盖 |
| 同节并发 | 20线程同一base恰1成功、19冲突，19份两版提议 |
| 跨宿主 | codex与pi同一base写同节，恰1成功、1冲突 |
| 公共卡并发/失败 | 同base第二次不覆盖；两个注入失败点现行始终存在且刷新可检索 |
| 冲突目录 | public_l1/search/死路环境均不含，既有正常卡行为不变 |
| 日志 | 固定五键，无正文/base；每次尝试一条 |
| 决策日志并发 | 20线程各1、4进程各5，均DL-0001..0020连续且完整 |
| 决策日志校验 | text/why空或合计201拒绝且文件不变；合计200可写 |
| 启动包 | sha增加≤60；冲突计数≤80、地图指针≤80；正常8格与已有预算压力仍在上限内 |
| 全量 | 无新增失败，diff-check通过；Windows隔离包验证，GUI由用户执行 |

## 地图与工作树审计

- 新map --project <名> [--repo <路径>]；--repo只读，不配置到真实项目路径。
- 默认git_root/paths；project_id随项目目录改名保留，缺ID标未回填，不自动回填。
- 地图UTF-8总字节≤12288，超限折叠最旧并列数量，不按时间判条目失效。
- 表格+简短汇总flowchart，只含git/项目卡事实；无reflog留空，两次生成除时间戳外相同。
- 无独有提交的tbrain/*分支折叠一行、动态计数；不硬编码23。
- 地图存在才放启动包指针，正文不注入，路径存在且仍守总预算。
- worktree audit只输出路径/前缀推断的创建者、独有提交、项目匹配、夹具名线索，明确是启发式。
- 真实仓库只读验证branch/worktree/status哈希相同，分支/工作树计数与git一致。
  真实Vault对应关系只用独立只读脚本统计数字；不对真实路径configure_project。
- v0.7.3全量污染探测把TEMP/TMP指到新哨兵目录，前后真实仓库branch/worktree/status相同，
  并检查哨兵外是否新增.tbrain-worktrees。不能复现就不改测试，不归因已有工作树。
- 30/100/300合成校准已完成；Obsidian渲染和真实宿主新会话定位由用户验证，保持未验证。

## 不做

不做W4、Dream、v1/v2、桌面地图页、分支目的、决策日志agent入口、changes_since或真实回填。
1E需根因报告与预览后另取用户批准、备份目录；本文件不授权写真实数据。
