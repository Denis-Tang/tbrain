from tbrain import prompt


def _sections(**overrides):
    data = {
        "最终目标": "给 coding agent 用的外挂记忆\n完成 0.6.0 最小闭环",
        "硬规则": "- 所有 Python 脚本用 uv run 执行（来源：U2）\n- settings.json 字段名不可改（来源：[[重构索引 2026-10-24-1]]#E4）",
        "当前阶段": "第 3 步：版本管理与宿主适配完成",
        "关键路径和命令": "- 测试命令：.venv\\Scripts\\python.exe -m pytest -q",
    }
    data.update(overrides)
    return data


def _resolver(name, ref):
    if name:
        return name == "重构索引 2026-10-24-1" and ref == "E4"
    return ref in ("U2", "E4")


def test_valid_sections_pass_and_are_cleaned():
    result = prompt.validate(_sections(), resolve_source=_resolver)
    assert result["rejected"] == []
    assert set(result["written"].keys()) == set(prompt.SECTION_ORDER)
    assert result["written"]["硬规则"].startswith("- ")


def test_hard_rule_without_source_is_rejected():
    result = prompt.validate(_sections(硬规则="- 不要改配置"), resolve_source=_resolver)
    assert result["rejected"]
    assert "来源" in result["rejected"][0]["reason"]


def test_hard_rule_with_unknown_reference_is_rejected():
    result = prompt.validate(_sections(硬规则="- 不要改配置（来源：[[不存在的会话]]#U9）"), resolve_source=_resolver)
    assert result["rejected"]
    assert "引用" in result["rejected"][0]["reason"]


def test_over_limit_items_are_rejected_with_count_hint():
    rules = "\n".join(f"- 规则{i}（来源：U2）" for i in range(13))
    result = prompt.validate(_sections(硬规则=rules), resolve_source=_resolver)
    assert result["rejected"]
    assert "超出" in result["rejected"][0]["reason"]


def test_unknown_section_is_rejected():
    result = prompt.validate({"最终目标": "a", "过程记录": "b"}, resolve_source=_resolver)
    assert any("固定节" in item["reason"] for item in result["rejected"])


def test_missing_key_path_is_written_with_warning():
    result = prompt.validate(_sections(关键路径和命令="- 入口文件：D:\\no\\such\\file.py"),
                             resolve_source=_resolver, path_exists=lambda value: False)
    assert result["rejected"] == []
    assert any("路径未找到" in warning for warning in result["warnings"])
    assert "入口文件" in result["written"]["关键路径和命令"]


def test_pronoun_and_time_words_produce_warnings():
    result = prompt.validate(_sections(当前阶段="该模块最近改过，现在等测试"),
                             resolve_source=_resolver)
    assert result["rejected"] == []
    assert any("代词" in warning or "相对时间" in warning for warning in result["warnings"])


def test_render_parse_roundtrip_with_user_section():
    text = prompt.render(_sections(), user="- 我的补充偏好")
    parsed = prompt.parse(text)
    assert "我的补充偏好" in parsed["user"]
    assert parsed["sections"]["最终目标"].startswith("给 coding agent")


def test_user_markers_are_kept_in_render():
    text = prompt.render(prompt.empty_sections(), user="hello")
    assert prompt.USER_START in text and prompt.USER_END in text
    assert prompt.MODEL_START in text and prompt.MODEL_END in text


def test_overlong_line_is_rejected():
    result = prompt.validate(_sections(最终目标="很长" * 45), resolve_source=_resolver)
    assert result["rejected"]
    assert "每行不超过" in result["rejected"][0]["reason"]


def test_parse_plain_old_format_without_markers():
    text = "## 最终目标\n目标 A\n\n## 硬规则\n- 规则（来源：U1）\n"
    parsed = prompt.parse(text)
    assert parsed["sections"]["最终目标"] == "目标 A"
    assert "规则" in parsed["sections"]["硬规则"]
