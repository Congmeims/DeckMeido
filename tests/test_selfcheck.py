"""桌面控件「可补装组件」：状态判定、单任务互斥、进度阶段推进、失败透传。

全部离线：不装包、不下载、不碰真实模型，只验证注册表的判定逻辑与安装器骨架。

    python -m unittest tests.test_selfcheck -v
"""
from __future__ import annotations

import io
import sys
import tempfile
import threading
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# server.depcheck -> server.tools 会导入 ptyprocess；本机 Windows 没装，
# 这里只是为了能导入模块，本文件的用例不会走到真正开终端的分支
if "ptyprocess" not in sys.modules:
    try:
        import ptyprocess  # noqa: F401
    except Exception:
        sys.modules["ptyprocess"] = types.ModuleType("ptyprocess")

from server import selfcheck as sc  # noqa: E402


class _FakeEngine:
    def __init__(self, runtime=True, model=True, dl=None):
        self.runtime = runtime
        self.model = model
        self.dl = dl or {}

    def status(self):
        return {"runtime": self.runtime, "model": self.model}

    def start_download(self):
        self.dl["started"] = True

    def download_status(self):
        return dict(self.dl)

    def model_ready(self):
        return self.model


class SelfcheckBase(unittest.TestCase):
    """每个用例都从「没有任务、没有 hook」的干净状态开始"""

    def setUp(self):
        self._old_tts = sc._tts
        sc._job = None
        sc._job_thread = None
        sc._hook = None
        sc._sudo_cache = None

    def tearDown(self):
        sc._tts = self._old_tts
        t = sc._job_thread
        if t and t.is_alive():
            t.join(2.0)
        sc._job = None
        sc._job_thread = None
        sc._hook = None
        sc._sudo_cache = None


class StatusTest(SelfcheckBase):
    def test_tts_三态(self):
        sc.set_tts(_FakeEngine(runtime=False, model=False))
        self.assertEqual(sc._tts_status()[0], "mismatch")
        sc.set_tts(_FakeEngine(runtime=True, model=False))
        self.assertEqual(sc._tts_status()[0], "missing")
        sc.set_tts(_FakeEngine(runtime=True, model=True))
        self.assertEqual(sc._tts_status(), ("ready", ""))

    def test_tts_引擎未注入(self):
        sc.set_tts(None)
        sc._tts = None
        status, detail = sc._tts_status()
        self.assertEqual(status, "mismatch")
        self.assertIn("未初始化", detail)

    def test_which_按命令实测(self):
        with mock.patch.object(sc.shutil, "which", return_value="/usr/bin/foo"):
            self.assertEqual(sc._which_status("foo", "foo"), ("ready", ""))
        with mock.patch.object(sc.shutil, "which", return_value=None):
            status, detail = sc._which_status("foo", "foo")
            self.assertEqual(status, "missing")
            self.assertIn("foo", detail)

    def test_nltk_按英文数据实测(self):
        with mock.patch.object(sc.tts_mod, "english_data_ready", return_value=True):
            self.assertEqual(sc._nltk_status(), ("ready", ""))
        with mock.patch.object(sc.tts_mod, "english_data_ready", return_value=False):
            self.assertEqual(sc._nltk_status()[0], "missing")

    def test_ydotoold_三种情况(self):
        # ydotool 都没装
        with mock.patch.object(sc.shutil, "which", return_value=None):
            self.assertEqual(sc._ydotoold_status()[0], "missing")
        # systemd 托管 = 就绪
        with mock.patch.object(sc.shutil, "which", return_value="/usr/bin/x"), \
                mock.patch.object(sc, "_run", return_value=(0, "")):
            self.assertEqual(sc._ydotoold_status()[0], "ready")
        # 只有裸进程、没交给 systemd = 重启后会失效
        def fake_run(args, timeout=4.0):
            if args[:2] == ["systemctl", "--user"]:
                return (3, "")
            return (0, "")
        with mock.patch.object(sc.shutil, "which", return_value="/usr/bin/x"), \
                mock.patch.object(sc, "_run", side_effect=fake_run):
            status, detail = sc._ydotoold_status()
            self.assertEqual(status, "mismatch")
            self.assertIn("systemd", detail)

    def test_快照里系统包是否可装跟随受限sudo探测(self):
        fake = _FakeEngine(runtime=True, model=True)
        sc.set_tts(fake)
        with mock.patch.object(sc, "sudo_silent", return_value=False):
            items = {i["key"]: i for i in sc.snapshot()["components"]}
        self.assertTrue(items["tts_model"]["can_install"])
        self.assertTrue(items["ydotoold"]["can_install"])
        self.assertFalse(items["ydotool"]["can_install"])
        self.assertTrue(items["ydotool"]["command"])
        with mock.patch.object(sc, "sudo_silent", return_value=True):
            items = {i["key"]: i for i in sc.snapshot()["components"]}
        self.assertTrue(items["ydotool"]["can_install"])

    def test_快照计数与状态标签(self):
        sc.set_tts(_FakeEngine(runtime=False, model=False))
        snap = sc.snapshot()
        self.assertEqual(snap["total"], len(sc.COMPONENTS))
        self.assertEqual(snap["ready"], sum(1 for i in snap["components"]
                                            if i["status"] == "ready"))
        for i in snap["components"]:
            self.assertIn(i["status"], sc.STATUS_LABELS)
            self.assertEqual(i["status_label"], sc.STATUS_LABELS[i["status"]])


