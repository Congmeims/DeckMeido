"""DeckMeido 的 Decky 插件后端。

插件以 root 身份加载（见 plugin.json 的 flags），只做三件事：
1. 从 gamescope 的 PipeWire 节点抓一帧当前游戏画面
2. 转发 QAM 和本机 :18765 之间的对话（包括流式增量）
3. 告诉 QAM 自己能不能用（后端在不在、配置文件读不读得到、画面能不能抓）

推理逻辑一行都不在这里：全部复用 DeckMeido 已有的后端，
插件只是"截图 + 转发 + 显示"。

前端不直连后端，而是调这里的 start_turn/wait_final/cancel，流式内容通过
`deckmeido_delta` 事件回推。这样 QAM 侧不用管 CEF 的 CSP 会不会拦 ws。
"""

import asyncio
import base64
import inspect
import itertools
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
import tomllib
from pathlib import Path
from urllib.parse import quote

import decky

# websockets 不在 Decky 自带的 python 环境里，但前端不直连后端就必须靠它转发。
# 装了就用它，没装则退到标准库的原始握手实现（见 _RawWSClient），功能一致。
try:
    from websockets.asyncio.client import connect as _ws_connect
except ImportError:
    try:
        from websockets import connect as _ws_connect
    except ImportError:
        _ws_connect = None


def _find_install_dir() -> Path:
    """找 DeckMeido 的安装目录（里面要有 server/main.py）。

    插件自己装在 ~/homebrew/plugins/DeckMeido，和后端不是同一个目录，
    所以不能靠 __file__ 往上推，只能按已知位置挨个试。
    """
    homes = ["/home/deck"]
    try:
        # root 下 Path.home() 是 /root，这里枚举 home 目录下的真实用户
        homes += [str(p) for p in Path("/home").iterdir() if p.is_dir()]
    except Exception:
        pass
    for home in dict.fromkeys(homes):
        for name in ("deckmeido", "DeckMeido", "SDagent", "sdagent"):
            cand = Path(home) / name
            if (cand / "server" / "main.py").exists():
                return cand
    return Path("/home/deck/deckmeido")


INSTALL_DIR = _find_install_dir()


def _data_dir() -> Path:
    """数据根：DECKMEIDO_DATA_DIR > <安装目录>/userdata/（与客户端、后端一致）"""
    env = (os.environ.get("DECKMEIDO_DATA_DIR") or "").strip()
    if env:
        return Path(env).expanduser()
    return INSTALL_DIR / "userdata"


def _config_paths() -> list[Path]:
    """配置路径：DECKMEIDO_DATA_DIR > <安装目录>/userdata/"""
    return [_data_dir() / "config.toml"]


def _config_path() -> Path:
    """第一个存在的配置；都不存在时返回首选路径用于报错"""
    candidates = _config_paths()
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


def _workspace() -> str:
    """这一轮的工作区：配置里的 [workspace].dir，和网页设置页那一项同源

    以前写死 /home/deck，装到别处或别的用户下时后端会把工作区边界判到一个
    不存在（或无权限）的目录上，工具全部报错。没配就不发，让后端用自己的默认值。
    """
    try:
        cfg = tomllib.loads(_config_path().read_text(encoding="utf-8"))
        return str((cfg.get("workspace") or {}).get("dir") or "").strip()
    except Exception:
        return ""

# 面板底部显示版本号：优先用后端 config 里报的版本（和网页设置页一致），
# 后端连不上时退回插件自己的 package.json
_PLUGIN_VERSION = "0.6.11"
try:
    _pkg = json.loads((Path(__file__).resolve().parent / "package.json").read_text(encoding="utf-8"))
    _PLUGIN_VERSION = str(_pkg.get("version") or _PLUGIN_VERSION)
except Exception:
    pass


_SERVER_CFG_ERROR = ""
_SERVER_CFG_LOGGED = ""


def _resolve_port(server: dict) -> dict:
    """端口优先级：DECKMEIDO_PORT > config.toml [server].port > 18765。

    直接在返回的 [server] 字典里归一，所有 int(cfg.get("port", ...)) 的调用点
    自动拿到同一个值，避免「状态页显示一个端口、健康检查问另一个端口」。
    """
    for candidate in (os.environ.get("DECKMEIDO_PORT"), server.get("port")):
        try:
            port = int(str(candidate).strip())
        except (TypeError, ValueError):
            continue
        if 1024 <= port <= 65535:
            server["port"] = port
            return server
    server["port"] = 18765
    return server


def _load_server_cfg() -> dict:
    """读后端的 [server] 段（端口 / 局域网开关）

    读不到或解析失败时把错误原文（带行号）留在 _SERVER_CFG_ERROR，
    由 get_status 的错误文案和日志原样带出。
    """
    global _SERVER_CFG_ERROR, _SERVER_CFG_LOGGED
    path = _config_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        _SERVER_CFG_ERROR = f"找不到配置文件 {path}"
    except OSError as exc:
        _SERVER_CFG_ERROR = f"读不了配置文件 {path}：{exc}"
    else:
        try:
            cfg = tomllib.loads(raw)
        except Exception as exc:
            _SERVER_CFG_ERROR = f"配置文件解析失败 {path}：{exc}"
        else:
            if _SERVER_CFG_ERROR:
                _SERVER_CFG_ERROR = ""
                _SERVER_CFG_LOGGED = ""
            return _resolve_port(cfg.get("server") or {})
    # 同一段错误只写一次日志，get_status 每秒都会读配置，别刷屏
    if _SERVER_CFG_ERROR and _SERVER_CFG_ERROR != _SERVER_CFG_LOGGED:
        decky.logger.error(_SERVER_CFG_ERROR)
        _SERVER_CFG_LOGGED = _SERVER_CFG_ERROR
    return {}


def _cfg_error_text() -> str:
    """配置文件读不到 / 写坏时的说明"""
    return _SERVER_CFG_ERROR or "配置里没有 [server] 段"


def _gamepad_from_core(core: dict) -> dict:
    """后端 /api/status 的手柄可用性 → 面板字段。

    ok=None 表示后端没给这个字段（旧版本或后端没连上），前端据此显示「未知」，
    绝不能默认可用于是给出一个错误绿灯。
    """
    data = (core or {}).get("data") or {}
    ok = data.get("gamepad_ok")
    return {"ok": ok if isinstance(ok, bool) else None,
            "reason": str(data.get("gamepad_reason") or "")}


# ---------- 插件状态目录 ----------
#
# 插件以 root 加载，但本机（QAM / 大屏）不再单独设解锁门：能加载插件就视为
# 已解锁，插件与后端同机，走回环连接免鉴权（不再需要令牌）。
# 这里只保留设备标识的落盘目录（decky.DECKY_PLUGIN_SETTINGS_DIR），
# 老版本 Decky 没有该属性时退回 ~/.deckmeido。

_STATE_DIR: Path | None = None


def _state_dir() -> Path:
    """插件状态目录（设备标识等），不存放任何鉴权凭据"""
    global _STATE_DIR
    if _STATE_DIR is not None:
        return _STATE_DIR
    cand = ""
    try:
        cand = str(getattr(decky, "DECKY_PLUGIN_SETTINGS_DIR", "") or "")
    except Exception:
        cand = ""
    if not cand:
        cand = str(Path.home() / ".deckmeido")
    p = Path(cand)
    try:
        p.mkdir(parents=True, exist_ok=True)
        os.chmod(p, 0o700)
    except Exception:
        pass
    _STATE_DIR = p
    return _STATE_DIR


