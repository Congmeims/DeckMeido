"""离线模式：一个开关，把所有「要离开这台机器」的请求快速挡回去。

范围只包括真正出网的调用：大模型 / 在线模型列表 / 更新检查 / 余额 / 模型下载 /
联网搜索与天气。回环地址与局域网私有地址视为本地，照常放行 —— 这是给
LM Studio / Ollama 这类本地模型留的可选插座，离线模式本身不依赖它。

开关关闭时所有判断都直接返回「在线」，行为与没有这个模块时完全一致。
"""
from __future__ import annotations

import ipaddress
import urllib.parse

# 界面上给用户看的短原因。不用「网络错误」这种词：它要能让人一眼看出
# 这是开关拦下的、不是网断了，也知道去哪儿关。
DEFAULT_REASON = (
    "离线模式已开启：该功能需要联网，已直接跳过（不会等待超时）。"
    "可在设置页关闭离线模式。"
)

# 不需要域名解析就能认出的本机名
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]"}

# 当前状态。只有一个布尔值，读写都在事件循环 / 工作线程里，赋值本身是原子的；
# 不做锁也够了，configure 只会被配置保存串行调用。
_state = {"enabled": False}


class OfflineError(RuntimeError):
    """离线模式下被主动拒绝的请求（继承 RuntimeError，兼容既有 except）。"""


def configure(cfg: dict | None) -> bool:
    """按 [runtime].offline_mode 更新开关（缺省 / 老配置 = 关闭）"""
    runtime = ((cfg or {}).get("runtime") or {})
    _state["enabled"] = bool(runtime.get("offline_mode", False))
    return _state["enabled"]


def enabled() -> bool:
    return _state["enabled"]


def set_enabled(on: bool) -> bool:
    """测试与内部脚本用；正常路径走 configure。"""
    _state["enabled"] = bool(on)
    return _state["enabled"]


def reason(feature: str = "") -> str:
    """给用户 / 模型看的可读原因。feature 例如「检查更新」「模型对话」。"""
    if not feature:
        return DEFAULT_REASON
    return (
        f"离线模式已开启，{feature}需要联网，已直接跳过（不会等待超时）。"
        "可在设置页关闭离线模式。"
    )


def is_local_url(url: str) -> bool:
    """目标是否在本机 / 局域网内（回环、私有网段、.local 域名）。

    解析不出主机名时按「不本地」处理：宁可多拦一次，也不要让离线模式
    因为一个畸形地址悄悄放过公网请求。
    """
    raw = str(url or "").strip()
    if not raw:
        return False
    try:
        host = (urllib.parse.urlsplit(raw).hostname or "").strip().lower()
    except ValueError:
        return False
    if not host:
        return False
    if host in _LOCAL_HOSTS or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(ip.is_loopback or ip.is_private or ip.is_link_local)


def check(url: str = "", feature: str = "") -> None:
    """离线模式下、且目标不本地时抛 OfflineError；其余情况立即返回。"""
    if not enabled():
        return
    if url and is_local_url(url):
        return
    raise OfflineError(reason(feature))
