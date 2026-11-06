"""输出脱敏、两级限流、长回复分段与相关配置的测试

都是纯函数/可注入时钟的用例，不连 QQ、不写磁盘。
"""
import unittest

from server import onebot, redact


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class RedactorTest(unittest.TestCase):
    def test_hit_is_masked(self):
        self.assertEqual(redact.redact_text("这是 Secret 内容", ["secret"]),
                         "这是 *** 内容")

    def test_case_insensitive(self):
        self.assertEqual(redact.redact_text("SECRET and secret", ["Secret"]),
                         "*** and ***")

    def test_fullwidth_text(self):
        self.assertEqual(redact.redact_text("ＳＥＣＲＥＴ", ["secret"]), "***")

    def test_fullwidth_word_in_config(self):
        self.assertEqual(redact.redact_text("secret", ["ｓｅｃｒｅｔ"]), "***")

    def test_unrelated_text_untouched(self):
        text = "今天天气不错，出门走走。"
        self.assertEqual(redact.redact_text(text, ["secret"]), text)

    def test_empty_table_is_noop(self):
        text = "保持原样"
        self.assertEqual(redact.redact_text(text, []), text)
        self.assertFalse(redact.Redactor([]).enabled)

    def test_single_char_words_ignored(self):
        # 单字词会误伤正常句子：配置里直接忽略
        self.assertEqual(redact.normalize_words(["a", "中"]), ())
        self.assertEqual(redact.redact_text("abc", ["a"]), "abc")

    def test_longest_word_wins(self):
        self.assertEqual(redact.redact_text("badword", ["bad", "badword"]), "***")

    def test_custom_mask(self):
        self.assertEqual(redact.redact_text("秘密行动", ["秘密"], "[已隐藏]"),
                         "[已隐藏]行动")

    def test_words_from_string(self):
        self.assertEqual(redact.normalize_words("秘密，内部\nSecret"),
                         ("秘密", "内部", "Secret"))

    def test_dedupe_words(self):
        self.assertEqual(redact.normalize_words(["秘密", "秘密 "]), ("秘密",))


class RateGateTest(unittest.TestCase):
    def test_user_quota_boundary(self):
        gate = onebot.RateGate(6, 10, clock=FakeClock())
        self.assertEqual(len([ok for ok, _ in (gate.check("1") for _ in range(6))
                              if ok]), 6)
        ok, notice = gate.check("1")
        self.assertFalse(ok)
        self.assertTrue(notice)

    def test_group_quota_across_users(self):
        gate = onebot.RateGate(6, 10, clock=FakeClock())
        allowed = 0
        for i in range(2):
            for user in ("1", "2", "3", "4", "5"):
                if gate.check(user, "g1")[0]:
                    allowed += 1
        self.assertEqual(allowed, 10)
        ok, notice = gate.check("6", "g1")
        self.assertFalse(ok)
        self.assertIn("群", notice)

    def test_limited_user_does_not_eat_group_quota(self):
        gate = onebot.RateGate(2, 3, clock=FakeClock())
        self.assertTrue(gate.check("1", "g1")[0])
        self.assertTrue(gate.check("1", "g1")[0])
        self.assertFalse(gate.check("1", "g1")[0])   # 个人超限，不占群名额
        self.assertTrue(gate.check("2", "g1")[0])
        ok, notice = gate.check("3", "g1")            # 群 3 条已满
        self.assertFalse(ok)
        self.assertTrue(notice)

    def test_user_notice_only_once(self):
        gate = onebot.RateGate(1, 10, clock=FakeClock())
        self.assertTrue(gate.check("1")[0])
        ok, notice = gate.check("1")
        self.assertFalse(ok)
        self.assertTrue(notice)
        for _ in range(3):
            ok, notice = gate.check("1")
            self.assertFalse(ok)
            self.assertEqual(notice, "")

    def test_group_notice_only_once(self):
        gate = onebot.RateGate(6, 1, clock=FakeClock())
        self.assertTrue(gate.check("1", "g1")[0])
        _, notice = gate.check("2", "g1")
        self.assertTrue(notice)
        _, notice = gate.check("3", "g1")
        self.assertEqual(notice, "")

    def test_notice_recovers_after_window(self):
        clock = FakeClock()
        gate = onebot.RateGate(1, 10, clock=clock)
        gate.check("1")
        _, notice = gate.check("1")
        self.assertTrue(notice)
        clock.now = 60.0          # 窗口未过（仍被限流），但提示的冷却已到
        _, notice = gate.check("1")
        self.assertTrue(notice)

    def test_zero_means_unlimited(self):
        gate = onebot.RateGate(0, 0, clock=FakeClock())
        for _ in range(20):
            self.assertTrue(gate.check("1", "g1")[0])


