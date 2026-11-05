"""请求来源校验（server/authguard.py）的单元测试。

访问令牌已下线，这里只覆盖 Cookie 认证需要的同源 / Sec-Fetch-Site 判断。
"""
from __future__ import annotations

import unittest

from server import authguard


class CsrfTest(unittest.TestCase):
    def test_same_origin_matches_scheme_and_host(self):
        self.assertTrue(authguard.same_origin("http://127.0.0.1:18765", "127.0.0.1:18765"))
        self.assertTrue(authguard.same_origin("https://deck.local", "deck.local"))
        self.assertTrue(authguard.same_origin(None, "127.0.0.1:18765"))

    def test_cross_site_origin_rejected(self):
        self.assertFalse(authguard.same_origin("https://evil.example", "127.0.0.1:18765"))
        self.assertFalse(authguard.same_origin("http://127.0.0.1:18765", ""))
        self.assertFalse(authguard.same_origin("file:///tmp/x", "127.0.0.1:18765"))

    def test_sec_fetch_site(self):
        self.assertTrue(authguard.same_site(None))
        self.assertTrue(authguard.same_site("same-origin"))
        self.assertTrue(authguard.same_site("none"))
        self.assertFalse(authguard.same_site("cross-site"))
        self.assertFalse(authguard.same_site("same-site"))


if __name__ == "__main__":
    unittest.main()
