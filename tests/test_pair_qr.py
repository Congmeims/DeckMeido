"""扫码配对接口回归测试。

覆盖：
- 生成二维码必须鉴权，返回一次性 token 与可解码的 PNG；
- 换取凭据校验有效期、一次性、撤销，失败原因明确；
- 换到的凭据与 PIN 配对同权（能访问受保护接口）；
- 日志里只留脱敏标识，不出现 token 明文。

    python -m unittest tests.test_pair_qr -v

数据根在 setUpClass 里用临时目录设置、tearDownClass 里恢复并删除，所以
`python -m unittest discover -s tests` 里不依赖外部环境变量，也不会写到
仓库的 userdata/（不受当前工作目录影响）。
"""
from __future__ import annotations

import base64
import importlib
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if "ptyprocess" not in sys.modules:
    try:
        import ptyprocess  # noqa: F401
    except Exception:
        sys.modules["ptyprocess"] = types.ModuleType("ptyprocess")

from fastapi.testclient import TestClient  # noqa: E402

_ENV = ("DECKMEIDO_DATA_DIR", "DECKMEIDO_ALLOW_SECOND_BACKEND", "DECKMEIDO_PORT")


def _paired_token(ip: str = "192.168.1.60") -> str:
    started = pairing.manager.start(ip, "DeckMeidoDeck/0.1 (Linux)")
    pair_id = started["pair_id"]
    pending = pairing.manager.pending()
    pin = pending[0]["pin"]
    pairing.manager.decide(pair_id, True, "127.0.0.1")
    _device, token = pairing.manager.confirm(pair_id, pin, ip)
    return token


class PairQrTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global main, pairing
        cls._old_env = {key: os.environ.get(key) for key in _ENV}
        cls._data = tempfile.mkdtemp(prefix="dm-pair-qr-")
        os.environ["DECKMEIDO_DATA_DIR"] = cls._data
        os.environ["DECKMEIDO_ALLOW_SECOND_BACKEND"] = "1"
        os.environ["DECKMEIDO_PORT"] = "18799"
        cfg = Path(cls._data) / "config.toml"
        shutil.copy(ROOT / "server" / "config.example.toml", cfg)
        cfg.write_text(cfg.read_text(encoding="utf-8").replace(
            "lan_enable = false", "lan_enable = true"), encoding="utf-8")

        # 数据根设好之后再导入：main 在导入期会读配置、做进程守卫
        main = importlib.import_module("server.main")
        pairing = importlib.import_module("server.pairing")
        # 设备表是模块级单例：换一个新实例，避免沿用其他测试模块缓存的内存设备，
        # 同时让 devices.json 跟着本次的临时数据根走
        pairing.manager = pairing.PairingManager()

        cls.client = TestClient(main.app)
        cls.token = _paired_token()
        cls.auth = {"Cookie": f"deckmeido_device={cls.token}"}

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        with main._qr_lock:
            main._qr_tokens.clear()
        main._qr_fails.clear()
        # 后续测试模块要用自己的数据根：恢复环境变量，并换回干净的设备表
        pairing.manager = pairing.PairingManager()
        for key, value in cls._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(cls._data, ignore_errors=True)


    def setUp(self):
        with main._qr_lock:
            main._qr_tokens.clear()
        main._qr_fails.clear()
        # TestClient 自带 CookieJar：上一条用例换取到的设备 Cookie 会残留，
        # 不清理的话「未鉴权」用例会被当成已配对设备
        self.client.cookies.clear()

    def _generate(self, host: str = "192.168.1.9:18799") -> dict:
        r = self.client.post("/api/pair/qr", json={"host": host}, headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def _redeem(self, token: str, headers: dict | None = None):
        return self.client.post("/api/pair/qr/redeem", json={"token": token},
                                headers=headers or {})

    # ---------- 生成 ----------

    def test_生成二维码需要鉴权(self):
        r = self.client.post("/api/pair/qr", json={"host": "192.168.1.9:18799"})
        self.assertEqual(r.status_code, 401)

    def test_返回一次性token与可解码的二维码(self):
        data = self._generate()
        self.assertTrue(data["ok"])
        self.assertEqual(data["address"], "http://192.168.1.9:18799")
        self.assertEqual(data["expires_in"], int(main.QR_TTL_S))
        self.assertTrue(data["payload"].startswith("deckmeido://pair?v=1&u="))
        self.assertIn("http%3A%2F%2F192.168.1.9%3A18799", data["payload"])
        self.assertTrue(data["qr"].startswith("data:image/png;base64,"))
        raw = base64.b64decode(data["qr"].split(",", 1)[1])
        self.assertTrue(raw.startswith(b"\x89PNG"))

    def test_刷新会让旧token立即失效(self):
        first = self._generate()
        old_token = self._payload_token(first["payload"])
        self._generate()
        r = self._redeem(old_token)
        self.assertEqual(r.status_code, 410, r.text)
        self.assertEqual(r.json()["code"], "revoked")

    def test_地址只保留根地址(self):
        data = self._generate("https://deck.example.com:9443/settings?x=1")
        self.assertEqual(data["address"], "https://deck.example.com:9443")

    # ---------- 换取 ----------

    def test_有效token换取凭据并能访问受保护接口(self):
        data = self._generate()
        token = self._payload_token(data["payload"])
        r = self._redeem(token, {"User-Agent": "DeckMeidoAndroid/0.1 (Android)"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["device"]["name"], "扫码设备")
        cookie = r.cookies.get(pairing.COOKIE_NAME)
        self.assertTrue(cookie, "换取成功后必须下发设备 Cookie")
        r2 = self.client.get("/api/devices",
                             headers={"Cookie": f"{pairing.COOKIE_NAME}={cookie}"})
        self.assertEqual(r2.status_code, 200, r2.text)
        # 换到的设备与 PIN 配对同权：出现在同一张设备表里
        self.assertTrue(any(d["device_id"] == body["device"]["device_id"]
                            for d in r2.json()["devices"]))

    def test_重复使用返回已使用(self):
        data = self._generate()
        token = self._payload_token(data["payload"])
        self.assertEqual(self._redeem(token).status_code, 200)
        r = self._redeem(token)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["code"], "used")

    def test_过期返回过期(self):
        data = self._generate()
        token = self._payload_token(data["payload"])
        rec = main._qr_tokens[pairing.token_hash(token)]
        rec["expires"] = time.time() - 1
        r = self._redeem(token)
        self.assertEqual(r.status_code, 410, r.text)
        self.assertEqual(r.json()["code"], "expired")

    def test_撤销后返回撤销(self):
        data = self._generate()
        token = self._payload_token(data["payload"])
        r = self.client.post("/api/pair/qr/revoke", headers=self.auth)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["revoked"], 1)
        r = self._redeem(token)
        self.assertEqual(r.status_code, 410, r.text)
        self.assertEqual(r.json()["code"], "revoked")

    def test_无效token返回明确原因(self):
        r = self._redeem("not-a-real-token")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "invalid")
        self.assertFalse(r.json()["ok"])

    def test_撤销接口需要鉴权(self):
        r = self.client.post("/api/pair/qr/revoke")
        self.assertEqual(r.status_code, 401)

    def test_扫码页面需要鉴权且页面里不带token(self):
        r = self.client.get("/pair/qr", headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("/api/pair/qr", r.text)
        self.assertNotIn("deckmeido://", r.text)
        self.assertEqual(self.client.get("/pair/qr").status_code, 401)

    # ---------- 日志脱敏 ----------

    def test_审计日志不含token明文(self):
        data = self._generate()
        token = self._payload_token(data["payload"])
        self._redeem(token)
        for event in main.audit.recent(50):
            self.assertNotIn(token, str(event))
            self.assertNotIn(data["payload"], str(event))

    @staticmethod
    def _payload_token(payload: str) -> str:
        return payload.split("&t=", 1)[1]


if __name__ == "__main__":
    unittest.main()
