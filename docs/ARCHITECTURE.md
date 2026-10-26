# Tbrain 0.6.0 实现架构（第 3、4 步）

> 本文件是并行开发的接口契约：模块边界、文件格式、数据结构、调用约定。后续实现以本文件为准；设计稿优先级高于本文件，冲突时以 canvas/调研报告为准并更新本文件与 DECISIONS。

## 1. 模块划分与写入范围

| 模块 | 文件 | 负责人 | 说明 |
|---|---|---|---|
| 版本/宿主 | `installer.py`、`update.py`、`proc.py`、`paths.py`、`hosts/*`、`cli.py`、`integrations/*`、`scripts/build.ps1` | B（hosts-versioning） | 固定入口、版本化安装、旧进程清理、connect/disconnect/doctor/upgrade、四宿主配置与 hook 适配 |
| 存储/索引 | `clock.py`、`naming.py`、`store.py`、`index.py` | A（core-store） | 时间工具、会话命名、vault 文件读写与所有权、SQLite 索引与中文检索 |
| 流程层 | `handoff.py`、`identity.py`、`hooks.py`、`session.py`、`service.py`、`prompt.py`、`mcp_server.py`、`desktop.py` | Lead | bootstrap/save/search/resume、hook 语义、handoff/v1、项目提示词校验、MCP 工具、桌面后端 API |
| 前端 | `web/index.html`、`web/app.js`、`web/bridge.js`、`web/style.css` | C（frontend） | 三页 UI；按本地前端设计基本要求（圆角、非线性动画、视觉对齐） |

公共约定：
- Python 3.11+，UTF-8 文本（文件读写统一 `utf-8`，写出用 `utf-8` 无 BOM）。Windows 行尾用 `\r\n` 或 `\n` 均可，但同一文件保持一致（新建文件用 `\n`）。
- 版本只从 `tbrain.__version__` 读。
- 时间只由程序写：`clock.py` 提供统一入口。
- 所有面向用户的写操作必须原子：同目录临时文件；新会话用 `os.link` 创建最终文件（已存在则递增序号），已有文件用 `os.replace` 更新。
- 程序只写自己的区块；用户手改检测用指纹（A 的 `store.py` 提供）。

## 2. 路径与设置

应用数据目录 `app_home`：`TBRAIN_HOME` 环境变量，否则 `~/.tbrain`。

```
~/.tbrain/
  settings.json
  index.sqlite3            # SQLite 索引（WAL, busy_timeout=5000）
  time.json                # 校时缓存
  processes/<pid>.json     # 进程登记（pid, version, role, exe, started_at）
  updates/                 # 下载的升级包与记录
  update-state.json
  logs/
```

vault（用户知识库，默认 `%USERPROFILE%\Documents\TbrainVault`）：

```
全局提示词.md
全局提示词变更日志.md
会话总结/<项目名|独立>/YYYY/MM/<名称>.md
项目/<项目名>/项目卡.md
项目/<项目名>/项目提示词.md
项目/<项目名>/项目提示词变更日志.md
项目/<项目名>/会话索引.md
公共池/<类型>/<卡片名>.md
公共池/<类型>/归档/<卡片名>.md
独立会话池/会话索引.md
附件/<类型>/<卡片名>.md
.tbrain/                 # 程序内部：待用户确认、迁移记录等（Obsidian 可读，不要求人工编辑）
```

`paths.py`：`app_home()`、`install_root()`、`default_install_root()`（`%LOCALAPPDATA%\Programs\Tbrain`）、`versions_dir()`、`current_dir()`、`bin_entry()`。

settings.json 字段（键名固定）：

```json
{
  "vault_path": "%USERPROFILE%\\Documents\\TbrainVault",
  "timezone": "Asia/Shanghai",
  "ntp_enabled": true,
  "timeout_hours": 6,
  "reminder_context_switch": true,
  "install_root": "C:\\Users\\<user>\\AppData\\Local\\Programs\\Tbrain"
}
```

## 3. clock.py（A）

