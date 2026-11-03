"""最小回归：误重启防护 + 游戏识别会话边界。

只依赖标准库，Windows / Linux 都能跑：
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import policy  # noqa: E402


class PolicyTest(unittest.TestCase):
    def ctx(self, text: str, **kw) -> policy.TurnContext:
        base = dict(sid="s1", turn_id="t2", user_text=text, client="qam",
                    device="dev1", boot_id="boot-a")
        base.update(kw)
        return policy.TurnContext(**base)

    def test_history_reboot_request_does_not_authorize_power(self):
        """(a) 历史里残留重启请求，当前消息只是问游戏 → 电源命令必须被拒"""
        ctx = self.ctx("当前游戏《Stellar Blade》这个 BOSS 怎么打？")
        decision = policy.check(
            "run_command", {"command": "sudo -S systemctl reboot"}, ctx, intent=None)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, "denied")
        self.assertTrue(decision.reason)

    def test_current_message_reboot_still_allowed(self):
        """反例：本轮用户明确要求重启时不能被误拦"""
        ctx = self.ctx("帮我重启一下电脑")
        decision = policy.check("run_command", {"command": "systemctl reboot"}, ctx)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.code, "user_message")

    def test_sudo_password_intent_only_authorizes_password_reply(self):
        """老密码文本留在 history 里也不算授权；只有有效 pending_intent + 密码样回复才行"""
        it = policy.make_intent("sudo_password", self.ctx("帮我重启电脑", turn_id="t1"),
                                now=1000.0, ttl=600.0)
        # 同一端、未过期、回复像密码 → 允许
        ok = policy.check("run_command", {"command": "sudo -S systemctl reboot"},
                          self.ctx("2333", boot_id="boot-a"), intent=it, now=1100.0)
        self.assertTrue(ok.allowed)
        self.assertEqual(ok.code, "sudo_password")
        # 但换成一句新任务 → 不授权
        bad = policy.check("run_command", {"command": "sudo -S systemctl reboot"},
                           self.ctx("当前游戏怎么打？", boot_id="boot-a"),
                           intent=it, now=1100.0)
        self.assertFalse(bad.allowed)

    def test_boot_id_change_reconciles_intent(self):
        """(b) boot_id 变化后 reboot=executed，sudo_password=expired"""
        reboot = policy.make_intent("reboot", self.ctx("帮我重启"), now=100.0)
        out = policy.reconcile_intent(reboot, "boot-b", now=200.0)
        self.assertEqual(out["status"], "executed")

        pwd = policy.make_intent("sudo_password", self.ctx("帮我重启"), now=100.0)
        out = policy.reconcile_intent(pwd, "boot-b", now=200.0)
        self.assertEqual(out["status"], "expired")

    def test_locked_shot_blocks_power_commands(self):
        """(c) 截图轮 locked_shot 下电源命令也不得执行"""
        self.assertTrue(policy.blocks_in_locked_shot(
            "run_command", {"command": "sudo systemctl reboot"}))
        self.assertTrue(policy.blocks_in_locked_shot(
            "run_command", {"command": "shutdown -h now"}))
        self.assertFalse(policy.blocks_in_locked_shot(
            "run_command", {"command": "uptime -s"}))

    def test_power_detection_not_fooled_by_plain_text(self):
        self.assertIsNone(policy.power_action("grep reboot /var/log/syslog"))
        self.assertIsNone(policy.power_action("systemctl status deckmeido"))
        self.assertEqual(policy.power_action("sudo -S systemctl reboot"), "reboot")
        self.assertEqual(policy.power_action("sh -c 'systemctl poweroff'"), "poweroff")

    def test_service_restart_without_power_intent_is_allowed(self):
        """本轮没提重启时，systemctl restart <service> 只是服务操作，必须放行"""
        ctx = self.ctx("把后端的日志清一下")
        for cmd in ("systemctl restart deckmeido.service",
                    "systemctl restart nginx",
                    "sudo -S systemctl --user restart deckmeido.service",
                    "systemctl restart --now deckmeido.service"):
            decision = policy.check("run_command", {"command": cmd}, ctx)
            self.assertTrue(decision.allowed, cmd)
            self.assertEqual(decision.code, "not_power", cmd)
        # 截图轮也不能把它当成屏幕/电源操作丢掉
        self.assertFalse(policy.blocks_in_locked_shot(
            "run_command", {"command": "systemctl restart deckmeido.service"}))

    def test_power_target_restart_still_blocked(self):
        """systemctl restart 指向电源 target 时仍然要拦；kexec 也算电源"""
        ctx = self.ctx("把后端的日志清一下")
        for cmd in ("systemctl restart systemd-reboot.service",
                    "systemctl restart reboot.target",
                    "systemctl restart systemd-poweroff.service",
                    "systemctl restart kexec.target",
                    "systemctl kexec"):
            decision = policy.check("run_command", {"command": cmd}, ctx)
            self.assertFalse(decision.allowed, cmd)
            self.assertEqual(decision.code, "denied", cmd)
        # 本轮明确说重启时，电源 target 的 restart 才可通过
        self.assertTrue(policy.check(
            "run_command", {"command": "systemctl restart systemd-reboot.service"},
            self.ctx("帮我重启电脑")).allowed)

    def test_negative_phrase_is_not_authorization(self):
        ctx = self.ctx("先别重启，我还没保存")
        decision = policy.check("run_command", {"command": "systemctl reboot"}, ctx)
        self.assertFalse(decision.allowed)

    def test_steamosctl_session_switch_requires_current_intent(self):
        """steamosctl 会重建会话：历史残留不授权，本轮明确要切模式才放行"""
        stale = self.ctx("当前游戏怎么打？")
        self.assertFalse(policy.check(
            "run_command", {"command": "steamosctl switch-to-desktop-mode"}, stale).allowed)
        now = self.ctx("切到桌面模式，帮我装个软件")
        self.assertTrue(policy.check(
            "run_command", {"command": "steamosctl switch-to-desktop-mode"}, now).allowed)
        # 普通服务操作不受影响
        self.assertTrue(policy.check(
            "run_command", {"command": "systemctl status deckmeido"}, now).allowed)


class GameBoundaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 插件代码 import 时会读 decky；测试环境里塞一个最小 stub
        if "decky" not in sys.modules:
            stub = types.ModuleType("decky")
            stub.DECKY_PLUGIN_SETTINGS_DIR = tempfile.mkdtemp(prefix="deckmeido-test-")
            stub.DECKY_PLUGIN_DIR = str(ROOT / "plugin")
            stub.logger = types.SimpleNamespace(
                info=lambda *a, **k: None, warning=lambda *a, **k: None,
                error=lambda *a, **k: None, debug=lambda *a, **k: None)
            sys.modules["decky"] = stub
        import plugin.main as m  # noqa: E402
        cls.m = m

    def test_added_before_last_client_version_is_ignored(self):
        """(d) added 早于最后一次 Client version: → 不返回旧游戏"""
        tail = "\n".join([
            "[2026-11-02 22:26:28] Game process added : AppID 3627188256",
            "[2026-11-03 10:04:39] Client version: 1762135509",
        ])
        self.assertIsNone(self.m._steam_running_from_tail(tail, {"3627188256": "StellaSora"}))

    def test_added_after_client_version_is_reported(self):
        tail = "\n".join([
            "[2026-11-03 10:04:39] Client version: 1762135509",
            "[2026-11-03 10:30:00] Game process added : AppID 12345",
        ])
        hit = self.m._steam_running_from_tail(tail, {"12345": "New Game"})
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], "12345")
        self.assertEqual(hit[1], "New Game")

    def test_no_client_version_means_no_confidence(self):
        tail = "[2026-11-02 22:26:28] Game process added : AppID 3627188256"
        self.assertIsNone(self.m._steam_running_from_tail(tail, {"3627188256": "StellaSora"}))

    def test_current_game_empty_when_no_proc_and_no_log_hit(self):
        """进程表为空、账本没有本次会话内的 added → _current_game 返回空"""
        from unittest import mock
        with mock.patch.object(self.m, "_app_names",
                               return_value={"3627188256": "StellaSora"}), \
             mock.patch.object(self.m, "_running_app_ids", return_value=[]), \
             mock.patch.object(self.m, "_steam_running_app", return_value=None):
            game = self.m._current_game()
        self.assertEqual(game["name"], "")
        self.assertEqual(game["confidence"], "none")
        self.assertEqual(game["source"], "none")


if __name__ == "__main__":
    unittest.main()
