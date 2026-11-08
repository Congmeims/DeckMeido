"""离线模式：一个开关，把所有「要离开这台机器」的请求快速挡回去。

范围只包括真正出网的调用：大模型 / 在线模型列表 / 更新检查 / 余额 / 模型下载 /
联网搜索与天气。回环地址与局域网私有地址视为本地，照常放行 —— 这是给
LM Studio / Ollama 这类本地模型留的可选插座，离线模式本身不依赖它。

开关关闭时所有判断都直接返回「在线」，行为与没有这个模块时完全一致。
"""
from __future__ import annotations

import ipaddress
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

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


# ---------- 公网 / 假内网探测 ----------
# 目的：判断「现在到得了公网，还是只能到局域网」，给界面一个「要不要切离线模式」
# 的建议。这里只负责探测，不自动改开关：用户随时可以拒绝，手动开关始终是最高优先级。
#
# 轻量约束：
#   - 用两个公网小端点并行测，总预算 2 秒，超时即判失败；
#   - 不做大文件下载（204 / 首页就够，几乎不占流量）；
#   - 结果缓存一段时间，界面反复打开设置页不会反复出网。

PROBE_TIMEOUT = 2.0
PROBE_TARGETS = (
    # 国内可直连、响应很小的端点；国外同样能访问
    "https://www.baidu.com/",
    # 标准连通性探测端点，正常返回 204
    "https://connectivitycheck.gstatic.com/generate_204",
)
# 判为「公网可达」后缓存久一点；判为不通缓存短一点，网络恢复能尽快反映
_PROBE_TTL_ONLINE = 120.0
_PROBE_TTL_FAILED = 30.0

_probe_lock = threading.Lock()
_probe_cache: dict = {"result": None}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随重定向：门户劫持通常就是 302 到登录页，跟过去只会得到一个 200 假象。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_PROBE_OPENER = urllib.request.build_opener(_NoRedirect())


def _fetch(url: str, timeout: float, box: dict) -> None:
    """一个探测目标；结果写进 box。任何异常都不外抛，失败本身就是要的结果。"""
    try:
        req = urllib.request.Request(
            url, method="GET",
            headers={"User-Agent": "DeckMeido-NetProbe/1.0", "Cache-Control": "no-cache"})
        with _PROBE_OPENER.open(req, timeout=timeout) as resp:
            code = int(getattr(resp, "status", 0) or 0)
            if 200 <= code < 400:
                box["ok"] = True
    except urllib.error.HTTPError as exc:
        # 没跟随的重定向会走到这里：能拿到 3xx 说明至少连上了某个本地/门户服务
        if 300 <= exc.code < 400:
            box["redirect"] = True
    except Exception as exc:  # noqa: BLE001 - 探测失败原因不向外抛
        box.setdefault("errors", []).append(type(exc).__name__)


def _has_default_route() -> bool:
    """有没有一条默认路由（连上网线 / Wi-Fi / VPN 就算是）。

    UDP connect 只查路由表、不发包，因此「有局域网但公网不通」时它仍然成功，
    正好用来区分「假内网」和「完全没联网」。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 53))
        addr = (sock.getsockname() or ("",))[0]
        return bool(addr) and addr != "0.0.0.0"
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _probe_now(timeout: float = PROBE_TIMEOUT) -> dict:
    """真正做一次探测（并行 + 总超时），返回结果字典"""
    boxes: list[dict] = []
    threads: list[threading.Thread] = []
    for target in PROBE_TARGETS:
        box: dict = {}
        boxes.append(box)
        th = threading.Thread(target=_fetch, args=(target, timeout, box),
                              name="netprobe", daemon=True)
        th.start()
        threads.append(th)
    deadline = time.monotonic() + max(0.5, timeout) + 0.2
    for th in threads:
        left = deadline - time.monotonic()
        if left > 0:
            th.join(left)
    online = any(b.get("ok") for b in boxes)
    portal = any(b.get("redirect") for b in boxes)
    if online:
        state, detail = "online", "公网可达"
    elif _has_default_route():
        state = "lan_only"
        detail = ("只能连到局域网或需要网页登录（疑似假内网）"
                  if portal else "只能连到局域网，公网不可达")
    else:
        state, detail = "offline", "没有可用的网络连接"
    return {
        "state": state,
        "detail": detail,
        "portal": portal,
        "checked": time.time(),
        "cached": False,
        "timeout": round(timeout, 2),
    }


def probe(force: bool = False, timeout: float = PROBE_TIMEOUT) -> dict:
    """公网连通性探测；默认走缓存，force=True 强制重新出网。

    返回 {"state": online|lan_only|offline, "detail": ..., "portal": bool, ...}。
    这个函数只描述现状，不会动 offline_mode 开关。
    """
    now = time.time()
    with _probe_lock:
        cached = _probe_cache.get("result")
        if cached and not force:
            ttl = _PROBE_TTL_ONLINE if cached.get("state") == "online" else _PROBE_TTL_FAILED
            if now - float(cached.get("checked") or 0) < ttl:
                out = dict(cached)
                out["cached"] = True
                return out
        try:
            result = _probe_now(min(max(float(timeout), 0.5), PROBE_TIMEOUT))
        except Exception as exc:  # noqa: BLE001 - 探测绝不能把接口打崩
            result = {"state": "offline", "detail": f"探测失败：{type(exc).__name__}",
                      "portal": False, "checked": time.time(), "cached": False,
                      "timeout": round(float(timeout), 2)}
        _probe_cache["result"] = result
        return dict(result)
