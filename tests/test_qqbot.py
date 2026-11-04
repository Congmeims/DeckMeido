"""QQ 通道（server/qqbot.py）的单元测试。

全部用本地假 WS（内存队列模拟 NapCat 的 WebSocket 服务器），
不连真实 NapCat，也不需要本机装 websockets。
"""
import asyncio
import json
import time
import unittest

from server import policy, qqbot


# ---------- 纯函数部分 ----------

class PrefixTest(unittest.TestCase):
    def test_prefix_is_nickname(self):
        got = qqbot.parse_command("DeckMeido /status", "DeckMeido")
        self.assertIsNotNone(got)
        self.assertEqual(got.kind, "status")
        self.assertEqual(got.text, "")

    def test_ascii_case_insensitive_and_direct_attach(self):
        self.assertEqual(qqbot.parse_command("deckmeido/status", "DeckMeido").kind, "status")
        self.assertEqual(qqbot.parse_command("  DECKMEIDO  /shot  ", "DeckMeido").kind, "shot")

    def test_nickname_changed(self):
        self.assertEqual(qqbot.parse_command("喵喵 /cancel", "喵喵").kind, "cancel")
        self.assertIsNone(qqbot.parse_command("DeckMeido /status", "喵喵"))

    def test_empty_nickname_falls_back(self):
        self.assertEqual(qqbot.prefix_for(""), "meido")
        self.assertEqual(qqbot.prefix_for("   "), "meido")
        self.assertEqual(qqbot.parse_command("meido 你好", "").kind, "chat")

    def test_plain_text_after_prefix_is_chat(self):
        got = qqbot.parse_command("DeckMeido   今天天气怎么样", "DeckMeido")
        self.assertEqual(got.kind, "chat")
        self.assertEqual(got.text, "今天天气怎么样")

    def test_prefix_only(self):
        self.assertEqual(qqbot.parse_command("DeckMeido", "DeckMeido").kind, "chat")
        self.assertEqual(qqbot.parse_command("DeckMeido", "DeckMeido").text, "")

    def test_unknown_command_is_chat(self):
        got = qqbot.parse_command("DeckMeido /foo bar", "DeckMeido")
        self.assertEqual(got.kind, "chat")
        self.assertEqual(got.text, "/foo bar")

    def test_none_without_prefix(self):
        self.assertIsNone(qqbot.parse_command("你好", "DeckMeido"))
        self.assertIsNone(qqbot.parse_command("", "DeckMeido"))


class RouteTest(unittest.TestCase):
    def cfg(self, **kw):
        base = dict(enable=True, ws_url="ws://127.0.0.1:3001", access_token="tok",
                    admin_qq=("10001",), allow_groups=("20001",))
        base.update(kw)
        return qqbot.parse_config(base)

    def ev(self, uin, text, group=None, at=False):
        return qqbot.MessageEvent(uin=str(uin), text=text, group_id=group,
                                  at_bot=at, self_id="99999")

    def test_private_stranger_ignored_silently(self):
        route = qqbot.route_message(self.cfg(), self.ev("30003", "meido /status"), "meido")
        self.assertEqual(route.action, "ignore")

    def test_private_admin_command(self):
        route = qqbot.route_message(self.cfg(), self.ev("10001", "meido /status"), "meido")
        self.assertEqual(route.action, "command")
        self.assertEqual(route.kind, "status")
        self.assertFalse(route.readonly)

    def test_private_admin_no_prefix_is_chat(self):
        route = qqbot.route_message(self.cfg(), self.ev("10001", "今天吃点啥"), "meido")
        self.assertEqual(route.action, "chat")
        self.assertEqual(route.text, "今天吃点啥")

    def test_group_not_in_whitelist(self):
        route = qqbot.route_message(self.cfg(), self.ev("10001", "meido /status", "29999", True), "meido")
        self.assertEqual(route.action, "ignore")

    def test_group_requires_at(self):
        route = qqbot.route_message(self.cfg(), self.ev("10001", "meido /status", "20001", False), "meido")
        self.assertEqual(route.action, "ignore")

    def test_group_non_admin_only_chat(self):
        route = qqbot.route_message(self.cfg(), self.ev("30003", "meido /status", "20001", True), "meido")
        self.assertEqual(route.action, "chat")
        self.assertTrue(route.readonly)
        self.assertNotIn("meido", route.text)

    def test_group_admin_also_chat_only(self):
        route = qqbot.route_message(self.cfg(), self.ev("10001", "meido /status", "20001", True), "meido")
        self.assertEqual(route.action, "chat")
        self.assertTrue(route.readonly)


