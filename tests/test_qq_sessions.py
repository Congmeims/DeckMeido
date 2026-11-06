"""QQ 会话隔离：群按群号、私聊按 QQ 号，重启后按标识恢复。

会话号的生成在 server/onebot.py（session_sid），标题与落盘在
server/session_store.py。这里不经过 main.py，避免把整个后端拉起来。
"""
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from server import session_store
from server.onebot import MessageEvent, session_sid
from server.session_store import (SessionStore, attach_voice, qq_session_title,
                                  session_order, set_title)


def ev(uin, group_id=None):
    return MessageEvent(uin=str(uin), text="hi", group_id=group_id)


class QqSessionSidTest(unittest.TestCase):
    def test_group_members_share_one_session(self):
        self.assertEqual(session_sid(ev(10001, 20001)), "qq-g-20001")
        self.assertEqual(session_sid(ev(30003, 20001)), "qq-g-20001")

    def test_different_groups_do_not_mix(self):
        self.assertNotEqual(session_sid(ev(30003, 20001)),
                            session_sid(ev(30003, 20002)))

    def test_group_and_private_do_not_mix(self):
        self.assertNotEqual(session_sid(ev(30003, 20001)),
                            session_sid(ev(30003)))

    def test_private_is_per_qq(self):
        self.assertEqual(session_sid(ev(10001)), "qq-p-10001")
        self.assertNotEqual(session_sid(ev(10001)), session_sid(ev(30003)))


class QqSessionTitleTest(unittest.TestCase):
    """标题 = QQ + 触发方标识后 5 位，不足 5 位按实际位数、不补零"""

    def test_group_title_uses_last_five_of_group_id(self):
        self.assertEqual(qq_session_title("qq-g-123456789"), "QQ56789")
        self.assertEqual(qq_session_title("qq-g-20001"), "QQ20001")

    def test_private_title_uses_last_five_of_qq(self):
        self.assertEqual(qq_session_title("qq-p-645849334"), "QQ49334")

    def test_short_id_is_not_padded(self):
        self.assertEqual(qq_session_title("qq-p-1234"), "QQ1234")
        self.assertEqual(qq_session_title("qq-g-1"), "QQ1")

    def test_old_group_uin_sid_uses_group_number(self):
        # 老格式（群号 + QQ 号）只认群号，标题以会话标识为准
        self.assertEqual(qq_session_title("qq-g-123456789-30003"), "QQ56789")

    def test_non_qq_sid_has_no_fixed_title(self):
        self.assertEqual(qq_session_title("default"), "")
        self.assertEqual(qq_session_title("abcd"), "")

    def test_manual_session_title_unchanged(self):
        data = {"id": "abcd", "title": "新会话"}
        set_title(data, [{"role": "user", "content": "帮我查个东西"}])
        self.assertEqual(data["title"], "帮我查个东西")

    def test_qq_session_title_ignores_first_message(self):
        data = {"id": "qq-g-123456789", "title": "新会话"}
        set_title(data, [{"role": "user", "content": "今天聊什么"}])
        self.assertEqual(data["title"], "QQ56789")

    def test_qq_title_survives_later_messages(self):
        # 同一 sid 换个首条消息再算一次，标题仍不变（只随会话标识）
        data = {"id": "qq-p-645849334", "title": "新会话"}
        set_title(data, [{"role": "user", "content": "第一句"}])
        self.assertEqual(data["title"], "QQ49334")
        set_title(data, [{"role": "user", "content": "完全不同的第二句"},
                         {"role": "user", "content": "第三句"}])
        self.assertEqual(data["title"], "QQ49334")


class SessionOrderTest(unittest.TestCase):
    """侧栏排序：updated 倒序，同一时间戳按 id 倒序，刷新前后顺序固定"""

    def test_tie_breaks_by_id_and_is_stable(self):
        items = [{"id": "b", "updated": 5}, {"id": "a", "updated": 5},
                 {"id": "c", "updated": 6}]
        self.assertEqual([i["id"] for i in session_order(items)], ["c", "b", "a"])
        self.assertEqual([i["id"] for i in session_order(list(reversed(items)))],
                         ["c", "b", "a"])


