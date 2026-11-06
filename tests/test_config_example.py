"""仓库里那份 config.example.toml 的回归测试。

这份模板会被 install.sh 原样复制成新用户的 config.toml。它一旦出现重复表名
之类的语法错误，全新安装第一次启动就会直接崩，所以这里直接读取仓库里的真实
文件做解析，不再另造 fixture。
"""
from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "server" / "config.example.toml"

# 各段必需的键：与 server/main.py、plugin/main.py、client/main.py
# 以及 install.sh 读取的键名保持一致，少一个都算回归。
REQUIRED = {
    "server": {"lan_enable", "port"},
    "runtime": {"dev_mode"},
    "security": {"device_days", "permission_mode"},
    "remote_input": {"enabled", "sensitivity", "max_minutes"},
    "llm": {
        "base_url", "api_key", "model", "timeout", "reasoning_effort",
        "max_output_tokens", "max_continuations",
    },
    "agent": {
        "max_tool_rounds", "context_window", "compact_ratio",
        "compact_target_left", "with_shot", "ask_template",
        "status_prompt", "translate_template",
    },
    "power": {"keep_awake"},
    "update": {"github_enable", "mirror", "github_repo"},
    "tts": {"enable", "engine", "voice", "speed", "max_chars"},
    "stt": {"enable", "duck_output"},
    "ui": {"language"},
    "workspace": {"dir"},
    "qq": {
        "enable", "ws_url", "access_token", "admin_qq", "allow_groups",
        "group_admin_commands", "guest_prompt", "allow_image", "max_image_mb",
        "image_recent_minutes", "image_save_dir", "text_log_path",
        "auto_launch_after_install", "auto_launch_wait_seconds",
        "rate_per_minute", "group_rate_per_minute", "max_message_chars",
        "reply_chunk_chars", "max_reply_chunks",
        "redact_words", "redact_mask",
        "group_memo", "group_memo_size", "group_memo_ttl", "group_memo_max_chars",
        "confirm_timeout", "reconnect_min", "reconnect_max",
        "ping_interval", "ping_timeout",
    },
}


class ConfigExampleTest(unittest.TestCase):
    def test_template_parses(self):
        """重复的 [security] 曾让 tomllib 直接抛 Cannot declare 异常。"""
        text = EXAMPLE.read_text(encoding="utf-8")
        cfg = tomllib.loads(text)
        self.assertIsInstance(cfg, dict)

    def test_required_sections_and_keys(self):
        cfg = tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))
        for section, keys in REQUIRED.items():
            self.assertIn(section, cfg, f"缺少 [{section}] 段")
            missing = keys - set(cfg[section])
            self.assertFalse(missing, f"[{section}] 缺少键：{sorted(missing)}")

    def test_security_declared_once(self):
        """模板里 [security] 只能出现一次。"""
        text = EXAMPLE.read_text(encoding="utf-8")
        count = sum(1 for line in text.splitlines() if line.strip() == "[security]")
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