class LimitsTest(unittest.TestCase):
    def test_rate_limiter_window(self):
        now = [0.0]
        limiter = qqbot.RateLimiter(2, clock=lambda: now[0])
        self.assertTrue(limiter.allow("10001"))
        self.assertTrue(limiter.allow("10001"))
        self.assertFalse(limiter.allow("10001"))
        # 别人的额度不受影响
        self.assertTrue(limiter.allow("30003"))
        now[0] = 61.0
        self.assertTrue(limiter.allow("10001"))

    def test_rate_limiter_zero_means_unlimited(self):
        limiter = qqbot.RateLimiter(0)
        for _ in range(50):
            self.assertTrue(limiter.allow("10001"))

    def test_truncate(self):
        self.assertEqual(qqbot.truncate_text("abcdef", 3), "abc")
        self.assertEqual(qqbot.truncate_text("abc", 3), "abc")
        self.assertEqual(qqbot.truncate_text("abc", 0), "abc")

    def test_split_reply(self):
        text = "\n".join(f"第{i}行内容" for i in range(40))
        chunks = qqbot.split_reply(text, limit=50, max_chunks=3)
        self.assertLessEqual(len(chunks), 3)
        for c in chunks:
            self.assertLessEqual(len(c), 50 + len(qqbot.TRUNCATED_NOTE))
        self.assertTrue(chunks[-1].endswith(qqbot.TRUNCATED_NOTE))

    def test_split_short_reply_kept_whole(self):
        self.assertEqual(qqbot.split_reply("好的", limit=50), ["好的"])
        self.assertEqual(qqbot.split_reply("", limit=50), [])


class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        cfg = qqbot.parse_config({})
        self.assertFalse(cfg.enable)
        self.assertEqual(cfg.rate_per_minute, qqbot.DEFAULT_RATE_PER_MINUTE)
        self.assertEqual(cfg.admin_qq, ())

    def test_string_values_coerced(self):
        cfg = qqbot.parse_config({"enable": "true", "admin_qq": "10001",
                                  "allow_groups": ["20001"], "rate_per_minute": "12"})
        self.assertTrue(cfg.enable)
        self.assertEqual(cfg.admin_qq, ("10001",))
        self.assertEqual(cfg.allow_groups, ("20001",))
        self.assertEqual(cfg.rate_per_minute, 12)

    def test_missing_token_rejected(self):
        cfg = qqbot.parse_config({"enable": True, "ws_url": "ws://127.0.0.1:3001",
                                  "admin_qq": ["10001"]})
        err = qqbot.validate_config(cfg)
        self.assertIn("access_token", err)

    def test_bad_url_rejected(self):
        for url in ("http://127.0.0.1:3001", "wss://", "127.0.0.1:3001", ""):
            cfg = qqbot.parse_config({"enable": True, "ws_url": url,
                                      "access_token": "tok", "admin_qq": ["10001"]})
            self.assertIn("ws_url", qqbot.validate_config(cfg), url)

    def test_missing_admin_rejected(self):
        cfg = qqbot.parse_config({"enable": True, "ws_url": "ws://127.0.0.1:3001",
                                  "access_token": "tok"})
        self.assertIn("admin_qq", qqbot.validate_config(cfg))

    def test_disabled_has_no_requirement(self):
        self.assertIsNone(qqbot.validate_config(qqbot.parse_config({})))

    def test_mask_url(self):
        self.assertEqual(qqbot._mask_url("wss://user:pass@example.com/ws"),
                         "wss://example.com/ws")