```python
@dataclass
class Now:
    utc: datetime          # tz-aware UTC，唯一真实时间
    local: datetime        # 按 settings.timezone 显示
    timezone: str          # IANA 名
    calibrated: bool       # 是否已校时（本次或缓存有效）
    source: str            # "ntp" | "https-date" | "system"
    offset_seconds: float  # 网络时间 - 系统时间

def now(app_home: Path, settings) -> Now: ...      # 每次返回带“当前时间”
def display(now: Now) -> str: ...                  # 例：2026-10-24 21:55:22 (Asia/Shanghai, 已校准)
def iso_utc(dt) -> str
def parse_utc(text) -> datetime
def sync(app_home: Path, settings, force=False) -> dict   # NTP -> HTTPS Date 兜底；失败标未校准
def set_timezone(settings, tz: str) -> None
def validate_timezone(tz: str) -> str              # 非法抛出 ValueError
```

规则：存储 UTC；NTP 用 socket 实现 SNTP（5s 超时，`ntp.aliyun.com`/`pool.ntp.org` 轮询），失败用 HTTPS `Date` 响应头（httpx，5s 超时，任选 `https://www.baidu.com`/`https://www.cloudflare.com`）；偏差记为 `time.json`，有效 6 小时；断网/关闭校时 → 用系统时间并 `calibrated=False`。依赖：`tzdata`（Windows 的 zoneinfo 数据）、`tzlocal`（系统时区检测）。

## 4. naming.py（A）

```python
def session_name(first_message: str, project_name: str, history_names: list[str],
                 date: str, sequence: int) -> str
def is_low_information(first_message: str) -> bool
def temp_name(date: str, sequence: int) -> str           # "未命名-YYYY-MM-DD-1"
def resume_name(old_name: str, date: str, sequence: int) -> str   # "<old> 续 YYYY-MM-DD-1"
def sanitize_filename(name: str) -> str
```

规则（canvas“会话命名”7 条）：去客套固定词表 → 去括号/代码块/网址、路径只留文件名 → 取第一句（。？！换行）→ ≤20 字直接用，超过用 jieba 分词按原文顺序取关键词（动/名词优先）→ 去掉与项目名重复词 → 用本项目历史会话名降权所有会话都有的词、使名称彼此可区分 → 清洗非法字符 → 末尾加 `日期-当日序号`。确定性：同一输入必得同一名称（历史词按传入 list 计算）。

## 5. store.py（A）

```python
class Store:
    def __init__(self, vault: Path, app_home: Path): ...
    # 目录与路径
    def ensure_layout(self) -> None
    def session_dir(self, project_name: str, when: datetime) -> Path
    def session_path(self, project_name: str, when: datetime, name: str) -> Path
    # 原子写
    def write_text(self, path: Path, text: str, *, fsync: bool = True, create: bool = False) -> None
        # tmp + os.link（create=True，撞名失败）或 os.replace（更新已有文件）
    def read_text(self, path: Path) -> str
    # 区块所有权
    def block_fingerprint(self, text: str) -> str
    def block_state(self, path: Path, block: str) -> dict      # {owner, fingerprint}
    def set_block(self, path: Path, block: str, owner: str, text: str) -> bool
        # 写前比较指纹：不一致且 owner 非 user → 转为 user 并返回 False（调用方生成“待用户确认”）
    # 项目
    def projects(self) -> list[dict]                            # name, paths, parent, allow_child, session_count
    def project(self, name: str) -> dict | None
    def save_project(self, name, paths, parent="", allow_child=False, rename_from="") -> dict
    def delete_project(self, name) -> dict
    def match_project(self, cwd: str) -> str                    # 最长路径匹配，"" = 独立
    # 提示词
    def global_prompt(self) -> str
    def save_global_prompt(self, text) -> dict                  # 只写 model 区；用户区保护
    def project_prompt(self, name: str) -> dict                 # {"sections": {...}, "user": "..."}
    def save_project_prompt(self, name: str, sections: dict) -> dict
        # 校验见 prompt.py；替换内容写变更日志；用户区不动
    # 公共池
    def public_cards(self, kind: str | None = None) -> list[dict]
    def save_public_card(self, kind, name, body, meta) -> dict  # 同对象新卡生效，旧卡标已被取代并归档
    def public_l1(self, max_lines=40) -> str                     # 程序生成：每类一行 + 热点事实
    # 附件（只读）
    def attachments(self, project_name: str = "") -> list[dict]  # name, type, path, note, group, projects
    # 索引文件（程序生成，不解析）
    def write_project_index(self, project_name: str, rows: list[dict]) -> Path
    def write_independent_index(self, rows: list[dict]) -> Path
    # 重命名与链接
    def rename_session(self, old_path: Path, new_name: str) -> Path   # 移动文件 + 返回新路径
    # 变更日志
    def append_changelog(self, path: Path, title: str, old: str) -> None
```

