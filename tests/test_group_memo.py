"""群常识只读缓存（server/groupmemo.py）与相关配置/菜单的单元测试。

重点：按群隔离、TTL 到点失效、条数与字数上限、停用条件，
以及「只注入群友只读轮次、不落进会话历史」的衔接方式。
"""
from __future__ import annotations

import unittest

from server import groupmemo, menu, onebot


class FakeClock:
    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class MemoTest(unittest.TestCase):
    def make(self, **kw):
        clock = FakeClock()
        kw.setdefault("size", 20)
        kw.setdefault("ttl", 300.0)
        kw.setdefault("max_chars", 400)
        return groupmemo.GroupMemo(clock=clock, **kw), clock

    def test_prefix_has_header_and_lines(self):
        memo, _ = self.make()
        memo.remember("20001", "张三", "今天开黑吗")
        text = memo.prefix("20001")
        self.assertIn(groupmemo.HEADER, text)
        self.assertIn(groupmemo.NOTE, text)
        self.assertIn("- 张三：今天开黑吗", text)
        self.assertTrue(text.endswith(groupmemo.FOOTER))

    def test_empty_group_or_text_not_recorded(self):
        memo, _ = self.make()
        self.assertFalse(memo.remember("20001", "张三", "   "))
        self.assertFalse(memo.remember("", "张三", "你好"))
        self.assertEqual(memo.prefix("20001"), "")
        self.assertEqual(memo.prefix("20002"), "")

    def test_keeps_only_latest_size(self):
        memo, _ = self.make(size=20)
        for i in range(1, 26):
            memo.remember("20001", "张三", f"第{i}条")
        text = memo.prefix("20001")
        # 只留最近 20 条：1~5 已被挤掉，6~25 在
        self.assertNotIn("第1条", text)
        self.assertNotIn("第5条", text)
        self.assertIn("第6条", text)
        self.assertIn("第25条", text)

    def test_ttl_expires_at_deadline(self):
        memo, clock = self.make(ttl=300.0)
        memo.remember("20001", "张三", "五分钟内")
        clock.now = 299.0
        self.assertIn("五分钟内", memo.prefix("20001"))
        clock.now = 300.0
        self.assertEqual(memo.prefix("20001"), "")
        # 全过期后也不会冒出旧内容
        memo.remember("20001", "张三", "新消息")
        self.assertNotIn("五分钟内", memo.prefix("20001"))
        self.assertIn("新消息", memo.prefix("20001"))

    def test_groups_are_isolated(self):
        memo, _ = self.make()
        memo.remember("20001", "甲", "A 群的秘密")
        memo.remember("20002", "乙", "B 群的日常")
        a = memo.prefix("20001")
        b = memo.prefix("20002")
        self.assertIn("A 群的秘密", a)
        self.assertNotIn("B 群的日常", a)
        self.assertIn("B 群的日常", b)
        self.assertNotIn("A 群的秘密", b)
        self.assertEqual(memo.prefix("20003"), "")

    def test_clear_only_current_group(self):
        memo, _ = self.make()
        memo.remember("20001", "甲", "一")
        memo.remember("20001", "甲", "二")
        memo.remember("20002", "乙", "三")
        self.assertEqual(memo.clear("20001"), 2)
        self.assertEqual(memo.prefix("20001"), "")
        self.assertEqual(memo.clear("20001"), 0)
        self.assertIn("三", memo.prefix("20002"))

    def test_disabled_when_size_or_ttl_zero(self):
        for kwargs in ({"size": 0}, {"ttl": 0}):
            memo, _ = self.make(**kwargs)
            self.assertFalse(memo.enabled)
            self.assertFalse(memo.remember("20001", "甲", "内容"))
            self.assertEqual(memo.prefix("20001"), "")

    def test_message_is_flattened_and_truncated(self):
        memo, _ = self.make()
        memo.remember("20001", "甲", "第一行\n\n第二行\t带空白")
        self.assertIn("第一行 第二行 带空白", memo.prefix("20001"))
        memo.remember("20001", "甲", "长" * 200)
        text = memo.prefix("20001")
        self.assertNotIn("长" * (groupmemo.MAX_ITEM_CHARS + 1), text)

    def test_max_chars_drops_oldest_first(self):
        memo, _ = self.make(max_chars=20)
        for i in range(1, 6):
            memo.remember("20001", "甲", f"消息{i}")
        text = memo.prefix("20001")
        self.assertIn("消息5", text)
        self.assertIn("消息4", text)
        self.assertNotIn("消息1", text)
        self.assertNotIn("消息2", text)


