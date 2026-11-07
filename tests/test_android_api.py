"""Android 客户端新增的两个后端接口回归测试。

覆盖：
- `GET /api/sessions/{sid}/history` 只读、不切换当前会话、路径校验；
- `POST /api/tts/say` 鉴权、参数校验、返回 WAV、临时文件清理。

    python -m unittest tests.test_android_api -v
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 导入 server.main 会读配置、做进程守卫；指到临时数据根，别碰真实 userdata
_ENV = ("DECKMEIDO_DATA_DIR", "DECKMEIDO_ALLOW_SECOND_BACKEND", "DECKMEIDO_PORT")
_OLD = {k: os.environ.get(k) for k in _ENV}
_DATA = tempfile.mkdtemp(prefix="dm-android-api-")
if "ptyprocess" not in sys.modules:
    try:
        import ptyprocess  # noqa: F401
    except Exception:
        sys.modules["ptyprocess"] = types.ModuleType("ptyprocess")
os.environ["DECKMEIDO_DATA_DIR"] = _DATA
os.environ["DECKMEIDO_ALLOW_SECOND_BACKEND"] = "1"
os.environ["DECKMEIDO_PORT"] = "18798"
shutil.copy(ROOT / "server" / "config.example.toml", Path(_DATA) / "config.toml")
# 远程接口测试需要局域网访问开启；否则 require_auth 会直接 403
_cfg = Path(_DATA) / "config.toml"
_cfg.write_text(_cfg.read_text(encoding="utf-8").replace(
    "lan_enable = false", "lan_enable = true"), encoding="utf-8")
from server import main, pairing  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def tearDownModule():
    for k, v in _OLD.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _paired_token(ip: str = "192.168.1.50") -> str:
    started = pairing.manager.start(ip, "DeckMeidoAndroid/0.1 (Android)")
    pair_id = started["pair_id"]
    pending = pairing.manager.pending()
    pin = pending[0]["pin"]
    pairing.manager.decide(pair_id, True, "127.0.0.1")
    _device, token = pairing.manager.confirm(pair_id, pin, ip)
    return token


class AndroidApiTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)
        cls.token = _paired_token()
        cls.auth = {"Cookie": f"deckmeido_device={cls.token}"}

    def _write_session(self, sid: str, title: str, history: list[dict]) -> Path:
        p = main.store.sess_path(sid)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "id": sid,
            "title": title,
            "created": 1000.0,
            "updated": 1001.0,
            "history": history,
        }, ensure_ascii=False), encoding="utf-8")
        return p


class SessionHistoryApiTest(AndroidApiTestBase):
    def test_需要设备鉴权(self):
        sid = "0123abcd"
        self._write_session(sid, "测试", [])
        r = self.client.get(f"/api/sessions/{sid}/history")
        self.assertEqual(r.status_code, 401)

    def test_读取历史且不切换当前会话(self):
        sid = "0123abcd"
        old_sid = main.store.current_sid
        old_hist = list(main.store.history)
        self._write_session(sid, "历史标题", [
            {"role": "user", "content": "你好", "ts": 1000.0},
            {"role": "assistant", "content": "你好呀", "ts": 1001.0},
        ])
        r = self.client.get(f"/api/sessions/{sid}/history", headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["id"], sid)
        self.assertEqual(body["title"], "历史标题")
        self.assertEqual(len(body["messages"]), 2)
        self.assertEqual(body["messages"][1]["content"], "你好呀")
        # 只读接口不能把服务端当前会话切走
        self.assertEqual(main.store.current_sid, old_sid)
        self.assertEqual(main.store.history, old_hist)

    def test_不存在的会话返回404(self):
        r = self.client.get("/api/sessions/nope1234/history", headers=self.auth)
        self.assertEqual(r.status_code, 404)

    def test_会话号不允许路径穿越(self):
        for bad in ("..", "a.b", "a/b", "%2e%2e"):
            r = self.client.get(f"/api/sessions/{bad}/history", headers=self.auth)
            self.assertIn(r.status_code, (404, 405), f"{bad}: {r.status_code}")


class TtsSayApiTest(AndroidApiTestBase):
    def test_需要设备鉴权(self):
        r = self.client.post("/api/tts/say", json={"text": "你好"})
        self.assertEqual(r.status_code, 401)

    def test_空文本返回400(self):
        r = self.client.post("/api/tts/say", json={"text": "  "}, headers=self.auth)
        self.assertEqual(r.status_code, 400)

    def test_超长文本返回400(self):
        r = self.client.post("/api/tts/say", json={"text": "a" * 2001},
                             headers=self.auth)
        self.assertEqual(r.status_code, 400)

    def test_不支持的语言返回400(self):
        r = self.client.post("/api/tts/say",
                             json={"text": "你好", "language": "xx"},
                             headers=self.auth)
        self.assertEqual(r.status_code, 400)

    def test_返回wav并在响应后删除临时文件(self):
        tmp = tempfile.NamedTemporaryFile(prefix="deckmeido-test-", suffix=".wav",
                                          delete=False)
        tmp.write(b"RIFF....WAVE")
        tmp.close()
        path = Path(tmp.name)
        with mock.patch.object(main.tts, "synth_wav", return_value=path) as synth:
            r = self.client.post("/api/tts/say",
                                 json={"text": "你好", "language": "zh"},
                                 headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers["content-type"], "audio/wav")
        self.assertEqual(r.content, b"RIFF....WAVE")
        synth.assert_called_once_with("你好", "zh")
        self.assertFalse(path.exists(), "临时 WAV 应在响应结束后删除")


if __name__ == "__main__":
    unittest.main()
