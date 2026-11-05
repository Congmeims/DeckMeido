"""config.toml 的安全写入。

历史问题：设置页保存提示词模板时按行正则替换 `key = ...`，三引号多行值被截断；
字符串序列化只转义了双引号，模板里的换行/反斜杠直接写出非法 TOML。配置坏一次，
后端下次启动就整个读不起来，插件还会把它误报成「没有 token」。

这里统一做三件事：
1. 任意值序列化成单行 basic string（换行写 `\\n`），不再产生多行字面量；
2. 替换时按字符区间吃掉旧值的完整范围（含三引号多行形式），不按行截断；
3. 整份文本先过 `tomllib.loads`，通过才备份 + 临时文件原子替换。
"""
from __future__ import annotations

import os
import re
import shutil
import time
import tomllib
from pathlib import Path


class ConfigWriteError(ValueError):
    """配置写入失败：消息里带着 tomllib 的行号/列号，接口原样回给前端"""


def _toml_string(value: str) -> str:
    """任意文本 → 单行 basic string（换行写成 \\n，不落多行字面量）"""
    out = []
    for ch in str(value):
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7f:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return _toml_string(value)


def _string_spans(text: str) -> list[tuple[int, int]]:
    """所有跨行字符串（\"\"\"…\"\"\" / '''…'''）的字符区间

    模板正文里可能出现 `[agent]`、`token =` 这类字面文本；按行正则找表头
    / 键名时必须先跳过这些区间，否则会把正文当成真配置改掉。
    """
    spans: list[tuple[int, int]] = []
    pos = 0
    pat = re.compile(r'("""|\'\'\')')
    while True:
        m = pat.search(text, pos)
        if not m:
            return spans
        q, start, i = m.group(1), m.start(), m.end()
        while True:
            k = text.find(q, i)
            if k < 0:
                spans.append((start, len(text)))
                return spans
            # """ 里的 \" 是转义引号，不算结束（\\" 这种双反斜杠则算）
            if q == '"""' and text[k - 1] == "\\" and (k - 2 < 0 or text[k - 2] != "\\"):
                i = k + 1
                continue
            spans.append((start, k + 3))
            pos = k + 3
            break


