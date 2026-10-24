"""naming.py 测试：canvas 7 条命名规则的确定性实现。"""

from __future__ import annotations

import random

import pytest

from tbrain import naming

DATE = "2026-10-24"


def name_of(message: str, project: str = "Tbrain", history=None, sequence: int = 1) -> str:
    return naming.session_name(message, project, list(history or []), DATE, sequence)


def base_of(name: str) -> str:
    """去掉程序追加的 ``-YYYY-MM-DD-序号`` 尾巴。"""
    return name.rsplit("-" + DATE + "-", 1)[0]


# ---------------------------------------------------------------------------
# 分词器惰性初始化与确定性


def test_jieba_is_loaded_lazily():
    naming._reset_segmenter()
    assert naming._JIEBA is None
    # 低信息判断不加载 jieba
    naming.is_low_information("继续")
    assert naming._JIEBA is None
    # 需要关键词提取时才加载
    long_message = "实现存储层的数据库索引，要求原子写入并处理项目卡增删改与最长路径匹配"
    naming.session_name(long_message, "Tbrain", [], DATE, 1)
    assert naming._JIEBA is not None


def test_naming_is_deterministic_and_ignores_global_random():
    message = "实现存储层的数据库索引，要求原子写入并处理项目卡增删改与最长路径匹配，同时归档公共池卡片"
    history = ["重构存储层", "重构索引", "重构命名模块"]
    first = naming.session_name(message, "Tbrain", history, DATE, 2)
    random.seed(0)
    second = naming.session_name(message, "Tbrain", history, DATE, 2)
    random.seed(123456)
    third = naming.session_name(message, "Tbrain", history, DATE, 2)
    assert first == second == third


def test_naming_does_not_use_system_time():
    a = naming.session_name("实现数据库索引", "Tbrain", [], "2026-10-24", 1)
    b = naming.session_name("实现数据库索引", "Tbrain", [], "2026-10-24", 1)
    assert a == b
    assert a.endswith("-2026-10-24-1")


# ---------------------------------------------------------------------------
# 低信息判断


@pytest.mark.parametrize(
    "message",
    ["继续", "接着上次", "ok", "OK", "好", "好的", "开始", "开始吧", "嗯", "收到", "", "   ", "？？？", "继续实现"],
)
def test_low_information_true(message):
    assert naming.is_low_information(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "实现数据库索引的原子写入",
        "修复 Claude Code hook 在 Windows 下失败的问题",
        "重构存储层的项目卡增删改",
    ],
)
def test_low_information_false(message):
    assert naming.is_low_information(message) is False


def test_low_information_message_gets_temp_name():
    assert name_of("继续") == "未命名-2026-10-24-1"
    assert name_of("", sequence=4) == "未命名-2026-10-24-4"


# ---------------------------------------------------------------------------
# 临时名 / 接续名 / 文件名清洗


def test_temp_name_and_resume_name_formats():
    assert naming.temp_name("2026-10-24", 1) == "未命名-2026-10-24-1"
    assert naming.temp_name("2026-10-24", 12) == "未命名-2026-10-24-12"
    assert naming.resume_name("旧会话", "2026-10-24", 2) == "旧会话 续 2026-10-24-2"
    assert naming.resume_name("旧会话-2026-10-20-3", "2026-10-24", 2) == "旧会话 续 2026-10-24-2"


def test_sanitize_filename_removes_windows_illegal_characters():
    result = naming.sanitize_filename('a/b\\c:d*e?f"g<h>i|j')
    for char in '\\/:*?"<>|':
        assert char not in result
    assert result == "a b c d e f g h i j"


def test_sanitize_filename_strips_edges_and_collapses_space():
    assert naming.sanitize_filename("  名称  .  ") == "名称"
    assert naming.sanitize_filename("多   空格") == "多 空格"
    assert naming.sanitize_filename("换\n行") == "换 行"
    assert naming.sanitize_filename("") == "未命名"
    assert naming.sanitize_filename(None) == "未命名"


def test_sanitize_filename_handles_reserved_and_length():
    assert naming.sanitize_filename("CON") == "_CON"
    long_name = "名" * 300
    assert len(naming.sanitize_filename(long_name)) <= naming.MAX_FILENAME_CHARS


def test_session_name_is_sanitized():
    name = name_of('实现 "索引"<测试>')
    for char in '\\/:*?"<>|':
        assert char not in name


# ---------------------------------------------------------------------------
# 7 条规则


def test_polite_prefix_removed():
    name = name_of("请帮我实现数据库索引")
    assert "请" not in name
    assert "帮我" not in name
    assert name.startswith("实现数据库索引")


def test_noise_removed_urls_parentheses_code_and_paths_keep_basename():
    message = "请帮我看看 https://example.com/a/b 的 `临时函数` D:\\work\\proj\\main.py 的 bug"
    name = name_of(message)
    assert "https" not in name
    assert "example" not in name
    assert "临时函数" not in name
    assert "D:\\" not in name
    assert "main.py" in name


def test_first_sentence_only():
    name = name_of("实现数据库索引。另外顺手重构命名模块")
    assert "实现数据库索引" in name
    assert "重构" not in name


def test_short_message_used_directly():
    name = name_of("实现数据库索引")
    assert base_of(name) == "实现数据库索引"
    assert name.endswith("-2026-10-24-1")


def test_long_message_keywords_within_twenty_chars_and_in_original_order():
    message = (
        "实现存储层的数据库索引，要求原子写入并处理项目卡增删改与最长路径匹配，"
        "同时保证公共池卡片在再次提交时正确归档替换"
    )
    name = name_of(message)
    base = base_of(name)
    assert 0 < len(base) <= naming.MAX_BASE_CHARS
    positions = [base.find(word) for word in ("数据库", "索引", "原子", "路径") if word in base]
    assert positions, base
    # 保留下来的词按原文顺序出现
    assert positions == sorted(positions)


def test_project_name_removed_from_name():
    name = name_of("Tbrain 的数据库索引实现", project="Tbrain")
    assert "Tbrain" not in name
    assert "数据库索引实现" in name


def test_history_common_words_downweighted():
    history = ["重构存储层", "重构索引模块", "重构命名模块"]
    message = (
        "重构存储层的数据库索引并补充原子写入测试，处理项目卡增删改和最长路径匹配，最后归档公共池卡片"
    )
    name = name_of(message, history=history)
    base = base_of(name)
    assert "重构" not in base  # 历史会话共有的词被降权，名称彼此可区分
    assert "数据库" in base or "索引" in base


def test_english_message_keeps_technology_words():
    name = name_of("Fix the SQLite WAL busy_timeout bug in our index layer, please")
    assert "SQLite" in name
    assert "WAL" in name


def test_suffix_is_date_and_sequence():
    assert name_of("实现数据库索引", sequence=3).endswith("-2026-10-24-3")
    assert name_of("实现数据库索引", sequence=12).endswith("-2026-10-24-12")


def test_history_names_with_suffix_are_stripped_for_weighting():
    history = ["重构数据库索引-2026-10-20-1", "重构数据库索引-2026-10-21-2"]
    message = "重构数据库索引并补充原子写入测试以及项目卡增删改和最长路径匹配的完整实现"
    name = name_of(message, history=history)
    assert "重构" not in base_of(name)
