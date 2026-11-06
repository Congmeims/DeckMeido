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


class SurfacePolicyTest(unittest.TestCase):
    """按入口区分单轮策略：ask/translate 保持一问一答，只有 chat 放开多轮"""

    def test_qam_ask_and_translate_stay_locked(self):
        """(1) QAM 的所有入口都保持一问一答，不会被大屏聊天页的放开放宽"""
        self.assertTrue(policy.shot_lock_for("qam", "ask"))
        self.assertTrue(policy.shot_lock_for("qam", "translate"))
        # QAM 面板里的普通输入（哪怕标了 chat）也不能放开
        self.assertTrue(policy.shot_lock_for("qam", "chat"))
        # 老前端没传 surface：按原来的一问一答处理，不能被这次改动误放开
        self.assertTrue(policy.shot_lock_for("qam", ""))
        self.assertTrue(policy.shot_lock_for("qam", None))
        # 大屏询问页 / 截图翻译仍然单轮
        self.assertTrue(policy.shot_lock_for("bigscreen", "ask"))
        self.assertTrue(policy.shot_lock_for("bigscreen", "translate"))

    def test_chat_surface_unlocks_tool_rounds(self):
        """(2) 只有大屏聊天页放开：工具调用后可以继续推进"""
        self.assertFalse(policy.shot_lock_for("bigscreen", "chat"))
        # 网页/桌面端本来就不在插件的锁里
        self.assertFalse(policy.shot_lock_for("web", "chat"))
        self.assertFalse(policy.shot_lock_for(None, "chat"))

    def test_chat_continuation_is_bounded(self):
        """(3) chat 兜底：只说不做最多补 2 次，之后按最终答复收尾"""
        self.assertTrue(policy.chat_should_continue("bigscreen", "chat", "好的，我去看看。", 0))
        self.assertTrue(policy.chat_should_continue("bigscreen", "chat", "稍等一下，我先查一下。", 1))
        self.assertFalse(policy.chat_should_continue("bigscreen", "chat", "好的，我去看看。", 2))
        # ask/translate 不走催促，保持答完即停
        self.assertFalse(policy.chat_should_continue("bigscreen", "ask", "好的，我去看看。", 0))
        self.assertFalse(policy.chat_should_continue("bigscreen", "translate", "好的，我去看看。", 0))
        # QAM 面板即使标了 chat 也不催，行为和小屏完全一致
        self.assertFalse(policy.chat_should_continue("qam", "chat", "好的，我去看看。", 0))
        # 正常收尾的答复不会被误催促
        self.assertFalse(policy.chat_should_continue(
            "bigscreen", "chat", "《Stellar Blade》这个 BOSS 弱点是背后核心，破防后一套带走。", 0))


class RepeatGuardTest(unittest.TestCase):
    """死循环闸门：同一工具同参数连续重复必须能自己停下来"""

    def test_signature_is_order_insensitive(self):
        self.assertEqual(policy.call_signature("desktop", {"a": 1, "b": 2}),
                         policy.call_signature("desktop", {"b": 2, "a": 1}))
        self.assertNotEqual(policy.call_signature("desktop", {"x": 1}),
                            policy.call_signature("desktop", {"x": 2}))

    def test_consecutive_repeat_counts(self):
        state: dict = {}
        self.assertEqual(policy.repeat_step(state, "screenshot|{}"), 1)
        self.assertEqual(policy.repeat_step(state, "screenshot|{}"), 2)
        self.assertEqual(policy.repeat_step(state, "screenshot|{}"), 3)
        # 换一个调用就重新计数
        self.assertEqual(policy.repeat_step(state, "run_command|{\"command\": \"ls\"}"), 1)
        self.assertEqual(policy.repeat_step(state, "screenshot|{}"), 1)

    def test_skip_then_abort_thresholds(self):
        """第 3 次不再执行，第 4 次直接收尾（88cad046 连截七次才被手动终止）"""
        self.assertEqual(policy.LOOP_SKIP_AT, 3)
        self.assertEqual(policy.LOOP_ABORT_AT, 4)
        state: dict = {}
        self.assertEqual([policy.repeat_step(state, "s|{}") for _ in range(4)],
                         [1, 2, 3, 4])


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