def _qam_device_id() -> str:
    """本面板的设备标识，和网页端的 DEVICE_ID 是同一个用途

    后端用 (client, device) 判断「这条待确认操作是不是你自己提的」，
    插件端原先只发了 client=qam 不带 device，于是 pending_action 永远
    匹配不上：在面板上回「确认」，模型会把确认提示语再问一遍。
    这里和设备标识放同一个目录，装在这个 Deck 上就一直是同一个值。
    """
    path = _state_dir() / "device_id"
    try:
        val = path.read_text(encoding="utf-8").strip()
        if val:
            return val
    except OSError:
        pass
    val = f"qam-{secrets.token_hex(8)}"
    try:
        path.write_text(val, encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError:
        pass
    return val


_QAM_DEVICE = _qam_device_id()


# ---------- 核心停止标记 ----------
#
# 「关闭核心」之后要求保持停止：插件加载时的自动拉起（_autostart_backend）
# 必须先看这个标记，有标记就跳过。只有用户显式点「启动」（start_backend /
# core_start）才清掉它。标记放在数据根的 core.stopped，插件（root）和桌面
# 客户端的 watchdog 都能看到，Decky / Steam 重启后仍然生效。

def _core_stopped_path() -> Path:
    # 和桌面客户端的 watchdog 共用这一个标记：放在数据根里 root / deck 都能 stat。
    # 存在 = 用户点过「关闭核心」，两条自动拉起路径（插件 _autostart_backend、
    # 客户端 watchdog）都必须跳过；只有显式「启动」才删掉它。
    return _data_dir() / "core.stopped"


def _core_stopped() -> bool:
    try:
        return _core_stopped_path().exists()
    except Exception:
        return False


def _mark_core_stopped(stopped: bool) -> None:
    path = _core_stopped_path()
    try:
        if stopped:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
            os.chmod(path, 0o600)
            if os.getuid() == 0:
                # 插件是 root，标记要交给 deck，否则客户端 / 后端那边删不掉
                os.chown(path, _target_uid(), -1)
        else:
            path.unlink(missing_ok=True)
    except Exception as exc:
        decky.logger.warning(f"写入核心停止标记失败：{exc}")


# ---------- 当前在玩的游戏 ----------
#
# 查攻略时「哪个游戏」比画面本身还关键：认错了游戏，搜回来的攻略全废。
# 所以不靠模型猜，按可靠度从高到低自己找：
# 1. 运行中进程的 SteamAppId 环境变量 —— Steam 启动游戏时会注入，最准
# 2. 按 appid 去 Steam 自己的配置里取名字 —— Steam 主页（按 Steam 键弹出的
#    界面）左上角显示的就是这个字段，所以名字必然和菜单上一致：
#    Steam 游戏在 localconfig.vdf 的 apps.<appid>.name（用户在库里改的备注也是它），
#    非 Steam 游戏在 shortcuts.vdf 的 AppName
# 3. 非 Steam 游戏的 SteamAppId 是 0，用 compatdata 目录名当编号再查（见下）
# 4. 都拿不到就交给模型看画面
#
# 没走「读菜单上的字」那条路：菜单是 gamescope 合成层，抓帧要把 QAM 收起来
# 而且得等动画，拿到一屏图像还得做 OCR 才有一行文本；直接读配置文件
# 是同一份数据源，更快也更准。


def _read_vdf_apps(path: Path) -> dict[str, str]:
    """从 localconfig.vdf 里抽出 apps { appid: name }

    VDF 是嵌套的花括号结构，app 段里还夹着 cloud 之类的小块，
    所以不能按「遇到第一个 } 就算完」来切，得做括号配对。
    """
    try:
        text = path.read_text(errors="ignore")
    except Exception:
        return {}
    apps: dict[str, str] = {}

    head = re.search(r'"apps"\s*\{', text)
    if not head:
        return apps
    depth, i = 1, head.end()
    while i < len(text) and depth:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    seg = text[head.end():i - 1]

    for km in re.finditer(r'"(\d{2,10})"\s*\{', seg):
        j, d = km.end(), 1
        while j < len(seg) and d:
            if seg[j] == "{":
                d += 1
            elif seg[j] == "}":
                d -= 1
            j += 1
        nm = re.search(r'"name"\s+"((?:[^"\\]|\\.)*)"', seg[km.end():j - 1])
        if nm:
            apps[km.group(1)] = nm.group(1)
    return apps


# 二进制 VDF（shortcuts.vdf）的类型字节
_BVDict_NEST, _BVDict_STR, _BVDict_INT, _BVDict_END = 0x00, 0x01, 0x02, 0x08


def _read_shortcuts(path: Path) -> dict[str, str]:
    """从 shortcuts.vdf（二进制）里抽出「添加的非 Steam 游戏」appid → AppName

    这个文件和 localconfig.vdf 不是一回事：它是二进制 VDF，
    类型字节 + 以 \\0 结尾的键名，值再按类型读。结构详见
    Valve 开发者 Wiki 的 Steam Library Shortcuts 条目：
    root「shortcuts」→ 若干「0/1/2…」→ 每项里有 appid(int32) 和 AppName(string)。

    库里改过的名字就是这里的 AppName，Steam 界面显示的就是它。
    注意改过名之后 appid 不会重算（还是当初 crc32(原名+exe) 的值），
    所以只能按 appid 精确匹配查 AppName，反推是行不通的。
    """
    try:
        data = path.read_bytes()
    except Exception:
        return {}
    out: dict[str, str] = {}

    def cstr(pos: int) -> tuple[str, int]:
        end = data.index(b"\0", pos)
        return data[pos:end].decode("utf-8", "replace"), end + 1

    def skip_dict(pos: int) -> int:
        while pos < len(data):
            t = data[pos]
            pos += 1
            if t == _BVDict_END:
                return pos
            key, pos = cstr(pos)
            if t == _BVDict_NEST:
                pos = skip_dict(pos)
            elif t == _BVDict_STR:
                _, pos = cstr(pos)
            elif t == _BVDict_INT:
                pos += 4
            else:
                return pos
        return pos

    try:
        pos = 0
        if data[pos] != _BVDict_NEST:
            return {}
        pos += 1
        root, pos = cstr(pos)
        if root != "shortcuts":
            return {}
        while pos < len(data):
            t = data[pos]
            pos += 1
            if t == _BVDict_END:
                break
            _, pos = cstr(pos)  # "0"/"1"/…
            appid, name = "", ""
            while pos < len(data):
                t2 = data[pos]
                pos += 1
                if t2 == _BVDict_END:
                    break
                k, pos = cstr(pos)
                if t2 == _BVDict_NEST:
                    pos = skip_dict(pos)
                elif t2 == _BVDict_STR:
                    v, pos = cstr(pos)
                    if k == "AppName":
                        name = v
                elif t2 == _BVDict_INT:
                    raw = data[pos:pos + 4]
                    pos += 4
                    if k == "appid" and len(raw) == 4:
                        # 有符号存的，Steam 界面按无符号十位显示
                        appid = str(int.from_bytes(raw, "little", signed=False))
                else:
                    break
            if appid and name:
                out[appid] = name
    except (ValueError, IndexError):
        # 文件被 Steam 写了一半或格式不认识：能捞多少算多少，别炸
        pass
    return out


_LOG_TS_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")


def _log_ts(line: str) -> float:
    """console_log 行首的 [YYYY-MM-DD HH:MM:SS] 转时间戳；解析不出返回 0"""
    m = _LOG_TS_RE.search(line)
    if not m:
        return 0.0
    try:
        return time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return 0.0


def _steam_running_from_tail(tail: str, names: dict[str, str]) -> tuple[str, str, float] | None:
    """从 console_log 尾部解析「最后一次客户端启动之后」仍在跑的游戏。

    Steam 每次启动客户端都会写一行 "Client version:"。账本里的 added/removed
    只在同一次客户端运行期间有意义：一次启动前留下的 added 很可能早已退出，
    甚至换过一个游戏，却被旧实现当成「当前正在玩」。所以必须先找到最后一条
    Client version:，把它之前的事件全部作废，只统计之后的部分。
    返回 (appid, name, added 时间戳)；没有可信结果返回 None。
    """
    if not names:
        return None
    running: list[tuple[str, float]] = []
    seen_client = False
    for line in tail.splitlines():
        if "Client version:" in line:
            # 新一次客户端启动：前面的账本作废
            running.clear()
            seen_client = True
            continue
        m = re.search(r"AppID (\d+)", line)
        if not m:
            continue
        appid = m.group(1)
        ts = _log_ts(line)
        if "Game process added" in line:
            # 同一款重启一次会再 added 一遍，去重免得越堆越多
            running = [(a, t) for a, t in running if a != appid]
            running.append((appid, ts))
        elif "Game process removed" in line:
            running = [(a, t) for a, t in running if a != appid]
    if not seen_client:
        # 尾部没有客户端启动边界：无法判断这些 added 属于哪一次运行，宁可不报
        return None
    # 最新启动的那款优先：启动器可能带出两层进程
    for appid, ts in reversed(running):
        if names.get(appid):
            return appid, names[appid], ts
    return None


def _steam_running_app(names: dict[str, str]) -> tuple[str, str, float] | None:
    """Steam 账本里当前在跑的那款，返回 (appid, name, added_ts)；没有则 None

    进程表里查不到 SteamAppId 时用它（Linux 原生版、启动器套娃的那类）。
    localconfig 的 LastPlayed 只有退出后才更新，所以不能拿它当「正在运行」。
    console_log.txt 里成对记着 "Game process added ... AppID <n>" 与
    "Game process removed ... AppID <n>"，按最后一条 Client version: 之后的
    顺序跑一遍，剩下没被移除的就是此刻在跑的那款——这是 Steam 自己的账本，
    比按画面猜靠谱。
    """
    if not names:
        return None
    for steam in _steam_homes(INSTALL_DIR.parent):
        log = steam / "logs" / "console_log.txt"
        try:
            # 只读尾部：这个日志能到几十 MB，整份读进内存在掌机上太贵
            size = log.stat().st_size
            with log.open("rb") as f:
                f.seek(max(0, size - 400_000))
                tail = f.read().decode("utf-8", "replace")
        except Exception:
            continue
        hit = _steam_running_from_tail(tail, names)
        if hit:
            return hit
    return None


def _steam_homes(home: Path) -> list[Path]:
    """Steam 安装目录的候选位置

    Deck 上 ~/.steam/steam 是个软链，正常都在，但装到别处（或改了用户目录）时
    会落在 XDG 的默认位置，所以多列几处。
    """
    return [
        home / ".steam/steam",
        home / ".local/share/Steam",
        home / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
    ]


def _all_userdata() -> list[Path]:
    """所有 Steam 账号的 userdata 目录

    Steam 主页左上角那个游戏名，读的就是这套数据：Steam 游戏走
    localconfig.vdf 的 apps.<appid>.name，非 Steam 游戏走 shortcuts.vdf 的
    AppName。所以直接把这两处拼起来，等于拿到和菜单上一样的名字，
    不用去截菜单画面或读屏幕文字。
    """
    home = INSTALL_DIR.parent
    out: list[Path] = []
    for steam in _steam_homes(home):
        ud = steam / "userdata"
        if ud.is_dir():
            out.extend(p for p in ud.iterdir() if p.is_dir())
    # install.sh 给 deckmeido 建了桌面快捷方式，说明这台机器的 XDG 用户目录
    # 被指到别处了（比如 Documents/deckmeido），Steam 也可能跟着挪了窝
    try:
        docs = (home / ".config/user-dirs.dirs").read_text(errors="ignore")
        m = re.search(r'XDG_DOCUMENTS_DIR="([^"]+)"', docs)
        if m:
            nav = Path(m.group(1).replace("$HOME", str(home))) / "steam/nav"
            out.extend(p for p in nav.iterdir() if p.is_dir())
    except Exception:
        pass
    return out


def _app_names() -> dict[str, str]:
    """汇总所有 Steam 账号的 appid → 名字（含非 Steam 游戏的备注名）"""
    out: dict[str, str] = {}
    for ud in _all_userdata():
        cfg = ud / "config" / "localconfig.vdf"
        for k, v in _read_vdf_apps(cfg).items():
            out.setdefault(k, v)
        # 非 Steam 游戏只存在 shortcuts.vdf 里，localconfig 里没有它们
        for k, v in _read_shortcuts(ud / "config" / "shortcuts.vdf").items():
            out.setdefault(k, v)
    return out


def _compat_app_id(compat_path: Path) -> list[str]:
    """由 compatdata/<编号> 目录推出可能的 appid 写法

    非 Steam 游戏没有商店 appid，Steam 拿 shortcuts.vdf 里那个 int32 当它的编号，
    界面按无符号显示成十位数字（如 2914928358），compatdata 目录名也是它。
    但目录名到底写成有符号还是无符号，不同 Steam 版本还没统一，
    所以两种都返回，交给 _app_names() 去命中。
    """
    name = compat_path.name.strip()
    if not name.lstrip("-").isdigit():
        return []
    n = int(name)
    out = [name]
    if n < 0:  # 写成了负数（-1380038938），换回无符号十位再试一次
        alt = n & 0xFFFFFFFF
        if str(alt) not in out:
            out.append(str(alt))
    return out


def _running_app_ids() -> list[str]:
    """扫 /proc 找 SteamAppId，排除 Steam 自己和 gamescope

    只读 environ，不读 cmdline：命令行里常带 store 页面之类的 appid，会把结果带偏。
    非 Steam 游戏的 SteamAppId 是 0，只能靠 STEAM_COMPAT_DATA_PATH 的目录名认。
    """
    ids: list[str] = []
    me = os.getpid()
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except Exception:
        return ids
    for pid in pids:
        if int(pid) == me:
            continue
        try:
            env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        except Exception:
            continue
        compat = ""
        for kv in env:
            if kv.startswith(b"SteamAppId="):
                val = kv.split(b"=", 1)[1].decode(errors="ignore").strip()
                if val and val != "0":
                    ids.append(val)
            elif kv.startswith(b"STEAM_COMPAT_DATA_PATH="):
                compat = kv.split(b"=", 1)[1].decode(errors="ignore").strip()
        if compat:
            # 变量指向 .../compatdata/<编号>，有时后面还跟着 /pfx
            ids.extend(_compat_app_id(Path(compat.rstrip("/").removesuffix("/pfx"))))
    return ids



def _current_game() -> dict:
    """当前正在运行的游戏：{appid, name, source, confidence, ts}

    两条路取同一个事实，不是靠画面猜：
    1. 进程环境变量里的 SteamAppId / STEAM_COMPAT_DATA_PATH —— Proton 游戏都带；
    2. Steam 日志里的 "Game process added / removed" 账本 —— 原生 Linux 版和
       启动器套娃那类没有第 1 条的，靠这条。
    账本只统计「最后一次 Client version: 之后」的事件；进程表为空且账本里
    没有本次客户端启动后的 added 时，直接返回空，不再把上一局游戏当成当前游戏。
    """
    names = _app_names()
    # 同一个游戏会有好几个子进程，环境变量一致；取第一个能查到名字的。
    # 非 Steam 游戏（库里那个十位编号）就走这条路：compatdata 目录名
    # 命中 shortcuts.vdf 的 appid，拿到的是用户在库里改过的 AppName。
    for appid in _running_app_ids():
        if names.get(appid):
            return {"appid": appid, "name": names[appid],
                    "source": "proc", "confidence": "high", "ts": time.time()}
    # 进程表里没有线索：用 Steam 自己的运行记录（已按客户端会话边界截断）
    hit = _steam_running_app(names)
    if hit:
        appid, name, ts = hit
        return {"appid": appid, "name": name,
                "source": "log", "confidence": "high", "ts": ts or time.time()}
    return {"appid": "", "name": "", "source": "none",
            "confidence": "none", "ts": 0}


# ---------- 身份与运行目录 ----------
#
# 游戏模式下抓帧必须站在 gamescope 所属的普通用户那边做（root 连不上它的
# PipeWire socket），所以这里的命令一律以 deck 用户身份跑。

def _target_uid() -> int:
    """要降权到哪个 uid：优先 /home/deck 的属主，退到 1000"""
    try:
        return Path("/home/deck").stat().st_uid
    except Exception:
        return 1000


_RUN_AS: list[str] | None = None


def _run_as() -> list[str]:
    """以 deck 用户跑命令的前缀；自己已经是 deck 就是空列表"""
    global _RUN_AS
    if _RUN_AS is not None:
        return _RUN_AS
    if os.getuid() == _target_uid():
        _RUN_AS = []
        return _RUN_AS
    if os.getuid() != 0:
        # 既不是 root 也不是 deck（比如开发机），降权无从谈起
        _RUN_AS = []
        return _RUN_AS
    user = "deck"
    if shutil.which("runuser"):
        _RUN_AS = ["runuser", "-u", user, "--"]
    elif shutil.which("sudo"):
        _RUN_AS = ["sudo", "-u", user, "--"]
    else:
        decky.logger.warning("既没有 runuser 也没有 sudo，只能以当前身份抓帧")
        _RUN_AS = []
    return _RUN_AS


def _plugin_dir() -> Path:
    """插件代码目录（~/homebrew/plugins/DeckMeido）"""
    try:
        cand = str(getattr(decky, "DECKY_PLUGIN_DIR", "") or "")
        if cand:
            return Path(cand)
    except Exception:
        pass
    return Path(__file__).resolve().parent


_OWNER_CHECKED = False


def _restore_owner():
    """把插件目录的属主还给 deck。

    Decky 以 root 加载插件，插件目录是 deck 的仓库软链接，root 在这里写
    __pycache__、票据之后，deck 用户再 `git pull` 就会报
    `unable to unlink old 'plugin/main.py': Permission denied`。
    这里在首次落盘时纠一次，代价是一次 stat 遍历。
    """
    global _OWNER_CHECKED
    if _OWNER_CHECKED or os.getuid() != 0:
        return
    _OWNER_CHECKED = True
    uid = _target_uid()
    root = _plugin_dir()
    try:
        if root.stat().st_uid != uid:
            os.chown(root, uid, -1)
        for child in root.rglob("*"):
            if child.stat().st_uid != uid:
                os.chown(child, uid, -1)
    except (OSError, AttributeError):
        pass


def _runtime_dir() -> str:
    """gamescope 的 PipeWire socket 在 /run/user/<deck uid> 下"""
    return f"/run/user/{_target_uid()}"


# 截屏落盘的目录：QAM 面板只拿路径，图不经过 Decky 的 IPC
_SHOT_DIR = Path("/tmp/deckmeido_shots")


def _make_thumb(src: Path, width: int = 480) -> str:
    """把原图缩成一张小 JPEG 的 base64，只给面板预览用

    原图 1MB 左右，面板要的只是「这是哪一帧」。缩到 480 宽后大约 40KB，
    走 Decky 的调用通道没有压力。机器上没有转换工具就返回空串，
    预览不显示，但不影响提问。
    """
    # 缩略图必须另起名字，不能拿原图改后缀：原图本身就是 .jpg，
    # with_suffix(".jpg") 会返回原图自己的路径，filesink 一开就把原图截断成 0 字节，
    # 之后 start_turn 读到的就是空文件（表现为「发了但没带图」）
    out = src.with_name(src.stem + "_thumb.jpg")
    for cmd in (["gst-launch-1.0"], ["ffmpeg", "-y"]):
        exe = shutil.which(cmd[0]) or (f"/usr/bin/{cmd[0]}" if Path(f"/usr/bin/{cmd[0]}").exists() else None)
        if not exe:
            continue
        if cmd[0] == "ffmpeg":
            argv = [exe, "-y", "-loglevel", "error", "-i", str(src),
                    "-vf", f"scale={width}:-1", str(out)]
        else:
            # 输入可能是 JPEG 也可能是 PNG，解码交给 decodebin，别写死 pngdec。
            # 显式给 height 并把 JPG 质量压到 80：只写 width=480 时 videoscale
            # 会把高度留成 800，出来的缩略图有 400KB 以上，白白撑大返回值
            argv = [exe, "-q", "filesrc", f"location={src}", "!", "decodebin", "!",
                    "videoscale", "!", "videoconvert", "!",
                    "video/x-raw,width=480,height=300", "!",
                    "jpegenc", "quality=80", "!", "filesink", f"location={out}"]
        try:
            subprocess.run(argv, capture_output=True, timeout=20, check=True)
            if out.exists() and out.stat().st_size > 0:
                data = base64.b64encode(out.read_bytes()).decode("ascii")
                out.unlink(missing_ok=True)
                return data
        except Exception:
            continue
    return ""


def _backend_log_fd() -> int:
    """后端 stdout/stderr 的去处：和客户端一样写 backend.log，不丢

    以前插件拉起后端只能 DEVNULL，崩了连 traceback 都没有；这里照
    client/main.py 的做法留一代备份，超过 5MB 就滚动。
    """
    log = _data_dir() / "logs" / "backend.log"
    _restore_owner()
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(log.parent, 0o700)
        if log.exists() and log.stat().st_size > 5 * 1024 * 1024:
            os.replace(log, log.with_name("backend.log.1"))
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        if os.getuid() == 0:
            # 插件是 root，日志要交给 deck，客户端下次追加时才写得动
            os.chown(log.parent, _target_uid(), -1)
            os.chown(log, _target_uid(), -1)
        os.write(fd, f"\n===== backend start {time.strftime('%Y-%m-%d %H:%M:%S')} (plugin) =====\n".encode())
        return fd
    except Exception:
        return -3  # subprocess.DEVNULL


async def _start_backend(port: int) -> tuple[bool, str]:
    """拉起后端，轮询到它应答为止；第二个返回值是失败原因

    以 deck 身份起（插件本身是 root，直接用 root 跑后端会让它生成的文件
    全归 root，下次客户端再拉就写不动了）。后端自己有单实例守卫
    （server/singleton.py），重复拉起的那份会自己退出，不会抢端口。
    """
    py = INSTALL_DIR / ".venv" / "bin" / "python"
    if not py.exists():
        return False, f"找不到 {py}"
    try:
        fd = _backend_log_fd()
        # XDG_RUNTIME_DIR 不能指望从 Decky 继承下来：插件是系统服务起的，
        # 环境里没有这个变量，后端的 pw-record（语音录音）和 PipeWire 抓帧都得靠它
        kwargs: dict = {
            "stdout": fd,
            "stderr": fd,
            "env": {**os.environ, "XDG_RUNTIME_DIR": _runtime_dir()},
        }
        if not _run_as():
            kwargs["start_new_session"] = True
        try:
            # 绑定地址每次拉起都重新读配置：lan_enable 关闭时只绑回环，
            # 开启才允许局域网（设置页改完点重启，下一次拉起即按新值）
            host = "0.0.0.0" if bool(_load_server_cfg().get("lan_enable", False)) else "127.0.0.1"
            proc = await asyncio.create_subprocess_exec(
                *_run_as(), str(py), "-m", "uvicorn", "server.main:app",
                "--host", host, "--port", str(port),
                # 心跳放宽：默认 20 秒收不到 pong 就主动断开。Deck 睡眠、切桌面/游戏模式、
                # 跑一条刷屏命令时事件循环偶尔卡几十秒，默认值会让它把好好的连接踢掉，
                # 前台表现就是「回答到一半突然整段消失、重连后又冒出来」
                "--ws-ping-interval", "60", "--ws-ping-timeout", "120",
                cwd=str(INSTALL_DIR), **kwargs,
            )
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except Exception:
                    pass
    except Exception as exc:
        return False, f"启动失败：{exc}"
    # 后端要加载模型、连设备，给足 40 秒
    cfg = _load_server_cfg()
    for _ in range(80):
        core = await _core_http(port, "GET", "/api/status", timeout=2.0)
        if core.get("ok"):
            if proc.returncode is not None:
                pass    # 守卫让它自己退了、端口上是既有那份，属正常
            else:
                # 不要直接丢引用：子进程对象被回收时 asyncio 会留一个
                # 「任务还没结束」的警告，而且退出码也没人收
                def _reap(p=proc):
                    try:
                        p.wait(timeout=1)
                    except Exception:
                        pass
                threading.Thread(target=_reap, daemon=True).start()
            return True, ""
        if proc.returncode is not None:
            return False, f"后端进程退出了（code {proc.returncode}），日志：{log}"
        await asyncio.sleep(0.5)
    # 40 秒没应答：这份进程要么卡在启动里，要么永远不会应答，杀掉免得占着端口
    if proc.returncode is None:
        try:
            proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=3)
        except Exception:
            pass
    return False, f"后端 40 秒内没有应答，日志：{log}"


