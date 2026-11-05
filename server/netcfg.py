"""监听地址与端口的统一解析（后端、客户端、安装脚本共用同一优先级）。

端口：DECKMEIDO_PORT 环境变量 > config.toml [server].port > 18765。
地址：lan_enable = true 才监听 0.0.0.0，否则只监听 127.0.0.1。
"""
from __future__ import annotations

DEFAULT_PORT = 18765
LOOPBACK = "127.0.0.1"
ANY = "0.0.0.0"
MIN_PORT = 1024
MAX_PORT = 65535


def valid_port(value) -> int | None:
    """合法端口返回 int，否则 None（设置页低于 1024 一律拒绝）。"""
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if MIN_PORT <= n <= MAX_PORT else None


def resolve_port(env_value=None, cfg_server: dict | None = None) -> int:
    """按优先级解析端口：环境变量 > 配置 > 默认值。"""
    env_port = valid_port(env_value)
    if env_port is not None:
        return env_port
    cfg_port = valid_port((cfg_server or {}).get("port"))
    if cfg_port is not None:
        return cfg_port
    return DEFAULT_PORT


def lan_enabled(cfg_server: dict | None = None) -> bool:
    return bool((cfg_server or {}).get("lan_enable", False))


def resolve_host(cfg_server: dict | None = None) -> str:
    """lan_enable 关闭时只绑定回环，开启才允许局域网访问。"""
    return ANY if lan_enabled(cfg_server) else LOOPBACK


def is_loopback(ip: str | None) -> bool:
    """请求来源是否本机（127.0.0.1 / ::1 / 空来源都按本机处理）。"""
    value = (ip or "").strip()
    if not value:
        return True
    return value in ("127.0.0.1", "::1", "localhost") or value.startswith("127.")


# 鉴权免检只认真正的回环地址；空来源 / localhost 字符串一律不放行，
# 避免代理头缺失或伪造时把外部请求误当成本机。
LOCAL_ADDRS = ("127.0.0.1", "::1", "::ffff:127.0.0.1")


def is_local(ip: str | None) -> bool:
    """鉴权用的严格回环判断：只有 127.0.0.1 / ::1 免验证。"""
    return (ip or "").strip().lower() in LOCAL_ADDRS