class VoiceMetaTest(unittest.TestCase):
    """语音元数据只挂到最后一条助手消息，老消息没有 voice 字段也不报错"""

    meta = {"url": "/api/voice/abc.wav", "seconds": 1.2,
            "speaker": "女仆", "bytes": 2048}

    def test_attach_to_last_assistant_only(self):
        hist = [
            {"role": "user", "content": "你好", "ts": 1},
            {"role": "assistant", "content": "第一轮", "ts": 2},
            {"role": "user", "content": "再来", "ts": 3},
            {"role": "assistant", "content": "第二轮", "ts": 4},
        ]
        ts = attach_voice(hist, self.meta)
        self.assertEqual(ts, 4)
        self.assertNotIn("voice", hist[1])
        self.assertEqual(hist[3]["voice"], self.meta)

    def test_attach_without_assistant_returns_none(self):
        self.assertIsNone(attach_voice([{"role": "user", "content": "hi"}], self.meta))
        self.assertIsNone(attach_voice([], self.meta))

    def test_empty_voice_is_ignored(self):
        hist = [{"role": "assistant", "content": "hi"}]
        self.assertIsNone(attach_voice(hist, {}))
        self.assertNotIn("voice", hist[0])

    def test_old_history_without_voice_is_untouched(self):
        hist = [{"role": "assistant", "content": "旧消息", "ts": 5}]
        self.assertNotIn("voice", hist[0])
        self.assertIsNone(attach_voice(hist, None))


class QqSessionPersistTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="deckmeido-sess-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patch_dir = mock.patch.object(session_store, "sessions_dir",
                                      return_value=self.tmp)
        patch_dir.start()
        self.addCleanup(patch_dir.stop)
        # 不让防抖定时器在测试结束后往真实数据根写 index.json
        patch_index = mock.patch.object(SessionStore, "_index_schedule",
                                        lambda self: None)
        patch_index.start()
        self.addCleanup(patch_index.stop)

    @staticmethod
    def barrier():
        session_store.io_submit(lambda: None).result(timeout=10)

    def test_sid_survives_restart(self):
        first = SessionStore()
        hist = [{"role": "user", "content": "群里聊了什么"}]
        first.write_session("qq-g-20001", hist)
        self.barrier()
        # 模拟重启：换一个全新的 store 只凭 sid 读回同一段历史
        restarted = SessionStore()
        data = restarted.read_session("qq-g-20001")
        self.assertEqual(data.get("history"), hist)
        self.assertEqual(data.get("title"), "QQ20001")

    def test_files_are_isolated_by_sid(self):
        store = SessionStore()
        store.write_session("qq-g-20001", [{"role": "user", "content": "A"}])
        store.write_session("qq-g-20002", [{"role": "user", "content": "B"}])
        store.write_session("qq-p-10001", [{"role": "user", "content": "C"}])
        self.barrier()
        fresh = SessionStore()
        self.assertEqual(fresh.read_session("qq-g-20001")["history"][0]["content"], "A")
        self.assertEqual(fresh.read_session("qq-g-20002")["history"][0]["content"], "B")
        self.assertEqual(fresh.read_session("qq-p-10001")["history"][0]["content"], "C")
        self.assertEqual(fresh.read_session("qq-g-20002").get("title"), "QQ20002")
        self.assertEqual(fresh.read_session("qq-p-10001").get("title"), "QQ10001")

    def test_title_is_not_rewritten_by_later_messages(self):
        """写两次（第二轮首条消息不同）后标题仍是首次的 QQ + 后 5 位"""
        store = SessionStore()
        store.write_session("qq-p-645849334", [{"role": "user", "content": "第一句"}])
        self.barrier()
        store.write_session("qq-p-645849334", [
            {"role": "user", "content": "第一句"},
            {"role": "assistant", "content": "回答"},
            {"role": "user", "content": "第二句"},
        ])
        self.barrier()
        data = SessionStore().read_session("qq-p-645849334")
        self.assertEqual(data.get("title"), "QQ49334")
        self.assertEqual(len(data.get("history") or []), 3)

    def test_view_does_not_bump_updated(self):
        """切走前的收尾写入（touch=False）不改 updated，点开会话不重排"""
        store = SessionStore()
        hist = [{"role": "user", "content": "旧内容"}]
        store.write_session("qq-p-20001", hist)
        self.barrier()
        before = dict(store.index()["qq-p-20001"])
        time.sleep(0.02)
        store.write_session("qq-p-20001", hist, touch=False)
        self.barrier()
        self.assertEqual(store.index()["qq-p-20001"]["updated"],
                         before["updated"])

    def test_new_message_bumps_updated(self):
        """只有真实活动（新消息落盘）才会前移"""
        store = SessionStore()
        store.write_session("qq-p-20002", [{"role": "user", "content": "一"}])
        self.barrier()
        before = store.index()["qq-p-20002"]["updated"]
        time.sleep(0.02)
        store.write_session("qq-p-20002", [
            {"role": "user", "content": "一"},
            {"role": "assistant", "content": "二"},
        ])
        self.barrier()
        self.assertGreater(store.index()["qq-p-20002"]["updated"], before)


if __name__ == "__main__":
    unittest.main()
