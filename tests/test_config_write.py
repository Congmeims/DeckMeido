"""config.toml 写入回归：多行/引号/反斜杠模板不能再把配置写坏。

只依赖标准库，Windows / Linux 都能跑：
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.tomlcfg import ConfigWriteError, write_config  # noqa: E402

BASE = """# 头部注释要保住
[server]
port = 18765
token = "abcde"

[agent]
model = "m1"
ask_template = \"\"\"看我卡在哪一步…
第二行，里面有 [agent] 和 token = 这样的字面文本
\"\"\"
translate_template = \"\"\"把画面上…
\"\"\"

[persona]
style = "friendly"
"""


class ConfigWriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.toml"
        self.path.write_text(BASE, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def read(self) -> str:
        return self.path.read_text(encoding="utf-8")

    def test_multiline_quotes_backslash_two_writes(self):
        """含换行、双引号、反斜杠的模板写两次后仍能读回，且值一模一样"""
        first = '第一行：我卡在 "最终BOSS" 了\n第二行：路径 C:\\DeckMeido\\server\n\t制表符'
        write_config(self.path, {"agent": {"ask_template": first}})
        parsed = tomllib.loads(self.read())
        self.assertEqual(parsed["agent"]["ask_template"], first)
        self.assertNotIn('ask_template = """', self.read())

        second = '换一段\n带 "引号" 和反斜杠 C:\\tmp\\x\n结束'
        write_config(self.path, {"agent": {"ask_template": second}})
        parsed = tomllib.loads(self.read())
        self.assertEqual(parsed["agent"]["ask_template"], second)
        # 第二次写入不能把第一次的内容或别的键带歪
        self.assertEqual(parsed["agent"]["translate_template"], "把画面上…\n")
        self.assertEqual(parsed["agent"]["model"], "m1")
        self.assertEqual(parsed["server"]["token"], "abcde")
        self.assertEqual(parsed["persona"]["style"], "friendly")
        self.assertIn("# 头部注释要保住", self.read())

    def test_other_keys_untouched(self):
        """改写一个键时，别的段和注释原样保留"""
        before = tomllib.loads(BASE)
        write_config(self.path, {"agent": {"ask_template": "单行模板"}})
        after = tomllib.loads(self.read())
        before["agent"]["ask_template"] = "单行模板"
        self.assertEqual(after, before)

    def test_missing_key_appended_to_section(self):
        """段内没有这个键时追加到段尾，且脚本不炸"""
        write_config(self.path, {"agent": {"with_shot": True}})
        parsed = tomllib.loads(self.read())
        self.assertIs(parsed["agent"]["with_shot"], True)
        self.assertIn("[persona]", self.read())

    def test_invalid_write_keeps_original(self):
        """校验不过就不落盘：原文件字节不变，也不产生备份"""
        original = self.read()
        with self.assertRaises(ConfigWriteError):
            write_config(self.path, {"agent": {"bad key": "x"}})
        self.assertEqual(self.read(), original)
        self.assertEqual(list(self.path.parent.glob("config.toml.bak-*")), [])
        self.assertFalse((self.path.parent / "config.toml.tmp").exists())

    def test_backup_kept_single_latest(self):
        """成功写入会留一份备份，且不会越攒越多"""
        write_config(self.path, {"agent": {"ask_template": "a"}})
        baks = list(self.path.parent.glob("config.toml.bak-*"))
        self.assertEqual(len(baks), 1)
        # 同一秒内再写：旧备份被删，只剩最新一份
        write_config(self.path, {"agent": {"ask_template": "b"}})
        baks = list(self.path.parent.glob("config.toml.bak-*"))
        self.assertEqual(len(baks), 1)
        tomllib.loads(baks[0].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
