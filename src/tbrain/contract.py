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
只有地基级别的改动才打断 Kai：数据库迁移、登录和权限、对外协议、安装和升级路径。命名、风格、小重构自己定，不要问。
不要用“要不要我继续”这种问题停下来。
做了决定要写下为什么：理由、取舍、决定人。不要只写做了什么。
目标和完成标准还没写清时，先问具体问题，并给出建议默认。探索可以读代码和测试，并保存证据、走不通的路、尝试。实现还没获批准：已完成、项目提示词、已完成状态和 PASS 会被拒绝。确认必须引用用户原话；原话要同意这个具体问题，或包含所确认的决定。决定人不必写在原话里，名字只是记录，不核验身份，也不提升权限。仓库和测试能回答的技术问题自己定，不要问。Tbrain 不能拦住宿主直接改文件。
换个说法或换成另一条失败，只要没有新的失败或通过次数，都算没有实质进展。同一测试的失败次数变化才算结果变了。连续没有实质进展会暂停，并先把热交接写进这次会话。暂停后可以记证据和理由。要继续，先写下具体下一步，再由这次暂停之后的用户原话同意那一条。暂停前的旧原话不够。Tbrain 在保存、启动包和 hook 上拦住继续做；它停不掉外部 Agent 进程。当前没有能核对 Windows 安装包的检查器，模型声明和无关的成功命令都不能把安装包标成 PASS。
权限：读这个工作区里的代码、日志和测试可以直接做。推送分支、开拉取请求、开预览，要先被允许。生产环境、删除数据、碰密钥，必须 Kai 明确允许。
收尾要交 A–E 最终验收，结论只能是 PASS、CONDITIONAL PASS 或 FAIL。CONDITIONAL PASS 必须写明人工剩余风险接受仍未完成。验收用短句写清楚：命令、路径、退出码，以及验过什么、没验什么。不要堆术语，也不要写「应该可以」。
Tbrain 桌面或 Windows 安装包：证据必须包含在真实 Windows 机器上安装并运行该产物。只跑 Linux 的 pytest 不算。没做这次真机验证时，结论不能是 PASS，要写 CONDITIONAL PASS 或 FAIL，并写明这个缺口。

Work contract: stay in this project's worktree only. Meet the goal and done criteria. Self-check with tbrain check and real evidence. Interrupt Kai only for foundation changes (database migrations, auth, external protocols, installer or upgrade paths), not for naming, style, or small refactors. Do not stop on weak questions. Record why: rationale, tradeoffs, and who decided. If the goal or done criteria are still unresolved, ask those concrete questions with the recommended default and do not turn them into PASS. A reworded failure is not new progress; a no-progress pause stops further autonomous saves and does not kill an external agent. Final acceptance is A task status, B verification and evidence, C risks, D deliverables (commit, artifact, how to run), E conclusion only PASS, CONDITIONAL PASS, or FAIL. CONDITIONAL PASS means human residual-risk acceptance is still pending. A Tbrain desktop or Windows installable cannot be PASS without a real Windows machine install-and-run; Linux pytest alone is not enough. Write short sentences with the command, path, exit code, and what was or was not verified. Do not write vague claims such as "should be fine".
"""


def render_contract(service, project: str = "", cwd: str = "") -> str:
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
        if status == "worktree" and recorded:
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
    try:
        from .progress import contract_extra

        extra = contract_extra(service, project)
    except Exception:
        extra = ""
    return text + (extra or "")


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