async def _stop_backend(port: int) -> None:
    """收掉正在跑的后端，连外层 runuser 一起（重启用）

    不能 `pkill -f 'uvicorn server.main:app'`：那份后端是
    `runuser -u deck -- <venv>/bin/python -m uvicorn ...` 起的，
    runuser 自己的命令行里也带着这串字，pkill 会把它一并杀了，
    或者反过来留下一个没人管的 runuser。这里按 /proc 里的 cmdline
    精确挑出「python -m uvicorn server.main:app」本身，再往上找它的
    runuser 父进程，一起送 SIGTERM，等它退干净。
    """
    try:
        victims: set[int] = set()
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    parts = [p.decode(errors="replace") for p in f.read().split(b"\0") if p]
            except OSError:
                continue
            # 只认真正跑后端的那个 python 进程，不认 runuser 包装（它只是抄了一遍参数）
            if not parts or "uvicorn" not in parts or "server.main:app" not in parts:
                continue
            if "runuser" in parts[0] or "sudo" in parts[0]:
                continue
            # 同一个进程组里的 runuser 也要收掉，否则它会在子进程死后留着
            try:
                os.kill(pid, signal.SIGTERM)
                victims.add(pid)
            except OSError:
                pass
        # 外层 runuser 是 root 身份，只能由插件（root）来杀
        for pid in victims:
            try:
                with open(f"/proc/{pid}/status") as f:
                    ppid = next(
                        int(line.split()[1]) for line in f if line.startswith("PPid:")
                    )
                with open(f"/proc/{ppid}/cmdline", "rb") as f:
                    parent = f.read().decode(errors="replace")
                if "runuser" in parent or "sudo" in parent:
                    os.kill(ppid, signal.SIGTERM)
            except (OSError, StopIteration, ValueError):
                pass
        # 等端口真正空出来：uvicorn 收到 SIGTERM 会先关监听再退
        for _ in range(20):
            if not await _port_busy(port):
                return
            await asyncio.sleep(0.25)
        decky.logger.warning(f"等了 5 秒端口 {port} 还占着，重启可能抢不到端口")
    except Exception as exc:
        decky.logger.error(f"停止后端出错：{exc}")


async def _port_busy(port: int) -> bool:
    """端口上是否还有服务在应答（只做 TCP 连接，不发请求）"""
    try:
        _, w = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), timeout=0.5
        )
        w.close()
        try:
            await w.wait_closed()
        except Exception:
            pass
        return True
    except (OSError, asyncio.TimeoutError):
        return False


async def _gamescope_pw_node() -> tuple[str | None, str]:
    """找 gamescope 的 PipeWire 视频源节点 id，第二个返回值是失败原因"""
    if not shutil.which("pw-dump"):
        return None, "机器上没有 pw-dump（pipewire 工具未安装）"
    cmd = _run_as() + ["pw-dump"]
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "XDG_RUNTIME_DIR": _runtime_dir()},
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=15)
    except asyncio.TimeoutError:
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=3)
            except Exception:
                pass
        return None, "pw-dump 超时（15s）"
    except Exception as exc:
        return None, f"pw-dump 跑不起来：{exc}"
    try:
        nodes = json.loads(out or b"[]")
    except Exception:
        detail = (err or b"").decode("utf-8", "replace").strip()[-200:]
        return None, f"pw-dump 输出解析失败{('：' + detail) if detail else ''}"
    for o in nodes:
        props = (o.get("info") or {}).get("props") or {}
        if (
            props.get("node.name") == "gamescope"
            and props.get("media.class") == "Video/Source"
        ):
            return str(o.get("id")), ""
    return None, "没找到 gamescope 的画面节点：可能不在游戏模式"


_GST_SEARCH = ("/usr/bin", "/usr/local/bin")


def _find_gst() -> str | None:
    """root 的 PATH 有时不含 /usr/bin，所以 PATH 找不到就按已知位置再找一遍"""
    found = shutil.which("gst-launch-1.0")
    if found:
        return found
    for d in _GST_SEARCH:
        cand = Path(d) / "gst-launch-1.0"
        if cand.exists():
            return str(cand)
    return None


async def _capture_once(node: str, gst: str, timeout: int = 12,
                        quick: float = 0.0) -> tuple[bool, str]:
    """抓一帧，返回 (成功, base64 或错误说明)

    超时收在 12 秒：正常一帧只要 0.2 秒，真卡住时用户不该对着「思考中…」干等 25 秒。
    quick > 0 时只等这么久就放弃，用来快速判断「画面是不是根本没在更新」。
    """
    tmp = Path("/tmp") / f"deckmeido_shot_{os.getpid()}_{next(_SHOT_SEQ)}.jpg"
    # 注意：pipewiresrc 的 path=... 不能和 num-buffers 塞进同一个 argv。
    # gst-launch 按 argv 逐个解析元素，两个属性挤在一项里就会被当成非法元素名，
    # 直接报 "erroneous pipeline: syntax error" 而一帧都不抓。
    #
    # 出 JPEG 而不是 PNG：1280x800 的游戏画面存 PNG 有 1200 万字节，base64 之后
    # 1600 万字符。LLM 网关对请求体有上限，这么大的图会被上游截断
    # （unexpected EOF → 503 no_healthy_account），看着就像「LLM 调用失败」，
    # 而同样配置下发纯文字是正常的。JPEG q90 大约 200KB，画质对看图问答绰绰有余。
    #
    # num-buffers=1：只要一帧。以前写 2 是想「连抓两帧取最后一帧」避开切场景的
    # 黑帧，但 gamescope 每次画面变化只投 1 帧 —— 凑不满第 2 帧进程就一直挂着，
    # 等到超时被 kill，抓到的那一帧也被当失败丢掉。实测画面静止时试抓已经写出
    # 291KB 的图，却因为超时被判成没抓到。
    cmd = _run_as() + [
        gst, "-q", "pipewiresrc", f"path={node}", "num-buffers=1",
        "do-timestamp=true", "!", "videoconvert", "!", "jpegenc", "quality=90", "!",
        "filesink", f"location={tmp}",
    ]
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "XDG_RUNTIME_DIR": _runtime_dir()},
        )
        _, err = await asyncio.wait_for(
            proc.communicate(), timeout=(quick if quick > 0 else timeout)
        )
        if not tmp.exists() or tmp.stat().st_size < 10000:
            detail = (err or b"").decode("utf-8", "replace").strip()[-300:]
            decky.logger.warning(f"抓帧没拿到有效画面：{detail or '文件为空'}")
            return False, f"抓帧没拿到有效画面{('：' + detail) if detail else ''}"
        size = tmp.stat().st_size
        data = base64.b64encode(tmp.read_bytes()).decode("ascii")
        # 日志里带上大小：前端拿到图却没显示时，先看这一条就知道是没抓到还是没渲染
        decky.logger.info(f"抓帧成功：{size} 字节")
        return True, data
    except asyncio.TimeoutError:
        if quick > 0:
            # 试抓没等到：多半是画面静止，交给上层推一下摇杆
            return False, "没等到新帧"
        return False, f"抓帧超时（{timeout}s）"
    except Exception as exc:
        return False, f"抓帧出错：{exc}"
    finally:
        # 超时/异常时 gst 还挂在 PipeWire 上：不杀掉，下一次抓帧会再起一个，
        # 几轮下来就是一堆没人读的抓帧进程
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=3)
            except Exception:
                pass
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


_SHOT_SEQ = itertools.count()
_SHOT_LOCK = asyncio.Lock()

# gamescope 只在画面内容变化时才往 PipeWire 投一帧。游戏画面静止（比如模拟器
# 停在游戏列表、或者玩家不动）时，pipewiresrc 连上去会一直空等到超时——
# 实测挂 60 秒一个 buffer 都没有，journal 里只有 streaming → paused。
# 用户那边看到的「按一下摇杆就能截图了」就是这个原因。
# 试抓等这么久就够判断：画面在动的话一帧只要 0.2 秒。
_POKE_WAIT = 1.5


async def _node_state_note() -> str:
    """抓帧失败时附带的现场说明：节点还在不在、PipeWire 说它什么状态"""
    node, why = await _gamescope_pw_node()
    if not node:
        return f"节点已消失：{why}"
    cmd = _run_as() + ["pw-dump"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, "XDG_RUNTIME_DIR": _runtime_dir()},
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        for o in json.loads(out or b"[]"):
            if str(o.get("id")) == node:
                state = (o.get("info") or {}).get("state") or "?"
                return f"节点 {node} 状态 {state}"
    except Exception:
        pass
    return f"节点 {node}"


