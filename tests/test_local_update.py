"""本地离线更新包：扫描、校验、版本门禁、原子替换与回滚。

全部在临时目录里跑，不碰真实安装目录、不需要网络。
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import local_update, updater  # noqa: E402


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_tarball(ver: str, files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    root = f"deckmeido-{ver}"
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for rel, text in files.items():
            data = text.encode("utf-8")
            ti = tarfile.TarInfo(f"{root}/{rel}")
            ti.size = len(data)
            ti.mode = 0o644
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def make_package(folder: Path, ver: str, payload_files: dict[str, str], *,
                 sidecar: bool = True, extra_members: dict[str, bytes] | None = None,
                 tamper_sha: bool = False, manifest_override: dict | None = None) -> Path:
    tarball = make_tarball(ver, payload_files)
    members = {
        f"files/deckmeido-{ver}.tar.gz": tarball,
        "files/install.sh": b"#!/usr/bin/env bash\necho hi\n",
        "files/latest.json": json.dumps({"tag": ver}).encode(),
    }
    if extra_members:
        members.update(extra_members)
    entries = [{"path": p, "size": len(d), "sha256": _sha(d)}
               for p, d in members.items()]
    manifest = {
        "format": "deckmeido-update",
        "version": ver,
        "platform": "linux-x86_64",
        "payload": {"file": f"files/deckmeido-{ver}.tar.gz",
                    "sha256": _sha(tarball)},
        "files": entries,
    }
    if manifest_override:
        manifest.update(manifest_override)
    sums = "".join(f"{e['sha256']}  {e['path']}\n" for e in entries)
    if tamper_sha:
        sums = sums.replace(entries[0]["sha256"], "0" * 64, 1)
    name = f"deckmeido-update-{ver}.zip"
    path = folder / name
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        z.writestr("SHA256SUMS", sums)
        for p, d in members.items():
            z.writestr(p, d)
    if sidecar:
        (folder / (name + ".sha256")).write_text(
            f"{_sha(path.read_bytes())}  {name}\n", encoding="utf-8")
    return path


class ScanTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def tearDown(self):
        self._td.cleanup()

    def _patch(self, current="0.7.0"):
        return (
            mock.patch.object(updater, "update_cfg",
                              lambda: {"local_enable": True,
                                       "local_dir": str(self.workspace)}),
            mock.patch.object(updater, "current_version", lambda: current),
        )

    def test_目录不存在时安静返回空(self):
        with mock.patch.object(updater, "update_cfg",
                               lambda: {"local_enable": True,
                                        "local_dir": str(self.root / "nope")}):
            st = local_update.status()
        self.assertFalse(st["available"])
        self.assertEqual(st["packages"], [])
        self.assertEqual(st["error"], "")

    def test_关掉后不扫描(self):
        make_package(self.workspace, "0.7.1", {"VERSION": "0.7.1\n"})
        with mock.patch.object(updater, "update_cfg",
                               lambda: {"local_enable": False,
                                        "local_dir": str(self.workspace)}):
            st = local_update.status()
        self.assertFalse(st["enabled"])
        self.assertFalse(st["available"])

    def test_识别比当前新的合法包(self):
        make_package(self.workspace, "0.7.1", {"VERSION": "0.7.1\n"})
        p1, p2 = self._patch("0.7.0")
        with p1, p2:
            st = local_update.status()
        self.assertTrue(st["available"])
        self.assertEqual(st["candidate"]["version"], "0.7.1")
        self.assertGreater(st["candidate"]["size"], 0)

    def test_同版本或更旧默认不显示为可更新(self):
        make_package(self.workspace, "0.7.0", {"VERSION": "0.7.0\n"})
        make_package(self.workspace, "0.6.9", {"VERSION": "0.6.9\n"})
        p1, p2 = self._patch("0.7.0")
        with p1, p2:
            st = local_update.status()
        self.assertFalse(st["available"])
        self.assertEqual(len(st["packages"]), 2)

    def test_缺包级校验文件直接拒绝(self):
        p = make_package(self.workspace, "0.7.1", {"VERSION": "0.7.1\n"},
                         sidecar=False)
        info = local_update.inspect(p)
        self.assertFalse(info["ok"])
        self.assertIn("缺少包级校验", info["error"])

    def test_包级哈希不匹配(self):
        p = make_package(self.workspace, "0.7.1", {"VERSION": "0.7.1\n"})
        (self.workspace / (p.name + ".sha256")).write_text("0" * 64 + "  " + p.name)
        info = local_update.inspect(p)
        self.assertFalse(info["ok"])
        self.assertIn("不匹配", info["error"])

    def test_清单外文件被拒绝(self):
        p = make_package(self.workspace, "0.7.1", {"VERSION": "0.7.1\n"})
        # 塞一个不在 manifest 清单里的成员；包级哈希要跟着重算，否则先被哈希拦下
        with zipfile.ZipFile(p, "a") as z:
            z.writestr("files/evil2.sh", b"x")
        (self.workspace / (p.name + ".sha256")).write_text(
            f"{_sha(p.read_bytes())}  {p.name}\n", encoding="utf-8")
        info = local_update.inspect(p)
        self.assertFalse(info["ok"])
        self.assertIn("清单外文件", info["error"])

    def test_路径穿越被拒绝(self):
        p = make_package(self.workspace, "0.7.1", {"VERSION": "0.7.1\n"},
                         extra_members={"../evil.sh": b"x"})
        info = local_update.inspect(p)
        self.assertFalse(info["ok"])
        self.assertIn("路径穿越", info["error"])

    def test_sha256sums_与清单不一致被拒绝(self):
        p = make_package(self.workspace, "0.7.1", {"VERSION": "0.7.1\n"},
                         tamper_sha=True)
        info = local_update.inspect(p)
        self.assertFalse(info["ok"])
        self.assertIn("SHA256SUMS", info["error"])

    def test_manifest_版本与文件名不一致被拒绝(self):
        p = make_package(self.workspace, "0.7.1", {"VERSION": "0.7.1\n"},
                         manifest_override={"version": "0.7.2"})
        info = local_update.inspect(p)
        self.assertFalse(info["ok"])
        self.assertIn("版本", info["error"])


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.install = self.root / "install"
        (self.install / "server" / "sessions").mkdir(parents=True)
        (self.install / "server" / "static" / "shots").mkdir(parents=True)
        (self.install / "plugin").mkdir()
        (self.install / "VERSION").write_text("0.7.0\n", encoding="utf-8")
        (self.install / "server" / "main.py").write_text("OLD", encoding="utf-8")
        (self.install / "server" / "config.toml").write_text("token = 'keep'\n",
                                                             encoding="utf-8")
        (self.install / "server" / "sessions" / "a.json").write_text("{}", encoding="utf-8")
        (self.install / "plugin" / "package.json").write_text('{"version":"0.7.0"}',
                                                             encoding="utf-8")
        self.patches = [
            mock.patch.object(updater, "INSTALL_DIR", self.install),
            mock.patch.object(updater, "update_cfg",
                              lambda: {"local_enable": True,
                                       "local_dir": str(self.workspace)}),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self._td.cleanup()

    def _apply(self, ver="0.7.1", **kw):
        p = make_package(self.workspace, ver, {
            "VERSION": ver + "\n",
            "server/main.py": "NEW",
            "plugin/package.json": json.dumps({"version": ver}),
        }, **kw)
        return local_update.apply_archive(p, restart=False)

    def test_应用保留用户数据并替换代码(self):
        with mock.patch.object(updater, "current_version", lambda: "0.7.0"):
            res = self._apply()
        self.assertEqual(res["version"], "0.7.1")
        self.assertEqual(res["mode"], "upgrade")
        self.assertEqual((self.install / "VERSION").read_text().strip(), "0.7.1")
        self.assertEqual((self.install / "server" / "main.py").read_text(), "NEW")
        self.assertIn("keep", (self.install / "server" / "config.toml").read_text())
        self.assertTrue((self.install / "server" / "sessions" / "a.json").exists())
        self.assertIn("0.7.1", (self.install / "plugin" / "package.json").read_text())
        self.assertFalse(list(self.install.glob(".update-old-*")))
        self.assertFalse(list(self.install.glob(".update-stage-*")))

    def test_同版本允许修复(self):
        with mock.patch.object(updater, "current_version", lambda: "0.7.1"):
            res = self._apply("0.7.1")
        self.assertEqual(res["mode"], "repair")

    def test_降级默认拒绝(self):
        with mock.patch.object(updater, "current_version", lambda: "0.7.2"):
            with self.assertRaises(RuntimeError) as ctx:
                self._apply("0.7.1")
        self.assertIn("拒绝降级", str(ctx.exception))
        self.assertEqual((self.install / "server" / "main.py").read_text(), "OLD")

    def test_显式确认后允许降级(self):
        with mock.patch.object(updater, "current_version", lambda: "0.7.2"):
            p = make_package(self.workspace, "0.7.1", {
                "VERSION": "0.7.1\n", "server/main.py": "NEW",
            })
            res = local_update.apply_archive(p, allow_downgrade=True, restart=False)
        self.assertEqual(res["mode"], "upgrade")
        self.assertEqual((self.install / "server" / "main.py").read_text(), "NEW")

    def test_替换中途失败会回滚(self):
        p = make_package(self.workspace, "0.7.1", {
            "VERSION": "0.7.1\n", "server/main.py": "NEW",
        })
        src = self.root / "extract"
        with zipfile.ZipFile(p) as z:
            payload = z.read("files/deckmeido-0.7.1.tar.gz")
        src.mkdir()
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tf:
            tf.extractall(src, filter="data")
        root = next(src.iterdir())

        real_replace = updater.os.replace
        calls = {"n": 0}

        def flaky(a, b):
            calls["n"] += 1
            # 第 3 次发生在第二项上：此时第一项已经完整换好，正好验证逆序回滚
            if calls["n"] == 3:
                raise OSError("模拟写入失败")
            return real_replace(a, b)

        with mock.patch.object(updater.os, "replace", flaky):
            with self.assertRaises(OSError):
                updater._install_tree(root, self.install / "update.log")
        self.assertEqual((self.install / "server" / "main.py").read_text(), "OLD")
        self.assertEqual((self.install / "VERSION").read_text().strip(), "0.7.0")
        self.assertFalse(list(self.install.glob(".update-old-*")))
        self.assertFalse(list(self.install.glob(".update-stage-*")))


class UploadTest(unittest.TestCase):
    def test_缺校验值的上传直接拒绝(self):
        with self.assertRaises(RuntimeError):
            local_update.save_upload(b"abc", "")

    def test_哈希不匹配的上传直接拒绝(self):
        with self.assertRaises(RuntimeError):
            local_update.save_upload(b"abc", "0" * 64)


if __name__ == "__main__":
    unittest.main()
