"""index.py 测试：SQLite/WAL 并发、FTS5 中文二字切分召回、排序、日志、映射表。

时间全部由固定 ``now_fn`` 注入，不依赖真实时钟。
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from tbrain import clock
from tbrain.index import Index

NOW = datetime(2026, 10, 24, 13, 0, 0, tzinfo=timezone.utc)
STAMP = "2026-10-24T13:00:00Z"


def iso_days_ago(days: float) -> str:
    return clock.iso_utc(NOW - timedelta(days=days))


@pytest.fixture()
def index(tmp_path) -> Index:
    value = Index(tmp_path / "home", now_fn=lambda: NOW)
    value.ensure_schema()
    return value


def add_session(index: Index, session_id: str, body: str, *, project: str = "P1",
                name: str = "", status: str = "进行中", updated_at: str = STAMP,
                agent: str = "claude-code", path: str = "", **extra) -> None:
    index.upsert_session(
        {
            "session_id": session_id,
            "project": project,
            "name": name or session_id,
            "path": path or f"会话总结/{session_id}.md",
            "status": status,
            "agent": agent,
            "cwd": extra.get("cwd", r"D:\work\p1"),
            "updated_at": updated_at,
            "last_activity_at": extra.get("last_activity_at", updated_at),
            "next_step": extra.get("next_step", "写测试"),
            "open_goals": extra.get("open_goals", 0),
        },
        body,
    )


# ---------------------------------------------------------------------------
# schema / WAL / meta


def test_schema_meta_and_wal_pragmas(index: Index):
    assert index.get_meta("schema_version") == "1"
    with index.connect() as conn:
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            ).fetchall()
        }
    assert journal == "wal"
    assert busy == 5000
    for table in (
        "meta",
        "files",
        "sessions",
        "sessions_fts",
        "public_cards",
        "public_fts",
        "session_links",
        "access_log",
        "blocks",
    ):
        assert table in tables


def test_meta_set_and_get(index: Index):
    index.set_meta("vault_path", r"D:\TbrainVault")
    assert index.get_meta("vault_path") == r"D:\TbrainVault"
    assert index.get_meta("missing", "默认值") == "默认值"


# ---------------------------------------------------------------------------
# 中文两两重叠切分


def test_bigram_terms_splits_chinese_into_overlapping_pairs(index: Index):
    assert index.bigram_terms("数据库") == ["数据", "据库"]
    assert index.bigram_terms("连接") == ["连接"]
    terms = index.bigram_terms("数据库 abc123 WAL")
    for expected in ("数据", "据库", "abc123", "wal"):
        assert expected in terms
    assert index.bigram_terms("") == []
    assert index.bigram_terms("数") == ["数"]


def test_bigram_terms_dedupes_in_order(index: Index):
    assert index.bigram_terms("数据数据") == ["数据", "据数"]
    assert index.bigram_terms("ab ab") == ["ab"]


# ---------------------------------------------------------------------------
# 会话写入、查询与中文召回


def test_upsert_session_read_and_remove(index: Index):
    add_session(index, "claude-code:1", "正文", name="会话一", open_goals=2)
    row = index.session("claude-code:1")
    assert row["name"] == "会话一"
    assert row["project"] == "P1"
    assert row["open_goals"] == 2
    assert index.session("missing") is None

    add_session(index, "claude-code:2", "正文", project="P2", status="已完成")
    assert [item["session_id"] for item in index.sessions("P1")] == ["claude-code:1"]
    assert {item["session_id"] for item in index.sessions()} == {"claude-code:1", "claude-code:2"}
    assert [item["session_id"] for item in index.sessions(status=["进行中"])] == ["claude-code:1"]

    index.upsert_session(
        {
            "session_id": "claude-code:1",
            "project": "P1",
            "name": "会话一改名",
            "path": "x.md",
            "status": "受阻",
            "updated_at": STAMP,
        },
        "改名后的独特内容",
    )
    renamed = index.session("claude-code:1")
    assert renamed["name"] == "会话一改名"
    assert renamed["status"] == "受阻"

    index.remove_session("claude-code:1")
    assert index.session("claude-code:1") is None
    assert index.search_sessions("独特", None) == []


def test_chinese_two_char_recall(index: Index):
    add_session(index, "s1", "实现数据库连接的原子写入，索引排序稳定。", name="数据库连接")
    add_session(index, "s2", "命名规则与文件清洗。", name="命名规则")
    assert [item["session_id"] for item in index.search_sessions("数据库", None)] == ["s1"]
    assert [item["session_id"] for item in index.search_sessions("据库", None)] == ["s1"]
    assert [item["session_id"] for item in index.search_sessions("数据", None)] == ["s1"]
    assert [item["session_id"] for item in index.search_sessions("命名", None)] == ["s2"]


def test_single_chinese_character_falls_back_to_like(index: Index):
    add_session(index, "s1", "实现数据库连接的原子写入", name="数据库")
    add_session(index, "s2", "命名规则", name="命名")
    assert [item["session_id"] for item in index.search_sessions("据", None)] == ["s1"]
    assert [item["session_id"] for item in index.search_sessions("名", None)] == ["s2"]
    assert index.search_sessions("异", None) == []


def test_ascii_query_is_case_insensitive(index: Index):
    add_session(index, "s1", "开启 WAL 模式并设置 busy_timeout。", name="索引")
    add_session(index, "s2", "无关内容。", name="其他")
    assert [item["session_id"] for item in index.search_sessions("WAL", None)] == ["s1"]
    assert [item["session_id"] for item in index.search_sessions("wal", None)] == ["s1"]


def test_search_respects_project_scope(index: Index):
    add_session(index, "s1", "数据库索引", project="P1")
    add_session(index, "s2", "数据库索引", project="P2")
    add_session(index, "s3", "数据库索引", project="")
    assert [item["session_id"] for item in index.search_sessions("数据库", ["P1"])] == ["s1"]
    assert [item["session_id"] for item in index.search_sessions("数据库", [""])] == ["s3"]
    assert {item["session_id"] for item in index.search_sessions("数据库", None)} == {
        "s1",
        "s2",
        "s3",
    }
    assert index.search_sessions("数据库", []) == []


def test_search_returns_contract_fields_and_limit(index: Index):
    for number in range(8):
        add_session(index, f"s{number}", f"数据库索引 第 {number} 条", name=f"会话{number}")
    rows = index.search_sessions("数据库", None, 5)
    assert len(rows) == 5
    for row in rows[:1]:
        for key in ("path", "updated_at", "project", "status", "name", "session_id"):
            assert key in row


def test_empty_and_blank_query_return_nothing(index: Index):
    add_session(index, "s1", "内容")
    assert index.search_sessions("", None, 5) == []
    assert index.search_sessions("   ", None, 5) == []


# ---------------------------------------------------------------------------
# resume 视图排序


def test_resume_view_status_priority_order(index: Index):
    add_session(index, "interrupted", "", status="已中断", updated_at=iso_days_ago(20))
    add_session(index, "running", "", status="进行中", updated_at=iso_days_ago(10))
    add_session(index, "blocked", "", status="受阻", updated_at=STAMP)
    add_session(index, "verify", "", status="待验证", updated_at=STAMP)
    add_session(index, "done", "", status="已完成", updated_at=STAMP)
    rows = index.resume_view("P1", [], limit=5)
    assert [row["session_id"] for row in rows] == [
        "interrupted",
        "running",
        "blocked",
        "verify",
        "done",
    ]
    assert [row["priority"] for row in rows] == [0, 1, 2, 3, 4]


def test_resume_view_recency_within_same_status(index: Index):
    add_session(index, "old", "", status="进行中", updated_at=iso_days_ago(10))
    add_session(index, "new", "", status="进行中", updated_at=iso_days_ago(1))
    add_session(index, "middle", "", status="进行中", updated_at=iso_days_ago(5))
    rows = index.resume_view("P1", [])
    assert [row["session_id"] for row in rows] == ["new", "middle", "old"]
    assert rows[0]["recency"] > rows[1]["recency"] > rows[2]["recency"]


def test_resume_view_excludes_history_beyond_90_days(index: Index):
    add_session(index, "recent_done", "", status="已完成", updated_at=iso_days_ago(5))
    add_session(index, "ancient_done", "", status="已完成", updated_at=iso_days_ago(100))
    rows = index.resume_view("P1", [])
    assert [row["session_id"] for row in rows] == ["recent_done"]


def test_resume_view_related_query_filters_then_sorts(index: Index):
    add_session(index, "db", "数据库索引的原子写入", name="数据库", updated_at=iso_days_ago(3))
    add_session(index, "naming", "命名规则与文件清洗", name="命名", updated_at=iso_days_ago(1))
    rows = index.resume_view("P1", [], related_query="数据库")
    assert [row["session_id"] for row in rows] == ["db"]
    # 无命中时退回全部（避免接续视图因为原命令不相关而空掉）
    rows = index.resume_view("P1", [], related_query="量子计算完全无关")
    assert {row["session_id"] for row in rows} == {"db", "naming"}


def test_resume_view_child_project_filter(index: Index):
    add_session(index, "root", "", project="Root", status="进行中")
    add_session(index, "child", "", project="Child", status="已中断")
    add_session(index, "other", "", project="Other", status="进行中")
    without_child = index.resume_view("Root", [])
    assert [row["session_id"] for row in without_child] == ["root"]
    with_child = index.resume_view("Root", ["Child"])
    assert [row["session_id"] for row in with_child] == ["child", "root"]  # 已中断优先级更高
    assert {row["session_id"] for row in with_child} == {"root", "child"}


def test_resume_view_independent_pool_and_cwd_priority(index: Index):
    add_session(index, "same", "", project="", status="进行中", updated_at=iso_days_ago(5),
                cwd=r"D:\work\a")
    add_session(index, "other", "", project="", status="进行中", updated_at=iso_days_ago(1),
                cwd=r"D:\work\b")
    rows = index.resume_view("", [], cwd=r"D:\WORK\A".lower())
    assert [row["session_id"] for row in rows] == ["same", "other"]
    rows = index.resume_view("", [])
    assert [row["session_id"] for row in rows] == ["other", "same"]


# ---------------------------------------------------------------------------
# 访问日志 / 文件扫描状态 / blocks / 身份映射


def test_access_log_records_and_reads_back(index: Index):
    index.log_access("s1", "bootstrap")
    index.log_access("s1", "save")
    index.log_access("s1", "resume")
    index.log_access("s2", "bootstrap")
    assert index.access_history("s1") == ["resume", "save", "bootstrap"]
    assert index.access_history("s1", limit=2) == ["resume", "save"]
    assert index.access_history("missing") == []


def test_files_scan_state_and_change_detection(index: Index):
    assert index.file_state("会话总结/a.md") is None
    index.mark_scan("会话总结/a.md", 100.0, 2048, "hash-a")
    state = index.file_state("会话总结/a.md")
    assert state["mtime"] == pytest.approx(100.0)
    assert state["size"] == 2048
    assert state["hash"] == "hash-a"
    assert index.file_changed("会话总结/a.md", 100.0, 2048, "hash-a") is False
    assert index.file_changed("会话总结/a.md", 101.0, 2048) is True
    assert index.file_changed("会话总结/a.md", 100.0, 2049) is True
    assert index.file_changed("会话总结/a.md", 100.0, 2048, "hash-b") is True
    assert index.file_changed("会话总结/new.md", 1.0, 1) is True
    index.mark_scan("会话总结/a.md", 102.0, 4096, "hash-b")
    assert index.file_state("会话总结/a.md")["size"] == 4096


def test_block_state_read_write_and_move(index: Index):
    index.set_block_state("项目/P1/项目提示词.md", "model", "model", "fp1")
    state = index.block_state("项目/P1/项目提示词.md", "model")
    assert state["owner"] == "model"
    assert state["fingerprint"] == "fp1"
    assert index.block_state("项目/P1/项目提示词.md", "user") is None
    index.set_block_state("项目/P1/项目提示词.md", "model", "user", "fp2")
    assert index.block_state("项目/P1/项目提示词.md", "model")["owner"] == "user"

    index.set_block_state("旧路径.md", "model", "user", "fp-hand")
    index.set_block_state("旧路径.md", "user", "user", "fp-user")
    index.move_block_path("旧路径.md", "新路径.md")
    assert index.block_state("旧路径.md", "model") is None
    assert index.block_state("新路径.md", "model")["fingerprint"] == "fp-hand"
    assert len(index.blocks_for_path("新路径.md")) == 2


def test_session_links_register_lookup_and_update(index: Index):
    index.register_link("claude-code", "host-1", "claude-code:host-1", r"D:\w", "hash-1", STAMP)
    link = index.get_session_link("claude-code", "host-1")
    assert link["session_id"] == "claude-code:host-1"
    assert link["cwd"] == r"D:\w"
    assert index.get_session_link("codex", "nope") is None
    # 同键更新而不是重复插入
    index.register_link("claude-code", "host-1", "claude-code:host-1b", r"D:\w", "hash-1", STAMP)
    assert index.get_session_link("claude-code", "host-1")["session_id"] == "claude-code:host-1b"
    # 便捷写法：created_at 用 now_fn
    index.set_session_link("codex", "host-9", "codex:host-9", r"D:\w", "hash-9")
    assert index.get_session_link("codex", "host-9")["created_at"] == STAMP


def test_find_link_by_cwd_and_message_latest_wins(index: Index):
    index.register_link("cc", "a", "session-a", r"D:\w", "same-hash", "2026-10-20T00:00:00Z")
    index.register_link("cc", "b", "session-b", r"D:\w", "same-hash", "2026-10-21T00:00:00Z")
    index.register_link("cc", "c", "session-c", r"D:\w", "other-hash", "2026-10-22T00:00:00Z")
    assert index.find_link_by_cwd_and_message(r"D:\w", "same-hash") == "session-b"
    assert index.find_link_by_cwd_and_message(r"D:\w", "missing") == ""
    assert index.find_link_by_cwd_and_message(r"D:\elsewhere", "same-hash") == ""


# ---------------------------------------------------------------------------
# 公共池


def test_public_card_upsert_and_search(index: Index):
    index.upsert_public(
        {"id": "安装-python", "kind": "安装", "name": "python", "state": "生效",
         "path": "公共池/安装/python.md", "updated_at": STAMP},
        "Python 3.12 安装在 D:\\Python312，验证命令 python -V。",
    )
    index.upsert_public(
        {"id": "代理-clash", "kind": "代理", "name": "clash", "state": "生效",
         "path": "公共池/代理/clash.md", "updated_at": STAMP},
        "Clash 代理端口 7890。",
    )
    assert [row["id"] for row in index.search_public("Python")] == ["安装-python"]
    assert [row["id"] for row in index.search_public("代理")] == ["代理-clash"]
    # 单字回退 LIKE
    assert [row["id"] for row in index.search_public("装")] == ["安装-python"]
    row = index.search_public("clash")[0]
    assert row["kind"] == "代理"
    assert row["path"] == "公共池/代理/clash.md"
    assert "body" in row


# ---------------------------------------------------------------------------
# 多进程/多线程并发写（WAL + busy_timeout）


def test_concurrent_thread_writes_do_not_fail(index: Index):
    errors: list[BaseException] = []

    def worker(worker_id: int) -> None:
        try:
            for number in range(10):
                add_session(
                    index,
                    f"w{worker_id}-{number}",
                    f"并发写入 数据库 索引 {worker_id}-{number}",
                    name=f"并发{worker_id}-{number}",
                )
        except BaseException as exc:  # noqa: BLE001 - 测试收集所有异常
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(number,)) for number in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == []
    assert len(index.sessions()) == 40
    assert len(index.search_sessions("并发写入", None, 5)) == 5


def test_concurrent_schema_initialization_on_fresh_home(tmp_path):
    """多进程/多线程同时首次连接同一 home：WAL 与建表初始化不应抛 database is locked。"""
    home = tmp_path / "home"
    errors: list[BaseException] = []

    def worker(worker_id: int) -> None:
        try:
            fresh = Index(home, now_fn=lambda: NOW)
            fresh.upsert_session(
                {
                    "session_id": f"init-{worker_id}",
                    "project": "P1",
                    "name": f"初始化{worker_id}",
                    "path": f"会话总结/init-{worker_id}.md",
                    "status": "进行中",
                    "updated_at": STAMP,
                },
                "并发首次初始化数据库",
            )
        except BaseException as exc:  # noqa: BLE001 - 测试收集所有异常
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(number,)) for number in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == []
    final = Index(home)
    assert len(final.sessions()) == 4
    with final.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
