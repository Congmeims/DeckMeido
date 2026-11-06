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

    def test_评分星号映射成可读词而不是被删掉(self):
        raw = "这是日服 v2.1.7 的立绘，角色 3★ 版本，CV 是 石川由依。"
        cleaned = ttsmod.readify(raw)
        # 只做 ★→星 的映射，其它字符一个都不能少
        self.assertEqual(cleaned, raw.replace("★", "星"))
        segs = ttsmod.route_segments(cleaned, "auto")
        self.assertIn("3星", "".join(part for _, part in segs))
        self.assertEqual(ttsmod.readify("★☆"), "星星")

    def test_分语种(self):
        self.assertEqual(ttsmod.detect_language("こんにちは"), "ja")
        self.assertEqual(ttsmod.detect_language("你好世界"), "zh")
        self.assertEqual(ttsmod.detect_language("Hello world"), "en")

    def test_日语句子里的汉字贴着假名按日语读(self):
        segs = ttsmod.route_segments("今日は晴れです。", "auto")
        self.assertTrue(all(lang == "ja" for lang, _ in segs))
        self.assertEqual("".join(text for _, text in segs), "今日は晴れです。")

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
        self.assertEqual("".join(text for _, text in segs), "a" * 50)


class ScriptRouteTest(unittest.TestCase):
    """逐段混读：每句都按脚本切开，语言标注与拼接无损"""

    def assertRoute(self, text, expected, language="auto"):
        segs = ttsmod.route_segments(text, language)
        self.assertEqual(segs, expected)
        # 无损：拼接后与去掉首尾空白的原句逐字一致
        self.assertEqual("".join(part for _, part in segs), text.strip())

    def test_中英混排(self):
        self.assertRoute("你好，world！", [("zh", "你好，"), ("en", "world！")])

    def test_中文加日语假名(self):
        # 汉字贴着假名 → 按日语读；同语言相邻片段会合并，避免把「你好」「です」拆成两次合成
        self.assertRoute("你好です", [("ja", "你好です")])

    def test_日语汉字与假名交替(self):
        self.assertRoute("私は学生です。", [("ja", "私は学生です。")])

    def test_纯汉字句子按中文读(self):
        self.assertRoute("日本語", [("zh", "日本語")])

    def test_数字跟相邻语言且绝不丢(self):
        self.assertRoute(
            "你好 123 world",
            [("zh", "你好 123 "), ("en", "world")])

    def test_引号里的日语与后面的中文(self):
        # 引号内单独按日语读，引号外的中文说明不再被拖成日语
        self.assertRoute(
            "「こんにちは」是你好的意思",
            [("ja", "「こんにちは」"), ("zh", "是你好的意思")])

    def test_拉丁占绝对多数整句按英文读(self):
        self.assertRoute(
            "Elegant Elegy is a skill.", [("en", "Elegant Elegy is a skill.")])

    def test_括号未闭合与嵌套不丢字符(self):
        for text in ("「ミカは学生です", "（「Elegant」）", "」多余的右括号"):
            segs = ttsmod.route_segments(text, "auto")
            self.assertEqual("".join(part for _, part in segs), text)

    def test_英文缩写与版本号整体保留(self):
        self.assertRoute("GPU", [("en", "GPU")])
        self.assertRoute("v0.7.0", [("en", "v0.7.0")])

    def test_纯符号与空句不合成(self):
        self.assertEqual(ttsmod.route_segments("！？…", "auto"), [])
        self.assertEqual(ttsmod.route_segments("   ", "auto"), [])
        self.assertEqual(ttsmod.route_segments("", "auto"), [])

    def test_固定语言优先不拆分(self):
        self.assertRoute("你好です。", [("en", "你好です。")], language="en")

    def test_多句各自切段仍无损(self):
        text = "你好，world！今日は晴れです。"
        segs = ttsmod.route_segments(text, "auto")
        self.assertEqual([lang for lang, _ in segs], ["zh", "en", "ja"])
        self.assertEqual("".join(part for _, part in segs), text)


