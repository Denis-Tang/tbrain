"""Tbrain MCP server: deterministic memory tools for coding agents.

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
- 保存时间由程序写入；事实发生时间、持续时间、数量、对象关系及适用范围按原话保留，不用保存时间替代。需要工作先后关系时引用编号，例如“D3 之后”。
- 指代其他条目用编号（D#、K#、E#、G#），不要复述内容。
- 证据 E# 只允许三种：文件:行号、命令+输出（≤3 行）、commit 号；没有证据的推断不要写成已排除。
- 已完成 D# 写成“动作 → 结果 [证据:E#]”；决策 K# 第一行写成“决策 | 理由”，并另起三行“理由：… / 取舍：… / 决定人：…”；
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
2. 本次技术决定：{"op":"append","section":"decision","items":[{"decision":"先修解析函数","reason":"失败已定位","rationale":"失败已定位","tradeoffs":"暂不改界面","decided_by":"Agent"}]}。用户长期决定用 save_decision，不重复写入 K# 或项目硬规则。
3. 走不通：{"op":"append","section":"dead_end","items":[{"attempt":"用 trigram 分词","reason_kind":"已证实不可行",
   "conditions":"SQLite 3.45 / FTS5","strength":"已排除","evidence":"E2"}]}
4. 证据：  {"op":"append","section":"evidence","items":[{"kind":"file","text":"src/tbrain/index.py:88"}]}
5. 未完成目标：{"op":"append","section":"goal","items":[{"text":"把 save 的拒绝重试写入待整理"}]}
6. 目标：  {"op":"replace","section":"target","text":"……（≤200 字）"}
7. 下一步：{"op":"replace","section":"next_step","text":"跑 pytest -q，确认中文检索用例全绿"}
8. 状态：  {"op":"set_status","status":"已完成","complete_goals":["G1"],"supersede":["K1","[[旧会话名]]#K2"]}
   supersede 只用于明确作废的决定，支持本会话或同项目来源；归档条目仍可引用，未确认建议不作为有效规则来源。
9. 环境变更：{"op":"env","items":[{"type":"安装","name":"jieba","version":"0.42.1","location":"venv",
   "install":"pip install jieba","uninstall":"pip uninstall jieba","verify":"python -c \"import jieba\"","base_sha":"ab12cd34","state":"生效"}]}
10. 项目提示词：{"op":"project_prompt","sections":{"最终目标":"…","硬规则":"- …（来源：[[会话名]]#U2）",
   "当前阶段":"…","关键路径和命令":"- 测试命令：…"},"base_sha":{"最终目标":"ab12cd34","硬规则":"ab12cd34","当前阶段":"ab12cd34","关键路径和命令":"ab12cd34"}}
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
        "启动/恢复本会话并返回启动包（全局及项目提示词、有效长期决定、公共池索引、附件卡片、接续视图前 5 条、当前时间）。"
        "有 hook 的宿主由 hook 自动完成，本工具用于无 hook 的宿主或需要手动刷新时。"
        "session_id 必须是宿主提供的稳定会话 ID，格式 <宿主名>:<宿主会话ID>（例如 claude-code:3f2a…）；"
        "agent 为 claude-code/codex/dsh/pi。新会话省略 project 时按 cwd 匹配；已有会话指定 project 会改变整段会话归属。"
        "仅保存其他项目的一项进展或决定时，在 save 或 save_decision 中传 project。"
        "返回 session_id 后，search/read/save/save_decision/resume 必须原样传入。"
    ))
    def bootstrap(session_id: str, agent: str, cwd: str, host_record: str = "", project: str = "") -> dict:
        result = service.bootstrap(session_id=session_id, agent=agent, cwd=cwd,
                                   host_record=host_record, project=(project or None))
        return _with_meta(service, result)

    @server.tool(description=(
        "按需检索本项目及允许读取的子项目的历史、长期决定与附件卡，再查公共池，每次最多 5 条并附来源路径与更新时间。"
        "项目开启『全局共享（读取非项目会话）』后，才额外检索独立会话池。"
        "session_id 用于确定本项目范围（省略时只搜公共池、全局附件与独立会话池）。"
        "检索命中不会切换保存目标。涉及已知失败对象时可把对象/方法写进 query。"
        "公共池命中附sha，更新已有卡时作为env的base_sha。"
        "结果只是摘要；使用 read 参数和 entry_ids 读取完整条目与证据，未读原文不要断言没有记录。"
    ))
    def search(query: str, session_id: str = "", limit: int = 5) -> dict:
        return _with_meta(service, service.search(query=query, session_id=session_id, limit=limit))

    @server.tool(description=(
        "只读历史记忆，不接续任务、不改变当前会话。target 使用 search 返回的 read.target，或会话 session_id、可见文档/已登记附件路径。"
        "entry_id 可指定 U#/D#/K#/E#/G#/X#（含归档）或决策日志 DL-xxxx；省略则读全文。"
        "E# 加 source=true 读取可见项目目录内或已登记附件的 UTF-8 正文；其余文件返回引用位置。G# 返回 done。"
        "start 为从0开始的字符偏移，limit 默认6000、最多20000；truncated=true 时以 next_start 续读，参数其余不变。"
        "只读取已保存内容，不代表拥有完整聊天记录；K# 是任务选择，不是用户长期业务约定。"
    ))
    def read(session_id: str, target: str, entry_id: str = '', start: int = 0,
             limit: int = 6000, source: bool = False) -> dict:
        return _with_meta(service, service.read(session_id, target, entry_id, start, limit, source))

    @server.tool(description=("保存本轮工作进展、证据、问题和下一步；临时要求及本次技术决定也在这里记录。"
                              "每次保存一个事项所属项目，project 可指定已有项目名，默认当前项目；后续调用仍用宿主 session_id。"
                              "用户长期决定调用 save_decision，不在本工具重复保存。只提交增量，不重写整篇。\n\n"
                              + WRITING_SPEC + "\n" + SAVE_TEMPLATE + "\n" + prompt.PROJECT_PROMPT_SPEC))
    def save(session_id: str, operations: list[dict], last_test: dict | None = None, project: str = '') -> dict:
        return _with_meta(service, service.save(session_id, operations, last_test, project))

    @server.tool(description=(
        "保存从用户原话提炼的项目方向和业务约定。自然表达含义清楚就记录，不要求正式确认；关键歧义先问。"
        "上下文只用于消解指代，不得把模型建议、实施选择或推测补成用户决定。保留条件、否定、时间和适用范围，未定内容仍写未定。"
        "text 为准确提炼的决定，source 为当前会话用户原话编号数组（如 [\"U2\",\"U4\"]）；"
        "why 仅填写用户说明的理由，未说明留空；text 与 why 合计最多200字。project 可指定事项所属的已有项目，默认当前项目。"
        "supersedes 仅填写被用户明确取代的目标项目 DL-xxxx 编号；含糊冲突先问，不自动作废。"
        "临时要求用 save；同一长期决定不重复写 K# 或项目硬规则。返回决定编号，供后续引用。"
        "程序自动附上真实来源原话；source存在只证明可追溯，不代表程序验证了提炼含义。"
    ))
    def save_decision(session_id: str, text: str, source: list[str], why: str = "",
                      supersedes: list[str] | None = None, project: str = '') -> dict:
        return _with_meta(service, service.save_decision(session_id=session_id, text=text, source=source,
                                                       why=why, supersedes=supersedes, project=project))

    @server.tool(description=(
        "返回旧会话的交接包（原始命令、目标、未完成目标、下一步、最近 3 条用户指令、已完成与证据清单及项目当前有效长期决定），"
        "并在当前会话总结写入“接续自”。target 是会话名或 session_id；旧会话文件不会被修改。"
        "仅用于继续旧任务；查询历史事实用 read，交接包不是完整历史。"
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
