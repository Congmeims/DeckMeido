"""离线模式：开关默认值、快速失败与本地放行。

全部用本地假数据 / 不发真实请求；重点是「开启后不等超时、关闭后不多拦」。
"""
from __future__ import annotations

import asyncio
import time
import tomllib
import unittest
from pathlib import Path

from server import netmode, tools, updater, usage
from server.llm import LLMClient

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "server" / "config.example.toml"


class NetmodeTest(unittest.TestCase):
    def setUp(self):
        netmode.set_enabled(False)

    def tearDown(self):
        netmode.set_enabled(False)

    def test_config_template_defaults_off(self):
        cfg = tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))
        self.assertIs(cfg["runtime"]["offline_mode"], False)

    def test_configure_reads_switch(self):
        self.assertFalse(netmode.configure({}))
        self.assertTrue(netmode.configure({"runtime": {"offline_mode": True}}))
        self.assertTrue(netmode.enabled())
        self.assertFalse(netmode.configure({"runtime": {"offline_mode": False}}))

    def test_is_local_url(self):
        for url in (
            "http://127.0.0.1:18765",
            "http://127.0.0.1:1234/v1",
            "http://localhost:3001/ws",
            "http://[::1]:8080",
            "http://192.168.1.10:1234/v1",
            "http://10.0.0.5:1234",
            "http://deck.local:1234",
        ):
            self.assertTrue(netmode.is_local_url(url), url)
        for url in (
            "https://api.deepseek.com",
            "https://api.deepseek.com/v1/chat/completions",
            "https://8.8.8.8",
            "https://mirror.example.com/deckmeido",
            "",
            "not-a-url",
        ):
            self.assertFalse(netmode.is_local_url(url), url)

    def test_disabled_never_blocks(self):
        netmode.set_enabled(False)
        netmode.check("https://api.deepseek.com", "模型对话")

    def test_enabled_blocks_external_fast(self):
        netmode.set_enabled(True)
        start = time.monotonic()
        with self.assertRaises(netmode.OfflineError) as ctx:
            netmode.check("https://api.deepseek.com", "模型对话")
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertIn("离线模式已开启", str(ctx.exception))
        self.assertIn("模型对话", str(ctx.exception))

    def test_enabled_allows_local(self):
        netmode.set_enabled(True)
        netmode.check("http://127.0.0.1:1234/v1", "模型对话")

    def test_llm_list_models_fails_without_network(self):
        netmode.set_enabled(True)
        client = LLMClient(
            base_url="https://api.deepseek.com",
            api_key="sk-test",
            model="deepseek-chat",
            max_tokens=None,
        )
        start = time.monotonic()

        async def run():
            try:
                await client.list_models()
            finally:
                # 其他测试可能把 httpx 换成精简桩（没有 aclose），这里不依赖具体实现
                try:
                    await client.close()
                except AttributeError:
                    pass

        with self.assertRaises(netmode.OfflineError):
            asyncio.run(run())
        self.assertLess(time.monotonic() - start, 0.5)

    def test_updater_check_fails_without_network(self):
        netmode.set_enabled(True)
        start = time.monotonic()
        with self.assertRaises(netmode.OfflineError):
            asyncio.run(updater.check())
        self.assertLess(time.monotonic() - start, 0.5)

    def test_balance_returns_readable_reason(self):
        netmode.set_enabled(True)
        result = asyncio.run(usage.fetch_balance("https://api.deepseek.com", "sk-test"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "offline")
        self.assertIn("离线模式已开启", result["error"])

    def test_outbound_tool_fails_fast_local_tools_stay(self):
        netmode.set_enabled(True)
        text = asyncio.run(tools.execute("web_search", {"query": "天气"}))
        self.assertIn("离线模式已开启", text)
        # 本地动作不在拦截名单里：截图、文件、命令、状态查看照常可用
        self.assertNotIn("screenshot", tools._OUTBOUND_TOOLS)
        self.assertNotIn("list_dir", tools._OUTBOUND_TOOLS)
        self.assertNotIn("run_command", tools._OUTBOUND_TOOLS)


if __name__ == "__main__":
    unittest.main()
