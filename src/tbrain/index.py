"""SQLite 索引与中文检索。

契约见 ``docs/ARCHITECTURE.md`` 第 6 节：

- ``app_home/index.sqlite3``，WAL，``busy_timeout=5000``（MCP / hook 多进程并发写）。
- 每次 ``connect()`` 新建连接（不跨线程共享），写入在单事务内提交。
- ``sessions_fts`` / ``public_fts`` 的 ``cn`` 列存“中文两两重叠切分”（数据库 -> 数据 据库），
  查询同样切分、OR 组合；ASCII/数字原样；单字中文查询退到 LIKE。
- 接续视图排序：状态优先级 + ``exp(-距今天数/τ)``（未完成 7 天、已完成 30 天）；
  检索结果按 bm25 + 状态优先级 + 新近度加权。
- ``schema_version`` 写 ``meta`` 表，方便以后迁移。
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from . import clock

DB_FILENAME = "index.sqlite3"
SCHEMA_VERSION = 1
BM25_CANDIDATES = 50

STATUS_PRIORITY = {
    "已中断": 0,
    "进行中": 1,
    "受阻": 2,
    "待验证": 3,
    "已完成": 4,
    "已放弃": 4,
}
UNFINISHED_STATUSES = ("已中断", "进行中", "受阻", "待验证")
TAU_UNFINISHED_DAYS = 7.0
TAU_FINISHED_DAYS = 30.0
HISTORY_DAYS = 90.0
STATUS_COUNT = 5  # 优先级 0..4

SESSION_FIELDS = (
    "project",
    "name",
    "path",
    "status",
    "agent",
    "cwd",
    "started_at",
    "updated_at",
    "last_activity_at",
    "next_step",
    "open_goals",
    "git_branch",
    "git_head",
    "git_dirty",
    "last_verify",
    "file_hash",
    "file_mtime",
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    mtime REAL,
    size INTEGER,
    hash TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    project TEXT,
    name TEXT,
    path TEXT,
    status TEXT,
    agent TEXT,
    cwd TEXT,
    started_at TEXT,
    updated_at TEXT,
    last_activity_at TEXT,
    next_step TEXT,
    open_goals INTEGER,
    git_branch TEXT,
    git_head TEXT,
    git_dirty INTEGER,
    last_verify TEXT,
    file_hash TEXT,
    file_mtime REAL
);

CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
    session_id UNINDEXED,
    project UNINDEXED,
    name,
    body,
    cn,
    tokenize = 'unicode61'
);

CREATE TABLE IF NOT EXISTS public_cards (
    id TEXT PRIMARY KEY,
    kind TEXT,
    name TEXT,
    state TEXT,
    path TEXT,
    body TEXT,
    updated_at TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS public_fts USING fts5(
    id UNINDEXED,
    kind UNINDEXED,
    name,
    body,
    cn,
    tokenize = 'unicode61'
);

CREATE TABLE IF NOT EXISTS session_links (
    host TEXT,
    host_session_id TEXT,
    session_id TEXT,
    cwd TEXT,
    first_message_hash TEXT,
    created_at TEXT,
    PRIMARY KEY (host, host_session_id)
);

CREATE TABLE IF NOT EXISTS access_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    kind TEXT,
    at TEXT
);

CREATE TABLE IF NOT EXISTS blocks (
    path TEXT,
    block TEXT,
    owner TEXT,
    fingerprint TEXT,
    updated_at TEXT,
    PRIMARY KEY (path, block)
);

CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_access_session ON access_log(session_id, at);
CREATE INDEX IF NOT EXISTS idx_session_links_lookup
    ON session_links(cwd, first_message_hash, created_at);
CREATE INDEX IF NOT EXISTS idx_public_cards_kind ON public_cards(kind);
"""


# ---------------------------------------------------------------------------
# 文本切分与时间工具


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x3400 <= code <= 0x4DBF  # CJK 扩展 A
        or 0x4E00 <= code <= 0x9FFF  # CJK 统一表意文字
        or 0xF900 <= code <= 0xFAFF  # CJK 兼容表意文字
        or 0x3040 <= code <= 0x30FF  # 日文假名（按汉字同样切分）
    )


def _char_class(ch: str) -> str:
    if _is_cjk(ch):
        return "cjk"
    if ch.isalnum() or ch == "_":
        return "word"
    return "sep"