def _in_spans(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(a <= pos < b for a, b in spans)


def _toml_value_end(text: str, i: int) -> int:
    """从 `=` 之后开始，吃掉一个 TOML 值的完整区间

    多行字符串要一直读到结尾的三引号，不能只读到行尾；数组/内联表按括号
    深度跨行配对。模板里的正文再长也不会被截断。
    """
    n = len(text)
    while i < n and text[i] in " \t":
        i += 1
    if text.startswith('"""', i) or text.startswith("'''", i):
        q = text[i:i + 3]
        j = i + 3
        while True:
            k = text.find(q, j)
            if k < 0:
                return n
            if q == '"""' and text[k - 1] == "\\" and (k - 2 < 0 or text[k - 2] != "\\"):
                j = k + 1
                continue
            return k + 3
    if text[i:i + 1] in ('"', "'"):
        q = text[i]
        j = i + 1
        while j < n:
            if q == '"' and text[j] == "\\":
                j += 2
                continue
            if text[j] == q:
                return j + 1
            if text[j] == "\n":
                return j
            j += 1
        return n
    if text[i:i + 1] in ("[", "{"):
        depth = 0
        j = i
        while j < n:
            ch = text[j]
            if ch in "\"'":
                j = _toml_value_end(text, j)
                continue
            if ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
                if depth == 0:
                    return j + 1
            j += 1
        return n
    j = text.find("\n", i)
    return n if j < 0 else j


def _section_range(text: str, section: str,
                   spans: list[tuple[int, int]] | None = None) -> tuple[int, int] | None:
    """[section] 表头之后、下一个表头之前的字符区间（不含表头那一行）"""
    spans = _string_spans(text) if spans is None else spans
    hdr = re.compile(rf"^\[{re.escape(section)}\]\s*(?:#.*)?$", re.M)
    start = None
    for m in hdr.finditer(text):
        if not _in_spans(m.start(), spans):
            start = m.end()
            break
    if start is None:
        return None
    nxt = re.compile(r"^\[[^\[\]\n]+\]\s*(?:#.*)?$", re.M)
    end = len(text)
    for m in nxt.finditer(text, start):
        if not _in_spans(m.start(), spans):
            end = m.start()
            break
    return start, end


def _toml_set(text: str, section: str, key: str, value) -> str:
    """在 [section] 内改写 key = value，没有这条就追加到该段末尾"""
    kv_line = f"{key} = {_toml_value(value)}"
    spans = _string_spans(text)
    rng = _section_range(text, section, spans)
    if rng is None:
        return text.rstrip("\n") + f"\n\n[{section}]\n{kv_line}\n"
    start, end = rng
    hit = None
    key_pat = re.compile(rf"^[ \t]*{re.escape(key)}[ \t]*=", re.M)
    for m in key_pat.finditer(text, start, end):
        if not _in_spans(m.start(), spans):
            hit = m
            break
    if hit is not None:
        value_end = _toml_value_end(text, hit.end())
        return text[: hit.end()] + " " + _toml_value(value) + text[value_end:]
    # 追加到段尾：段尾紧贴着下一个表头时补一个换行，别把表头挤到同一行
    prefix = "" if text[:end].endswith("\n") else "\n"
    return text[:end] + prefix + kv_line + "\n" + text[end:]


def _write_atomic(path: Path, text: str) -> None:
    """临时文件 + os.replace：写到一半断电也不会留下半截 config.toml"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    if os.name != "nt":
        # 配置里有 API Key 等敏感项，Linux 下只给属主读写（Windows 忽略权限位）
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _backup(path: Path) -> None:
    """落盘前留一份最新备份；只保留这一份，避免越攒越多"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, bak)
    for old in path.parent.glob(f"{path.name}.bak-*"):
        if old != bak:
            try:
                old.unlink()
            except OSError:
                pass


def write_config(path: Path, updates: dict, apply=None) -> None:
    """把 {section: {key: value}} 写回 path（保留注释），成功后调用 apply(parsed)

    值为 dict 时当作子表处理（写进 [section.key]），例如 persona.style。
    整份文本先过 tomllib 校验，失败就原样不动并把错误抛给调用方。
    """
    original = path.read_text(encoding="utf-8")
    text = original
    for section, kv in updates.items():
        for key, value in kv.items():
            if isinstance(value, dict):
                sub = f"{section}.{key}"
                # 先清掉旧版本把整个子表写成一行字符串留下的 `key = "{...}"`：
                # 它会让 [section.key] 表头变成「重复定义」，整个配置解析失败。
                # 只在父段内删 —— 全文替换会误伤子表里的同名键。
                spans = _string_spans(text)
                rng = _section_range(text, section, spans)
                if rng is None:
                    # 没有父段就整段现建，之后直接写子表头
                    text = text.rstrip("\n") + f"\n\n[{section}]\n"
                else:
                    start, end = rng
                    hit = None
                    pat = re.compile(rf"^[ \t]*{re.escape(key)}[ \t]*=", re.M)
                    for m in pat.finditer(text, start, end):
                        if not _in_spans(m.start(), spans):
                            hit = m
                            break
                    if hit is not None:
                        line_start = text.rfind("\n", 0, hit.start()) + 1
                        value_end = _toml_value_end(text, hit.end())
                        text = text[:line_start] + text[value_end:].lstrip("\n")
                # 子表不存在时先建一个空表头，下面逐项往里写
                if not re.search(rf"^\[{re.escape(sub)}\]\s*$", text, re.M):
                    text = text.rstrip("\n") + f"\n\n[{sub}]\n"
                for sub_key, sub_value in value.items():
                    text = _toml_set(text, sub, sub_key, sub_value)
            else:
                text = _toml_set(text, section, key, value)
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigWriteError(
            f"配置校验失败，本次没有写入（{exc}）。文件仍是修改前的版本"
        ) from exc
    if text != original:
        _backup(path)
        _write_atomic(path, text)
    if apply is not None:
        apply(parsed)