class BackoffTest(unittest.TestCase):
    def test_growth_and_cap(self):
        b = qqbot.Backoff(base=1, cap=60, factor=2, jitter=0)
        self.assertEqual([b.delay(i) for i in range(8)],
                         [1, 2, 4, 8, 16, 32, 60, 60])

    def test_jitter_stays_below_cap(self):
        b = qqbot.Backoff(base=1, cap=10, jitter=0.5, rand=lambda: 0.99)
        for i in range(10):
            self.assertLessEqual(b.delay(i), 10)


# ---------- 假 WS + 通道流程 ----------

class FakeWS:
    """内存版 NapCat WebSocket：收到的帧塞队列，发出去的帧记下来并自动回 API 响应"""

    def __init__(self):
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, data):
        payload = json.loads(data)
        self.sent.append(payload)
        self.inbox.put_nowait(json.dumps({
            "status": "ok", "retcode": 0, "echo": payload.get("echo"), "data": None,
        }, ensure_ascii=False))

    def feed(self, obj):
        self.inbox.put_nowait(json.dumps(obj, ensure_ascii=False))

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.inbox.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def close(self):
        self.closed = True
        self.inbox.put_nowait(None)


def private_msg(uin, text, self_id="99999", mid="1"):
    return {"post_type": "message", "message_type": "private", "user_id": uin,
            "self_id": self_id, "message_id": mid,
            "message": [{"type": "text", "data": {"text": text}}]}


def group_msg(group, uin, text, at=True, self_id="99999", mid="2"):
    segs = []
    if at:
        segs.append({"type": "at", "data": {"qq": self_id}})
    segs.append({"type": "text", "data": {"text": text}})
    return {"post_type": "message", "message_type": "group", "group_id": group,
            "user_id": uin, "self_id": self_id, "message_id": mid, "message": segs}


async def wait_until(cond, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.01)
    return bool(cond())


