"""自定义主题接口：上传壁纸、撤销、微调配色、读取当前主题。

路由模式与 routes_files 一致：init(require_auth, get_main) 注入鉴权与运行态读取，
register(app) 时才定义路由函数，保持 main.api_xxx 的既有写法。

壁纸与配色存 config.toml 的 [appearance] 段：
    wallpaper = "wallpaper.jpg"      # 空串 = 无壁纸
    palette   = '{"dark":{...},"light":{...},"source":"#xxx"}'   # JSON 字符串
    dim       = 45                   # 压暗 0-80
    blur      = 0                    # 背景模糊 px 0-24
文件本身在 userdata/wallpapers/，由 /wallpapers 静态挂载公开提供（<img> 带不了令牌）。
"""
from __future__ import annotations

import re
import time

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import paths, themecolor

router = APIRouter()

require_auth = None
get_main = None

_REGISTERED: dict = {}
__all_routes__ = ("api_theme_get", "api_theme_wallpaper", "api_theme_clear",
                  "api_theme_palette", "api_theme_config", "api_theme_candidate")


class _TuneReq(BaseModel):
    dim: int | None = None
    blur: int | None = None


# dim 在前端是「界面透明度」：越大上层 UI 越透、壁纸越清楚（见 index.html
# 的 applyCfgAppearance）。新装默认 45，与设置页滑条的初始位置一致
DIM_DEFAULT = 45
BLUR_DEFAULT = 0


class _PaletteReq(BaseModel):
    brand: str | None = None


def init(require_auth_fn, get_main_fn) -> None:
    global require_auth, get_main
    require_auth = require_auth_fn
    get_main = get_main_fn
    router.dependencies.append(Depends(require_auth))


def _appearance() -> dict:
    """config.toml 的 [appearance] 段（现读，apply_config 换绑后仍然有效）"""
    cfg = get_main().CFG.get("appearance") or {}
    return cfg if isinstance(cfg, dict) else {}


def _clamp_int(value, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def current_theme() -> dict:
    """当前主题的快照：壁纸 URL（含版本号防缓存）、调色板、压暗/模糊。

    配置里记的文件名可能已被手动删掉，这里以磁盘为准兜底。
    """
    app = _appearance()
    name = str(app.get("wallpaper") or "")
    if name and not (paths.wallpapers_dir() / name).is_file():
        name = ""
    if not name:
        found = themecolor.wallpaper_files()
        name = found[0] if found else ""
    palette = themecolor.load_palette(app.get("palette"))
    if name and not palette:
        palette = None
    dim = _clamp_int(app.get("dim"), DIM_DEFAULT, 0, 80)
    blur = _clamp_int(app.get("blur"), BLUR_DEFAULT, 0, 24)
    return {
        "wallpaper": name,
        # 带上修改时间：同文件名换图后浏览器不会继续用旧缓存
        "url": f"/wallpapers/{name}?v={_mtime(name)}" if name else "",
        "thumb": f"/wallpapers/wallpaper_thumb.jpg?v={_mtime(name)}" if name else "",
        "palette": palette,
        # 壁纸取到的原始强调色：前端「恢复默认强调色」用它重算整套配色
        "source": str((palette or {}).get("source") or ""),
        "dim": dim,
        "blur": blur,
        # 这两项的落盘时间（毫秒）。前端拿它判断一份快照是不是自己那次保存
        # 之前的旧货：/api/config 是异步拉的，回来时可能比刚存的值还旧
        "dim_ts": _stamp(str(app.get("dim_ts") or "")),
        "blur_ts": _stamp(str(app.get("blur_ts") or "")),
    }


def _stamp(value: str) -> int:
    """配置里的保存时间戳（毫秒字符串）-> int；没有或坏数据按 0"""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _mtime(name: str) -> int:
    try:
        return int((paths.wallpapers_dir() / name).stat().st_mtime)
    except OSError:
        return 0


async def _publish(updates: dict, data: dict | None = None) -> None:
    """写配置并把新主题推给所有端（网页/客户端/插件/大屏）

    data 只给 api_theme_config 用：它刚把两项一起写完，快照就是这次的结果。
    其余广播一律现读配置 —— 曾经在这里加过「拖动窗口内沿用进程里缓存的
    旧值」，进程重启后缓存是默认值，换壁纸的广播就会把滑条打回 0。
    """
    m = get_main()
    m.save_config({"appearance": updates})
    try:
        await m.manager.broadcast({"type": "theme_changed", "data": data or current_theme()})
    except Exception:
        pass


def publish_saved(name: str, palette: dict) -> None:
    """工具换了壁纸后调用：写配置并同步所有端。

    tools 是同步上下文、也没有事件循环的引用，这里就用 asyncio.run 起一个
    短任务；正在跑的 agent 回合本身在另一个任务里，互不阻塞。
    """
    import asyncio
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_publish({"wallpaper": name,
                              "palette": themecolor.dump_palette(palette)}))
        return
    # 已在事件循环里（工具由 agent 回合调用）：排到循环上，不等待
    asyncio.ensure_future(_publish({"wallpaper": name,
                                   "palette": themecolor.dump_palette(palette)}))


