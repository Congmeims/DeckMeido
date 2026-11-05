"""访问令牌：生成、定长比较与强度约定。

令牌在安装时随机生成，不再写死出厂口令。采用 Crockford Base32 字母表
（0-9 与 A-Z 去掉 I/L/O/U），生成与校验都按这套规则归一化；校验时不分
大小写，带连字符或空格的粘贴内容也能直接通过。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from urllib.parse import urlsplit

# 默认长度：12 位 Crockford Base32，约 60 bit（32^12 ≈ 2^60），
# 足够挡住在线猜测，手抄与口述也不至于太长。
TOKEN_LEN = 12
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def gen_token(length: int = TOKEN_LEN) -> str:
    """生成访问令牌（存储为小写），如 7k3m9x4q2n8r。"""
    return "".join(secrets.choice(ALPHABET) for _ in range(length)).lower()


def norm(value: str | None) -> str:
    """令牌归一化：去空格/连字符、转大写、I/L→1、O→0，最后统一小写。

    生成端与校验端都用这一套规则，所以粘贴 ``XXXX-XXXX-XXXX`` 或手抄时
    把 0 写成 O、1 写成 I/L 都能通过。
    """
    s = "".join(ch for ch in (value or "").upper() if not ch.isspace() and ch != "-")
    s = s.replace("I", "1").replace("L", "1").replace("O", "0")
    return s.lower()


def grouped(value: str | None, size: int = 4) -> str:
    """展示用分组（大写）：每 ``size`` 位一组用连字符分开，如 ABCD-EFGH-JKMN。"""
    t = norm(value).upper()
    return "-".join(t[i:i + size] for i in range(0, len(t), size))


def token_eq(expected: str | None, provided: str | None) -> bool:
    """定长比较两个令牌，避免按字符或长度差异猜出接近的令牌。

    先各自取 sha256 再比较，这样无论输入多长，比较耗时都基本一致。
    归一化对两端同样生效，旧令牌（meido / 16 位 / 自定义）照旧可用。
    """
    a = hashlib.sha256(norm(expected).encode()).digest()
    b = hashlib.sha256(norm(provided).encode()).digest()
    return hmac.compare_digest(a, b)


def same_origin(origin: str | None, host: str | None) -> bool:
    """Cookie 认证的写操作要求同源，挡住跨站表单/脚本。

    浏览器发跨站请求一定会带 Origin；命令行/旧客户端通常没有 Origin，
    这时交给 Sec-Fetch-Site 判断（见 same_site）。
    """
    value = (origin or "").strip()
    if not value:
        return True
    target = (host or "").strip()
    if not target:
        return False
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and parts.netloc.lower() == target.lower()


def same_site(value: str | None) -> bool:
    """Sec-Fetch-Site 只允许 same-origin / none（直接输入地址）。"""
    site = (value or "").strip().lower()
    return not site or site in ("same-origin", "none")