文件格式约定：

### 会话总结 `会话总结/.../<名称>.md`

```markdown
---
name: <名称>
project: <项目名>            # 独立会话为空
session_id: "claude-code:uuid"
agent: claude-code
cwd: D:\work\project
host_record: ""             # 宿主会话记录地址（有则写）
started_at: 2026-10-24T13:00:00Z
updated_at: 2026-10-24T13:05:00Z
last_activity_at: 2026-10-24T13:05:00Z
resumed_from: ""            # 有则写 [[旧会话]]
git: {branch: "", head: "", dirty: 0}
last_verify: {command: "", exit_code: null, at: ""}
status: 进行中
---

## 原始命令
（逐字，程序永不改）

## 用户指令
### U1
（逐字；超过 2000 字只存前 2000 字 + 原文位置说明）

## 目标
（≤200 字）

## 已完成
### D1
动作 → 结果 [证据:E1]

## 决策
### K1
决策 | 理由

## 未走通的路
### X1
尝试 | 原因类别 | 当时条件 | 结论强度

## 证据
### E1
（文件:行号 / 命令+输出≤3 行 / commit 号）

## 未完成目标
### G1
- [ ] 内容

## 下一步
（1 条 ≤150 字，“动作 + 怎么验证”）

## 待整理
（未校验原文，标“未校验”）

## 待用户确认
（冲突项）

## 归档
（D#/K# 超 30 条后移入）
```

### 项目卡 `项目/<名>/项目卡.md`

```markdown
---
name: <项目名>
paths:
  - D:\work\project
parent: ""
allow_child: false
created_at: 2026-10-24T13:00:00Z
---
（用户备注区，程序不动）
```

### 全局提示词 `全局提示词.md`

```markdown
<!-- owner:user -->
（用户手写，程序与模型都不改）
<!-- /owner:user -->
<!-- owner:model -->
（第一期不启用模型区）
<!-- /owner:model -->
```

### 公共池卡片 `公共池/<类型>/<名>.md`

```markdown
---
id: <类型>-<名称>
kind: 安装|环境变量|PATH|代理|配置|工作区|工具
name: ...
version: ...
install: ...
package_id: ...
location: ...
scope: ...
uninstall: ...
verify: ...
source_session: "[[会话名]]"
created_at: ...
updated_at: ...
state: 生效|已被取代
history: []          # [{at, action, note}]
---
（正文：模型/用户可读说明；程序只改 YAML 与 history）
```

### 附件卡片 `附件/<类型>/<名>.md`（只读）

```markdown
---
name: 卡片名
type: skills|插件|工具|程序|模型|提示词|参考资料
path: D:\...\文件或目录
note: 备注（模型可读）
group: 分组名
projects: []        # 空 = 所有项目
---
```

### 项目提示词（严格按《项目提示词写作规范》）

```markdown
<!-- owner:user -->
## 用户补充
（用户手写区）
<!-- /owner:user -->

<!-- owner:model -->
## 最终目标
## 硬规则
## 当前阶段
## 关键路径和命令
<!-- /owner:model -->
```

## 6. index.py（A）

SQLite（`app_home/index.sqlite3`，WAL，`PRAGMA busy_timeout=5000`）：

