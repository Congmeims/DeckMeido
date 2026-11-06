"""多组 API 配置（server/apicfg.py）的单元测试。

覆盖：旧版单组配置迁移（不丢 Key、不双写）、增删改与当前使用切换、
删除当前条目后的回退、Key 掩码，以及 config.toml 内联表数组的写入往返。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import apicfg  # noqa: E402
from server.tomlcfg import write_config  # noqa: E402


class ConfigBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dm-apicfg-"))
        self.path = self.tmp / "config.toml"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, text: str) -> None:
        self.path.write_text(text, encoding="utf-8")

    def parsed(self) -> dict:
        return tomllib.loads(self.path.read_text(encoding="utf-8"))


class LegacyMigrationTest(ConfigBase):
    def test_旧版单组配置折成一条默认记录并清空旧键(self):
        self.write(
            '[llm]\n'
            'base_url = "https://api.deepseek.com/"\n'
            'api_key = "sk-legacy-1234567890"\n'
            'model = "deepseek-chat"\n'
        )
        cfg = tomllib.loads(self.path.read_text(encoding="utf-8"))
        state = apicfg.load(cfg, self.path)

        self.assertEqual(len(state["profiles"]), 1)
        self.assertEqual(state["current"], apicfg.LEGACY_ID)
        prof = state["profiles"][0]
        self.assertEqual(prof["name"], "默认")
        self.assertEqual(prof["base_url"], "https://api.deepseek.com")
        # 不丢 Key
        self.assertEqual(prof["api_key"], "sk-legacy-1234567890")

        data = self.parsed()
        self.assertEqual(len(data["llm"]["apis"]), 1)
        self.assertEqual(data["llm"]["current_api"], apicfg.LEGACY_ID)
        self.assertEqual(data["llm"]["apis"][0]["api_key"], "sk-legacy-1234567890")
        # 不重复写两个来源：旧键已清空
        self.assertEqual(data["llm"]["base_url"], "")
        self.assertEqual(data["llm"]["api_key"], "")
        # 配置里的其它键保持原样
        self.assertEqual(data["llm"]["model"], "deepseek-chat")

    def test_迁移幂等_再读不会多出一条(self):
        self.write(
            '[llm]\n'
            'base_url = "https://api.deepseek.com"\n'
            'api_key = "sk-legacy-1234567890"\n'
        )
        cfg1 = tomllib.loads(self.path.read_text(encoding="utf-8"))
        first = apicfg.load(cfg1, self.path)
        cfg2 = tomllib.loads(self.path.read_text(encoding="utf-8"))
        second = apicfg.load(cfg2, self.path)
        self.assertEqual(len(first["profiles"]), 1)
        self.assertEqual(len(second["profiles"]), 1)
        self.assertEqual(second["profiles"][0]["id"], apicfg.LEGACY_ID)
        self.assertEqual(len(self.parsed()["llm"]["apis"]), 1)

    def test_没有任何配置时返回空列表(self):
        self.write('[llm]\nmodel = "deepseek-chat"\n')
        cfg = tomllib.loads(self.path.read_text(encoding="utf-8"))
        state = apicfg.load(cfg, self.path)
        self.assertEqual(state["profiles"], [])
        self.assertEqual(state["current"], "")
        self.assertIsNone(apicfg.active(state))


class ProfilesUpdateTest(ConfigBase):
    def setUp(self):
        super().setUp()
        self.write(
            '[llm]\n'
            'current_api = "api-a"\n'
            'apis = [\n'
            '  { id = "api-a", name = "主接口", base_url = "https://a.example", api_key = "sk-aaaaaaaa1111" },\n'
            '  { id = "api-b", name = "备用", base_url = "https://b.example", api_key = "sk-bbbbbbbb2222" },\n'
            ']\n'
        )
        self.cfg = tomllib.loads(self.path.read_text(encoding="utf-8"))
        self.state = apicfg.from_config(self.cfg)

    def test_新增与重命名保留稳定_id(self):
        state = apicfg.merge_update(self.state, [
            {"id": "api-a", "name": "主力接口", "base_url": "https://a.example/v1", "api_key": ""},
            {"id": "api-b", "name": "备用", "base_url": "https://b.example", "api_key": ""},
            {"id": "", "name": "第三组", "base_url": "https://c.example", "api_key": "sk-cccccccc3333"},
        ], "api-a")
        self.assertEqual(len(state["profiles"]), 3)
        self.assertEqual(state["current"], "api-a")
        first = state["profiles"][0]
        self.assertEqual(first["id"], "api-a")
        self.assertEqual(first["name"], "主力接口")
        self.assertEqual(first["base_url"], "https://a.example/v1")
        # 改名/改地址不动 Key
        self.assertEqual(first["api_key"], "sk-aaaaaaaa1111")
        new = state["profiles"][2]
        self.assertTrue(new["id"].startswith("api-"))
        self.assertEqual(new["api_key"], "sk-cccccccc3333")

    def test_留空与打码串都不覆盖已有_Key(self):
        masked = apicfg.mask_key("sk-aaaaaaaa1111")
        state = apicfg.merge_update(self.state, [
            {"id": "api-a", "name": "主接口", "base_url": "https://a.example", "api_key": ""},
            {"id": "api-b", "name": "备用", "base_url": "https://b.example", "api_key": masked},
        ], "api-a")
        self.assertEqual(state["profiles"][0]["api_key"], "sk-aaaaaaaa1111")
        self.assertEqual(state["profiles"][1]["api_key"], "sk-bbbbbbbb2222")

    def test_真_Key_会替换(self):
        state = apicfg.merge_update(self.state, [
            {"id": "api-a", "name": "主接口", "base_url": "https://a.example",
             "api_key": "sk-newnewnew9999"},
        ], "api-a")
        self.assertEqual(state["profiles"][0]["api_key"], "sk-newnewnew9999")

    def test_删除当前条目回退到剩余第一条(self):
        state = apicfg.merge_update(self.state, [
            {"id": "api-b", "name": "备用", "base_url": "https://b.example", "api_key": ""},
        ], "api-a")
        self.assertEqual(len(state["profiles"]), 1)
        self.assertEqual(state["profiles"][0]["id"], "api-b")
        self.assertEqual(state["current"], "api-b")

    def test_全部删除后没有当前配置(self):
        state = apicfg.merge_update(self.state, [], "api-a")
        self.assertEqual(state["profiles"], [])
        self.assertEqual(state["current"], "")
        self.assertIsNone(apicfg.active(state))

    def test_切换当前使用与非法_id(self):
        state = apicfg.select(self.state, "api-b")
        self.assertEqual(state["current"], "api-b")
        same = apicfg.select(state, "api-not-exist")
        self.assertEqual(same["current"], "api-b")

    def test_对外只给掩码(self):
        rows = apicfg.public(self.state)
        self.assertEqual(rows[0]["api_key"], "sk-aaa…1111")
        self.assertTrue(rows[0]["api_key_set"])
        self.assertTrue(rows[0]["current"])
        blob = repr(rows)
        self.assertNotIn("sk-aaaaaaaa1111", blob)
        self.assertNotIn("sk-bbbbbbbb2222", blob)

    def test_保存后能按_toml_读回(self):
        state = apicfg.merge_update(self.state, [
            {"id": "api-a", "name": "主接口", "base_url": "https://a.example", "api_key": ""},
            {"id": "api-b", "name": "备用", "base_url": "https://b.example", "api_key": ""},
            {"id": "", "name": "第三组", "base_url": "https://c.example", "api_key": "sk-cccccccc3333"},
        ], "api-b")
        self.assertEqual(len(state["profiles"]), 3)
        write_config(self.path, {"llm": apicfg.to_updates(state)})
        data = self.parsed()
        self.assertEqual(len(data["llm"]["apis"]), 3)
        self.assertEqual(data["llm"]["current_api"], "api-b")
        self.assertEqual(data["llm"]["apis"][2]["api_key"], "sk-cccccccc3333")
        # 旧键被清空，不是两个来源
        self.assertEqual(data["llm"].get("base_url", ""), "")
        self.assertEqual(data["llm"].get("api_key", ""), "")


class MaskTest(unittest.TestCase):
    def test_掩码规则(self):
        self.assertEqual(apicfg.mask_key(""), "")
        self.assertEqual(apicfg.mask_key("sk-7sw1234567890uo3o"), "sk-7sw…uo3o")
        # 太短的 Key 不暴露后半段
        self.assertEqual(apicfg.mask_key("sk-abc"), "sk…")


if __name__ == "__main__":
    unittest.main()
