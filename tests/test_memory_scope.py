"""长期记忆 scope：写入归属、注入过滤、清理边界与旧数据迁移。

全部用临时目录，不碰真实 memory.json；重点保证升级后旧条目还能读、
群里不会读到私聊记忆。
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import memory


class ScopeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "memory.json"
        self._path_patch = mock.patch.object(memory, "_path", return_value=self.path)
        self._path_patch.start()
        with memory._used_lock:
            memory._USED.clear()

    def tearDown(self):
        self._path_patch.stop()
        with memory._used_lock:
            memory._USED.clear()
        self.tmp.cleanup()

    # ---------- 数据模型 ----------

    def test_norm_scope(self):
        self.assertEqual(memory.norm_scope(None), memory.SCOPE_PRIVATE)
        self.assertEqual(memory.norm_scope(""), memory.SCOPE_PRIVATE)
        self.assertEqual(memory.norm_scope("group: 123 "), "group:123")
        self.assertEqual(memory.norm_scope("person:456"), "person:456")
        for bad in ("group:", "person:", "elsewhere"):
            self.assertEqual(memory.norm_scope(bad), memory.SCOPE_PRIVATE, bad)

    def test_scope_helpers(self):
        self.assertEqual(memory.scope_group("123"), "group:123")
        self.assertEqual(memory.scope_group(""), memory.SCOPE_PRIVATE)
        self.assertEqual(memory.scope_person("456"), "person:456")
        self.assertEqual(memory.scope_person(""), memory.SCOPE_PRIVATE)
        # 群回合注入群 + 发言人；私聊回合注入私聊 + 对方
        self.assertEqual(memory.scopes_for("123", "456"), ("group:123", "person:456"))
        self.assertEqual(memory.scopes_for(None, "456"), ("private", "person:456"))
        self.assertEqual(memory.scopes_for(None, None), ("private",))
        self.assertEqual(memory.write_scope("123"), "group:123")
        self.assertEqual(memory.write_scope(None), "private")

    # ---------- 旧数据迁移 ----------

    def test_legacy_items_without_scope_read_as_private(self):
        self.path.write_text(json.dumps({"items": [
            {"id": "old1", "kind": "profile", "text": "回答简短一点"},
            {"id": "old2", "kind": "note", "text": "项目用 uv"},
        ]}, ensure_ascii=False), encoding="utf-8")
        items = memory.load()["items"]
        self.assertEqual([x["scope"] for x in items], ["private", "private"])
        # 旧条目照旧注入私聊，不会丢
        text, ids = memory.block(scopes=("private",))
        self.assertIn("回答简短一点", text)
        self.assertEqual(ids, ["old1", "old2"])
        # 群里默认组合不会看到私聊内容
        text, ids = memory.block(scopes=memory.scopes_for("123", "456"))
        self.assertEqual((text, ids), ("", []))

    def test_invalid_scope_does_not_leak_into_group(self):
        self.path.write_text(json.dumps({"items": [
            {"id": "bad", "kind": "note", "text": "私事", "scope": "group:"},
        ]}, ensure_ascii=False), encoding="utf-8")
        item = memory.load()["items"][0]
        self.assertEqual(item["scope"], "private")

    # ---------- 写入归属 ----------

    def test_add_respects_explicit_and_context_scope(self):
        memory.add("note", "本群约定", scope="group:123")
        memory.add("note", "他的偏好", scope="person:456")
        memory.add("note", "主人的偏好")
        scopes = sorted(x["scope"] for x in memory.load()["items"])
        self.assertEqual(scopes, ["group:123", "person:456", "private"])

        token = memory.set_context(memory.scopes_for("123", "456"), memory.write_scope("123"))
        try:
            item = memory.add("note", "群里记住的事")
        finally:
            memory.reset_context(token)
        self.assertEqual(item["scope"], "group:123")
        self.assertEqual(memory.current_write_scope(), "private")

    def test_dedupe_only_within_same_scope(self):
        first = memory.add("note", "回答简短一点", scope="private")
        again = memory.add("note", "回答简短一点。", scope="private")
        self.assertEqual(first["id"], again["id"])
        other = memory.add("note", "回答简短一点", scope="group:123")
        self.assertNotEqual(first["id"], other["id"])
        self.assertEqual(len(memory.load()["items"]), 2)

    # ---------- 注入与查看 ----------

    def test_block_only_injects_current_scopes(self):
        memory.add("profile", "主人喜欢简短", scope="private")
        memory.add("note", "本群周五活动", scope="group:123")
        memory.add("note", "小明怕辣", scope="person:456")
        text, ids = memory.block(scopes=memory.scopes_for("123", "456"))
        self.assertIn("本群周五活动", text)
        self.assertIn("小明怕辣", text)
        self.assertNotIn("主人喜欢简短", text)
        self.assertEqual(len(ids), 2)
        # 私聊回合只看私聊 + 对方，不带群记忆
        text, ids = memory.block(scopes=memory.scopes_for(None, "456"))
        self.assertIn("主人喜欢简短", text)
        self.assertIn("小明怕辣", text)
        self.assertNotIn("本群周五活动", text)

    def test_block_budget_drops_old_notes_first(self):
        for i in range(20):
            memory.add("note", f"n{i:02d}" + "x" * 290, scope="private")
        text, ids = memory.block(scopes=("private",), max_chars=1000)
        self.assertLessEqual(len(text), 1000)
        # 新的留下、旧的先丢；磁盘上一条都没删
        self.assertIn("n19", text)
        self.assertNotIn("n00", text)
        self.assertEqual(len(memory.load()["items"]), 20)

    def test_block_budget_keeps_profile_over_notes(self):
        memory.add("profile", "称呼我主人", scope="private")
        for i in range(10):
            memory.add("note", f"n{i}" + "x" * 290, scope="private")
        text, _ = memory.block(scopes=("private",), max_chars=400)
        self.assertIn("称呼我主人", text)

    def test_list_scope_sorted_and_limited(self):
        memory.add("note", "b", scope="group:123")
        memory.add("profile", "a", scope="group:123")
        items = memory.list_scope("group:123")
        self.assertEqual([x["text"] for x in items], ["a", "b"])
        self.assertEqual(len(memory.list_scope("group:123", limit=1)), 1)
        self.assertEqual(memory.list_scope("group:999"), [])

    # ---------- 清理边界 ----------

    def test_clear_scope_only_removes_that_scope(self):
        memory.add("note", "私聊", scope="private")
        memory.add("note", "本群", scope="group:123")
        memory.add("note", "他", scope="person:456")
        self.assertEqual(memory.clear_scope("group:123"), 1)
        left = {x["scope"] for x in memory.load()["items"]}
        self.assertEqual(left, {"private", "person:456"})
        self.assertEqual(memory.clear_scope("group:123"), 0)

    def test_clear_scopes_handles_multiple_and_normalizes(self):
        memory.add("note", "群", scope="group:123")
        memory.add("note", "人", scope="person:456")
        memory.add("note", "私聊", scope="private")
        self.assertEqual(memory.clear_scopes(("group:123", "person:456")), 2)
        self.assertEqual([x["text"] for x in memory.load()["items"]], ["私聊"])
        # 空集合什么都不删，避免误清
        self.assertEqual(memory.clear_scopes(()), 0)


if __name__ == "__main__":
    unittest.main()
