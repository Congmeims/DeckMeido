"""自定义主题：从壁纸取主色，推导深/浅两套配色变量。

只做纯计算与落盘，不依赖 main 的运行态，方便单独测试：

- `extract_palette(img_bytes)`：Pillow 打开 → 缩样 → 量化取主色 → 生成两套变量
- `save_wallpaper(img_bytes)`：校验格式 → 写 wallpapers/wallpaper.<ext> + 缩略图
- `clear_wallpaper()`：删掉壁纸文件与缩略图
- `apply_to_config(updates_fn, palette, filename, dim, blur)` 不在这里：
  配置写入沿用 main.update_config，路由和 AI 工具各自调。

配色规则：
- 主色 `brand`：主色相 + 饱和度，明度抬到 0.62（深色底上作强调色可读）
- 深色 `bg/surface`：同色相，低饱和（0.18/0.22），明度 0.07/0.11
- 浅色 `bg/surface`：同色相，低饱和，明度 0.97/0.93
- `user_bubble` 与 `brand` 一致
"""
from __future__ import annotations

import colorsys
import io
import json
import re
import uuid

WALLPAPER_MAX = 20 * 1024 * 1024          # 与附件上限一致
FALLBACK = "#1a9fff"                     # 取不出主色时退回品牌蓝
_EXT_BY_FMT = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp", "GIF": "gif", "BMP": "bmp"}
MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"RIFF", "webp"),                    # 需再判 WEBP 字样，见 sniff_ext
    (b"GIF8", "gif"),
    (b"BM", "bmp"),
)


def sniff_ext(head: bytes) -> str | None:
    """按魔数判断图片类型；不是图片返回 None"""
    for magic, ext in MAGIC:
        if head.startswith(magic):
            if ext == "webp" and head[8:12] != b"WEBP":
                continue
            return ext
    return None


def _hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(c)))) for c in rgb)


def _hsl_hex(h: float, s: float, l: float) -> str:
    r, g, b = colorsys.hls_to_rgb(h % 1.0, max(0.0, min(1.0, l)), max(0.0, min(1.0, s)))
    return _hex((r * 255, g * 255, b * 255))


def dominant_color(img_bytes: bytes) -> str:
    """取一张图的主色（十六进制）。

    缩到 64×64 后量化成 8 色，逐色算「占比 × 饱和度」，跳过近黑/近白
    （约占 8% 亮度两端）与低饱和的灰，选得分最高的。全灰或全黑时退回
    FALLBACK —— 壁纸结构色太重时宁可要一个可用的强调色，也不要刺眼的灰。
    """
    from PIL import Image
    try:
        raw = Image.open(io.BytesIO(img_bytes))
        raw.load()
    except Exception:
        return FALLBACK
    try:
        img = raw.convert("RGBA")
        # 透明底先垫白：PNG 图标类壁纸大量像素是透明的，不然主色会是「黑」
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        bg.alpha_composite(img)
        img = bg.convert("RGB").resize((64, 64))
        pal = img.convert("P", palette=Image.Palette.ADAPTIVE, colors=8)
        counts = pal.getcolors() or []
        total = sum(c for c, _ in counts) or 1
        best_score = 0.0
        best = None
        for count, idx in counts:
            r, g, b = pal.getpalette()[idx * 3: idx * 3 + 3]
            h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
            if l < 0.12 or l > 0.92:
                continue                      # 近黑近白，当背景处理
            if s < 0.12:
                continue                      # 灰白，撑不起强调色
            score = (count / total) * (0.35 + s)
            if score > best_score:
                best_score, best = score, (h, s, l)
    except Exception:
        return FALLBACK
    if best is None:
        return FALLBACK
    h, s, _ = best
    return _hsl_hex(h, max(0.45, min(1.0, s * 1.1)), 0.62)


