"""前端结构自检：防止「删别的代码时顺手删掉主对话连接」。

e8351a5 的现场：移除网页端远程终端时，同一个 <script> 里的主对话
connect()、心跳、可见性上报一起被删掉，页面照常加载却永远掉线，
外加整段逻辑不在 devtools 里跑就发现不了。

这里只做静态断言（不启动浏览器）：
  1. 内联脚本能被 node --check 解析（语法坏掉的部署直接拦住）；
  2. 主对话连接的三个关键符号都还在：connect 定义、/ws 的 WebSocket、
     以及 bootAuth 与 connect 的衔接；
  3. 接线完整：bootAuth 通过后确实会调用 connect()。

    python -m unittest tests.test_frontend_wiring -v
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "server" / "static" / "index.html"
NODE = shutil.which("node") or shutil.which("node.exe")


def _html() -> str:
    return HTML.read_text(encoding="utf-8")


def _inline_scripts() -> list[str]:
    """取出 index.html 里所有内联 <script>（跳过 src= 外链）"""
    return re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", _html(), re.S)


class FrontendWiringTest(unittest.TestCase):
    def test_inline_scripts_parse(self):
        """内联脚本必须语法合法：大段删除后最容易留下半截代码"""
        if not NODE:
            self.skipTest("没有 node，跳过 JS 语法检查")
        scripts = [s for s in _inline_scripts() if s.strip()]
        self.assertTrue(scripts, "index.html 里没有内联脚本？")
        for i, src in enumerate(scripts):
            with tempfile.NamedTemporaryFile(
                "w", suffix=".js", delete=False, encoding="utf-8"
            ) as fh:
                fh.write(src)
                path = fh.name
            try:
                res = subprocess.run(
                    [NODE, "--check", path], capture_output=True, text=True
                )
            finally:
                Path(path).unlink(missing_ok=True)
            self.assertEqual(
                res.returncode, 0,
                f"第 {i + 1} 段内联脚本语法错误：\n{(res.stderr or '')[:1200]}",
            )

    def test_main_chat_connection_present(self):
        """主对话连接必须存在：缺少它页面能开但一直掉线（e8351a5 事故）"""
        text = _html()
        self.assertIn(
            "function connect() {", text,
            "主对话 connect() 不见了：删除终端/其他模块时被误删",
        )
        self.assertRegex(
            text, r"ws = new WebSocket\([^)]*'/ws'",
            "主对话 /ws 的 WebSocket 创建不见了：界面会一直显示掉线",
        )

    def test_boot_auth_starts_connection(self):
        """鉴权通过必须真的建连，否则配对页消失后停在掉线状态"""
        text = _html()
        m = re.search(r"function bootAuth\(\) \{(.*?)\n  \}", text, re.S)
        self.assertIsNotNone(m, "bootAuth() 不见了")
        body = m.group(1)
        self.assertIn(
            "connect()", body,
            "bootAuth 通过后没有调用 connect()：会出现「不配对但掉线」",
        )
        self.assertIn(
            "bootAuthTries", body,
            "bootAuth 缺少后端未就绪时的退避重试（冷启动会误报连不上）",
        )

    def test_heartbeat_helpers_present(self):
        """主连接依赖的心跳/上报函数不能被别的删除顺带带走"""
        text = _html()
        for name in ("startHeartbeat", "stopHeartbeat",
                     "startViewReport", "stopViewReport"):
            self.assertIn(
                f"function {name}(", text,
                f"{name}() 不见了：主连接会抛 ReferenceError 直接断开",
            )


if __name__ == "__main__":
    unittest.main()
