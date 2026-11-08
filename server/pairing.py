"""局域网设备配对：PIN 状态机、设备凭据与设备列表存储。

流程参考 Sunshine/Moonlight：
  1. 浏览器 POST /api/pair/start，拿到 pair_id（此时还没资格登录）；
  2. 掌机设置页看到待配对设备与 4 位 PIN，点「允许」并把 PIN 报给用户；
  3. 浏览器 POST /api/pair/confirm 提交 pair_id + pin；
  4. 校验通过后签发设备专属 Cookie，服务端只保存 token 的哈希。

安全约定：
  - PIN 一次性、60 秒有效、最多 3 次尝试；错满 3 次本次配对作废，
    需要重新发起配对（不按 IP 封禁）；
  - device token 只在下发那一刻存在于响应 Cookie 里，落盘的是 sha256；
  - 审计日志只记 IP/UA 摘要/结果，绝不记录 PIN 或 token。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

from . import audit, paths

PIN_TTL_S = 60.0
PIN_MAX_ATTEMPTS = 3
DEFAULT_DEVICE_DAYS = 30
COOKIE_NAME = "deckmeido_device"
MAX_PENDING = 8

STATE_PENDING = "pending"
STATE_APPROVED = "approved"
STATE_DENIED = "denied"
STATE_EXPIRED = "expired"
STATE_PAIRED = "paired"
# 错满 3 次：本次配对直接作废，浏览器必须重新发起（与「掌机拒绝」区分开，
# 前端据此收起 PIN 输入并引导重新配对）
STATE_FAILED = "failed"
_ACTIVE = (STATE_PENDING, STATE_APPROVED)


def token_hash(raw: str) -> str:
    """设备令牌落盘前的哈希；令牌本身是高熵随机串，不需要再加盐。"""
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def ua_summary(ua: str | None) -> str:
    """把一整条 User-Agent 压成一句可读的设备描述（最多 40 字）。"""
    text = (ua or "").strip()
    if not text:
        return "未知浏览器"
    browser = "浏览器"
    for key, name in (
        ("edg", "Edge"), ("chrome", "Chrome"), ("firefox", "Firefox"),
        ("safari", "Safari"), ("opera", "Opera"), ("vivaldi", "Vivaldi"),
    ):
        if key in text.lower():
            browser = name
            break
    system = ""
    for key, name in (
        ("iphone", "iPhone"), ("ipad", "iPad"), ("android", "Android"),
        ("windows", "Windows"), ("mac os", "macOS"), ("linux", "Linux"),
    ):
        if key in text.lower():
            system = name
            break
    label = f"{browser} / {system}" if system else browser
    return label[:40]


def _safe_mode(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _security_dir() -> Path:
    path = paths.data_dir() / "security"
    path.mkdir(parents=True, exist_ok=True)
    _safe_mode(path, 0o700)
    return path


class PairError(Exception):
    """配对失败：code 给接口映射 HTTP 状态，message 直接给用户看。"""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class DeviceStore:
    """devices.json 的读写：字段只含哈希，令牌明文不落盘。"""

    def __init__(self, path: Path | None = None, now=time.time):
        self._path = Path(path) if path else None
        self._now = now
        self._lock = threading.RLock()
        self._devices: list[dict] = []
        self._by_hash: dict[str, dict] = {}
        self._loaded = False
        self._persist_at: dict[str, float] = {}

    @property
    def path(self) -> Path:
        if self._path is not None:
            return self._path
        return _security_dir() / "devices.json"

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw or "{}")
        except FileNotFoundError:
            data = {}
        except Exception:
            # 文件损坏不能把所有人锁在外面：当成空列表，等下一次写入覆盖
            data = {}
        items = data.get("devices") if isinstance(data, dict) else None
        if not isinstance(items, list):
            items = []
        self._devices = [d for d in items if isinstance(d, dict) and d.get("device_id")]
        self._by_hash = {str(d.get("token_hash") or ""): d for d in self._devices
                         if d.get("token_hash")}

    def _save(self) -> None:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        _safe_mode(path.parent, 0o700)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"devices": self._devices}, ensure_ascii=False,
                                  indent=2), encoding="utf-8")
        _safe_mode(tmp, 0o600)
        os.replace(tmp, path)
        _safe_mode(path, 0o600)

    @staticmethod
    def _public(d: dict) -> dict:
        """去掉 token_hash，只给设置页需要的字段。"""
        return {
            "device_id": d.get("device_id", ""),
            "name": d.get("name", ""),
            "ip": d.get("ip", ""),
            "ua": d.get("ua", ""),
            "created_at": d.get("created_at", 0),
            "last_seen": d.get("last_seen", 0),
        }

    def create(self, name: str, ip: str, ua: str) -> tuple[dict, str]:
        with self._lock:
            self._load()
            token = secrets.token_urlsafe(32)
            now = self._now()
            device = {
                "device_id": secrets.token_hex(8),
                "token_hash": token_hash(token),
                "name": (name or ua_summary(ua))[:40],
                "ip": ip,
                "ua": (ua or "")[:300],
                "created_at": now,
                "last_seen": now,
            }
            self._devices.append(device)
            self._by_hash[device["token_hash"]] = device
            self._save()
            return self._public(device), token

    def authenticate(self, token: str | None) -> dict | None:
        """按哈希查设备；命中时节流更新 last_seen（最多每分钟落盘一次）。"""
        raw = (token or "").strip()
        if not raw:
            return None
        h = token_hash(raw)
        with self._lock:
            self._load()
            device = self._by_hash.get(h)
            if not device:
                return None
            now = self._now()
            device["last_seen"] = now
            last = self._persist_at.get(device["device_id"], 0)
            if now - last >= 60:
                self._persist_at[device["device_id"]] = now
                self._save()
            return self._public(device)

    def list(self) -> list[dict]:
        with self._lock:
            self._load()
            return [self._public(d) for d in
                    sorted(self._devices, key=lambda d: d.get("created_at", 0),
                           reverse=True)]

    def count(self) -> int:
        with self._lock:
            self._load()
            return len(self._devices)

    def rename(self, device_id: str, name: str) -> dict:
        clean = (name or "").strip()[:40]
        if not clean:
            raise PairError("bad_name", "设备名称不能为空", 400)
        with self._lock:
            self._load()
            for d in self._devices:
                if d.get("device_id") == device_id:
                    d["name"] = clean
                    self._save()
                    return self._public(d)
        raise PairError("not_found", "设备不存在或已被移除", 404)

    def remove(self, device_id: str) -> bool:
        with self._lock:
            self._load()
            before = len(self._devices)
            self._devices = [d for d in self._devices if d.get("device_id") != device_id]
            if len(self._devices) == before:
                return False
            self._by_hash = {str(d.get("token_hash") or ""): d for d in self._devices
                             if d.get("token_hash")}
            self._save()
            return True

    def remove_all(self) -> int:
        with self._lock:
            self._load()
            n = len(self._devices)
            self._devices = []
            self._by_hash = {}
            if n:
                self._save()
            return n


class PairingManager:
    """配对会话状态机；会话只在内存里，重启后重新配对即可。"""

    def __init__(self, device_path: Path | None = None, now=time.time):
        self._now = now
        self._lock = threading.RLock()
        self._sessions: dict[str, dict] = {}
        self.devices = DeviceStore(device_path, now=now)

    def _expire_locked(self) -> None:
        now = self._now()
        for s in self._sessions.values():
            if s["state"] in _ACTIVE and now >= s["expires"]:
                s["state"] = STATE_EXPIRED
                s["pin"] = ""
        # 已完成/失效的会话留 5 分钟供前端查最终状态，之后清掉
        for pid in [k for k, s in self._sessions.items()
                    if s["state"] not in _ACTIVE and now - s["expires"] > 300]:
            self._sessions.pop(pid, None)

    def start(self, ip: str, ua: str) -> dict:
        with self._lock:
            self._expire_locked()
            if len([s for s in self._sessions.values() if s["state"] in _ACTIVE]) >= MAX_PENDING:
                raise PairError("too_many", "待配对设备过多，请稍后再试", 409)
            pair_id = secrets.token_urlsafe(12)
            now = self._now()
            session = {
                "pair_id": pair_id,
                "pin": f"{secrets.randbelow(10000):04d}",
                "ip": ip,
                "ua": (ua or "")[:300],
                "name": ua_summary(ua),
                "created_at": now,
                "expires": now + PIN_TTL_S,
                "attempts": 0,
                "state": STATE_PENDING,
            }
            self._sessions[pair_id] = session
            audit.record("pair", "start", "ok", ip=ip, detail=session["name"])
            return self._public_session(session)

    def _public_session(self, s: dict, with_pin: bool = False) -> dict:
        out = {
            "pair_id": s["pair_id"],
            "state": s["state"],
            "ip": s["ip"],
            "ua": s["ua"],
            "name": s["name"],
            "attempts": s["attempts"],
            "created_at": s["created_at"],
            "expires_in": max(0, int(s["expires"] - self._now())),
        }
        if with_pin:
            out["pin"] = s["pin"]
        return out

    def status(self, pair_id: str) -> dict:
        with self._lock:
            self._expire_locked()
            s = self._sessions.get(pair_id)
            if not s:
                raise PairError("not_found", "配对请求不存在或已失效", 404)
            return self._public_session(s)

    def pending(self, include_finished: bool = False) -> list[dict]:
        """掌机设置页用：带 PIN、按创建时间升序。

        include_finished=True 时把刚结束（已拒绝 / 已过期 / 已作废 / 已配对）
        的会话也带回来：设置页点完「允许 / 拒绝」后要把结果留在原地，
        不能点完就从列表里消失（用户反馈「点完毫无变化」）。
        """
        with self._lock:
            self._expire_locked()
            return [self._public_session(s, with_pin=True)
                    for s in sorted(self._sessions.values(), key=lambda x: x["created_at"])
                    if include_finished or s["state"] in _ACTIVE]

    def refresh(self, pair_id: str) -> dict:
        with self._lock:
            self._expire_locked()
            s = self._sessions.get(pair_id)
            if not s:
                raise PairError("not_found", "配对请求不存在或已失效", 404)
            if s["state"] not in _ACTIVE:
                raise PairError("finished", "该配对请求已经结束", 409)
            s["pin"] = f"{secrets.randbelow(10000):04d}"
            s["expires"] = self._now() + PIN_TTL_S
            s["attempts"] = 0
            audit.record("pair", "refresh", "ok", ip=s["ip"], detail=s["name"])
            return self._public_session(s, with_pin=True)

    def decide(self, pair_id: str, allow: bool, host_ip: str = "") -> dict:
        with self._lock:
            self._expire_locked()
            s = self._sessions.get(pair_id)
            if not s:
                raise PairError("not_found", "配对请求不存在或已失效", 404)
            if s["state"] not in _ACTIVE:
                raise PairError("finished", "该配对请求已经结束", 409)
            s["state"] = STATE_APPROVED if allow else STATE_DENIED
            if not allow:
                s["pin"] = ""
            audit.record("pair", "allow" if allow else "deny", "ok",
                         ip=s["ip"], reason="host:" + (host_ip or "-"), detail=s["name"])
            return self._public_session(s, with_pin=allow)

    def confirm(self, pair_id: str, pin: str, ip: str = "") -> tuple[dict, str]:
        """校验 PIN 并创建设备，返回 (公开设备, 设备令牌)。"""
        entered = re.sub(r"\s+", "", pin or "")
        with self._lock:
            self._expire_locked()
            s = self._sessions.get(pair_id)
            if not s:
                raise PairError("not_found", "配对请求不存在或已失效", 404)
            if s["state"] == STATE_EXPIRED:
                audit.record("pair", "confirm", "fail", ip=ip, reason="expired",
                             detail=s["name"])
                raise PairError("expired", "PIN 已过期，请在掌机上刷新 PIN", 410)
            if s["state"] == STATE_DENIED:
                audit.record("pair", "confirm", "fail", ip=ip, reason="denied",
                             detail=s["name"])
                raise PairError("denied", "掌机已拒绝这次配对", 403)
            if s["state"] == STATE_FAILED:
                audit.record("pair", "confirm", "fail", ip=ip, reason="exhausted",
                             detail=s["name"])
                raise PairError("too_many", "PIN 错误次数过多，本次配对已作废，请重新发起配对", 409)
            if s["state"] == STATE_PAIRED:
                audit.record("pair", "confirm", "fail", ip=ip, reason="used",
                             detail=s["name"])
                raise PairError("used", "该配对请求已经使用过", 409)
            if s["state"] != STATE_APPROVED:
                audit.record("pair", "confirm", "fail", ip=ip, reason="not_approved",
                             detail=s["name"])
                raise PairError("not_approved", "请先在掌机上点「允许」", 409)
            if s["attempts"] >= PIN_MAX_ATTEMPTS:
                s["state"] = STATE_FAILED
                raise PairError("too_many", "PIN 错误次数过多，本次配对已作废，请重新发起配对", 409)
            if not entered or not hmac.compare_digest(s["pin"], entered):
                s["attempts"] += 1
                audit.record("pair", "confirm", "fail", ip=ip, reason="pin",
                             detail=s["name"])
                if s["attempts"] >= PIN_MAX_ATTEMPTS:
                    s["state"] = STATE_FAILED
                    s["pin"] = ""
                    raise PairError("too_many", "PIN 错误次数过多，本次配对已作废，请重新发起配对", 409)
                left = PIN_MAX_ATTEMPTS - s["attempts"]
                raise PairError("bad_pin", f"PIN 不正确，还可以尝试 {left} 次", 401)
            # PIN 一次性：成功后立即作废，避免同一请求被重放
            s["pin"] = ""
            s["state"] = STATE_PAIRED
            device, token = self.devices.create(s["name"], s["ip"], s["ua"])
            audit.record("pair", "confirm", "ok", ip=ip, detail=device.get("name", ""))
            return device, token


manager = PairingManager()
