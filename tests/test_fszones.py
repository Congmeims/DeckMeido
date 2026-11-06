"""文件管理器三级分区：判定、realpath 越界、`..` 绕过、写操作 403、禁浏览。

    python -m unittest tests.test_fszones -v
"""
from __future__ import annotations

import asyncio
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

from server import fszones  # noqa: E402

# 导入 server.main 会读配置、做进程守卫；指到临时数据根，别碰真实 userdata
_ENV = ("DECKMEIDO_DATA_DIR", "DECKMEIDO_ALLOW_SECOND_BACKEND", "DECKMEIDO_PORT")
_OLD = {k: os.environ.get(k) for k in _ENV}
_DATA = tempfile.mkdtemp(prefix="dm-fszone-")
if "ptyprocess" not in sys.modules:
    try:
        import ptyprocess  # noqa: F401
    except Exception:
        sys.modules["ptyprocess"] = types.ModuleType("ptyprocess")
os.environ["DECKMEIDO_DATA_DIR"] = _DATA
os.environ["DECKMEIDO_ALLOW_SECOND_BACKEND"] = "1"
os.environ["DECKMEIDO_PORT"] = "18797"
shutil.copy(ROOT / "server" / "config.example.toml", Path(_DATA) / "config.toml")
from server import main  # noqa: E402

for _k, _v in _OLD.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v


def tearDownModule():
    shutil.rmtree(_DATA, ignore_errors=True)


class ZoneClassifyTest(unittest.TestCase):
    def test_工作区无标识可写(self):
        info = fszones.zone_info("/home/deck/DeckMeido/a.txt",
                                 ["/home/deck/DeckMeido"])
        self.assertEqual(info["zone"], fszones.ZONE_WORKSPACE)
        self.assertFalse(info["readonly"])
        self.assertEqual(fszones.write_block("/home/deck/DeckMeido/a.txt",
                                             ["/home/deck/DeckMeido"]), "")

    def test_默认工作区就是DeckMeido(self):
        info = fszones.zone_info("/home/deck/DeckMeido/notes/x.md")
        self.assertEqual(info["zone"], fszones.ZONE_WORKSPACE)
        self.assertEqual(info["label"], "工作区域")

    def test_用户空间也是工作区(self):
        roots = ["/home/deck/DeckMeido"]
        for p in ("/home/deck/MeidoSpace", "/home/deck/MeidoSpace/a/b.txt"):
            info = fszones.zone_info(p, roots)
            self.assertEqual(info["zone"], fszones.ZONE_WORKSPACE, p)
            self.assertFalse(info["readonly"], p)
            self.assertEqual(info["label"], "工作区域", p)
            self.assertEqual(fszones.write_block(p, roots), "", p)
        # 前缀相同但不是同一个目录：不能被顺手升级成工作区
        info = fszones.zone_info("/home/deck/MeidoSpaceX/a", roots)
        self.assertEqual(info["zone"], fszones.ZONE_CAUTION)
        # 整个家目录仍是谨慎区，不能因为工作区而放开
        info = fszones.zone_info("/home/deck", roots)
        self.assertEqual(info["zone"], fszones.ZONE_CAUTION)

    def test_家目录与SD卡是谨慎区可写(self):
        for p in ("/home/deck/Downloads/a.zip", "/home/deck",
                  "/run/media", "/run/media/foo", "/run/media/deck",
                  "/run/media/deck/USB/x"):
            info = fszones.zone_info(p, ["/home/deck/DeckMeido"])
            self.assertEqual(info["zone"], fszones.ZONE_CAUTION, p)
            self.assertFalse(info["readonly"], p)
            self.assertEqual(fszones.write_block(p, ["/home/deck/DeckMeido"]), "", p)

    def test_run_media软链接目录按目标判区(self):
        def fake_realpath(p):
            p = os.fspath(p)
            if p == "/run/media/SDCARD":
                # 走真实 realpath 归一，Windows 上才会带上和其它根一致的盘符
                return os.path.realpath("/run/media/deck/ABCD-1234")
            return os.path.realpath(p)

        with mock.patch.object(fszones, "_realpath", fake_realpath):
            info = fszones.zone_info("/run/media/SDCARD", ["/home/deck/DeckMeido"])
            self.assertEqual(info["zone"], fszones.ZONE_CAUTION)
            self.assertFalse(info["readonly"])

    def test_危险区只读(self):
        for p in ("/", "/etc/passwd", "/usr/bin", "/var/log", "/boot", "/opt/a",
                  "/srv", "/lib/modules", "/home/alice/doc",
                  "/run", "/run/user", "/run/user/1000"):
            info = fszones.zone_info(p, ["/home/deck/DeckMeido"])
            self.assertEqual(info["zone"], fszones.ZONE_DANGER, p)
            self.assertTrue(info["readonly"], p)
            self.assertEqual(fszones.write_block(p, ["/home/deck/DeckMeido"]),
                             fszones.REASON_READ_ONLY_ZONE, p)

    def test_proc_sys_dev禁止浏览(self):
        for p in ("/proc", "/proc/1/status", "/sys/class", "/dev/null"):
            info = fszones.zone_info(p, ["/home/deck/DeckMeido"])
            self.assertEqual(info["zone"], fszones.ZONE_FORBIDDEN, p)
            self.assertTrue(fszones.browse_denied(p, ["/home/deck/DeckMeido"]), p)
            self.assertEqual(fszones.write_block(p, ["/home/deck/DeckMeido"]),
                             fszones.REASON_FORBIDDEN_ZONE, p)

    def test_规范化重复斜杠点点尾斜杠(self):
        # 开发机是 Windows，canonical 会带盘符；和标准写法比结果一致即可
        self.assertEqual(fszones.canonical("/home//deck/./x/../y/"),
                         fszones.canonical("/home/deck/y"))
        info = fszones.zone_info("/home//deck/./a/../b/", ["/home/deck/DeckMeido"])
        self.assertEqual(info["zone"], fszones.ZONE_CAUTION)

    def test_点点点绕过到危险区(self):
        for p in ("/run/media/deck/../../etc", "/home/deck/DeckMeido/../../etc/passwd"):
            info = fszones.zone_info(p, ["/home/deck/DeckMeido"])
            self.assertEqual(info["zone"], fszones.ZONE_DANGER, p)
            self.assertTrue(info["readonly"], p)

    def test_大小写不误判(self):
        # Linux 区分大小写：/HOME/deck 不是 /home/deck
        if os.name != "nt":
            info = fszones.zone_info("/HOME/deck/a", ["/home/deck/DeckMeido"])
            self.assertEqual(info["zone"], fszones.ZONE_DANGER)

    def test_软链接按realpath判区(self):
        # 模拟 /home/deck/link -> /etc/passwd：只替换 realpath，不动真实文件系统
        def fake_realpath(p):
            p = os.fspath(p)
            if p == "/home/deck/link":
                return "/etc/passwd"
            return os.path.realpath(p)

        with mock.patch.object(fszones, "_realpath", fake_realpath):
            info = fszones.zone_info("/home/deck/link", ["/home/deck/DeckMeido"])
            self.assertEqual(info["zone"], fszones.ZONE_DANGER)
            self.assertTrue(info["readonly"])
            self.assertEqual(fszones.write_block("/home/deck/link",
                                                 ["/home/deck/DeckMeido"]),
                             fszones.REASON_READ_ONLY_ZONE)

    def test_只读挂载按只读(self):
        with mock.patch.object(fszones, "_linux_mount_readonly", lambda p: True):
            info = fszones.zone_info("/run/media/deck/CARD/x", ["/home/deck/DeckMeido"])
            self.assertEqual(info["zone"], fszones.ZONE_CAUTION)
            self.assertTrue(info["readonly"])
            self.assertEqual(fszones.write_block("/run/media/deck/CARD/x",
                                                 ["/home/deck/DeckMeido"]),
                             fszones.REASON_READ_ONLY_MOUNT)