def _runs(text: str) -> Iterator[tuple[str, str]]:
    """把文本切成连续的中文段 / ASCII 词段 / 分隔符段。"""
    current = ""
    current_class = ""
    for ch in text:
        kind = _char_class(ch)
        if kind == "sep":
            if current and current_class != "sep":
                yield current, current_class
            current = ""
            current_class = ""
            continue
        if kind != current_class:
            if current:
                yield current, current_class
            current = ch
            current_class = kind
        else:
            current += ch
    if current:
        yield current, current_class


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _normalize_path(value: str) -> str:
    import os

    text = str(value or "").strip().replace("/", os.sep)
    if not text:
        return ""
    return os.path.normcase(os.path.normpath(text))


def _now_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_ts(text: str) -> datetime | None:
    if not text:
        return None
    try:
        return clock.parse_utc(str(text))
    except (ValueError, TypeError):
        return None


def _days_since(updated_at: str, now: datetime) -> float:
    parsed = _parse_ts(updated_at)
    if parsed is None:
        return 365.0
    delta = (_now_aware(now) - parsed).total_seconds() / 86400.0
    return max(0.0, delta)


def status_priority(status: str) -> int:
    return STATUS_PRIORITY.get((status or "").strip(), 3)


def tau_days(status: str) -> float:
    return TAU_FINISHED_DAYS if (status or "").strip() in ("已完成", "已放弃") else TAU_UNFINISHED_DAYS


def recency(updated_at: str, status: str, now: datetime) -> float:
    """``exp(-距上次更新天数 / τ)``，未完成 τ=7、已完成/已放弃 τ=30。"""
    days = _days_since(updated_at, now)
    import math

    return math.exp(-days / tau_days(status))


def is_finished(status: str) -> bool:
    return (status or "").strip() in ("已完成", "已放弃")


