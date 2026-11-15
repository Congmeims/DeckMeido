"""请求来源校验：同源判断（仅保留给仍需要的场合）。

局域网鉴权已改为「已配对设备的 Bearer 令牌」（见 server/pairing.py）：
令牌放在 Authorization 请求头里，浏览器不会自动携带，所以不再需要
Cookie 那套 CSRF 防护（same_origin / Sec-Fetch-Site）。
"""
from __future__ import annotations

from urllib.parse import urlsplit


def same_origin(origin: str | None, host: str | None) -> bool:
    """判断 Origin 是否与 Host 同源（无 Origin 视为通过）。"""
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
