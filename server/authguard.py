"""访问令牌：生成、定长比较与强度约定。

令牌在安装时随机生成，不再写死出厂口令。字母表去掉容易看错的
0/o/1/l/i；校验时不分大小写，全大写输入也能连上。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time

# 默认长度：16 位小写字母 + 数字，约有 2^79 种组合，足够挡住在线猜测
TOKEN_LEN = 16
ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"


def gen_token(length: int = TOKEN_LEN) -> str:
    """生成访问令牌，如 k7m2p9x4qw3n8r5t。"""
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def norm(value: str | None) -> str:
    """令牌统一按小写比较，输入不分大小写，容忍首尾空白。"""
    return (value or "").strip().lower()


def token_eq(expected: str | None, provided: str | None) -> bool:
    """定长比较两个令牌，避免按字符或长度差异猜出接近的令牌。

    先各自取 sha256 再比较，这样无论输入多长，比较耗时都基本一致。
    """
    a = hashlib.sha256(norm(expected).encode()).digest()
    b = hashlib.sha256(norm(provided).encode()).digest()
    return hmac.compare_digest(a, b)


# 失败尝试限速：只统计「令牌不对」，正常请求不占额度。
# 同一 IP 在 5 分钟内失败 20 次后，后续请求一律 429，等窗口滑过去自动恢复。
FAIL_WINDOW_S = 300.0
FAIL_MAX = 20


class FailureLimiter:
    """按来源 IP 记录失败尝试，超过阈值就暂时拒绝。"""

    def __init__(self, window: float = FAIL_WINDOW_S, max_fails: int = FAIL_MAX):
        self.window = window
        self.max_fails = max_fails
        self._fails: dict[str, list[float]] = {}

    def _prune(self, ip: str, now: float) -> list[float]:
        recent = [t for t in self._fails.get(ip) or [] if now - t < self.window]
        if recent:
            self._fails[ip] = recent
        else:
            self._fails.pop(ip, None)
        return recent

    def blocked(self, ip: str) -> bool:
        if not ip:
            return False
        return len(self._prune(ip, time.monotonic())) >= self.max_fails

    def record(self, ip: str) -> None:
        if not ip:
            return
        now = time.monotonic()
        recent = self._prune(ip, now)
        recent.append(now)
        self._fails[ip] = recent

    def reset(self) -> None:
        self._fails.clear()


limiter = FailureLimiter()
