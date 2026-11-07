"""桌面控件「可补装组件」：状态判定、单任务互斥、进度阶段推进、失败透传。

全部离线：不装包、不下载、不碰真实模型，只验证注册表的判定逻辑与安装器骨架。

    python -m unittest tests.test_selfcheck -v
"""
from __future__ import annotations

import io
import ssl
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
        sc._clear_password()

    def tearDown(self):
        sc._tts = self._old_tts
        t = sc._job_thread
        if t and t.is_alive():
            t.join(2.0)
        sc._job = None
        sc._job_thread = None
        sc._hook = None
        sc._sudo_cache = None
        sc._clear_password()


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

    def test_快照里系统包始终可在软件内安装_是否需要授权跟随sudo探测(self):
        fake = _FakeEngine(runtime=True, model=True)
        sc.set_tts(fake)
        with mock.patch.object(sc, "sudo_silent", return_value=False):
            items = {i["key"]: i for i in sc.snapshot()["components"]}
        self.assertTrue(items["tts_model"]["can_install"])
        self.assertTrue(items["ydotoold"]["can_install"])
        # 没有免密 sudo 也能在软件内装，只是界面会要求输入管理员密码
        self.assertTrue(items["ydotool"]["can_install"])
        self.assertTrue(items["ydotool"]["needs_auth"])
        with mock.patch.object(sc, "sudo_silent", return_value=True):
            items = {i["key"]: i for i in sc.snapshot()["components"]}
        self.assertFalse(items["ydotool"]["needs_auth"])

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

    def test_系统包没有免密sudo时走界面密码流程(self):
        item = {"key": "ydotool", "name": "x", "size": "", "action": "package",
                "command": "bash install.sh check --fix", "privileged": True,
                "status_fn": lambda: ("ready", "")}
        tmp = tempfile.mkdtemp(prefix="dm-pkg-")
        spec = {"package": "ydotool", "version": "0.2.1",
                "url": "https://m/x.pkg.tar.zst", "filename": "x.pkg.tar.zst"}
        calls = []

        def fake_exec(mode, password, path, name, timeout=630.0):
            calls.append((mode, password))
            return 0, ""

        with mock.patch.object(sc, "package_spec", return_value=spec), \
                mock.patch.object(sc, "_workspace_tmp", return_value=Path(tmp)), \
                mock.patch.object(sc, "_download_to"), \
                mock.patch.object(sc, "_verify_package"), \
                mock.patch.object(sc, "sudo_silent", return_value=False), \
                mock.patch.object(sc, "_pkexec_ready", return_value=False), \
                mock.patch.object(sc, "_request_password", return_value="pw"), \
                mock.patch.object(sc, "_exec_install", side_effect=fake_exec), \
                mock.patch.object(sc.shutil, "which", return_value="/usr/bin/ydotool"):
            sc._run_job(item)
        job = sc.job_snapshot()
        self.assertEqual(job["state"], "done")
        self.assertEqual(calls, [("password", "pw")])
        self.assertFalse(Path(tmp).exists())

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

    def test_未预期异常不把Python原文抛给界面(self):
        """真机曾把 ValueError 原文抛到卡片；现在只给类型 + 日志指引"""
        item = {"key": "grim", "name": "grim（截图备选）", "size": "",
                "action": "package", "command": "", "privileged": True}
        boom = ValueError(
            "stdout and stderr arguments may not be used with capture_output.")
        with mock.patch.object(sc, "_install_package", side_effect=boom), \
                mock.patch.object(sc.logger, "exception") as log:
            sc._run_job(item)
        job = sc.job_snapshot()
        self.assertEqual(job["state"], "error")
        self.assertNotIn("capture_output", job["error"])
        self.assertIn("内部错误", job["error"])
        self.assertIn("ValueError", job["error"])
        log.assert_called_once()

    def test_已知业务失败原因仍然原样展示(self):
        item = {"key": "tts_model", "name": "x", "size": "", "action": "python",
                "command": "", "privileged": False}
        with mock.patch.object(sc, "_install_tts", side_effect=RuntimeError("磁盘满了")):
            sc._run_job(item)
        self.assertEqual(sc.job_snapshot()["error"], "磁盘满了")


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