def _luminance(hx: str) -> float:
    """#rrggbb 的相对亮度（0~1，sRGB 加权），用来判断配色够不够亮/暗"""
    m = re.fullmatch(r"#([0-9a-fA-F]{6})", (hx or "").strip())
    if not m:
        return 0.0
    v = int(m.group(1), 16)
    r, g, b = (v >> 16) & 255, (v >> 8) & 255, v & 255

    def lin(c: int) -> float:
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def palette_from_color(hex_color: str) -> dict:
    """由一个强调色生成浅色 / 深色 / 暗黑三套主题变量

    同一张壁纸在三档主题下应该拿到三个不同亮度的强调色：浅色档要鲜亮，
    深色档要沉一点，暗黑档最暗但不能是黑（黑字压在黑底上等于消失）。
    HSL 的明度 0.5 附近就是纯色本身，往上加亮饱和度会掉、往下压会发灰，
    所以三档这样取：
        浅色 0.50（饱和度高，压在浅底上跳得出）
        深色 0.44（略沉）
        暗黑 0.36（最暗，但仍保证相对亮度 ≥ 0.10，不会糊进 #000）
    色相始终来自壁纸主色，三档是「同一个色、不同明度」。
    """
    m = re.fullmatch(r"#([0-9a-fA-F]{6})", (hex_color or "").strip())
    if not m:
        hex_color = FALLBACK
        m = re.fullmatch(r"#([0-9a-fA-F]{6})", hex_color)
    v = int(m.group(1), 16)
    r, g, b = (v >> 16) & 255, (v >> 8) & 255, v & 255
    h, _l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    sat = max(0.45, min(1.0, s * 1.1))

    def brand_at(lum: float, sat_v: float = sat) -> str:
        """按目标明度出一个强调色；过暗时往上提，保证不是近黑色"""
        out = _hsl_hex(h, sat_v, lum)
        floor = 0.10
        for _ in range(6):
            if _luminance(out) >= floor:
                break
            lum = min(0.9, lum + 0.05)
            out = _hsl_hex(h, sat_v, lum)
        return out

    brand_light = brand_at(0.50, max(0.55, min(1.0, s * 1.25)))
    brand_dark = brand_at(0.44)
    brand_oled = brand_at(0.36)
    dark = {
        "brand": brand_dark,
        "user_bubble": brand_dark,
        "bg": _hsl_hex(h, 0.18, 0.07),
        "surface": _hsl_hex(h, 0.22, 0.11),
        # 第二层底：气泡、输入框、小灰字提示条的衬底。比 surface 再亮一档，
        # 深色主题下这两层才分得开（以前前端只拿三档主题的固定灰，
        # 放着壁纸配色不用，浅色档下就是一块浅灰压在花图上）
        "surface_2": _hsl_hex(h, 0.20, 0.15),
    }
    light = {
        "brand": brand_light,
        "user_bubble": brand_light,
        "bg": _hsl_hex(h, 0.14, 0.97),
        "surface": _hsl_hex(h, 0.16, 0.93),
        "surface_2": _hsl_hex(h, 0.18, 0.90),
    }
    # 暗黑档：除了强调色更暗，底色也要比深色档更黑（面板本来是半透的，
    # 只改 surface 的话两档在屏幕上几乎一样，所以 bg/surface 一起压）
    oled = {
        "brand": brand_oled,
        "user_bubble": brand_oled,
        "bg": "#000000",
        # 明度压到 0.02 / 0.04：面板是半透叠加在壁纸上的，明度再往上一点
        # 在屏幕上就和纯黑分不开了，用户反馈「暗黑还不够黑」就是在说这一档
        "surface": _hsl_hex(h, 0.22, 0.02),
        "surface_2": _hsl_hex(h, 0.20, 0.04),
    }
    return {"dark": dark, "light": light, "oled": oled, "source": hex_color}


def extract_palette(img_bytes: bytes) -> dict:
    """壁纸字节 -> {dark, light, oled, source}"""
    return palette_from_color(dominant_color(img_bytes))


