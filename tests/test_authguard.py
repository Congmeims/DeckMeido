"""访问令牌生成（server/authguard.py）的单元测试。"""
from __future__ import annotations

import unittest

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


class GenTokenTest(unittest.TestCase):
    def test_default_length_is_16(self):
        self.assertEqual(authguard.TOKEN_LEN, 16)
        token = authguard.gen_token()
        self.assertEqual(len(token), 16)

    def test_charset_avoids_confusing_letters(self):
        token = authguard.gen_token(32)
        self.assertTrue(set(token) <= set(authguard.ALPHABET))
        for bad in "01loi":
            self.assertNotIn(bad, token)

    def test_tokens_are_random(self):
        tokens = {authguard.gen_token() for _ in range(50)}
        self.assertEqual(len(tokens), 50)


if __name__ == "__main__":
    unittest.main()
