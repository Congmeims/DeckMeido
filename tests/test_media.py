"""媒体播控：意图词表、动作白名单、duck/restore 配对、失败降级。

全部用假 busctl（替换 media._run），不碰真实 D-Bus，也不会出声。
"""
from __future__ import annotations

import json
import subprocess
import sys
import types
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
                self.assertEqual(got.kind, "transport")
                self.assertEqual(got.action, expect)
                self.assertTrue(got.feedback)  # 反馈词不能为空

    def test_intent_priority(self):
        # 「停止播放」不能被「播放」抢走；「取消静音」必须优先于「静音」
        self.assertEqual(media.match_intent("停止播放").action, "stop")
        self.assertEqual(media.match_intent("取消静音").action, "unmute")
        self.assertEqual(media.match_intent("解除静音").action, "unmute")

    def test_intent_with_punctuation_and_spaces(self):
        self.assertEqual(media.match_intent(" 下一首！ ").action, "next")
        self.assertEqual(media.match_intent("音量，调大").action, "volume_up")

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


class FakeLoader:
    """按方法名回放的假 Decky Loader RPC，记录每次调用参数。"""

    def __init__(self, handlers=None):
        self.handlers = handlers or {}
        self.calls = []

    def __call__(self, method, *args):
        self.calls.append((method, args))
        value = self.handlers.get(method)
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value(*args)
        if value is None:
            return {}
        return value

    def methods(self):
        return [m for m, _ in self.calls]


class FakePlayback:
    """get_playback 的真相源：queue_play / play_queue 会真的改它。"""

    def __init__(self, index=3, mode="normal", playing=False, current=None,
                 insert_error=False):
        self.index = index
        self.mode = mode
        self.playing = playing
        self.current = current
        self.pending = None
        self.insert_error = insert_error

    def handlers(self):
        return {
            "get_playback": lambda *_: self.snapshot(),
            "queue_insert_next": self.insert,
            "queue_play": self.play_index,
            "play_queue": self.play_queue,
        }

    def snapshot(self):
        return {
            "index": self.index,
            "queue_mode": self.mode,
            "playing": self.playing,
            "current": {"id": self.current} if self.current else None,
        }

    def insert(self, item):
        if self.insert_error:
            raise media.MediaError("insert failed")
        self.pending = str(item.get("id"))
        return None

    def play_index(self, index):
        self.index = int(index)
        if self.pending is not None:
            self.current = self.pending
            self.pending = None
        self.playing = True
        return None

    def play_queue(self, items, start_index=0):
        self.current = str(items[start_index].get("id"))
        self.index = int(start_index)
        self.playing = True
        return None


class FakeSocket:
    """内存里的单条 Loader WS 连接。"""

    def __init__(self, responder, sent):
        self.responder = responder
        self.sent = sent
        self._inbox = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send(self, payload):
        request = json.loads(payload)
        self.sent.append(request)
        self._inbox = [json.dumps(msg, ensure_ascii=False)
                       for msg in self.responder(request)]

    def recv(self, timeout=None):
        if not self._inbox:
            raise TimeoutError("没有更多消息")
        return self._inbox.pop(0)


def _install_fake_ws(testcase, responder):
    """把 `websockets.sync.client.connect` 换成内存实现，返回收到的请求列表。"""
    sent = []

    def connect(url, **kwargs):
        return FakeSocket(responder, sent)

    try:
        import websockets.sync.client as client
    except Exception:  # 没装 websockets：塞一个最小替身模块
        modules = {
            "websockets": types.ModuleType("websockets"),
            "websockets.sync": types.ModuleType("websockets.sync"),
            "websockets.sync.client": types.ModuleType("websockets.sync.client"),
        }
        modules["websockets"].sync = modules["websockets.sync"]
        modules["websockets.sync"].client = modules["websockets.sync.client"]
        modules["websockets.sync.client"].connect = connect
        saved = {name: sys.modules.get(name) for name in modules}
        sys.modules.update(modules)

        def restore():
            for name, old in saved.items():
                if old is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = old

        testcase.addCleanup(restore)
    else:
        original = client.connect
        client.connect = connect
        testcase.addCleanup(setattr, client, "connect", original)

    original_token = media._loader_token
    media._loader_token = lambda: "test-token"
    testcase.addCleanup(setattr, media, "_loader_token", original_token)
    return sent