class BotTest(unittest.IsolatedAsyncioTestCase):
    def make(self, ws=None, *, runner=None, config=None, **kwargs):
        ws = ws or FakeWS()
        calls = []

        async def default_runner(text, *, sid, device, readonly, turn_id=""):
            calls.append({"text": text, "sid": sid, "device": device,
                          "readonly": readonly, "turn_id": turn_id})
            return {"reply": "好的"}

        runner = runner or default_runner
        cleared = []

        async def default_shot():
            return b"png", "ok"

        async def default_status():
            return {"core": "运行中", "mode": "桌面模式", "version": "0.6.2"}

        async def factory(cfg):
            return ws

        bot = qqbot.QQBot(
            runner=runner, screenshot=default_shot, status_provider=default_status,
            nickname=kwargs.pop("nickname", lambda: "meido"),
            pending_clear=lambda device, sid: cleared.append((device, sid)),
            connect_factory=factory,
            sleep=kwargs.pop("sleep", asyncio.sleep),
        )
        cfg = qqbot.parse_config(config or {
            "enable": True, "ws_url": "ws://127.0.0.1:3001",
            "access_token": "tok", "admin_qq": ["10001"],
            "allow_groups": ["20001"],
        })
        bot.configure(cfg)
        self.calls, self.cleared = calls, cleared
        return bot, ws

    async def test_missing_token_refuses_start(self):
        bot, _ = self.make(config={"enable": True, "ws_url": "ws://127.0.0.1:3001",
                                   "admin_qq": ["10001"]})
        await bot.start()
        state = bot.state()
        self.assertEqual(state["state"], "misconfigured")
        self.assertIn("access_token", state["detail"])
        self.assertFalse(state["running"])

    async def test_private_admin_chat_and_silent_stranger(self):
        bot, ws = self.make()
        await bot.start()
        self.assertTrue(await wait_until(lambda: bot.state()["state"] == "connected"))

        ws.feed(private_msg("30003", "meido /status"))
        await bot.wait_idle()
        self.assertEqual(self.calls, [])
        self.assertEqual(ws.sent, [])

        ws.feed(private_msg("10001", "meido 今天天气怎么样"))
        await bot.wait_idle()
        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        self.assertEqual(call["text"], "今天天气怎么样")
        self.assertEqual(call["sid"], "qq-p-10001")
        self.assertEqual(call["device"], "qq:private:10001")
        self.assertFalse(call["readonly"])
        reply = [s for s in ws.sent if s.get("action") == "send_private_msg"]
        self.assertEqual(str(reply[0]["params"]["user_id"]), "10001")
        await bot.stop()

    async def test_group_at_required_and_chat_only(self):
        bot, ws = self.make()
        await bot.start()
        self.assertTrue(await wait_until(lambda: bot.state()["state"] == "connected"))

        ws.feed(group_msg("20001", "30003", "meido /status", at=False))
        await bot.wait_idle()
        self.assertEqual(self.calls, [])

        ws.feed(group_msg("20001", "30003", " meido 你好", at=True))
        await bot.wait_idle()
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.calls[0]["readonly"])
        self.assertEqual(self.calls[0]["sid"], "qq-g-20001-30003")
        self.assertEqual(self.calls[0]["device"], "qq:group:20001:30003")
        self.assertEqual(self.calls[0]["text"], "你好")
        sent = [s for s in ws.sent if s.get("action") == "send_group_msg"]
        self.assertEqual(sent[0]["params"]["message"][0]["type"], "at")
        self.assertEqual(str(sent[0]["params"]["message"][0]["data"]["qq"]), "30003")
        await bot.stop()

    async def test_status_command(self):
        bot, ws = self.make()
        await bot.start()
        self.assertTrue(await wait_until(lambda: bot.state()["state"] == "connected"))
        ws.feed(private_msg("10001", "meido /status"))
        await bot.wait_idle()
        texts = [s["params"]["message"] for s in ws.sent
                 if s.get("action") == "send_private_msg"]
        self.assertTrue(any("设备状态" in str(t) for t in texts))
        await bot.stop()

    async def test_rate_limit_is_silent(self):
        bot, ws = self.make(config={
            "enable": True, "ws_url": "ws://127.0.0.1:3001",
            "access_token": "tok", "admin_qq": ["10001"], "rate_per_minute": 1,
        })
        await bot.start()
        self.assertTrue(await wait_until(lambda: bot.state()["state"] == "connected"))
        ws.feed(private_msg("10001", "meido 一", mid="a"))
        ws.feed(private_msg("10001", "meido 二", mid="b"))
        await bot.wait_idle()
        self.assertEqual(len(self.calls), 1)
        # 第二条被限流丢弃：只应有一条回复
        replies = [s for s in ws.sent if s.get("action") == "send_private_msg"]
        self.assertEqual(len(replies), 1)
        self.assertEqual(bot.state()["stats"]["rate_limited"], 1)
        await bot.stop()

    async def test_incoming_truncated(self):
        bot, ws = self.make(config={
            "enable": True, "ws_url": "ws://127.0.0.1:3001",
            "access_token": "tok", "admin_qq": ["10001"], "max_message_chars": 10,
        })
        await bot.start()
        self.assertTrue(await wait_until(lambda: bot.state()["state"] == "connected"))
        ws.feed(private_msg("10001", "meido " + "很" * 40))
        await bot.wait_idle()
        self.assertEqual(len(self.calls), 1)
        self.assertLessEqual(len(self.calls[0]["text"]), 10)
        await bot.stop()

    async def test_confirm_reuses_turn_id(self):
        async def runner(text, *, sid, device, readonly, turn_id=""):
            self.calls.append({"text": text, "turn_id": turn_id})
            if len(self.calls) == 1:
                return {"reply": "需要确认：重启设备", "pending": {
                    "turn_id": "t1", "description": "重启设备"}}
            return {"reply": "已执行"}

        bot, ws = self.make(runner=runner)
        await bot.start()
        self.assertTrue(await wait_until(lambda: bot.state()["state"] == "connected"))
        ws.feed(private_msg("10001", "meido 帮我重启"))
        await bot.wait_idle()
        self.assertTrue(self.calls[0]["turn_id"].startswith("qq-"))
        ws.feed(private_msg("10001", "确认"))
        await bot.wait_idle()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[1]["turn_id"], "t1")
        await bot.stop()

    async def test_new_message_cancels_pending(self):
        async def runner(text, *, sid, device, readonly, turn_id=""):
            self.calls.append({"text": text, "turn_id": turn_id})
            if len(self.calls) == 1:
                return {"reply": "需要确认", "pending": {"turn_id": "t1",
                                                        "description": "重启设备"}}
            return {"reply": "好"}

        bot, ws = self.make(runner=runner)
        await bot.start()
        self.assertTrue(await wait_until(lambda: bot.state()["state"] == "connected"))
        ws.feed(private_msg("10001", "meido 帮我重启"))
        await bot.wait_idle()
        ws.feed(private_msg("10001", "meido 算了先不弄"))
        await bot.wait_idle()
        self.assertEqual(self.cleared, [("qq:private:10001", "qq-p-10001")])
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(self.calls[1]["turn_id"].startswith("qq-"))
        await bot.stop()

    async def test_confirm_expires(self):
        async def runner(text, *, sid, device, readonly, turn_id=""):
            self.calls.append({"text": text, "turn_id": turn_id})
            return {"reply": "需要确认", "pending": {"turn_id": "t1",
                                                    "description": "重启设备"}}

        bot, ws = self.make(runner=runner, config={
            "enable": True, "ws_url": "ws://127.0.0.1:3001",
            "access_token": "tok", "admin_qq": ["10001"], "confirm_timeout": 0,
        })
        await bot.start()
        self.assertTrue(await wait_until(lambda: bot.state()["state"] == "connected"))
        ws.feed(private_msg("10001", "meido 帮我重启"))
        await bot.wait_idle()
        ws.feed(private_msg("10001", "确认"))
        await bot.wait_idle()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.cleared, [("qq:private:10001", "qq-p-10001")])
        texts = [str(s["params"]["message"]) for s in ws.sent
                 if s.get("action") == "send_private_msg"]
        self.assertTrue(any("超时" in t for t in texts))
        await bot.stop()

    async def test_cancel_command(self):
        async def slow_runner(text, *, sid, device, readonly, turn_id=""):
            self.calls.append({"text": text})
            await asyncio.sleep(30)
            return {"reply": "完成"}

        bot, ws = self.make(runner=slow_runner)
        await bot.start()
        self.assertTrue(await wait_until(lambda: bot.state()["state"] == "connected"))
        ws.feed(private_msg("10001", "meido 慢慢做件事"))
        await wait_until(lambda: len(self.calls) == 1)
        ws.feed(private_msg("10001", "meido /cancel"))
        await bot.wait_idle()
        texts = [str(s["params"]["message"]) for s in ws.sent
                 if s.get("action") == "send_private_msg"]
        self.assertTrue(any("已终止" in t for t in texts))
        await bot.stop()

    async def test_shot_sends_image(self):
        bot, ws = self.make()
        await bot.start()
        self.assertTrue(await wait_until(lambda: bot.state()["state"] == "connected"))
        ws.feed(private_msg("10001", "meido /shot"))
        await bot.wait_idle()
        msg = [s for s in ws.sent if s.get("action") == "send_private_msg"][0]["params"]["message"]
        self.assertEqual(msg[1]["type"], "image")
        self.assertTrue(msg[1]["data"]["file"].startswith("base64://"))
        await bot.stop()