def _minmax(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    low = min(values.values())
    high = max(values.values())
    if high - low <= 1e-12:
        return {key: 1.0 for key in values}
    return {key: (value - low) / (high - low) for key, value in values.items()}


# ---------------------------------------------------------------------------
# Index


class Index:
    """SQLite 索引。每次操作新建连接，天然适配多进程并发写。"""

    def __init__(self, app_home: Path, *, now_fn=None):
        self.app_home = Path(app_home)
        self.path = self.app_home / DB_FILENAME
        self._now_fn = now_fn
        self._schema_ready = False
        self._schema_lock = threading.Lock()

    # -- 基础 ---------------------------------------------------------------

    def _now(self) -> datetime:
        if self._now_fn is not None:
            value = self._now_fn()
            if isinstance(value, datetime):
                return _now_aware(value)
            return datetime.fromtimestamp(float(value), timezone.utc)
        return clock.utc_now(self.app_home)

    def _stamp(self) -> str:
        return clock.iso_utc(self._now())

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """打开连接（busy_timeout=5000，WAL 只在初始化时设一次），成功提交、失败回滚。

        多进程首次启动时，设置 WAL / 建表可能撞上 ``database is locked``。这里对
        “连接 + 初始化 schema”整体做 5 次短退避重试；``busy_timeout`` 先于 WAL 设置，
        等 schema 就绪后不再在每次连接时重复 ``PRAGMA journal_mode=WAL``。
        """
        self.app_home.mkdir(parents=True, exist_ok=True)
        conn: sqlite3.Connection | None = None
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(5):
            try:
                conn = sqlite3.connect(str(self.path), timeout=5.0)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("PRAGMA synchronous=NORMAL")
                if not self._schema_ready:
                    with self._schema_lock:
                        if not self._schema_ready:
                            self._ensure_schema(conn)
                            self._schema_ready = True
                break
            except sqlite3.OperationalError as exc:
                if conn is not None:
                    try:
                        conn.close()
                    except sqlite3.Error:
                        pass
                conn = None
                last_error = exc
                message = str(exc).lower()
                if ("locked" not in message and "busy" not in message) or attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
        if conn is None:
            raise last_error or sqlite3.OperationalError("无法打开索引数据库")
        try:
            yield conn
            conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def ensure_schema(self) -> None:
        with self.connect():
            pass

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        # WAL 是持久设置，只在首次初始化时设置；已是 wal 则跳过，避免并发锁竞争。
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()
            current = str(mode[0]).lower() if mode is not None else ""
        except sqlite3.OperationalError:
            current = ""
        if current != "wal":
            conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA_SQL)
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is None:
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            return
        try:
            version = int(row[0])
        except (TypeError, ValueError):
            version = 0
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"索引数据库版本 {version} 高于程序支持的 {SCHEMA_VERSION}，请升级 Tbrain"
            )
        if version < SCHEMA_VERSION:
            self._migrate(conn, version)

    def _migrate(self, conn: sqlite3.Connection, from_version: int) -> None:
        """预留迁移入口；当前只有 1 版。"""
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )

    # -- meta ---------------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                (str(key), str(value)),
            )

    def get_meta(self, key: str, default: str = "") -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (str(key),)).fetchone()
        return str(row["value"]) if row is not None else default

    # -- 中文切分 -----------------------------------------------------------

    def bigram_terms(self, text: str) -> list[str]:
        """中文连续串两两重叠切分；ASCII/数字原样（小写）；去重保序。"""
        terms: list[str] = []
        seen: set[str] = set()
        for chunk, kind in _runs(str(text or "")):
            produced: list[str] = []
            if kind == "cjk":
                if len(chunk) == 1:
                    produced.append(chunk)
                else:
                    produced.extend(chunk[i:i + 2] for i in range(len(chunk) - 1))
            elif kind == "word":
                produced.append(chunk.lower())
            for term in produced:
                if term and term not in seen:
                    seen.add(term)
                    terms.append(term)
        return terms

    def _query_terms(self, query: str) -> list[str]:
        terms = self.bigram_terms(query)
        if len(terms) <= 1:
            return terms
        multi = [term for term in terms if len(term) >= 2 or not _is_cjk(term[0])]
        return multi or terms

    @staticmethod
    def _match_expression(terms: Sequence[str]) -> str:
        return " OR ".join('"' + str(term).replace('"', '""') + '"' for term in terms if term)

    @staticmethod
    def _single_cjk_query(query: str) -> str:
        compact = re.sub(r"\s+", "", str(query or ""))
        compact = compact.strip("，。！？、；：,.!?;:~～…—-_·•")
        if len(compact) == 1 and _is_cjk(compact):
            return compact
        return ""

    # -- 写入：会话 ---------------------------------------------------------

    def upsert_session(self, row: dict, body: str = "") -> None:
        session_id = str(row.get("session_id") or "").strip()
        if not session_id:
            raise ValueError("session_id 不能为空")
        values: dict[str, Any] = {}
        for field in SESSION_FIELDS:
            value = row.get(field, "" if field not in ("open_goals", "git_dirty", "file_mtime") else None)
            if field in ("open_goals", "git_dirty"):
                value = int(value or 0)
            elif field == "file_mtime":
                value = float(value) if value not in (None, "") else None
            elif value is None:
                value = ""
            values[field] = value
        text_body = str(body or "")
        name = str(values["name"] or "")
        project = str(values["project"] or "")
        cn = " ".join(self.bigram_terms(f"{name}\n{text_body}"))
        columns = ["session_id", *SESSION_FIELDS]
        placeholders = ", ".join("?" for _ in columns)
        assignments = ", ".join(f"{field} = excluded.{field}" for field in SESSION_FIELDS)
        with self.connect() as conn:
            conn.execute(
                f"INSERT INTO sessions({', '.join(columns)}) VALUES({placeholders}) "
                f"ON CONFLICT(session_id) DO UPDATE SET {assignments}",
                [session_id, *[values[field] for field in SESSION_FIELDS]],
            )
            conn.execute("DELETE FROM sessions_fts WHERE session_id = ?", (session_id,))
            conn.execute(
                "INSERT INTO sessions_fts(session_id, project, name, body, cn) VALUES(?, ?, ?, ?, ?)",
                (session_id, project, name, text_body, cn),
            )

    def remove_session(self, session_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (str(session_id),))
            conn.execute("DELETE FROM sessions_fts WHERE session_id = ?", (str(session_id),))

    def session(self, session_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (str(session_id),)
            ).fetchone()
        return dict(row) if row is not None else None

    def sessions(
        self,
        project: str | None = None,
        status: list[str] | None = None,
    ) -> list[dict]:
        where: list[str] = []
        args: list[Any] = []
        if project is not None:
            if isinstance(project, (list, tuple, set)):
                names = [str(item) for item in project]
                if not names:
                    return []
                where.append("project IN (" + ", ".join("?" for _ in names) + ")")
                args.extend(names)
            else:
                where.append("project = ?")
                args.append(str(project))
        if status:
            values = [str(item) for item in status]
            where.append("status IN (" + ", ".join("?" for _ in values) + ")")
            args.extend(values)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        sql = (
            "SELECT * FROM sessions"
            + clause
            + " ORDER BY COALESCE(NULLIF(updated_at, ''), last_activity_at, started_at) DESC,"
            " session_id ASC"
        )
        with self.connect() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [dict(row) for row in rows]

    # -- 检索内部 -----------------------------------------------------------

    def _fts_session_hits(
        self,
        conn: sqlite3.Connection,
        query: str,
        projects: Sequence[str] | None = None,
        limit: int = BM25_CANDIDATES,
    ) -> list[tuple[str, float]]:
        terms = self._query_terms(query)
        if not terms:
            return []
        sql = (
            "SELECT session_id, bm25(sessions_fts, 0.0, 0.0, 2.0, 1.0, 1.5) AS rank "
            "FROM sessions_fts WHERE sessions_fts MATCH ?"
        )
        args: list[Any] = [self._match_expression(terms)]
        if projects is not None:
            names = [str(item) for item in projects]
            if not names:
                return []
            sql += " AND project IN (" + ", ".join("?" for _ in names) + ")"
            args.extend(names)
        sql += " ORDER BY rank LIMIT ?"
        args.append(int(limit))
        try:
            rows = conn.execute(sql, args).fetchall()
        except sqlite3.OperationalError:
            return []
        return [(str(row["session_id"]), float(row["rank"])) for row in rows]

    def _like_session_ids(
        self,
        conn: sqlite3.Connection,
        char: str,
        projects: Sequence[str] | None = None,
    ) -> list[str]:
        pattern = f"%{_like_escape(char)}%"
        sql = (
            "SELECT session_id FROM sessions_fts "
            "WHERE (name LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')"
        )
        args: list[Any] = [pattern, pattern]
        if projects is not None:
            names = [str(item) for item in projects]
            if not names:
                return []
            sql += " AND project IN (" + ", ".join("?" for _ in names) + ")"
            args.extend(names)
        try:
            rows = conn.execute(sql, args).fetchall()
        except sqlite3.OperationalError:
            return []
        return [str(row["session_id"]) for row in rows]

    @staticmethod
    def _sessions_by_ids(conn: sqlite3.Connection, ids: Iterable[str]) -> dict[str, dict]:
        values = [str(item) for item in ids]
        if not values:
            return {}
        placeholders = ", ".join("?" for _ in values)
        rows = conn.execute(
            f"SELECT * FROM sessions WHERE session_id IN ({placeholders})", values
        ).fetchall()
        return {str(row["session_id"]): dict(row) for row in rows}

    @staticmethod
    def _composite_score(row: dict, relevance: float, now: datetime) -> float:
        status = str(row.get("status") or "")
        updated = str(row.get("updated_at") or row.get("last_activity_at") or "")
        priority = status_priority(status)
        status_score = 1.0 - priority / STATUS_COUNT
        score = 0.6 * relevance + 0.25 * recency(updated, status, now) + 0.15 * status_score
        if status in UNFINISHED_STATUSES:
            score += 0.1
        return score

    # -- 检索：会话 ---------------------------------------------------------

    def search_sessions(
        self,
        query: str,
        projects: list[str] | None = None,
        limit: int = 5,
    ) -> list[dict]:
        query = str(query or "").strip()
        if not query or limit <= 0:
            return []
        now = self._now()
        with self.connect() as conn:
            single = self._single_cjk_query(query)
            raw_rel: dict[str, float] = {}
            if single:
                ids = self._like_session_ids(conn, single, projects)
                rows = self._sessions_by_ids(conn, ids)
                raw_rel = {session_id: 1.0 for session_id in rows}
            else:
                hits = self._fts_session_hits(conn, query, projects=projects)
                ids = [session_id for session_id, _rank in hits]
                rows = self._sessions_by_ids(conn, ids)
                raw_rel = {
                    session_id: float(-rank)  # bm25 越小越好，取负后越大越好
                    for session_id, rank in hits
                    if session_id in rows
                }
        if not rows:
            return []
        normalized = _minmax(raw_rel)
        scored: list[tuple[float, dict]] = []
        for session_id, row in rows.items():
            relevance = normalized.get(session_id, 0.0)
            score = self._composite_score(row, relevance, now)
            enriched = dict(row)
            enriched["score"] = round(score, 6)
            enriched["relevance"] = round(relevance, 6)
            scored.append((score, enriched))
        scored.sort(key=lambda item: (-item[0], str(item[1].get("session_id") or "")))
        return [item[1] for item in scored[: int(limit)]]

    def resume_view(
        self,
        project: str,
        child_projects: list[str] | None = None,
        limit: int = 5,
        related_query: str = "",
        *,
        cwd: str = "",
    ) -> list[dict]:
        """接续视图：状态优先级 > 新近度；可选按原命令相关度先筛。"""
        now = self._now()
        projects: list[str] | None
        if project is None:
            projects = None
        else:
            projects = [str(project)] + [str(item) for item in (child_projects or [])]
        with self.connect() as conn:
            rows = self._sessions_for_projects(conn, projects)
            relevance_order: dict[str, int] = {}
            if str(related_query or "").strip():
                single = self._single_cjk_query(related_query)
                if single:
                    hits = [(session_id, 0.0) for session_id in self._like_session_ids(conn, single, projects)]
                else:
                    hits = self._fts_session_hits(conn, related_query, projects=projects)
                matched = {session_id for session_id, _rank in hits}
                if matched:
                    rows = [row for row in rows if str(row["session_id"]) in matched]
                    relevance_order = {
                        str(session_id): index for index, (session_id, _rank) in enumerate(hits)
                    }
        candidates: list[tuple[tuple, dict]] = []
        for row in rows:
            status = str(row.get("status") or "")
            if is_finished(status) and _days_since(
                str(row.get("updated_at") or row.get("last_activity_at") or ""), now
            ) > HISTORY_DAYS:
                continue  # 完成超过 90 天：移入历史段，默认不加载
            priority = status_priority(status)
            recent = recency(
                str(row.get("updated_at") or row.get("last_activity_at") or ""), status, now
            )
            cwd_rank = 0
            if cwd:
                cwd_rank = 0 if _normalize_path(str(row.get("cwd") or "")) == _normalize_path(cwd) else 1
            rel_rank = relevance_order.get(str(row["session_id"]), 10**9)
            enriched = dict(row)
            enriched["priority"] = priority
            enriched["recency"] = round(recent, 6)
            key = (
                cwd_rank,
                priority,
                -recent,
                rel_rank,
                str(row.get("name") or ""),
                str(row["session_id"]),
            )
            candidates.append((key, enriched))
        candidates.sort(key=lambda item: item[0])
        return [item[1] for item in candidates[: int(limit)]]

    def _sessions_for_projects(
        self,
        conn: sqlite3.Connection,
        projects: Sequence[str] | None,
    ) -> list[dict]:
        sql = "SELECT * FROM sessions"
        args: list[Any] = []
        if projects is not None:
            names = [str(item) for item in projects]
            if not names:
                return []
            sql += " WHERE project IN (" + ", ".join("?" for _ in names) + ")"
            args.extend(names)
        rows = conn.execute(sql, args).fetchall()
        return [dict(row) for row in rows]

    # -- 公共池 -------------------------------------------------------------

    def upsert_public(self, row: dict, body: str = "") -> None:
        card_id = str(row.get("id") or "").strip()
        if not card_id:
            raise ValueError("公共池卡片 id 不能为空")
        kind = str(row.get("kind") or "")
        name = str(row.get("name") or "")
        state = str(row.get("state") or "生效")
        path = str(row.get("path") or "")
        updated_at = str(row.get("updated_at") or self._stamp())
        text_body = str(body or "")
        cn = " ".join(self.bigram_terms(f"{kind}\n{name}\n{text_body}"))
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO public_cards(id, kind, name, state, path, body, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET kind = excluded.kind, name = excluded.name, "
                "state = excluded.state, path = excluded.path, body = excluded.body, "
                "updated_at = excluded.updated_at",
                (card_id, kind, name, state, path, text_body, updated_at),
            )
            conn.execute("DELETE FROM public_fts WHERE id = ?", (card_id,))
            conn.execute(
                "INSERT INTO public_fts(id, kind, name, body, cn) VALUES(?, ?, ?, ?, ?)",
                (card_id, kind, name, text_body, cn),
            )

    def search_public(self, query: str, limit: int = 5) -> list[dict]:
        query = str(query or "").strip()
        if not query or limit <= 0:
            return []
        now = self._now()
        with self.connect() as conn:
            single = self._single_cjk_query(query)
            raw_rel: dict[str, float] = {}
            if single:
                pattern = f"%{_like_escape(single)}%"
                ids = [
                    str(row["id"])
                    for row in conn.execute(
                        "SELECT id FROM public_fts "
                        "WHERE name LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\' LIMIT ?",
                        (pattern, pattern, BM25_CANDIDATES),
                    ).fetchall()
                ]
                rows = self._public_by_ids(conn, ids)
                raw_rel = {card_id: 1.0 for card_id in rows}
            else:
                terms = self._query_terms(query)
                if not terms:
                    return []
                try:
                    hits = conn.execute(
                        "SELECT id, bm25(public_fts, 0.0, 0.0, 2.0, 1.0, 1.5) AS rank "
                        "FROM public_fts WHERE public_fts MATCH ? ORDER BY rank LIMIT ?",
                        (self._match_expression(terms), BM25_CANDIDATES),
                    ).fetchall()
                except sqlite3.OperationalError:
                    return []
                ids = [str(row["id"]) for row in hits]
                rows = self._public_by_ids(conn, ids)
                raw_rel = {
                    str(row["id"]): float(-row["rank"])
                    for row in hits
                    if str(row["id"]) in rows
                }
        if not rows:
            return []
        normalized = _minmax(raw_rel)
        scored: list[tuple[float, dict]] = []
        for card_id, row in rows.items():
            relevance = normalized.get(card_id, 0.0)
            active = 1.0 if str(row.get("state") or "") == "生效" else 0.0
            score = (
                0.6 * relevance
                + 0.25 * recency(str(row.get("updated_at") or ""), "已完成", now)
                + 0.15 * active
            )
            enriched = dict(row)
            enriched["score"] = round(score, 6)
            enriched["relevance"] = round(relevance, 6)
            scored.append((score, enriched))
        scored.sort(key=lambda item: (-item[0], str(item[1].get("id") or "")))
        return [item[1] for item in scored[: int(limit)]]

    @staticmethod
    def _public_by_ids(conn: sqlite3.Connection, ids: Iterable[str]) -> dict[str, dict]:
        values = [str(item) for item in ids]
        if not values:
            return {}
        placeholders = ", ".join("?" for _ in values)
        rows = conn.execute(
            f"SELECT * FROM public_cards WHERE id IN ({placeholders})", values
        ).fetchall()
        return {str(row["id"]): dict(row) for row in rows}

    # -- 访问日志 -----------------------------------------------------------

    def log_access(self, session_id: str, kind: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO access_log(session_id, kind, at) VALUES(?, ?, ?)",
                (str(session_id), str(kind), self._stamp()),
            )

    def access_history(self, session_id: str, limit: int = 10) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT kind FROM access_log WHERE session_id = ? "
                "ORDER BY at DESC, id DESC LIMIT ?",
                (str(session_id), int(limit)),
            ).fetchall()
        return [str(row["kind"]) for row in rows]

    # -- 文件扫描状态 -------------------------------------------------------

    def mark_scan(self, path: str, mtime: float, size: int, hash: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO files(path, mtime, size, hash) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET mtime = excluded.mtime, "
                "size = excluded.size, hash = excluded.hash",
                (str(path), float(mtime), int(size), str(hash or "")),
            )

    def file_state(self, path: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM files WHERE path = ?", (str(path),)).fetchone()
        return dict(row) if row is not None else None

    def file_changed(
        self,
        path: str,
        mtime: float,
        size: int,
        hash: str | None = None,
    ) -> bool:
        """按 mtime + size（以及可选 hash）判断文件是否需要重新入库。"""
        state = self.file_state(path)
        if state is None:
            return True
        try:
            same_mtime = abs(float(state.get("mtime") or 0.0) - float(mtime)) < 1e-6
            same_size = int(state.get("size") or 0) == int(size)
        except (TypeError, ValueError):
            return True
        if not (same_mtime and same_size):
            return True
        if hash is not None and str(state.get("hash") or "") != str(hash):
            return True
        return False

    # -- 区块所有权状态 -----------------------------------------------------

    def block_state(self, path: str, block: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM blocks WHERE path = ? AND block = ?", (str(path), str(block))
            ).fetchone()
        return dict(row) if row is not None else None

    def set_block_state(self, path: str, block: str, owner: str, fingerprint: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO blocks(path, block, owner, fingerprint, updated_at) "
                "VALUES(?, ?, ?, ?, ?) "
                "ON CONFLICT(path, block) DO UPDATE SET owner = excluded.owner, "
                "fingerprint = excluded.fingerprint, updated_at = excluded.updated_at",
                (str(path), str(block), str(owner), str(fingerprint), self._stamp()),
            )

    def blocks_for_path(self, path: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM blocks WHERE path = ? ORDER BY block", (str(path),)
            ).fetchall()
        return [dict(row) for row in rows]

    def move_block_path(self, old_path: str, new_path: str) -> None:
        """文件改名后把 blocks 表的 path 主键一起搬过去。"""
        old_path = str(old_path)
        new_path = str(new_path)
        if old_path == new_path:
            return
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM blocks WHERE path = ?", (old_path,)
            ).fetchall()
            for row in rows:
                exists = conn.execute(
                    "SELECT 1 FROM blocks WHERE path = ? AND block = ?",
                    (new_path, row["block"]),
                ).fetchone()
                if exists is None:
                    conn.execute(
                        "INSERT INTO blocks(path, block, owner, fingerprint, updated_at) "
                        "VALUES(?, ?, ?, ?, ?)",
                        (
                            new_path,
                            row["block"],
                            row["owner"],
                            row["fingerprint"],
                            row["updated_at"],
                        ),
                    )
            conn.execute("DELETE FROM blocks WHERE path = ?", (old_path,))

    # -- 会话身份映射 -------------------------------------------------------

    def register_link(
        self,
        host: str,
        host_session_id: str,
        session_id: str,
        cwd: str = "",
        first_message_hash: str = "",
        created_at: str = "",
    ) -> None:
        """记录 (宿主, 宿主会话 ID) -> Tbrain session_id；同键 REPLACE。"""
        stamp = str(created_at or "") or self._stamp()
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO session_links(host, host_session_id, session_id, cwd, "
                "first_message_hash, created_at) VALUES(?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(host, host_session_id) DO UPDATE SET "
                "session_id = excluded.session_id, cwd = excluded.cwd, "
                "first_message_hash = excluded.first_message_hash, "
                "created_at = excluded.created_at",
                (
                    str(host),
                    str(host_session_id),
                    str(session_id),
                    str(cwd or ""),
                    str(first_message_hash or ""),
                    stamp,
                ),
            )

    def set_session_link(
        self,
        host: str,
        host_session_id: str,
        session_id: str,
        cwd: str = "",
        first_message_hash: str = "",
    ) -> None:
        """便捷写法：created_at 用当前时间（固定 now_fn）填，保持可排序。"""
        self.register_link(
            host, host_session_id, session_id, cwd, first_message_hash, self._stamp()
        )

    def get_session_link(self, host: str, host_session_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM session_links WHERE host = ? AND host_session_id = ?",
                (str(host), str(host_session_id)),
            ).fetchone()
        return dict(row) if row is not None else None

    def find_link_by_cwd_and_message(self, cwd: str, first_message_hash: str) -> str:
        """同 cwd 且“原始命令 == 首条消息”完全相同 -> 复用原 session_id（取最新）。"""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT session_id FROM session_links "
                "WHERE cwd = ? AND first_message_hash = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (str(cwd or ""), str(first_message_hash or "")),
            ).fetchone()
        return str(row["session_id"]) if row is not None and row["session_id"] else ""

    def links_for_session(self, session_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM session_links WHERE session_id = ? ORDER BY created_at DESC",
                (str(session_id),),
            ).fetchall()
        return [dict(row) for row in rows]