class PackageSpecTest(SelfcheckBase):
    URL = "https://mirror.example/extra/os/x86_64/ydotool-0.2.1-1-x86_64.pkg.tar.zst"

    def _proc(self, name="ydotool", version="0.2.1", url=None):
        return _FakeProc(stdout=f"{name}|{version}|{url or self.URL}\n")

    def test_只接受系统包组件(self):
        with self.assertRaises(sc.PackageError) as ctx:
            sc.package_spec("no-such-component")
        self.assertEqual(ctx.exception.reason, "unknown_component")
        with self.assertRaises(sc.PackageError) as ctx:
            sc.package_spec("tts_model")
        self.assertEqual(ctx.exception.reason, "not_package")

    def test_已就绪的包不再安装(self):
        with mock.patch.object(sc.shutil, "which", return_value="/usr/bin/ydotool"), \
                mock.patch.object(sc.subprocess, "run") as run:
            with self.assertRaises(sc.PackageError) as ctx:
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
            with self.assertRaises(sc.PackageError) as ctx:
                sc.package_spec("ydotool")
        self.assertEqual(ctx.exception.reason, "unsafe_url")


class PackageInstallTest(SelfcheckBase):
    """软件内安装：下载 → 校验 → 提权 → 安装 → 重新探测，全程清理临时文件"""

    SPEC = {"package": "ydotool", "version": "0.2.1",
            "url": "https://mirror.example/ydotool-0.2.1-1-x86_64.pkg.tar.zst",
            "filename": "ydotool-0.2.1-1-x86_64.pkg.tar.zst"}

    def _item(self, status=("ready", "")):
        return {"key": "ydotool", "name": "ydotool（点击 / 输入）", "size": "",
                "action": "package", "command": "", "privileged": True,
                "status_fn": lambda: status}

    def _fake_download(self, spec, path, component):
        path.write_bytes(b"PKG")

    def _run(self, sudo_silent=False, pkexec=False, exec_side_effect=None,
             password="pw", status=("ready", "")):
        tmp = tempfile.mkdtemp(prefix="dm-pkg-")
        calls = []

        def fake_exec(mode, pw, path, name, timeout=630.0):
            calls.append((mode, pw))
            if exec_side_effect:
                return exec_side_effect.pop(0)
            return 0, ""

        patches = [
            mock.patch.object(sc, "package_spec", return_value=dict(self.SPEC)),
            mock.patch.object(sc, "_workspace_tmp", return_value=Path(tmp)),
            mock.patch.object(sc, "_download_to", side_effect=self._fake_download),
            mock.patch.object(sc, "_verify_package"),
            mock.patch.object(sc, "sudo_silent", return_value=sudo_silent),
            mock.patch.object(sc, "_pkexec_ready", return_value=pkexec),
            mock.patch.object(sc, "_request_password", return_value=password),
            mock.patch.object(sc, "_exec_install", side_effect=fake_exec),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        sc._run_job(self._item(status))
        return tmp, calls

    def test_免密sudo一次完成并清理临时目录(self):
        tmp, calls = self._run(sudo_silent=True)
        job = sc.job_snapshot()
        self.assertEqual(job["state"], "done")
        self.assertEqual(calls, [("sudo", None)])
        self.assertFalse(Path(tmp).exists())

    def test_polkit被拒后回退到界面密码(self):
        tmp, calls = self._run(
            pkexec=True,
            exec_side_effect=[(126, "Error executing command as another user: Not authorized"),
                              (0, "")])
        self.assertEqual(sc.job_snapshot()["state"], "done")
        self.assertEqual(calls, [("pkexec", None), ("password", "pw")])
        self.assertFalse(Path(tmp).exists())

    def test_免密sudo失效时回退界面密码(self):
        tmp, calls = self._run(
            sudo_silent=True,
            exec_side_effect=[(1, "sudo: a password is required"), (0, "")])
        self.assertEqual(sc.job_snapshot()["state"], "done")
        self.assertEqual(calls, [("sudo", None), ("password", "pw")])
        self.assertFalse(Path(tmp).exists())

    def test_pacman失败透传真实原因(self):
        detail = "error: failed to commit transaction (conflicting files)\ngrim: exists in filesystem"
        tmp, _calls = self._run(sudo_silent=True, exec_side_effect=[(1, detail)])
        job = sc.job_snapshot()
        self.assertEqual(job["state"], "error")
        self.assertIn("failed to commit", job["error"])
        self.assertIn("exists in filesystem", job["error"])
        self.assertFalse(Path(tmp).exists())

    def test_只读保护关闭失败给明确提示(self):
        self._run(sudo_silent=True,
                  exec_side_effect=[(11, "DECKMEIDO_ERR readonly_disable")])
        job = sc.job_snapshot()
        self.assertEqual(job["state"], "error")
        self.assertIn("只读", job["error"])
        self.assertIn("steamos-readonly disable", job["error"])

    def test_只读保护恢复失败仍算完成但给出提示(self):
        self._run(sudo_silent=True,
                  exec_side_effect=[(0, "DECKMEIDO_WARN restore_failed")])
        job = sc.job_snapshot()
        self.assertEqual(job["state"], "done")
        self.assertIn("只读保护", job["message"])

    def test_装完仍找不到命令要报错(self):
        self._run(sudo_silent=True, status=("missing", "找不到 ydotool"))
        job = sc.job_snapshot()
        self.assertEqual(job["state"], "error")
        self.assertIn("找不到", job["error"])

    def test_下载成功写文件并推进进度(self):
        body = b"PKG-DATA"
        tmp = tempfile.mkdtemp(prefix="dm-dl-")
        path = Path(tmp) / "a.pkg.tar.zst"
        with mock.patch.object(sc.urllib.request, "urlopen",
                               return_value=_FakeResp(body)):
            sc._download_to(self.SPEC, path, "ydotool")
        self.assertEqual(path.read_bytes(), body)
        self.assertEqual(sc.job_snapshot()["phase"], "download")
        self.assertGreater(sc.job_snapshot()["percent"], 3.0)

    def test_下载失败透传原因(self):
        tmp = tempfile.mkdtemp(prefix="dm-dl-")
        path = Path(tmp) / "a.pkg.tar.zst"
        with mock.patch.object(sc.urllib.request, "urlopen",
                               side_effect=OSError("镜像连不上")):
            with self.assertRaises(sc.PackageError) as ctx:
                sc._download_to(self.SPEC, path, "ydotool")
        self.assertEqual(ctx.exception.reason, "download_failed")
        self.assertIn("下载失败", str(ctx.exception))

    def test_下载异常不把Python原文抛给界面(self):
        """真机 SSL EOF 时界面曾出现 `<urlopen error ...>`；现在只给分类文案"""
        tmp = tempfile.mkdtemp(prefix="dm-dl-")
        path = Path(tmp) / "a.pkg.tar.zst"
        boom = ssl.SSLError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred "
                            "in violation of protocol (_ssl.c:1082)")
        with mock.patch.object(sc.urllib.request, "urlopen", side_effect=boom), \
                mock.patch.object(sc.logger, "warning"):
            with self.assertRaises(sc.PackageError) as ctx:
                sc._download_to(self.SPEC, path, "ydotool")
        text = str(ctx.exception)
        self.assertIn("连接被镜像提前断开", text)
        self.assertNotIn("_ssl.c", text)
        self.assertNotIn("UNEXPECTED_EOF", text)

    def test_网络异常按类型分类(self):
        self.assertIn("超时", sc._net_error_text(TimeoutError("timed out")))
        self.assertIn("证书", sc._net_error_text(
            ssl.SSLError("certificate verify failed")))
        self.assertIn("DNS", sc._net_error_text(OSError(
            "[Errno -2] Name or service not known")))
        self.assertIn("404", sc._net_error_text(OSError("HTTP Error 404: Not Found")))

    def test_下载不完整要报错(self):
        tmp = tempfile.mkdtemp(prefix="dm-dl-")
        path = Path(tmp) / "a.pkg.tar.zst"
        body = b"x" * 10
        with mock.patch.object(sc.urllib.request, "urlopen",
                               return_value=_FakeResp(body, length=20)):
            with self.assertRaises(sc.PackageError) as ctx:
                sc._download_to(self.SPEC, path, "ydotool")
        self.assertEqual(ctx.exception.reason, "download_failed")
        self.assertIn("不完整", str(ctx.exception))

    def test_校验包名不符要拒绝(self):
        tmp = tempfile.mkdtemp(prefix="dm-verify-")
        path = Path(tmp) / "x.pkg.tar.zst"
        path.write_bytes(b"PKG")
        with mock.patch.object(sc, "_run", return_value=(0, "other 1.0\n")):
            with self.assertRaises(sc.PackageError) as ctx:
                sc._verify_package(path, self.SPEC)
        self.assertEqual(ctx.exception.reason, "verify_failed")
        self.assertIn("名称不符", str(ctx.exception))

    def test_空文件校验失败(self):
        tmp = tempfile.mkdtemp(prefix="dm-verify-")
        path = Path(tmp) / "x.pkg.tar.zst"
        path.write_bytes(b"")
        with self.assertRaises(sc.PackageError) as ctx:
            sc._verify_package(path, self.SPEC)
        self.assertEqual(ctx.exception.reason, "download_failed")

    def test_没有sudo权限时给出真实原因(self):
        sc._job = {"component": "ydotool", "state": "need_password"}
        sc._pw_waiting = True
        with mock.patch.object(
                sc, "_sudo_check",
                return_value=(False, "no_sudoers",
                              "deck is not in the sudoers file")):
            res = sc.provide_password("ydotool", "pw")
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "no_sudoers")
        self.assertIn("sudoers", res["error"])
        sc._pw_waiting = False
        sc._job = None

    def test_密码错误三次终止(self):
        sc._job = {"component": "ydotool", "state": "need_password"}
        sc._pw_waiting = True
        with mock.patch.object(
                sc, "_sudo_check",
                return_value=(False, "bad_password", "Sorry")):
            r1 = sc.provide_password("ydotool", "bad")
            r2 = sc.provide_password("ydotool", "bad")
            r3 = sc.provide_password("ydotool", "bad")
        self.assertEqual(r1["reason"], "bad_password")
        self.assertEqual(r2["reason"], "bad_password")
        self.assertEqual(r3["reason"], "too_many_attempts")
        sc._pw_waiting = False
        sc._job = None

    def test_非等待状态提交密码被拒绝(self):
        res = sc.provide_password("ydotool", "pw")
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "not_waiting")

    def test_无polkit代理时不尝试弹窗(self):
        with mock.patch.object(sc.shutil, "which", return_value="/usr/bin/tool"), \
                mock.patch.object(sc, "_run", return_value=(1, "")):
            self.assertFalse(sc._pkexec_ready())

    def test_有polkit代理时才走弹窗(self):
        with mock.patch.object(sc.shutil, "which", return_value="/usr/bin/tool"), \
                mock.patch.object(sc, "_run", return_value=(0, "123 polkit-kde")):
            self.assertTrue(sc._pkexec_ready())

    def test_免密sudo失效字样判定(self):
        self.assertTrue(sc._needs_password("sudo: a password is required"))
        self.assertFalse(sc._needs_password("error: failed to commit transaction"))

    def test_空密码被拒绝(self):
        res = sc.provide_password("ydotool", "   ")
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "empty")

    def test_polkit被拒时提示polkit原因(self):
        tmp = tempfile.mkdtemp(prefix="dm-pkg-")
        msgs = []

        def fake_pw(key, message, timeout_reason=""):
            msgs.append((message, timeout_reason))
            return "pw"

        def fake_exec(mode, pw, path, name, timeout=630.0):
            return (126, "Error executing command as another user: Not authorized") \
                if mode == "pkexec" else (0, "")

        with mock.patch.object(sc, "package_spec", return_value=dict(self.SPEC)), \
                mock.patch.object(sc, "_workspace_tmp", return_value=Path(tmp)), \
                mock.patch.object(sc, "_download_to", side_effect=self._fake_download), \
                mock.patch.object(sc, "_verify_package"), \
                mock.patch.object(sc, "sudo_silent", return_value=False), \
                mock.patch.object(sc, "_pkexec_ready", return_value=True), \
                mock.patch.object(sc, "_request_password", side_effect=fake_pw), \
                mock.patch.object(sc, "_exec_install", side_effect=fake_exec):
            sc._run_job(self._item())
        self.assertEqual(sc.job_snapshot()["state"], "done")
        self.assertEqual(msgs[0][1], "polkit 授权未通过")
        self.assertIn("polkit", msgs[0][0])

    def test_等待密码超时带上polkit原因(self):
        with mock.patch.object(sc, "_pw_event", threading.Event()), \
                mock.patch.object(sc, "_set"), \
                mock.patch.object(sc, "PASSWORD_WAIT_SECONDS", 0.01):
            with self.assertRaises(RuntimeError) as ctx:
                sc._request_password("grim", "等待授权", timeout_reason="polkit 授权未通过")
        self.assertIn("超时", str(ctx.exception))
        self.assertIn("polkit", str(ctx.exception))

    def test_失败原因按类型区分(self):
        self.assertIn("sudoers", sc._install_error_text(
            "deck is not in the sudoers file. This incident will be reported.", 1))
        self.assertIn("密码未被 sudo 接受", sc._install_error_text(
            "sudo: 1 incorrect password attempt", 1))
        self.assertIn("polkit", sc._install_error_text(
            "Error executing command as another user: Not authorized", 126))
        text = sc._install_error_text(
            "error: failed to commit transaction (conflicting files)\n"
            "grim: exists in filesystem", 1)
        self.assertIn("pacman 退出码 1", text)
        self.assertIn("exists in filesystem", text)


