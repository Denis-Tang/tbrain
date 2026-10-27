"""会话命名：按 canvas「会话命名」7 条规则生成确定性名称，不调用大模型。

规则（见 ``执行前流程（最小闭环）.canvas``）：

1. 去客套：删开头固定词表（我想让你 / 帮我 / 请 / 麻烦 / 能不能 / 先 …）。
2. 去噪声：删括号补充、代码块、网址；路径只留文件名。
3. 取第一句（按 ``。？！`` 与换行切分）。
4. ≤20 字直接用；超过则 jieba 分词取关键词，按原文顺序拼接（动词/名词优先）。
5. 去掉与所属项目名重复的词。
6. 参考本项目历史会话名降权“所有会话都有的词”，使名称彼此可区分。
7. 清洗文件名非法字符，末尾加 ``日期-当日序号``。

确定性：同一输入必得同一名称；不读系统时间、不使用随机数。
jieba 在首次需要分词时才加载（测试可 monkeypatch ``_JIEBA`` / ``_load_segmenter``）。
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence

MAX_BASE_CHARS = 20
MIN_BASE_CHARS = 6
MAX_FILENAME_CHARS = 120
_ILLEGAL_FILENAME_CHARS = set('\\/:*?"<>|')
_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# 开头客套/口头词；按长度降序匹配，避免“请”先吃掉“请帮我”。
_POLITE_PREFIXES: tuple[str, ...] = tuple(
    sorted(
        (
            "我想麻烦你",
            "我想请你",
            "我想让你",
            "可不可以帮我",
            "可不可以",
            "能不能帮我",
            "能不能",
            "可以帮我",
            "我想",
            "请你",
            "请帮我",
            "帮我",
            "帮忙",
            "麻烦你",
            "麻烦",
            "请问",
            "请",
            "我想问一下",
            "先",
            "现在",
            "然后",
            "那么",
            "嗯",
            "那个",
            "你好",
        ),
        key=len,
        reverse=True,
    )
)

# 信息量过低的整句（比较前会去掉空白与标点、ASCII 转小写）。
_LOW_INFORMATION_PHRASES = {
    "继续",
    "接着",
    "接着上次",
    "接着来",
    "接着干",
    "继续上次",
    "继续吧",
    "继续工作",
    "继续做",
    "继续干",
    "继续完成",
    "继续弄",
    "继续搞",
    "继续之前",
    "上次",
    "上次的",
    "之前的",
    "之前",
    "前面那个",
    "那个",
    "好",
    "好的",
    "好吧",
    "行",
    "行了",
    "可以",
    "嗯",
    "哦",
    "噢",
    "ok",
    "okay",
    "hi",
    "hello",
    "你好",
    "在吗",
    "收到",
    "开始",
    "开始吧",
    "确认",
    "是的",
    "对",
    "没问题",
    "谢谢",
    "重试",
    "下一步",
    "继续把",
    "接着做",
}

# 只有动作、没有对象时视为低信息。
_ACTION_ONLY_WORDS = (
    "继续",
    "接着",
    "上次",
    "之前",
    "重试",
    "开始",
    "实现",
    "修复",
    "检查",
    "看看",
    "看一下",
    "查一下",
    "改一下",
    "弄一下",
    "优化",
    "整理",
    "处理",
    "分析",
    "写",
    "做",
    "运行",
    "测试",
    "部署",
    "完成",
    "搞",
    "弄",
    "来",
    "去",
    "再",
    "一下",
    "帮我",
    "帮忙",
    "请",
    "吧",
    "呢",
    "啊",
    "哦",
)

# jieba 词性中视为“关键词”的动/名词前缀。
_PRIMARY_FLAGS = ("n", "v", "a")

# 不承载名称信息的高频泛化词（动词/副词），降低其优先级。
_GENERIC_WORDS = {
    "实现",
    "保证",
    "能够",
    "提交",
    "归档",
    "替换",
    "进行",
    "完成",
    "添加",
    "使用",
    "支持",
    "需要",
    "正确",
    "同时",
    "再次",
    "可以",
    "开始",
    "处理",
    "整理",
    "检查",
    "分析",
    "优化",
    "运行",
    "测试",
}

_STOPWORDS = {
    "的",
    "了",
    "着",
    "过",
    "地",
    "得",
    "我",
    "你",
    "您",
    "他",
    "她",
    "它",
    "我们",
    "你们",
    "他们",
    "这",
    "那",
    "这个",
    "那个",
    "这些",
    "那些",
    "这里",
    "那里",
    "什么",
    "怎么",
    "如何",
    "为什么",
    "哪个",
    "哪些",
    "可以",
    "需要",
    "想要",
    "现在",
    "然后",
    "因为",
    "所以",
    "但是",
    "如果",
    "就是",
    "还是",
    "或者",
    "以及",
    "并且",
    "而且",
    "和",
    "与",
    "及",
    "或",
    "请",
    "帮",
    "帮忙",
    "麻烦",
    "嗯",
    "啊",
    "吧",
    "呢",
    "吗",
    "呀",
    "哦",
    "比较",
    "非常",
    "很",
    "太",
    "更",
    "最",
    "能",
    "会",
    "要",
    "把",
    "被",
    "给",
    "对",
    "从",
    "到",
    "在",
    "是",
    "有",
    "没",
    "不",
    "就",
    "都",
    "也",
    "还",
    "又",
    "再",
    "先",
    "等",
    "让",
    "使",
    "用",
    "通过",
    "关于",
    "进行",
    "一个",
    "一些",
    "一种",
    "一样",
    "一直",
    "已经",
    "应该",
    "可能",
    "其实",
    "目前",
    "之后",
    "以后",
    "以前",
}

_FENCE_RE = re.compile(r"```.*?(?:```|\Z)", re.S)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_URL_RE = re.compile(r"(?:https?|ftp)://[^\s，。！？、；：()（）\[\]{}<>\"']+", re.I)
_WIN_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\)[^\s，。！？、；：()（）\[\]{}<>\"']+")
_POSIX_PATH_RE = re.compile(r"(?<![0-9A-Za-z_.-])(?:\.{1,2}[\\/]|/)[A-Za-z0-9_.-]+(?:[\\/][A-Za-z0-9_.-]+)+")
_PAREN_RE = re.compile(r"[（(][^（）()]*[）)]")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_SENTENCE_SPLIT_RE = re.compile(r"[。？！?!\n\r]+")
_EDGE_PUNCT = " \t，。！？、；：,.!?;:~～…—-·•【】[]（）(){}<>《》\"'“”‘’"

# jieba 惰性加载缓存；测试可直接替换或重置。
_JIEBA = None


def _load_segmenter():
    """首次分词时加载 jieba（约 0.4 秒），之后复用。"""
    global _JIEBA
    if _JIEBA is None:
        import jieba.posseg as posseg

        _JIEBA = posseg
    return _JIEBA


def _reset_segmenter() -> None:
    """清空 jieba 缓存（仅测试使用）。"""
    global _JIEBA
    _JIEBA = None


# ---------------------------------------------------------------------------
# 文本清洗


def _strip_polite_prefix(text: str) -> str:
    changed = True
    while changed and text:
        changed = False
        for prefix in _POLITE_PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix):].lstrip(" \t，,。.、：:")
                changed = True
                break
    return text


def _replace_path(match: re.Match[str]) -> str:
    token = match.group(0)
    name = re.split(r"[\\/]", token.rstrip("\\/"))[-1]
    return name or " "


def _clean_message(message: str) -> str:
    text = (message or "").replace("\u3000", " ").replace("\u00a0", " ")
    text = _FENCE_RE.sub(" ", text)
    text = _INLINE_CODE_RE.sub(" ", text)
    text = _URL_RE.sub(" ", text)
    text = _WIN_PATH_RE.sub(_replace_path, text)
    text = _POSIX_PATH_RE.sub(_replace_path, text)
    for _ in range(2):
        text = _PAREN_RE.sub(" ", text)
    text = _strip_polite_prefix(text.strip())
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip().strip(_EDGE_PUNCT)


def _first_sentence(text: str) -> str:
    for chunk in _SENTENCE_SPLIT_RE.split(text):
        chunk = chunk.strip().strip(_EDGE_PUNCT)
        if chunk:
            return chunk
    return ""


def _compact(text: str) -> str:
    """去空白与标点后的紧凑形式，用于低信息判断。"""
    value = re.sub(r"[\s]+", "", text or "")
    value = re.sub(r"[，。！？、；：,.!?;:~～…—\-·•【】\[\]（）(){}<>《》\"'“”‘’]", "", value)
    return value.lower()


def is_low_information(first_message: str) -> bool:
    """首条消息是否信息量过低（纯指令、纯客套、纯标点/数字等）。"""
    cleaned = _clean_message(first_message)
    compact = _compact(cleaned)
    if not compact:
        return True
    if compact in _LOW_INFORMATION_PHRASES:
        return True
    if len(compact) <= 2:
        return True
    if re.fullmatch(r"[\d\W_]+", compact, flags=re.UNICODE):
        return True
    # 只由“继续/实现/检查”等无宾语的动词构成
    remainder = compact
    for word in sorted(_ACTION_ONLY_WORDS, key=len, reverse=True):
        remainder = remainder.replace(word, "")
    remainder = remainder.strip("的了着过和与及或")
    return not remainder


# ---------------------------------------------------------------------------
# 分词与关键词


def _tokenize(text: str) -> list[tuple[str, str]]:
    segmenter = _load_segmenter()
    tokens: list[tuple[str, str]] = []
    try:
        pairs = segmenter.cut(text)
    except Exception:  # 分词器异常时退回逐字，保证确定性
        return [(ch, "n") for ch in text if _CJK_RE.match(ch) or ch.isalnum()]
    for item in pairs:
        word = getattr(item, "word", None)
        flag = getattr(item, "flag", "")
        if word is None:  # 兼容普通 jieba.cut 返回 str
            word, flag = str(item), ""
        word = str(word).strip()
        if word:
            tokens.append((word, flag))
    return tokens


def _meaningful(word: str) -> bool:
    value = word.strip()
    if not value:
        return False
    compact = _compact(value)
    if not compact:
        return False
    if compact in _STOPWORDS:
        return False
    # 中文单字多是虚词/量词（的、层、卡、时），不单独进名称；ASCII 词保留。
    if len(compact) == 1 and _CJK_RE.match(compact):
        return False
    if len(compact) == 1 and not compact.isalnum():
        return False
    return True


def _flag_score(flag: str) -> float:
    if flag.startswith("n"):
        return 2.0
    if flag.startswith("v"):
        return 1.4
    if flag.startswith("a"):
        return 1.2
    if flag[:1] in {"j", "l", "i", "s", "f"}:
        return 1.0
    return 0.7


def _normalize(text: str) -> str:
    return _compact(text)


def _history_weight(history_names: Sequence[str], history_df: dict[str, int]) -> dict[str, float]:
    """历史会话名里出现频率越高的词，权重越低。"""
    total = len([name for name in history_names if str(name or "").strip()])
    if total <= 0:
        return {}
    return {word: count / total for word, count in history_df.items()}


def _trim_token(word: str) -> str:
    """去掉分词结果首尾粘连的虚词（如 jieba 把“改与”并成一个词）。"""
    value = word.strip()
    edge = "的了着过和与及或并而在时吗呢吧啊呀哦"
    while value and value[0] in edge:
        value = value[1:]
    while value and value[-1] in edge:
        value = value[:-1]
    return value


def _join_words(words: Sequence[str]) -> str:
    """中文词直接拼接；含 ASCII 词时用空格分隔，避免英文连成一串。"""
    output = ""
    previous = ""
    for word in words:
        if not word:
            continue
        ascii_word = bool(re.fullmatch(r"[A-Za-z0-9_.+#-]+", word))
        ascii_prev = bool(re.fullmatch(r"[A-Za-z0-9_.+#-]+", previous))
        if output and (ascii_word or ascii_prev):
            output += " "
        output += word
        previous = word
    return output


def _history_terms(history_names: Iterable[str]) -> dict[str, int]:
    counter: dict[str, int] = {}
    for name in history_names:
        text = _strip_suffix(str(name or ""))
        if not text:
            continue
        seen: set[str] = set()
        for raw_word, _flag in _tokenize(text):
            word = _trim_token(raw_word)
            if not _meaningful(word):
                continue
            norm = _normalize(word)
            if norm:
                seen.add(norm)
        for norm in seen:
            counter[norm] = counter.get(norm, 0) + 1
    return counter


def _strip_suffix(name: str) -> str:
    """去掉程序加的 ``-YYYY-MM-DD-序号`` 尾巴，只留提炼部分。"""
    text = name.strip()
    if text.lower().endswith(".md"):
        text = text[:-3]
    return re.sub(r"-\d{4}-\d{2}-\d{2}-\d+$", "", text).strip()


def _keywords(segment: str, project_name: str, history_names: Sequence[str]) -> str:
    tokens = _tokenize(segment)
    project_norm = _normalize(project_name)
    history_df = _history_terms(history_names)
    weights = _history_weight(history_names, history_df)

    candidates: list[tuple[int, str, float]] = []
    seen: set[str] = set()
    for index, (raw_word, flag) in enumerate(tokens):
        word = _trim_token(raw_word)
        if not _meaningful(word):
            continue
        norm = _normalize(word)
        if not norm or norm in seen:
            continue
        if project_norm and (norm == project_norm or (len(norm) >= 2 and norm in project_norm)):
            continue
        seen.add(norm)
        score = _flag_score(flag)
        if _WORD_RE.fullmatch(word):
            # 技术名（SQLite/WAL/hook/Windows…）对区分度贡献大，抬高权重。
            score = max(score, 1.3)
        if norm in _GENERIC_WORDS:
            score -= 0.6
        score -= weights.get(norm, 0.0) * 1.2
        candidates.append((index, word, score))

    if not candidates:
        return ""

    def width(items: list[tuple[int, str, float]]) -> int:
        return sum(len(word) for _idx, word, _score in items)

    # 先只留高分关键词（名词/动词/形容词），预算不够再按分数从低到高丢。
    strong = [item for item in candidates if item[2] >= 1.0]
    kept = list(strong or candidates)
    while kept and width(kept) > MAX_BASE_CHARS:
        # 分数最低的先丢；同分时丢靠后的，尽量保留原文靠前的词。
        worst = min(range(len(kept)), key=lambda i: (kept[i][2], -kept[i][0]))
        del kept[worst]

    # 高分词太少时补足最低可读长度，避免出现“重构”这种过短名称。
    if width(kept) < MIN_BASE_CHARS:
        kept_ids = {item[0] for item in kept}
        for item in candidates:
            if item[0] in kept_ids:
                continue
            if width(kept) + len(item[1]) > MAX_BASE_CHARS:
                continue
            kept.append(item)
            kept_ids.add(item[0])
            if width(kept) >= MIN_BASE_CHARS:
                break

    kept.sort(key=lambda item: item[0])
    return _join_words([word for _idx, word, _score in kept])


def _remove_project_terms(text: str, project_name: str) -> str:
    project = (project_name or "").strip()
    if not project or len(project) < 2:
        return text
    result = re.sub(re.escape(project), " ", text, flags=re.IGNORECASE)
    for word, _flag in _tokenize(project):
        norm = word.strip()
        if len(norm) >= 2 and _normalize(norm):
            result = re.sub(re.escape(norm), " ", result, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", result).strip()


# ---------------------------------------------------------------------------
# 文件名


def sanitize_filename(name: str) -> str:
    """去掉 Windows 非法字符、换行、首尾空格与点；空名退回“未命名”。"""
    text = str(name or "")
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    chars: list[str] = []
    for ch in text:
        if ch in _ILLEGAL_FILENAME_CHARS or ord(ch) < 32:
            chars.append(" ")
        else:
            chars.append(ch)
    text = "".join(chars)
    text = re.sub(r"\s+", " ", text)
    text = text.strip()
    while text and text[-1] in " .":
        text = text[:-1]
    while text and text[0] in " .":
        text = text[1:]
    if len(text) > MAX_FILENAME_CHARS:
        text = text[:MAX_FILENAME_CHARS].rstrip(" .")
    if not text:
        return "未命名"
    if text.split(".")[0].upper() in _RESERVED_NAMES:
        text = "_" + text
    return text


def _date_sequence_suffix(date: str, sequence: int) -> str:
    day = str(date or "").strip()
    try:
        seq = int(sequence)
    except (TypeError, ValueError):
        seq = 0
    return f"{day}-{seq}" if day else str(seq)


# ---------------------------------------------------------------------------
# 公开接口


def temp_name(date: str, sequence: int) -> str:
    """低信息首条消息的临时名：``未命名-YYYY-MM-DD-1``。"""
    return sanitize_filename(f"未命名-{_date_sequence_suffix(date, sequence)}")


def resume_name(old_name: str, date: str, sequence: int) -> str:
    """接续后的名称：``<old> 续 YYYY-MM-DD-1``。"""
    old = _strip_suffix(str(old_name or ""))
    old = sanitize_filename(old) if old else ""
    base = f"{old} 续 {_date_sequence_suffix(date, sequence)}" if old else f"续 {_date_sequence_suffix(date, sequence)}"
    return sanitize_filename(base)


def session_name(
    first_message: str,
    project_name: str,
    history_names: list[str],
    date: str,
    sequence: int,
) -> str:
    """按 7 条规则生成会话名（``<提炼>-<日期>-<序号>``）。"""
    if is_low_information(first_message):
        return temp_name(date, sequence)

    cleaned = _clean_message(first_message)
    segment = _first_sentence(cleaned) or cleaned
    segment = segment.strip().strip(_EDGE_PUNCT)
    # 名称里不放句中顿号/逗号，改用空格分隔，读起来更接近短语。
    segment = re.sub(r"[，、；：,;:]+", " ", segment)
    segment = re.sub(r"\s+", " ", segment).strip()
    if not segment:
        return temp_name(date, sequence)

    if len(segment) <= MAX_BASE_CHARS:
        base = _remove_project_terms(segment, project_name)
        base = base.strip().strip(_EDGE_PUNCT)
        base = re.sub(r"^[的了着过和与及或]+", "", base).strip()
        if not base:
            base = segment
    else:
        base = _keywords(segment, project_name, history_names)
        if not base:
            base = _first_sentence(_remove_project_terms(segment, project_name))
        if not base:
            base = segment[:MAX_BASE_CHARS]

    base = sanitize_filename(base)
    if not base or base == "未命名":
        base = "会话"
    suffix = _date_sequence_suffix(date, sequence)
    return sanitize_filename(f"{base}-{suffix}")