class ReconnectTest(unittest.IsolatedAsyncioTestCase):
    async def test_backoff_then_connected(self):
        sleeps: list[float] = []

        async def fast_sleep(d):
            sleeps.append(d)

        attempts = {"n": 0}

        async def factory(cfg):
            attempts["n"] += 1
            if attempts["n"] <= 2:
                raise RuntimeError("NapCat 还没起来")
            return FakeWS()

        bot = qqbot.QQBot(
            runner=lambda *a, **k: asyncio.sleep(0, result={"reply": "ok"}),
            screenshot=lambda: asyncio.sleep(0, result=(None, "")),
            status_provider=lambda: asyncio.sleep(0, result={}),
            nickname=lambda: "meido",
            connect_factory=factory, sleep=fast_sleep,
        )
        bot.configure(qqbot.parse_config({
            "enable": True, "ws_url": "ws://127.0.0.1:3001",
            "access_token": "tok", "admin_qq": ["10001"],
            "reconnect_min": 1, "reconnect_max": 1,
        }))
        await bot.start()
        self.assertTrue(await wait_until(lambda: attempts["n"] >= 3))
        self.assertTrue(await wait_until(lambda: bot.state()["state"] == "connected"))
        self.assertEqual(len(sleeps), 2)
        # 抖动只往下，两段等待都不会超过封顶值
        self.assertTrue(all(0 < d <= 1 for d in sleeps))
        state = bot.state()
        self.assertEqual(state["attempts"], 0)
        self.assertTrue(state["stats"]["reconnects"] >= 2)
        await bot.stop()

    async def test_stop_from_reconnecting(self):
        sleeps: list[float] = []

        async def slow_sleep(d):
            sleeps.append(d)
            # 一定要真的让出事件循环：退避等待本身是非阻塞的
            await asyncio.sleep(0.05)

        async def factory(cfg):
            raise RuntimeError("永远连不上")

        bot = qqbot.QQBot(
            runner=lambda *a, **k: asyncio.sleep(0, result={"reply": "ok"}),
            screenshot=lambda: asyncio.sleep(0, result=(None, "")),
            status_provider=lambda: asyncio.sleep(0, result={}),
            nickname=lambda: "meido",
            connect_factory=factory, sleep=slow_sleep,
        )
        bot.configure(qqbot.parse_config({
            "enable": True, "ws_url": "ws://127.0.0.1:3001",
            "access_token": "tok", "admin_qq": ["10001"],
        }))
        await bot.start()
        self.assertTrue(await wait_until(lambda: bot.state()["state"] == "reconnecting"))
        self.assertEqual(bot.state()["state"], "reconnecting")
        await bot.stop()
        self.assertEqual(bot.state()["state"], "stopped")


