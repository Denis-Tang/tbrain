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
权限：读这个工作区里的代码、日志和测试可以直接做。推送分支、开拉取请求、开预览，要先被允许。生产环境、删除数据、碰密钥，必须 Kai 明确允许。
收尾要交 A–E 最终验收，结论只能是 PASS、CONDITIONAL PASS 或 FAIL。CONDITIONAL PASS 必须写明人工剩余风险接受仍未完成。
Tbrain 桌面或 Windows 安装包：证据必须包含在真实 Windows 机器上安装并运行该产物。只跑 Linux 的 pytest 不算。没做这次真机验证时，结论不能是 PASS，要写 CONDITIONAL PASS 或 FAIL，并写明这个缺口。

Work contract: stay in this project's worktree only. Meet the goal and done criteria. Self-check with tbrain check and real evidence. Interrupt Kai only for foundation changes (database migrations, auth, external protocols, installer or upgrade paths), not for naming, style, or small refactors. Do not stop on weak questions. Record why: rationale, tradeoffs, and who decided. Final acceptance is A task status, B verification and evidence, C risks, D deliverables (commit, artifact, how to run), E conclusion only PASS, CONDITIONAL PASS, or FAIL. CONDITIONAL PASS means human residual-risk acceptance is still pending. A Tbrain desktop or Windows installable cannot be PASS without a real Windows machine install-and-run; Linux pytest alone is not enough.
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
    if service is not None and project:
        card = service.store.project(project) or {}
        recorded = str(card.get("worktree") or "").strip()
        paths = card.get("paths") or []
        worktree = recorded or (str(paths[0]) if paths else worktree)
        text = service.store.read_text(service.store.project_dir(project) / COMPLETION_FILE)
        found_goal = _section(text, "目标")
        found_done = _section(text, "完成标准")
        if found_goal:
            goal = found_goal
        if found_done:
            done = found_done
    return _TEMPLATE.format(worktree=_flat(worktree, 300), goal=_flat(goal, 300), done=_flat(done, 400))


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
