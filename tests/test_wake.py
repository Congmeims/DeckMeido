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

from server import wake  # noqa: E402


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
        self.text = "把客厅灯打开"

        def duck():
            self.duck_calls += 1

        def restore():
            self.restore_calls += 1

        def transcribe(wav):
            self.transcribe_calls.append(wav)
            return self.text

        async def chat(text, force_tts=False, client=""):
            self.chat_calls.append((text, force_tts, client))

        self.chat_fn = chat
        self.service = wake.WakeService(
            model_root=Path(self.tmp.name),
            detector_factory=lambda root, cfg: self.detector,
            capture_factory=lambda: None,
            transcribe=transcribe,
            chat=chat,
            duck=duck,
            restore=restore,
        )
        self.service.set_tts_stop(self._tts_stop)
        self.service.set_handlers(cancel_chat=self._cancel_chat)
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

    async def _drain(self):
        for _ in range(8):
            await asyncio.sleep(0)
            task = self.service._task
            if task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=0.2)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass

    async def _arm(self):
        await self.service.arm()
        self.assertEqual(self.service.state, "armed")
        self.assertTrue(self.detector.loaded)

    async def _wake(self):
        self.detector.hit = True
        self.service.feed_frame(FRAME, now=0.0)
        self.detector.hit = False
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
        for i in range(14):  # 2.24s > wake_timeout_sec
            self.service.feed_frame(FRAME, now=i * 0.16)
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

    # ---- 状态与异常 ----

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