SONGS = [
    {"mid": "1001", "name": "稻香", "singer": "周杰伦", "album": "魔杰座",
     "duration": 221, "cover": "c1", "media_mid": "1001", "max_quality": "lossless"},
    {"mid": "1002", "name": "晴天", "singer": "周杰伦", "album": "叶惠美",
     "duration": 269, "cover": "c2", "media_mid": "1002", "max_quality": "lossless"},
    {"mid": "1003", "name": "Slam Dunk", "singer": "MerrinZephyr",
     "album": "SLAPHOUSE", "duration": 180, "cover": "c3",
     "media_mid": "1003", "max_quality": "none"},
]


def _search_all(*_args):
    return {"ok": True, "songs": list(SONGS)}


class SongTests(unittest.TestCase):
    def setUp(self):
        self._orig_rpc = media._rpc
        self.loader = FakeLoader({"search_songs": _search_all,
                                  "get_playback": {"index": 3, "queue_mode": "normal"},
                                  "get_queue": {"items": [
                                      {"id": "1", "name": "A", "singer": "甲"},
                                      {"id": "2", "name": "B", "singer": "乙"},
                                  ]}})
        media._rpc = self.loader
        media._reset()

    def tearDown(self):
        media._rpc = self._orig_rpc
        media._reset()

    # ---- 意图抽取：点播 ----

    def test_song_intents_extract_query(self):
        cases = {
            "放一首周杰伦的稻香": ("周杰伦", "稻香"),
            "放周杰伦的《稻香》": ("稻香",),
            "放《稻香》": ("稻香",),
            "播放稻香": ("稻香",),
            "来点周杰伦": ("周杰伦",),
            "放陈奕迅的歌": ("陈奕迅",),
            "听首晴天吧": ("晴天",),
        }
        for text, expects in cases.items():
            with self.subTest(text=text):
                intent = media.match_intent(text)
                self.assertIsNotNone(intent)
                self.assertEqual(intent.kind, "song")
                for word in expects:
                    self.assertIn(word, intent.query)

    def test_random_intents(self):
        for text in ("随机放一首", "随便放点", "随便来一首", "随机播放"):
            with self.subTest(text=text):
                intent = media.match_intent(text)
                self.assertIsNotNone(intent)
                self.assertEqual(intent.kind, "random")

    def test_song_intent_does_not_steal_transport(self):
        # 「继续播放」不能被抽成歌名「放」
        self.assertEqual(media.match_intent("继续播放").action, "play")
        self.assertEqual(media.match_intent("播放").action, "play")
        # 泛称不算点播，交给模型
        for text in ("放音乐", "来点歌", "放一首歌", "随便"):
            with self.subTest(text=text):
                intent = media.match_intent(text)
                self.assertTrue(intent is None or intent.kind != "song")

    def test_song_questions_go_to_model(self):
        for text in ("放的是哪首歌", "稻香是谁唱的", "下一首是什么歌"):
            with self.subTest(text=text):
                self.assertIsNone(media.match_intent(text))

    # ---- 匹配分数 ----

    def test_pinyin_tolerates_wrong_or_similar_chars(self):
        # 错别字 / 同音字走拼音，分数必须过阈值
        self.assertGreaterEqual(media.score_song("道香", SONGS[0]), media.MATCH_THRESHOLD)
        self.assertGreaterEqual(media.score_song("周杰论", SONGS[0]), media.MATCH_THRESHOLD)

    def test_english_is_case_and_space_insensitive(self):
        self.assertGreaterEqual(media.score_song("slamdunk", SONGS[2]),
                                media.MATCH_THRESHOLD)
        self.assertGreaterEqual(media.score_song("SLAM DUNK", SONGS[2]),
                                media.MATCH_THRESHOLD)

    def test_unrelated_query_scores_low(self):
        self.assertLess(media.score_song("晴天", {"name": "稻香", "singer": "周杰伦"}),
                        media.MATCH_THRESHOLD)

    # ---- 解析 ----

    def test_resolve_unique_top_plays_directly(self):
        plan = media.resolve_song("道香")
        self.assertEqual(plan.status, "ok")
        self.assertEqual(plan.name, "稻香")
        self.assertIn("放", plan.speech)
        self.assertIn("周杰伦", plan.speech)
        self.assertEqual(plan.item["id"], "1001")

    def test_resolve_close_scores_ask_once(self):
        # 两个周杰伦的歌同分：不同歌名，不能随便挑，先确认
        plan = media.resolve_song("周杰伦")
        self.assertEqual(plan.status, "ask")
        self.assertIn("吗", plan.speech)
        self.assertIn(plan.name, plan.speech)

    def test_same_title_covers_do_not_ask(self):
        # 同名翻唱分数一样高，但歌名相同：直接放最高分，反馈带歌手名
        covers = [
            {"mid": "1", "name": "稻香", "singer": "甲", "album": ""},
            {"mid": "2", "name": "稻香", "singer": "乙", "album": ""},
            {"mid": "3", "name": "稻香 (女声版)", "singer": "丙", "album": ""},
        ]
        self.loader.handlers["search_songs"] = lambda *_: {"ok": True, "songs": covers}
        plan = media.resolve_song("稻香")
        self.assertEqual(plan.status, "ok")
        self.assertIn(plan.singer, plan.speech)
        self.assertEqual(plan.item["id"], "1")

    def test_resolve_miss_is_spoken(self):
        def only_unrelated(*_args):
            return {"ok": True, "songs": [{"mid": "9", "name": "晴天",
                                           "singer": "周杰伦", "album": "叶惠美"}]}
        self.loader.handlers["search_songs"] = only_unrelated
        plan = media.resolve_song("不存在的歌")
        self.assertEqual(plan.status, "miss")
        self.assertIn("没找到", plan.speech)

    def test_resolve_falls_back_to_split_tokens(self):
        def by_token(keyword, page=0):
            if keyword == "周杰伦的稻香":
                return {"ok": True, "songs": []}
            if keyword == "稻香":
                return {"ok": True, "songs": [SONGS[0]]}
            return {"ok": True, "songs": []}
        self.loader.handlers["search_songs"] = by_token
        plan = media.resolve_song("周杰伦的稻香")
        self.assertEqual(plan.status, "ok")
        self.assertEqual(plan.name, "稻香")

    def test_search_error_is_readable(self):
        self.loader.handlers["search_songs"] = {"ok": False, "error": "not_login"}
        with self.assertRaises(media.MediaError) as ctx:
            media.search_songs("稻香")
        self.assertIn("没登录", str(ctx.exception))
        self.assertEqual(ctx.exception.speech, "查不到")

    # ---- 随机 ----

    def test_random_excludes_current_track(self):
        self.loader.handlers["get_playback"] = {"index": 0, "queue_mode": "normal"}
        plan = media.resolve_random()
        self.assertEqual(plan.status, "ok")
        self.assertEqual(plan.item["id"], "2")  # 当前是第 0 首，只能挑第 1 首

    def test_random_empty_queue_is_readable(self):
        self.loader.handlers["get_queue"] = {"items": []}
        with self.assertRaises(media.MediaError) as ctx:
            media.resolve_random()
        self.assertIn("曲库是空的", str(ctx.exception))
        self.assertEqual(ctx.exception.speech, "曲库空的")

    # ---- 播放计划 ----

    def test_play_plan_keeps_queue_in_normal_mode(self):
        self.loader.handlers.update(FakePlayback(index=3, current="3").handlers())
        media.play_plan({"id": "1001", "name": "稻香"})
        self.assertEqual(self.loader.methods(),
                         ["get_playback", "queue_insert_next", "queue_play",
                          "get_playback"])
        self.assertEqual(self.loader.calls[2][1], (4,))

    def test_play_plan_restarts_queue_in_radio_mode(self):
        self.loader.handlers.update(
            FakePlayback(index=0, mode="radio", current="9").handlers())
        media.play_plan({"id": "1002", "name": "晴天"})
        self.assertEqual(self.loader.methods(),
                         ["get_playback", "play_queue", "get_playback"])
        self.assertEqual(self.loader.calls[1][1][1], 0)

    def test_play_plan_rejects_incomplete_item(self):
        with self.assertRaises(media.MediaError):
            media.play_plan({"name": "稻香"})
        self.assertEqual(self.loader.calls, [])

    def test_play_plan_reports_when_playback_did_not_start(self):
        # 插件接受了调用但没开播（音源失败）：不能当成功回去报「放稻香」
        fake = FakePlayback(index=3, current="3")
        fake.play_index = lambda index: setattr(fake, "index", int(index))
        self.loader.handlers.update(fake.handlers())
        with self.assertRaises(media.MediaError) as ctx:
            media.play_plan({"id": "1001", "name": "稻香"})
        self.assertEqual(ctx.exception.speech, "没放出来")

    def test_play_plan_rebuild_path_also_verifies(self):
        # 插入失败退回 play_queue：同样要回读，播放没起来就报错
        fake = FakePlayback(index=3, mode="normal", current="3",
                            insert_error=True)
        fake.play_queue = lambda items, start_index=0: setattr(
            fake, "current", str(items[0].get("id")))
        self.loader.handlers.update(fake.handlers())
        with self.assertRaises(media.MediaError) as ctx:
            media.play_plan({"id": "1001", "name": "稻香"})
        self.assertEqual(ctx.exception.speech, "没放出来")
        self.assertEqual(self.loader.methods(),
                         ["get_playback", "queue_insert_next", "play_queue",
                          "get_playback"])

    # ---- RPC 唯一 id ----

    def test_rpc_ids_are_unique_per_request(self):
        sent = _install_fake_ws(
            self, lambda req: [{"type": 1, "id": req["id"], "result": {}}])
        media._rpc = self._orig_rpc
        media._rpc("get_queue")
        media._rpc("get_playback")
        self.assertEqual(len(sent), 2)
        self.assertNotEqual(sent[0]["id"], sent[1]["id"])

    def test_rpc_ignores_previous_response_with_stale_id(self):
        def responder(req):
            return [
                {"type": 1, "id": req["id"] - 1, "result": {"stale": True}},
                {"type": 1, "id": req["id"], "result": {"ok": True}},
            ]
        _install_fake_ws(self, responder)
        media._rpc = self._orig_rpc
        result = media._rpc("get_queue")
        self.assertEqual(result, {"ok": True})

    # ---- 打点 ----

    def test_resolve_logs_chain_fields(self):
        events = []
        original = media.devlog.log
        media.devlog.log = lambda event, **fields: events.append((event, fields))
        self.addCleanup(setattr, media.devlog, "log", original)
        media.resolve_song("稻香")
        logged = {}
        for event, fields in events:
            logged.setdefault(event, fields)
        self.assertEqual(logged["media.song.search"]["count"], len(SONGS))
        self.assertEqual(logged["media.song.search"]["threshold"],
                         media.MATCH_THRESHOLD)
        resolve = logged["media.song.resolve"]
        self.assertEqual(resolve["status"], "ok")
        self.assertEqual(resolve["name"], "稻香")
        self.assertGreaterEqual(resolve["top_score"], media.MATCH_THRESHOLD)

    # ---- RPC 白名单 ----

    def test_rpc_method_whitelist(self):
        # 用真实 _rpc 验证白名单：不该连 Loader，也不该执行任何东西
        media._rpc = self._orig_rpc
        with self.assertRaises(media.MediaError):
            media._rpc("get_file_contents", "/etc/passwd")
        with self.assertRaises(media.MediaError):
            media._rpc("eval", "1+1")
        self.assertEqual(self.loader.calls, [])
        self.assertTrue(media._RPC_METHODS)

    # ---- 确认回答 ----

    def test_is_yes_parsing(self):
        for text in ("对", "是的", "嗯", "没错", "就这个"):
            with self.subTest(text=text):
                self.assertIs(media.is_yes(text), True)
        for text in ("不是", "不对", "换一个", "不要"):
            with self.subTest(text=text):
                self.assertIs(media.is_yes(text), False)
        self.assertIsNone(media.is_yes("今天天气怎么样"))


if __name__ == "__main__":
    unittest.main()