def publish_reset() -> None:
    """工具撤销壁纸后调用：清壁纸与配色并同步所有端（调用约定同 publish_saved）"""
    import asyncio
    updates = {"wallpaper": "", "palette": ""}
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_publish(updates))
        return
    asyncio.ensure_future(_publish(updates))


def register(app) -> None:
    @router.get("/api/theme")
    async def api_theme_get():
        return current_theme()

    @router.post("/api/theme/wallpaper")
    async def api_theme_wallpaper(file: UploadFile = File(...)):
        raw = await file.read(themecolor.WALLPAPER_MAX + 1)
        try:
            name, palette = themecolor.save_wallpaper(raw)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        await _publish({
            "wallpaper": name,
            "palette": themecolor.dump_palette(palette),
        })
        return {"ok": True, "theme": current_theme()}

    @router.post("/api/theme/clear")
    async def api_theme_clear():
        themecolor.clear_wallpaper()
        await _publish({"wallpaper": "", "palette": ""})
        return {"ok": True, "theme": current_theme()}

    @router.post("/api/theme/palette")
    async def api_theme_palette(req: _PaletteReq):
        app_cfg = _appearance()
        palette = themecolor.load_palette(app_cfg.get("palette")) or {}
        if req.brand is not None:
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", req.brand.strip()):
                return {"ok": False, "error": "颜色格式应为 #rrggbb"}
            palette = themecolor.palette_from_color(req.brand.strip())
        elif not palette:
            return {"ok": False, "error": "当前没有配色可调"}
        await _publish({"palette": themecolor.dump_palette(palette)})
        return {"ok": True, "theme": current_theme()}

    @router.post("/api/theme/config")
    async def api_theme_config(req: _TuneReq):
        """压暗 / 模糊：两个滑杆停手后落到这里，别的端立刻跟着变

        只改传上来的那一项：另一项不在 updates 里，所以不会被这次调用
        顺手用「读-改-写」的旧值盖掉；同时记下这一项的落盘时间，
        前端据此丢掉自己保存之前拍下的旧快照。
        """
        now = int(time.time() * 1000)
        updates = {}
        if req.dim is not None:
            updates["dim"] = _clamp_int(req.dim, DIM_DEFAULT, 0, 80)
            updates["dim_ts"] = str(now)
        if req.blur is not None:
            updates["blur"] = _clamp_int(req.blur, BLUR_DEFAULT, 0, 24)
            updates["blur_ts"] = str(now)
        await _publish(updates)
        return {"ok": True, "theme": current_theme()}

    @router.get("/api/theme/candidates/{cid}")
    async def api_theme_candidate(cid: str):
        """候选壁纸图。给聊天里那 4 张预览用，不鉴权（静态图，和 /wallpapers 同口径）。
        文件过期被回收过就 404，前端会显示「已过期」而不是一张破图"""
        p = themecolor.candidate_path(cid)
        if p is None:
            raise HTTPException(404, "候选图已过期")
        return FileResponse(p, headers={"Cache-Control": "public, max-age=3600"})

    app.include_router(router)
    _REGISTERED.update({name: locals()[name] for name in __all_routes__})
    globals().update(_REGISTERED)