```sql
meta(key TEXT PRIMARY KEY, value TEXT);                -- schema_version, vault_path
files(path TEXT PRIMARY KEY, mtime REAL, size INTEGER, hash TEXT);
sessions(session_id TEXT PRIMARY KEY, project TEXT, name TEXT, path TEXT,
         status TEXT, agent TEXT, cwd TEXT, started_at TEXT, updated_at TEXT,
         last_activity_at TEXT, next_step TEXT, open_goals INTEGER,
         git_branch TEXT, git_head TEXT, git_dirty INTEGER, last_verify TEXT,
         file_hash TEXT, file_mtime REAL);
sessions_fts(session_id UNINDEXED, project UNINDEXED, name, body, cn);  -- FTS5
public_cards(id TEXT PRIMARY KEY, kind TEXT, name TEXT, state TEXT, path TEXT, body TEXT, updated_at TEXT);
public_fts(id UNINDEXED, kind UNINDEXED, name, body, cn);               -- FTS5
session_links(host TEXT, host_session_id TEXT, session_id TEXT, cwd TEXT,
              first_message_hash TEXT, created_at TEXT,
              PRIMARY KEY(host, host_session_id));
access_log(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, kind TEXT, at TEXT);
blocks(path TEXT, block TEXT, owner TEXT, fingerprint TEXT, updated_at TEXT,
       PRIMARY KEY(path, block));
```

API：

```python
class Index:
    def __init__(self, app_home: Path): ...
    def connect(self): ...                              # contextmanager，事务内写
    def upsert_session(self, row: dict, body: str) -> None     # 同事务更新 FTS（DELETE+INSERT）
    def upsert_public(self, row: dict, body: str) -> None
    def remove_session(self, session_id: str) -> None
    def session(self, session_id: str) -> dict | None
    def sessions(self, project: str | None = None, status: list[str] | None = None) -> list[dict]
    def resume_view(self, project: str, child_projects: list[str], limit=5, related_query="") -> list[dict]
    def search_sessions(self, query: str, projects: list[str], limit=5) -> list[dict]
    def search_public(self, query: str, limit=5) -> list[dict]
    def bigram_terms(self, text: str) -> list[str]       # 中文两两重叠切分，供 FTS 查询构造
    def log_access(self, session_id: str, kind: str) -> None
    def access_history(self, session_id: str, limit=10) -> list[str]
    def mark_scan(self, path: str, mtime: float, size: int, hash: str) -> None
    def file_state(self, path: str) -> dict | None
    def block_state(self, path: str, block: str) -> dict | None
    def set_block_state(self, path: str, block: str, owner: str, fingerprint: str) -> None
```

检索规则：FTS5 表 `cn` 列存中文两两重叠切分（如“数据库”→“数据 据库”），查询同样切分；ASCII 词按原样；单字中文查询退回 `LIKE`；结果按 bm25 + 状态优先级 + 新近度加权；每次最多 5 条，附 `path`/`updated_at`/`project`/`status`。

## 7. handoff.py（Lead）

```python
SECTIONS = ["原始命令", "用户指令", "目标", "已完成", "决策", "未走通的路", "证据", "未完成目标", "下一步", "待整理", "待用户确认", "归档"]
STATUSES = ["进行中", "受阻", "待验证", "已完成", "已放弃", "已中断"]
REASON_KINDS = ["环境/权限限制", "时间不够", "证据不足", "已证实不可行"]
STRENGTHS = ["已排除", "暂缓"]
@dataclass
class Summary: header: dict; raw_command: str; users: list[dict]; target: str; done: list[dict];
               decisions: list[dict]; dead_ends: list[dict]; evidence: list[dict]; goals: list[dict];
               next_step: str; pending: list[str]; conflicts: list[str]; archive: dict
def load(path: Path) -> Summary
def render(summary: Summary) -> str
def new_summary(...) -> Summary
def next_number(items: list[dict], prefix: str) -> int
```

编号：U/D/K/X/E/G 各自递增；`D#` 每条 ≤200 字；`D#`/`K#` 超 30 条移入“归档”节。

## 8. identity.py（Lead）

```python
@dataclass
class Identity:
    session_id: str          # "<host>:<host_session_id>"
    host: str
    host_session_id: str
def make(host: str, host_session_id: str) -> Identity
def ensure_summary(store, index, identity, cwd, host_record, first_message, settings) -> tuple[Summary, bool]
    # 只按宿主会话 ID 复用；新 ID（含 fork）新建薄总结，相同目录与首条消息不决定身份。
```