class PersonaRouteTest(unittest.TestCase):
    """蔚蓝档案日服角色资料：中文说明为主体，夹日文专名/台词/英文术语"""

    def assertRoute(self, text, expected, cleaned=None):
        if cleaned is not None:
            text = ttsmod.readify(text)
            self.assertEqual(text, cleaned)
        segs = ttsmod.route_segments(text, "auto")
        self.assertEqual(segs, expected)
        self.assertEqual("".join(part for _, part in segs), text.strip())

    def test_中文句里有片假名专名(self):
        # 句首ミカ是片假名，整句仍判中文；假名段读 ja，其余中文读 zh
        self.assertRoute(
            "ミカ是崔尼蒂综合学园的学生会长，喜欢甜点，武器是MG。",
            [("ja", "ミカ"),
             ("zh", "是崔尼蒂综合学园的学生会长，喜欢甜点，武器是"),
             ("en", "MG。")])

    def test_引号台词单独按日语读(self):
        self.assertRoute(
            "她的口头禅是「甘いものは正義です」，意思是甜食就是正义。",
            [("zh", "她的口头禅是"),
             ("ja", "「甘いものは正義です」"),
             ("zh", "，意思是甜食就是正义。")])

    def test_英文技能名与缩写(self):
        self.assertRoute(
            "技能「Elegant Elegy」会对范围内敌人造成伤害，简称EX。",
            [("zh", "技能"),
             ("en", "「Elegant Elegy」"),
             ("zh", "会对范围内敌人造成伤害，简称"),
             ("en", "EX。")])

    def test_中文里的片假名学校名(self):
        self.assertRoute(
            "她住在トリニティ的宿舍，喜欢红茶和蛋糕。",
            [("zh", "她住在"),
             ("ja", "トリニティ"),
             ("zh", "的宿舍，喜欢红茶和蛋糕。")])

    def test_片假名专名开头的中文句仍按中文读(self):
        # 两个ミカ让假名占比刚好超过 0.30，但没有平假名，判 zh 才是对的
        self.assertRoute(
            "她喜欢ミカ，但ミカ是学生会长。",
            [("zh", "她喜欢"), ("ja", "ミカ，"),
             ("zh", "但"), ("ja", "ミカ"), ("zh", "是学生会长。")])
        self.assertRoute("ミカ的设定：", [("ja", "ミカ"), ("zh", "的设定：")])

    def test_纯日文整句按日语读(self):
        self.assertRoute(
            "ミカはトリニティ総合学園の生徒会長で、甘いものが大好きです。",
            [("ja", "ミカはトリニティ総合学園の生徒会長で、甘いものが大好きです。")])

    def test_版本号星级与日文汉字人名(self):
        # 石川由依是纯汉字日文人名，没有假名也没有词典 → 仍读中文（已知限制）
        self.assertRoute(
            "这是日服 v2.1.7 的立绘，角色 3★ 版本，CV 是 石川由依。",
            [("zh", "这是日服 "),
             ("en", "v2.1.7 "),
             ("zh", "的立绘，角色 3星 版本，"),
             ("en", "CV "),
             ("zh", "是 石川由依。")],
            cleaned="这是日服 v2.1.7 的立绘，角色 3星 版本，CV 是 石川由依。")


