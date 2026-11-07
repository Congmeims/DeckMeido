"""文件管理器上传：按磁盘剩余空间动态定上限 + 原始流写盘。

覆盖：
- 上限 = 可用空间 - 安全余量；小盘按剩余一半留余量；无空间时为 0；
- 413 文案同时给出「当前上限」与「该文件大小」；
- 客户端声明的体积超限时直接拒绝；
- 实际写入超过上限时中断并清理半成品；
- 正常大小落盘成功；
- 只读/危险区仍然 403，放宽上限没有绕过写权限判断；
- 未配对设备 401。

    python -m unittest tests.test_upload_limits -v
"""
from __future__ import annotations

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
_DATA = tempfile.mkdtemp(prefix="dm-upload-limit-")
if "ptyprocess" not in sys.modules:
    try:
        import ptyprocess  # noqa: F401
    except Exception:
        sys.modules["ptyprocess"] = types.ModuleType("ptyprocess")
os.environ["DECKMEIDO_DATA_DIR"] = _DATA
os.environ["DECKMEIDO_ALLOW_SECOND_BACKEND"] = "1"
os.environ["DECKMEIDO_PORT"] = "18799"
shutil.copy(ROOT / "server" / "config.example.toml", Path(_DATA) / "config.toml")
_cfg_path = Path(_DATA) / "config.toml"
_cfg_path.write_text(
    _cfg_path.read_text(encoding="utf-8").replace(
        "lan_enable = false", "lan_enable = true"), encoding="utf-8")

from server import main, pairing  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def tearDownModule():
    for k, v in _OLD.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    shutil.rmtree(_DATA, ignore_errors=True)


def _paired_token(ip: str = "192.168.1.90") -> str:
    started = pairing.manager.start(ip, "DeckMeidoUploadTest/1.0")
    pair_id = started["pair_id"]
    pin = pairing.manager.pending()[0]["pin"]
    pairing.manager.decide(pair_id, True, "127.0.0.1")
    _device, token = pairing.manager.confirm(pair_id, pin, ip)
    return token


