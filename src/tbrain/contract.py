"""Plain-language work contract placed on bootstrap and resume."""

from __future__ import annotations

from .store import COMPLETION_FILE

_TEMPLATE = """\
## 工作约定
只改这个项目的这一处工作区：{worktree}
字段：goal目标、done完成标准、evidence证据、verify验证。
目标：{goal}
做完的标准：{done}
改后跑本地检查tbrain check；真实证据写文件行号、命令输出/退出码或commit，禁写「应该可以」。地基级别改动（数据库迁移、登录和权限、对外协议、安装和升级路径）由用户决定；命名、风格、小重构自定。不要用“要不要我继续”这种问题停下来。决定记理由、取舍、决定人。
首条消息先查指令、项目文件和记录，再复述任务目标、本次范围与业务规则、交付结果、完成标准，含信息保留与验收；记忆不能跳过。用通俗中文、短句和主动句，同一概念同一名称。只写确认事实，未知待确认。save understanding后展示，等用户确认。
支持原生确认弹窗时必须优先弹窗；主动选确认才记confirm(dialog_answer="确认")；默认选中、取消、关闭或未提交都不算确认。无弹窗先结束回复，等后续用户整条原话去首尾空白后完全等于“确认”，再用U#记录confirm；不接受近义句或长消息中的字样。首次确认前补充更正后，更新理解再等，同条确认不算；不重存未变理解。
未确认时不执行任务。确认后不因后续消息、压缩、接续或项目模板缺项重复确认整个任务；只有用户指出整体理解有误时才重做首轮确认。确认后明确新要求直接遵守；仅新业务缺口集中列问题和影响，继续无关工作，不重启整项确认；用G#记录，用户回答后记决定/原话再complete_goals。已定不反复问；技术自查，未知数据留空/待核实。
换说法或换失败而无新证据仍算停滞。连续没有实质进展会暂停并存热交接；暂停只记证据、理由、下一步。恢复先提具体路线，再由这次暂停之后的用户原话同意那一条，旧授权不算；Tbrain停不掉外部Agent。
本工作区代码/日志/测试可直接读。推送分支、开拉取请求、开预览，要先被允许。生产环境、删除数据、碰密钥，必须 用户 明确允许。
收尾A–E：任务/验证证据/风险/交付物/结论。只用PASS、CONDITIONAL PASS 或 FAIL。CONDITIONAL PASS 必须写明人工剩余风险接受仍未完成。短句写命令、路径、退出码、验过和没验什么。
Tbrain桌面或Windows安装包须在真实 Windows 机器上安装并运行该产物；只有Linux pytest或未验真机，结论不能是 PASS。Windows 安装运行结果由报告者声明并附证据，不靠关键词或无关命令认证。Tbrain 不能拦住宿主直接改文件；决定人只是记录，不核验身份，不提升权限。

"""


def render_contract(service, project: str = "", cwd: str = "", *, task_mode: bool = False) -> str:
    if not task_mode:
        return ""
    try:
        return _render(service, str(project or "").strip(), str(cwd or "").strip())
    except Exception:
        return _render(None, "", str(cwd or "").strip())


def _render(service, project: str, cwd: str) -> str:
    worktree = cwd or "还没有绑定代码目录"
    goal = "还没有写下目标。"
    done = "还没有写下完成标准。"
    if service is not None and project:
        card = service.store.project(project) or {}
        recorded = str(card.get("worktree") or "").strip()
        paths = card.get("paths") or []
        status = str(card.get("worktree_status") or "")
        if cwd and service.store.match_project(cwd) == project:
            worktree = cwd
        elif status == "worktree" and recorded:
            worktree = recorded
        elif paths:
            worktree = str(paths[0])
        text = service.store.read_text(service.store.project_dir(project) / COMPLETION_FILE)
        found_goal = _section(text, "目标")
        found_done = _section(text, "完成标准")
        if found_goal:
            goal = found_goal
        if found_done:
            done = found_done
    text = _TEMPLATE.format(worktree=_flat(worktree, 300), goal=_flat(goal, 300), done=_flat(done, 400))
    return text


def _section(text: str, heading: str) -> str:
    collecting = False
    body: list[str] = []
    for line in str(text or "").replace("\r\n", "\n").split("\n"):
        if line.startswith("## "):
            if collecting:
                break
            collecting = line[3:].strip() == heading
            continue
        if collecting:
            body.append(line)
    return "\n".join(body).strip()


def _flat(text: str, limit: int) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value or "（空）"
    return value[: limit - 1] + "…"
