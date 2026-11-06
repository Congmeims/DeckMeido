"""语音合成（piper-plus / espeak-ng 兜底）的回归测试。

这些用例全都不需要真实模型、音频设备或网络：只验证配置归一化、降级判定、
文本清洗/分语种、以及「新句打断旧句」的取消逻辑。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import tts as ttsmod  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class NormalizeEngineTest(unittest.TestCase):
    def test_已知引擎名统一成两种(self):
        self.assertEqual(ttsmod.normalize_engine("piper-plus"), "piper-plus")
        self.assertEqual(ttsmod.normalize_engine("piper_plus"), "piper-plus")
        self.assertEqual(ttsmod.normalize_engine("auto"), "piper-plus")
        self.assertEqual(ttsmod.normalize_engine("espeak-ng"), "espeak-ng")
        self.assertEqual(ttsmod.normalize_engine("espeak"), "espeak-ng")
        self.assertEqual(ttsmod.normalize_engine("spd-say"), "espeak-ng")

    def test_认不出来的值退回_espeak_避免彻底没声(self):
        self.assertEqual(ttsmod.normalize_engine("whatever"), "espeak-ng")
        self.assertEqual(ttsmod.normalize_engine(""), "espeak-ng")
        self.assertEqual(ttsmod.normalize_engine(None), "espeak-ng")


class ConfigParseTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_越界参数夹到合法区间(self):
        t = ttsmod.TTS(
            engine="bogus", speed=1, max_chars=0,
            speed_scale=9, noise_scale=-1, length_scale=99,
            language="xx", model_root=self.root,
        )
        self.assertEqual(t.engine, "espeak-ng")
        self.assertEqual(t.speed, 80)
        self.assertEqual(t.max_chars, 20)
        self.assertEqual(t.speed_scale, 2.0)
        self.assertEqual(t.noise_scale, 0.0)
        self.assertEqual(t.length_scale, 3.0)
        self.assertEqual(t.language, "auto")

    def test_apply_合并部分键且保持其余值(self):
        t = ttsmod.TTS(engine="piper-plus", voice="tsukuyomi",
                       speed=170, model_root=self.root)
        t.apply({"engine": "espeak-ng", "voice": "cmn", "speed": 200})
        self.assertEqual(t.engine, "espeak-ng")
        self.assertEqual(t.voice, "cmn")
        self.assertEqual(t.speed, 200)
        # 没传的键保持原值
        self.assertEqual(t.max_chars, 300)
        self.assertEqual(t.speed_scale, 1.0)

    def test_切换引擎释放已加载模型(self):
        t = ttsmod.TTS(engine="piper-plus", model_root=self.root)
        t._voice = object()
        t.apply({"engine": "espeak-ng"})
        self.assertIsNone(t._voice)


class FallbackTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self._min = ttsmod.MIN_ONNX_BYTES
        ttsmod.MIN_ONNX_BYTES = 10

    def tearDown(self):
        ttsmod.MIN_ONNX_BYTES = self._min
        self._td.cleanup()

    def _make_model(self, onnx_size=32):
        (self.root / ttsmod.ONNX_NAME).write_bytes(b"\0" * onnx_size)
        (self.root / ttsmod.CONFIG_NAME).write_text(
            json.dumps({"language_id_map": {"ja": 0, "zh": 1, "en": 2}}),
            encoding="utf-8")

    def test_运行时缺失时退回_espeak_并给出原因(self):
        t = ttsmod.TTS(engine="piper-plus", model_root=self.root)
        t._runtime = False
        state = t.status()
        self.assertEqual(state["active_engine"], "espeak-ng")
        self.assertFalse(state["model"])
        self.assertIn("piper-plus", state["reason"])

    def test_模型缺失时说明未下载(self):
        t = ttsmod.TTS(engine="piper-plus", model_root=self.root)
        t._runtime = True
        state = t.status()
        self.assertEqual(state["active_engine"], "espeak-ng")
        self.assertFalse(state["model"])
        self.assertIn("模型未下载", state["reason"])

    def test_运行时与模型都在时用_piper(self):
        self._make_model()
        t = ttsmod.TTS(engine="piper-plus", model_root=self.root)
        t._runtime = True
        state = t.status()
        self.assertEqual(state["active_engine"], "piper-plus")
        self.assertTrue(state["model"])
        self.assertEqual(state["reason"], "")
        self.assertTrue(state["ready"])

    def test_模型文件太小视为未下载(self):
        self._make_model(onnx_size=5)
        t = ttsmod.TTS(engine="piper-plus", model_root=self.root)
        t._runtime = True
        self.assertFalse(t.model_ready())
        self.assertEqual(t.status()["active_engine"], "espeak-ng")

    def test_配置非法时校验抛出可读错误(self):
        (self.root / ttsmod.ONNX_NAME).write_bytes(b"\0" * 32)
        (self.root / ttsmod.CONFIG_NAME).write_text("not json", encoding="utf-8")
        t = ttsmod.TTS(model_root=self.root)
        with self.assertRaises(ttsmod.TTSError) as ctx:
            t._validate_files(self.root)
        self.assertEqual(ctx.exception.kind, "model")
        self.assertIn("config.json", str(ctx.exception))

    def test_下载源顺序与自定义地址(self):
        auto = ttsmod.TTS(model_root=self.root, model_source="auto")
        self.assertEqual([s["name"] for s in auto._source_defs()], ["mirror", "hf"])

        mirror = ttsmod.TTS(model_root=self.root, model_source="mirror")
        names = [s["name"] for s in mirror._source_defs()]
        self.assertEqual(names, ["mirror"])
        self.assertIn("hf-mirror.com", list(mirror._source_defs()[0]["files"].values())[0])

        hf = ttsmod.TTS(model_root=self.root, model_source="hf")
        self.assertEqual([s["name"] for s in hf._source_defs()], ["hf"])

        custom = ttsmod.TTS(model_root=self.root, model_source="https://example.com/base/")
        defs = custom._source_defs()
        self.assertEqual(defs[0]["name"], "custom")
        self.assertTrue(
            all(url.startswith("https://example.com/base/") for url in defs[0]["files"].values()))


class ReadifyRouteTest(unittest.TestCase):
    def test_清洗代码块_url_与_markdown_标记(self):
        text = "看这个 ```print(1)``` 还有 `cmd`，地址 https://a.b/c ！"
        out = ttsmod.readify(text)
        self.assertNotIn("```", out)
        self.assertNotIn("http", out)
        self.assertNotIn("`", out)

    def test_分语种(self):
        self.assertEqual(ttsmod.detect_language("こんにちは"), "ja")
        self.assertEqual(ttsmod.detect_language("你好世界"), "zh")
        self.assertEqual(ttsmod.detect_language("Hello world"), "en")

    def test_句子里假名优先判为日语(self):
        segs = ttsmod.route_segments("今日は晴れです。", "auto")
        self.assertEqual(segs, [("ja", "今日は晴れです。")])

    def test_中英混排按脚本切开(self):
        segs = ttsmod.route_segments("你好Hello世界。", "auto")
        langs = [lang for lang, _ in segs]
        self.assertIn("zh", langs)
        self.assertIn("en", langs)
        self.assertTrue(all(text.strip() for _, text in segs))

    def test_强制语言覆盖自动判定(self):
        segs = ttsmod.route_segments("你好世界。", "en")
        self.assertEqual(segs, [("en", "你好世界。")])

    def test_超长段落按上限切开(self):
        segs = ttsmod.route_segments("a" * 50, "auto", limit=20)
        self.assertTrue(len(segs) >= 3)
        self.assertTrue(all(len(text) <= 20 for _, text in segs))


class _FakeProc:
    returncode = None

    def __init__(self):
        self.terminated = False
        self.killed = False
        self.returncode = None

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


class InterruptTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def test_stop_终止正在播放的进程(self):
        t = ttsmod.TTS(enable=True, engine="espeak-ng", model_root=self.root)
        proc = _FakeProc()
        t._proc = proc
        _run(t.stop())
        self.assertTrue(proc.terminated)
        self.assertIsNone(t._proc)

    def test_新句打断旧句(self):
        t = ttsmod.TTS(enable=True, engine="espeak-ng", model_root=self.root)
        started = asyncio.Event()
        calls = []

        async def fake_espeak(text, event):
            calls.append(text)
            started.set()
            if len(calls) == 1:
                # 第一句模拟长播报：只有被新句打断（event 置位）才会结束
                while not event.is_set():
                    await asyncio.sleep(0.01)
                return
            # 后面的句子模拟自然念完
            await asyncio.sleep(0.1)

        t._speak_espeak = fake_espeak
        t._espeak_binary = lambda: "/usr/bin/espeak-ng"

        async def scenario():
            first = asyncio.create_task(t.speak("第一句。"))
            await asyncio.wait_for(started.wait(), 2)
            second = asyncio.create_task(t.speak("第二句。"))
            await asyncio.wait_for(asyncio.gather(first, second), 3)
            return first, second

        first, second = _run(scenario())
        self.assertTrue(first.done())
        self.assertTrue(second.done())
        self.assertGreaterEqual(len(calls), 2)

    def test_关闭时不出声_force_仍可试听(self):
        t = ttsmod.TTS(enable=False, engine="espeak-ng", model_root=self.root)
        calls = []

        async def fake_espeak(text, event):
            calls.append(text)

        t._speak_espeak = fake_espeak
        _run(t.speak("不该出声。"))
        self.assertEqual(calls, [])
        _run(t.preview("试听一句。"))
        self.assertEqual(calls, ["试听一句。"])


if __name__ == "__main__":
    unittest.main()