class MemoConfigTest(unittest.TestCase):
    def test_defaults_off(self):
        cfg = onebot.parse_config({})
        self.assertFalse(cfg.group_memo)
        self.assertEqual(cfg.group_memo_size, onebot.DEFAULT_GROUP_MEMO_SIZE)
        self.assertEqual(cfg.group_memo_ttl, onebot.DEFAULT_GROUP_MEMO_TTL)
        self.assertEqual(cfg.group_memo_max_chars, onebot.DEFAULT_GROUP_MEMO_MAX_CHARS)

    def test_custom_values_parsed(self):
        cfg = onebot.parse_config({
            "group_memo": "true", "group_memo_size": "30",
            "group_memo_ttl": "120", "group_memo_max_chars": 500,
        })
        self.assertTrue(cfg.group_memo)
        self.assertEqual(cfg.group_memo_size, 30)
        self.assertEqual(cfg.group_memo_ttl, 120.0)
        self.assertEqual(cfg.group_memo_max_chars, 500)

    def test_zero_max_chars_falls_back(self):
        cfg = onebot.parse_config({"group_memo_max_chars": 0})
        self.assertEqual(cfg.group_memo_max_chars, onebot.DEFAULT_GROUP_MEMO_MAX_CHARS)


class ConfigureRebuildTest(unittest.TestCase):
    def make_bot(self):
        return onebot.OneBotClient(
            runner=None, screenshot=None, status_provider=None,
            nickname=lambda: "meido")

    def test_config_change_drops_old_cache(self):
        bot = self.make_bot()
        bot.configure({"allow_groups": ["20001"], "group_memo": True})
        bot._memo.remember("20001", "甲", "旧缓存")
        self.assertIn("旧缓存", bot._memo.prefix("20001"))
        bot.configure({"allow_groups": ["20001", "20002"], "group_memo": True})
        self.assertEqual(bot._memo.prefix("20001"), "")


class GuestPromptInjectionTest(unittest.TestCase):
    def test_context_appended_only_within_turn(self):
        base = onebot.guest_prompt("你是{name}", "琴音")
        self.assertEqual(base, "你是琴音")
        block = groupmemo.HEADER + "\n- 甲：在聊新游戏\n" + groupmemo.FOOTER
        token = onebot._TURN_GROUP_CONTEXT.set(block)
        try:
            got = onebot.guest_prompt("你是{name}", "琴音")
        finally:
            onebot._TURN_GROUP_CONTEXT.reset(token)
        self.assertIn("你是琴音", got)
        self.assertIn(block, got)
        # 轮次结束后不残留
        self.assertEqual(onebot.guest_prompt("你是{name}", "琴音"), "你是琴音")

    def test_other_group_never_leaks(self):
        memo = groupmemo.GroupMemo()
        memo.remember("20001", "甲", "A 群内容")
        self.assertEqual(memo.prefix("20002"), "")
        self.assertNotIn("A 群内容", memo.prefix("20002"))


class MenuEntryTest(unittest.TestCase):
    def test_parse_and_alias(self):
        self.assertEqual(menu.parse("meido /memo", "meido").kind, "memo_clear")
        self.assertEqual(menu.parse("meido /memoclear", "meido").kind, "memo_clear")
        self.assertEqual(menu.match_alias("清除本群缓存"), ("清除本群缓存", "memo_clear"))
        self.assertEqual(menu.match_alias("忘掉群聊"), ("忘掉群聊", "memo_clear"))

    def test_admin_only(self):
        self.assertFalse(menu.authorize("memo_clear", is_admin=False))
        self.assertTrue(menu.authorize("memo_clear", is_admin=True))

    def test_help_lists_entry(self):
        text = menu.help_text("meido")
        self.assertIn("/memo", text)
        self.assertIn("清除本群常识缓存", text)


if __name__ == "__main__":
    unittest.main()
