"""上传任务卡的纯函数测试：汇总进度 / 体积格式化 / 失败聚合。

index.html 是单文件内联脚本，这里只抽取 [FB-UPLOAD-HELPERS] 标记之间的
纯函数，用 node 跑断言；没有 node 时跳过（不伪造结果）。

    python -m unittest tests.test_upload_aggregate -v
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "server" / "static" / "index.html"
NODE = shutil.which("node") or shutil.which("node.exe")


def _extract_helpers() -> str:
    text = HTML.read_text(encoding="utf-8")
    m = re.search(
        r"// \[FB-UPLOAD-HELPERS-BEGIN\](.*?)// \[FB-UPLOAD-HELPERS-END\]",
        text, re.S,
    )
    if not m:
        raise AssertionError("index.html 里找不到 FB-UPLOAD-HELPERS 标记段")
    return m.group(1)


HARNESS = r"""
var out = {};
out.fmt0 = fbFmtBytes(0);
out.fmt1 = fbFmtBytes(1023);
out.fmt2 = fbFmtBytes(1024);
out.fmt3 = fbFmtBytes(1536);
out.fmt4 = fbFmtBytes(1288490188);
var longPath = 'Project Virtual Girl/Project Virtual Girl_Data/sharedassets0.assets';
out.mid = fbMidPath(longPath, 30);
out.midShort = fbMidPath('a.bin', 30);

// 完成 2、失败 2（其中一个遍历阶段失败、无字节）、正在传 1（只传了一半）
var task = {status: 'running', files: [
  {status: 'done', size: 10},
  {status: 'done', size: 20},
  {status: 'uploading', size: 30, loaded: 15, rel: 'a/b/c.bin'},
  {status: 'fail', size: 40, loaded: 10, rel: 'd/e.bin', err: 'x'},
  {status: 'fail', size: 0, rel: 'bad/dir', err: '读不到'}
]};
out.stats = fbTaskStats(task);
out.summary = fbTaskSummary(task);

out.allDone = fbTaskSummary({status: 'done', files: [
  {status: 'done', size: 10}, {status: 'done', size: 20}
]});
out.cancelled = fbTaskSummary({status: 'cancelled', files: [
  {status: 'done', size: 10}, {status: 'fail', size: 40}, {status: 'cancelled', size: 50}
]});
out.empty = fbTaskSummary({status: 'done', files: []});
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "没有 node，跳过前端纯函数测试")
class UploadAggregateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        script = _extract_helpers() + "\n" + HARNESS
        fd, cls.path = tempfile.mkstemp(suffix=".js", prefix="dm-upload-agg-")
        with open(fd, "w", encoding="utf-8") as f:
            f.write(script)
        r = subprocess.run([NODE, cls.path], capture_output=True, text=True)
        if r.returncode != 0:
            raise AssertionError(f"node 执行失败：{r.stderr}")
        cls.out = json.loads(r.stdout.strip())

    @classmethod
    def tearDownClass(cls):
        Path(cls.path).unlink(missing_ok=True)

    def test_体积格式化(self):
        self.assertEqual(self.out["fmt0"], "0 B")
        self.assertEqual(self.out["fmt1"], "1023 B")
        self.assertEqual(self.out["fmt2"], "1.0 KB")
        self.assertEqual(self.out["fmt3"], "1.5 KB")
        self.assertEqual(self.out["fmt4"], "1.2 GB")

    def test_长路径中间省略(self):
        mid = self.out["mid"]
        self.assertLessEqual(len(mid), 30)
        self.assertIn("…", mid)
        self.assertTrue(mid.startswith("Project Virtual"))
        self.assertTrue(mid.endswith("assets"))
        self.assertEqual(self.out["midShort"], "a.bin")

    def test_汇总进度计算(self):
        st = self.out["stats"]
        self.assertEqual(st["total"], 5)
        self.assertEqual(st["done"], 2)
        self.assertEqual(st["failed"], 2)
        self.assertEqual(st["totalBytes"], 100)
        # 已完成 10+20 + 正在传 15 + 失败项已传 10 = 55
        self.assertEqual(st["sentBytes"], 55)
        self.assertEqual(st["percent"], 55)
        self.assertEqual(st["current"], "a/b/c.bin")

    def test_失败聚合到一行结果(self):
        self.assertEqual(self.out["summary"],
                         "5 个文件 · 100 B · 2 成功 2 失败")
        self.assertEqual(self.out["allDone"], "2 个文件 · 30 B · 2 成功")
        self.assertEqual(self.out["cancelled"],
                         "3 个文件 · 100 B · 已取消 · 1 成功 1 失败")
        self.assertEqual(self.out["empty"], "0 个文件 · 0 成功")