class MixedLanguageRegressionTest(unittest.TestCase):
    """中英日混排回归：整句主语言不被几个技术词带跑，引号内独立语言不丢。"""

    def route(self, text, **kwargs):
        return ttsmod.route_segments(text, "auto", **kwargs)

    def test_技术词嵌在中文句里不整句换英文(self):
        # 旧版按拉丁字符占比 0.75 判：这种句子会被判成 en，整句换语言
        segs = self.route("把 API 和 GPU 的配置写进 README 再跑一遍。")
        self.assertEqual(segs[0][0], "zh")
        self.assertEqual(segs[-1][0], "zh")
        # 英文词本身仍按 en 送，保证发音；但前后中文留在 zh
        self.assertIn(("en", "API "), segs)
        self.assertEqual("".join(t for _, t in segs),
                         "把 API 和 GPU 的配置写进 README 再跑一遍。")

    def test_蔚蓝档案角色资料混排(self):
        text = "蔚蓝档案的Mika（ミカ）是崔尼蒂综合学园的学生会长，喜欢甜点。"
        segs = self.route(text)
        langs = [lang for lang, _ in segs]
        self.assertEqual(langs[0], "zh")
        self.assertIn("ja", langs)   # 括号里的片假名专名单独按日语
        self.assertIn("en", langs)   # Mika 按英文读
        self.assertEqual("".join(t for _, t in segs), text)

    def test_翻译文档的中文说明加日文原文(self):
        text = "原文「ミカは学生です」的意思是「Mika is a student」。"
        segs = self.route(text)
        self.assertEqual(segs[0], ("zh", "原文"))
        self.assertEqual(segs[1], ("ja", "「ミカは学生です」"))
        self.assertIn(("zh", "的意思是"), segs)
        # 句末「。」按既有规则并入前一句，随英文段一起送
        self.assertIn(("en", "「Mika is a student」。"), segs)
        self.assertEqual("".join(t for _, t in segs), text)

    def test_技术文档英文术语与路径(self):
        text = "在 config.toml 里设置 port = 18765，然后重启后端。"
        segs = self.route(text)
        self.assertEqual(segs[0][0], "zh")
        self.assertEqual(segs[-1][0], "zh")
        self.assertEqual("".join(t for _, t in segs), text)

    def test_括号内独立语言边界(self):
        text = "他说「OK，thanks！」然后走了。"
        segs = self.route(text)
        self.assertEqual(segs[0], ("zh", "他说"))
        # 既有句切规则先按「！」断开，闭合引号随下一段中文走；语言归属判对了
        self.assertEqual(segs[1], ("en", "「OK，thanks！"))
        self.assertEqual(segs[-1], ("zh", "」然后走了。"))
        self.assertEqual("".join(t for _, t in segs), text)

    def test_整句英文仍判英文(self):
        self.assertEqual(self.route("Elegant Elegy is a skill."),
                         [("en", "Elegant Elegy is a skill.")])

    def test_日文句里的汉字全部按日语读(self):
        segs = self.route("石川由依は声優です。")
        self.assertTrue(all(lang == "ja" for lang, _ in segs))
        self.assertEqual("".join(t for _, t in segs), "石川由依は声優です。")

    def test_可选日文汉字词表(self):
        # 无词表：纯汉字日文人名按中文读（已知限制）
        self.assertEqual(self.route("石川由依"), [("zh", "石川由依")])
        # 词表命中：整词按日语读
        self.assertEqual(
            self.route("石川由依", ja_terms=frozenset({"石川由依"})),
            [("ja", "石川由依")])


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


class SegmentFallbackTest(unittest.TestCase):
    """个别片段失败只跳过该段；只有全部失败才整段退回 espeak"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def _tts(self):
        t = ttsmod.TTS(enable=True, engine="piper-plus", model_root=self.root)
        t._piper_ready = lambda: True
        t._espeak_binary = lambda: "/usr/bin/espeak-ng"
        return t

    def test_单段失败不重念整句(self):
        t = self._tts()
        played = []
        espeak_calls = []

        def synth(text, language):
            if "bad" in text:
                raise ttsmod.TTSError("合成炸了", kind="synth")
            p = self.root / f"{len(played)}.wav"
            p.write_bytes(b"RIFF")
            return p

        async def play(wav, event):
            played.append(wav)

        async def espeak(text, event):
            espeak_calls.append(text)

        t._synth_wav = synth
        t._play = play
        t._speak_espeak = espeak
        _run(t.speak("好 bad 坏翻译。"))
        self.assertEqual(espeak_calls, [])
        self.assertEqual(len(played), 2)  # 其余两段照常念完
        self.assertEqual(t.status()["failed_segments"], 1)
        self.assertIn("1/3", t._last_error)
        self.assertIn("合成炸了", t._last_error)

    def test_全部失败才整段退回_espeak(self):
        t = self._tts()
        espeak_calls = []

        def synth(text, language):
            raise ttsmod.TTSError("合成炸了", kind="synth")

        async def espeak(text, event):
            espeak_calls.append(text)

        t._synth_wav = synth
        t._speak_espeak = espeak
        _run(t.speak("好 bad 坏翻译。"))
        self.assertEqual(len(espeak_calls), 1)
        self.assertEqual(t.status()["failed_segments"], 3)
        self.assertIn("全部 3 段", t._last_error)


if __name__ == "__main__":
    unittest.main()
