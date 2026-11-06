"""固定命令菜单与记忆清洗的纯函数测试

覆盖：命令解析、未知命令回落聊天、别名整句匹配、权限拒绝、
危险动作确认分级、以及写记忆前的 @提及/昵称清洗。
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import menu, memory


class ParseTest(unittest.TestCase):
    def test_slash_after_prefix(self):
        self.assertEqual(menu.parse("meido /status", "meido").kind, "status")

    def test_slash_case_insensitive(self):
        self.assertEqual(menu.parse("Meido /DEPS", "meido").kind, "deps")

    def test_slash_with_argument(self):
        got = menu.parse("meido /shot 现在", "meido")
        self.assertEqual((got.kind, got.text), ("shot", "现在"))

    def test_unknown_slash_falls_back_to_chat(self):
        got = menu.parse("meido /foo bar", "meido")
        self.assertEqual((got.kind, got.text), ("chat", "/foo bar"))

    def test_prefix_only_is_empty_chat(self):
        self.assertEqual(menu.parse("meido", "meido").kind, "chat")

    def test_missing_prefix_returns_none(self):
        self.assertIsNone(menu.parse("随便聊聊", "meido"))

    def test_prefix_for_default(self):
        self.assertEqual(menu.prefix_for(""), menu.DEFAULT_PREFIX)


class AliasTest(unittest.TestCase):
    def test_alias_exact(self):
        self.assertEqual(menu.match_alias("检查依赖"), ("检查依赖", "deps"))

    def test_alias_with_polite_wrapper(self):
        self.assertEqual(menu.match_alias("麻烦截个屏吧"), ("截个屏", "shot"))

    def test_alias_with_punctuation(self):
        self.assertEqual(menu.match_alias("截屏！"), ("截屏", "shot"))

    def test_long_sentence_not_alias(self):
        self.assertIsNone(menu.match_alias("帮我看看这个画面里的人物在说什么"))

    def test_unrelated_not_alias(self):
        self.assertIsNone(menu.match_alias("今天天气不错"))


class PermissionTest(unittest.TestCase):
    def test_admin_allowed(self):
        self.assertTrue(menu.authorize("status", is_admin=True))

    def test_guest_rejected(self):
        self.assertFalse(menu.authorize("status", is_admin=False))

    def test_admin_allowed_even_in_group(self):
        # 主人豁免：群里也直接放行，不再有群指令开关
        self.assertTrue(menu.authorize("deps", is_admin=True))

    def test_unknown_kind_rejected(self):
        self.assertFalse(menu.authorize("no_such", is_admin=True))

    def test_unknown_slash_detected(self):
        self.assertEqual(menu.unknown_slash("/nope"), "/nope")
        self.assertEqual(menu.unknown_slash("meido /nope a b"), "")
        self.assertEqual(menu.unknown_slash("/shot"), "")
        self.assertEqual(menu.unknown_slash("/status 参数"), "")
        self.assertEqual(menu.unknown_slash("今天吃点啥"), "")


class ConfirmTest(unittest.TestCase):
    SOFT = menu.Spec("restart", ("restart",), dangerous=True)
    HARD = menu.Spec("wipe", ("wipe",), dangerous=True, hard=True)
    SAFE = menu.Spec("status", ("status",))

    def test_safe_command_needs_no_confirm(self):
        self.assertFalse(menu.needs_confirm(self.SAFE))
        self.assertEqual(menu.confirm_decision(self.SAFE, "随便"), "execute")

    def test_soft_confirm(self):
        for text in ("确认", "好", "确定。"):
            self.assertEqual(menu.confirm_decision(self.SOFT, text), "execute")
        self.assertEqual(menu.confirm_decision(self.SOFT, "算了"), "cancel")

    def test_hard_confirm(self):
        self.assertEqual(menu.confirm_decision(self.HARD, "确认执行"), "execute")
        self.assertEqual(menu.confirm_decision(self.HARD, "确认"), "ask_hard")
        self.assertEqual(menu.confirm_decision(self.HARD, "明天再说"), "cancel")

    def test_none_spec_is_safe(self):
        self.assertFalse(menu.needs_confirm(None))
        self.assertEqual(menu.confirm_decision(None, "x"), "execute")


class HelpTest(unittest.TestCase):
    def test_help_lists_commands(self):
        text = menu.help_text("meido")
        self.assertIn("/deps", text)
        self.assertIn("检查依赖状态（只检测，不安装）", text)
        self.assertIn("/status", text)

    def test_help_has_no_install_entry(self):
        text = menu.help_text("meido")
        self.assertNotIn("/install", text)
        self.assertNotIn("安装依赖", text)


class CleanTest(unittest.TestCase):
    def test_strips_bot_mention(self):
        got = memory.clean_text("@meido 帮我记住明天买菜", names=("meido",))
        self.assertEqual(got, "帮我记住明天买菜")

    def test_strips_cq_at_code(self):
        got = memory.clean_text("[CQ:at,qq=123] 明天开会", names=("meido",))
        self.assertEqual(got, "明天开会")

    def test_keeps_other_mentions(self):
        got = memory.clean_text("@张三 说明天要来", names=("meido",))
        self.assertEqual(got, "@张三 说明天要来")

    def test_keeps_email_address(self):
        got = memory.clean_text("把结果发到 a@b.com", names=("meido", "b"))
        self.assertEqual(got, "把结果发到 a@b.com")

    def test_strips_sender_label(self):
        got = memory.clean_text("小明：明天提醒我开会", names=("小明",))
        self.assertEqual(got, "明天提醒我开会")

    def test_strips_sender_label_per_line(self):
        got = memory.clean_text("小明：第一句\n小明：第二句", names=("小明",))
        self.assertEqual(got, "第一句\n第二句")

    def test_collapses_spaces_and_leading_punct(self):
        got = memory.clean_text("@meido,  帮我看看", names=("meido",))
        self.assertEqual(got, "帮我看看")


class MemoryAddTest(unittest.TestCase):
    def test_add_cleans_before_write(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "memory.json"
            with mock.patch.object(memory, "_path", return_value=path):
                item = memory.add("note", "@meido [CQ:at,qq=1] 记得下班带伞",
                                  names=("meido",))
            self.assertEqual(item["text"], "记得下班带伞")


if __name__ == "__main__":
    unittest.main()
