"""store.py 测试：vault 布局、原子写、区块所有权、项目、提示词、公共池、索引与重命名。

全部使用 pytest ``tmp_path``，不读写真实 ~/.tbrain 或真实 vault。
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tbrain.index import Index
from tbrain.store import Store

FIXED_NOW = datetime(2026, 10, 24, 13, 0, 0, tzinfo=timezone.utc)
STAMP = "2026-10-24T13:00:00Z"


@pytest.fixture()
def store(tmp_path) -> Store:
    vault = tmp_path / "vault"
    home = tmp_path / "home"
    value = Store(vault, home, now_fn=lambda: FIXED_NOW)
    value.ensure_layout()
    return value


def _tmp_files(directory: Path) -> list[Path]:
    return [path for path in directory.rglob("*.tmp")]


# ---------------------------------------------------------------------------
# 布局与原子写


def test_ensure_layout_creates_directories_and_defaults(store: Store):
    vault = store.vault
    for relative in ("会话总结", "项目", "公共池", "独立会话池", "附件", ".tbrain"):
        assert (vault / relative).is_dir()
    prompt = vault / "全局提示词.md"
    assert prompt.is_file()
    text = prompt.read_text(encoding="utf-8")
    assert "<!-- owner:user -->" in text
    assert "<!-- owner:model -->" in text
    index_file = vault / "独立会话池" / "会话索引.md"
    assert index_file.is_file()
    assert "本文件由程序生成" in index_file.read_text(encoding="utf-8")


def test_ensure_layout_is_idempotent(store: Store):
    prompt = store.vault / "全局提示词.md"
    before = prompt.read_bytes()
    before_mtime = prompt.stat().st_mtime_ns
    time.sleep(0.002)
    store.ensure_layout()
    store.ensure_layout()
    assert prompt.read_bytes() == before
    assert prompt.stat().st_mtime_ns == before_mtime


def test_atomic_write_and_read(tmp_path, store: Store):
    target = tmp_path / "a" / "b.md"
    store.write_text(target, "第一行\n第二行\n")
    assert store.read_text(target) == "第一行\n第二行\n"
    assert _tmp_files(tmp_path) == []
    assert store.read_text(tmp_path / "missing.md") == ""


def test_atomic_write_keeps_old_content_when_replace_fails(tmp_path, store: Store, monkeypatch):
    target = tmp_path / "keep.md"
    store.write_text(target, "旧内容")
    import tbrain.store as store_module

    def broken_replace(src, dst):
        raise OSError("模拟写盘失败")

    monkeypatch.setattr(store_module.os, "replace", broken_replace)
    with pytest.raises(OSError):
        store.write_text(target, "新内容")
    assert store.read_text(target) == "旧内容"
    assert _tmp_files(tmp_path) == []


def test_write_text_retries_transient_permission_error(tmp_path, store: Store):
    import tbrain.store as store_module

    target = tmp_path / "retry.md"
    real_replace = store_module.os.replace
    state = {"calls": 0}

    def flaky_replace(src, dst):
        state["calls"] += 1
        if state["calls"] == 1:
            raise PermissionError("模拟另一个进程占用目标文件")
        return real_replace(src, dst)

    store_module.os.replace = flaky_replace
    try:
        store.write_text(target, "最终内容")
    finally:
        store_module.os.replace = real_replace
    assert target.read_text(encoding="utf-8") == "最终内容"
    assert _tmp_files(tmp_path) == []


def test_write_text_accepts_concurrent_identical_write(tmp_path, store: Store):
    """两进程同时首次 ensure_layout：目标已被写成同内容时不再抛 PermissionError。"""
    import tbrain.store as store_module

    target = tmp_path / "same.md"
    target.write_text("同一内容\n", encoding="utf-8")
    real_replace = store_module.os.replace

    def locked_replace(src, dst):
        raise PermissionError("[WinError 5] 目标文件正被另一进程替换")

    store_module.os.replace = locked_replace
    try:
        store.write_text(target, "同一内容\n")
    finally:
        store_module.os.replace = real_replace
    assert target.read_text(encoding="utf-8") == "同一内容\n"
    assert _tmp_files(tmp_path) == []


def test_write_text_raises_after_retries_for_different_content(tmp_path, store: Store):
    import tbrain.store as store_module

    target = tmp_path / "different.md"
    target.write_text("另一进程的内容", encoding="utf-8")
    real_replace = store_module.os.replace

    def locked_replace(src, dst):
        raise PermissionError("目标一直被占用")

    store_module.os.replace = locked_replace
    try:
        with pytest.raises(PermissionError):
            store.write_text(target, "本次内容")
    finally:
        store_module.os.replace = real_replace
    assert target.read_text(encoding="utf-8") == "另一进程的内容"
    assert _tmp_files(tmp_path) == []


def test_ensure_layout_tolerates_concurrent_creation(tmp_path):
    """模拟另一进程在 write 期间创建了同名文件：ensure_layout 不抛异常。"""
    vault = tmp_path / "vault"
    home = tmp_path / "home"
    store = Store(vault, home, now_fn=lambda: FIXED_NOW)

    def racing_write(path, text, *, fsync=True):
        Path(path).write_text(text, encoding="utf-8")
        raise PermissionError("[WinError 5] 另一进程同时创建")

    store.write_text = racing_write  # type: ignore[method-assign]
    store.ensure_layout()  # 不应抛异常
    assert store.global_prompt_path().is_file()
    assert store.independent_index_path().is_file()


def test_read_text_tolerates_utf8_bom(tmp_path, store: Store):
    target = tmp_path / "bom.md"
    target.write_bytes("\ufeff你好".encode("utf-8"))
    assert store.read_text(target) == "你好"


# ---------------------------------------------------------------------------
# 区块指纹与所有权


def test_block_fingerprint_normalizes_line_endings_and_blank_lines(store: Store):
    left = store.block_fingerprint("第一行\r\n第二行  \r\n\r\n")
    right = store.block_fingerprint("第一行\n第二行\n")
    assert left == right
    assert left != store.block_fingerprint("第一行\n第二行改了\n")


def test_global_prompt_save_writes_user_area_only(store: Store):
    path = store.vault / "全局提示词.md"
    model_before = store.block_state(path, "model")
    assert store.global_prompt() == ""

    result = store.save_global_prompt("用户手写的全局规则\n第二行")
    assert result["ok"] is True and result["written"] is True
    assert store.global_prompt() == "用户手写的全局规则\n第二行"
    # model 区内容与指纹都不变
    assert store.block_state(path, "model") == model_before
    text = path.read_text(encoding="utf-8")
    assert "（第一期不启用模型区）" in text
    # 用户区标记仍是 user
    assert store.block_state(path, "user")["owner"] == "user"

    store.save_global_prompt("第二版")
    assert store.global_prompt() == "第二版"
    changelog = (store.vault / "全局提示词变更日志.md").read_text(encoding="utf-8")
    assert "第二版" in changelog or "用户区更新" in changelog


def test_global_prompt_is_not_auto_modified(store: Store):
    store.save_global_prompt("不要被程序改")
    path = store.vault / "全局提示词.md"
    before = path.read_bytes()
    # 各种程序操作都不应改全局提示词
    store.save_project("P1", [r"D:\work\p1"])
    store.public_l1()
    store.attachments()
    store.write_independent_index([])
    store.ensure_layout()
    assert path.read_bytes() == before


def test_global_prompt_plain_file_adopted_without_data_loss(store: Store):
    path = store.vault / "全局提示词.md"
    path.write_text("用户原来的纯文本提示词", encoding="utf-8")
    assert store.global_prompt() == "用户原来的纯文本提示词"
    store.save_global_prompt("新内容")
    assert store.global_prompt() == "新内容"


# ---------------------------------------------------------------------------
# 项目


def test_save_project_writes_readable_yaml_and_returns_payload(store: Store):
    payload = store.save_project("Tbrain", [r"D:\work\tbrain", "E:/work/other"], allow_child=True)
    assert payload["name"] == "Tbrain"
    assert payload["paths"] == [r"D:\work\tbrain", r"E:\work\other"]
    assert payload["allow_child"] is True
    assert payload["created_at"] == STAMP
    text = store.project_card_path("Tbrain").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "name: Tbrain" in text
    assert r"D:\work\tbrain" in text
    assert "allow_child: true" in text
    # 中文可读（不被转义成 \uXXXX）
    store.save_project("中文项目", [r"D:\工作\项目"])
    assert "中文项目" in store.project_card_path("中文项目").read_text(encoding="utf-8")
    assert [item["name"] for item in store.projects()] == ["Tbrain", "中文项目"]


def test_save_project_keeps_created_at_and_user_body(store: Store):
    store.save_project("P1", [r"D:\p1"])
    card = store.project_card_path("P1")
    data = card.read_text(encoding="utf-8")
    card.write_text(data + "\n用户备注：这个项目很重要\n", encoding="utf-8")
    payload = store.save_project("P1", [r"D:\p1", r"D:\p1-extra"])
    assert payload["created_at"] == STAMP
    assert payload["paths"] == [r"D:\p1", r"D:\p1-extra"]
    assert "用户备注：这个项目很重要" in card.read_text(encoding="utf-8")


def test_save_project_rejects_invalid_name_and_parent(store: Store):
    with pytest.raises(ValueError):
        store.save_project("", [])
    with pytest.raises(ValueError):
        store.save_project("bad/name", [])
    with pytest.raises(ValueError):
        store.save_project("bad:name", [])
    store.save_project("P1", [r"D:\p1"])
    with pytest.raises(ValueError):
        store.save_project("P2", [], parent="不存在的项目")
    with pytest.raises(ValueError):
        store.save_project("P1", [], parent="P1")
    store.save_project('child', [], parent='P1')
    with pytest.raises(ValueError, match='循环'):
        store.save_project('P1', [], parent='child')


def test_match_project_longest_prefix_windows_case_and_separators(store: Store):
    store.save_project("Root", [r"D:\work"])
    store.save_project("Child", [r"D:\work\proj"], parent="Root")
    assert store.match_project(r"D:\work") == "Root"
    assert store.match_project(r"D:\work\other") == "Root"
    assert store.match_project(r"D:\work\proj\src\main.py") == "Child"
    assert store.match_project("d:/WORK/PROJ/src") == "Child"
    assert store.match_project(r"C:\elsewhere") == ""
    assert store.match_project("") == ""


def test_rename_project_moves_sessions_and_updates_front_matter(store: Store):
    store.save_project("旧项目", [r"D:\old"], allow_child=True)
    store.save_project("子项目", [r"D:\old\child"], parent="旧项目")
    session = store.session_path("旧项目", FIXED_NOW, "会话一")
    store.write_text(
        session,
        "---\nname: 会话一\nproject: 旧项目\n---\n\n正文\n",
    )
    result = store.save_project(
        "新项目", [r"D:\new"], parent="", allow_child=True, rename_from="旧项目"
    )
    assert result["name"] == "新项目"
    assert store.project("旧项目") is None
    assert not (store.vault / "项目" / "旧项目").exists()
    assert store.project("新项目") is not None
    new_session = store.session_path("新项目", FIXED_NOW, "会话一")
    assert new_session.is_file()
    assert "project: 新项目" in new_session.read_text(encoding="utf-8")
    assert store.project("子项目")["parent"] == "新项目"


def test_delete_project_archives_to_internal_dir_and_keeps_sessions(store: Store):
    store.save_project("待删", [r"D:\gone"], allow_child=True)
    store.save_project("子项目", [r"D:\gone\child"], parent="待删")
    session = store.session_path("待删", FIXED_NOW, "旧会话")
    store.write_text(session, "---\nname: 旧会话\nproject: 待删\n---\n\n正文\n")
    result = store.delete_project("待删")
    assert result["ok"] is True
    assert Path(result["archived_to"]).is_dir()
    assert (store.vault / ".tbrain" / "已删除项目").is_dir()
    assert store.project("待删") is None
    assert session.is_file()  # 会话总结不删
    assert result["sessions_kept"] == 1
    assert store.project("子项目")["parent"] == ""
    with pytest.raises(ValueError):
        store.delete_project("不存在")


# ---------------------------------------------------------------------------
# 项目提示词


def test_project_prompt_sections_round_trip(store: Store):
    from tbrain import handoff
    summary = handoff.new_summary(name="来源会话", project="P1", session_id="codex:source", agent="codex",
                                  cwd="", host_record="", started_at=STAMP, raw_command="第一条")
    handoff.add_user(summary, "第二条")
    handoff.add_user(summary, "第三条：写入必须原子")
    store.save_project("P1", [r"D:\p1"])
    initial = store.project_prompt("P1")
    assert set(initial["sections"]) == {"最终目标", "硬规则", "当前阶段", "关键路径和命令"}
    assert initial["sections"]["最终目标"] == ""

    result = store.save_project_prompt(
        "P1",
        {
            "最终目标": "完成最小闭环",
            "硬规则": "- 写入必须原子（来源：#U3）",
            "当前阶段": "第 4 步",
        },
        summary=summary,
    )
    assert set(result["written"]) == {"最终目标", "硬规则", "当前阶段"}
    assert result["rejected"] == []
    sections = store.project_prompt("P1")["sections"]
    assert sections["最终目标"] == "完成最小闭环"
    assert "（来源：#U3）" in sections["硬规则"]
    assert sections["当前阶段"] == "第 4 步"
    assert sections["关键路径和命令"] == ""


def test_project_prompt_validation_rejects_bad_entries(store: Store):
    store.save_project("P1", [r"D:\p1"])
    result = store.save_project_prompt(
        "P1",
        {"硬规则": "- 没有来源的规则", "不存在的节": "内容"},
    )
    assert result["written"] == {}
    rejected = {item["section"] for item in result["rejected"]}
    assert "硬规则" in rejected
    assert "不存在的节" in rejected
    for item in result["rejected"]:
        assert item["reason"]
        assert "example" in item
    assert store.project_prompt("P1")["sections"]["硬规则"] == ""


def test_project_prompt_changelog_records_old_content(store: Store):
    store.save_project("P1", [r"D:\p1"])
    store.save_project_prompt("P1", {"最终目标": "旧目标"})
    store.save_project_prompt("P1", {"最终目标": "新目标"})
    changelog = store.project_changelog_path("P1").read_text(encoding="utf-8")
    assert "旧目标" in changelog
    assert "最终目标" in changelog


def test_project_prompt_preserves_user_area_and_protects_hand_edited_model(store: Store):
    store.save_project("P1", [r"D:\p1"])
    path = store.project_prompt_path("P1")
    store.save_project_prompt("P1", {"最终目标": "目标一"})
    text = path.read_text(encoding="utf-8")
    text = text.replace("## 用户补充", "## 用户补充\n用户手写的补充要求")
    path.write_text(text, encoding="utf-8")
    # 用户区内容保留
    assert "用户手写的补充要求" in store.project_prompt("P1")["user"]
    # 手改 model 区 -> 转为用户所有，不再覆盖
    model_text = path.read_text(encoding="utf-8").replace("目标一", "用户手改目标")
    path.write_text(model_text, encoding="utf-8")
    result = store.save_project_prompt("P1", {"最终目标": "程序新目标"})
    assert result["written"] == {}
    assert result["rejected"]
    assert result.get("pending_confirm")
    assert "用户手改目标" in path.read_text(encoding="utf-8")
    assert store.block_state(path, "model")["owner"] == "user"


# ---------------------------------------------------------------------------
# 公共池


def test_public_card_supersede_archives_old_card(store: Store):
    first = store.save_public_card(
        "安装", "python", {"version": "3.11", "location": r"D:\Python311"}
    )
    assert first["id"] == "安装-python"
    assert Path(first["path"]).is_file()
    second = store.save_public_card(
        "安装", "python", {"version": "3.12", "location": r"D:\Python312"}, source_session="[[会话]]"
    )
    assert second["replaced"] is not None
    archived = Path(second["archived_path"])
    assert archived.is_file()
    cards = store.public_cards("安装")
    assert len(cards) == 1
    assert cards[0]["version"] == "3.12"
    assert cards[0]["state"] == "生效"
    assert cards[0]["source_session"] == "[[会话]]"
    old_text = archived.read_text(encoding="utf-8")
    assert "已被取代" in old_text
    assert "3.11" in old_text
    assert "history" in old_text


def test_public_card_validates_kind_and_name(store: Store):
    with pytest.raises(ValueError):
        store.save_public_card("未知类型", "x", {})
    with pytest.raises(ValueError):
        store.save_public_card("安装", "", {})


def test_public_card_user_changes_remain_active_and_conflict_persists(store):
    card = store.save_public_card('工具', '测试工具', {'version': '1', 'body': '模型原文'})
    path = Path(card['path'])
    path.write_text(path.read_text(encoding='utf-8').replace('模型原文', '用户手改正文'), encoding='utf-8')
    for version in ('2', '3'):
        with pytest.raises(ValueError, match='待用户确认'):
            store.save_public_card('工具', '测试工具', {'version': version, 'body': '模型更新'})
    assert '用户手改正文' in store.public_cards()[0]['body']
    assert not list((path.parent / '归档').glob('*.md'))
    assert '待用户确认' in Store(store.vault, store.app_home).pending_confirmations('')
    from tbrain.service import TbrainService
    service = TbrainService(store.app_home, vault=store.vault)
    service.settings.update({'ntp_enabled': False})
    service.bootstrap('codex:cards', 'codex', str(store.vault))
    result = service.save('codex:cards', [{'op': 'env', 'items': [
        {'type': '工具', 'name': '测试工具', 'version': '4'}, {'type': '工具', 'name': '另一工具', 'version': '1'}]}])
    assert result['written']['env'] == ['工具-另一工具'] and len(result['rejected']) == 1


def test_public_l1_contains_kind_lines_and_hot_facts(store: Store):
    store.save_public_card("安装", "python", {"version": "3.12", "location": r"D:\Python312"})
    store.save_public_card("代理", "clash", {"location": "127.0.0.1:7890"})
    store.save_project("P1", [r"D:\work\p1"])
    text = store.public_l1()
    assert "# 公共池" in text
    assert "安装：1 条" in text
    assert "代理：1 条" in text
    assert "python" in text
    assert "工作区路径" in text and "P1" in text
    assert len(text.splitlines()) <= 40


# ---------------------------------------------------------------------------
# 附件（只读）


def test_attachments_are_read_only_and_filtered_by_project(store: Store):
    base = store.vault / "附件" / "skills"
    base.mkdir(parents=True)
    card = base / "索引技能.md"
    content = (
        "---\nname: 索引技能\ntype: skills\npath: D:\\skills\\index.md\n"
        "note: 检索规范\ngroup: 检索\nprojects: []\n---\n\n正文\n"
    )
    card.write_text(content, encoding="utf-8")
    project_card = base / "项目技能.md"
    project_card.write_text(
        "---\nname: 项目技能\ntype: skills\npath: D:\\skills\\p.md\nprojects: [P1]\n---\n",
        encoding="utf-8",
    )
    before = card.read_bytes()
    all_cards = store.attachments()
    assert {item["name"] for item in all_cards} == {"项目技能", "索引技能"}
    p1_cards = store.attachments("P1")
    assert {item["name"] for item in p1_cards} == {"索引技能", "项目技能"}
    p2_cards = store.attachments("P2")
    assert [item["name"] for item in p2_cards] == ["索引技能"]
    entry = next(item for item in all_cards if item["name"] == "索引技能")
    assert entry["type"] == "skills"
    assert entry["path"] == r"D:\skills\index.md"
    assert entry["note"] == "检索规范"
    assert entry["group"] == "检索"
    assert card.read_bytes() == before  # 只读：不改文件


# ---------------------------------------------------------------------------
# 会话索引与重命名


def test_write_project_index_has_generated_comment_and_rows(store: Store):
    store.save_project("P1", [r"D:\p1"])
    path = store.write_project_index(
        "P1",
        [
            {
                "name": "会话一",
                "status": "进行中",
                "agent": "claude-code",
                "updated_at": STAMP,
                "open_goals": 2,
                "next_step": "写测试",
            }
        ],
    )
    assert path == store.project_index_path("P1")
    text = path.read_text(encoding="utf-8")
    assert text.startswith("<!-- 本文件由程序生成")
    assert "[[会话一]]" in text
    assert "进行中" in text
    assert "claude-code" in text
    assert "写测试" in text
    assert store.project("P1")["session_count"] == 1


def test_write_independent_index_groups_by_cwd(store: Store):
    path = store.write_independent_index(
        [
            {"name": "独立一", "status": "进行中", "cwd": r"D:\a", "updated_at": STAMP},
            {"name": "独立二", "status": "已中断", "cwd": r"D:\b", "updated_at": STAMP},
        ]
    )
    text = path.read_text(encoding="utf-8")
    assert "本文件由程序生成" in text
    assert "## D:\\a" in text
    assert "## D:\\b" in text
    assert "[[独立一]]" in text


def test_session_path_layout_for_project_and_independent(store: Store):
    project_path = store.session_path("P1", FIXED_NOW, "会话一")
    assert project_path.parent.parts[-2:] == ("2026", "10")
    assert "会话总结" in project_path.parts and "P1" in project_path.parts
    independent = store.session_path("独立", FIXED_NOW, "独立会话")
    assert independent.parent.name == "10"
    assert independent.parent.parent.name == "2026"
    assert independent.parent.parent.parent.name == "独立"


def test_rename_session_updates_file_links_index_and_blocks(store: Store):
    store.save_project("P1", [r"D:\p1"])
    old = store.session_path("P1", FIXED_NOW, "旧会话名")
    store.write_text(old, "---\nname: 旧会话名\nproject: P1\n---\n\n正文不动。\n")
    other = store.session_path("P1", FIXED_NOW, "另一个会话")
    store.write_text(
        other,
        "---\nname: 另一个会话\nproject: P1\n---\n\n"
        "参见 [[旧会话名]]、[[旧会话名|别名]]、[[旧会话名#目标]]，以及 [[旧会话名字]]（不应改）。\n",
    )
    store.write_project_index(
        "P1", [{"name": "旧会话名", "status": "进行中", "agent": "cc", "updated_at": STAMP}]
    )
    index = Index(store.app_home, now_fn=lambda: FIXED_NOW)
    old_rel = store._rel(old)
    index.set_block_state(old_rel, "model", "model", "fp-old")

    new_path = store.rename_session(old, "新会话名")
    assert new_path.name == "新会话名.md"
    assert new_path.is_file() and not old.exists()
    assert "name: 新会话名" in new_path.read_text(encoding="utf-8")
    assert "正文不动。" in new_path.read_text(encoding="utf-8")
    link_text = other.read_text(encoding="utf-8")
    assert "[[新会话名]]" in link_text
    assert "[[新会话名|别名]]" in link_text
    assert "[[新会话名#目标]]" in link_text
    assert "[[旧会话名字]]" in link_text  # 精确替换，不误伤相似名
    index_text = store.project_index_path("P1").read_text(encoding="utf-8")
    assert "[[新会话名]]" in index_text and "[[旧会话名]]" not in index_text
    assert index.block_state(old_rel, "model") is None
    assert index.block_state(store._rel(new_path), "model")["fingerprint"] == "fp-old"
    assert _tmp_files(store.vault) == []


def test_rename_session_rejects_existing_target(store: Store):
    store.save_project("P1", [r"D:\p1"])
    first = store.session_path("P1", FIXED_NOW, "会话一")
    second = store.session_path("P1", FIXED_NOW, "会话二")
    store.write_text(first, "---\nname: 会话一\nproject: P1\n---\n")
    store.write_text(second, "---\nname: 会话二\nproject: P1\n---\n")
    with pytest.raises(ValueError):
        store.rename_session(first, "会话二")
    assert first.is_file()


def test_append_changelog_appends_entries(store: Store):
    path = store.vault / "项目" / "P1" / "项目提示词变更日志.md"
    store.append_changelog(path, "第一次替换", "旧内容一")
    store.append_changelog(path, "第二次替换", "旧内容二")
    text = path.read_text(encoding="utf-8")
    assert "第一次替换" in text and "第二次替换" in text
    assert "旧内容一" in text and "旧内容二" in text
    assert text.index("第一次替换") < text.index("第二次替换")


def test_block_state_and_set_block_protection(store: Store):
    path = store.project_prompt_path("P1")
    store.save_project("P1", [r"D:\p1"])
    path = store.project_prompt_path("P1")
    assert store.block_state(path, "model")["owner"] == "model"
    assert store.set_block(path, "model", "model", "第一版内容") is True
    assert store.block_state(path, "model")["fingerprint"] == store.block_fingerprint("第一版内容")
    # 模拟用户手改
    path.write_text(
        path.read_text(encoding="utf-8").replace("第一版内容", "用户内容"),
        encoding="utf-8",
    )
    assert store.set_block(path, "model", "model", "第二版内容") is False
    assert "用户内容" in path.read_text(encoding="utf-8")
    assert store.block_state(path, "model")["owner"] == "user"
    # 已归用户后继续写仍拒绝
    assert store.set_block(path, "model", "model", "第三版内容") is False
    assert "第三版内容" not in path.read_text(encoding="utf-8")
