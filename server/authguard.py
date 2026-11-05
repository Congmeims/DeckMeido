"""访问令牌：生成与强度约定。

令牌在安装时随机生成，不再写死出厂口令。字母表去掉容易看错的
0/o/1/l/i；校验时不分大小写，全大写输入也能连上。
"""
from __future__ import annotations

import secrets

# 默认长度：16 位小写字母 + 数字，约有 2^79 种组合，足够挡住在线猜测
TOKEN_LEN = 16
ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"


def gen_token(length: int = TOKEN_LEN) -> str:
    """生成访问令牌，如 k7m2p9x4qw3n8r5t。"""
    return "".join(secrets.choice(ALPHABET) for _ in range(length))
