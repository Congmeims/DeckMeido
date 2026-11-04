"""状态栏接口：缓存、忙碌跳过、失败回模板；模型全部用假函数，不打网"""
from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from server import status_line


class StatusLineTest(unittest.TestCase):
    def setUp(self):
        status_line._cache.clear()
        status_line._inflight.clear()

    def run_line(self, **kwargs) -> dict:
        return asyncio.run(status_line.generate(**kwargs))

    def test_default_prompt_and_override(self):
        self.assertIn("20 个汉字", status_line.prompt_text({}))
        self.assertEqual(status_line.prompt_text({"status_prompt": "自定义"}), "自定义")
        self.assertIn("20 个汉字", status_line.prompt_text({"status_prompt": "  "}))

    def test_no_key_uses_template(self):
        res = self.run_line(state="idle", nickname="琴音", configured=False)
        self.assertEqual(res["text"], "琴音，正在待命~")
        self.assertEqual(res["source"], "template")
        self.assertFalse(res["cached"])

    def test_busy_never_calls_model(self):
        async def boom(_messages):
            raise AssertionError("忙碌时不应请求模型")

        res = self.run_line(state="busy", nickname="琴音", busy=True, chat=boom)
        self.assertEqual(res["text"], "琴音，正在处理…")
        self.assertEqual(res["source"], "template")

    def test_error_template(self):
        res = self.run_line(state="error", nickname="琴音", configured=False)
        self.assertEqual(res["text"], "琴音，出了点小状况…")

    def test_high_confidence_game_in_template(self):
        res = self.run_line(state="idle", game="艾尔登法环", game_confidence="high",
                            nickname="琴音", configured=False)
        self.assertEqual(res["text"], "琴音，在《艾尔登法环》里待命~")

    def test_low_confidence_game_is_dropped(self):
        res = self.run_line(state="idle", game="艾尔登法环", game_confidence="low",
                            nickname="琴音", configured=False)
        self.assertEqual(res["text"], "琴音，正在待命~")

    def test_ai_success_and_cache_hit(self):
        calls = []

        async def chat(messages):
            calls.append(messages)
            self.assertEqual(messages[0]["role"], "system")
            self.assertIn("当前状态：idle", messages[1]["content"])
            self.assertIn("称呼：琴音", messages[1]["content"])
            return {"content": "琴音，今天也一起加油~"}

        first = self.run_line(state="idle", nickname="琴音", chat=chat,
                              prompt="自定义提示词")
        self.assertEqual(first, {"text": "琴音，今天也一起加油~",
                                 "source": "ai", "cached": False})
        self.assertEqual(calls[0][0]["content"], "自定义提示词")

        second = self.run_line(state="idle", nickname="琴音", chat=chat)
        self.assertEqual(second["text"], "琴音，今天也一起加油~")
        self.assertTrue(second["cached"])
        self.assertEqual(len(calls), 1)

    def test_cache_expires_after_ten_minutes(self):
        async def chat(_messages):
            return {"content": "新的一句~"}

        status_line._cache[("idle", "", "琴音")] = (
            status_line.time.monotonic() - status_line.TTL - 1, "旧的一句")
        res = self.run_line(state="idle", nickname="琴音", chat=chat)
        self.assertEqual(res["text"], "新的一句~")
        self.assertFalse(res["cached"])

    def test_cache_key_includes_game_and_name(self):
        calls = []

        async def chat(_messages):
            calls.append(1)
            return {"content": "换了一句~"}

        self.run_line(state="idle", game="游戏甲", game_confidence="high",
                      nickname="琴音", chat=chat)
        self.run_line(state="idle", game="游戏乙", game_confidence="high",
                      nickname="琴音", chat=chat)
        self.run_line(state="idle", game="游戏甲", game_confidence="high",
                      nickname="小鹿", chat=chat)
        self.assertEqual(len(calls), 3)

    def test_model_failure_falls_back(self):
        async def chat(_messages):
            raise RuntimeError("模型不支持")

        res = self.run_line(state="idle", nickname="琴音", chat=chat)
        self.assertEqual(res["text"], "琴音，正在待命~")
        self.assertEqual(res["source"], "template")
        self.assertFalse(res["cached"])

    def test_timeout_falls_back(self):
        async def slow(_messages):
            await asyncio.sleep(1)
            return {"content": "迟到的文案"}

        with mock.patch.object(status_line, "TIMEOUT", 0.02):
            res = self.run_line(state="idle", nickname="琴音", chat=slow)
        self.assertEqual(res["text"], "琴音，正在待命~")
        self.assertEqual(res["source"], "template")

    def test_too_long_text_falls_back(self):
        async def chat(_messages):
            return {"content": "这是一句明显超过二十个字的啰嗦状态文案，后面还有很多很多字"}

        res = self.run_line(state="idle", nickname="琴音", chat=chat)
        self.assertEqual(res["text"], "琴音，正在待命~")
        self.assertEqual(res["source"], "template")

    def test_missing_nickname_uses_persona_then_default(self):
        res = self.run_line(state="idle", persona_name="琴音", configured=False)
        self.assertEqual(res["text"], "琴音，正在待命~")
        res = self.run_line(state="idle", configured=False)
        self.assertEqual(res["text"], "Meido，正在待命~")

    def test_concurrent_calls_share_one_request(self):
        calls = []

        async def chat(_messages):
            calls.append(1)
            await asyncio.sleep(0.05)
            return {"content": "琴音，在等你~"}

        async def both():
            return await asyncio.gather(
                status_line.generate(state="idle", nickname="琴音", chat=chat),
                status_line.generate(state="idle", nickname="琴音", chat=chat),
            )

        first, second = asyncio.run(both())
        self.assertEqual(len(calls), 1)
        self.assertEqual(first["text"], "琴音，在等你~")
        self.assertEqual(second["text"], "琴音，在等你~")
        self.assertTrue(second["cached"])

    def test_clean_text_strips_quotes_and_whitespace(self):
        self.assertEqual(status_line.clean_text(' “琴音，在待命~” '), "琴音，在待命~")
        self.assertEqual(status_line.clean_text("第一行\n第二行"), "第一行")
        self.assertEqual(status_line.clean_text(""), "")


if __name__ == "__main__":
    unittest.main()