class JobTest(SelfcheckBase):
    def test_未知组件被拒绝(self):
        res = sc.start("no-such-component")
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "unknown_component")

    def test_同一时间只允许一个任务(self):
        entered = threading.Event()
        release = threading.Event()

        def fake_run(item):
            entered.set()
            release.wait(2.0)

        with mock.patch.object(sc, "_run_job", side_effect=fake_run):
            first = sc.start("tts_model")
            self.assertTrue(first["ok"])
            self.assertTrue(entered.wait(1.0))
            second = sc.start("nltk_data")
            release.set()
            sc._job_thread.join(2.0)
        self.assertFalse(second["ok"])
        self.assertEqual(second["reason"], "busy")
        self.assertIn("进行", second["error"])

    def test_进度百分比被夹在0到100并且按序推进(self):
        seen = []
        sc.set_progress_hook(seen.append)
        sc._set("nltk_data", "download", -5, "负数")
        sc._set("nltk_data", "unpack", 432.0, "超界")
        self.assertEqual(seen[0]["percent"], 0.0)
        self.assertEqual(seen[1]["percent"], 100.0)
        self.assertEqual([s["phase"] for s in seen], ["download", "unpack"])

    def test_失败的错误原样透传(self):
        item = {"key": "tts_model", "name": "x", "size": "", "action": "python",
                "command": "", "privileged": False}
        with mock.patch.object(sc, "_install_tts", side_effect=RuntimeError("磁盘满了")):
            sc._run_job(item)
        job = sc.job_snapshot()
        self.assertEqual(job["state"], "error")
        self.assertEqual(job["error"], "磁盘满了")

    def test_需要提权时给终端命令而不是假装成功(self):
        item = {"key": "ydotool", "name": "x", "size": "", "action": "package",
                "command": "bash install.sh check --fix", "privileged": True}
        with mock.patch.object(sc, "sudo_silent", return_value=False):
            sc._run_job(item)
        job = sc.job_snapshot()
        self.assertEqual(job["state"], "need_terminal")
        self.assertEqual(job["error"], "bash install.sh check --fix")

    def test_不支持自动安装的分支落到错误态(self):
        item = {"key": "mystery", "name": "x", "size": "", "action": "python",
                "command": "", "privileged": False}
        sc._run_job(item)
        job = sc.job_snapshot()
        self.assertEqual(job["state"], "error")
        self.assertIn("不支持", job["error"])

    def test_sudo探测失败时不声称可静默安装(self):
        with mock.patch.object(sc, "_run", return_value=(1, "a password is required")):
            sc._sudo_cache = None
            self.assertFalse(sc.sudo_silent())
        # 探测结果有缓存，短时间内不会反复弹进程
        with mock.patch.object(sc, "_run", return_value=(0, "")) as run:
            self.assertFalse(sc.sudo_silent())
            self.assertEqual(run.call_count, 0)


