"""请求来源校验：同源判断与 Sec-Fetch-Site 约定。

访问令牌已经下线，局域网鉴权改为「已配对设备的 HttpOnly Cookie」；
这里只保留 Cookie 认证需要的 CSRF 防护工具。
"""
from __future__ import annotations

from urllib.parse import urlsplit


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