class ExecInstallTest(SelfcheckBase):
    """subprocess.run 的参数组合：capture_output 不能和 stdout/stderr 混用"""

    def test_只传capture_output并合并两路输出(self):
        proc = _FakeProc(rc=1, stdout="out line\n", stderr="err line\n")
        with mock.patch.object(sc.subprocess, "run", return_value=proc) as run:
            rc, out = sc._exec_install(
                "password", "pw", Path("/tmp/x.pkg.tar.zst"), "grim")
        self.assertEqual(rc, 1)
        self.assertIn("out line", out)
        self.assertIn("err line", out)
        kwargs = run.call_args.kwargs
        self.assertNotIn("stdout", kwargs)
        self.assertNotIn("stderr", kwargs)
        self.assertTrue(kwargs["capture_output"])
        # 密码模式统一走 askpass：密码只进子进程环境，不进 argv/stdin
        self.assertNotIn("input", kwargs)
        self.assertEqual(kwargs["stdin"], sc.subprocess.DEVNULL)
        self.assertEqual(kwargs["env"]["SUDO_ASKPASS_REQUIRE"], "force")
        self.assertEqual(kwargs["env"]["DECKMEIDO_SUDO_PASSWORD"], "pw")
        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], ["sudo", "-A"])
        self.assertNotIn("pw", " ".join(argv))

    def test_免密sudo与pkexec不传密码(self):
        for mode, expect in (("sudo", ["sudo", "-n"]), ("pkexec", ["pkexec"])):
            proc = _FakeProc(rc=0, stdout="ok")
            with mock.patch.object(sc.subprocess, "run", return_value=proc) as run:
                rc, out = sc._exec_install(
                    mode, None, Path("/tmp/x.pkg.tar.zst"), "grim")
            self.assertEqual(rc, 0)
            self.assertEqual(out, "ok")
            kwargs = run.call_args.kwargs
            self.assertNotIn("stdout", kwargs)
            self.assertNotIn("stderr", kwargs)
            self.assertNotIn("input", kwargs)
            self.assertEqual(run.call_args.args[0][:len(expect)], expect)


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
