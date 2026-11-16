# 阶段 1A：保存与并发验收约定

基座：阶段0版本修复 e2c749f，原有测试基线见 FREEZE-stage0。
来源：规划3/11 §1A；当前用户AGENTS要求最小实现，不扩修索引或新建治理系统。

## 需修的实际路径

- save_project保留用户正文、created_at和所有未知字段，只更新本入口管理的键。
- vault写锁使用filelock，补到项目卡保存、执行记录、删除、改名及会话改名。
- configure_project中的git prepare保持锁外；项目卡和执行记录分别短持锁。
- timeout_fallback在锁外算阈值和候选，逐会话锁内重读并判断是否仍需超时处理。
- settings读改写使用独立锁，首次未设vault也可保存。唯一临时文件、fsync、原子替换。
- operations边界报告OSError与已成功项；最终会话文件写失败保持抛异常，不伪报成功。
- 不改frontmatter字段集、索引schema、设置键集或公共池语义。

## 改前依据与时长

500会话均引用同一来源，50个已超时。TEMP校准脚本经隔离check运行。
整段service.rename_session拟包锁最大12.622秒；分段后确认主要是上轮500变动文件索引刷新。
Store.rename_session本体最大1.645秒；锁内重读目标及flow.rename_session最大1.541秒。
因此先在新增外层锁外完成session_detail/refresh，锁内重新读取目标再改名。
原有refresh_index自身整库锁最大11.796秒，是已知瓶颈，1A不扩修、不声称满足短锁阈值。
save_project最大7.68毫秒；record_execution最大15.89毫秒；单会话超时写最大55.39毫秒。

冻结的新/修改临界段上限：改名2秒；项目卡保存、执行记录、单会话超时各1秒。
vault等待超时10秒，settings等待超时5秒；改后重测，不以扩大超时凑通过。

## 直接验证

- 项目卡自造字段、worktree字段、created_at及正文在保存和task_mode=false的configure后完整。
- C10用Event屏障复现读旧写回：改前连续5次全失败；改后连续20次全通过。
- prepare阻塞期间另一线程save在1秒内完成。此项旧代码本就应通过，不造失败。
- 项目保存/save压力、vault→settings锁顺序、两个进程settings更新各重复20次通过。
  锁顺序固定vault→settings，settings锁内不取vault锁。不使用sleep制造测试时序。
- settings有/无vault两种情况保留不同键、UTF-8无BOM、临时文件无残留。
- operation中途PermissionError返回真实成功/拒绝；后续可保存项继续，最终落盘失败仍抛出。
- 保留跨进程项目章节存量用例。阶段结束全量无新增失败，git diff --check通过。

## 交付边界

R1合并阶段0修复，版本0.7.4；首次升级M2已定，不重新提问。
构建、安装、回退只在TEMP隔离根；真实安装、宿主配置、凭据不动。
真实ID和公共池回填仍需预览后批准；不push、不发布。
桌面GUI、Obsidian手改验证由用户执行，未执行就如实标未验证。

## 用户补充验证结果

用户U10原话：“GUI已经验证”。此前G5桌面GUI待验项按用户报告关闭。
用户未另述包版本和操作明细；本记录不扩展为所有候选版本或Obsidian手改验证均已通过。
