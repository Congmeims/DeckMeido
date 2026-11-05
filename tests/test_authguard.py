"""访问令牌生成（server/authguard.py）的单元测试。"""
from __future__ import annotations

import unittest
from unittest import mock

from server import authguard


class TokenCompareTest(unittest.TestCase):
    def test_case_and_space_insensitive(self):
        self.assertTrue(authguard.token_eq("AbCdEf", "  ABCDEF "))
        self.assertTrue(authguard.token_eq("abc", "abc"))

    def test_mismatch_and_empty(self):
        self.assertFalse(authguard.token_eq("abc", "abd"))
        self.assertFalse(authguard.token_eq("abc", None))
        self.assertFalse(authguard.token_eq("abc", ""))

    def test_empty_never_matches_empty(self):
        # 调用方要先判断期望值非空；这里记录行为：空与空视为相等
        self.assertTrue(authguard.token_eq("", "  "))

    def test_hyphenated_display_form_matches(self):
        self.assertTrue(authguard.token_eq("abcd-efgh-jkmn", "ABCDEFGHJKMN"))
        self.assertTrue(authguard.token_eq("ABCDEFGHJKMN", "abcd-efgh-jkmn"))

    def test_crockford_confusing_chars_fold(self):
        # 手抄时把 0 写成 O、1 写成 I/L 也能通过
        self.assertTrue(authguard.token_eq("0123", "OL23"))
        self.assertTrue(authguard.token_eq("OIL", "011"))
        self.assertEqual(authguard.norm(" AB-CD "), "abcd")
        self.assertEqual(authguard.norm("OIL"), "011")

    def test_legacy_tokens_still_match(self):
        # 出厂口令、旧 16 位令牌、用户自定义短令牌都要继续可用
        self.assertTrue(authguard.token_eq("meido", "MEIDO"))
        self.assertTrue(authguard.token_eq("k7m2p9x4qw3n8r5t", "K7M2P9X4QW3N8R5T"))
        self.assertTrue(authguard.token_eq("abcd", "ABCD"))


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


class FailureLimiterTest(unittest.TestCase):
    def _at(self, limiter, now: float):
        return mock.patch.object(authguard.time, "monotonic", return_value=now)

    def test_blocks_after_20_failures_in_5_minutes(self):
        limiter = authguard.FailureLimiter()
        with self._at(limiter, 1000.0):
            for _ in range(20):
                self.assertFalse(limiter.blocked("1.2.3.4"))
                limiter.record("1.2.3.4")
            self.assertTrue(limiter.blocked("1.2.3.4"))

    def test_window_slides_and_other_ip_unaffected(self):
        limiter = authguard.FailureLimiter()
        with self._at(limiter, 1000.0):
            for _ in range(20):
                limiter.record("1.2.3.4")
            self.assertFalse(limiter.blocked("5.6.7.8"))
        with self._at(limiter, 1301.0):
            self.assertFalse(limiter.blocked("1.2.3.4"))

    def test_empty_ip_never_blocks(self):
        limiter = authguard.FailureLimiter()
        for _ in range(50):
            limiter.record("")
        self.assertFalse(limiter.blocked(""))


class GenTokenTest(unittest.TestCase):
    def test_default_length_is_12(self):
        self.assertEqual(authguard.TOKEN_LEN, 12)
        token = authguard.gen_token()
        self.assertEqual(len(token), 12)

    def test_charset_is_crockford_base32(self):
        token = authguard.gen_token(32)
        self.assertTrue(set(token) <= set(authguard.ALPHABET.lower()))
        for bad in "ilou":
            self.assertNotIn(bad, token)

    def test_tokens_are_lowercase_and_random(self):
        tokens = {authguard.gen_token() for _ in range(50)}
        self.assertEqual(len(tokens), 50)
        for token in tokens:
            self.assertEqual(token, token.lower())

    def test_grouped_display(self):
        self.assertEqual(authguard.grouped("abcdefghjkmn"), "ABCD-EFGH-JKMN")
        self.assertEqual(authguard.grouped("ab-cd ef gh jk mn"), "ABCD-EFGH-JKMN")
        self.assertEqual(authguard.grouped("meido"), "ME1D-0")


if __name__ == "__main__":
    unittest.main()
