"""install.sh 的静态回归：set -e 下不能有「函数末尾返回非零」的裸调用。

0.8.8 → 0.8.9 升级时出现过一次真实事故：optional_downloads 里
「Python 运行依赖」那一行是函数的最后几条命令之一，opt_download 对
「已存在」的项返回 1，没有 `|| true` 兜住，函数以 1 结束 → 整个安装
静默退出。日志里只剩「组件清单」最后一行，看起来像安装器跳过了全部步骤。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "install.sh"


def _function_body(src: str, name: str) -> str:
    """按大括号配对取出某个 shell 函数的函数体（忽略字符串里的花括号）"""
    m = re.search(rf"^{re.escape(name)}\s*\(\)\s*\{{", src, re.MULTILINE)
    if not m:
        raise AssertionError(f"install.sh 里找不到函数 {name}")
    depth, i = 0, m.end() - 1
    while i < len(src):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[m.end():i]
        i += 1
    raise AssertionError(f"函数 {name} 的大括号不配对")


class OptionalDownloadsTest(unittest.TestCase):
    def setUp(self):
        self.src = SRC.read_text(encoding="utf-8")
        self.body = _function_body(self.src, "optional_downloads")

    def _call_starts(self) -> list[int]:
        return [m.start() for m in re.finditer(r"^\s*opt_download\b", self.body, re.MULTILINE)]

    def _statement(self, start: int) -> str:
        """从 opt_download 开始，取到该条语句结束（续行符 \\ 之后继续）"""
        out = []
        for line in self.body[start:].splitlines():
            out.append(line)
            if not line.rstrip().endswith("\\"):
                break
        return "\n".join(out)

    def test_chained_case_branches_are_guarded(self):
        """`case ... esac` 的空分支返回 0，但同一条链上的下一段可能返回 1"""
        for start in self._call_starts():
            stmt = self._statement(start)
            if "case " in stmt and "esac" in stmt and "|| true" not in stmt:
                self.fail("opt_download 的参数里串了 case 分支，必须补 `|| true`：\n" + stmt)

    def test_calls_not_last_command_are_guarded(self):
        """非函数尾部的 opt_download 必须有 `|| true`，否则 set -e 会终止安装"""
        for start in self._call_starts():
            stmt = self._statement(start)
            self.assertIn("|| true", stmt,
                          "opt_download 返回 1 时会让 set -e 退出安装：\n" + stmt)


if __name__ == "__main__":
    unittest.main()