class NltkInstallTest(SelfcheckBase):
    def _zip(self) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("readme.txt", "ok")
        return buf.getvalue()

    def test_进度单调不倒退且首源失败会重试一次(self):
        seen = []
        sc.set_progress_hook(seen.append)
        calls = {"n": 0}
        blob = self._zip()

        def fake_get(url, timeout=30.0):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("连接被重置")
            return blob

        tmp = tempfile.mkdtemp(prefix="dm-nltk-")
        with mock.patch.object(sc.tts_mod, "nltk_data_dir", return_value=Path(tmp)), \
                mock.patch.object(sc.tts_mod, "english_data_ready", return_value=True), \
                mock.patch.object(sc, "_poll_sleep"), \
                mock.patch.object(sc, "_http_get", side_effect=fake_get):
            sc._install_nltk()

        # 第一次失败后立刻重试同一个源，所以首个包的下载一共 2 次请求
        self.assertEqual(calls["n"], 1 + len(sc.NLTK_PACKAGES))
        percents = [s["percent"] for s in seen]
        self.assertEqual(percents, sorted(percents), percents)
        self.assertEqual(seen[-1]["phase"], "verify")
        self.assertEqual(seen[-1]["percent"], 100.0)

    def test_解包后校验失败要报错(self):
        tmp = tempfile.mkdtemp(prefix="dm-nltk-")
        with mock.patch.object(sc.tts_mod, "nltk_data_dir", return_value=Path(tmp)), \
                mock.patch.object(sc.tts_mod, "english_data_ready", return_value=False), \
                mock.patch.object(sc, "_poll_sleep"), \
                mock.patch.object(sc, "_http_get", return_value=self._zip()):
            with self.assertRaises(RuntimeError) as ctx:
                sc._install_nltk()
        self.assertIn("校验失败", str(ctx.exception))

    def test_全部源都失败时报错并带原因(self):
        tmp = tempfile.mkdtemp(prefix="dm-nltk-")
        with mock.patch.object(sc.tts_mod, "nltk_data_dir", return_value=Path(tmp)), \
                mock.patch.object(sc, "_poll_sleep"), \
                mock.patch.object(sc, "_http_get", side_effect=OSError("超时")):
            with self.assertRaises(RuntimeError) as ctx:
                sc._install_nltk()
        self.assertIn("下载失败", str(ctx.exception))
        self.assertIn("超时", str(ctx.exception))


class _FakeProc:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


