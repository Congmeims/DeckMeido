"""QQ 会话隔离：群按群号、私聊按 QQ 号，重启后按标识恢复。

会话号的生成在 server/onebot.py（session_sid），标题与落盘在
server/session_store.py。这里不经过 main.py，避免把整个后端拉起来。
"""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import session_store
from server.onebot import MessageEvent, session_sid
from server.session_store import (SessionStore, qq_session_title, set_title)


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


if __name__ == "__main__":
    unittest.main()