async def grab_screenshot(node: str | None = None,
                          node_reason: str = "") -> tuple[bool, str, bool]:
    """抓一帧游戏画面，返回 (成功, base64 或错误说明, 是不是「画面静止」)

    写法和 server/tools.py 的 _screenshot_gamescope 保持一致。
    场景静止时 gamescope 不吐帧（见 _POKE_WAIT 的说明），所以先试抓 1.5 秒。
    试抓没等到就**不再自己动手**：实测 uinput 注入的输入 gamescope 根本不认
    （xdotool / ydotool / 自建虚拟手柄全试过，只有真摇杆能触发重绘），
    推摇杆那一步注定失败，只会白等一轮 12 秒。改成把这个状态交回给前端，
    由面板弹卡片请用户自己动一下。

    node 由调用方先探好时直接传进来（面板那条路要先按模式分流，避免重复
    跑一次 pw-dump）；为 None 时这里自己探。
    """
    if node is None:
        node, node_reason = await _gamescope_pw_node()
    if not node:
        return False, node_reason, False

    gst = _find_gst()
    if not gst:
        return False, "机器上没有 gst-launch-1.0（gst-plugins-base 未安装）", False

    # 同一时刻只允许一个抓帧进程，否则前端连点会把 /tmp 搅乱
    async with _SHOT_LOCK:
        ok, payload = await _capture_once(node, gst, quick=_POKE_WAIT)
        if ok:
            return True, payload, False
        if payload == "没等到新帧":
            # 节点在、gst 也在，就是没画面变化 —— 需要用户动一下
            decky.logger.info("抓帧试抓没等到新帧，请用户动一下画面")
            return False, "画面静止，动一下摇杆或触摸板", True
        # 其他失败（节点消失、gst 报错）：重探一次节点再试
        first = payload
        await asyncio.sleep(0.5)
        node2, why2 = await _gamescope_pw_node()
        if not node2:
            return False, why2, False
        ok, payload = await _capture_once(node2, gst)
        if not ok:
            decky.logger.error(f"抓帧两次失败：{first} / {payload}（节点 {node}->{node2}）")
            return False, f"{payload}（连试两次；节点 {node2}，第一次：{first}）", False
    return True, payload, False


async def _desktop_screenshot(port: int) -> tuple[bool, str]:
    """桌面模式：插件自己没有抓帧能力，交给后端（spectacle/mss）那条路

    返回 (成功, base64 或错误说明)。后端给的是压缩过的 1280 宽 JPEG，
    和 gamescope 那条的产物一样，先落盘再交给 start_turn。
    """
    r = await _core_http(port, "POST", "/api/screenshot",
                         timeout=25.0, body={})
    if not r["ok"]:
        return False, str(r.get("error") or "后端没有应答")
    d = r.get("data") or {}
    if not d.get("ok"):
        return False, str(d.get("error") or "后端没有抓到画面")
    image = str(d.get("image") or "")
    if not image:
        return False, "后端没有返回画面"
    return True, image


# ---------- 与后端 :18765 的通话 ----------
#
# 后端每轮对话的广播是发给「所有」ws 连接的，同一个后端可能同时挂着网页端。
# 所以每次开工先开一条常驻连接并拿到 history 里的 sid，转发时只认这个 sid。

class _RawWSClient:
    """不依赖 websockets 库的最小客户端（只要文本帧，够后端用）"""

    def __init__(self, url: str, user_agent: str = ""):
        self.url = url
        self.user_agent = user_agent
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._buf = b""

    async def __aenter__(self):
        from urllib.parse import urlsplit, urlunsplit

        u = urlsplit(self.url)
        host = u.hostname or "127.0.0.1"
        port = u.port or 80
        target = urlunsplit(("", "", u.path or "/", u.query, ""))
        self._reader, self._writer = await asyncio.open_connection(host, port)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {target} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Origin: http://{host}:{port}\r\n"
            + (f"User-Agent: {self.user_agent}\r\n" if self.user_agent else "")
        ) + "\r\n"
        self._writer.write(req.encode())
        await self._writer.drain()
        head = await self._reader.readuntil(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status_line:
            raise RuntimeError(f"握手被拒：{status_line[:80]!r}")
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self):
        w, self._writer = self._writer, None
        if w:
            try:
                w.write(_frame(0x8, b""))
                await w.drain()
            except Exception:
                pass
            w.close()

    async def send(self, text: str):
        assert self._writer
        self._writer.write(_frame(0x1, text.encode()))
        await self._writer.drain()

    async def recv(self) -> str:
        while True:
            frame = await self._read_frame()
            if frame is None:
                raise RuntimeError("后端关掉了连接")
            opcode, payload = frame
            if opcode == 0x1:
                return payload.decode()
            if opcode == 0x8:
                raise RuntimeError("后端发来 close 帧")

    async def _filled(self, n: int):
        assert self._reader
        while len(self._buf) < n:
            chunk = await self._reader.read(65536)
            if not chunk:
                return None
            self._buf += chunk
        return self._buf

    async def _read_frame(self) -> tuple[int, bytes] | None:
        """返回 (opcode, payload)；服务端帧不加掩码，分片帧拼起来"""
        buf_parts: list[bytes] = []
        first_op: int | None = None
        while True:
            head = await self._filled(2)
            if head is None:
                return None
            b0, b1 = head[0], head[1]
            opcode = b0 & 0x0F
            fin = bool(b0 & 0x80)
            length = b1 & 0x7F
            masked = bool(b1 & 0x80)
            offset = 2
            if length == 126:
                ext = await self._filled(offset + 2)
                if ext is None:
                    return None
                length = int.from_bytes(ext[2:4], "big")
                offset += 2
            elif length == 127:
                ext = await self._filled(offset + 8)
                if ext is None:
                    return None
                length = int.from_bytes(ext[2:10], "big")
                offset += 8
            mask = b""
            if masked:
                ext = await self._filled(offset + 4)
                if ext is None:
                    return None
                mask = ext[offset:offset + 4]
                offset += 4
            body = await self._filled(offset + length)
            if body is None:
                return None
            payload = body[offset:offset + length]
            self._buf = body[offset + length:]
            if mask:
                payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
            if opcode == 0x9:  # ping
                assert self._writer
                self._writer.write(_frame(0xA, payload))
                await self._writer.drain()
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode in (0x1, 0x2):
                first_op = opcode
                buf_parts.append(payload)
                if fin:
                    return first_op, b"".join(buf_parts)
            elif opcode == 0x0:
                buf_parts.append(payload)
                if fin:
                    return first_op or 0x1, b"".join(buf_parts)
            elif opcode == 0x8:
                return 0x8, payload


def _frame(opcode: int, payload: bytes, mask: bool = True) -> bytes:
    """客户端发出的帧必须加掩码"""
    head = bytearray([0x80 | opcode])
    n = len(payload)
    mask_bit = 0x80 if mask else 0
    if n < 126:
        head.append(mask_bit | n)
    elif n < 65536:
        head.append(mask_bit | 126)
        head += n.to_bytes(2, "big")
    else:
        head.append(mask_bit | 127)
        head += n.to_bytes(8, "big")
    if not mask:
        return bytes(head) + payload
    key = os.urandom(4)
    return bytes(head) + key + bytes(c ^ key[i % 4] for i, c in enumerate(payload))


class _Backend:
    """一条连到后端的常驻 ws。

    只用来「发」：每一轮带 turn_id 发出去，订阅由 _TailWS 单独负责。
    """

    def __init__(self, url: str):
        self.url = url
        self._impl = None
        self._send_lock = asyncio.Lock()
        # 本端这一轮的 turn_id：发 chat 时自己生成，之后靠它认出哪些 δ 是自己的。
        # 不能用 history 里的 sid：那是「会话」id，所有端共享，别的端提问时
        # δ 的 sid 和本端完全一样，会被误当成自己这一轮拼进正文
        self.turn = ""

    async def open(self):
        # 后端用 UA 判断「这条连接是网页还是手机」，插件不声明就会被当成一个
        # 额外的网页端，界面上白多出一栏来源。标成 QAM 才认得出是谁
        hdr = {"User-Agent": "DeckMeido-QAM/1.0"}
        if _ws_connect is not None:
            self._impl = await _ws_connect(
                self.url, max_size=32 * 1024 * 1024, open_timeout=10,
                additional_headers=hdr,
            )
        else:
            self._impl = await _RawWSClient(self.url, user_agent=hdr["User-Agent"]).__aenter__()

    async def recv(self) -> str:
        msg = await self._impl.recv()
        return msg if isinstance(msg, str) else msg.decode()

    async def send(self, payload: dict):
        text = json.dumps(payload)
        async with self._send_lock:
            await self._impl.send(text)

    def sockname(self) -> str:
        """本端在这条连接上的本地端口，和后端日志里的 127.0.0.1:xxxxx 对得上"""
        w = getattr(self._impl, "_writer", None)
        try:
            host, port = w.get_extra_info("sockname")[:2]
            return f"{host}:{port}"
        except Exception:
            return "?"

    async def close(self):
        impl, self._impl = self._impl, None
        if not impl:
            return
        try:
            await impl.close()
        except Exception:
            pass


_BACKEND: _Backend | None = None
_READER: asyncio.Task | None = None
_ACC = ""            # 本轮已收到的增量正文
_LAST_REPLY = ""     # 本轮确定下来的完整正文
_FINAL_EVENT = asyncio.Event()
# 本端这一轮是否正在跑
_STREAM_RUNNING = False
# 这一轮在后端开始的时刻（秒），前端用来过滤「终止后才迟到的旧状态」
_STREAM_SINCE = 0.0
# 本端这一轮从发起到收尾的锁：连着点两次「发送」会各发一条 chat，
# 两条流共用 _ACC 和 _FINAL_EVENT，正文会串成一团
_TURN_LOCK = asyncio.Lock()
# 面板展示的对话：所有端的提问和回复，{role, text, client, ts}
_THREAD: list[dict] = []
# 面板只显示最近这么多条：QAM 里挂着几百条会把滚动拖卡。
# 这只是显示上限，完整对话在后端历史里，网页端照样能看到全部
_THREAD_MAX = 20
# 最近一次推给面板的 thread 快照，等界面 ready 时补发（见 _emit）
_LAST_THREAD: dict | None = None
# 最近一次截图落盘的本地路径：它比后端回显的 /shots/ 更早可用。
# 后端要等 ws 把 16MB 的图收完才落盘，这段空窗里各端（尤其电脑网页）只看到
# 一条没有画面的提问，看起来就像「插件截的图在电脑端不显示」
_LAST_SHOT_PATH = ""
# 本轮正在发出去的那张图：从截图落盘一直留到这一轮收尾。
# 以前只在 start_turn 里清，而且 user_message 只认 _LAST_SHOT_PATH，
# 于是「后端广播没带 events」这条路上就没有图可补
_TURN_SHOT_PATH = ""
# 面板上的来源标记由后端打标后原样带过来，这里不再维护一份名字映射


def _push_thread(item: dict):
    """追加一条并推给面板。

    user_message 的 ts 在历史里唯一，重复推送（重连后补发）按 ts 去掉。
    """
    ts = float(item.get("ts") or 0)
    for m in (_THREAD[-8:] if ts else []):
        if m.get("role") == item["role"] and abs(float(m.get("ts") or 0) - ts) < 1e-6:
            return
    # ts 为 0 的是「本地先落、还没拿到后端时间戳」的那条（见 start_turn）：
    # 后端回声在 ts 上对不上，按角色+正文把本地占位换掉
    if ts:
        for m in reversed(_THREAD[-8:]):
            if (m.get("role") == item["role"] and not float(m.get("ts") or 0)
                    and str(m.get("text") or "") == str(item.get("text") or "")):
                # 本地占位是纯文字的，图只挂在本机这张缩略图以外的路径上：
                # 回填时补进去，否则这一轮的画面要等下次重连才会出现
                if not item.get("image") and m.get("image"):
                    item = {**item, "image": m["image"]}
                m.update(item)
                _emit({"kind": "thread", "items": _THREAD})
                return
    _THREAD.append(item)
    if len(_THREAD) > _THREAD_MAX:
        del _THREAD[: len(_THREAD) - _THREAD_MAX]
    _emit({"kind": "thread", "items": _THREAD})


def _shot_url(path: str) -> str:
    """把截图路径拼成面板能直接加载的地址

    后端给的 /shots/xxx 走回环；插件自己截的那张在 /tmp 下，交给后端落盘以后
    才会有 /shots/ 的地址。所以这里两种都认：以 / 开头的当后端静态路径，
    其余的当本机文件路径。回环访问免鉴权，不再携带令牌。
    """
    if not path:
        return ""
    cfg = _load_server_cfg()
    port = int(cfg.get("port", 18765))
    if path.startswith("/"):
        return f"http://127.0.0.1:{port}{path}"
    return f"http://127.0.0.1:{port}/shots/local?path={quote(path)}"


def _sync_thread(hist: list):
    """用后端给的完整历史对齐本地列表（连上时、别的端改会话时）

    只看文本消息：随图那条多模态消息（content 是数组）不进面板，
    它的画面由挂在 user 消息 events 里的路径显示。
    """
    out = []
    for m in hist:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            continue
        content = m.get("content")
        if isinstance(content, list):
            continue
        image = ""
        for ev in m.get("events") or []:
            if isinstance(ev, dict) and ev.get("image"):
                image = _shot_url(str(ev["image"]))
                break
        out.append({
            "role": m["role"],
            # brief 是界面专用的短文案（截屏询问那套提示词一百多字，长显会把面板撑开）
            "text": str(m.get("brief") or content or ""),
            "client": str(m.get("client") or ""),
            "ts": float(m.get("ts") or 0),
            **({"image": image} if image else {}),
        })
    _THREAD[:] = out[-_THREAD_MAX:]
    _emit({"kind": "thread", "items": _THREAD})


def _backend_url(port: int) -> str:
    # 插件与后端同机，回环连接免鉴权，不需要令牌
    return f"ws://127.0.0.1:{port}/ws"


def _backend_addr() -> int:
    """从配置读端口：重连循环自己重建连接时要用"""
    cfg = _load_server_cfg()
    return int(cfg.get("port", 18765))


