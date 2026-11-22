"""自定义主题：从壁纸取主色，推导深/浅两套配色变量。

只做纯计算与落盘，不依赖 main 的运行态，方便单独测试：

- `extract_palette(img_bytes)`：Pillow 打开 → 缩样 → 量化取主色 → 生成两套变量
- `save_wallpaper(img_bytes)`：校验格式 → 写 wallpapers/wallpaper.<ext> + 缩略图
- `clear_wallpaper()`：删掉壁纸文件与缩略图
- `apply_to_config(updates_fn, palette, filename, dim, blur)` 不在这里：
  配置写入沿用 main.update_config，路由和 AI 工具各自调。

配色规则：
- 十五色模式（默认）：壁纸量化成 15 个实际出现过的颜色，按亮度切成
  最暗 5 / 居中 5 / 最亮 5，分别给暗黑、深色、浅色三档；每档内部
  底 / 面板 / 第二层底 按明暗取三位，强调色和气泡色取段内最「跳」的两个
- 单色模式（一张图颜色太少时兜底）：主色相 + 饱和度推出三档，明度
  浅色 0.50 / 深色 0.44 / 暗黑 0.36，底色用同色相低饱和
- 用户手动改强调色：`retint_palette` 保留底色骨架，只换强调色/气泡色
"""
from __future__ import annotations

import colorsys
import io
import json
import re
import sys
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


