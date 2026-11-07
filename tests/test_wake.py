"""智能音箱模式的状态机与三道闸（不需要真麦克风/真模型）。

用假的检测器、转写和对话入口驱动真实 WakeService：
* KWS 命中 → 必须有 VAD 真人声、且持续够长，才转写、才发送；
* 半双工 duck 只在真正开始录音时引用一次，转写前必须恢复；
* 识别为空、说话太短、用户中止、对话报错都要回到可用状态；
* 音频只以内存 WAV 交给 STT，模型目录里不产生任何临时音频文件。
"""
from __future__ import annotations

import asyncio
import io
import sys
import tempfile
import unittest
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import media, wake  # noqa: E402


class FakeDetector:
    def __init__(self):
        self.hit = False
        self.speech_on = False
        self.resets = 0
        self.loaded = False

    def load(self):
        self.loaded = True

    def accept(self, frame):
        return "你好梅朵" if self.hit else None

    def speech(self, frame):
        return self.speech_on

    def reset(self):
        self.resets += 1


FRAME = b"\x00\x00" * (wake.FRAME_BYTES // 2)


class FakeCapture:
    """只记录暂停状态；不产生真实音频。"""

    def __init__(self):
        self.paused = []
        self.started = False

    def start(self):
        self.started = True

    def read(self, timeout=0.2):
        return b""

    def set_paused(self, paused):
        self.paused.append(bool(paused))

    def stop(self):
        self.started = False


class WakeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.detector = FakeDetector()
        self.duck_calls = 0
        self.restore_calls = 0
        self.transcribe_calls = []
        self.chat_calls = []
        self.tts_stop_calls = 0
        self.cancel_calls = 0
        self.ack_synth_calls = []
        self.ack_play_calls = []
        self.ack_data = b"RIFF-fake-ack"
        self.ack_synth_error = None
        self.ack_play_error = None
        self.ack_play_gate = None
        self.text = "把客厅灯打开"
        # 音乐 duck / 固定动作：只记调用顺序，不碰真实 MPRIS
        self.media_events = []
        self.media_error = None
        self.media_duck_result = True
        self.states = []
        # 点播：假解析 / 假随机 / 假开播，全部只记调用
        self.song_plan = media.SongPlan("ok", "放周杰伦的稻香",
                                        {"id": "1001", "name": "稻香"},
                                        "稻香", "周杰伦")
        self.random_song_plan = media.SongPlan("ok", "放了",
                                               {"id": "1002", "name": "晴天"},
                                               "晴天", "周杰伦")
        self.resolve_calls = []
        self.played_items = []
        self.play_error = None

        def duck():
            self.duck_calls += 1

        def restore():
            self.restore_calls += 1

        def transcribe(wav):
            self.transcribe_calls.append(wav)
            return self.text

        async def chat(text, force_tts=False, client=""):
            self.chat_calls.append((text, force_tts, client))

        def ack_synth(text):
            self.ack_synth_calls.append(text)
            if self.ack_synth_error:
                raise self.ack_synth_error
            return self.ack_data

        async def ack_play(data):
            self.ack_play_calls.append(data)
            if self.ack_play_gate is not None:
                await self.ack_play_gate.wait()
            if self.ack_play_error:
                raise self.ack_play_error

        def media_duck():
            self.media_events.append("duck")
            return self.media_duck_result

        def media_restore():
            self.media_events.append("restore")

        def media_action(action):
            self.media_events.append(f"action:{action}")
            if self.media_error:
                raise self.media_error

        def media_resolve(query):
            self.resolve_calls.append(query)
            if isinstance(self.song_plan, Exception):
                raise self.song_plan
            return self.song_plan

        def media_random():
            self.media_events.append("random")
            return self.random_song_plan

        def media_play(item):
            self.media_events.append("play")
            self.played_items.append(item)
            if self.play_error:
                raise self.play_error

        self.chat_fn = chat
        self.service = wake.WakeService(
            model_root=Path(self.tmp.name),
            detector_factory=lambda root, cfg: self.detector,
            capture_factory=lambda: None,
            transcribe=transcribe,
            chat=chat,
            duck=duck,
            restore=restore,
            ack_synth=ack_synth,
            ack_play=ack_play,
            media_duck=media_duck,
            media_restore=media_restore,
            media_action=media_action,
            media_resolve=media_resolve,
            media_random=media_random,
            media_play=media_play,
        )
        self.service.set_tts_stop(self._tts_stop)
        self.service.set_handlers(
            cancel_chat=self._cancel_chat,
            on_state=lambda st: self.states.append(st["state"]))
        self.service.apply({"enable": True})

    def _tts_stop(self):
        self.tts_stop_calls += 1
        async def done():
            return None
        return done()

    def _cancel_chat(self):
        self.cancel_calls += 1
        task = self.service._task
        if task is not None and not task.done():
            task.cancel()

    async def _drain(self, svc=None):
        svc = svc or self.service
        for _ in range(8):
            await asyncio.sleep(0)
            task = svc._task
            if task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=0.2)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass

    async def _arm(self):
        await self.service.arm()
        self.assertEqual(self.service.state, "armed")
        self.assertTrue(self.detector.loaded)
        await self._wait_ack()

    async def _wait_ack(self):
        """等后台预合成结束：成功或失败都算准备好，不能卡住测试。"""
        for _ in range(200):
            if self.service._ack_wav or self.service._ack_error:
                return
            await asyncio.sleep(0.01)
        self.fail("唤醒回应预合成没有完成")

    async def _wake(self, expect_listening: bool = True):
        self.detector.hit = True
        self.service.feed_frame(FRAME, now=self.service._clock())
        self.detector.hit = False
        self.assertEqual(self.service.state, "waking")
        await self._drain()
        if expect_listening:
            self.assertEqual(self.service.state, "listening")

    async def _speak_then_silence(self, speech_frames: int, silence_frames: int,
                                   start: float = 0.0):
        self.detector.speech_on = True
        for i in range(speech_frames):
            self.service.feed_frame(FRAME, now=start + i * 0.16)
        self.detector.speech_on = False
        for i in range(silence_frames):
            self.service.feed_frame(FRAME, now=start + (speech_frames + i) * 0.16)

    # ---- 基本链路 ----

    async def test_wake_listen_transcribe_chat_followup(self):
        await self._arm()
        await self._wake()
        await self._speak_then_silence(4, 5)  # 640ms 语音，800ms 静音 → 端点
        await self._drain()
        self.assertEqual(self.duck_calls, 1)
        self.assertEqual(self.restore_calls, 1)
        self.assertEqual(len(self.transcribe_calls), 1)
        self.assertEqual(self.chat_calls, [(self.text, True, "wake")])
        # 交给 STT 的必须是内存 WAV，且没有落盘
        with wave.open(io.BytesIO(self.transcribe_calls[0]), "rb") as wav:
            self.assertEqual(wav.getnchannels(), 1)
            self.assertEqual(wav.getframerate(), wake.SAMPLE_RATE)
        self.assertEqual(list(Path(self.tmp.name).glob("*.wav")), [])
        # 播放结束 → 追问窗口；窗口内没人说话再退回待机
        self.assertEqual(self.service.state, "listening")
        self.service.feed_frame(FRAME, now=self.service._clock() + 5.0)
        self.assertEqual(self.service.state, "armed")

    # ---- 三道闸 ----

    async def test_gate_no_speech_is_not_transcribed_or_sent(self):
        await self._arm()
        await self._wake()
        # 回应播完后重新计时：超过 wake_timeout_sec 没人说话就退回待机
        self.service.feed_frame(FRAME, now=self.service._wait_until + 0.1)
        self.assertEqual(self.service.state, "armed")
        self.assertEqual(self.transcribe_calls, [])
        self.assertEqual(self.chat_calls, [])
        self.assertEqual(self.duck_calls, 0)

    async def test_gate_too_short_speech_is_dropped(self):
        await self._arm()
        await self._wake()
        await self._speak_then_silence(2, 5)  # 320ms < 600ms
        await self._drain()
        self.assertEqual(self.transcribe_calls, [])
        self.assertEqual(self.chat_calls, [])
        self.assertEqual(self.service.state, "armed")
        self.assertEqual(self.duck_calls, 1)
        self.assertEqual(self.restore_calls, 1)

    async def test_gate_empty_transcript_returns_to_armed(self):
        self.text = ""
        await self._arm()
        await self._wake()
        await self._speak_then_silence(4, 5)
        await self._drain()
        self.assertEqual(len(self.transcribe_calls), 1)
        self.assertEqual(self.chat_calls, [])
        self.assertEqual(self.service.state, "armed")

    async def test_gate_kws_miss_does_nothing(self):
        await self._arm()
        for i in range(10):
            self.service.feed_frame(FRAME, now=i * 0.16)
        self.assertEqual(self.service.state, "armed")
        self.assertEqual(self.transcribe_calls, [])
        self.assertEqual(self.duck_calls, 0)

    # ---- 唤醒回应「我在」 ----

    async def test_ack_is_prepared_before_wake_and_not_synthesized_on_hit(self):
        await self._arm()
        self.assertEqual(self.ack_synth_calls, ["我在"])
        self.assertTrue(self.service.status()["ack"]["ready"])
        await self._wake()
        # 命中瞬间只播放缓存，绝不能再去合成
        self.assertEqual(self.ack_play_calls, [self.ack_data])
        self.assertEqual(len(self.ack_synth_calls), 1)
        self.assertEqual(self.service.state, "listening")

    async def test_ack_synth_failure_degrades_to_screen_only(self):
        self.ack_synth_error = RuntimeError("TTS 崩了")
        await self._arm()
        st = self.service.status()
        self.assertFalse(st["ack"]["ready"])
        self.assertIn("回应合成失败", st["ack"]["error"])
        await self._wake()
        self.assertEqual(self.ack_play_calls, [])
        # 降级后整条对话链路必须照常可用
        await self._speak_then_silence(4, 5)
        await self._drain()
        self.assertEqual(self.chat_calls, [(self.text, True, "wake")])
        self.assertEqual(self.service.state, "listening")

    async def test_ack_play_failure_still_enters_listening(self):
        await self._arm()
        self.ack_play_error = RuntimeError("播放器没起来")
        await self._wake()
        self.assertEqual(self.service.state, "listening")
        self.assertIn("回应播放失败", self.service._ack_error)
        await self._speak_then_silence(4, 5)
        await self._drain()
        self.assertEqual(self.chat_calls, [(self.text, True, "wake")])

    async def test_listen_timeout_reverts_state_to_armed(self):
        await self._arm()
        await self._wake()
        self.assertEqual(self.service.state, "listening")
        self.service.feed_frame(FRAME, now=self.service._wait_until + 0.1)
        # 屏幕状态必须跟着回待机，不能停在「在听」
        self.assertEqual(self.service.state, "armed")
        self.assertEqual(self.transcribe_calls, [])

    async def test_stop_while_ack_playing_returns_to_armed(self):
        gate = asyncio.Event()
        self.ack_play_gate = gate
        await self._arm()
        self.detector.hit = True
        self.service.feed_frame(FRAME, now=self.service._clock())
        self.detector.hit = False
        self.assertEqual(self.service.state, "waking")
        await self.service.stop()
        await self._drain()
        self.assertEqual(self.service.state, "armed")
        self.assertEqual(self.transcribe_calls, [])
        gate.set()

    async def test_ack_pauses_capture_until_listening(self):
        cap = FakeCapture()

        async def ack_play(data):
            self.ack_play_calls.append(data)

        svc = wake.WakeService(
            model_root=Path(self.tmp.name),
            detector_factory=lambda root, cfg: self.detector,
            capture_factory=lambda: cap,
            ack_synth=lambda text: self.ack_data,
            ack_play=ack_play,
        )
        svc.apply({"enable": True})
        await svc.arm()
        for _ in range(200):
            if svc._ack_wav:
                break
            await asyncio.sleep(0.01)
        self.detector.hit = True
        svc.feed_frame(FRAME, now=svc._clock())
        self.detector.hit = False
        self.assertEqual(svc.state, "waking")
        # 播回应期间不能采集
        self.assertTrue(cap.paused[-1])
        await self._drain(svc)
        self.assertEqual(svc.state, "listening")
        self.assertFalse(cap.paused[-1])
        await svc.disarm()

    async def test_tts_start_switches_to_speaking(self):
        release = asyncio.Event()

        async def chat(text, force_tts=False, client=""):
            self.chat_calls.append((text, True, "wake"))
            await release.wait()

        self.service.set_handlers(chat=chat)
        await self._arm()
        await self._wake()
        await self._speak_then_silence(4, 5)
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertEqual(self.service.state, "thinking")
        self.service.note_tts("start")
        self.assertEqual(self.service.state, "speaking")
        self.service.note_tts("end")
        release.set()
        await self._drain()
        self.assertEqual(self.service.state, "listening")

    async def test_stop_cancels_round_and_releases_duck(self):
        started = asyncio.Event()

        async def chat(text, force_tts=False, client=""):
            self.chat_calls.append((text, True, "wake"))
            started.set()
            await asyncio.Event().wait()

        self.service.set_handlers(chat=chat)
        await self._arm()
        await self._wake()
        await self._speak_then_silence(4, 5)
        await asyncio.wait_for(started.wait(), timeout=1.0)
        self.assertEqual(self.service.state, "thinking")
        await self.service.stop()
        await self._drain()
        self.assertEqual(self.service.state, "armed")
        self.assertEqual(self.cancel_calls, 1)
        self.assertEqual(self.tts_stop_calls, 1)
        self.assertEqual(self.restore_calls, 1)

    async def test_transcribe_error_enters_error_then_recovers(self):
        def boom(wav):
            raise RuntimeError("模型崩了")

        self.service.set_handlers(transcribe=boom)
        await self._arm()
        await self._wake()
        await self._speak_then_silence(4, 5)
        await self._drain()
        self.assertEqual(self.chat_calls, [])
        self.assertEqual(self.service.state, "error")
        self.assertIn("模型崩了", self.service.status()["last_error"])
        # 组件是好的，只是这一轮识别失败：3 秒后必须自动回到待机
        self.assertIsNotNone(self.service._error_timer)

    async def test_arm_failure_stays_in_error(self):
        """启动就失败（缺模型/缺 sherpa/麦克风被占）不能自动自称「监听中」"""
        def boom(root, cfg):
            raise wake.WakeError("没有 sherpa")

        svc = wake.WakeService(model_root=Path(self.tmp.name),
                               detector_factory=boom, capture_factory=lambda: None)
        svc.apply({"enable": True})
        with self.assertRaises(wake.WakeError):
            await svc.arm()
        self.assertEqual(svc.state, "error")
        st = svc.status()
        self.assertFalse(st["armed"])
        self.assertEqual(st["state"], "error")
        self.assertIsNone(svc._error_timer)
        self.assertFalse(st["runtime"]["detector"])

    async def test_disabled_service_ignores_frames(self):
        await self._arm()
        await self.service.disarm()
        await self._wake_should_ignore()
        self.assertEqual(self.service.state, "disarmed")

    async def _wake_should_ignore(self):
        self.service.feed_frame(FRAME, now=0.0)
        self.assertEqual(self.transcribe_calls, [])

    # ---- 音乐口令快速通道 ----

    async def test_media_intent_runs_without_chat_or_thinking(self):
        self.text = "下一首"
        await self._arm()
        await self._wake()
        await self._speak_then_silence(4, 5)
        await self._drain()
        self.assertEqual(self.media_events.count("action:next"), 1)
        self.assertEqual(self.chat_calls, [])
        self.assertEqual(len(self.transcribe_calls), 1)
        self.assertEqual(self.service.state, "armed")
        self.assertNotIn("thinking", self.states)  # 不进思考态
        self.assertIn("speaking", self.states)     # 但要有反馈播报
        # 唤醒时压低一次，反馈播完恢复一次
        self.assertEqual(self.media_events.count("duck"), 1)
        self.assertEqual(self.media_events.count("restore"), 1)
        # 反馈是模块内的短词，不是模型回答
        self.assertEqual(self.ack_synth_calls[-1], "切了")

    async def test_media_volume_action_restores_duck_first(self):
        self.text = "音量调大"
        await self._arm()
        await self._wake()
        await self._speak_then_silence(4, 5)
        await self._drain()
        self.assertEqual(self.media_events.count("action:volume_up"), 1)
        events = self.media_events
        # 必须先恢复原音量再执行加减，否则恢复动作会覆盖用户要求的音量
        self.assertLess(events.index("restore"), events.index("action:volume_up"))
        self.assertEqual(events.count("restore"), 1)

    async def test_media_failure_degrades_and_next_round_works(self):
        self.text = "暂停"
        self.media_error = media.MediaError("没找到播放器", "没有播放器")
        await self._arm()
        await self._wake()
        await self._speak_then_silence(4, 5)
        await self._drain()
        self.assertEqual(self.chat_calls, [])
        self.assertEqual(self.service.state, "armed")
        self.assertIn("没找到播放器", self.service.status()["last_error"])
        self.assertEqual(self.ack_synth_calls[-1], "没有播放器")
        self.assertEqual(self.media_events.count("restore"), 1)
        # 播控失败不能影响下一轮主流程
        self.text = "把客厅灯打开"
        await self._wake()
        await self._speak_then_silence(4, 5)
        await self._drain()
        self.assertEqual(self.chat_calls, [("把客厅灯打开", True, "wake")])
        self.assertEqual(self.service.state, "listening")

    async def test_media_intent_miss_falls_back_to_chat(self):
        self.text = "今天天气怎么样"
        await self._arm()
        await self._wake()
        await self._speak_then_silence(4, 5)
        await self._drain()
        self.assertEqual(self.chat_calls, [("今天天气怎么样", True, "wake")])
        self.assertEqual([e for e in self.media_events if e.startswith("action:")], [])

    async def test_wake_timeout_restores_music(self):
        await self._arm()
        await self._wake()
        self.service.feed_frame(FRAME, now=self.service._wait_until + 0.1)
        self.assertEqual(self.service.state, "armed")
        self.assertEqual(self.media_events.count("duck"), 1)
        self.assertEqual(self.media_events.count("restore"), 1)

    async def test_stop_during_ack_restores_music(self):
        gate = asyncio.Event()
        self.ack_play_gate = gate
        await self._arm()
        self.detector.hit = True
        self.service.feed_frame(FRAME, now=self.service._clock())
        self.detector.hit = False
        self.assertEqual(self.service.state, "waking")
        await self.service.stop()
        await self._drain()
        self.assertEqual(self.service.state, "armed")
        self.assertEqual(self.media_events.count("restore"), 1)
        gate.set()

    async def test_music_duck_skipped_when_nothing_playing(self):
        self.media_duck_result = False
        await self._arm()
        await self._wake()
        await self._speak_then_silence(4, 5)
        await self._drain()
        # 没压成功就绝不能去「恢复」：那会把用户音量写成旧值
        self.assertGreaterEqual(self.media_events.count("duck"), 1)
        self.assertEqual(self.media_events.count("restore"), 0)

    async def test_chat_round_keeps_music_ducked_until_answer_ends(self):
        release = asyncio.Event()

        async def chat(text, force_tts=False, client=""):
            self.chat_calls.append((text, True, "wake"))
            # 回答播报期间音乐必须仍是压低的
            self.assertEqual(self.media_events.count("duck"), 1)
            self.assertEqual(self.media_events.count("restore"), 0)
            await release.wait()

        self.service.set_handlers(chat=chat)
        await self._arm()
        await self._wake()
        await self._speak_then_silence(4, 5)
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertEqual(self.media_events.count("restore"), 0)
        release.set()
        await self._drain()
        self.assertEqual(self.media_events.count("restore"), 1)
        self.assertEqual(self.service.state, "listening")

    # ---- 声控点播 ----

    async def test_song_intent_plays_without_chat(self):
        self.text = "放一首稻香"
        await self._arm()
        await self._wake()
        await self._speak_then_silence(4, 5)
        await self._drain()
        self.assertEqual(self.resolve_calls, ["稻香"])
        self.assertEqual(self.played_items, [{"id": "1001", "name": "稻香"}])
        self.assertEqual(self.chat_calls, [])
        self.assertNotIn("thinking", self.states)
        self.assertEqual(self.ack_synth_calls[-1], "放周杰伦的稻香")
        self.assertEqual(self.service.state, "armed")
        # 唤醒压一次、反馈念完恢复一次
        self.assertEqual(self.media_events.count("duck"), 1)
        self.assertEqual(self.media_events.count("restore"), 1)

    async def test_random_song_intent(self):
        self.text = "随机放一首"
        await self._arm()
        await self._wake()
        await self._speak_then_silence(4, 5)
        await self._drain()
        self.assertIn("random", self.media_events)
        self.assertEqual(self.played_items, [{"id": "1002", "name": "晴天"}])
        self.assertEqual(self.chat_calls, [])
        self.assertEqual(self.service.state, "armed")

    async def test_transcript_intent_and_plan_are_logged(self):
        events = []
        original = wake.devlog.log
        wake.devlog.log = lambda event, **fields: events.append((event, fields))
        self.addCleanup(setattr, wake.devlog, "log", original)
        self.text = "放一首稻香"
        await self._arm()
        await self._wake()
        await self._speak_then_silence(4, 5)
        await self._drain()
        logged = {}
        for event, fields in events:
            logged.setdefault(event, fields)
        self.assertEqual(logged["wake.transcribe"]["text"], self.text)
        self.assertEqual(logged["wake.intent"]["kind"], "song")
        self.assertEqual(logged["wake.intent"]["query"], "稻香")
        self.assertEqual(logged["wake.song.plan"]["status"], "ok")
        self.assertTrue(logged["wake.song.play"]["ok"])

    async def test_song_miss_speaks_and_returns_to_armed(self):
        self.song_plan = media.SongPlan("miss", "没找到那首歌")
        self.text = "放一首稻香"
        await self._arm()
        await self._wake()
        await self._speak_then_silence(4, 5)
        await self._drain()
        self.assertEqual(self.played_items, [])
        self.assertEqual(self.chat_calls, [])  # 不落到模型硬猜
        self.assertEqual(self.ack_synth_calls[-1], "没找到那首歌")
        self.assertEqual(self.service.state, "armed")

    async def test_song_ambiguous_asks_once_then_yes(self):
        self.song_plan = media.SongPlan("ask", "是周杰伦的稻香吗",
                                        {"id": "1001", "name": "稻香"},
                                        "稻香", "周杰伦")
        self.text = "来点周杰伦"
        await self._arm()
        await self._wake()
        await self._speak_then_silence(4, 5)
        await self._drain()
        # 问完之后回监听等回答，不能先放着
        self.assertEqual(self.service.state, "listening")
        self.assertEqual(self.played_items, [])
        self.assertEqual(self.ack_synth_calls[-1], "是周杰伦的稻香吗")
        self.assertIsNotNone(self.service._pending_confirm)
        # 回答「对」→ 直接放
        self.text = "对"
        await self._speak_then_silence(4, 5)
        await self._drain()
        self.assertEqual(self.played_items, [{"id": "1001", "name": "稻香"}])
        self.assertIsNone(self.service._pending_confirm)
        self.assertEqual(self.service.state, "armed")

    async def test_song_ambiguous_no_cancels(self):
        self.song_plan = media.SongPlan("ask", "是周杰伦的稻香吗",
                                        {"id": "1001", "name": "稻香"},
                                        "稻香", "周杰伦")
        self.text = "来点周杰伦"
        await self._arm()
        await self._wake()
        await self._speak_then_silence(4, 5)
        await self._drain()
        self.text = "不是"
        await self._speak_then_silence(4, 5)
        await self._drain()
        self.assertEqual(self.played_items, [])
        self.assertEqual(self.ack_synth_calls[-1], "那算了")
        self.assertIsNone(self.service._pending_confirm)
        self.assertEqual(self.service.state, "armed")

    async def test_confirm_is_asked_at_most_once(self):
        self.song_plan = media.SongPlan("ask", "是周杰伦的稻香吗",
                                        {"id": "1001", "name": "稻香"},
                                        "稻香", "周杰伦")
        self.text = "来点周杰伦"
        await self._arm()
        await self._wake()
        await self._speak_then_silence(4, 5)
        await self._drain()
        # 听不清的回答不能再追问一次，按普通输入交给模型
        self.text = "今天天气怎么样"
        await self._speak_then_silence(4, 5)
        await self._drain()
        self.assertEqual(self.chat_calls, [("今天天气怎么样", True, "wake")])
        self.assertIsNone(self.service._pending_confirm)
        self.assertEqual(self.resolve_calls, ["周杰伦"])

    async def test_song_play_failure_speaks_reason(self):
        self.play_error = media.MediaError("播放器报错", "没放出来")
        self.text = "放一首稻香"
        await self._arm()
        await self._wake()
        await self._speak_then_silence(4, 5)
        await self._drain()
        self.assertEqual(self.ack_synth_calls[-1], "没放出来")
        self.assertIn("播放器报错", self.service.status()["last_error"])
        self.assertEqual(self.service.state, "armed")

    async def test_song_search_failure_speaks_reason(self):
        self.song_plan = media.MediaError("曲库没登录", "查不到")
        self.text = "放一首稻香"
        await self._arm()
        await self._wake()
        await self._speak_then_silence(4, 5)
        await self._drain()
        self.assertEqual(self.played_items, [])
        self.assertEqual(self.ack_synth_calls[-1], "查不到")
        self.assertEqual(self.chat_calls, [])
        self.assertEqual(self.service.state, "armed")

    async def test_pending_confirm_cleared_on_listen_timeout(self):
        self.song_plan = media.SongPlan("ask", "是周杰伦的稻香吗",
                                        {"id": "1001", "name": "稻香"},
                                        "稻香", "周杰伦")
        self.text = "来点周杰伦"
        await self._arm()
        await self._wake()
        await self._speak_then_silence(4, 5)
        await self._drain()
        self.service.feed_frame(FRAME, now=self.service._wait_until + 0.1)
        self.assertEqual(self.service.state, "armed")
        self.assertIsNone(self.service._pending_confirm)
        self.assertEqual(self.media_events.count("restore"), 1)  # 本轮唤醒压过一次

    # ---- 配置/状态 ----

    async def test_status_has_no_transcript_and_maps_enable(self):
        self.service.apply({"enable": True, "wake_words": "你好梅朵"})
        st = self.service.status()
        self.assertTrue(st["enabled"])
        self.assertEqual(st["wake_words"], ["你好梅朵"])
        self.assertNotIn(self.text, repr(st))
        self.assertNotIn("transcript", st)

    async def test_arm_requires_enabled(self):
        self.service.apply({"enable": False})
        with self.assertRaises(wake.WakeError):
            await self.service.arm()

    async def test_detector_resets_stream_after_hit(self):
        """命中后必须 reset_stream：错了这个方法，KWS 会一直停在结果态，永远不再触发"""
        class FakeSpotter:
            def __init__(self):
                self.resets = 0

            def is_ready(self, stream):
                return False

            def get_result(self, stream):
                return "你好梅朵"

            def reset_stream(self, stream):
                self.resets += 1

        class FakeStream:
            def accept_waveform(self, rate, samples):
                pass

        import numpy as np

        det = wake.SherpaDetector(Path(self.tmp.name), ["你好梅朵"], 0.25, 8)
        det._spotter = FakeSpotter()
        det._stream = FakeStream()
        det._np = np
        self.assertEqual(det.accept(FRAME), "你好梅朵")
        self.assertEqual(det._spotter.resets, 1)


if __name__ == "__main__":
    unittest.main()
