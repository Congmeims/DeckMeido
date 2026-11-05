"""设备配对（server/pairing.py）与监听配置（server/netcfg.py）的单元测试。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from server import audit, netcfg, pairing


class PairingTestBase(unittest.TestCase):
    def setUp(self):
        audit.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "security" / "devices.json"
        self.clock = [1000.0]
        self.mgr = pairing.PairingManager(self.path, now=lambda: self.clock[0])

    def tearDown(self):
        self._tmp.cleanup()
        audit.clear()

    def _session(self, ip="192.168.1.20", ua="Mozilla/5.0 (Windows NT 10.0) Chrome/120"):
        started = self.mgr.start(ip, ua)
        return started["pair_id"], self.mgr.pending()[0]

    def _pair(self, remember_ip="192.168.1.20"):
        pair_id, s = self._session(remember_ip)
        self.mgr.decide(pair_id, True, "127.0.0.1")
        return pair_id, s["pin"]


class PinLifecycleTest(PairingTestBase):
    def test_start_returns_no_pin_to_browser(self):
        started, pending = self._session()
        self.assertEqual(len(pending["pin"]), 4)
        self.assertTrue(pending["pin"].isdigit())
        self.assertNotIn("pin", started)

    def test_requires_host_allow_before_confirm(self):
        pair_id, s = self._session()
        with self.assertRaises(pairing.PairError) as ctx:
            self.mgr.confirm(pair_id, s["pin"])
        self.assertEqual(ctx.exception.code, "not_approved")

    def test_pin_is_one_time(self):
        pair_id, pin = self._pair()
        device, token = self.mgr.confirm(pair_id, pin)
        self.assertTrue(token)
        self.assertEqual(device["ip"], "192.168.1.20")
        self.assertIsNotNone(self.mgr.devices.authenticate(token))
        # 同一 pair_id + PIN 不能重放
        with self.assertRaises(pairing.PairError) as ctx:
            self.mgr.confirm(pair_id, pin)
        self.assertEqual(ctx.exception.status, 409)

    def test_pin_expires_after_60s(self):
        pair_id, pin = self._pair()
        self.clock[0] += 61
        self.assertEqual(self.mgr.status(pair_id)["state"], "expired")
        with self.assertRaises(pairing.PairError) as ctx:
            self.mgr.confirm(pair_id, pin)
        self.assertEqual(ctx.exception.code, "expired")

    def test_three_wrong_attempts_lock_session(self):
        pair_id, pin = self._pair()
        for _ in range(2):
            with self.assertRaises(pairing.PairError) as ctx:
                self.mgr.confirm(pair_id, "0000" if pin != "0000" else "1111")
            self.assertEqual(ctx.exception.code, "bad_pin")
        with self.assertRaises(pairing.PairError) as ctx:
            self.mgr.confirm(pair_id, "0000" if pin != "0000" else "1111")
        self.assertEqual(ctx.exception.code, "too_many")
        # 锁定后即使 PIN 正确也不能再通过
        with self.assertRaises(pairing.PairError):
            self.mgr.confirm(pair_id, pin)

    def test_refresh_regenerates_pin_and_resets_attempts(self):
        pair_id, pin = self._pair()
        with self.assertRaises(pairing.PairError):
            self.mgr.confirm(pair_id, "0000" if pin != "0000" else "1111")
        refreshed = self.mgr.refresh(pair_id)
        self.assertEqual(refreshed["pin"], self.mgr.pending()[0]["pin"])
        self.assertEqual(refreshed["attempts"], 0)
        with self.assertRaises(pairing.PairError):
            self.mgr.confirm(pair_id, pin)  # 旧 PIN 作废
        self.mgr.confirm(pair_id, refreshed["pin"])

    def test_deny_blocks_confirm(self):
        pair_id, _ = self._session()
        self.mgr.decide(pair_id, False, "127.0.0.1")
        with self.assertRaises(pairing.PairError) as ctx:
            self.mgr.confirm(pair_id, "0000")
        self.assertEqual(ctx.exception.status, 403)


class DeviceStoreTest(PairingTestBase):
    def test_token_stored_as_hash_only(self):
        pair_id, pin = self._pair()
        _, token = self.mgr.confirm(pair_id, pin)
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn(token, raw)
        data = json.loads(raw)["devices"][0]
        self.assertEqual(data["token_hash"], pairing.token_hash(token))
        self.assertNotIn("token", data)
        if os.name != "nt":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_remove_invalidates_cookie_immediately(self):
        pair_id, pin = self._pair()
        device, token = self.mgr.confirm(pair_id, pin)
        self.assertIsNotNone(self.mgr.devices.authenticate(token))
        self.assertTrue(self.mgr.devices.remove(device["device_id"]))
        self.assertIsNone(self.mgr.devices.authenticate(token))

    def test_rename_and_remove_all(self):
        pair_id, pin = self._pair()
        device, _ = self.mgr.confirm(pair_id, pin)
        renamed = self.mgr.devices.rename(device["device_id"], "  客厅平板  ")
        self.assertEqual(renamed["name"], "客厅平板")
        with self.assertRaises(pairing.PairError):
            self.mgr.devices.rename(device["device_id"], "   ")
        self.assertEqual(self.mgr.devices.count(), 1)
        self.assertEqual(self.mgr.devices.remove_all(), 1)
        self.assertEqual(self.mgr.devices.list(), [])

    def test_device_fields_present(self):
        pair_id, pin = self._pair()
        device, _ = self.mgr.confirm(pair_id, pin)
        for key in ("device_id", "name", "ip", "created_at", "last_seen"):
            self.assertIn(key, device)
        self.assertNotIn("token_hash", device)


class AuditTest(PairingTestBase):
    def test_pair_fail_and_success_are_recorded_without_secrets(self):
        pair_id, pin = self._pair()
        with self.assertRaises(pairing.PairError):
            self.mgr.confirm(pair_id, "0000" if pin != "0000" else "1111")
        _, token = self.mgr.confirm(pair_id, pin)
        events = audit.recent(20)
        self.assertTrue(any(e["action"] == "confirm" and e["result"] == "fail"
                            for e in events))
        self.assertTrue(any(e["action"] == "confirm" and e["result"] == "ok"
                            for e in events))
        blob = json.dumps(events, ensure_ascii=False)
        self.assertNotIn(token, blob)
        self.assertNotIn(pin, blob.replace("pin", ""))  # 事件里只允许出现 reason=pin
        for ev in events:
            for key in ("ts", "kind", "action", "result", "ip", "reason", "detail"):
                self.assertIn(key, ev)

    def test_ring_buffer_is_bounded(self):
        for i in range(audit.MAX_EVENTS + 40):
            audit.record("test", "w", "ok", ip="1.2.3.4", detail=str(i))
        self.assertEqual(len(audit.recent(audit.MAX_EVENTS)), audit.MAX_EVENTS)


class NetcfgTest(unittest.TestCase):
    def test_port_priority_env_then_config_then_default(self):
        self.assertEqual(netcfg.resolve_port("18766", {"port": 19000}), 18766)
        self.assertEqual(netcfg.resolve_port(None, {"port": 19000}), 19000)
        self.assertEqual(netcfg.resolve_port("", {}), netcfg.DEFAULT_PORT)
        self.assertEqual(netcfg.resolve_port("abc", {"port": "19000"}), 19000)

    def test_port_range_validation(self):
        self.assertEqual(netcfg.valid_port(1024), 1024)
        self.assertEqual(netcfg.valid_port(65535), 65535)
        self.assertIsNone(netcfg.valid_port(80))
        self.assertIsNone(netcfg.valid_port(70000))
        self.assertIsNone(netcfg.valid_port("x"))

    def test_lan_enable_controls_bind_host(self):
        self.assertEqual(netcfg.resolve_host({}), "127.0.0.1")
        self.assertEqual(netcfg.resolve_host({"lan_enable": False}), "127.0.0.1")
        self.assertEqual(netcfg.resolve_host({"lan_enable": True}), "0.0.0.0")

    def test_loopback_detection(self):
        self.assertTrue(netcfg.is_loopback("127.0.0.1"))
        self.assertTrue(netcfg.is_loopback("::1"))
        self.assertTrue(netcfg.is_loopback(""))
        self.assertFalse(netcfg.is_loopback("192.168.1.10"))

    def test_auth_loopback_is_strict(self):
        """鉴权免检只认真正的回环地址，空来源/伪造字符串不算本机。"""
        self.assertTrue(netcfg.is_local("127.0.0.1"))
        self.assertTrue(netcfg.is_local("::1"))
        self.assertTrue(netcfg.is_local("::ffff:127.0.0.1"))
        self.assertFalse(netcfg.is_local(""))
        self.assertFalse(netcfg.is_local("localhost"))
        self.assertFalse(netcfg.is_local("192.168.1.10"))
        self.assertFalse(netcfg.is_local("10.0.0.2"))
        self.assertFalse(netcfg.is_local("127.0.0.1.evil.example"))