def image_lum(img_bytes: bytes) -> float:
    """整张图的平均亮度（0~1，按 sRGB 相对亮度加权）。

    前端拿它给 --muted（小灰字）分档：浅色壁纸透上来时面板变亮，灰字要加深，
    深色壁纸反过来。取不出图时按 0.5（中间）处理，等于不加不减。
    """
    from PIL import Image
    try:
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB").resize((32, 32))
        px = list(img.getdata())
    except Exception:
        return 0.5
    if not px:
        return 0.5

    def lin(c: int) -> float:
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    return sum(0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
               for r, g, b in px) / len(px)


def lum_bucket(lum: float) -> str:
    """平均亮度 -> light / mid / dark 三档（前端按档位调灰字深浅）"""
    if lum >= 0.55:
        return "light"
    if lum <= 0.22:
        return "dark"
    return "mid"


def wallpaper_lum() -> float:
    """当前壁纸的平均亮度：优先读保存时记下的缓存，没有就把壁纸缩一遍现算"""
    from . import paths
    folder = paths.wallpapers_dir()
    try:
        cached = (folder / "wallpaper_lum.txt").read_text().strip()
        return max(0.0, min(1.0, float(cached)))
    except (OSError, ValueError):
        pass
    found = wallpaper_files()
    if not found:
        return 0.5
    try:
        return image_lum((folder / found[0]).read_bytes())
    except OSError:
        return 0.5


def dump_palette(palette: dict) -> str:
    """配置里 palette 一栏是 JSON 字符串（TOML 里存嵌套表会丢注释，保持扁平）"""
    return json.dumps(palette, ensure_ascii=False, separators=(",", ":"))


def load_palette(text) -> dict | None:
    """配置里读回的 palette 字符串 -> dict；坏数据返回 None"""
    if isinstance(text, dict):
        return text if text.get("dark") else None
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("dark"), dict):
        return None
    # 老配置存的是「只含 brand/bg/surface」的配色：缺的键按强调色现算补上，
    # 不然 surface_2 没有值，壁纸模式下小灰字和第二层衬底会退回固定灰
    src = str(data.get("source") or data["dark"].get("brand") or "")
    fresh = palette_from_color(src)
    for mode in ("dark", "light"):
        got = data.get(mode) if isinstance(data.get(mode), dict) else {}
        for k, v in fresh[mode].items():
            got.setdefault(k, v)
        data[mode] = got
    return data


def save_wallpaper(img_bytes: bytes) -> tuple[str, dict]:
    """校验并落盘壁纸，返回 (文件名, 调色板)。

    固定文件名 wallpaper.<ext>：换图先删旧的一族文件（不同扩展名的残留也清掉），
    另存一张长边 480 的缩略图给设置页预览。
    抛 ValueError（不是图片 / 太大），由调用方转成 400。
    """
    from PIL import Image
    from . import paths

    if len(img_bytes) > WALLPAPER_MAX:
        raise ValueError(f"图片超过 {WALLPAPER_MAX // (1024 * 1024)}MB")
    ext = sniff_ext(img_bytes[:16])
    if not ext:
        raise ValueError("不是支持的图片格式")
    try:
        img = Image.open(io.BytesIO(img_bytes))
        img.load()
    except Exception:
        raise ValueError("图片解析失败") from None
    fmt = _EXT_BY_FMT.get(img.format or "", ext)
    if img.format and img.format not in _EXT_BY_FMT:
        raise ValueError(f"不支持 {img.format} 格式")
    ext = fmt

    folder = paths.wallpapers_dir()
    clear_wallpaper()
    name = f"wallpaper.{ext}"
    (folder / name).write_bytes(img_bytes)

    thumb = img.convert("RGB")
    thumb.thumbnail((480, 480))
    thumb.save(folder / "wallpaper_thumb.jpg", "JPEG", quality=82)
    # 平均亮度顺手记下来：前端每次开设置都要读，不想每次都解一遍原图
    try:
        (folder / "wallpaper_lum.txt").write_text(f"{image_lum(img_bytes):.4f}")
    except OSError:
        pass
    # 设置页里预览用的长边 960 图：原图动辄几 MB，从设置切进来时要等整张传完
    # 才看得到东西；缩略图 480 太糊，这一张放在两者之间，只在需要时加载
    preview = img.convert("RGB")
    preview.thumbnail((960, 960))
    preview.save(folder / "wallpaper_preview.jpg", "JPEG", quality=86)
    return name, extract_palette(img_bytes)


