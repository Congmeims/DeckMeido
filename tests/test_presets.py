"""按游戏提示词预设：存储 CRUD、appid/名称回退、切换与恢复、坏数据容错"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import presets


class PresetsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dm-presets-"))
        self._env = mock.patch.dict(os.environ, {
            "DECKMEIDO_DATA_DIR": str(self.tmp),
        })
        self._env.start()
        presets.reset_runtime()

    def tearDown(self):
        presets.reset_runtime()
        self._env.stop()
        os.environ.pop("DECKMEIDO_DATA_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self) -> Path:
        return self.tmp / "presets" / "presets.json"

    # ---- 存储 CRUD ----
    def test_upsert_then_list_and_persist(self):
        item = presets.upsert("12345", "艾尔登法环", "说话像褪色者")
        self.assertEqual(item["appid"], "12345")
        self.assertEqual(item["title"], "艾尔登法环")
        self.assertEqual(self.path().exists(), True)

        items = presets.list_presets()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["prompt"], "说话像褪色者")

        # 再读一遍仍然在，说明落盘了
        raw = json.loads(self.path().read_text(encoding="utf-8"))
        self.assertEqual(raw["version"], presets.SCHEMA_VERSION)

    def test_upsert_same_game_updates_in_place(self):
        first = presets.upsert("777", "游戏甲", "第一版")
        second = presets.upsert("777", "游戏甲", "第二版")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["prompt"], "第二版")
        self.assertEqual(len(presets.list_presets()), 1)

    def test_update_title_and_prompt(self):
        item = presets.upsert("", "小游戏", "原文")
        updated = presets.update(item["id"], prompt="新文", title="自定义名")
        self.assertEqual(updated["title"], "自定义名")
        self.assertEqual(updated["prompt"], "新文")
        self.assertEqual(updated["game_name"], "小游戏")
        self.assertEqual(presets.list_presets()[0]["title"], "自定义名")

    def test_delete(self):
        item = presets.upsert("", "小游戏", "原文")
        self.assertTrue(presets.delete(item["id"]))
        self.assertEqual(presets.list_presets(), [])
        self.assertFalse(presets.delete(item["id"]))

    def test_invalid_input_rejected(self):
        with self.assertRaises(ValueError):
            presets.upsert("", "", "有内容但没游戏")
        with self.assertRaises(ValueError):
            presets.upsert("1", "游戏", "   ")

    def test_empty_appid_name_only_valid(self):
        """没有 appid（非 Steam 游戏）时只靠游戏名也能存，appid 保持空串"""
        item = presets.upsert("", "Steam 之外的程序", "只用名字绑定")
        self.assertEqual(item["appid"], "")
        self.assertEqual(item["title"], "Steam 之外的程序")

    def test_appid_text_normalization(self):
        """payload 里的 appid 统一成字符串；0/布尔/空白都算无效"""
        self.assertEqual(presets.appid_text(2312770427), "2312770427")
        self.assertEqual(presets.appid_text(" 123 "), "123")
        for bad in ("0", 0, "", None, True, False, "abc"):
            self.assertEqual(presets.appid_text(bad), "")

    def test_write_failure_propagates(self):
        """写盘失败向上抛 OSError，且旧文件内容不能被改坏"""
        presets.upsert("1", "游戏", "原文")
        with mock.patch.object(presets, "_write", side_effect=OSError("磁盘只读")):
            with self.assertRaises(OSError):
                presets.upsert("1", "游戏", "新文")
        self.assertEqual(presets.list_presets()[0]["prompt"], "原文")

    # ---- appid / 名称回退 ----
    def test_appid_wins_over_same_name(self):
        a = presets.upsert("111", "同名游戏", "A 游戏提示词")
        b = presets.upsert("222", "同名游戏", "B 游戏提示词")
        self.assertNotEqual(a["id"], b["id"])
        hit = presets.find(presets.list_presets(), "222", "同名游戏")
        self.assertEqual(hit["id"], b["id"])
        hit = presets.find(presets.list_presets(), "111", "")
        self.assertEqual(hit["id"], a["id"])

    def test_name_fallback_when_no_appid(self):
        item = presets.upsert("", "稻草人之地", "无 appid 的提示词")
        hit = presets.find(presets.list_presets(), "", " 稻草人之地 ")
        self.assertEqual(hit["id"], item["id"])
        # 全半角/大小写/空白差异也要能命中
        en = presets.upsert("", "Stardew Valley", "星露谷提示词")
        hit = presets.find(presets.list_presets(), "", "  stardeW   valley ")
        self.assertEqual(hit["id"], en["id"])

    def test_old_name_only_preset_binds_appid_later(self):
        old = presets.upsert("", "老游戏", "老数据")
        again = presets.upsert("999", "老游戏", "新内容")
        self.assertEqual(again["id"], old["id"])
        self.assertEqual(again["appid"], "999")
        hit = presets.find(presets.list_presets(), "999", "")
        self.assertEqual(hit["id"], old["id"])

    # ---- 自动套用与恢复 ----
    def test_switch_applies_and_exit_restores_default(self):
        presets.upsert("123", "游戏甲", "游戏甲专用提示词")
        view = presets.set_active_game("123", "游戏甲")
        self.assertTrue(view["applied"])
        self.assertEqual(view["title"], "游戏甲")
        self.assertEqual(presets.effective_prompt("全局默认"), "游戏甲专用提示词")
        # 全局配置没被改写
        self.assertEqual(presets.list_presets()[0]["prompt"], "游戏甲专用提示词")

        view = presets.set_active_game("", "")
        self.assertFalse(view["applied"])
        self.assertEqual(view["preset_id"], "")
        self.assertEqual(presets.effective_prompt("全局默认"), "全局默认")

    def test_game_without_preset_keeps_default(self):
        presets.upsert("123", "游戏甲", "甲")
        view = presets.set_active_game("456", "游戏乙")
        self.assertFalse(view["applied"])
        self.assertEqual(view["game_name"], "游戏乙")
        self.assertEqual(presets.effective_prompt("全局默认"), "全局默认")

    def test_delete_active_preset_restores_default(self):
        item = presets.upsert("123", "游戏甲", "甲")
        presets.set_active_game("123", "游戏甲")
        presets.delete(item["id"])
        self.assertEqual(presets.effective_prompt("全局默认"), "全局默认")
        self.assertFalse(presets.active_view()["applied"])

    def test_update_active_preset_takes_effect_immediately(self):
        item = presets.upsert("123", "游戏甲", "旧")
        presets.set_active_game("123", "游戏甲")
        presets.update(item["id"], prompt="新", title="新名字")
        self.assertEqual(presets.effective_prompt("全局默认"), "新")
        self.assertEqual(presets.active_view()["title"], "新名字")

    # ---- 坏数据容错 ----
    def test_corrupt_json_does_not_raise(self):
        self.path().parent.mkdir(parents=True, exist_ok=True)
        self.path().write_text("{ this is not json", encoding="utf-8")
        self.assertEqual(presets.load(), [])
        self.assertEqual(presets.effective_prompt("全局默认"), "全局默认")
        # 坏文件不阻塞后续保存
        presets.upsert("1", "游戏", "提示词")
        self.assertEqual(len(presets.list_presets()), 1)

    def test_bad_records_skipped(self):
        self.path().parent.mkdir(parents=True, exist_ok=True)
        self.path().write_text(json.dumps({
            "version": presets.SCHEMA_VERSION,
            "presets": [
                "not-a-dict",
                {"appid": "1", "game_name": "游戏", "prompt": "   "},
                {"appid": "2", "game_name": "好游戏", "prompt": "可用"},
            ],
        }, ensure_ascii=False), encoding="utf-8")
        items = presets.load()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["game_name"], "好游戏")

    def test_unknown_version_still_readable(self):
        self.path().parent.mkdir(parents=True, exist_ok=True)
        self.path().write_text(json.dumps({
            "version": 99,
            "presets": [{"appid": "", "game_name": "游戏", "prompt": "提示词"}],
        }, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(len(presets.load()), 1)


if __name__ == "__main__":
    unittest.main()
