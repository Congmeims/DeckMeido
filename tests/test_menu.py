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

    def test_prefix_only_is_none(self):
        # 只喊昵称、没跟空格但紧贴着别的字：不是叫机器人
        self.assertIsNone(menu.parse("meido你好", "meido"))
        # 单独喊昵称 / 昵称 + 空白：算叫了机器人，只是空闲聊
        self.assertEqual(menu.parse("meido", "meido").kind, "chat")
        self.assertEqual(menu.parse("meido  ", "meido").kind, "chat")

    def test_missing_prefix_returns_none(self):
        self.assertIsNone(menu.parse("随便聊聊", "meido"))

    def test_prefix_for_default(self):
        self.assertEqual(menu.prefix_for(""), menu.DEFAULT_PREFIX)

    def test_zh_command_after_prefix(self):
        # 「昵称 + 空格 + 中文指令」直接执行，不必带反斜杠
        self.assertEqual(menu.parse("露露 菜单", "露露").kind, "help")
        self.assertEqual(menu.parse("露露 截屏", "露露").kind, "shot")
        self.assertEqual(menu.parse("露露 状态", "露露").kind, "status")
        self.assertEqual(menu.parse("露露 停", "露露").kind, "cancel")

    def test_zh_command_with_argument(self):
        got = menu.parse("露露 清空记忆 群", "露露")
        self.assertEqual((got.kind, got.text), ("memory_clear", "群"))

    def test_zh_non_command_is_chat(self):
        # 昵称后面不是指令词：按闲聊处理
        got = menu.parse("露露 今天天气不错", "露露")
        self.assertEqual((got.kind, got.text), ("chat", "今天天气不错"))

    def test_zh_command_requires_space(self):
        # 昵称后面没空白：`露露你好` 是普通聊天，不算叫机器人
        self.assertIsNone(menu.parse("露露你好", "露露"))
        self.assertEqual(menu.parse("露露  菜单", "露露").kind, "help")


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

    def test_memory_slash_and_clear(self):
        self.assertEqual(menu.match_slash("/memory").kind, "memory")
        self.assertEqual(menu.match_slash("/mem group").kind, "memory")
        # /memory clear 是危险动作，参数要原样带给确认后的 handler
        got = menu.match_slash("/memory clear group")
        self.assertEqual((got.kind, got.text), ("memory_clear", "group"))
        got = menu.match_slash("/memory 清空 人")
        self.assertEqual((got.kind, got.text), ("memory_clear", "人"))
        # 只是查看某个 scope，不能误判成清空
        self.assertEqual(menu.match_slash("/memory 群").kind, "memory")
        self.assertEqual(menu.match_slash("/memory private").kind, "memory")

    def test_memory_clear_permission_and_danger(self):
        self.assertTrue(menu.authorize("memory", is_admin=True))
        self.assertTrue(menu.authorize("memory_clear", is_admin=True))
        # 群友不能查看/清空群记忆：权限体系和其它固定命令一致
        self.assertFalse(menu.authorize("memory", is_admin=False))
        self.assertFalse(menu.authorize("memory_clear", is_admin=False))
        self.assertTrue(menu.needs_confirm(menu.spec_for("memory_clear")))

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
        self.assertIn("查依赖", text)
        self.assertIn("检查依赖状态（只检测，不安装）", text)
        self.assertIn("状态", text)
        self.assertIn("meido 菜单", text)

    def test_help_has_no_slash_commands(self):
        # 用户可见文案只留中文指令，不再出现英文反斜杠指令
        text = menu.help_text("露露")
        for name in menu.SLASH_MAP:
            self.assertNotIn("/" + name, text)
        self.assertNotIn("/", text)

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


class RemoteDangerTest(unittest.TestCase):
    """QQ 远程额外高危命令：卸载 / 清理包缓存 / 自动清理"""

    def test_uninstall_commands(self):
        for cmd in ("flatpak uninstall org.example.App",
                    "flatpak remove org.example.App",
                    "snap remove foo",
                    "brew uninstall wget",
                    "rpm -e pkg"):
            self.assertTrue(menu.remote_command_danger(cmd), cmd)

    def test_clean_and_autoremove(self):
        for cmd in ("pacman -Sc", "sudo pacman -Scc", "apt autoremove",
                    "sudo apt-get autoremove"):
            self.assertTrue(menu.remote_command_danger(cmd), cmd)

    def test_nested_shell_is_checked(self):
        self.assertTrue(menu.remote_command_danger(
            "sh -c 'flatpak uninstall org.example.App'"))

    def test_read_only_commands_are_normal(self):
        for cmd in ("flatpak list", "pacman -Qi foo", "snap list",
                    "systemctl status foo", "ls -la", "apt list --installed"):
            self.assertEqual(menu.remote_command_danger(cmd), "", cmd)


if __name__ == "__main__":
    unittest.main()
