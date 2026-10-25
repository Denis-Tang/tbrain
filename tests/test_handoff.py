from datetime import datetime, timezone

from tbrain import handoff


def _summary():
    return handoff.new_summary(
        name="重构索引 2026-10-24-1", project="重构索引",
        session_id="claude-code:abc", agent="claude-code",
        cwd=r"D:\work\proj", host_record="", started_at="2026-10-24T13:00:00Z",
        raw_command="帮我把索引改成 SQLite", provisional=False,
    )


def test_new_summary_records_raw_command_and_u1():
    summary = _summary()
    assert summary.raw_command == "帮我把索引改成 SQLite"
    assert [item["text"] for item in summary.users] == ["帮我把索引改成 SQLite"]
    assert summary.header["saved_users"] == 0
    assert summary.status == "进行中"


def test_render_parse_roundtrip_keeps_sections_and_items():
    summary = _summary()
    handoff.add_item(summary, "已完成", "把 vault.py 写入改成原子写 [证据:E1]")
    handoff.add_item(summary, "决策", "SQLite 只做索引 | Markdown 是唯一事实源")
    handoff.add_item(summary, "未走通的路", "用 trigram 分词 | 已证实不可行 | SQLite 3.45 | 已排除 [证据:E2]")
    handoff.add_item(summary, "证据", "vault.py:42")
    handoff.add_goal(summary, "跑通中文检索")
    summary.target = "把第二大脑重构成 Markdown 唯一事实源的 Tbrain"
    summary.next_step = "跑 pytest -q 验证中文检索"
    summary.header["status"] = "待验证"
    text = handoff.render(summary)
    parsed = handoff.parse(text)
    assert parsed.name == summary.name
    assert parsed.project == "重构索引"
    assert parsed.raw_command == summary.raw_command
    assert parsed.target == summary.target
    assert parsed.next_step == summary.next_step
    assert parsed.status == "待验证"
    assert [item["text"] for item in parsed.users] == [item["text"] for item in summary.users]
    assert parsed.items("已完成")[0]["id"] == "D1"
    assert parsed.items("决策")[0]["id"] == "K1"
    assert parsed.items("未走通的路")[0]["id"] == "X1"
    assert parsed.items("证据")[0]["id"] == "E1"
    assert parsed.goals()[0]["id"] == "G1"


def test_numbering_continues_across_parse():
    summary = _summary()
    handoff.add_item(summary, "已完成", "第一条")
    text = handoff.render(summary)
    parsed = handoff.parse(text)
    assert handoff.next_number(parsed, "已完成") == 2
    assert handoff.add_item(parsed, "已完成", "第二条") == "D2"


def test_complete_goals_and_supersede_decisions():
    summary = _summary()
    handoff.add_goal(summary, "目标一")
    handoff.add_goal(summary, "目标二")
    handoff.add_item(summary, "决策", "旧决策 | 理由")
    done = handoff.complete_goals(summary, ["g1"])
    assert done == ["G1"]
    assert summary.goals()[0]["done"] is True
    superseded = handoff.supersede_decisions(summary, ["K1"])
    assert superseded == ["K1"]
    assert "被取代" in summary.items("决策")[0]["text"]


def test_done_and_decision_overflow_to_archive():
    summary = _summary()
    for index in range(handoff.ARCHIVE_THRESHOLD + 3):
        handoff.add_item(summary, "已完成", f"完成 {index}")
    assert len(summary.sections["已完成"]) == handoff.ARCHIVE_THRESHOLD
    assert len(summary.archive) == 3
    parsed = handoff.parse(handoff.render(summary))
    assert len(parsed.sections["已完成"]) == handoff.ARCHIVE_THRESHOLD
    assert len(parsed.archive) == 3


def test_long_user_message_is_clipped_with_note():
    summary = handoff.new_summary(
        name="n", project="", session_id="x:y", agent="pi", cwd="/tmp",
        host_record="", started_at="2026-10-24T00:00:00Z", raw_command="x" * 5000,
    )
    assert len(summary.users[0]["text"]) < 5000
    assert "原文位置" in summary.users[0]["text"]


def test_fingerprint_ignores_trailing_whitespace_and_newlines():
    left = "## 目标\nabc\n"
    right = "## 目标\r\nabc   \r\n\r\n"
    assert handoff.fingerprint(left) == handoff.fingerprint(right)


def test_parse_without_front_matter_still_returns_sections():
    text = "## 原始命令\nhello\n\n## 下一步\n做 A 然后验证\n"
    summary = handoff.parse(text)
    assert summary.raw_command == "hello"
    assert summary.next_step == "做 A 然后验证"
    assert summary.session_id == ""


def test_open_goals_helper():
    summary = _summary()
    handoff.add_goal(summary, "a")
    assert handoff.has_open_goals(summary) is True
    handoff.complete_goals(summary, ["G1"])
    assert handoff.has_open_goals(summary) is False
