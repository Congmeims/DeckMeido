"""提示词集中存放处

这些文本原来散在各处（main.py 的 build_system_prompt、_mode_block 里几十行
f-string 拼接），改一句话要在 Python 里翻很久，用户也看不出模型每轮实际读到了
什么。现在统一放进同目录的 md 文件，打开就能看。

分工：
  - 这里的 md 是「模板」：文字固定，改完重启后端才生效
  - 人设（meidosoul.json）、权限模式、长期记忆是「数据」：每次拼系统提示时
    重新读取，保存设置后立刻生效，不受本模块缓存影响

加载策略：模块 import 时读一次，之后全是内存字符串。改动频率低，不值得每轮
去碰磁盘；需要重新加载就重启后端。

分段写法（在 md 文件里）：

    <!-- @part: 名字 -->
    正文
    <!-- /@part -->

part() 按名字取一段，取不到返回空串。md 文件里其余内容（比如开头的说明
注释）不参与拼接，只给人看。
"""

from __future__ import annotations

import re
from pathlib import Path

_DIR = Path(__file__).resolve().parent

# part 块：从 `<!-- @part: x -->` 到 `<!-- /@part -->`。正文原样保留（含段首空行），
# 因为 mode_* 那两段靠段首的两个换行和系统提示主体隔开。
# 结束标记允许紧贴正文（写在行尾），这样正文本身的结尾空白不会被吃掉。
_PART_RE = re.compile(
    r"<!--\s*@part:\s*([\w.-]+)\s*-->\n(.*?)[ \t]*<!--\s*/@part\s*-->",
    re.S,
)


def _read(name: str) -> str:
    return (_DIR / name).read_text(encoding="utf-8")


def _parse(text: str) -> dict[str, str]:
    return {m.group(1): m.group(2) for m in _PART_RE.finditer(text)}


_SYSTEM = _read("system.md")
_MODES = _parse(_read("modes.md"))
_RUNTIME = _parse(_read("runtime.md"))


def system_template() -> str:
    """系统提示主体模板（含 {name} {traits} {root} {home} {mode_desc} {vision_note}）"""
    return _SYSTEM


def part(name: str) -> str:
    """取一段可切换/零碎的提示词，取不到返回空串

    modes.md 和 runtime.md 共用一个命名空间，名字不要重。
    """
    return _MODES.get(name) or _RUNTIME.get(name) or ""