会话内容变化时在 `.tbrain/会话版本/<会话ID指纹>/U<轮次>-<版本>.md` 保留 Markdown 保存点；心跳复用同一语义版本。`resume` 的 `fork_revision` 精确选点，`fork_user_count` 选择该用户轮次后的最新版本；无分叉参数为普通接续。分支只继承选定保存点，新会话原始命令和父会话文件不被覆盖。

公共池整卡、项目提示词按节持久化用户所有权与写入指纹。来源校验读取真实总结的 U#/E#，局部引用通过当前 Summary 解析；不存在的引用退回。冲突内容保存到项目或公共池的 `待用户确认.md`，启动时显示。会话写入与外部刷新都同步生成 Markdown 索引；生成过程使用 SQLite 写事务避免过期视图后写覆盖。

`resume_feedback` 保存展示时的候选与实际接续选择，离线回放 τ=3/7/14 的 MRR、前5命中率。50 条样本后允许评估；状态优先、同状态按新近的规则下 τ 不改变名次，不作无效切换。上下文提醒不推测缺少价格依据的费用百分比。

## 9. hooks.py（Lead）

输入中性事件：

```python
@dataclass
class HookEvent:
    host: str                 # claude-code|codex|dsh|pi|dsh-native
    kind: str                 # session-start|user-prompt|stop|pre-compact|session-end|context
    host_session_id: str
    cwd: str
    source: str = ""          # session-start: startup|resume|clear|compact|fork
    prompt: str = ""          # user-prompt
    stop_hook_active: bool = False
    turn_id: str = ""
    context_tokens: int | None = None
    context_window: int | None = None

@dataclass
class HookResult:
    additional_context: str = ""
    decision: str = ""        # "" | "block"
    reason: str = ""
    heartbeat: bool = True

def handle(event: HookEvent, deps) -> HookResult: ...
```

语义：
- `user-prompt`：若会话不存在 → 立项+bootstrap 启动包作为 `additional_context`；记录 U#；若已存在 → 只记录 U#，返回空（不重复注入）；以 `save_state` 判断是否本轮已保存。
- `session-start`：`startup` 不做事（等第一条消息）；`resume`/`compact`/`clear` → 复用原总结，重新组装启动包（检索词=原命令+最近 3 条 U#）注入。
- `stop`：本轮流式结束；若本轮未 save → 返回 `decision=block`（Claude/Codex 阻止结束），DSH 原生改 steer，pi 只提醒；`stop_hook_active=True` 时不再阻断。刷新心跳。
- `pre-compact`：返回重新注入用户指令的上下文（防压缩丢约束），并提醒 save。
- `session-end`：状态“进行中”→“已中断”；刷新心跳。
- `context`：DSH/pi 提供上下文用量时，判断是否建议换会话（阈值/收益估算），结果附在返回值。
- 超时兜底在每次 handle 开头执行：把“进行中且 last_activity 超过 N 小时”的会话标“已中断”；同 ID 再次活动 → 改回“进行中”。
- 版本自检：若登记的最高 MCP 版本 ≠ current `__version__`，`additional_context` 前加一行提醒。

## 10. session.py / service.py（Lead）

```python
class TbrainService:
    def __init__(self, home: Path | None = None): ...
    def bootstrap(self, host, host_session_id, cwd, host_record="", project=None) -> dict
    def search(self, query, session_id="", limit=5) -> dict
    def save(self, session_id: str, operations: list[dict], last_test: dict | None = None) -> dict
    def resume(self, session_id: str, target: str) -> dict
    def status(self) -> dict
    def sessions(self, filter="all") -> list[dict]
    def settings(self) -> dict; def save_settings(self, values: dict) -> dict
    def projects(self) -> list[dict]; def configure_project(...); def delete_project(...)
    def global_prompt(self); def save_global_prompt(text)
    def project_prompt(name); def save_project_prompt(name, sections)
    def doctor(self) -> dict               # 委托 hosts.doctor
    def connect(host); def disconnect(host)   # 委托 hosts
    def check_update(force); def apply_upgrade(...)
    def time(self) -> dict
```