def _emit(msg: dict):
    """把一条消息推给 QAM（前端 addEventListener("deckmeido_delta")）

    界面可能比面板开关得晚（Decky 在面板首帧之后才挂监听），后端连得早时
    history 那一条 thread 会落空，所以这里顺手留一份最新快照，等面板调
    ready() 再补发。

    新版 Decky 的 decky.emit 是协程，直接调用什么都不会发出去（只在日志里
    留一条 RuntimeWarning），连当前事件循环也要忙到下一次 await 才第一次真正
    执行 —— 所以这里必须挂到循环上跑，不能当同步函数使。
    """
    global _LAST_THREAD
    if msg.get("kind") == "thread":
        _LAST_THREAD = msg
    try:
        res = decky.emit("deckmeido_delta", msg)
    except Exception as exc:
        decky.logger.warning(f"推送前端失败：{exc}")
        return
    if inspect.isawaitable(res):
        try:
            task = asyncio.get_running_loop().create_task(res)
        except RuntimeError:
            # 没有运行中的循环（不该发生），至少别把结果丢掉
            decky.logger.warning("推送前端失败：没有事件循环")
            return
        # 推送失败不能只留在「Task exception was never retrieved」里，
        # 更要紧的是别让它冒泡回去把调用方（比如 start_turn）一起带死
        task.add_done_callback(_emit_done)


def _emit_done(task: asyncio.Task):
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        decky.logger.warning(f"推送前端失败：{exc}")


def _finalize(cancelled: bool = False):
    """把这一轮冻结下来并推给前端。

    后端的收尾顺序是 delta(final=True, reply) → assistant_message(全文) → status；
    delta(final=True) 那条已经带整份全文（reply），所以读循环收到它就立刻冻结，
    assistant_message 只负责往面板列表里补一条带 ts 的正式记录。
    status.running 不能当收尾依据：current_task 在回复广播之前就被置空，
    那一刻正文还没到。
    """
    global _LAST_REPLY, _STREAM_RUNNING, _STREAM_SINCE, _TURN_SHOT_PATH
    # 同一轮可能被两条路径冻结（正常收尾一条，连接断开兜底一条）：
    # 已经冻结过就别重复发 final，也别再动锁
    if _FINAL_EVENT.is_set():
        return
    _LAST_REPLY = _ACC
    _STREAM_RUNNING = False
    _STREAM_SINCE = 0.0
    # 这一轮结束了，本地那张图不再回落给别的端的提问
    _TURN_SHOT_PATH = ""
    _emit({"kind": "final", "text": _ACC, "cancelled": cancelled})
    _FINAL_EVENT.set()
    if _TURN_LOCK.locked():
        _TURN_LOCK.release()


async def _read_loop(bk: _Backend):
    """常驻读：history 对齐列表，之后所有端（客户端/网页/手机/插件）的消息都进面板

    后端把用户提问和助手回复广播给每一条 ws 连接，所以这里能拿到别的端的对话。
    本端发出去的那一轮还要额外走 delta / final，把那几十秒的流式过程显示出来。

    连接断了要自己接回来：以前这里直接 return，读任务结束但 _BACKEND 还挂着，
    下次 _ensure_backend 看见「_READER 已结束」会重连，可在那之前面板上所有端
    的消息都静默丢失。现在断线后原地退避重连，重连上立刻重新拉一次 history
    对齐，并把本端没等到收尾的那一轮标成中断（它的 final 永远等不到了）。
    """
    global _ACC, _BACKEND, _STREAM_RUNNING, _STREAM_SINCE
    delay = 1.0
    while True:
        try:
            raw = await bk.recv()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            decky.logger.info(f"后端连接断开：{exc}")
            # 本端这一轮还在等答复：收尾消息随连接一起丢了，_FINAL_EVENT 永远
            # 不会 set。不在这里冻结的话，面板会一直显示「正在思考」
            if bk.turn and not _FINAL_EVENT.is_set():
                _finalize(cancelled=True)
            await bk.close()
            if bk is not _BACKEND:
                # 已经被 _drop_backend 换掉了（取消、退出），不要接回来
                return
            _BACKEND = None
            _STREAM_RUNNING = False
            decky.logger.info(f"准备重连后端（{delay:.0f}s 后）")
            # 退避重连：成功才换 bk，失败就继续退避再试。
            # 以前失败时换了一个没连上的新对象，下一轮 recv 立刻抛
            # 'NoneType' object has no attribute 'recv'，日志到这里就断了，
            # 之后再也不会重连——插件从此永久掉线。
            while True:
                new = await _reconnect(delay)
                if new is not None:
                    break
                delay = min(delay * 2, 30.0)
            delay = 1.0
            bk = new
            continue
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        typ = msg.get("type")
        # 只认本端这一轮的 turn_id：会话 sid 是所有端共用的，拿它过滤等于没过滤
        tid = str(msg.get("turn_id") or "")
        mine = bool(tid) and tid == bk.turn
        if typ == "history":
            _sync_thread(msg.get("messages") or [])
            continue
        if typ == "status":
            # 状态广播不带 sid（它在改配置时也会发），所以放在 sid 过滤之前
            data = msg.get("data") or {}
            run = bool(data.get("running"))
            # 记下来：重连/换到别的端开会话时，前端靠它决定要不要弹「正在思考」
            _STREAM_RUNNING = run
            if run:
                _STREAM_SINCE = float(data.get("running_since") or 0)
            else:
                _STREAM_SINCE = 0.0
            # 开始时间也一起带上：前端刚点过终止时，靠它识别「这是旧那轮的迟到状态」，
            # 避免思考指示灭掉又被一条陈旧 running=true 点亮
            _emit({"kind": "status", "running": run, "since": _STREAM_SINCE})
            continue
        if typ == "pad_order":
            # 虚拟手柄的换序请求：全局广播、不属于某一轮，所以也要放在 sid 过滤前。
            # 排序状态只在 Steam 进程里，后端改不了，只能交给前端去调
            # SteamClient.Input.SwapControllerOrder（详见前端 swapPadOrder）
            act = str(msg.get("action") or "")
            decky.logger.info(f"收到后端 pad_order/{act}，转给前端")
            _emit({"kind": "pad_order", "action": act})
            continue
        if typ == "user_message":
            item = {
                "role": "user",
                # 带 brief 的那一轮（截屏询问）只显示短文案：
                # 一百多字的提示词长显会把面板撑开
                "text": str(msg.get("brief") or msg.get("text") or ""),
                "client": str(msg.get("client") or ""),
                "ts": float(msg.get("ts") or 0),
            }
            # 截屏询问附带的画面：后端已存成 /shots/xxx 并挂在 events 上
            for ev in msg.get("events") or []:
                if isinstance(ev, dict) and ev.get("image"):
                    item["image"] = _shot_url(str(ev["image"]))
                    break
            # 后端那条广播里带不出图（落盘失败）时，若这一轮就是本机刚截的那张，
            # 用本地那份顶上：面板上总得有一张能看到的画面
            if not item.get("image") and item["client"] == "qam":
                local = _TURN_SHOT_PATH or _LAST_SHOT_PATH
                if local:
                    item["image"] = _shot_url(local)
            _push_thread(item)
            continue
        if typ == "assistant_message":
            # 助手消息现在也带来源（后端按同一轮的提问打标）；老数据没有这个字段，
            # 退回「最近一条 user 的 client」来归户
            text = str(msg.get("text") or "")
            _push_thread({
                "role": "assistant",
                "text": text,
                "client": str(msg.get("client") or _nearby_client()),
                "ts": float(msg.get("ts") or 0),
            })
            if not mine:
                continue
            if msg.get("cancelled"):
                # 用户点了终止：已生成的部分比「已终止当前操作」有用，保留它
                _ACC = _ACC or text or "已终止当前操作"
                _finalize(cancelled=True)
            else:
                # 唯一带完整正文的收尾消息
                _ACC = text or _ACC
                _finalize()
            continue
        if not mine:
            # 别的会话的流式过程与本端这一轮无关，正文已由 assistant_message 收下
            continue
        if typ == "delta":
            text = str(msg.get("text") or "")
            if text:
                _ACC += text
                _emit({"kind": "delta", "text": text})
            if msg.get("final"):
                # 正文已完整：这条是唯一带整份全文的流式消息，assistant_message 之后
                # 只会把它再覆盖一遍同样的内容。以前这里只改写 _ACC、不发出去，
                # 前端只能等 wait_final 的兜底返回值补全文，那段已经攒下的增量还挂在
                # 流式气泡上，看起来就是同一句话显示了两遍。这里直接把定稿推给前端
                _ACC = str(msg.get("reply") or _ACC)
                _finalize()
        elif typ == "tool_activity":
            # 「正在读文件：…」这类过程提示，只给界面看，不进正文
            _emit({"kind": "tool", "text": str(msg.get("text") or "")})


def _nearby_client() -> str:
    """找最近一条 user 消息的来源，当作这轮回复的归属"""
    for m in reversed(_THREAD):
        if m.get("role") == "user":
            return str(m.get("client") or "")
    return ""


async def _reconnect(delay: float) -> _Backend | None:
    """重连一次：先退避再试，成功就重新拉一份 history 对齐面板

    只在读循环里调用。失败返回 None，由调用方继续退避。
    """
    global _BACKEND
    await asyncio.sleep(delay)
    # 每次尝试都用新建的连接：上一条已经被 close 了，复用它只会立刻抛错
    port = _backend_addr()
    bk = _Backend(_backend_url(port))
    try:
        await bk.open()
    except Exception as exc:
        decky.logger.info(f"重连后端失败：{exc}")
        return None
    _BACKEND = bk
    decky.logger.info(f"后端已重连：{bk.sockname()}")
    # 断线期间别的端可能说过话，补一份完整历史
    try:
        await bk.send({"type": "history"})
    except Exception as exc:
        decky.logger.info(f"重连后拉 history 失败：{exc}")
    return bk


async def _ensure_backend() -> tuple[_Backend | None, str]:
    """拿到可用的后端连接，第二个返回值是错误说明"""
    global _BACKEND, _READER
    cfg = _load_server_cfg()
    if not _config_path().exists():
        return None, f"找不到配置：{_config_path()}"
    port = int(cfg.get("port", 18765))
    if _BACKEND and _READER and not _READER.done():
        return _BACKEND, ""
    await _drop_backend()
    bk = _Backend(_backend_url(port))
    try:
        await bk.open()
    except Exception as exc:
        decky.logger.error(f"连不上后端 :{port}（{exc}）")
        return None, f"连不上后端 :{port}（{exc}）"
    _BACKEND = bk
    _READER = asyncio.create_task(_read_loop(bk))
    decky.logger.info(f"已连上后端 :{port}，本端 {bk.sockname()}")
    return bk, ""


async def _drop_backend():
    global _BACKEND, _READER
    r, _READER = _READER, None
    bk, _BACKEND = _BACKEND, None
    if r:
        r.cancel()
        try:
            await r
        except BaseException:
            pass
    if bk:
        await bk.close()


def _lan_ip() -> str:
    """本机在局域网里的地址，给别的端填后端地址用

    UDP connect 的源地址跟着默认路由走：开了 Clash/加速器后默认出口是
    198.18.x 的 TUN fake-ip，别的设备连不上。所以这里枚举网卡只挑 RFC1918
    私有段，和 server 的 _lan_ip 保持一致；没连局域网时退回 127.0.0.1。
    """
    import re
    import subprocess

    try:
        out = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "scope", "global"],
            capture_output=True, text=True, timeout=3,
        ).stdout
        ips = re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", out)
    except Exception:
        ips = []
    private = [a for a in ips if a.startswith("192.168.") or a.startswith("10.")
               or re.match(r"172\.(1[6-9]|2\d|3[01])\.", a)]
    if not private:
        return "127.0.0.1"
    private.sort(key=lambda a: (not a.startswith("192.168."), not a.startswith("10.")))
    return private[0]


async def _core_http(port: int, method: str, path: str,
                     timeout: float = 5.0, body: dict | None = None) -> dict:
    """向后端的本机接口发一个请求，返回 {"ok": bool, "data": {...} | "error": str}

    不走 ws：这类请求都只要一个结论，HTTP 一步拿到。插件与后端同机，
    回环请求免鉴权，因此不再附带令牌头。
    """
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
    except Exception as exc:
        return {"ok": False, "error": f"连不上 :{port}（{exc}）"}
    try:
        payload = b""
        extra = ""
        if body is not None:
            payload = json.dumps(body).encode()
            extra = (f"Content-Type: application/json\r\n"
                     f"Content-Length: {len(payload)}\r\n")
        req = (
            f"{method} {path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"{extra}"
            "Connection: close\r\n\r\n"
        ).encode() + payload
        writer.write(req)
        await writer.drain()
        # 后端返回体可能很大（history 全量），一次 read() 只拿到首个 TCP 段，
        # 所以读到 EOF 为止
        chunks = []
        while True:
            chunk = await asyncio.wait_for(reader.read(65536), timeout=timeout)
            if not chunk:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    head, _, body = raw.partition(b"\r\n\r\n")
    if b" 200 " not in head.split(b"\r\n", 1)[0]:
        return {"ok": False, "error": head.split(b"\r\n", 1)[0].decode("utf-8", "replace")[:60]}
    try:
        return {"ok": True, "data": json.loads(body)}
    except Exception:
        return {"ok": False, "error": "返回内容不是 JSON"}


async def _core_status(port: int) -> dict:
    """问一次后端自己的 /api/status，确认女仆核心活着（在不在、什么模式）

    get_status 是面板打开时第一个调用，Decky 会一直等着它返回，
    超时太长时后端没起来的话整个 QAM 都像卡死，所以只给 2 秒。
    """
    return await _core_http(port, "GET", "/api/status", timeout=2.0)