class _FakeResp:
    """最小可用的 urlopen 响应：按块读、带 Content-Length"""

    def __init__(self, data, length=None):
        self._data = data
        self._pos = 0
        self.headers = {"Content-Length": str(len(data) if length is None else length)}

    def read(self, n=-1):
        if self._pos >= len(self._data):
            return b""
        end = len(self._data) if n is None or n < 0 else min(len(self._data), self._pos + n)
        chunk = self._data[self._pos:end]
        self._pos = end
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class DownloadTest(SelfcheckBase):
    URL = "https://mirror.example/extra/os/x86_64/ydotool-0.2.1-1-x86_64.pkg.tar.zst"

    def _proc(self, name="ydotool", version="0.2.1", url=None):
        return _FakeProc(stdout=f"{name}|{version}|{url or self.URL}\n")

    def test_只接受系统包组件(self):
        with self.assertRaises(sc.PackageDownloadError) as ctx:
            sc.package_spec("no-such-component")
        self.assertEqual(ctx.exception.reason, "unknown_component")
        with self.assertRaises(sc.PackageDownloadError) as ctx:
            sc.package_spec("tts_model")
        self.assertEqual(ctx.exception.reason, "not_package")

    def test_已就绪的包不再下载(self):
        with mock.patch.object(sc.shutil, "which", return_value="/usr/bin/ydotool"), \
                mock.patch.object(sc.subprocess, "run") as run:
            with self.assertRaises(sc.PackageDownloadError) as ctx:
                sc.package_spec("ydotool")
        self.assertEqual(ctx.exception.reason, "already_ready")
        run.assert_not_called()

    def test_文件名含包名版本且地址只来自pacman(self):
        with mock.patch.object(sc.shutil, "which", return_value=None), \
                mock.patch.object(sc.subprocess, "run", return_value=self._proc()) as run:
            spec = sc.package_spec("ydotool")
        self.assertEqual(spec["package"], "ydotool")
        self.assertEqual(spec["version"], "0.2.1")
        self.assertEqual(spec["url"], self.URL)
        self.assertEqual(spec["filename"], "ydotool-0.2.1-1-x86_64.pkg.tar.zst")
        argv = run.call_args[0][0]
        self.assertEqual(argv[:4], ["pacman", "-Sp", "--print-format", "%n|%v|%l"])
        # 请求参数只应带包名，不能带任何 URL
        self.assertEqual(argv[4:], ["ydotool"])

    def test_espeak使用真实包名(self):
        url = "https://mirror.example/extra/espeak-ng-1.51.1-6-x86_64.pkg.tar.zst"
        with mock.patch.object(sc.shutil, "which", return_value=None), \
                mock.patch.object(sc.subprocess, "run",
                                  return_value=self._proc("espeak-ng", "1.51.1", url)) as run:
            spec = sc.package_spec("espeak")
        self.assertEqual(spec["package"], "espeak-ng")
        self.assertEqual(run.call_args[0][0][4], "espeak-ng")

    def test_拒绝非http地址(self):
        with mock.patch.object(sc.shutil, "which", return_value=None), \
                mock.patch.object(sc.subprocess, "run",
                                  return_value=self._proc(url="file:///etc/passwd")):
            with self.assertRaises(sc.PackageDownloadError) as ctx:
                sc.package_spec("ydotool")
        self.assertEqual(ctx.exception.reason, "unsafe_url")

    def test_下载成功_写入临时文件并可清理(self):
        body = b"PKG-DATA"
        with mock.patch.object(sc.shutil, "which", return_value=None), \
                mock.patch.object(sc.subprocess, "run", return_value=self._proc()), \
                mock.patch.object(sc.urllib.request, "urlopen", return_value=_FakeResp(body)):
            res = sc.download_package("ydotool")
        try:
            self.assertTrue(res["ok"])
            self.assertEqual(res["size"], len(body))
            self.assertIn("ydotool", res["filename"])
            self.assertIn("0.2.1", res["filename"])
            self.assertEqual(Path(res["path"]).read_bytes(), body)
            self.assertEqual(sc.job_snapshot()["state"], "done")
        finally:
            sc.cleanup_package(res["path"])
        self.assertFalse(Path(res["path"]).exists())

    def test_下载失败透传原因(self):
        with mock.patch.object(sc.shutil, "which", return_value=None), \
                mock.patch.object(sc.subprocess, "run", return_value=self._proc()), \
                mock.patch.object(sc.urllib.request, "urlopen",
                                  side_effect=OSError("镜像连不上")):
            with self.assertRaises(sc.PackageDownloadError) as ctx:
                sc.download_package("ydotool")
        self.assertEqual(ctx.exception.reason, "download_failed")
        self.assertIn("镜像连不上", str(ctx.exception))
        job = sc.job_snapshot()
        self.assertEqual(job["state"], "error")
        self.assertIn("镜像连不上", job["error"])

    def test_下载不完整要报错(self):
        body = b"x" * 10
        with mock.patch.object(sc.shutil, "which", return_value=None), \
                mock.patch.object(sc.subprocess, "run", return_value=self._proc()), \
                mock.patch.object(sc.urllib.request, "urlopen",
                                  return_value=_FakeResp(body, length=20)):
            with self.assertRaises(sc.PackageDownloadError) as ctx:
                sc.download_package("ydotool")
        self.assertEqual(ctx.exception.reason, "download_failed")
        self.assertIn("不完整", str(ctx.exception))


class UninstallPlanTest(unittest.TestCase):
    """卸载只覆盖我们自己管理的东西，系统包一律拒绝"""

    def test_系统包不允许卸载(self):
        plan = sc.uninstall_plan("grim")
        self.assertFalse(plan["allowed"])
        self.assertIn("不支持", plan["reason"])

    def test_守护进程只撤销自启不删包(self):
        plan = sc.uninstall_plan("ydotoold")
        self.assertTrue(plan["allowed"])
        self.assertEqual(plan["paths"], [])
        self.assertIn("ydotool.service", plan["target"])

    def test_未知组件不允许卸载(self):
        self.assertFalse(sc.uninstall_plan("nope")["allowed"])


if __name__ == "__main__":
    unittest.main()