启动包拼接顺序（bootstrap/session-start 注入）：全局提示词用户区 → 项目提示词 → 公共池 L1 → 附件卡片 → 接续视图前 5 条 → 当前时间。按宿主上限裁剪（字符预算：claude-code 9000、codex 8000、dsh 12000、pi 12000；超出时尾部提示“可调用 bootstrap/search 获取”）。

bootstrap 判断项目：`match_project(cwd)` 最长路径匹配；根项目默认不读子项目内容；根项目 `allow_child=true` 时接续视图附子项目进行中条目并标注来源。

save 操作（JSON 数组，每项一个 dict）：

```json
{"op":"append","section":"done|decision|dead_end|evidence|goal|user","items":[...]}
{"op":"replace","section":"target|next_step","text":"..."}
{"op":"set_status","status":"进行中|受阻|待验证|已完成|已放弃","complete_goals":["G1"],"supersede":["K1"]}
{"op":"env","items":[{...公共池卡片字段...}]}
{"op":"project_prompt","sections":{"最终目标":"...","硬规则":"...","当前阶段":"...","关键路径和命令":"..."}}
```

校验（部分接受）：字段名/枚举/长度/证据格式；不合格条目退回并附允许值与一条正确示例；同一操作重试超过 2 次（按 session 记录）→ 原文写“待整理”并标“未校验”。合格条目立即写入。自动补：时间、git 分支/HEAD/改动文件数、最近验证（若 `last_test` 或宿主 hook 记录存在）。同事务更新索引。返回：写入编号 + 当前时间 + 被拒条目 + 提醒。

## 11. mcp_server.py（Lead）

四个工具，会话身份显式传（P9）：

- `bootstrap(session_id, agent, cwd, host_record="", project="")`
- `search(query, session_id="", limit=5)`
- `save(session_id, operations, last_test=None)`
- `resume(session_id, target)`

全部返回带 `tbrain_version` 与 `current_time`。工具 description 里写入写作规范、填空模板与《项目提示词写作规范》全文（长 description 可接受）。

## 12. hosts/*（B）

```python
# hosts/base.py
@dataclass
class HostSpec:
    key: str                    # claude-code|codex|dsh|pi
    display: str
    config_paths: list[Path]
    hook_config_path: Path
    supports_context: bool

def normalize(host: str, raw: dict) -> HookEvent      # 宿主 hook 输入 → 中性事件
def render(host: str, result: HookResult) -> dict     # 中性结果 → 宿主 hook 输出
def connect(host, app_home, install_root, settings) -> dict     # 备份 + 只写 Tbrain 条目
def disconnect(host, app_home, install_root, settings) -> dict
def doctor(host, app_home, install_root, settings) -> dict
def doctor_all(...) -> dict
```

- Claude Code：写 `~/.claude/settings.json` 的 hooks（SessionStart/UserPromptSubmit/Stop/PreCompact/SessionEnd），`shell:"powershell"` 全引号命令指向固定入口；MCP 用 `claude mcp add` 或写 `~/.claude.json` 用户级条目（第二种更可控）——只增 Tbrain 条目，改前备份 `<file>.tbrain-<时间戳>.bak`。
- Codex：写 `~/.codex/hooks.json`（4~5 事件、command + commandWindows）与 `config.toml [mcp_servers.tbrain]`；`doctor` 读取 `[hooks.state.*]` 判断 trusted；黄色提示用户执行 `/hooks` 信任。写入后不自动写 trusted_hash。
- DSH：写 `~/.dsh/profiles/desktop/cordis.patch.yml`（insert `tbrain-mcp`、`tbrain-cc-hooks`(configPath=app_home/hooks.json)、`tbrain-native`(file URL 或包名)）；安装 bridge 包到 profile（若 `dsh plugin add` 不可用则写 package.json 依赖并提示用户安装，或直接 file 插件 + 依赖已有）。`doctor` 检查引擎版本、桥版本、插件条目、旧 shared-brain 条目。
- pi：写 `~/.pi/agent/settings.json` 的 `extensions` 或安装 `integrations/pi/tbrain.ts` 到 `~/.pi/agent/extensions/`；MCP 不需要。扩展通过 `tbrain hook --host pi`/`tbrain pi-tool <name>` 调用。
- 共用 hooks 文件：`app_home/hooks.json`（Claude 风格，固定入口命令），供 Claude/Codex/DSH 桥共用；内容稳定（升级不改）。

