"""输出侧敏感词替换：按配置的词表做字面替换

只作用于机器人发出去的文字（AI 回复与固定命令输出），不改用户输入。
匹配对大小写与全角/半角不敏感（「Secret」「ＳＥＣＲＥＴ」都算同一个词），
其余字符原样保留。

默认不启用（词表为空），是一张纯配置驱动的保守开关：
- 单字词容易误伤正常句子，长度不足 2 的词直接忽略
- 词表按长度从长到短替换，避免「bad」先把「badword」拆掉
"""
from __future__ import annotations

import re
import unicodedata

MAX_WORDS = 100        # 词表条数上限，防止配置写炸
MAX_WORD_CHARS = 30    # 单个词长度上限
DEFAULT_MASK = "***"

_FULLWIDTH_OFFSET = 0xFEE0
_FULLWIDTH_MIN, _FULLWIDTH_MAX = 0x21, 0x7E


def normalize_words(words) -> tuple[str, ...]:
    """清洗词表：去空白、去重、丢掉单字词与超长词（保持原有顺序）"""
    if words is None:
        return ()
    if isinstance(words, str):
        # 允许一行写多个，用逗号/顿号/换行分隔
        words = re.split(r"[,\n，、;；]+", words)
    try:
        items = list(words)
    except TypeError:
        return ()
    out: list[str] = []
    for raw in items:
        word = unicodedata.normalize("NFKC", str(raw or "")).strip()
        if len(word) < 2 or len(word) > MAX_WORD_CHARS:
            continue
        if word not in out:
            out.append(word)
        if len(out) >= MAX_WORDS:
            break
    return tuple(out)


def _char_class(ch: str) -> str:
    """单个字符的匹配片段：ASCII 字母数字同时覆盖大小写与全角形式"""
    norm = unicodedata.normalize("NFKC", ch)
    variants = {norm}
    if len(norm) == 1 and norm.isascii() and norm.isalnum():
        for case in (norm.lower(), norm.upper()):
            if len(case) == 1:
                variants.add(case)
                code = ord(case)
                if _FULLWIDTH_MIN <= code <= _FULLWIDTH_MAX:
                    variants.add(chr(code + _FULLWIDTH_OFFSET))
    parts = sorted(variants, key=len)
    if len(parts) == 1:
        return re.escape(parts[0])
    return "[" + "".join(re.escape(p) for p in parts) + "]"


def compile_word(word: str) -> re.Pattern:
    """把一个词编译成大小写、全半角都不敏感的字面匹配"""
    norm = unicodedata.normalize("NFKC", word)
    return re.compile("".join(_char_class(ch) for ch in norm))


class Redactor:
    """输出脱敏器：apply() 是幂等的字面替换，空词表时原样返回"""

    def __init__(self, words=(), mask: str = DEFAULT_MASK):
        self.words = normalize_words(words)
        self.mask = str(mask or DEFAULT_MASK).strip() or DEFAULT_MASK
        # 长词优先，避免短词先把长词拆碎
        self._rules = [(compile_word(w), w) for w in
                       sorted(self.words, key=len, reverse=True)]

    @property
    def enabled(self) -> bool:
        return bool(self._rules)

    def apply(self, text: str) -> str:
        out = str(text or "")
        if not out or not self._rules:
            return out
        for rx, _word in self._rules:
            out = rx.sub(self.mask, out)
        return out


def redact_text(text: str, words=(), mask: str = DEFAULT_MASK) -> str:
    """一次性调用：适合不方便持有 Redactor 的地方"""
    return Redactor(words, mask).apply(text)
