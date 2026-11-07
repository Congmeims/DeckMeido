"""sudo 授权通道 + run_command stdin 回归测试。

覆盖：
- stdin 真正送达子进程、超时不挂死；
- askpass 参数/环境正确（密码不进 argv、不进助手脚本）；
- 缓存命中不重复询问、TTL 到期重新索要；
- 密码错误/不在 sudoers/askpass 不可用/超时的错误分类；
- 日志、审计、命令回显里不出现密码明文。

    python -m unittest tests.test_sudo_auth -v
"""
from __future__ import annotations

import asyncio
import io
import os
import subprocess
import sys
import threading
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 与 test_selfcheck 一致：server.tools 会导入 ptyprocess，Windows 开发机没有
if "ptyprocess" not in sys.modules:
    try:
        import ptyprocess  # noqa: F401
    except Exception:
        sys.modules["ptyprocess"] = types.ModuleType("ptyprocess")

from server import audit, sudo_auth, tools  # noqa: E402


class SudoAuthBase(unittest.TestCase):
    def setUp(self):
        sudo_auth.clear_all()
        sudo_auth.configure({})
        self.events = []
        sudo_auth.set_hook(self.events.append)

    def tearDown(self):
        sudo_auth.clear_all()
        sudo_auth.set_hook(None)
        sudo_auth.configure({})

    def states(self, state):
        return [e for e in self.events if e.get("state") == state]


class AskpassTests(SudoAuthBase):
    def test_askpass_helper_has_no_password(self):
        env = sudo_auth.sudo_env("hunter2!")
        self.assertEqual(env["SUDO_ASKPASS_REQUIRE"], "force")
        self.assertEqual(env[sudo_auth.ASKPASS_ENV], "hunter2!")
        helper = Path(env["SUDO_ASKPASS"])
        content = helper.read_text(encoding="utf-8")
        self.assertNotIn("hunter2!", content)
        self.assertIn(sudo_auth.ASKPASS_ENV, content)
        self.assertTrue(helper.name.endswith(".sh"))

    def test_verify_uses_askpass_env_not_argv(self):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen["env"] = kwargs.get("env") or {}
            seen["stdin"] = kwargs.get("stdin")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(sudo_auth.subprocess, "run", side_effect=fake_run):
            ok, reason, message = sudo_auth.verify("s3cret-pw")

        self.assertTrue(ok)
        self.assertEqual(reason, "")
        self.assertEqual(seen["argv"][:2], ["sudo", "-A"])
        self.assertIn("-v", seen["argv"])
        self.assertNotIn("s3cret-pw", " ".join(seen["argv"]))
        self.assertEqual(seen["env"][sudo_auth.ASKPASS_ENV], "s3cret-pw")
        self.assertEqual(seen["stdin"], subprocess.DEVNULL)


class CacheTests(SudoAuthBase):
    def test_cache_hit_does_not_ask_again(self):
        sudo_auth.store("pw-123456", "test", now=1000.0)
        with mock.patch.object(sudo_auth, "silent_sudo", return_value=False):
            first = sudo_auth.prepare("sudo systemctl restart plugin_loader", now=1001.0)
            second = sudo_auth.prepare("sudo systemctl restart plugin_loader", now=1002.0)
        self.assertTrue(first["ok"])
        self.assertEqual(first["mode"], "askpass")
        self.assertEqual(first["password"], "pw-123456")
        self.assertEqual(second["mode"], "askpass")
        self.assertEqual(self.states("need_password"), [])

    def test_ttl_expiry_asks_again(self):
        sudo_auth.store("pw-123456", "test", now=1000.0)
        with mock.patch.object(sudo_auth, "silent_sudo", return_value=False):
            res = sudo_auth.prepare("sudo true", now=1000.0 + sudo_auth.ttl() + 1)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "need_auth")
        self.assertEqual(len(self.states("need_password")), 1)
        self.assertIsNotNone(sudo_auth.pending_status())

    def test_configure_ttl(self):
        sudo_auth.configure({"security": {"sudo_ttl": 120}})
        self.assertEqual(sudo_auth.ttl(), 120)
        sudo_auth.configure({"security": {"sudo_ttl": 0}})
        self.assertEqual(sudo_auth.ttl(), sudo_auth.DEFAULT_TTL)

    def test_provide_password_stores_and_does_not_leak(self):
        with mock.patch.object(sudo_auth, "verify", return_value=(True, "", "")):
            with mock.patch.object(sudo_auth, "_audit_auth") as aud:
                res = sudo_auth.provide_password("pw-abcdef", "ui")
        self.assertTrue(res["ok"])
        self.assertNotIn("pw-abcdef", str(aud.call_args))
        self.assertEqual(sudo_auth.cache_status()["available"], True)
        self.assertNotIn("pw-abcdef", str(sudo_auth.cache_status()))