## 13. 版本、进程与升级（B）

固定入口布局：

```
%LOCALAPPDATA%\Programs\Tbrain\
  bin\tbrain.cmd            # 稳定入口：读取 current.txt → versions\<ver>\tbrain.exe %*
  current.txt               # 当前版本号（原子写）
  current\                  # 可选 junction → versions\<ver>（兼容直接引用 current 的配置）
  versions\0.6.0\...
  versions\0.6.1\...
```

- `installer.py`：`install(archive|dir)`, `stage`, `switch_current(version)`, `uninstall`, `prune_versions(keep=2)`, `rollback`。
- `proc.py`：`register(role, version)`, `unregister()`, `live_processes()`, `evict_old_versions(current_version, timeout=5)`, `watch_exit_requests()`（后台线程：发现 `processes/<pid>.exit` 或旧版本请求 → 优雅退出；被强制结束由对方 `taskkill`）。MCP/hook/desktop 进程启动即登记。
- `update.py`：GitHub Releases 检查（沿用 0.5.2 逻辑）+ `--from` 本地包；SHA256 校验；`download`/`verify`。
- `upgrade`：下载校验 → 解压 `versions/<new>` → 停旧进程 → 切 current → 跑 doctor → 保留最近 2 个版本。
- `doctor` 输出结构：`{"ok": bool, "items": [{"host": "claude-code|codex|dsh|pi|install|processes", "status": "green|yellow|red", "detail": "..."}]}`；Codex 未信任 → yellow；旧 shared-brain 残留/版本不一致/固定入口缺失 → red。

## 14. desktop.py API（Lead 实现，C 使用）

```
status() -> {version, ready, vault_path, settings, pending_update}
sessions(filter) -> [{name, project, status, agent, updated, next_step, path}]
session_detail(path) -> {name, project, status, markdown, path}
rename_session(path, name) -> {name, path}
global_prompt() -> {markdown}
save_global_prompt(markdown) -> {}
projects() -> [{name, paths, parent, allow_child, sessions}]
project_save(name, paths, parent, allow_child, rename_from) -> {}
project_delete(name) -> {}
project_prompt(name) -> {sections, user}
project_prompt_save(name, sections) -> {written, rejected}
settings() -> {settings, timezones}
save_settings(values) -> {settings, restart_required}
time() -> {utc, local, display, timezone, calibrated, source}
doctor() -> {ok, items}
connect(host) -> {ok, detail}
disconnect(host) -> {ok, detail}
update_check(force) -> {...}
update_apply(version="") -> {...}
open_document(path) -> {}
```

## 15. 测试与验收

- 单元测试：`tests/test_clock.py`、`test_naming.py`、`test_store.py`、`test_index.py`、`test_handoff.py`、`test_identity.py`、`test_hooks.py`、`test_save_validation.py`、`test_prompt.py`、`test_hosts*.py`、`test_installer.py`、`test_update.py`。
- 验收测试：`tests/acceptance/`（8 条场景，第 5 步）。
- 分工：A/B/C 各自写自己模块的测试；Lead 写流程/验收/集成测试。所有人先用 `python -m pytest -q` 跑全量再交付。
- 真实宿主端到端：构建分发包后，用隔离配置目录（Claude `CLAUDE_CONFIG_DIR`、Codex `CODEX_HOME`、pi `PI_CODING_AGENT_DIR`）先测 Claude/Codex/pi，允许少量真实模型花费；DSH 单独验证。
- 前端验收：桌面启动截图 + 三页可交互（Lead 用真实桌面程序检查）。

## 16. 本阶段之外

第 6 步（迁移/解绑）只做 dry-run 报告与实现；真实迁移、修改用户全局配置、停止旧进程等不可逆操作等用户确认后再执行。