def _grid_colors(img, cols: int = 8, rows: int = 8) -> list[tuple[float, float, float, int]]:
    """把图切成 cols×rows 网格，每格出两个样本：均值色、格内最饱和的像素色。

    返回 [(r, g, b, 权重)]，权重是该格像素数。
    只取均值会把格内的小面积亮色（招牌、眼睛高光、晚霞）跟周围背景平均掉，
    整张图越平均越灰 —— 15 个名额里就没有一个能当强调色。所以每格额外
    记一个饱和度最高的原始像素：底色靠均值（渐变/噪点被平滑掉），
    强调色靠这支最饱和的样本，两边的优点都留得住。
    """
    w, h = img.size
    if w <= 0 or h <= 0:
        return []
    px = img.tobytes()
    out = []
    for gy in range(rows):
        y0, y1 = gy * h // rows, (gy + 1) * h // rows
        if y1 <= y0:
            continue
        for gx in range(cols):
            x0, x1 = gx * w // cols, (gx + 1) * w // cols
            if x1 <= x0:
                continue
            sr = sg = sb = 0
            n = 0
            best = None          # (饱和度 + 明度权重, r, g, b)
            for y in range(y0, y1):
                base = y * w * 3
                for x in range(x0, x1):
                    i = base + x * 3
                    r, g, b = px[i], px[i + 1], px[i + 2]
                    sr += r
                    sg += g
                    sb += b
                    n += 1
                    if n % 3:            # 每 3 个像素评一次，够用且省一半开销
                        continue
                    mx, mn = max(r, g, b), min(r, g, b)
                    if not mx:
                        continue
                    sat = (mx - mn) / mx
                    score = sat * (0.3 + mx / 255)   # 太暗的高饱和像素也不算
                    if best is None or score > best[0]:
                        best = (score, r, g, b)
            if n:
                out.append((sr / n, sg / n, sb / n, n))
            # 格内最饱和样本只在真有颜色时才加：纯灰格里它还是灰，
            # 加进来只会多塞一堆重复的无彩色
            if best is not None and best[0] >= 0.18:
                # 最饱和样本按格子的三分之一权重参与统计，不压过成片的底色
                out.append((best[1], best[2], best[3], max(1, n // 3)))
    return out


def wallpaper_colors(img_bytes: bytes, n: int = 15) -> list[tuple[float, float, float, str]]:
    """把壁纸分成 15 色：最亮的 5 个 / 居中的 5 个 / 最暗的 5 个。

    一张图先按网格取样（每格出均值色和格内最饱和色两个样本），再统计成
    若干种实际出现过的颜色；进桶前把纯灰丢掉、把近黑近白降权，避免
    白墙/黑边/灰阴影占满名额。按亮度排序后切三段，三档主题各用其中一段，
    段内按亮度从小到大依次对应「底 / 面板 / 第二层 / 强调 / 气泡」五个槽位。
    返回 [(亮度, 占比, 饱和度, #rrggbb)]，按亮度从暗到亮。
    取不出图时返回空列表，调用方退回单色推导。
    """
    from PIL import Image
    try:
        raw = Image.open(io.BytesIO(img_bytes))
        raw.load()
        img = raw.convert("RGBA")
        # 透明底先垫白，否则 PNG 图标类壁纸的主色会是黑
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        bg.alpha_composite(img)
        img = bg.convert("RGB")
        # 先按 8×8 网格取每格均值：逐像素统计时，一段渐变会碎成十几个
        # 相邻桶、噪点和文字边缘也会各自占一桶，十几个名额被同一片色占满。
        # 取均值等于先做一次局部平滑，留下的才是「成片的颜色」。
        # 缩到 96×96 后自己按 8 级色桶做直方图，不用 Pillow 的 quantize：
        # 本机 Pillow 13 的 MEDIANCUT / ADAPTIVE 会把红绿通道算错
        # （源像素 (227,205,85) 量化成 #48931b 绿色），出来的配色整体偏绿。
        # 自己在 RGB 空间分桶没有这个问题，且能拿到每桶的真实占比。
        img = img.resize((96, 96))
    except Exception:
        return []
    samples = _grid_colors(img)
    if not samples:
        return []
    buckets: dict[tuple[int, int, int], list[int]] = {}
    drop_grey = 0.0          # 被丢掉的中灰度：纯灰撑不起强调色，占比也不该压过彩色
    for r, g, b, w in samples:
        _h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
        # 近黑 / 近白按四分之一权重留下：它们是三档主题的「底」唯一能来的方向
        # （浅色档要白底、暗黑档要黑底），但也不能让一片纯白/纯黑把整段占满
        weight = w if 0.03 <= l <= 0.93 else max(1, w // 4)
        # 中间的灰（明度 0.53~0.79 且饱和度 < 0.19）直接丢；两端的低饱和色
        # 保住 —— 黑白灰壁纸的底色本来就该是黑白灰
        if 0.53 <= l <= 0.79 and s < 0.19:
            drop_grey += weight
            continue
        key = (int(r) >> 5, int(g) >> 5, int(b) >> 5)   # 每通道 8 级，共 512 桶
        acc = buckets.get(key)
        if acc is None:
            buckets[key] = [r * weight, g * weight, b * weight, weight]
        else:
            acc[0] += r * weight
            acc[1] += g * weight
            acc[2] += b * weight
            acc[3] += weight
    if not buckets:
        return []
    # 占比的分母用未被丢掉的像素：一张图大半是灰墙时，剩下那点颜色
    # 在整图里本来就只占一小块，不能让灰把它的占比摊薄
    total = float(sum(a[3] for a in buckets.values()) + drop_grey) or 1.0
    entries = []
    for r, g, b, cnt in buckets.values():
        r, g, b = r / cnt, g / cnt, b / cnt
        h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
        # 占比不到 0.4% 的桶是噪点/抗锯齿边缘，丢掉
        if cnt / total < 0.004:
            continue
        entries.append([h, s, l, _luminance(_hex((r, g, b))), float(cnt)])
    if not entries:
        return []
    # 相邻色桶先聚成「一种颜色」：壁纸里大片天空/渐变会被分进好几个桶，
    # 不聚的话 15 个名额会被同一片深蓝占满，三档底色几乎一样。
    # 按亮度排序后，与上一个颜色足够接近（明度 6% + 色相差 12° 内）就并进去
    entries.sort(key=lambda x: x[3])
    clusters: list[list[float]] = []
    for e in entries:
        if clusters:
            last = clusters[-1]
            dh = abs(e[0] - last[0])
            if dh > 0.5:
                dh = 1 - dh
            # 跟「上一次并入的色」比而不是跟簇的原色比，避免渐变一路连成一片；
            # 这里用簇中心的亮度比，等价于与上次并入色比较
            if abs(e[3] - last[3]) < 0.05 and dh < 0.03:
                w = last[4] + e[4]
                last[0] = (last[0] * last[4] + e[0] * e[4]) / w
                last[1] = (last[1] * last[4] + e[1] * e[4]) / w
                last[2] = (last[2] * last[4] + e[2] * e[4]) / w
                last[4] = w
                last[3] = (last[3] * (w - e[4]) + e[3] * e[4]) / w
                continue
        clusters.append(list(e))
    # 聚完还超过 15 种时：最暗两个、最亮两个锁住（三档各自的「底」靠它们），
    # 中间按占比挑 —— 只按占比挑会把两端的极端色全丢掉，浅色档就取不到
    # 真正亮的颜色，底色会变成一片灰米色
    clusters.sort(key=lambda x: x[3])
    if len(clusters) > n:
        ends = clusters[:2] + clusters[-2:]
        take = sorted(clusters[2:-2], key=lambda x: x[4], reverse=True)[:n - 4]
        clusters = sorted(ends + take, key=lambda x: x[3])
    merged = clusters
    while len(merged) < n:
        # 颜色不足 15 个（纯色图、窄色阶截图）：在两色之间插值补齐。
        # 优先补占比够、颜色不重复的那一对，并把补出来的色号标成低占比，
        # 免得凑数的插值色被排到前面顶掉真实颜色
        best, bi = None, -1
        for i in range(len(merged) - 1):
            a, b = merged[i], merged[i + 1]
            if abs(a[2] - b[2]) < 0.004 and abs(a[0] - b[0]) < 0.01:
                continue
            score = b[3] - a[3]
            # 亮度差太小的一对不插值：插出来的色几乎和两端一样，白白占掉
            # 一个名额，还会把这一档的强调色名额挤成一片同色
            if score < 0.02:
                continue
            if best is None or score > best:
                best, bi = score, i
        if bi < 0:
            break
        a, b = merged[bi], merged[bi + 1]
        mid = [(a[k] + b[k]) / 2 for k in range(4)] + [min(a[4], b[4])]
        merged.insert(bi + 1, mid)
    return [(m[3], m[4] / total, m[1], _hsl_hex(m[0], m[1], m[2])) for m in merged]


def tier_palette(five: list[str], tier: str,
                 accent: list[str] | None = None) -> dict:
    """一整段 5 色 -> 一套主题变量（bg / surface / surface_2 / brand / user_bubble）

    tier 是这一档的颜色池，按亮度从暗到亮排好。底色三格固定按亮度取
    （浅色档取最亮三个，深色/暗黑档取最暗三个，见 `bg_pick`）；
    强调色和气泡色不再按亮度固定挑，而是取池内「存在感」最高的两个
    （见 `accent_pick`）—— 一张几乎全灰、只有眼睛一抹红的壁纸，
    红在亮度序里排不进前几格，以前三档的强调色就全是灰。

    accent 是整张图里最跳的颜色（跨三档共用）：本档池子里没有彩色时
    用它兜底，深色档的强调色配浅色底也在可读范围内。

    slots 就是用户能在设置页里逐格改的那 5 个颜色。
    """
    if len(five) < 5:
        return {}
    pool = accent or []
    if tier == "light":
        # 浅底：从底到气泡一条从亮到暗的阶梯，每格固定往下走 8~9 个百分点。
        # 以前强调/气泡两格直接拿原色，明度不受控，遇到壁纸里两种亮色
        # 挨得很近时，第 4、5 格几乎一模一样（实测差 0.011），整排看着
        # 不是阶梯而是三格渐变加两个同色块。
        step = [0.958, 0.868, 0.778]
        brand_l, bubble_l = 0.688, 0.598
    else:
        # 暗底：同理一条从暗到亮的阶梯，深色档比暗黑档整体略亮，两档才分得开
        if tier == "dark":
            step, brand_l, bubble_l = [0.060, 0.180, 0.300], 0.420, 0.540
        else:
            step, brand_l, bubble_l = [0.020, 0.078, 0.136], 0.194, 0.252
    brand_c, bubble_c = accent_pick(pool, five, tier)
    bg, surface, surface_2 = bg_pick(five, tier)
    out = [_tune(c, t) for c, t in zip([bg, surface, surface_2], step)]
    brand = _tune(brand_c, brand_l)
    bubble = _tune(bubble_c, bubble_l)
    return {
        "brand": brand,
        "user_bubble": bubble,
        "bg": out[0],
        "surface": out[1],
        "surface_2": out[2],
    }


def _hls(hex_color: str):
    m = re.fullmatch(r"#([0-9a-fA-F]{6})", hex_color or "")
    if not m:
        return 0.0, 0.0, 0.0
    v = int(m.group(1), 16)
    return colorsys.rgb_to_hls(((v >> 16) & 255) / 255,
                               ((v >> 8) & 255) / 255, (v & 255) / 255)


def bg_pick(five: list[tuple], tier: str) -> list[str]:
    """底色三格（底 / 面板 / 第二层）：按亮度取，浅色档取最亮三个

    five 是 [(亮度, 占比, 饱和度, 色值)]，已按亮度从暗到亮排好。
    底色只认亮度：浅色主题底下垫什么色，观感差别比「有没有彩色」大得多。
    """
    ordered = sorted(five, key=lambda r: r[0])
    seg = ordered[-3:] if tier == "light" else ordered[:3]
    cols = [r[3] for r in seg]
    return cols if tier == "light" else list(reversed(cols))


def accent_pick(pool: list[tuple], five: list[tuple], tier: str) -> tuple[str, str]:
    """强调色 / 气泡色：取池子里「存在感」最高的两个颜色

    候选只留饱和度够的彩色（黑白灰当强调色等于没有强调色），按
    「饱和度 × 占比^0.35」排序：一张图里只有眼睛两抹红时，红虽然面积小，
    但饱和度高，排在灰蓝前面；大面积的低饱和天空色则靠占比补回来。
    只有本档池子里一个彩色都没有（暗黑档经常如此）时才跨档兜底。

    强调色取了偏亮的彩色时，气泡色优先挑明度差得够开的那个：
    两格贴在壁纸上看差别更大，不然用户看到的是「强调和气泡一个颜色」。
    """
    def score(c) -> float:
        return c[2] * (max(c[1], 1e-6) ** 0.35)

    def colorful(rows):
        return [c for c in rows if c[2] >= 0.15]

    cand = colorful(five)
    # 暗黑档的池子本来就整段近黑，饱和度高也救不回来：直接用整图的候选
    if len(cand) < 2 and pool:
        cand = colorful(pool) or colorful(five) or list(five)
    cand = sorted(cand or list(five), key=score, reverse=True)
    # 强调色和气泡色得是两个颜色：色相岔开，或饱和度/明度明显不同都行；
    # 全都太像时（壁纸只有一抹同色相的红）就复用同一个色
    a = cand[0][3]
    h1, l1, s1 = _hls(a)
    b = a
    for c in cand[1:]:
        h2, l2, s2 = _hls(c[3])
        dh = abs(h1 - h2)
        dh = min(dh, 1 - dh)
        if dh >= 0.08 or abs(s2 - s1) >= 0.12 or abs(l2 - l1) >= 0.12:
            b = c[3]
            break
    return a, b


# 用户逐档挑的强调 / 气泡：{tier: {"brand": hex, "user_bubble": hex}}。
# 独立于 slots 记：slots 里第 3/4 格是同名的值，改底/面板时会被整份重写，
# 靠它回填才不会被顺手抹掉
ACCENT_KEYS = ("light", "dark", "oled")


def _tier_accent(pal: dict, tier: str) -> dict:
    acc = (pal.get("accents") or {}).get(tier)
    if isinstance(acc, dict) and acc.get("brand"):
        return {"brand": acc["brand"],
                "user_bubble": acc.get("user_bubble") or acc["brand"]}
    five = (pal.get("slots") or {}).get(tier) or []
    if len(five) >= 5:
        return {"brand": five[3], "user_bubble": five[4]}
    got = pal.get(tier) or {}
    return {"brand": got.get("brand") or "",
            "user_bubble": got.get("user_bubble") or got.get("brand") or ""}


def _sync_slots(out: dict) -> dict:
    """按 tier 的派生值把 slots 第 3/4 格、accents 对齐，保证两边说的是同一件事"""
    out["slots"] = {k: list(v) for k, v in (out.get("slots") or {}).items()}
    accents = {k: dict(v) for k, v in (out.get("accents") or {}).items()}
    for tier in ACCENT_KEYS:
        got = out.get(tier)
        if not isinstance(got, dict) or not got.get("brand"):
            continue
        accents[tier] = {"brand": got["brand"],
                         "user_bubble": got.get("user_bubble") or got["brand"]}
        five = out["slots"].get(tier) or []
        if len(five) >= 5:
            five[3] = got["brand"]
            five[4] = accents[tier]["user_bubble"]
            out["slots"][tier] = five
    if accents:
        out["accents"] = accents
    return out


def _tune(hex_color: str, target: float) -> str:
    """把颜色按 HSL 明度挪到 target，色相不动

    只调明度，所以出来的还是「这张图的颜色」—— 灰度图得到的仍是灰度，
    不会被凭空染上一层红/蓝。
    底色必须收敛饱和度：明度越接近白，饱和度上限压得越低（0.90 明度
    时只剩 ~30% 原饱和）。否则一眼好看的天空色压成 #f9e1c7 这种米黄底，
    整片界面上所有文字都像蒙了一层蜡。
    """
    m = re.fullmatch(r"#([0-9a-fA-F]{6})", hex_color or "")
    if not m:
        return hex_color
    v = int(m.group(1), 16)
    h, _l, s = colorsys.rgb_to_hls(((v >> 16) & 255) / 255,
                                   ((v >> 8) & 255) / 255, (v & 255) / 255)
    # 目标越亮，允许的饱和度越低：0.80 明度起开始收，到 0.95 只剩两成。
    # 暗底不压，低明度下高饱和也压得住
    if target > 0.80:
        s = s * max(0.0, min(1.0, (1.0 - target) / 0.15))
    return _hsl_hex(h, s, target)


def palette_from_wallpaper(img_bytes: bytes) -> dict | None:
    """壁纸 -> 十五色三档主题。分不出足够颜色时返回 None（调用方退回单色）

    每档各取一段 5 色：暗黑档用最暗的 5 个、深色档用居中的 5 个、
    浅色档用最亮的 5 个。段内按亮度从暗到亮排成五个槽位，三档共用
    同一套槽位定义（见 `tier_palette`）：

        0 底 / 1 面板 / 2 第二层底 / 3 强调 / 4 用户气泡

    这样 5 格全部有确定位置 —— 浅色档第 4 格是底，深色/暗黑档第 0 格
    是底，设置页把 3×5 的色板画出来，用户点哪格改哪格就行。
    """
    colors = wallpaper_colors(img_bytes)
    if len(colors) < 15:
        return None
    segs = {"oled": colors[:5], "dark": colors[5:10], "light": colors[10:]}
    # 强调色的候选来自整张图，不按亮度切段：壁纸里最跳的颜色（比如一张
    # 全灰图里的一点红）落在哪一段是碰运气，按段取就只剩灰
    pool = sorted(colors, key=lambda r: r[2] * (max(r[1], 1e-6) ** 0.35),
                  reverse=True)
    pal: dict = {"tiers": {}, "slots": {}, "accents": {}, "source": ""}
    for tier, seg in segs.items():
        five = [r[3] for r in sorted(seg, key=lambda r: r[0])]     # 从暗到亮
        got = tier_palette(seg, tier, pool)
        if not got:
            return None
        pal[tier] = got
        pal["tiers"][tier] = list(five)
        # 槽位：0=底 1=面板 2=第二层 3=强调 4=气泡
        pal["slots"][tier] = [got["bg"], got["surface"], got["surface_2"],
                              got["brand"], got["user_bubble"]]
        pal["accents"][tier] = {"brand": got["brand"],
                                "user_bubble": got["user_bubble"]}
    # source 用深色档的强调色：设置页「手动调整」是拿它比对的
    pal["source"] = pal["dark"]["brand"]
    return pal


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
    """壁纸字节 -> {dark, light, oled, tiers, slots, source}

    优先走「十五色」：壁纸里最亮 / 居中 / 最暗各 5 色分别给三档主题，
    底色、面板、第二层底、强调色、气泡色全部来自这张图本身。
    颜色种类太少（纯色图、灰度很小的截图）时退回单色推导，
    那时没有 tiers/slots，设置页只画得出一档四个色块。
    """
    pal = palette_from_wallpaper(img_bytes)
    if pal:
        return pal
    return palette_from_color(dominant_color(img_bytes))


def retint_palette(palette: dict, brand: str, tier: str | None = None) -> dict:
    """用户手动指定强调色：只改指定那一档，其余档保持壁纸原配色

    以前这里三档一起改 —— 用户想改浅色档的强调色，深色/暗黑档跟着变成
    同一个值，而三档底子明暗不同，同一个色在其中两档根本看不清。
    不传 tier 时改当前正在用的那档（没有就用深色），前端会把当时显示的
    档位一起发上来。

    底色三格不动，只换强调色与气泡色，`slots` 第 3/4 格和 `accents`
    一起对齐（见 `_sync_slots`）。tier 不在三档里时按深色档处理。

    `source` 保持壁纸取到的原强调色不动 —— 它是「恢复壁纸原配色」的
    唯一依据，早期版本写成了用户手动色，按一次就再也回不去了。
    壁纸原值另存在 `origin`（第一次手动改时快照整份 slots 留底）。
    """
    slots = (palette or {}).get("slots")
    if not isinstance(slots, dict) or not slots:
        return palette
    # 前端没带 tier 时按深色档处理（设置页「整体强调色」的约定），
    # 不能当成「三档一起改」：那样一次手动改色会把三档全刷成同一个色
    if tier not in ACCENT_KEYS:
        tier = "dark"
    out = {"slots": {}, "tiers": palette.get("tiers") or {},
           "source": palette.get("source") or "",
           "accents": {k: dict(v) for k, v in (palette.get("accents") or {}).items()},
           "origin": palette.get("origin") or {
               "slots": {k: list(v) for k, v in slots.items()},
               "tiers": palette.get("tiers") or {},
               "source": palette.get("source") or "",
               "accents": {k: dict(v) for k, v in (palette.get("accents") or {}).items()},
           }}
    for t in ACCENT_KEYS:
        got = dict(palette.get(t) or {})
        five = slots.get(t) or []
        if len(five) < 5:
            return palette
        if t == tier:
            got["brand"] = brand
            got["user_bubble"] = brand
        out[t] = got
        out["slots"][t] = list(five)
    out = _sync_slots(out)
    # 有点奇怪但必要：_sync_slots 会按每档的派生值回填 slots，
    # 上面没动的档要保持原样，所以再按原 slots 校正一遍没改的档
    for t in ACCENT_KEYS:
        if t != tier and slots.get(t):
            out["slots"][t] = list(slots[t])
            prev = palette.get("accents") or {}
            if prev.get(t):
                out["accents"][t] = dict(prev[t])
    return out


def restore_palette(palette: dict) -> dict | None:
    """「恢复壁纸原配色 / 恢复默认蓝」：把手动改过的配色整份还原

    - origin 里有壁纸原件的，原样还原（含逐格改过的底色）；
    - 没有 origin（从没手动改过，或者配色是旧版本留下的）但壁纸还在的，
      用壁纸按算法重新取一次色；
    - 两者都没有（没壁纸、配色来自旧版手动色推导）返回 None，
      调用方改回默认蓝。
    """
    origin = (palette or {}).get("origin")
    if isinstance(origin, dict) and origin.get("slots"):
        out = {k: v for k, v in palette.items() if k != "origin"}
        out["slots"] = {k: list(v) for k, v in origin["slots"].items()}
        out["tiers"] = origin.get("tiers") or {}
        out["source"] = origin.get("source") or ""
        if origin.get("accents"):
            out["accents"] = {k: dict(v) for k, v in origin["accents"].items()}
        else:
            out.pop("accents", None)
        for tier in ("light", "dark", "oled"):
            five = out["slots"].get(tier) or []
            if len(five) < 5:
                continue
            acc = _tier_accent(out, tier)
            out[tier] = {"brand": acc["brand"], "user_bubble": acc["user_bubble"],
                         "bg": five[0], "surface": five[1], "surface_2": five[2]}
        if out["slots"]:
            return out
    wall = _current_wallpaper_bytes()
    if wall:
        fresh = palette_from_wallpaper(wall)
        if fresh:
            return fresh
    return None


def _current_wallpaper_bytes() -> bytes | None:
    """当前壁纸的原始字节；没有壁纸或读不到时 None"""
    from . import paths
    try:
        found = wallpaper_files()
        if not found:
            return None
        return (paths.wallpapers_dir() / found[0]).read_bytes()
    except Exception:
        return None


def palette_from_five(palette: dict, tier: str, five: list[str],
                      only: int | None = None) -> dict:
    """用户手动改了某一档的色块：只重算这一档，其余档保持不变

    five 的槽位顺序与 slots 一致：底 / 面板 / 第二层 / 强调 / 气泡。
    传了 only 就只采纳那一格 —— 前端把整档 5 个值一起回传（底下的派生值
    可能已经过期），只认用户动过的那一格，其余以服务端现值为准。
    不传时五格原样落地（用户选的色就是最终色）。

    强调/气泡两格还要留一份在 accents 里：改底、面板时这两格不跟着变。
    """
    if not isinstance(palette, dict) or len(five) < 5:
        return palette
    out = dict(palette)
    out["tiers"] = dict(palette.get("tiers") or {})
    out["slots"] = {k: list(v) for k, v in (palette.get("slots") or {}).items()}
    # 逐格改色同样要留一份原件：第一次改之前快照整份 slots，
    # 「恢复壁纸原配色」才有东西还原
    if not out.get("origin"):
        out["origin"] = {
            "slots": {k: list(v) for k, v in out["slots"].items()},
            "tiers": dict(out["tiers"]),
            "source": palette.get("source") or "",
            "accents": {k: dict(v) for k, v in (palette.get("accents") or {}).items()},
        }
    cur = _tier_accent(palette, tier)
    five = list(five)
    # 强调/气泡以服务端现值为准：前端那份整档快照可能已被别处改过
    # （比如另一台设备刚改过强调色），回写时不能顺手覆盖
    five[3] = cur["brand"] or five[3]
    five[4] = cur["user_bubble"] or five[4]
    if only is not None and 0 <= only < 5:
        prev = ((palette.get("slots") or {}).get(tier) or [])
        for i in (0, 1, 2):
            if i != only and len(prev) > i:
                five[i] = prev[i]
    out[tier] = {
        "brand": five[3],
        "user_bubble": five[4],
        "bg": five[0],
        "surface": five[1],
        "surface_2": five[2],
    }
    out["slots"][tier] = list(five)
    out["tiers"][tier] = list(five)
    return _sync_slots(out)


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
    for mode in ("dark", "light", "oled"):
        if not isinstance(data.get(mode), dict):
            data[mode] = dict(fresh[mode])
            continue
        for k, v in fresh[mode].items():
            data[mode].setdefault(k, v)
        # 老配置没有 slots（十五色模式才写）：按这一档最终的五个色值补出来，
        # 设置页才画得出可以点的 5 格色板
        slots = data.setdefault("slots", {})
        if isinstance(slots, dict) and not slots.get(mode):
            m = data[mode]
            slots[mode] = [m.get("bg"), m.get("surface"), m.get("surface_2"),
                           m.get("brand"), m.get("user_bubble")]
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
# 会话里还引用着的候选图不按张数裁：聊天记录里那张预览要能点开，
# 一搜四张、连搜四五轮就把前面几轮的前三张删了，界面上就只剩
# 「已过期」的灰块（用户看到的就是「发了四张，三张过期」）。
# 这里单独给一个上限，只防止会话越攒越多时盘被撑爆
CANDIDATE_LIVE_MAX = 120


def candidates_dir():
    from . import paths
    d = paths.wallpapers_dir() / "candidates"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_candidate(img_bytes: bytes, trim: bool = True) -> tuple[str, str]:
    """把下载到的候选图落盘，返回 (id, 扩展名)。只校验不取色。

    取色留到真正应用时再做：用户挑不中的图没必要跑一遍量化。

    trim=False 留给「一次搜一批」的场景：调用方把整批都写完再统一裁一次。
    以前每存一张就裁一次，而这时会话还没把 id 记进历史，新存的图在
    orphan_candidate_ids 眼里全是孤儿（只有最后一张因为排在 CANDIDATE_KEEP
    以内被留住）——用户看到的就是「发了四张，三张已过期」。
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
    if trim:
        _trim_candidates()
    return cid, ext


def _trim_candidates() -> None:
    """裁掉没人再引用的旧候选图

    以前只按修改时间留最新 CANDIDATE_KEEP 张，发出去的那几批只要超过 16 张，
    前几批的前几张就会被删掉——聊天记录里的预览随即变成「已过期」。
    现在先把「会话 / 回收站 / 当前壁纸还在引用」的 id 挑出来：
    引用的最多留 CANDIDATE_LIVE_MAX 张（超出按时间删最老的，极端情况才会触发），
    没被引用的才按 CANDIDATE_KEEP 裁。引用判定复用 session_store，
    和删会话时的孤儿图回收是同一套口径
    """
    folder = candidates_dir()
    files = []
    for pat in ("*.png", "*.jpg", "*.webp", "*.gif", "*.bmp"):
        files.extend(folder.glob(pat))
    files.sort(key=lambda p: p.stat().st_mtime)

    live: set[str] = set()
    try:
        # 延迟导入：session_store 会反过来 import 本模块
        from . import session_store
        orphan = session_store.orphan_candidate_ids()
    except Exception as e:
        # 读不到会话就当全都还在用：宁可这次不裁，也不能把聊天里的图删成破图
        print(f"[DeckMeido] 候选图引用扫描失败，跳过裁剪：{e!r}", file=sys.stderr)
        return
    live = {p.stem for p in files if p.stem not in orphan}

    live_files = [p for p in files if p.stem in live]
    dead_files = [p for p in files if p.stem not in live]
    doomed = []
    if len(live_files) > CANDIDATE_LIVE_MAX:
        doomed += live_files[:-CANDIDATE_LIVE_MAX]
    if len(dead_files) > CANDIDATE_KEEP:
        doomed += dead_files[:-CANDIDATE_KEEP]
    for p in doomed:
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


def _cid_re() -> re.Pattern:
    return re.compile(r"[0-9a-f]{12}")


def candidates_in(obj) -> set[str]:
    """扫一份会话 / 回收站会话里引用到的候选图 id。

    引用点有三处：工具事件里的 candidates（每张一个 id）、落盘时挂在回答上的
    候选清单（编号 ↔ id，见 main.handle_chat 的 cand_tag）、以及正文里可能出现的
    id 文本。宁可多认几个也不能漏——漏一个就会把还在用的图删掉。
    """
    pat = _cid_re()
    found: set[str] = set()

    def walk(v) -> None:
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)
        elif isinstance(v, str):
            found.update(m.group(0) for m in pat.finditer(v))

    walk(obj)
    return found


def delete_candidates(cids) -> int:
    """删掉这些候选图，返回实际删掉的张数（文件已不在的不算）"""
    n = 0
    for cid in set(cids or ()):
        p = candidate_path(cid)
        if not p:
            continue
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    return n


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