async def _core_prefs(port: int) -> dict:
    """读后端的偏好项（语音播报、带画面、版本号、当前模型），面板设置区要显示/切换这些"""
    r = await _core_http(port, "GET", "/api/config", timeout=2.0)
    if not r["ok"]:
        return {"ok": False, "error": r.get("error", ""), "tts": False,
                "with_shot": True, "version": "", "model": ""}
    d = r.get("data") or {}
    return {"ok": True, "tts": bool(d.get("tts_enable")),
            # 带画面询问：老后端没有这个字段就当开（和前端默认值一致）
            "with_shot": bool(d.get("with_shot", True)),
            # 语音输入：配置在 [stt] 段，/api/stt 才有；这里同时带一份供面板显示
            "stt": bool(d.get("stt_enable", True)),
            # 录音时静音外放（[stt] duck_output，默认开）
            "stt_duck": bool(d.get("stt_duck_output", True)),
            "stt_duck_available": bool((d.get("stt_duck") or {}).get("available")),
            "version": str(d.get("version") or ""),
            "model": str(d.get("model") or ""),
            # 人设昵称：状态行前缀用，缺失时前端有默认称呼兜底
            "persona_name": str(d.get("persona_name") or "")}


async def _session_post(what: str, path: str) -> dict:
    """会话类操作共用的 POST：拿配置、确认后端活着、发请求"""
    cfg = _load_server_cfg()
    port = int(cfg.get("port", 18765))
    r = await _core_http(port, "POST", path, timeout=8.0, body={})
    if not r["ok"]:
        # 后端在跑但 reject 了，和根本没跑是两回事，错误里带上原始原因
        decky.logger.error(f"{what} 失败：{r.get('error')}")
        return {"ok": False, "error": str(r.get("error") or "后端没有应答")}
    data = r.get("data") or {}
    out = {"ok": True, "id": str(data.get("id") or "")}
    # 后端在「当前会话本来就没说过话」时不会新建（防止连点刷出一排空会话），
    # 把它翻成前端能看懂的字段，否则按钮看起来毫无反应
    if data.get("unchanged"):
        out["unchanged"] = True
    return out


