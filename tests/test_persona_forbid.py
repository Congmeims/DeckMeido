"""禁用词句：默认空、旧逗号串兼容、空列表能存能读"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import persona


class PersonaForbidTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="deckmeido-persona-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.file = self.tmp / "meidosoul.json"
        patch = mock.patch.object(persona, "_path", return_value=self.file)
        patch.start()
        self.addCleanup(patch.stop)

    def test_default_is_empty(self):
        self.assertEqual(persona.load()["style"]["forbid"], [])

    def test_legacy_comma_string_becomes_list(self):
        self.file.write_text(json.dumps({
            "name": "DeckMeido",
            "style": {"forbid": "喵，呢~,人家、哼"},
        }, ensure_ascii=False), encoding="utf-8")
        forbid = persona.load()["style"]["forbid"]
        self.assertEqual(forbid, ["喵", "呢~", "人家", "哼"])

    def test_empty_list_survives_save_and_load(self):
        saved = persona.save({
            "name": "DeckMeido",
            "style": {"address": "主人", "forbid": []},
        })
        self.assertEqual(saved["style"]["forbid"], [])
        self.assertEqual(persona.load()["style"]["forbid"], [])

    def test_space_inside_phrase_is_kept(self):
        saved = persona.save({
            "name": "DeckMeido",
            "style": {"address": "主人", "forbid": ["不许 这样说"]},
        })
        self.assertEqual(saved["style"]["forbid"], ["不许 这样说"])


if __name__ == "__main__":
    unittest.main()