class FsEndpointTest(unittest.TestCase):
    """直接调 async 端点，验证 403 与禁浏览的接线（不经 auth 依赖）。"""

    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="dm-ws-")
        self.danger = tempfile.mkdtemp(prefix="dm-danger-")
        self._patch = mock.patch.object(main, "_fs_workspace_roots",
                                        return_value=[self.ws])
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        shutil.rmtree(self.ws, ignore_errors=True)
        shutil.rmtree(self.danger, ignore_errors=True)

    @staticmethod
    def _body(resp):
        return json.loads(resp.body.decode("utf-8"))

    def test_危险区删除返回403且原因机器可读(self):
        resp = asyncio.run(main.api_fs(main._FsReq(op="delete", path="/etc/hosts")))
        self.assertEqual(resp.status_code, 403)
        body = self._body(resp)
        self.assertFalse(body["ok"])
        self.assertEqual(body["reason"], fszones.REASON_READ_ONLY_ZONE)

    def test_改名到危险区返回403(self):
        resp = asyncio.run(main.api_fs(main._FsReq(
            op="rename", path="/home/deck/a.txt", path2="/etc/a.txt")))
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self._body(resp)["reason"], fszones.REASON_READ_ONLY_ZONE)

    def test_危险目录上传返回403(self):
        from starlette.datastructures import UploadFile

        up = UploadFile(filename="x.bin", file=open(os.devnull, "rb"))
        try:
            resp = asyncio.run(main.api_upload_to(up, dir=self.danger))
        finally:
            up.file.close()
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self._body(resp)["reason"], fszones.REASON_READ_ONLY_ZONE)

    def test_禁止浏览路径返回403(self):
        resp = asyncio.run(main.api_browse(path="/proc"))
        self.assertEqual(resp.status_code, 403)
        body = self._body(resp)
        self.assertEqual(body["zone"], fszones.ZONE_FORBIDDEN)
        self.assertEqual(body["reason"], fszones.REASON_FORBIDDEN_ZONE)
        # 导航不能被禁止浏览判死：父级仍要回给前端，面包屑/上一级能退回
        target = Path("/proc").resolve()
        self.assertEqual(body["parent"], str(target.parent))

    def test_无权限目录仍能回上一级(self):
        # 开发机没有 /root 的权限语义，用真实存在的临时目录模拟 scandir 被拒
        target = Path(self.danger).resolve()
        with mock.patch.object(main.os, "scandir", side_effect=PermissionError):
            data = asyncio.run(main.api_browse(path=self.danger))
        self.assertEqual(data["path"], str(target))
        self.assertEqual(data["parent"], str(target.parent))
        self.assertIn("没有权限", data["error"])
        self.assertEqual(data["zone"], fszones.ZONE_DANGER)
        self.assertTrue(data["readonly"])

    def test_根目录没有上一级(self):
        target = Path("/").resolve()
        with mock.patch.object(main.os, "scandir", side_effect=PermissionError):
            data = asyncio.run(main.api_browse(path="/"))
        self.assertEqual(data["path"], str(target))
        self.assertIsNone(data["parent"])

    def test_工作区浏览带zone字段(self):
        (Path(self.ws) / "a.txt").write_text("x", encoding="utf-8")
        data = asyncio.run(main.api_browse(path=self.ws))
        self.assertEqual(data["zone"], fszones.ZONE_WORKSPACE)
        self.assertFalse(data["readonly"])
        self.assertIn("a.txt", [e["name"] for e in data["entries"]])


if __name__ == "__main__":
    unittest.main()
