"""跨屏键鼠接管的最小回归：鉴权、协议、位移合并、释放与超时。

只依赖标准库，Windows / Linux 都能跑：
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import remote_input as ri  # noqa: E402


class FakeBackend:
    """不碰真实设备，只记录调用；异步接口与真后端保持一致。"""

    kind = "fake"

    def __init__(self):
        self.moves = []
        self.buttons = []
        self.keys = []
        self.wheels = []
        self.released = []
        self.closed = 0

    async def start(self):
        return self

    async def move(self, dx, dy):
        self.moves.append((dx, dy))

    async def button(self, b, down):
        self.buttons.append((b, down))

    async def wheel(self, d):
        self.wheels.append(d)

    async def key(self, code, down):
        self.keys.append((code, down))

    async def release_all(self, keys, buttons):
        self.released.append((set(keys), set(buttons)))

    async def close(self):
        self.closed += 1


def make_session(backend=None, minutes=30):
    backend = backend or FakeBackend()
    return ri.InputSession(backend, minutes), backend


class AccessTest(unittest.TestCase):
    def cfg(self, **kw):
        base = {"enabled": True, "bind_lan": True}
        base.update(kw)
        return base

    def test_unauthorized_rejected(self):
        """令牌错误或为空都必须被拒，且先于开关判断"""
        self.assertEqual(
            ri.access_error(self.cfg(), "192.168.1.9", "abcd", "efgh"),
            ri.RI_UNAUTHORIZED)
        self.assertEqual(
            ri.access_error(self.cfg(), "192.168.1.9", "abcd", ""),
            ri.RI_UNAUTHORIZED)
        self.assertEqual(
            ri.access_error(self.cfg(), "192.168.1.9", "", "x"),
            ri.RI_UNAUTHORIZED)

    def test_enabled_and_lan_gate(self):
        self.assertEqual(
            ri.access_error(self.cfg(enabled=False), "127.0.0.1", "abcd", "abcd"),
            ri.RI_DISABLED)
        # 旧配置 bind_lan = false 时局域网来源被拒，本机回环放行
        self.assertEqual(
            ri.access_error(self.cfg(bind_lan=False), "192.168.1.9", "abcd", "abcd"),
            ri.RI_LOCAL_ONLY)
        self.assertIsNone(
            ri.access_error(self.cfg(bind_lan=False), "127.0.0.1", "abcd", "abcd"))
        self.assertIsNone(
            ri.access_error(self.cfg(), "192.168.1.9", "abcd", "ABCD"))

    def test_merged_switch_allows_lan(self):
        """合并成单开关后，新配置只有 enabled = true 就应该允许局域网"""
        self.assertIsNone(
            ri.access_error({"enabled": True}, "192.168.1.9", "abcd", "abcd"))
        self.assertFalse(ri.enabled({"enabled": False}))
        self.assertTrue(ri.enabled({"enabled": True}))

    def test_legacy_bind_lan_still_limits(self):
        """旧配置残留的 bind_lan = false 不能被合并开关悄悄放开到局域网"""
        legacy = {"enabled": True, "bind_lan": False}
        self.assertEqual(
            ri.access_error(legacy, "192.168.1.9", "abcd", "abcd"),
            ri.RI_LOCAL_ONLY)
        self.assertIsNone(
            ri.access_error(legacy, "127.0.0.1", "abcd", "abcd"))
        self.assertFalse(ri.enabled(legacy))
        self.assertTrue(ri.enabled({"enabled": True, "bind_lan": True}))


class KeyMapTest(unittest.TestCase):
    def test_required_codes_present(self):
        """交付要求点名的常用键必须全部能映射"""
        missing = [c for c in ri.REQUIRED_CODES if c not in ri.KEYMAP]
        self.assertEqual(missing, [])

    def test_codes_are_valid_linux_keys(self):
        self.assertTrue(all(0 < v <= 0x2ff for v in ri.KEYMAP.values()))
        self.assertEqual(ri.KEYMAP["Escape"], 1)
        self.assertEqual(ri.KEYMAP["Enter"], 28)
        self.assertEqual(ri.KEYMAP["Space"], 57)
        self.assertEqual(ri.KEYMAP["F12"], 88)
        self.assertEqual(ri.KEYMAP["ArrowUp"], 103)
        self.assertEqual(ri.KEYMAP["MetaLeft"], 125)


class SessionTest(unittest.IsolatedAsyncioTestCase):
    async def test_move_batching(self):
        """连续 mousemove 必须合并成一次注入，而不是每个事件刷一个进程"""
        sess, backend = make_session()
        await sess.handle({"t": "move", "dx": 5, "dy": 3})
        await sess.handle({"t": "move", "dx": -2, "dy": 4})
        await sess.flush_moves()
        self.assertEqual(backend.moves, [(3, 7)])

    async def test_move_clamped(self):
        sess, _ = make_session()
        await sess.handle({"t": "move", "dx": 99999, "dy": -99999})
        self.assertEqual(sess.pending_dx, ri.MAX_ABS_MOVE)
        self.assertEqual(sess.pending_dy, -ri.MAX_ABS_MOVE)

    async def test_abnormal_disconnect_releases_held(self):
        """超时/断线结束时，按下的键盘和鼠标键都必须补发抬起"""
        sess, backend = make_session()
        await sess.handle({"t": "key", "code": "KeyA", "p": True})
        await sess.handle({"t": "btn", "b": 0, "p": True})
        await sess.handle({"t": "btn", "b": 2, "p": True})
        self.assertEqual(sess.held_keys, {ri.KEYMAP["KeyA"]})
        self.assertEqual(sess.held_buttons, {0, 2})
        await sess.end(ri.RI_TIMEOUT)
        self.assertEqual(backend.released,
                         [({ri.KEYMAP["KeyA"]}, {0, 2})])
        self.assertEqual(backend.closed, 1)

    async def test_end_is_idempotent(self):
        sess, backend = make_session()
        await sess.handle({"t": "key", "code": "KeyW", "p": True})
        self.assertTrue(await sess.end(ri.RI_DISCONNECTED))
        self.assertFalse(await sess.end(ri.RI_RELEASED))
        self.assertEqual(len(backend.released), 1)

    async def test_unmapped_key_ignored(self):
        sess, backend = make_session()
        await sess.handle({"t": "key", "code": "NumpadComma", "p": True})
        self.assertEqual(backend.keys, [])
        self.assertEqual(sess.held_keys, set())

    async def test_protocol_errors(self):
        sess, _ = make_session()
        for bad in ({"t": "btn", "b": 9, "p": True},
                    {"t": "key", "code": 123, "p": True},
                    {"t": "nope"}):
            with self.assertRaises(ri.RemoteInputError) as cm:
                await sess.handle(bad)
            self.assertEqual(cm.exception.code, ri.RI_BAD_MSG)

    async def test_release_message(self):
        sess, _ = make_session()
        self.assertEqual(await sess.handle({"t": "release"}), ri.RI_RELEASED)

    async def test_timeout_and_deadline(self):
        sess, _ = make_session(minutes=30)
        now = time.monotonic()
        sess.last_seen = now - 11
        self.assertTrue(sess.heartbeat_expired(now))
        sess.last_seen = now
        self.assertFalse(sess.heartbeat_expired(now))
        self.assertAlmostEqual(sess.deadline - sess.started, 1800, delta=1)
        self.assertFalse(sess.expired(now))
        self.assertTrue(sess.expired(now + 1801))


if __name__ == "__main__":
    unittest.main()
