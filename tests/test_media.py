"""媒体播控：意图词表、动作白名单、duck/restore 配对、失败降级。

全部用假 busctl（替换 media._run），不碰真实 D-Bus，也不会出声。
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import media  # noqa: E402


class FakeBus:
    """按 busctl 的输出格式回放 get/set/call/list。"""

    def __init__(self, services=None, status="Playing", volume=0.8):
        self.services = (["org.mpris.MediaPlayer2.decky_music.instance42"]
                         if services is None else services)
        self.status = status
        self.volume = volume
        self.calls = []
        self.fail_call = False

    def run(self, cmd, timeout=3.0):
        self.calls.append(list(cmd))
        args = cmd[2:]  # 去掉 busctl --user
        sub = args[0]
        if sub == "list":
            out = "NAME PID PROCESS USER CONNECTION UNIT ACTIVE SUB\n"
            for name in self.services:
                out += f"{name} 42 player deck :1.1 plugin_loader.service - -\n"
            return subprocess.CompletedProcess(cmd, 0, out, "")
        if sub == "get-property":
            prop = args[4]
            if prop == "Volume":
                return subprocess.CompletedProcess(cmd, 0, f"d {self.volume}\n", "")
            if prop == "PlaybackStatus":
                return subprocess.CompletedProcess(cmd, 0, f's "{self.status}"\n', "")
        if sub == "set-property":
            self.volume = float(args[6])
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if sub == "call":
            if self.fail_call:
                return subprocess.CompletedProcess(
                    cmd, 1, "",
                    "Call failed: org.freedesktop.DBus.Error.ServiceUnknown")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    # ---- 断言辅助 ----

    def sets(self):
        return [c for c in self.calls if c[2] == "set-property"]

    def methods(self):
        return [c[-1] for c in self.calls if c[2] == "call"]


class MediaTests(unittest.TestCase):
    def setUp(self):
        self._orig_run = media._run
        self.bus = FakeBus()
        media._run = self.bus.run
        media._reset()

    def tearDown(self):
        media._run = self._orig_run
        media._reset()

    # ---- 意图词表 ----

    def test_intent_words_cover_all_actions(self):
        cases = {
            "下一首": "next",
            "换一首": "next",
            "切歌": "next",
            "上一首": "previous",
            "暂停": "pause",
            "先停一下": "pause",
            "继续": "play",
            "继续播放": "play",
            "播放": "play",
            "停止": "stop",
            "别放了": "stop",
            "音量调大": "volume_up",
            "大点声": "volume_up",
            "音量调小": "volume_down",
            "小点声": "volume_down",
            "静音": "mute",
            "取消静音": "unmute",
        }
        for text, expect in cases.items():
            with self.subTest(text=text):
                got = media.match_intent(text)
                self.assertIsNotNone(got)
                self.assertEqual(got[0], expect)
                self.assertTrue(got[1])  # 反馈词不能为空

    def test_intent_priority(self):
        # 「停止播放」不能被「播放」抢走；「取消静音」必须优先于「静音」
        self.assertEqual(media.match_intent("停止播放")[0], "stop")
        self.assertEqual(media.match_intent("取消静音")[0], "unmute")
        self.assertEqual(media.match_intent("解除静音")[0], "unmute")

    def test_intent_with_punctuation_and_spaces(self):
        self.assertEqual(media.match_intent(" 下一首！ ")[0], "next")
        self.assertEqual(media.match_intent("音量，调大")[0], "volume_up")

    def test_intent_ignores_normal_sentences(self):
        for text in ("把客厅灯打开", "今天天气怎么样", "", "   "):
            with self.subTest(text=text):
                self.assertIsNone(media.match_intent(text))

    def test_intent_rejects_questions(self):
        # 带疑问词的句子交给模型，避免「下一首是什么歌」被误切歌
        for text in ("我想知道下一首歌唱的是什么内容呢",
                     "这首歌唱的什么意思",
                     "现在放的是哪首歌"):
            with self.subTest(text=text):
                self.assertIsNone(media.match_intent(text))

    # ---- 动作白名单 ----

    def test_unknown_action_is_rejected_before_any_command(self):
        with self.assertRaises(media.MediaError):
            media.execute("rm -rf /")
        self.assertEqual(self.bus.calls, [])

    def test_actions_map_to_fixed_mpris_methods(self):
        for action, method in (("next", "Next"), ("previous", "Previous"),
                               ("pause", "Pause"), ("play", "Play"),
                               ("stop", "Stop")):
            with self.subTest(action=action):
                media.execute(action)
                self.assertEqual(self.bus.methods()[-1], method)
        self.assertEqual(media.VOLUME_ACTIONS,
                         ("volume_up", "volume_down", "mute", "unmute"))

    def test_prefers_decky_music_service(self):
        self.bus.services = ["org.mpris.MediaPlayer2.other.instance1",
                             "org.mpris.MediaPlayer2.decky_music.instance42"]
        media.execute("next")
        self.assertIn("decky_music", self.bus.calls[-1][3])

    # ---- 音量 ----

    def test_volume_step_and_clamp(self):
        self.bus.volume = 0.95
        media.execute("volume_up")
        self.assertAlmostEqual(self.bus.volume, 1.0)
        self.bus.volume = 0.05
        media.execute("volume_down")
        self.assertAlmostEqual(self.bus.volume, 0.0)

    def test_mute_and_unmute_restore_previous(self):
        self.bus.volume = 0.6
        media.execute("mute")
        self.assertAlmostEqual(self.bus.volume, 0.0)
        media.execute("unmute")
        self.assertAlmostEqual(self.bus.volume, 0.6)

    # ---- duck / restore ----

    def test_duck_and_restore_pair(self):
        self.assertTrue(media.duck())
        self.assertAlmostEqual(self.bus.volume, 0.2)
        media.restore()
        self.assertAlmostEqual(self.bus.volume, 0.8)

    def test_duck_twice_only_sets_once(self):
        self.assertTrue(media.duck())
        before = len(self.bus.sets())
        self.assertTrue(media.duck())
        self.assertEqual(len(self.bus.sets()), before)
        media.restore()
        self.assertAlmostEqual(self.bus.volume, 0.8)

    def test_duck_skipped_when_not_playing(self):
        self.bus.status = "Paused"
        self.assertFalse(media.duck())
        self.assertEqual(self.bus.sets(), [])
        media.restore()  # 没压过，恢复必须是空操作
        self.assertEqual(self.bus.sets(), [])

    def test_duck_skipped_without_player(self):
        self.bus.services = []
        self.assertFalse(media.duck())
        media.restore()
        self.assertEqual(self.bus.sets(), [])

    def test_restore_keeps_user_volume_change(self):
        self.assertTrue(media.duck())
        self.bus.volume = 0.5  # 用户中途自己调了（或说了「音量调大」）
        media.restore()
        self.assertAlmostEqual(self.bus.volume, 0.5)

    def test_restore_without_duck_does_nothing(self):
        media.restore()
        self.assertEqual(self.bus.calls, [])

    # ---- 失败降级 ----

    def test_command_failure_is_readable(self):
        self.bus.fail_call = True
        with self.assertRaises(media.MediaError) as ctx:
            media.execute("next")
        self.assertIn("命令失败", str(ctx.exception))
        self.assertEqual(ctx.exception.speech, "操作失败")

    def test_no_bustctl_is_readable(self):
        def boom(cmd, timeout=3.0, **kwargs):
            raise FileNotFoundError("busctl")

        orig = media.subprocess.run
        media.subprocess.run = boom
        # setUp 把 _run 换成了假总线；这里用真实实现验证「没有 busctl」的降级
        media._run = self._orig_run
        self.addCleanup(setattr, media.subprocess, "run", orig)
        with self.assertRaises(media.MediaError) as ctx:
            media.execute("pause")
        self.assertIn("busctl", str(ctx.exception))
        self.assertEqual(ctx.exception.speech, "控制不了")

    def test_duck_never_raises(self):
        def boom(cmd, timeout=3.0):
            raise media.MediaError("炸了")

        media._run = boom
        self.assertFalse(media.duck())
        media.restore()  # 不能抛


if __name__ == "__main__":
    unittest.main()