class UploadLimitCalcTest(unittest.TestCase):
    """上限判定是纯函数，直接对 upload_limits / _fmt_bytes 断言。"""

    def _limits_for(self, free: int) -> dict:
        usage = mock.Mock()
        usage.free = free
        with mock.patch.object(main.shutil, "disk_usage", return_value=usage):
            return main.upload_limits(tempfile.gettempdir())

    def test_大盘保留一百二十八兆余量(self):
        free = 1024 * 1024 * 1024
        caps = self._limits_for(free)
        self.assertEqual(caps["free"], free)
        self.assertEqual(caps["reserve"], main.UPLOAD_FREE_RESERVE)
        self.assertEqual(caps["limit"], free - main.UPLOAD_FREE_RESERVE)

    def test_小盘按剩余一半留余量(self):
        free = 100 * 1024 * 1024
        caps = self._limits_for(free)
        # 100MB < 2×128MB，余量改成一半，避免小盘完全不让传
        self.assertEqual(caps["reserve"], free // 2)
        self.assertEqual(caps["limit"], free - free // 2)

    def test_没有剩余空间时上限为零(self):
        self.assertEqual(self._limits_for(0)["limit"], 0)

    def test_拿不到磁盘信息时按零处理(self):
        with mock.patch.object(main.shutil, "disk_usage", side_effect=OSError):
            caps = main.upload_limits(tempfile.gettempdir())
        self.assertEqual(caps["free"], 0)
        self.assertEqual(caps["limit"], 0)

    def test_体积格式化(self):
        self.assertEqual(main._fmt_bytes(0), "0 B")
        self.assertEqual(main._fmt_bytes(1023), "1023 B")
        self.assertEqual(main._fmt_bytes(1024), "1.0 KB")
        self.assertEqual(main._fmt_bytes(1536), "1.5 KB")
        self.assertEqual(main._fmt_bytes(128 * 1024 * 1024), "128.0 MB")

    def test_超限文案同时包含上限和文件大小(self):
        err = main._upload_limit_error(100 * 1024 * 1024, 200 * 1024 * 1024)
        self.assertFalse(err["ok"])
        self.assertIn("100.0 MB", err["detail"])
        self.assertIn("200.0 MB", err["detail"])
        self.assertEqual(err["limit"], 100 * 1024 * 1024)
        self.assertEqual(err["size"], 200 * 1024 * 1024)


class UploadStreamApiTest(unittest.TestCase):
    """原始流接口：声明超限直接拒绝、实际超限清理半成品、正常落盘、权限不放宽。"""

    @classmethod
    def setUpClass(cls):
        cls._env = mock.patch.dict(os.environ, {
            "DECKMEIDO_DATA_DIR": _DATA,
            "DECKMEIDO_ALLOW_SECOND_BACKEND": "1",
        })
        cls._env.start()
        cls.workspace = tempfile.mkdtemp(prefix="dm-upload-ws-")
        cls._ws = mock.patch.object(main.tools, "WORKSPACE", Path(cls.workspace))
        cls._ws.start()
        # 固定上限 3KB，避免测试真去读磁盘剩余空间
        cls.caps = {"free": 4096, "reserve": 1024, "limit": 3072}
        cls._limit = mock.patch.object(main, "upload_limits",
                                       return_value=dict(cls.caps))
        cls._limit.start()
        cls.client = TestClient(main.app)
        cls.token = _paired_token()
        cls.auth = {"Cookie": f"deckmeido_device={cls.token}"}

    @classmethod
    def tearDownClass(cls):
        cls._limit.stop()
        cls._ws.stop()
        cls._env.stop()
        shutil.rmtree(cls.workspace, ignore_errors=True)

    def _post(self, name: str, body: bytes, declared=None, headers=None):
        h = dict(self.auth)
        if declared is not None:
            h["X-File-Size"] = str(declared)
        if headers:
            h.update(headers)
        return self.client.post(
            f"/api/upload-stream?dir={self.workspace}&name={name}",
            content=body, headers=h,
        )

    def test_未配对设备返回401(self):
        r = self.client.post(
            f"/api/upload-stream?dir={self.workspace}&name=a.bin",
            content=b"abc", headers={"X-File-Size": "3"},
        )
        self.assertEqual(r.status_code, 401, r.text)

    def test_声明大小超限直接413(self):
        r = self._post("big.bin", b"x" * 4, declared=9999)
        self.assertEqual(r.status_code, 413, r.text)
        body = r.json()
        self.assertIn("3.0 KB", body["detail"])
        self.assertIn("9.8 KB", body["detail"])
        self.assertEqual(body["limit"], 3072)
        self.assertEqual(body["size"], 9999)
        self.assertFalse((Path(self.workspace) / "big.bin").exists())

    def test_实际写入超限会清理半成品(self):
        r = self._post("overflow.bin", b"x" * 5000)
        self.assertEqual(r.status_code, 413, r.text)
        self.assertIn("当前上限", r.json()["detail"])
        self.assertFalse((Path(self.workspace) / "overflow.bin").exists())

    def test_未超限正常落盘(self):
        payload = b"y" * 2000
        r = self._post("ok.bin", payload, declared=len(payload))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["ok"])
        self.assertEqual((Path(self.workspace) / "ok.bin").read_bytes(), payload)
        (Path(self.workspace) / "ok.bin").unlink()

    def test_只读危险区仍然403(self):
        outside = tempfile.mkdtemp(prefix="dm-upload-outside-")
        try:
            r = self.client.post(
                f"/api/upload-stream?dir={outside}&name=a.bin",
                content=b"abc", headers={**self.auth, "X-File-Size": "3"},
            )
            self.assertEqual(r.status_code, 403, r.text)
            body = r.json()
            self.assertFalse(body["ok"])
            self.assertTrue(body.get("reason"), body)
            self.assertFalse((Path(outside) / "a.bin").exists())
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_上限接口返回当前上限(self):
        r = self.client.get(
            f"/api/upload-limit?dir={self.workspace}", headers=self.auth)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["limit"], 3072)
        self.assertEqual(body["reserve"], 1024)
        self.assertEqual(body["free"], 4096)
