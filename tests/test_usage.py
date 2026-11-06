"""API 用量与余额统计的单元测试。

覆盖：usage 字段解析（含缓存命中/未命中）、按日聚合与跨日边界、
闲时/标准时段判定（含时区与法定节假日）、按官方单价的费用估算、
余额接口成功与失败、无 Key 场景。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

if not hasattr(httpx, "MockTransport"):
    # test_shot_plugin / test_gamepad_status 在 Windows 上会往 sys.modules 塞占位 httpx；
    # 按 discover 的字母序它们先执行，这里换回真正的包，否则 MockTransport 不存在
    sys.modules.pop("httpx", None)
    import httpx  # noqa: F811

from server import usage  # noqa: E402

# server.usage 可能已被前面的用例用占位 httpx 导入过，显式换回真包
usage.httpx = httpx


class UsageTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dm-usage-"))
        self._env = mock.patch.dict(os.environ, {
            "DECKMEIDO_DATA_DIR": str(self.tmp),
        })
        self._env.start()

    def tearDown(self):
        self._env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def usage_file(self, day: str) -> Path:
        return self.tmp / "usage" / f"{day}.json"


class ParseUsageTest(unittest.TestCase):
    def test_deepseek_缓存命中与未命中(self):
        raw = {
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "total_tokens": 1200,
            "prompt_cache_hit_tokens": 600,
            "prompt_cache_miss_tokens": 400,
        }
        parsed = usage.parse_usage(raw)
        self.assertEqual(parsed["prompt_tokens"], 1000)
        self.assertEqual(parsed["completion_tokens"], 200)
        self.assertEqual(parsed["total_tokens"], 1200)
        self.assertEqual(parsed["cache_hit_tokens"], 600)
        self.assertEqual(parsed["cache_miss_tokens"], 400)

    def test_openai_details_cached_tokens_兼容(self):
        raw = {
            "prompt_tokens": 500,
            "completion_tokens": 100,
            "total_tokens": 600,
            "prompt_tokens_details": {"cached_tokens": 128},
        }
        parsed = usage.parse_usage(raw)
        self.assertEqual(parsed["cache_hit_tokens"], 128)
        self.assertEqual(parsed["cache_miss_tokens"], 372)

    def test_缺字段与空值不炸(self):
        self.assertEqual(usage.parse_usage(None)["prompt_tokens"], 0)
        parsed = usage.parse_usage({"prompt_tokens": 100})
        self.assertEqual(parsed["total_tokens"], 100)
        self.assertEqual(parsed["cache_hit_tokens"], 0)
        self.assertEqual(parsed["cache_miss_tokens"], 100)


class PeakWindowTest(unittest.TestCase):
    def test_北京时间工作日上午是高峰(self):
        # 2026-11-06 是周五；UTC 01:00 = 北京时间 09:00
        dt = datetime.fromisoformat("2026-11-06T01:00:00+00:00")
        self.assertTrue(usage.is_peak(dt))
        # UTC 03:59 = 北京 11:59，仍在上午高峰
        self.assertTrue(usage.is_peak(
            datetime.fromisoformat("2026-11-06T03:59:00+00:00")))
        # UTC 04:00 = 北京 12:00，午间闲时
        self.assertFalse(usage.is_peak(
            datetime.fromisoformat("2026-11-06T04:00:00+00:00")))

    def test_下午高峰与晚间闲时(self):
        # 北京 14:00-18:00 = UTC 06:00-10:00
        self.assertTrue(usage.is_peak(
            datetime.fromisoformat("2026-11-06T06:00:00+00:00")))
        self.assertFalse(usage.is_peak(
            datetime.fromisoformat("2026-11-06T10:00:00+00:00")))

    def test_周末全天闲时(self):
        # 2026-11-07 周六 10:00 北京时间
        self.assertFalse(usage.is_peak(
            datetime.fromisoformat("2026-11-07T02:00:00+00:00")))

    def test_法定节假日全天闲时(self):
        # 2026-10-05 周一，国庆假期内 10:00 北京时间
        self.assertFalse(usage.is_peak(
            datetime.fromisoformat("2026-10-05T02:00:00+00:00")))
        # 同一天的假后第一个工作日（10-08 周四）10:00 恢复高峰
        self.assertTrue(usage.is_peak(
            datetime.fromisoformat("2026-10-08T02:00:00+00:00")))


class CostTest(unittest.TestCase):
    def test_缓存命中未命中分别计价(self):
        # flash 高峰：命中 0.04 / 未命中 2 / 输出 8（每百万 tokens）
        cost = usage.cost_for("deepseek-flash", 1_000_000, 1_000_000, 1_000_000, True)
        self.assertAlmostEqual(cost, 0.04 + 2.0 + 8.0)

    def test_闲时半价(self):
        peak = usage.cost_for("deepseek-flash", 500_000, 500_000, 500_000, True)
        off = usage.cost_for("deepseek-flash", 500_000, 500_000, 500_000, False)
        self.assertAlmostEqual(peak, off * 2)

    def test_pro_档位与旧模型名(self):
        self.assertEqual(usage.model_tier("deepseek-v4-pro"), "pro")
        self.assertEqual(usage.model_tier("deepseek-chat"), "flash")
        self.assertEqual(usage.model_tier("deepseek-reasoner"), "flash")
        self.assertIsNone(usage.model_tier("gpt-4o"))

    def test_未收录模型不估算费用(self):
        self.assertIsNone(usage.cost_for("gpt-4o", 1, 1, 1, True))


class RecordTest(UsageTestBase):
    def test_按日聚合与缓存统计(self):
        now = datetime.fromisoformat("2026-11-06T15:00:00+08:00")  # 周五下午高峰
        self.assertTrue(usage.record({"prompt_tokens": 1000, "completion_tokens": 200,
                                      "total_tokens": 1200,
                                      "prompt_cache_hit_tokens": 600,
                                      "prompt_cache_miss_tokens": 400},
                                     "deepseek-chat", now=now))
        self.assertTrue(usage.record({"prompt_tokens": 500, "completion_tokens": 100,
                                      "total_tokens": 600,
                                      "prompt_cache_hit_tokens": 0,
                                      "prompt_cache_miss_tokens": 500},
                                     "deepseek-chat", now=now))
        data = json.loads(self.usage_file("2026-11-06").read_text(encoding="utf-8"))
        m = data["models"]["deepseek-chat"]
        self.assertEqual(m["requests"], 2)
        self.assertEqual(m["prompt_tokens"], 1500)
        self.assertEqual(m["completion_tokens"], 300)
        self.assertEqual(m["cache_hit_tokens"], 600)
        self.assertEqual(m["peak"]["requests"], 2)
        self.assertEqual(m["offpeak"]["requests"], 0)

        snap = usage.snapshot(7, now=now, base_url="https://api.deepseek.com")
        self.assertEqual(len(snap["days"]), 1)
        day = snap["days"][0]
        self.assertEqual(day["requests"], 2)
        self.assertEqual(day["cache_hit_rate"], 40.0)
        self.assertTrue(day["models"][0]["priced"])
        # 600 命中 + 900 未命中 + 300 输出，高峰价
        expected = 600 / 1e6 * 0.04 + 900 / 1e6 * 2.0 + 300 / 1e6 * 8.0
        self.assertAlmostEqual(day["cost_cny"], round(expected, 6))

    def test_跨日边界按北京时间分文件(self):
        before = datetime.fromisoformat("2026-11-06T23:59:00+08:00")
        after = datetime.fromisoformat("2026-11-07T00:01:00+08:00")
        usage.record({"prompt_tokens": 10, "completion_tokens": 1}, "deepseek-chat",
                     now=before)
        usage.record({"prompt_tokens": 20, "completion_tokens": 2}, "deepseek-chat",
                     now=after)
        self.assertTrue(self.usage_file("2026-11-06").exists())
        self.assertTrue(self.usage_file("2026-11-07").exists())
        # UTC 时间同一时刻也落在北京日期：2026-11-06T15:59Z = 北京 23:59
        self.assertEqual(
            usage._cn(datetime.fromisoformat("2026-11-06T15:59:00+00:00")).date().isoformat(),
            "2026-11-06")

    def test_峰谷分桶让费用按当时时段折算(self):
        peak = datetime.fromisoformat("2026-11-06T10:00:00+08:00")
        off = datetime.fromisoformat("2026-11-06T20:00:00+08:00")
        payload = {"prompt_tokens": 1_000_000, "completion_tokens": 0,
                   "prompt_cache_hit_tokens": 0,
                   "prompt_cache_miss_tokens": 1_000_000}
        usage.record(payload, "deepseek-flash", now=peak)
        usage.record(payload, "deepseek-flash", now=off)
        snap = usage.snapshot(7, now=off)
        # 高峰 2 元 + 闲时 1 元
        self.assertAlmostEqual(snap["totals"]["cost_cny"], 3.0)
        self.assertEqual(snap["tier"], "offpeak")

    def test_空_usage_不记录(self):
        self.assertFalse(usage.record({}, "deepseek-chat"))
        self.assertFalse(self.usage_file("2026-11-06").exists())

    def test_清空与导出(self):
        usage.record({"prompt_tokens": 10, "completion_tokens": 1}, "deepseek-chat",
                     now=datetime.fromisoformat("2026-11-06T10:00:00+08:00"))
        exported = usage.export_data()
        self.assertIn("2026-11-06", exported["days"])
        self.assertEqual(usage.clear(), 1)
        self.assertEqual(usage.export_data()["days"], {})


class BalanceTest(unittest.TestCase):
    def _client(self, handler) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def _fetch(self, handler, base_url: str, api_key: str) -> dict:
        async def run() -> dict:
            cli = self._client(handler)
            try:
                return await usage.fetch_balance(base_url, api_key, client=cli)
            finally:
                await cli.aclose()
        return asyncio.run(run())

    def test_成功解析_CNY_优先(self):
        body = {
            "is_available": True,
            "balance_infos": [
                {"currency": "USD", "total_balance": "1.00",
                 "granted_balance": "0.00", "topped_up_balance": "1.00"},
                {"currency": "CNY", "total_balance": "110.00",
                 "granted_balance": "10.00", "topped_up_balance": "100.00"},
            ],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(str(request.url), "https://api.deepseek.com/user/balance")
            self.assertEqual(request.headers["authorization"], "Bearer sk-test")
            return httpx.Response(200, json=body)

        result = self._fetch(handler, "https://api.deepseek.com", "sk-test")
        self.assertTrue(result["ok"])
        self.assertTrue(result["is_available"])
        self.assertEqual(result["currency"], "CNY")
        self.assertEqual(result["total_balance"], "110.00")
        self.assertEqual(result["granted_balance"], "10.00")
        self.assertEqual(result["topped_up_balance"], "100.00")

    def test_v1_地址_404_后回退根路径(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            if str(request.url).endswith("/v1/user/balance"):
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json={
                "is_available": True,
                "balance_infos": [{"currency": "CNY", "total_balance": "5.00",
                                   "granted_balance": "0.00",
                                   "topped_up_balance": "5.00"}],
            })

        result = self._fetch(handler, "https://api.deepseek.com/v1", "sk-test")
        self.assertTrue(result["ok"])
        self.assertEqual(len(seen), 2)

    def test_401_返回明确原因(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "invalid key"})

        result = self._fetch(handler, "https://api.deepseek.com", "bad")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "http_401")
        self.assertIn("401", result["error"])

    def test_无_Key_不请求(self):
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("未配置 Key 时不应发起请求")

        result = self._fetch(handler, "https://api.deepseek.com", "  ")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "unconfigured")

    def test_网络不通返回中文原因(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        result = self._fetch(handler, "https://api.deepseek.com", "sk-test")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "network")
        self.assertIn("网络不通", result["error"])


if __name__ == "__main__":
    unittest.main()
