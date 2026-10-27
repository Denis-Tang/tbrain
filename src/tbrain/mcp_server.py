"""Tbrain MCP server: four deterministic tools for coding agents.

The tools never call a model.  The model submits incremental operations; the
program validates, numbers, indexes and writes them.
"""

from __future__ import annotations

from pathlib import Path

from . import __version__, prompt
from .service import TbrainService

WRITING_SPEC = """\
写作规范（写进每一条记忆前先读一遍，一次写对）：
- 一条只说一件事，句式：对象 + 动作 + 结果。对象写全称（文件名、函数名、路径、命令、工具名），
  不写“它、这个、上面那个、该模块、最近、刚才、现在、昨天”。
- 时间由程序写入，不要自己写时间；需要先后关系时引用编号，例如“D3 之后”。
- 指代其他条目用编号（D#、K#、E#、G#），不要复述内容。
- 证据 E# 只允许三种：文件:行号、命令+输出（≤3 行）、commit 号；没有证据的推断不要写成已排除。
- 已完成 D# 写成“动作 → 结果 [证据:E#]”；决策 K# 写成“决策 | 理由”；
  未走通的路 X# 写成“尝试 | 原因类别 | 当时条件 | 结论强度”，原因类别只允许
  环境/权限限制、时间不够、证据不足、已证实不可行；结论强度只允许 已排除（必须附证据）或 暂缓。
- 下一步只能 1 条（≤150 字），写成“动作 + 怎么验证”；目标 ≤200 字。
- 单条 ≤200 字；D#/K# 各超过 30 条会自动移入归档。
- operations 按顺序执行：先保存证据，再用返回的 E# 引用；只补交 rejected 中失败的部分。
- ok=true 表示全部通过；ok=false 时 written 仍列出已保存内容，待整理不代表校验通过。
- last_test 仅在没有宿主采集结果时补报，不能覆盖宿主记录。
"""

SAVE_TEMPLATE = """\
save 操作示例（operations 是对象数组，一次可提交多个操作）：
1. 完成项：{"op":"append","section":"done","items":[{"text":"给 index.py 增加中文两字切分影子列 [证据:E1]"}]}
2. 决策：  {"op":"append","section":"decision","items":[{"text":"SQLite 只做索引 | Markdown 是唯一事实源"}]}
3. 走不通：{"op":"append","section":"dead_end","items":[{"attempt":"用 trigram 分词","reason_kind":"已证实不可行",
   "conditions":"SQLite 3.45 / FTS5","strength":"已排除","evidence":"E2"}]}
4. 证据：  {"op":"append","section":"evidence","items":[{"kind":"file","text":"src/tbrain/index.py:88"}]}
5. 未完成目标：{"op":"append","section":"goal","items":[{"text":"把 save 的拒绝重试写入待整理"}]}
6. 目标：  {"op":"replace","section":"target","text":"……（≤200 字）"}
7. 下一步：{"op":"replace","section":"next_step","text":"跑 pytest -q，确认中文检索用例全绿"}
8. 状态：  {"op":"set_status","status":"已完成","complete_goals":["G1"]}
9. 环境变更：{"op":"env","items":[{"type":"安装","name":"jieba","version":"0.42.1","location":"venv",
   "install":"pip install jieba","uninstall":"pip uninstall jieba","verify":"python -c \"import jieba\"","state":"生效"}]}
10. 项目提示词：{"op":"project_prompt","sections":{"最终目标":"…","硬规则":"- …（来源：[[会话名]]#U2）",
   "当前阶段":"…","关键路径和命令":"- 测试命令：…"}}
"""


def _with_meta(service: TbrainService, result: dict) -> dict:
    if isinstance(result, dict):
        result.setdefault("tbrain_version", __version__)
        if "current_time" not in result:
            try:
                result["current_time"] = service.time_now().get("display", "")
            except Exception:
                result["current_time"] = ""
    return result


def create_server(home: Path | None = None, *, service=None):
    from mcp.server.fastmcp import FastMCP

    service = service or TbrainService(home)
    server = FastMCP("Tbrain")
    server._mcp_server.version = __version__

    @server.tool(description=(
        "启动/恢复本会话并返回启动包（全局提示词、项目提示词、公共池索引、附件卡片、接续视图前 5 条、当前时间）。"
        "有 hook 的宿主由 hook 自动完成，本工具用于无 hook 的宿主或需要手动刷新时。"
        "session_id 必须是宿主提供的稳定会话 ID，格式 <宿主名>:<宿主会话ID>（例如 claude-code:3f2a…）；"
        "agent 为 claude-code/codex/dsh/pi。project 仅用于明确切换保存目标，省略表示按 cwd 匹配。"
        "返回 session_id 后，search/save/resume 必须原样传入。"
    ))
    def bootstrap(session_id: str, agent: str, cwd: str, host_record: str = "", project: str = "") -> dict:
        result = service.bootstrap(session_id=session_id, agent=agent, cwd=cwd,
                                   host_record=host_record, project=(project or None))
        return _with_meta(service, result)

    @server.tool(description=(
        "按需检索：本项目 → 公共池 → 独立会话池，每次最多 5 条并附来源路径与更新时间。"
        "session_id 用于确定本项目范围（省略时只搜公共池与独立会话池）。"
        "检索命中不会切换保存目标。涉及已知失败对象时可把对象/方法写进 query。"
    ))
    def search(query: str, session_id: str = "", limit: int = 5) -> dict:
        return _with_meta(service, service.search(query=query, session_id=session_id, limit=limit))

    @server.tool(description=("把本轮增量写入当前会话总结。只提交变化的部分，不要重写整篇。\n\n"
                              + WRITING_SPEC + "\n" + SAVE_TEMPLATE + "\n" + prompt.PROJECT_PROMPT_SPEC))
    def save(session_id: str, operations: list[dict], last_test: dict | None = None) -> dict:
        return _with_meta(service, service.save(session_id=session_id, operations=operations, last_test=last_test))

    @server.tool(description=(
        "返回旧会话的交接包（原始命令、目标、未完成目标、下一步、最近 3 条用户指令、已完成与证据清单），"
        "并在当前会话总结写入“接续自”。target 是会话名或 session_id；旧会话文件不会被修改。"
        "历史分支传 fork_revision（精确保存版本）或 fork_user_count（用户轮次），只继承该保存点。"
    ))
    def resume(session_id: str, target: str, fork_user_count: int | None = None, fork_revision: str = "") -> dict:
        """历史分支传 fork_revision（精确保存版本）或 fork_user_count（用户轮次）；省略为普通接续。"""
        return _with_meta(service, service.resume(session_id=session_id, target=target,
                          fork_user_count=fork_user_count, fork_revision=fork_revision))

    return server


def run(home: Path | None = None, agent: str = '') -> None:
    import os

    service = TbrainService(home, agent=agent)
    watcher = None
    try:
        from . import proc

        proc.register("mcp", service.version(), home=service.home)
        watcher = proc.start_watcher(on_exit=lambda: os._exit(0), home=service.home)
        # 启动即淘汰：新版本一启动就通知旧版本进程退出（后台执行，不拖慢握手）。
        proc.evict_in_background(service.version(), home=service.home)
    except Exception:
        watcher = None
    try:
        create_server(home, service=service).run(transport="stdio")
    finally:
        try:
            if watcher is not None:
                watcher.stop()
            from . import proc

            proc.unregister(home=service.home)
        except Exception:
            pass