class PolicyReadonlyTest(unittest.TestCase):
    def test_group_member_cannot_touch_device(self):
        ctx = policy.TurnContext(readonly=True, client="qq",
                                 user_text="帮我重启设备")
        decision = policy.check("run_command", {"command": "systemctl reboot"}, ctx)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "readonly")

    def test_group_member_can_still_query(self):
        ctx = policy.TurnContext(readonly=True, client="qq")
        self.assertTrue(policy.check("get_weather", {}, ctx).allowed)
        self.assertTrue(policy.check("web_search", {"query": "x"}, ctx).allowed)

    def test_non_readonly_unaffected(self):
        ctx = policy.TurnContext(user_text="重启")
        self.assertTrue(policy.check("run_command", {"command": "systemctl reboot"}, ctx).allowed)


class NormalizeEventTest(unittest.TestCase):
    def test_private_string_message(self):
        ev = qqbot.normalize_event({
            "post_type": "message", "message_type": "private", "user_id": 10001,
            "self_id": 99999,
            "message": "meido /status",
        })
        self.assertEqual(ev.uin, "10001")
        self.assertEqual(ev.text, "meido /status")
        self.assertIsNone(ev.group_id)

    def test_group_cq_at_detection(self):
        ev = qqbot.normalize_event({
            "post_type": "message", "message_type": "group", "user_id": 30003,
            "group_id": 20001, "self_id": 99999,
            "message": "[CQ:at,qq=99999] 你好",
        })
        self.assertTrue(ev.at_bot)
        self.assertEqual(ev.group_id, "20001")
        self.assertNotIn("CQ:at", ev.text)

    def test_ignores_non_message(self):
        self.assertIsNone(qqbot.normalize_event({"post_type": "meta_event"}))
        self.assertIsNone(qqbot.normalize_event({"post_type": "notice"}))


if __name__ == "__main__":
    unittest.main()