class SplitReplyLimitTest(unittest.TestCase):
    def test_default_is_300_by_3(self):
        self.assertEqual(onebot.DEFAULT_REPLY_CHUNK_CHARS, 300)
        self.assertEqual(onebot.DEFAULT_MAX_REPLY_CHUNKS, 3)

    def test_long_text_truncated_to_three(self):
        chunks = onebot.split_reply("字" * 1000)
        self.assertEqual(len(chunks), 3)
        self.assertEqual(len(chunks[0]), 300)
        self.assertTrue(chunks[-1].endswith(onebot.TRUNCATED_NOTE))

    def test_short_text_single_chunk(self):
        self.assertEqual(onebot.split_reply("好的"), ["好的"])


class GuardConfigTest(unittest.TestCase):
    def test_defaults(self):
        cfg = onebot.parse_config({})
        self.assertEqual(cfg.rate_per_minute, 6)
        self.assertEqual(cfg.group_rate_per_minute, 10)
        self.assertEqual(cfg.reply_chunk_chars, 300)
        self.assertEqual(cfg.max_reply_chunks, 3)
        self.assertEqual(cfg.redact_words, ())
        self.assertEqual(cfg.redact_mask, "***")

    def test_parse_custom(self):
        cfg = onebot.parse_config({
            "rate_per_minute": "5", "group_rate_per_minute": "9",
            "reply_chunk_chars": "200", "max_reply_chunks": "2",
            "redact_words": "秘密，内部", "redact_mask": "[隐藏]",
        })
        self.assertEqual(cfg.rate_per_minute, 5)
        self.assertEqual(cfg.group_rate_per_minute, 9)
        self.assertEqual(cfg.reply_chunk_chars, 200)
        self.assertEqual(cfg.max_reply_chunks, 2)
        self.assertEqual(cfg.redact_words, ("秘密", "内部"))
        self.assertEqual(cfg.redact_mask, "[隐藏]")

    def test_zero_chunks_falls_back(self):
        cfg = onebot.parse_config({"max_reply_chunks": 0})
        self.assertEqual(cfg.max_reply_chunks, 3)


class HotReloadTest(unittest.TestCase):
    """configure() 重建限流与脱敏器：改配置即生效，不用重启"""

    def _bot(self):
        async def runner(text, *, sid, device, readonly, turn_id="", image=None):
            return {"reply": "好的"}

        async def shot():
            return b"", ""

        return onebot.OneBotClient(
            runner=runner, screenshot=shot, status_provider=lambda: {},
            nickname=lambda: "meido", pending_clear=lambda device, sid: None,
        )

    def test_reconfigure_updates_redactor(self):
        bot = self._bot()
        bot.configure({"enable": False, "redact_words": ["秘密"]})
        self.assertTrue(bot._redactor.enabled)
        self.assertEqual(bot._redactor.apply("这是秘密"), "这是***")
        bot.configure({"enable": False, "redact_words": []})
        self.assertFalse(bot._redactor.enabled)
        self.assertEqual(bot._redactor.apply("这是秘密"), "这是秘密")

    def test_reconfigure_updates_gate(self):
        bot = self._bot()
        bot.configure({"enable": False, "rate_per_minute": 1})
        self.assertTrue(bot._gate.check("1")[0])
        self.assertFalse(bot._gate.check("1")[0])
        bot.configure({"enable": False, "rate_per_minute": 2})
        self.assertTrue(bot._gate.check("1")[0])


if __name__ == "__main__":
    unittest.main()