def clear_wallpaper() -> None:
    """删掉壁纸及其缩略图（不存在就跳过）"""
    from . import paths
    folder = paths.wallpapers_dir()
    for p in folder.glob("wallpaper*"):
        try:
            p.unlink()
        except OSError:
            pass


# ---------- 壁纸候选（AI 搜图后先给用户挑，选中才应用） ----------

# 候选图单独放一个目录，不碰 wallpaper.* 那一族，所以预览阶段完全不影响当前壁纸。
# 上限 16 张：一轮最多给 4 张，连搜几次也不会把盘塞满
CANDIDATE_KEEP = 16


def candidates_dir():
    from . import paths
    d = paths.wallpapers_dir() / "candidates"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_candidate(img_bytes: bytes) -> tuple[str, str]:
    """把下载到的候选图落盘，返回 (id, 扩展名)。只校验不取色。

    取色留到真正应用时再做：用户挑不中的图没必要跑一遍量化。
    """
    from PIL import Image

    if len(img_bytes) > WALLPAPER_MAX:
        raise ValueError(f"图片超过 {WALLPAPER_MAX // (1024 * 1024)}MB")
    ext = sniff_ext(img_bytes[:16])
    if not ext:
        raise ValueError("不是支持的图片格式")
    try:
        img = Image.open(io.BytesIO(img_bytes))
        img.load()
    except Exception:
        raise ValueError("图片解析失败") from None
    ext = _EXT_BY_FMT.get(img.format or "", ext)

    cid = uuid.uuid4().hex[:12]
    folder = candidates_dir()
    (folder / f"{cid}.{ext}").write_bytes(img_bytes)
    _trim_candidates()
    return cid, ext


def _trim_candidates() -> None:
    """按修改时间保留最新 CANDIDATE_KEEP 张，旧的直接删"""
    folder = candidates_dir()
    files = []
    for pat in ("*.png", "*.jpg", "*.webp", "*.gif", "*.bmp"):
        files.extend(folder.glob(pat))
    files.sort(key=lambda p: p.stat().st_mtime)
    for p in files[:-CANDIDATE_KEEP]:
        try:
            p.unlink()
        except OSError:
            pass


def candidate_path(cid: str):
    """按 id 找回候选图文件；id 格式不对或文件已过期返回 None"""
    if not re.fullmatch(r"[0-9a-f]{12}", cid or ""):
        return None
    for p in candidates_dir().glob(f"{cid}.*"):
        return p
    return None


def candidate_url(cid: str) -> str:
    # 必须走 /api/theme/candidates/<id>：候选图落盘带扩展名，而 /wallpapers
    # 静态挂载按原名找文件，/wallpapers/candidates/<id> 一定 404
    return f"/api/theme/candidates/{cid}"


def wallpaper_files() -> list[str]:
    """现存壁纸文件名（用于配置里记录的旧名字已不存在时做兜底查找）"""
    from . import paths
    return sorted(p.name for p in paths.wallpapers_dir().glob("wallpaper.*"))


def describe_current(container) -> str:
    """给模型看的一句「现在的外观」：没有壁纸时返回空串，不占提示词。

    container 是 main 模块（或任何带 CFG 的对象）；CFG 热更新会换绑，
    所以由调用方传进来现读，这里不缓存。
    只描述壁纸与强调色，不描述压暗/模糊——模型无从判断那些数值该不该提。
    """
    cfg = getattr(container, "CFG", None) or {}
    app = cfg.get("appearance") or {}
    name = str(app.get("wallpaper") or "")
    if not name:
        return ""
    pal = load_palette(app.get("palette"))
    brand = ""
    if pal:
        brand = str((pal.get("dark") or {}).get("brand") or "")
    text = f"界面当前使用了自定义壁纸（{name}）"
    if brand:
        text += f"，强调色 {brand}"
    return text + "；配色随壁纸主色自动生成，文字压在壁纸上时靠压暗层保证可读。"
