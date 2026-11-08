"""Plain-language work contract placed on bootstrap and resume."""

from __future__ import annotations

from .store import COMPLETION_FILE

_TEMPLATE = """\
## 工作约定
只改这个项目的这一处工作区，不要改别的项目或其他副本。
工作区：{worktree}
完成标准字段：goal 目标、done 完成标准、evidence 证据、verify 验证。
目标：{goal}
做完的标准：{done}
自己验证：改完要跑本地检查（tbrain check），并留下真实证据（文件和行号、命令和输出，或 commit 号）。证据要配得上你声称的完成程度。
地基级别改动（数据库迁移、登录和权限、对外协议、安装和升级路径）需要 用户 决定。缺少关键业务口径时先查已有规则，查不清再集中询问。命名、风格、小重构自己定，不要问。
不要用“要不要我继续”这种问题停下来。
做了决定要写下为什么：理由、取舍、决定人。不要只写做了什么。
新会话收到首条用户消息后，先按“任务目标、本次范围与业务规则、交付结果、完成标准”四项复述理解，再等待用户明确确认。确认的是完整任务理解，包括用户要的结果、业务处理口径、信息保留要求和验收标准，不是技术方案。已有记忆和完成标准不能跳过这一步。只写确认的事实；未知内容写待确认。用通俗中文、短句、主动句，一句只说一件事，同一概念使用同一名称。先用 save 的 understanding 操作保存并展示复述。支持原生确认弹窗时必须优先弹窗，只有用户主动提交“确认”选项后才用 confirm 的 dialog_answer="确认" 记录；默认选中、取消、关闭或未提交都不算确认。没有弹窗时提示“请单独回复：确认”，结束本轮回复并等待；只有后续用户原话去除首尾空白后完全等于“确认”才用 confirm 引用 U#。不接受近义句或长消息中的确认字样，不猜确认意图。用户补充或更正信息后，更新并展示复述，再按上述方式等待确认；同一消息含更正和“确认”也不能直接开工。提交 confirm 前不要重存未变的复述。确认后不因后续消息、压缩、接续或项目模板缺项重复确认整个任务，只有用户指出整体理解有误时才重做首轮确认。执行中发现现有资料无法确定、且会改变业务结果的新缺口，可以集中补问：列出问题及影响范围，继续不依赖该决定的部分，不重启整项确认。已确认规则和明确的新要求直接遵守；技术问题自行查清。未知数据事实按已定口径留空或进入核实清单，不逐条打断开发。业务缺口用未完成目标 G# 记录，用户回答后保存决定和原话来源，再通过 complete_goals 关闭；未回答的保留，不猜定口径，不反复询问已回答的问题。未确认时不执行任务，已完成、项目提示词、已完成状态和验收会被拒绝。Tbrain 不能拦住宿主直接改文件。决定人只是记录，不核验身份，也不提升权限。
换个说法或换成另一条失败，只要没有新证据，都算没有实质进展。连续没有实质进展会暂停，并先把热交接写进这次会话。暂停后可以记证据和理由。要继续，先写下具体下一步，再由这次暂停之后的用户原话同意那一条。暂停前的旧原话不够。Tbrain 在保存、启动包和 hook 上拦住继续做；它停不掉外部 Agent 进程。
权限：读这个工作区里的代码、日志和测试可以直接做。推送分支、开拉取请求、开预览，要先被允许。生产环境、删除数据、碰密钥，必须 用户 明确允许。
收尾要交 A–E 最终验收，结论只能是 PASS、CONDITIONAL PASS 或 FAIL。CONDITIONAL PASS 必须写明人工剩余风险接受仍未完成。验收用短句写清楚：命令、路径、退出码，以及验过什么、没验什么。不要堆术语，也不要写「应该可以」。
Tbrain 桌面或 Windows 安装包：证据必须包含在真实 Windows 机器上安装并运行该产物。只跑 Linux 的 pytest 不算。没做这次真机验证时，结论不能是 PASS，要写 CONDITIONAL PASS 或 FAIL，并写明这个缺口。
Windows 安装运行结果由报告者声明并附证据。Tbrain 记录声明供人核对，不通过关键词或宿主上的无关命令认证安装结果。

Work contract: stay in this project's worktree only. Meet the goal and done criteria. Self-check with tbrain check and real evidence. Ask the user about foundation changes (database migrations, auth, external protocols, installer or upgrade paths) and unresolved business decisions that affect the result. Resolve naming, style, small refactors, and answerable technical questions yourself. Do not stop on weak questions. Record why: rationale, tradeoffs, and who decided. After the first user message in a new session, restate the task goal, scope and business rules, deliverables, and done criteria in plain language. After showing and saving the restatement, prefer the native confirmation dialog. Only an actively submitted Confirm option permits confirm with dialog_answer="确认". A default selection, cancellation, dismissal, or free-text supplement is not confirmation. Without a dialog, end the reply and wait for a later user message whose trimmed full text is exactly "确认"; cite its U# in confirm. Do not infer assent from synonyms or keywords. Incorporate supplements and corrections, show the updated restatement, and repeat this waiting step even if that message also mentions confirmation. Do not resave unchanged understanding before confirm. Confirm the desired outcome, not the technical plan. Existing memory cannot skip this step. Once confirmed, do not repeat the overall confirmation unless the user reports a misunderstanding. First check existing instructions and records. Ask new, necessary business questions together, state which work depends on each answer, and continue independent work. Track these gaps as existing open goals and close them after recording the user decision and source. Do not invent business rules or repeatedly ask answered questions. Missing data facts follow the established unknown-data policy. A reworded failure is not new progress; a no-progress pause stops further autonomous saves and does not kill an external agent. Final acceptance is A task status, B verification and evidence, C risks, D deliverables (commit, artifact, how to run), E conclusion only PASS, CONDITIONAL PASS, or FAIL. CONDITIONAL PASS means human residual-risk acceptance is still pending. A Tbrain desktop or Windows installable cannot be PASS without a real Windows machine install-and-run; Linux pytest alone is not enough. Write short sentences with the command, path, exit code, and what was or was not verified. Do not write vague claims such as "should be fine".
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
    note = ""
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
        note = str(card.get("worktree_error") or card.get("worktree_limitation") or "").strip()
        text = service.store.read_text(service.store.project_dir(project) / COMPLETION_FILE)
        found_goal = _section(text, "目标")
        found_done = _section(text, "完成标准")
        if found_goal:
            goal = found_goal
        if found_done:
            done = found_done
    text = _TEMPLATE.format(worktree=_flat(worktree, 300), goal=_flat(goal, 300), done=_flat(done, 400))
    if note:
        text += "\n" + note + "\n"
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