class Plugin:
    async def get_status(self) -> dict:
        """QAM 打开时先问一次：后端连得上吗、截图能不能用

        本机面板不再需要解锁；这里只报能力，不含画面或对话内容。
        读不到后端配置时通过 config_error 给出明确原因。
        """
        cfg = _load_server_cfg()
        port = int(cfg.get("port", 18765))
        node, why = await _gamescope_pw_node()
        core = await _core_status(port)
        gamepad = _gamepad_from_core(core)
        # 后端在、但插件这边没连着（比如后端重启过）就顺手补一条：
        # 换序这类全局广播全靠这条长连接，断了就静默丢消息
        if core.get("ok") and _BACKEND is None:
            await self._stay_connected()
        prefs = await _core_prefs(port) if core.get("ok") else {}
        # 更新检测走 /api/version 里的缓存结果（后端启动时查过一次，之后每天刷新），
        # 设置页只是展示，不在这条路径上现发网络请求
        ver = {}
        if core.get("ok"):
            vr = await _core_http(port, "GET", "/api/version", timeout=3.0)
            if vr.get("ok"):
                ver = vr.get("data") or {}
        upd = ver.get("update") or {}
        # 查游戏名要扫进程表、读 Steam 日志，放线程里做：面板一刷新就调这个接口，
        # 同步跑会把 Decky 的事件循环卡住
        game = await asyncio.to_thread(_current_game)
        if node:
            cap_ok = _find_gst() is not None
            cap_reason = "" if cap_ok else (
                why or "机器上没有 gst-launch-1.0（gst-plugins-base 未安装）")
        else:
            # 桌面模式：后端 /api/status 里 spectacle 的探测结果
            ctrls = (core.get("data") or {}).get("controls") or {}
            spec = next((i for i in (ctrls.get("items") or [])
                         if i.get("key") == "spectacle"), None)
            cap_ok = bool(core.get("ok")) and bool(spec and spec.get("ok"))
            if not core.get("ok"):
                cap_reason = "核心没起来，桌面模式下暂时截不到画面"
            elif not (spec and spec.get("ok")):
                cap_reason = "没装 spectacle，桌面模式下暂时截不到画面"
            else:
                cap_reason = ""
        # 面板只会写「探测失败（原因）」，日志里留一份，事后能对上时间点
        decky.logger.info(
            f"get_status：核心 {'正常' if core.get('ok') else '异常'}"
            f"{'' if core.get('ok') else '（' + str(core.get('error') or '') + '）'}，"
            f"画面节点 {'有' if node else '没有'}"
            f"{'' if node else '（' + why + '）'}，"
            f"游戏 {game.get('name') or '无'}"
            f"（来源 {game.get('source') or 'none'} / 置信 {game.get('confidence') or 'none'}）"
        )
        lan_ip = _lan_ip()
        return {
            "install_dir": str(INSTALL_DIR),
            "config_ok": _config_path().exists(),
            "port": port,
            "gamescope": node is not None,
            # 画面可用性：游戏模式看 gamescope 节点 + gst；桌面模式插件自己不
            # 抓帧，改由后端 spectacle/mss 抓，所以看「后端在不在 + spectacle
            # 装没装」。前端据此决定是直接给提示，还是照常退页去抓（省掉白等）
            "capture_ok": cap_ok,
            "capture_reason": cap_reason,
            "uid": os.getuid(),
            "as_user": " ".join(_run_as()) or "(self)",
            "proxy": "websockets" if _ws_connect else "raw",
            # 面板第二行显示成「192.168.x.x:18765」，别的设备照这个地址连
            "ip": lan_ip,
            # 「局域网访问」区块：地址，前端展示并提供复制
            # lan_enable=false 时只监听回环，前端把地址标成「仅本机」并给出开关提示
            "lan_enable": bool(cfg.get("lan_enable", False)),
            "lan_url": f"http://{lan_ip or '127.0.0.1'}:{port}",
            # 掌机状态行：有人尝试配对 / 连续鉴权失败时显示一句提示
            "security_note": str((core.get("data") or {}).get("security_note") or ""),
            "pending_pair": int((core.get("data") or {}).get("pending_pair") or 0),
            # 女仆核心（后端自身的 /api/status）在不在、跑没跑起来
            "core_ok": bool(core.get("ok")),
            "core_reason": str(core.get("error") or ""),
            "core_mode": str((core.get("data") or {}).get("mode") or ""),
            # 用户点过「关闭核心」且没再点「启动」：前端据此显示「已停止」
            # 而不是「异常」，并把「重启」置灰
            "core_stopped": _core_stopped(),
            # 设置区：语音播报开关（由后端 tts.enable 决定）与版本号（底部显示）
            "tts": bool(prefs.get("tts")),
            # 带画面询问开关：面板一关一开组件会重挂载，状态得从后端补回来
            "with_shot": bool(prefs.get("with_shot", True)),
            # 语音输入开关（[stt] enable）：关掉时后端会释放识别模型省内存
            "stt": bool(prefs.get("stt", True)),
            # 录音时静音外放：设置页的开关，机器不支持时仍可保存、只是不起作用
            "stt_duck": bool(prefs.get("stt_duck", True)),
            "stt_duck_available": bool(prefs.get("stt_duck_available", False)),
            "version": str(prefs.get("version") or _PLUGIN_VERSION),
            # 更新检测：两处设置页统一显示「有更新 / 当前最新」，不摆占位横线
            "update_has": bool(upd.get("has_update")),
            "update_latest": str(upd.get("latest") or ""),
            # 设置区：当前模型名，下拉菜单的首帧先显示它，再异步拉全量列表
            "model": str(prefs.get("model") or ""),
            # 人设昵称：状态行用它做前缀（persona 缺失时前端退回默认称呼）
            "persona_name": str(prefs.get("persona_name") or ""),
            # 当前正在运行的游戏名：抓帧时会再查一次，这里给面板一个初值，
            # 让用户按「截屏询问」之前就能看到这一问会带上哪款游戏。
            # source / confidence / ts 让面板和日志都能看出名字从哪来、可不可信；
            # 前端只在 confidence == "high" 时才把名字注入提问
            "game_name": (game.get("name") or ""),
            "game_source": (game.get("source") or ""),
            "game_confidence": (game.get("confidence") or ""),
            "game_ts": (game.get("ts") or 0),
            # 虚拟手柄能不能用：只透传后端 /api/status 的判断（插件是 root，
            # 它自己的权限不等于后端进程的权限）。后端没给这个字段（旧版本或
            # 后端没连上）时保持 None，前端显示「未知」而不是假设可用。
            "gamepad_ok": gamepad.get("ok"),
            "gamepad_reason": gamepad.get("reason", ""),
            # 读不到后端配置：前端据此显示明确原因
            "config_error": "" if cfg else _cfg_error_text(),
        }

    async def pair_info(self) -> dict:
        """设备配对：待配对会话（带 PIN/倒计时）与已配对设备。

        仅供掌机设置页轮询；PIN 只在这里返回，浏览器侧的 /api/pair/status
        永远拿不到 PIN。
        """
        cfg = _load_server_cfg()
        port = int(cfg.get("port", 18765))
        pending = await _core_http(port, "GET", "/api/pair/pending")
        devices = await _core_http(port, "GET", "/api/devices")
        return {
            "ok": bool(pending.get("ok")),
            "sessions": (pending.get("data") or {}).get("sessions") or [],
            "devices": (devices.get("data") or {}).get("devices") or [],
            "lan_enable": bool(cfg.get("lan_enable", False)),
            "error": str(pending.get("error") or devices.get("error") or ""),
        }

    async def pair_refresh(self, pair_id: str) -> dict:
        cfg = _load_server_cfg()
        return await _core_http(int(cfg.get("port", 18765)),
                                "POST", "/api/pair/refresh", body={"pair_id": pair_id})

    async def pair_decide(self, pair_id: str, allow: bool) -> dict:
        cfg = _load_server_cfg()
        return await _core_http(int(cfg.get("port", 18765)),
                                "POST", "/api/pair/decision",
                                body={"pair_id": pair_id, "allow": bool(allow)})

    async def pair_remove(self, device_id: str) -> dict:
        cfg = _load_server_cfg()
        return await _core_http(int(cfg.get("port", 18765)),
                                "POST", "/api/devices/remove",
                                body={"device_id": device_id})

    async def pair_rename(self, device_id: str, name: str) -> dict:
        cfg = _load_server_cfg()
        return await _core_http(int(cfg.get("port", 18765)),
                                "POST", "/api/devices/rename",
                                body={"device_id": device_id, "name": name})

    async def pair_remove_all(self) -> dict:
        cfg = _load_server_cfg()
        return await _core_http(int(cfg.get("port", 18765)),
                                "POST", "/api/devices/remove_all")

    async def session_open(self) -> dict:
        """面板打开时调一次：连上后端并拉回全部对话（所有端的提问与回复）

        连上后 _read_loop 会先收到 history 并推一条 thread 事件，前端不必等这里的返回。
        """
        bk, why = await _ensure_backend()
        if not bk:
            return {"ok": False, "error": why}
        return {"ok": True}

    async def ready(self) -> dict:
        """前端挂好监听后调一次：把积攒下来的对话补推过去

        Decky 是先渲染面板、后挂 addEventListener，而 history 那条 thread 可能在
        面板首帧之前就推完了。这里不重新连后端，只把快照再发一遍。
        """
        if _LAST_THREAD:
            _emit(_LAST_THREAD)
        elif _THREAD:
            _emit({"kind": "thread", "items": _THREAD})
        # 面板重开时后端可能正在跑一轮（别端发起的）：状态得补一次，
        # 否则界面上什么提示都没有，用户会以为卡住了
        _emit({"kind": "status", "running": _STREAM_RUNNING, "since": _STREAM_SINCE})
        return {"ok": True}

    async def start_backend(self) -> dict:
        """面板上的「启动后端」：重开机进游戏模式后后端通常是没跑的

        先探一下再拉，避免和客户端同时拉起两份（后端有单实例守卫，
        多半是白等 40 秒才返回成功）。
        """
        cfg = _load_server_cfg()
        port = int(cfg.get("port", 18765))
        # 用户显式点「启动」：清掉停止标记，之后的自动拉起才恢复
        _mark_core_stopped(False)
        core = await _core_http(port, "GET", "/api/status", timeout=2.0)
        if core.get("ok"):
            return {"ok": True, "already": True}
        decky.logger.info("QAM 拉起后端 :%s", port)
        ok, why = await _start_backend(port)
        if not ok:
            decky.logger.error("拉起后端失败：%s", why)
            return {"ok": False, "error": why}
        # 后端换了新连接，旧的 ws 已经没用，重连一次面板才拿得到 history
        await _drop_backend()
        await _ensure_backend()
        return {"ok": True, "already": False}

    async def session_new(self) -> dict:
        """新建会话：QAM 上这一轮聊下去不对劲时，从这里重新开一头

        走 HTTP 而不是 ws：后端受理后会向所有连接广播 history/session_changed，
        插件侧的 _read_loop 收到就自然刷新，不需要在这里额外处理返回值。
        """
        return await _session_post("session_new", "/api/sessions/create")

    async def set_tts(self, enabled: bool) -> dict:
        """切语音播报：交给后端写入 [tts] enable，热更新、不用重启"""
        cfg = _load_server_cfg()
        port = int(cfg.get("port", 18765))
        r = await _core_http(port, "POST",
                             f"/api/tts?enabled={'true' if enabled else 'false'}")
        if not r["ok"]:
            return {"ok": False, "error": r.get("error") or "后端没有接受这次修改"}
        return {"ok": True, "tts": bool((r.get("data") or {}).get("tts_enable"))}

    async def set_with_shot(self, enabled: bool) -> dict:
        """切「带画面询问」：写进后端 [agent] with_shot，面板重开后仍是这个值"""
        cfg = _load_server_cfg()
        port = int(cfg.get("port", 18765))
        r = await _core_http(port, "POST", "/api/config",
                             body={"with_shot": bool(enabled)})
        if not r["ok"]:
            return {"ok": False, "error": r.get("error") or "后端没有接受这次修改"}
        return {"ok": True, "with_shot": bool((r.get("data") or {}).get("with_shot", enabled))}

    async def set_port(self, port: int) -> dict:
        """设置后端端口：写 [server].port，返回新端口与「需重启」标记。

        三处设置页（QAM / 大屏 / 网页）共用这一个配置键；保存后由调用方
        再点「重启后端」或自动重启，新端口才会真正生效。
        """
        try:
            value = int(port)
        except (TypeError, ValueError):
            return {"ok": False, "error": "端口需为 1024-65535 之间的整数"}
        if not 1024 <= value <= 65535:
            return {"ok": False, "error": "端口需为 1024-65535 之间的整数"}
        cfg = _load_server_cfg()
        cur_port = int(cfg.get("port", 18765))
        r = await _core_http(cur_port, "POST", "/api/config",
                             body={"port": value})
        if not r["ok"]:
            return {"ok": False, "error": r.get("error") or "后端没有接受这次修改"}
        data = r.get("data") or {}
        return {"ok": True, "port": int(data.get("port", value)),
                "restart_required": bool(data.get("restart_required", True))}

    async def set_stt(self, enabled: bool) -> dict:
        """切语音输入：写后端 [stt] enable，关掉时后端顺手释放识别模型"""
        cfg = _load_server_cfg()
        port = int(cfg.get("port", 18765))
        r = await _core_http(port, "POST",
                             f"/api/stt/toggle?enabled={'true' if enabled else 'false'}")
        if not r["ok"]:
            return {"ok": False, "error": r.get("error") or "后端没有接受这次修改"}
        return {"ok": True, "stt": bool((r.get("data") or {}).get("stt_enable", enabled))}

    async def set_stt_duck(self, enabled: bool) -> dict:
        """切「录音时静音外放」：写后端 [stt] duck_output，热更新"""
        cfg = _load_server_cfg()
        port = int(cfg.get("port", 18765))
        r = await _core_http(port, "POST", "/api/config",
                             body={"stt_duck_output": bool(enabled)})
        if not r["ok"]:
            return {"ok": False, "error": r.get("error") or "后端没有接受这次修改"}
        return {"ok": True,
                "stt_duck": bool((r.get("data") or {}).get("stt_duck_output", enabled))}

    async def stt_duck(self, on: bool) -> dict:
        """录音开始/结束时静音或恢复系统输出

        后端的静音失败本身就是静默降级，这里再兜一层：任何异常都返回
        ok=True，前端按「发完就忘」处理，绝不能让静音影响录音。
        """
        try:
            cfg = _load_server_cfg()
            r = await _core_http(int(cfg.get("port", 18765)),
                                 "POST", f"/api/stt/duck?on={'true' if on else 'false'}",
                                 timeout=3.0)
            return {"ok": True, **(r.get("data") or {})}
        except Exception:
            return {"ok": True, "ducked": False}

    async def get_ask_template(self) -> dict:
        """读「截屏询问」的提示词模板（存在后端配置里，网页设置页改的也是这份）"""
        cfg = _load_server_cfg()
        r = await _core_http(int(cfg.get("port", 18765)),
                             "GET", "/api/ask-template")
        if not r["ok"]:
            return {"ok": False, "error": r.get("error") or "读取失败"}
        data = r.get("data") or {}
        return {"ok": True, "text": str(data.get("text") or ""),
                "default": str(data.get("default") or "")}

    async def get_models(self) -> dict:
        """当前模型 + 服务商可用模型列表，供面板上的下拉菜单用

        列表要现场问服务商，网络差的时候十几秒没有回音，Decky 会把它看成
        「插件卡死」并把整个 QAM 挂住。所以这里只读本机配置里的当前模型
        （本机 HTTP，毫秒级），列表另走 refresh_models，前端异步拿。
        """
        cfg = _load_server_cfg()
        port = int(cfg.get("port", 18765))
        cur = await _core_http(port, "GET", "/api/config", timeout=3.0)
        if not cur["ok"]:
            return {"ok": False, "error": cur.get("error") or "读不到后端配置", "model": ""}
        return {"ok": True, "model": str((cur.get("data") or {}).get("model") or "")}

    async def refresh_models(self) -> dict:
        """问服务商要模型列表：慢就放弃，让下拉菜单保持「只有当前模型」"""
        cfg = _load_server_cfg()
        port = int(cfg.get("port", 18765))
        cur = await _core_http(port, "GET", "/api/config", timeout=3.0)
        model = str((cur.get("data") or {}).get("model") or "") if cur["ok"] else ""
        r = await _core_http(port, "GET", "/api/models", timeout=8.0)
        if not r["ok"]:
            return {"ok": False, "error": r.get("error") or "拉取模型列表失败", "model": model}
        models = [str(m) for m in ((r.get("data") or {}).get("models") or []) if str(m).strip()]
        # 当前模型不在列表里（手填的、或服务商不返回）也要能显示出来
        if model and model not in models:
            models.insert(0, model)
        return {"ok": True, "model": model, "models": models}

    async def set_model(self, model: str) -> dict:
        """切模型：写回后端配置，下一个回合就生效"""
        name = str(model or "").strip()
        if not name:
            return {"ok": False, "error": "模型名不能为空"}
        cfg = _load_server_cfg()
        r = await _core_http(int(cfg.get("port", 18765)),
                             "POST", "/api/config", timeout=15.0, body={"model": name})
        if not r["ok"]:
            return {"ok": False, "error": r.get("error") or "后端没有接受这次修改"}
        return {"ok": True, "model": str((r.get("data") or {}).get("model") or name)}

    async def set_ask_template(self, text: str) -> dict:
        """改「截屏询问」的提示词模板：传空串表示恢复默认"""
        cfg = _load_server_cfg()
        r = await _core_http(int(cfg.get("port", 18765)),
                             "POST", "/api/ask-template", body={"text": text})
        if not r["ok"]:
            return {"ok": False, "error": r.get("error") or "后端没有接受这次修改"}
        return {"ok": True, "text": str((r.get("data") or {}).get("text") or "")}

    async def get_translate_template(self) -> dict:
        """读「截图翻译」的提示词模板（存在后端配置里，真正用于翻译请求）"""
        cfg = _load_server_cfg()
        r = await _core_http(int(cfg.get("port", 18765)),
                             "GET", "/api/translate-template")
        if not r["ok"]:
            return {"ok": False, "error": r.get("error") or "读取失败"}
        data = r.get("data") or {}
        return {"ok": True, "text": str(data.get("text") or ""),
                "default": str(data.get("default") or "")}

    async def set_translate_template(self, text: str) -> dict:
        """改「截图翻译」的提示词模板：传空串表示恢复默认"""
        cfg = _load_server_cfg()
        r = await _core_http(int(cfg.get("port", 18765)),
                             "POST", "/api/translate-template", body={"text": text})
        if not r["ok"]:
            return {"ok": False, "error": r.get("error") or "后端没有接受这次修改"}
        return {"ok": True, "text": str((r.get("data") or {}).get("text") or "")}

    async def stt_status(self) -> dict:
        """语音输入可用性：依赖、模型、录音设备三者都齐了按钮才亮"""
        cfg = _load_server_cfg()
        r = await _core_http(int(cfg.get("port", 18765)),
                             "GET", "/api/stt", timeout=3.0)
        if not r["ok"]:
            return {"ok": False, "error": r.get("error") or "读不到语音状态"}
        return {"ok": True, **(r.get("data") or {})}

    async def stt_download_model(self) -> dict:
        """启动语音模型下载（约 1GB）：后端已改成后台任务，这里秒回状态

        进度走 stt_status 里的 download 字段轮询，不再让桥接请求挂 10 分钟。
        """
        cfg = _load_server_cfg()
        r = await _core_http(int(cfg.get("port", 18765)),
                             "POST", "/api/stt/download", timeout=15.0)
        if not r["ok"]:
            return {"ok": False, "error": r.get("error") or "模型下载启动失败"}
        return {"ok": True, **(r.get("data") or {})}

    async def stt_download_cancel(self) -> dict:
        """取消模型下载：清掉半包，状态回到 cancelled"""
        cfg = _load_server_cfg()
        r = await _core_http(int(cfg.get("port", 18765)),
                             "POST", "/api/stt/download/cancel", timeout=15.0)
        if not r["ok"]:
            return {"ok": False, "error": r.get("error") or "取消失败"}
        return {"ok": True, **(r.get("data") or {})}

    async def stt_transcribe(self, wav_b64: str) -> dict:
        """WAV(base64) → 文字

        音频走 base64 而不是二进制参数：Decky 的方法桥只传 JSON，
        320KB 的 10 秒录音编成 base64 约 430KB，一次传完没问题。
        """
        try:
            wav = base64.b64decode(wav_b64 or "", validate=True)
        except Exception:
            return {"ok": False, "error": "音频编码不对"}
        if not wav:
            return {"ok": False, "error": "没有录到声音"}
        cfg = _load_server_cfg()
        port = int(cfg.get("port", 18765))
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
        except Exception as exc:
            return {"ok": False, "error": f"连不上 :{port}（{exc}）"}
        try:
            head = (
                f"POST /api/stt/transcribe HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                f"Content-Type: audio/wav\r\n"
                f"Content-Length: {len(wav)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
            writer.write(head + wav)
            await writer.drain()
            chunks = []
            while True:
                chunk = await asyncio.wait_for(reader.read(65536), timeout=120.0)
                if not chunk:
                    break
                chunks.append(chunk)
            raw = b"".join(chunks)
        except Exception as exc:
            return {"ok": False, "error": f"识别请求失败（{exc}）"}
        finally:
            try:
                writer.close()
            except Exception:
                pass
        # 手动拆响应：前面不发 header 就收不到状态码，这里按空行切开
        sep = raw.find(b"\r\n\r\n")
        if sep < 0:
            return {"ok": False, "error": "后端返回不完整"}
        body = raw[sep + 4:]
        try:
            data = json.loads(body.decode("utf-8", "replace") or "{}")
        except Exception:
            return {"ok": False, "error": "后端返回的不是 JSON"}
        if not raw.startswith(b"HTTP/1.1 200") and not raw.startswith(b"HTTP/1.0 200"):
            return {"ok": False, "error": str(data.get("detail") or "识别失败")}
        return {"ok": True, "text": str(data.get("text") or "")}

    async def core_start(self) -> dict:
        """「启动」按钮：显式清掉停止标记，再把核心拉起来

        和 start_backend 的区别：这个面向大屏页的三个核心按钮，返回里带
        already / version，前端据此决定按钮状态；停止标记只在这里和
        start_backend 里清除。
        """
        cfg = _load_server_cfg()
        port = int(cfg.get("port", 18765))
        _mark_core_stopped(False)
        core = await _core_http(port, "GET", "/api/status", timeout=2.0)
        if core.get("ok"):
            await self._stay_connected()
            ver = await _core_http(port, "GET", "/api/version", timeout=3.0)
            return {"ok": True, "already": True,
                    "version": str((ver.get("data") or {}).get("version") or "")}
        decky.logger.info("大屏页启动核心 :%s", port)
        ok, why = await _start_backend(port)
        if not ok:
            decky.logger.error("启动核心失败：%s", why)
            return {"ok": False, "error": why}
        await _drop_backend()
        await self._stay_connected()
        ver = await _core_http(port, "GET", "/api/version", timeout=3.0)
        return {"ok": True, "already": False,
                "version": str((ver.get("data") or {}).get("version") or "")}

    async def core_restart(self) -> dict:
        """重启女仆核心：由插件自己收掉旧进程，再用启动时那套命令拉起

        不能让后端自己重启（见 server/main.py 的 /api/restart）：
        后端是被这里用 `runuser -u deck -- <venv>/bin/python ...` 降权拉起的，
        它自己 `pkill -f 'uvicorn server.main:app'` 会把外层 runuser 一起带走，
        再用 nohup 就地起一份——那份既没有 runuser 包装、又和插件失去联系，
        实测结果就是旧进程没了、新的没起来，重启变成「把后端关掉」。
        插件这边握着完整的启动命令（_start_backend），所以由它来重放。
        """
        # 「关闭」之后必须保持停止：重启不算启动，不能顺手把标记清掉
        if _core_stopped():
            return {"ok": False, "error": "核心已停止，先点「启动」"}
        cfg = _load_server_cfg()
        port = int(cfg.get("port", 18765))

        # 收掉旧进程（含外层 runuser 包装），再按启动时那套命令重放
        await _stop_backend(port)
        ok, why = await _start_backend(port)
        if not ok:
            return {"ok": False, "error": f"重启失败：{why}"}
        back = await _core_http(port, "GET", "/api/version", timeout=3.0)
        ver = str((back.get("data") or {}).get("version") or "") if back["ok"] else ""
        return {"ok": True, "version": ver}

    async def core_kill(self) -> dict:
        """关闭核心并保持停止：先落停止标记，再收进程

        标记先写：这样即便正好有一次自动拉起在排队，看到标记也会跳过。
        插件加载时的 _autostart_backend、以及系统的 restart 策略都不会再拉它；
        只有用户点「启动」（core_start / start_backend）才会恢复。
        """
        cfg = _load_server_cfg()
        port = int(cfg.get("port", 18765))
        _mark_core_stopped(True)
        await _stop_backend(port)
        # 主动断掉 ws，别让读循环在后台对着已死的端口反复重连
        await _drop_backend()
        return {"ok": True}

    async def screenshot(self, hide: bool = False) -> dict:
        """抓当前游戏画面

        hide=True 时前端会先把 QAM 收起来：gamescope 的 pipewiresrc 抓的是
        合成后的画面，QAM 面板本身也在里面。收面板到真正抓帧之间要留够时间
        —— 面板的收起动画加一次重绘接近 1 秒，短了抓到的还是「游戏 + 侧栏」，
        甚至抓到侧栏正在切走的通知页。
        """
        # 和抓帧一起把当前游戏的 appid/名字带回去：查攻略时要拼进提示词，
        # 单独再开一次接口既慢又容易和画面错位。这一项要读 vdf、翻进程表，
        # 放在等画面之前做，省下来的时间正好也留给侧栏收干净。
        game = await asyncio.to_thread(_current_game)
        if hide:
            await asyncio.sleep(1.2)
        cfg = _load_server_cfg()
        port = int(cfg.get("port", 18765))
        # 先按模式分流：有 gamescope 节点才是游戏模式，插件自己从 PipeWire 抓；
        # 桌面模式（含从桌面进 Big Picture）没有节点，交给后端的 spectacle/mss
        node, node_why = await _gamescope_pw_node()
        if node:
            ok, payload, need_poke = await grab_screenshot(node, node_why)
        else:
            ok, payload = await _desktop_screenshot(port)
            need_poke = False
        if not ok:
            if need_poke:
                # 画面静止：这是「请用户配合一下」，不是故障，用 info 记
                decky.logger.info(f"screenshot 需要用户动一下画面：{payload}")
                return {"ok": False, "error": payload, "needPoke": True, "game": game}
            if node:
                # 失败时顺手把节点状态记一笔：gamescope 节点在 idle 时是 suspended，
                # 「节点在但状态不对」和「节点没了」要分得开，否则只能反复猜
                state = await _node_state_note()
                decky.logger.error(f"screenshot 失败：{payload}；{state}")
                return {"ok": False, "error": f"{payload}（{state}）", "game": game}
            # 桌面模式：再附「没找到 gamescope 节点」只会误导人，给一句人话
            decky.logger.error(f"桌面模式截图失败：{payload}")
            return {"ok": False,
                    "error": f"桌面模式下暂时截不到画面（{payload}）", "game": game}
        # 图片只落到本地文件，不经过 Decky 的 IPC 回传：一张 1280x800 的 PNG
        # base64 有 16MB 左右，这么大的字符串塞进 JS↔Python 的调用会静默失败
        # ——前端既收不到图，随后的 start_turn 也整个派发不到后端。
        shot = Path(_SHOT_DIR) / f"shot_{os.getpid()}_{next(_SHOT_SEQ)}.jpg"
        shot.parent.mkdir(parents=True, exist_ok=True)
        shot.write_bytes(base64.b64decode(payload))
        decky.logger.info(f"screenshot 返回 ok，图存到 {shot}（{shot.stat().st_size} 字节）")
        global _LAST_SHOT_PATH
        _LAST_SHOT_PATH = str(shot)
        # 面板上的预览另存一张缩略图：确认「截到的是哪一帧」不需要原图，
        # 几十 KB 的 JPEG 才塞得进返回值
        return {"ok": True, "shot": str(shot), "thumb": _make_thumb(shot), "game": game}

    async def start_turn(
        self, text: str, shot: str | None = None, brief: str = "",
        surface: str = "", client: str = "",
    ) -> dict:
        """把一轮对话发给后端。返回后流式内容通过 deckmeido_delta 推给前端

        shot 是 screenshot 落盘的文件路径（不是 base64）：16MB 的图片数据
        塞进 Decky 的 IPC 会让整次调用静默失败，所以图只在插件进程内部读。
        brief 只在面板上顶替正文：截屏询问的提示词有一百多字，长显会把面板撑开，
        发出去的仍然是 text 本身。
        surface 标明这一轮来自哪个入口：chat=大屏聊天页（放开多轮），
        ask/translate=询问页与截图翻译（保持一问一答）。不传按旧行为处理。
        client 标明是哪个界面发起的：bigscreen=大屏，其余（默认）按 qam 面板处理；
        后端据此保证只有大屏聊天页放开单轮限制。
        """
        decky.logger.info(f"start_turn 被调用：{len(text or '')} 字，图 {shot or '(无)'}")
        global _ACC, _LAST_REPLY, _STREAM_RUNNING, _STREAM_SINCE, _TURN_SHOT_PATH
        text = str(text or "").strip()
        if not text:
            return {"ok": False, "error": "问题是空的"}
        if _TURN_LOCK.locked():
            # 上一轮还在跑：面板上连点两下发送就会走到这里。不拦住的话两条流
            # 共用 _ACC/_FINAL_EVENT，正文会混在一起
            return {"ok": False, "error": "上一轮还没结束，先等它或者点终止"}

        image = ""
        if shot:
            try:
                raw = Path(shot).read_bytes()
                image = base64.b64encode(raw).decode("ascii")
                # 读到的字节数和写入时的对不上，说明文件在两次调用之间被清空/替换了
                decky.logger.info(f"start_turn：读图 {len(raw)} 字节 -> base64 {len(image)} 字符")
                if not raw:
                    decky.logger.error(f"start_turn：图文件是空的（{shot}），这一轮退回纯文字")
            except Exception as exc:
                # 图读不出来不阻断这一轮：退回纯文字，并在日志里留原因
                decky.logger.error(f"start_turn 读图失败（{shot}）：{exc}")

        bk, why = await _ensure_backend()
        if not bk:
            return {"ok": False, "error": why}
        await _TURN_LOCK.acquire()
        _ACC = ""
        _LAST_REPLY = ""
        _FINAL_EVENT.clear()
        _STREAM_RUNNING = True
        # 本轮 turn_id：由本端生成，_read_loop 靠它把 δ 和 final 认出来。
        # 同一时刻只允许一轮，所以发之前先把它记在连接上
        bk.turn = f"qam-{os.urandom(6).hex()}"
        payload = {"type": "chat", "text": text, "client": "qam",
                   # device 必须带上：后端靠 (client, device) 认领「挂起的高危操作」，
                   # 少了它 pending_action 永远匹配不上，用户在面板上说「确认」
                   # 会被当成普通提问再问一遍要不要执行
                   "device": _QAM_DEVICE,
                   "turn_id": bk.turn}
        if brief:
            payload["brief"] = str(brief).strip()
        if surface:
            payload["surface"] = str(surface).strip()
        if client:
            payload["client"] = str(client).strip()
        if image:
            payload["image"] = image
            payload["image_prompt"] = "（这是当前游戏画面，结合画面回答用户的问题）"
        # 这一轮的画面路径留到收尾：后端广播里万一没带 events（落盘失败、
        # 历史被裁），user_message 还能用本地这份把面板的图补上
        _TURN_SHOT_PATH = str(shot) if image else ""
        ws = _workspace()
        if ws:
            payload["workspace"] = ws
        sock = bk.sockname()
        decky.logger.info(f"start_turn：正文 {len(text)} 字，图 {len(image)} 字符，经 {sock} 发出")
        try:
            await bk.send(payload)
        except Exception as exc:
            decky.logger.error(f"start_turn 发送失败：{exc}")
            # 没发出去就没有这一轮，锁要还回去，否则面板从此永远「上一轮没结束」
            _TURN_LOCK.release()
            _STREAM_RUNNING = False
            return {"ok": False, "error": f"发不出去：{exc}"}
        decky.logger.info("start_turn：已发出")
        # 提问不等后端回声，先在面板上落一条：后端广播回来时会按 ts 去重，
        # 不会出现两条一模一样的「插件端」。
        # 本机这次截的图连同上面这份 payload 一起交给后端了：清掉本地记录，
        # 免得下一次别的端发来的、恰好也没带图的提问被塞上这张
        global _LAST_SHOT_PATH
        _LAST_SHOT_PATH = ""
        return {"ok": True}

    async def wait_final(self, timeout: float = 900) -> dict:
        """等本轮收尾，返回完整正文。前端收到 final 事件就不用调这个"""
        try:
            await asyncio.wait_for(_FINAL_EVENT.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            # 等了这么久还没收尾：把锁还回去，否则之后每一轮都报「上一轮没结束」
            _finalize(cancelled=True)
            return {"ok": False, "error": f"等了 {timeout:.0f}s 还没有答复"}
        return {"ok": True, "text": _LAST_REPLY}

    async def cancel(self) -> dict:
        """掐掉这一轮：直接调后端的 /api/cancel，连接保持不动

        以前这里先 _drop_backend() 再发请求，等于每次终止都把 ws 拆掉：
        拆掉的后果是全端广播断档、读循环退出、下一轮又要重连一次；而且面板还
        要自己补一句「已中断」，和后端回的「已终止当前操作」叠成两句。
        现在只发 HTTP 终止请求，收尾交给后端广播（它会带 cancelled=True 的
        assistant_message），连接一直活着。
        """
        cfg = _load_server_cfg()
        port = int(cfg.get("port", 18765))
        err = ""
        writer = None
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            req = (
                "POST /api/cancel?client=qam&device=deck HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                "Content-Length: 0\r\n"
                "Connection: close\r\n\r\n"
            )
            writer.write(req.encode())
            await writer.drain()
            resp = await asyncio.wait_for(reader.read(), timeout=10)
            if b" 200 " not in resp.split(b"\r\n", 1)[0]:
                err = resp.split(b"\r\n", 1)[0].decode("utf-8", "replace")[:80]
        except Exception as exc:
            err = str(exc)
        finally:
            # 读超时/连接被拒时异常路径也要关：不然每次终止都漏一个 fd
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
        if err:
            # 终止请求没送到（后端挂了）：连接上的收尾消息也等不到了，
            # 就地冻结，别让面板一直转圈
            decky.logger.warning(f"/api/cancel 失败：{err}")
            _finalize(cancelled=True)
        else:
            # 终止请求已受理：先把本端状态熄掉，重挂面板/读缓存时不会再点亮
            global _STREAM_RUNNING, _STREAM_SINCE
            _STREAM_RUNNING = False
            _STREAM_SINCE = 0.0
        return {"ok": not err, "error": err}

    async def _main(self):
        decky.logger.info(f"DeckMeido 插件已加载 v{_PLUGIN_VERSION}，安装目录 {INSTALL_DIR}")
        _run_as()
        # 以 root 加载会把属主写歪，加载时先纠一次，免得 deck 用户 git pull 失败
        _restore_owner()
        # 开机默认进游戏模式，这时后端通常没在跑，用户不点「启动后端」就等于插件不可用。
        # 后台自动拉一次：成功与否都不打扰用户，面板上本来就有状态和执行按钮。
        asyncio.get_running_loop().create_task(self._autostart_backend())

    async def _autostart_backend(self, retries: int = 3) -> None:
        """插件加载时自动拉起后端（失败不提示，面板上的按钮仍可手动重试）

        开机那几十秒里网络、磁盘都可能还没就绪，所以退避重试几次；
        后端自己有单实例守卫，和客户端同时拉起也不会起两份。

        ⚠️ 用户点过「关闭核心」时（core_stopped 标记在），这里必须完全跳过：
        这就是「关闭后保持停止」的那条自动拉起路径。只有用户点「启动」
        （core_start / start_backend）清掉标记后，下一次插件加载才会再自动拉起。
        """
        if _core_stopped():
            decky.logger.info("自动启动后端跳过：用户已点过「关闭核心」")
            return
        cfg = _load_server_cfg()
        port = int(cfg.get("port", 18765))
        if not cfg:
            decky.logger.warning(f"自动启动后端跳过：{_cfg_error_text()}")
            return
        for attempt in range(1, retries + 1):
            await asyncio.sleep(3 if attempt == 1 else 10)
            # 等待期间用户可能在大屏页点了「关闭核心」：每一轮都重新检查
            if _core_stopped():
                decky.logger.info("自动启动后端中止：用户已点过「关闭核心」")
                return
            core = await _core_http(port, "GET", "/api/status", timeout=2.0)
            if core.get("ok"):
                decky.logger.info(f"后端已在运行 :{port}，无需自动启动")
                await self._stay_connected()
                return
            decky.logger.info(f"自动启动后端 :{port}（第 {attempt}/{retries} 次）")
            ok, why = await _start_backend(port)
            if ok:
                decky.logger.info(f"后端已自动启动 :{port}")
                await self._stay_connected()
                return
            decky.logger.warning(f"自动启动后端失败（第 {attempt} 次）：{why}")
        decky.logger.error("自动启动后端多次失败，需要用户在面板上手动启动")

    async def _stay_connected(self) -> None:
        """插件加载后就把 WS 连上，并一直保持

        以前只在面板打开会话 / 发消息时才连，于是「AI 调工具时没人连在后端」
        很常见：后端广播的 pad_order 这类全局事件就全丢了（手柄换序一直不生效
        就是这么来的）。这里主动连一条，断了由 _read_loop 自己重连。
        """
        try:
            bk, why = await _ensure_backend()
        except Exception as exc:
            decky.logger.warning(f"插件加载后连接后端失败：{exc}")
            return
        if bk is None:
            decky.logger.warning(f"插件加载后连接后端失败：{why}")
        else:
            decky.logger.info("插件加载后已连上后端，等待推送")

    async def _unload(self):
        await _drop_backend()
        decky.logger.info("DeckMeido 插件卸载")

    async def _uninstall(self):
        await _drop_backend()
        decky.logger.info("DeckMeido 插件卸载")