class ErrorCategoryTests(SudoAuthBase):
    def test_bad_password(self):
        reason, msg = sudo_auth.classify_failure(
            1, "Sorry, try again.\nsudo: 3 incorrect password attempts")
        self.assertEqual(reason, "bad_password")
        self.assertIn("密码", msg)

    def test_not_in_sudoers(self):
        reason, msg = sudo_auth.classify_failure(
            1, "deck is not in the sudoers file. This incident will be reported.")
        self.assertEqual(reason, "no_sudoers")
        self.assertIn("sudoers", msg)

    def test_askpass_unavailable(self):
        reason, msg = sudo_auth.classify_failure(
            1, "sudo: no askpass program specified, try setting SUDO_ASKPASS")
        self.assertEqual(reason, "askpass")

    def test_timeout(self):
        reason, msg = sudo_auth.classify_failure(1, "", timed_out=True)
        self.assertEqual(reason, "timeout")

    def test_verify_timeout_maps_to_timeout(self):
        with mock.patch.object(
                sudo_auth.subprocess, "run",
                side_effect=subprocess.TimeoutExpired(cmd="sudo", timeout=1)):
            ok, reason, msg = sudo_auth.verify("pw")
        self.assertFalse(ok)
        self.assertEqual(reason, "timeout")

    def test_remaining_attempts(self):
        with mock.patch.object(
                sudo_auth, "verify",
                return_value=(False, "bad_password", "管理员密码不正确")):
            first = sudo_auth.provide_password("bad", "ui")
            second = sudo_auth.provide_password("bad", "ui")
            third = sudo_auth.provide_password("bad", "ui")
        self.assertEqual(first["reason"], "bad_password")
        self.assertEqual(first["remaining"], 2)
        self.assertEqual(second["remaining"], 1)
        self.assertEqual(third["reason"], "too_many_attempts")
        for res in (first, second, third):
            self.assertNotIn("bad", res["error"])


class RedactionTests(SudoAuthBase):
    def test_inline_password_is_detected_and_redacted(self):
        cmd = "echo 'my-secret-pw' | sudo -S systemctl restart plugin_loader"
        self.assertTrue(sudo_auth.inline_sudo_password(cmd))
        redacted = sudo_auth.redact_command(cmd)
        self.assertNotIn("my-secret-pw", redacted)
        self.assertIn("***", redacted)
        self.assertIn("sudo", redacted)

    def test_normal_echo_pipe_is_not_rejected(self):
        self.assertFalse(sudo_auth.inline_sudo_password("echo y | sudo tee /etc/x"))

    def test_stdin_redaction(self):
        self.assertEqual(sudo_auth.redact_stdin("my-secret-pw"), "***")
        self.assertEqual(sudo_auth.redact_stdin(""), "")

    def test_cached_password_is_redacted_everywhere(self):
        sudo_auth.store("cached-secret-1", "test")
        text = "readback cached-secret-1 and 'cached-secret-1'"
        out = sudo_auth.redact_text(text)
        self.assertNotIn("cached-secret-1", out)

    def test_sudo_s_is_rewritten_to_askpass(self):
        self.assertEqual(
            sudo_auth.normalize_command("sudo -S systemctl restart x"),
            "sudo -A systemctl restart x")


class StdinDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def _posix_stub(self):
        # run_command 里有几处 Linux 专用调用（getuid / pwd）；Windows 开发机
        # 跑测试时用桩替代，只验证 stdin 与 sudo 分支逻辑
        return mock.patch.object(tools.os, "getuid", create=True, return_value=1000)

    async def test_stdin_reaches_child(self):
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c",
            "import sys; data = sys.stdin.read(); sys.stdout.write(data)",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out = await tools._comm_bounded(proc, b"hello-stdin\n", 10.0)
        self.assertEqual(out.decode("utf-8", "replace").strip(), "hello-stdin")

    async def test_stdin_writer_does_not_hang_on_timeout(self):
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(10)",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    tools._comm_bounded(proc, b"ignored\n", 0.4), timeout=5.0)
        finally:
            proc.kill()
            await proc.wait()

    async def test_run_command_rejects_inline_password_and_keeps_log_clean(self):
        buf = io.StringIO()
        with self._posix_stub(), redirect_stdout(buf):
            res = await tools.execute(
                "run_command",
                {"command": "echo 'leak-me-now' | sudo -S systemctl restart plugin_loader",
                 "reason": "重启服务"},
            )
        self.assertIn("拒绝执行", res)
        self.assertNotIn("leak-me-now", buf.getvalue())
        self.assertNotIn("leak-me-now", res)

    async def test_run_command_without_auth_does_not_spawn(self):
        sudo_auth.clear_all()
        sudo_auth.set_hook(lambda payload: None)
        with self._posix_stub(), \
                mock.patch.object(sudo_auth, "silent_sudo", return_value=False), \
                mock.patch.object(asyncio, "create_subprocess_shell") as spawn:
            res = await tools._run_command(
                "sudo systemctl restart plugin_loader", reason="重启服务")
        self.assertIn("授权", res)
        spawn.assert_not_called()
        self.assertIsNotNone(sudo_auth.pending_status())


class AuditTests(SudoAuthBase):
    def test_audit_records_have_no_password(self):
        audit.clear()
        with mock.patch.object(sudo_auth, "verify", return_value=(True, "", "")):
            sudo_auth.provide_password("pw-never-log", "ui")
        events = audit.recent(10)
        self.assertTrue(events)
        self.assertNotIn("pw-never-log", str(events))
        self.assertEqual(events[0]["kind"], "sudo")
        self.assertEqual(events[0]["action"], "auth")


if __name__ == "__main__":
    unittest.main()
