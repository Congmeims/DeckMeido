"""文件与终端接口：上传、浏览、预览、文件操作、终端（HTTP + PTY WebSocket）

从 main.py 平移出来的一整块。这些端点要回读 main 的运行态
（CFG / INSTALL_DIR / PERMISSION_MODE / CUSTOM_PERM 会被 apply_config 整体重新赋值），
所以统一用 get_main() 现读，本模块不缓存任何一份。

工作区相关的纯函数（路径判定、上传上限、文本抽取）也一并搬到这里，
main 侧保留同名引用，其余调用点不用改。
"""
from __future__ import annotations

import asyncio
import io
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

from fastapi import (APIRouter, Depends, HTTPException, Query, Request,
                     Response, UploadFile, WebSocket, WebSocketDisconnect)
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from . import approval, audit, devlog, fszones, paths, policy, tools
from .session_store import store

router = APIRouter()

require_auth = None
get_main = None

# 路由函数定义在 register 里，登记后由 main 取走，保持 main.api_xxx 的既有写法
_REGISTERED: dict = {}
__all_routes__ = (
    "api_upload", "api_upload_to", "api_upload_limit", "api_upload_stream",
    "api_files", "api_preview", "api_browse", "api_fs", "api_terminal",
    "api_terminal_stop", "api_terminal_run", "api_workspace_file",
    "api_file_stat_batch", "api_zip", "api_file_stat",
)


def init(require_auth_fn, get_main_fn) -> None:
    """由 main 在 app 创建后调用；鉴权闸门与运行态读取都注进来"""
    global require_auth, get_main
    require_auth = require_auth_fn
    get_main = get_main_fn
    # 鉴权在建 router 时挂一次：整组路由统一要令牌，
    # 不再逐条写 dependencies=[Depends(require_auth)]
    router.dependencies.append(Depends(require_auth))


def _m():
    return get_main()


# 按图片处理的扩展名：常见格式一律进 uploads 直接给模型看。
# avif/heif 在 Deck 的浏览器里常解不了，但 PIL 能转，所以也收；
# bmp/tiff/ico 同理。svg 不在此列（是文本/脚本，走工作区引用）
_IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp",
            ".avif", ".heic", ".heif", ".tif", ".tiff", ".ico"}
_TEXT_EXT = {".txt", ".md", ".py", ".js", ".ts", ".json", ".csv", ".log", ".sh",
             ".yml", ".yaml", ".toml", ".ini", ".cfg", ".xml", ".html", ".css",
             ".c", ".h", ".cpp", ".rs", ".go", ".java", ".sql", ".conf"}
# 单个附件上限，防止一次拖进几个 G 的文件把服务拖死
_UPLOAD_MAX = 20 * 1024 * 1024

# 文件管理器搬运文件不再写死 500MB：上限 = 目标磁盘剩余空间 - 安全余量。
# 余量默认 128MB；剩余不足 256MB 时改留一半，避免「还有 100MB 却完全不让传」。
UPLOAD_FREE_RESERVE = 128 * 1024 * 1024


def _fmt_bytes(n: int) -> str:
    """人类可读体积（1024 进制，与前端 fbFmtBytes 同一口径）"""
    value = float(max(0, int(n or 0)))
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(value)} B"
    return f"{value:.1f} {units[idx]}"


def upload_limits(path) -> dict:
    """目标目录所在磁盘的可用空间与单文件上限（唯一判定口径）。

    limit = free - reserve；reserve 是留给系统/其它进程的安全余量。
    拿不到磁盘信息（异常文件系统）时 free 记 0，宁可拒绝也不盲写。
    """
    # 同 upload_limits：测试会整体替换 main.upload_limits 这类入口，
    # 而 main 侧的同名薄壳把标记挂在「转发函数」本身上，所以这里只看转发函数
    m = _m()
    fn = getattr(m, "upload_limits", None)
    if fn is not None and getattr(fn, "_routes_files_delegate", False) is not True \
            and fn is not upload_limits:
        return fn(path)
    try:
        free = int(m.shutil.disk_usage(str(path)).free)
    except OSError:
        free = 0
    free = max(0, free)
    reserve = min(m.UPLOAD_FREE_RESERVE, free // 2)
    return {"free": free, "reserve": reserve, "limit": max(0, free - reserve)}


def _upload_limit_error(limit: int, size: int) -> dict:
    """413 的统一结构：文案里同时给出「当前上限」和「该文件大小」"""
    return {
        "ok": False,
        "detail": (f"单文件超过当前上限 {_fmt_bytes(limit)}，"
                   f"该文件 {_fmt_bytes(size)}"),
        "limit": int(limit),
        "size": int(size),
    }


def _upload_target_dir(dir: str) -> Path:
    """上传目标目录：展开 ~、转绝对路径、校验存在（三个上传接口共用）"""
    target = Path(dir).expanduser() if (dir or "").strip() else tools.WORKSPACE
    if not target.is_absolute():
        target = tools.WORKSPACE / target
    try:
        target = target.resolve()
    except OSError:
        raise HTTPException(status_code=400, detail="目标目录无效")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="目标目录不存在")
    return target


async def _stream_to_file(dest: Path, chunks, limit: int, declared: int) -> int:
    """边收边写盘，返回写入字节数；超上限抛 413、写入出错抛 500（都删掉半成品）

    chunks 是异步迭代器（multipart 的 file.read 或 request.stream 都包一层传进来），
    两块路径共用这一份逻辑：写之前先判上限，落盘量不会超过 limit；
    任何失败都要把写了一半的目标文件清掉，不能留个残缺文件占着原名。
    """
    written = 0
    too_large = 0
    try:
        with open(dest, "wb") as out:
            async for chunk in chunks:
                if not chunk:
                    continue
                written += len(chunk)
                if written > limit:
                    too_large = max(declared, written)
                    break
                out.write(chunk)
    except Exception as e:
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        raise HTTPException(status_code=500, detail=f"写入失败：{e}") from e
    if too_large:
        dest.unlink(missing_ok=True)
        info = _upload_limit_error(limit, too_large)
        raise HTTPException(status_code=413, detail=info["detail"])
    return written


async def _file_chunks(file, size: int = 1024 * 1024):
    """把 UploadFile 包成异步迭代器，交给 _stream_to_file 分块消费"""
    while True:
        chunk = await file.read(size)
        if not chunk:
            return
        yield chunk


async def _raw_chunks(request):
    """原始请求体按块吐出来（/api/upload-stream 用，不经过 multipart 临时文件）"""
    async for chunk in request.stream():
        if chunk:
            yield chunk


async def _extract_text(raw: bytes, ext: str):
    """尽量把文档抽成纯文本；抽不出来返回 None（按二进制附件处理）"""
    if ext in _TEXT_EXT:
        return raw.decode("utf-8", errors="replace")
    if ext == ".docx":
        # docx 就是个 zip，正文在 word/document.xml，去掉标签即可
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                xml = z.read("word/document.xml").decode("utf-8", errors="replace")
        except (KeyError, zipfile.BadZipFile, OSError):
            return None
        xml = xml.replace("</w:p>", "\n")
        return re.sub(r"<[^>]+>", "", xml).strip()
    if ext == ".pdf":
        # 有 pdftotext 就抽文字层，没有就算了。
        # 走 to_thread：大 PDF 解析可能要十几秒，阻塞事件循环会让所有 ws 心跳停摆
        try:
            r = await asyncio.to_thread(
                subprocess.run, ["pdftotext", "-q", "-", "-"], input=raw,
                capture_output=True, timeout=25,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if r.returncode != 0:
            return None
        return r.stdout.decode("utf-8", errors="replace") or None
    return None


# 文件浏览器预览用的分类。只影响面板里显示什么图标、用哪种方式预览，
# 和「能不能读」无关，所以放成白名单也无所谓
_KIND_MAP = {
    "image": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".avif", ".ico",
              ".heic", ".heif", ".tif", ".tiff"},
    "video": {".mp4", ".webm", ".mkv", ".mov", ".m4v", ".avi", ".ogv"},
    "audio": {".mp3", ".flac", ".wav", ".ogg", ".m4a", ".aac", ".opus", ".oga"},
    "pdf": {".pdf"},
    "text": {".txt", ".md", ".log", ".csv", ".json", ".toml", ".ini", ".cfg", ".yml",
             ".yaml", ".xml", ".html", ".css", ".js", ".ts", ".py", ".sh", ".c", ".h",
             ".cpp", ".rs", ".go", ".java", ".sql", ".conf", ".srt", ".desktop"},
}


def _file_kind(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    for kind, exts in _KIND_MAP.items():
        if ext in exts:
            return kind
    return "other"


def _fs_workspace_roots() -> list[str]:
    """分区判定的工作区：配置的工作区 + 安装目录（Deck 上是 /home/deck/DeckMeido）。"""
    m = _m()
    # 同 upload_limits：测试会整体替换 main._fs_workspace_roots，薄壳转发认得出来就跳过
    fn = getattr(m, "_fs_workspace_roots", None)
    if fn is not None and getattr(fn, "_routes_files_delegate", False) is not True \
            and fn is not _fs_workspace_roots:
        return fn()
    # 用 m.tools / m.INSTALL_DIR：测试会把 main.tools 换成指向临时工作区的替身
    mod = getattr(m, "tools", tools)
    roots: list[str] = []
    for root in (mod.WORKSPACE, m.INSTALL_DIR):
        if root:
            roots.append(str(root))
    return roots


_FS_DENY_MSG = {
    fszones.REASON_READ_ONLY_ZONE: "危险区域（系统目录）只读，不能执行写操作",
    fszones.REASON_READ_ONLY_MOUNT: "该挂载点当前是只读的（写保护），不能写入",
    fszones.REASON_FORBIDDEN_ZONE: "该目录禁止访问",
}


def _fs_denied_response(path) -> JSONResponse | None:
    """写操作统一拦截：危险区 / 只读挂载返回 403 + 机器可读原因。

    前端也按同一套 zone 置灰按钮，但这里是唯一事实来源——绕过前端直接
    打接口同样会被拦住。
    """
    roots = _fs_workspace_roots()
    info = fszones.zone_info(path, roots)
    reason = fszones.write_block(path, roots)
    if not reason:
        return None
    return JSONResponse(status_code=403, content={
        "ok": False,
        "reason": reason,
        "zone": info["zone"],
        "path": info["path"],
        "error": _FS_DENY_MSG.get(reason, "该位置不可写"),
    })


class _FsReq(BaseModel):
    op: str
    path: str
    path2: str = ""


def _workspace_file(rel: str) -> Path:
    """把「相对工作区的路径」解析成真实文件，越界或不存在一律 404

    模型给的链接是相对工作区的路径，这里只认这个根，防止 ../ 一路穿到工作区外面去。
    """
    root = tools.WORKSPACE.resolve()
    try:
        target = (root / rel).resolve()
    except OSError:
        raise HTTPException(status_code=404, detail="file not found")
    if root != target and root not in target.parents:
        raise HTTPException(status_code=403, detail="outside workspace")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return target


def _trash_dir(path: Path) -> Path | None:
    """该路径所在磁盘的回收站目录；不可用时返回 None。

    按 XDG 规范：通常盘用 ~/.local/share/Trash，其它挂载点用 <挂载点>/.Trash-$uid
    （必须确实存在且可写，否则文件管理器不会去那里找，移到那儿等于永久删除）。
    """
    uid = os.getuid() if hasattr(os, "getuid") else 1000
    home = Path.home()
    try:
        if path == home or home in path.parents:
            base = Path(os.environ.get("XDG_DATA_HOME")
                        or (home / ".local" / "share")) / "Trash"
        else:
            # 找 path 所属的挂载点：从自身往上走到 anchor，取第一个能读到 dev 的差异点
            anchor = Path(path.anchor or "/")
            mount = anchor
            cur = path
            try:
                dev = path.stat().st_dev
            except OSError:
                dev = None
            while cur != cur.parent:
                if dev is not None:
                    try:
                        if cur.stat().st_dev != dev:
                            break
                    except OSError:
                        break
                mount = cur
                cur = cur.parent
            base = mount / f".Trash-{uid}"
    except OSError:
        return None
    return base if base.is_dir() and os.access(base, os.W_OK) else None


def _trash_unique(dest_dir: Path, name: str) -> Path:
    """同名已存在时补 .1 / .2，别覆盖之前删的"""
    dest = dest_dir / name
    if not dest.exists() and not dest.is_symlink():
        return dest
    stem, suffix = os.path.splitext(name)
    for i in range(1, 1000):
        cand = dest_dir / f"{stem}.{i}{suffix}"
        if not cand.exists() and not cand.is_symlink():
            return cand
    raise OSError("回收站里同名文件过多")


def _trash_info_lines(src: Path) -> str:
    """回收站 info 文件的内容：原绝对路径 + 删除时间（UTF-8 做 URL 转义）"""
    return "Path=" + quote(str(src)) + "\nDeletionDate=" + \
        time.strftime("%Y-%m-%dT%H:%M:%S") + "\n"


def _move_to_trash(target: Path) -> Path:
    """把 target 移进回收站，返回它在 files/ 下的最终位置。

    同盘直接 rename；跨盘（rename 报 EXDEV）退化成长轮询复制再删源，
    期间出错就把半成品清掉，源文件保持不动。
    """
    base = _trash_dir(target)
    if base is None:
        raise OSError("该位置没有可用的回收站（~/.local/share/Trash 或 .Trash-<uid>）")
    files_dir = base / "files"
    info_dir = base / "info"
    files_dir.mkdir(parents=True, exist_ok=True)
    info_dir.mkdir(parents=True, exist_ok=True)
    dest = _trash_unique(files_dir, target.name)
    try:
        os.rename(target, dest)
    except OSError:
        # 跨设备：先拷到回收站再删源。复制到一半失败时清掉目标，源不受影响
        try:
            if target.is_dir() and not target.is_symlink():
                shutil.copytree(target, dest, symlinks=True)
            else:
                shutil.copy2(target, dest, follow_symlinks=False)
        except Exception:
            if dest.is_dir() and not dest.is_symlink():
                shutil.rmtree(dest, ignore_errors=True)
            else:
                try:
                    dest.unlink(missing_ok=True)
                except OSError:
                    pass
            raise
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
    # info 文件写失败不影响「已经进了回收站」，只是文件管理器里看不见来源
    try:
        (info_dir / (dest.name + ".trashinfo")).write_text(
            _trash_info_lines(target), encoding="utf-8")
    except OSError:
        pass
    return dest


def _zip_add_dir(zf: zipfile.ZipFile, root: Path, arc_root: str) -> None:
    """把整个目录按 arc_root/ 前缀递归压进去

    软链接不跟：指到目录外（甚至 /）的链接会把整盘拖进来，
    这里只压真实文件，链接本身直接跳过。打不开的文件跳过不中断。
    """
    for p in root.rglob("*"):
        if p.is_symlink() or not p.is_file():
            continue
        try:
            arc = f"{arc_root}/{p.relative_to(root).as_posix()}"
            zf.write(p, arcname=arc)
        except (OSError, ValueError):
            continue


def _zip_pack(targets: list[Path], out: Path) -> tuple[bool, int]:
    """在工作线程里把 targets 打进 out 文件，返回 (是否成功, 字节数)

    写临时文件而不是内存：sd 卡上的游戏、存档库动辄几个 G，
    全塞进内存会把后端打挂；落盘后由 FileResponse 流式发出去。
    """
    used: set[str] = set()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED,
                         allowZip64=True) as zf:
        for target in targets:
            if target.is_dir():
                # 单个目录：保留它自己的名字作为 zip 内的顶层目录
                arc = target.name
                n = 1
                while arc in used:
                    n += 1
                    arc = f"{target.name}_{n}"
                used.add(arc)
                _zip_add_dir(zf, target, arc)
                continue
            # 同名文件加序号，别让后面的覆盖前面的
            arc = target.name
            n = 1
            while arc in used:
                n += 1
                arc = f"{target.stem}_{n}{target.suffix}"
            used.add(arc)
            # 单选的软链接同样不跟：/api/zip?path=xxx 传进来的是 resolve 过的路径，
            # 但调用方也可能本身就是个链接，指向 /etc/shadow 之类的地方
            if target.is_symlink() or not target.is_file():
                continue
            try:
                zf.write(target, arcname=arc)
            except OSError:
                continue
    try:
        size = out.stat().st_size
    except OSError:
        return False, 0
    # 只有文件头（目录里一个可读文件都没有）也算没打成
    return (size > 22, size)


# 面板自己敲的命令：和白名单无关，但同样要过危险判定，
# 用户手动敲的也按当前权限模式审批，避免网页端变成绕过审批的后门
_term_lock: asyncio.Lock | None = None


def _term_lock_get() -> asyncio.Lock:
    """惰性建锁：模块导入时还没有事件循环，提前 new 出来会绑到错的循环上"""
    global _term_lock
    if _term_lock is None:
        _term_lock = asyncio.Lock()
    return _term_lock


# ---------- 路由 ----------


def register(app) -> None:
    """由 main 在 app 创建后调用：挂 HTTP 路由与终端 WebSocket"""
    @router.post("/api/upload")
    async def api_upload(file: UploadFile):
        """上传附件。

        图片 → userdata/uploads/，返回 /uploads/xxx.png，前端插成 ![图片](url) 走多模态；
        其他文件 → 工作区 uploads/，返回相对工作区的路径，前端插成 @{uploads/xxx}，
        由 _expand_context 带上正文（文本类直接读，docx/pdf 现抽文字）。
        """
        try:
            raw = await file.read()
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"读取上传内容失败：{e}") from e
        if len(raw) > _UPLOAD_MAX:
            raise HTTPException(status_code=413, detail="文件超过 20MB")
        fn = Path(file.filename or "file")
        ext = fn.suffix.lower()
        stem = re.sub(r"[^\w\-.]+", "_", fn.stem)[:40] or "file"
        ts = int(time.time())

        if ext in _IMG_EXT or not ext:
            ext = ext if ext in _IMG_EXT else ".png"
            up = paths.uploads_dir()
            up.mkdir(exist_ok=True)
            name = f"up_{ts}{ext}"
            (up / name).write_bytes(raw)
            return {"url": f"/uploads/{name}", "kind": "image", "name": fn.name}

        up = tools.WORKSPACE / "uploads"
        up.mkdir(parents=True, exist_ok=True)
        body = await _extract_text(raw, ext)
        if body is not None:
            # 有文字层就存成 .txt，@{} 展开时不会喂二进制给模型
            keep = up / f"{ts}_{stem}.txt"
            keep.write_text(f"# {fn.name}\n\n{body}", encoding="utf-8")
            kind = "text"
        else:
            keep = up / f"{ts}_{stem}{ext}"
            keep.write_bytes(raw)
            kind = "binary"
        rel = str(keep.relative_to(tools.WORKSPACE))
        return {"url": f"/uploads/{keep.name}", "path": rel, "kind": kind, "name": fn.name}

    @router.post("/api/upload-to")
    async def api_upload_to(file: UploadFile, dir: str = ""):
        """文件管理器里的拖放上传（multipart 版）。

        和 /api/upload 不同，这里不做文本抽取、不塞进对话上下文，就是纯搬运。
        上限按目标磁盘剩余空间动态判定（见 upload_limits），写入仍是分块流式的。
        网页端改用 /api/upload-stream（原始请求体直接落盘，不经过 multipart 临时文件）。
        """
        target_dir = _upload_target_dir(dir)
        denied = _fs_denied_response(target_dir)
        if denied:
            return denied
        fn = Path(file.filename or "file").name
        dest = target_dir / fn
        if dest.exists():
            raise HTTPException(status_code=409, detail=f"已存在同名文件：{fn}")
        caps = upload_limits(target_dir)
        limit = caps["limit"]
        declared = int(file.size or 0)
        if declared > 0 and declared > limit:
            return JSONResponse(status_code=413,
                                content=_upload_limit_error(limit, declared))
        written = await _stream_to_file(dest, _file_chunks(file), limit, declared)
        return {"ok": True, "name": fn, "dir": str(target_dir), "size": written}

    @router.get("/api/upload-limit")
    async def api_upload_limit(dir: str = ""):
        """上传前查询目标目录的单文件上限（前端据此给出「当前上限 X，该文件 Y」）。"""
        target_dir = _upload_target_dir(dir)
        denied = _fs_denied_response(target_dir)
        if denied:
            return denied
        return {"ok": True, "dir": str(target_dir), **upload_limits(target_dir)}

    @router.post("/api/upload-stream")
    async def api_upload_stream(request: Request, dir: str = "", name: str = "",
                                overwrite: bool = False):
        """原始流上传：请求体就是文件本身，边收边写盘。

        网页文件管理器走这条路径。multipart 解析会把整份内容先落到临时文件，
        大文件等于多占一份磁盘、也多了「先落整份拷贝」这一步；这里直接读
        request.stream()，1MB 一块写进目标文件，内存占用恒定。
        权限判定仍走 _fs_denied_response（危险区/只读挂载一律 403，不能绕过）。

        同名文件的默认行为仍是 409 拒绝；只有前端先问过用户、显式带上
        overwrite=1 时才覆盖（覆盖是把目标文件截断重写，不是先删再建）。
        """
        target_dir = _upload_target_dir(dir)
        denied = _fs_denied_response(target_dir)
        if denied:
            return denied
        fn = Path(name or "file").name
        if not fn or fn in (".", ".."):
            raise HTTPException(status_code=400, detail="文件名为空")
        dest = target_dir / fn
        if dest.exists() and not overwrite:
            raise HTTPException(status_code=409, detail=f"已存在同名文件：{fn}")
        if dest.is_dir():
            raise HTTPException(status_code=409, detail=f"同名目录已存在，无法覆盖：{fn}")
        caps = upload_limits(target_dir)
        limit = caps["limit"]
        try:
            declared = int(request.headers.get("x-file-size") or 0)
        except ValueError:
            declared = 0
        # 客户端声明的大小超限：先回 413，不必让对方把几个 G 传完再报错
        if declared > 0 and declared > limit:
            return JSONResponse(status_code=413,
                                content=_upload_limit_error(limit, declared))
        written = await _stream_to_file(dest, _raw_chunks(request), limit, declared)
        return {"ok": True, "name": fn, "dir": str(target_dir), "size": written}

    @router.get("/api/files")
    async def api_files():
        """列出工作区文件（@ 引用候选）"""
        out = []
        try:
            for p in sorted(tools.WORKSPACE.rglob("*")):
                if p.is_file() and not any(part.startswith(".") for part in p.parts):
                    out.append(str(p.relative_to(tools.WORKSPACE)))
                    if len(out) >= 300:
                        break
        except OSError:
            pass
        return {"workspace": str(tools.WORKSPACE), "files": out}

    @router.get("/api/preview")
    async def api_preview(path: str, raw: int = 0):
        """给文件浏览器预览用：吐任意路径的文件

        和 /files/{path} 不一样：那个锁死在工作区里，只给对话附件用；
        面板要能预览 AI 正在看的任何目录，所以这里认绝对路径。
        默认回 JSON（元信息 + 文本前 64KB，前端一次拿齐）；
        raw=1 时直接回文件本体，给 <img>/<video>/<audio>/<iframe> 用——
        视频/音频要能拖进度条就得支持 Range，所以走 FileResponse。
        """
        try:
            target = Path(path).expanduser().resolve()
        except OSError:
            raise HTTPException(status_code=404, detail="file not found")
        if fszones.browse_denied(target, _fs_workspace_roots()):
            # /proc、/sys、/dev 连读都不给：这类"文件"多数是设备/内核视图，
            # 预览它们既没意义也容易把进程拖住
            return JSONResponse(status_code=403, content={
                "ok": False, "zone": fszones.ZONE_FORBIDDEN,
                "reason": fszones.REASON_FORBIDDEN_ZONE,
                "path": str(target),
                "error": "该目录禁止浏览（/proc、/sys、/dev）",
            })
        if not target.is_file():
            raise HTTPException(status_code=404, detail="file not found")
        if raw:
            # FileResponse 自带 Range / ETag 处理，浏览器可以边下边播
            return FileResponse(target)
        kind = _file_kind(target.name)
        try:
            stat = target.stat()
        except OSError:
            raise HTTPException(status_code=404, detail="file not found")
        info = {"path": str(target), "name": target.name, "kind": kind,
                "size": stat.st_size, "mtime": stat.st_mtime}
        if kind == "text":
            try:
                info["text"] = target.read_bytes()[:65536].decode("utf-8", "replace")
            except OSError as e:
                info["error"] = f"读取失败：{e}"
        return info

    @router.get("/api/browse")
    async def api_browse(path: str = ""):
        """列单层目录内容（右侧文件浏览器用）

        与 /api/files 的区别：那个只吐工作区里的文件、一次全量；这里要能浏览
        任意目录（AI 可能在看 /home/deck/xxx），所以只列一层、按需展开。
        路径不存在或没权限时返回 error，前端显示成一行提示而不是整块报错。
        """
        raw = (path or "").strip()
        # 不传 path 就回到工作区，用户第一次打开面板看到的是自己的工作区
        target = Path(raw).expanduser() if raw else tools.WORKSPACE
        if not target.is_absolute():
            # 相对路径一律相对工作区解释，避免 cwd 变化导致行为漂移
            target = tools.WORKSPACE / target
        # AI 给的是文件路径时列它所在的目录并把它标出来，面板据此高亮；
        # 联动事件里不带 is_dir 也能靠这个兜住
        select = ""
        parent = None
        try:
            requested = target
            target = target.resolve()
            # 先算父级：无权限/不可读/不是目录时也要能靠「上一级」退回去，
            # 不能因为列表失败就把导航一起判死
            parent = str(target.parent) if str(target.parent) != str(target) else None
            # 断链：链接还在、目标没了。明确说清楚，别让用户点进去看到空白页
            if requested.is_symlink() and not requested.exists():
                z = fszones.zone_info(target, _fs_workspace_roots())
                link_parent = (str(requested.parent)
                               if str(requested.parent) != str(requested) else None)
                return {"path": str(requested), "parent": link_parent, "entries": [],
                        "link_broken": True, "link_target": str(target),
                        "zone": z["zone"], "readonly": z["readonly"],
                        "zone_label": z["label"], "zone_reason": z["reason"],
                        "error": f"链接目标不存在：{requested} -> {target}"}
            if target.is_file():
                select = target.name
                target = target.parent
                parent = str(target.parent) if str(target.parent) != str(target) else None
        except OSError:
            return {"path": raw, "parent": None, "entries": [], "error": "路径无效"}

        zone = fszones.zone_info(target, _fs_workspace_roots())
        zone_fields = {"zone": zone["zone"], "readonly": zone["readonly"],
                       "zone_label": zone["label"], "zone_reason": zone["reason"]}
        if zone["zone"] == fszones.ZONE_FORBIDDEN:
            # /proc、/sys、/dev 不是只读，是根本不给进；前端按 reason 显示提示
            return JSONResponse(status_code=403, content={
                "ok": False, "path": str(target), "parent": parent, "entries": [],
                "reason": fszones.REASON_FORBIDDEN_ZONE, **zone_fields,
                "error": f"{target} 属于禁止浏览的系统目录（/proc、/sys、/dev），已拒绝进入",
            })

        if not target.is_dir():
            # 工作区目录可能被用户/清理脚本删掉：打开文件管理器前先按工作区重建一次，
            # 仍失败就把具体原因说清楚，别只回一句「目录不存在或不是文件夹」
            m = _m()
            if not raw and str(target) == str(Path(tools.WORKSPACE).resolve()):
                if m.ensure_workspace(str(tools.WORKSPACE)):
                    target = Path(tools.WORKSPACE)
                else:
                    return {"path": str(target), "parent": parent, "entries": [],
                            **zone_fields,
                            "error": f"工作区目录 {target} 建不出来（{m.WORKSPACE_ERROR}）。"
                                     "请检查该路径是否有写权限，或在设置里换一个目录"}
            else:
                return {"path": str(target), "parent": parent, "entries": [], **zone_fields,
                        "error": f"目录 {target} 不存在"}
        entries = []
        try:
            with os.scandir(target) as it:
                for e in it:
                    # 软链接要跟随判断：/run/media/ 下的 SDCARD 是"指向 SD 卡的目录链接"，
                    # 用 follow_symlinks=False 会把它当成普通文件，点不进去
                    try:
                        is_link = e.is_symlink()
                    except OSError:
                        is_link = False
                    if is_link:
                        try:
                            is_dir = e.is_dir(follow_symlinks=True)
                        except OSError:
                            is_dir = False
                    else:
                        try:
                            is_dir = e.is_dir(follow_symlinks=False)
                        except OSError:
                            is_dir = False
                    item = {"name": e.name, "dir": is_dir}
                    if is_link:
                        item["link"] = True
                        try:
                            item["link_target"] = os.path.realpath(e.path)
                        except OSError:
                            item["link_target"] = ""
                        # 断链：exists 会跟随链接，False 说明目标没了。
                        # 注意 DirEntry 没有 exists() 方法（用了会 AttributeError → 整目录 500）
                        try:
                            item["broken"] = not os.path.exists(e.path)
                        except OSError:
                            item["broken"] = False
                    if not is_dir:
                        # kind 给前端挑图标和预览方式用（image/video/audio/pdf/text/other）
                        item["kind"] = _file_kind(e.name)
                    # 大小与修改时间：目录也带 mtime，条目一律显示「类型/大小/修改时间」
                    try:
                        st = e.stat(follow_symlinks=False)
                        item["mtime"] = st.st_mtime
                        if not is_dir:
                            item["size"] = st.st_size
                    except OSError:
                        pass
                    entries.append(item)
        except PermissionError:
            return {"path": str(target), "parent": parent, "entries": [], **zone_fields,
                    "error": "没有权限读取这个目录"}
        except OSError as e:
            return {"path": str(target), "parent": parent, "entries": [], **zone_fields,
                    "error": f"读取失败：{e}"}
        # 文件夹在前、同类按名字排，隐藏文件照样列出（用户可能就是要看 .config）
        entries.sort(key=lambda x: (not x["dir"], x["name"].lower()))
        parent = str(target.parent) if str(target.parent) != str(target) else None
        return {"path": str(target), "parent": parent, "entries": entries,
                "select": select, **zone_fields}

    @router.post("/api/fs")
    async def api_fs(req: _FsReq):
        """文件面板的右键操作：rename / delete / copy / move / mkdir。

        面板本来就能浏览任意目录（/api/browse、/api/preview 都认绝对路径），
        所以这里同样不做工作区限制——这些是用户在面板上手动点的操作，
        不是 AI 越权读文件。
        写操作按 fszones 的三级分区统一拦截：危险区只读，谨慎区可写（挂载 ro 时只读），
        返回 403 + reason（read_only_zone / read_only_mount / forbidden_zone）。
        """
        op = req.op
        try:
            p = Path(req.path).expanduser().resolve()
        except OSError:
            return {"ok": False, "error": "路径无效"}

        # 分区写保护：危险区和只读挂载一律 403。改名/移动/复制要连目标一起判，
        # 防止「从谨慎区搬到 /etc」或者经软链接父目录落到危险区。
        check_paths = []
        if op in ("delete", "mkdir", "rename", "move"):
            check_paths.append(p)
        p2 = None
        if op in ("rename", "move", "copy"):
            try:
                p2 = Path(req.path2).expanduser().resolve()
            except OSError:
                return {"ok": False, "error": "路径无效"}
            check_paths.append(p2)
        for cand in check_paths:
            denied = _fs_denied_response(cand)
            if denied:
                return denied

        # 根目录和家目录本身不给动，防止手滑把整个盘删了
        if op in ("delete", "rename", "move") and (p == Path(p.anchor) or p == Path.home()):
            return {"ok": False, "error": "不允许对根目录/家目录执行此操作"}

        try:
            # 这几个操作都是同步阻塞的，而 copytree / rmtree 碰上大目录能跑上几十秒。
            # 直接写在 async 函数里，整段时间事件循环是停的：流式输出断掉、心跳发不出去、
            # 其它接口全部排队。丢进线程池跑，循环照常转
            if op == "delete":
                # 走回收站而不是直接抹掉：终端/文件管理器删的东西要能在同一个
                # 回收站里找回来。回收站不可用时直接报错，不偷偷退化成永久删除
                await asyncio.to_thread(_move_to_trash, p)
            elif op == "mkdir":
                # path 是父目录，path2 是目录名
                name = (req.path2 or "").strip()
                if not name or "/" in name or name in (".", ".."):
                    return {"ok": False, "error": "目录名不合法"}
                (p / name).mkdir(parents=False, exist_ok=False)
            elif op in ("rename", "move", "copy"):
                if p2 is None:
                    return {"ok": False, "error": "目标路径无效"}
                if p2 == p:
                    return {"ok": False, "error": "源和目标相同"}
                if p2.exists():
                    return {"ok": False, "error": "目标已存在"}
                p2.parent.mkdir(parents=True, exist_ok=True)
                if op == "rename" or op == "move":
                    await asyncio.to_thread(shutil.move, str(p), str(p2))
                else:
                    if p.is_dir():
                        await asyncio.to_thread(shutil.copytree, p, p2)
                    else:
                        await asyncio.to_thread(shutil.copy2, p, p2)
            else:
                return {"ok": False, "error": "未知操作"}
        except FileNotFoundError:
            return {"ok": False, "error": "文件不存在"}
        except FileExistsError:
            return {"ok": False, "error": "目标已存在"}
        except PermissionError:
            return {"ok": False, "error": "没有权限"}
        except OSError as e:
            return {"ok": False, "error": f"操作失败：{e}"}
        return {"ok": True}

    @router.get("/api/terminal")
    async def api_terminal():
        """当前正在跑（或刚跑完）的那条命令，给右侧终端面板轮询

        只返回最近一条：历史在面板本地攒。没有命令时 state 为 null。
        """
        return {"state": tools.CURRENT_CMD}

    @router.post("/api/terminal/stop")
    async def api_terminal_stop():
        """终止面板里正在跑的那条命令（小窗上的终止按钮）

        先砍进程树（按登记表），再取消正在等它的对话轮次。不 cancel 的话，
        /api/terminal/run 那个请求会一直挂在里面等子进程，前端按钮永远转圈；
        但它可能是用户自己敲的，没有对话轮次可取消，所以这一步允许为空。
        """
        m = _m()
        killed = await tools.abort_all_procs()
        if m._chat_busy():
            m.current_task.cancel()
            m.set_overlay(False)
        return {"ok": True, "killed": killed}

    @router.post("/api/terminal/run")
    async def api_terminal_run(payload: dict):
        """面板里手动执行一条命令（终端 tab 的输入框）

        改造后：起一个真 PTY 会话（ptyprocess），不再走 script -qefc 模拟。
        命令的生命周期由前端 /ws/terminal WebSocket 接管——输出实时推流，
        按键实时写回。这里只负责「起命令 + 审批」，不阻塞等待输出。
        """
        cmd = str(payload.get("command") or "").strip()
        if not cmd:
            raise HTTPException(status_code=400, detail="命令为空")
        m = _m()
        # 和对话里同一个审批通道（approval.resolve）：手动模式一律先问，智能/自定义
        # 模式只拦危险命令，完全访问直接跑。面板不能变成绕过审批的后门，也不该自己
        # 另写一套规则 —— 规则只在 approval.py 里维护一份
        verdict = approval.resolve(
            "run_command", {"command": cmd},
            # full_access 必须和对话里的构造点同步：终端面板在完全访问档下同样
            # 享有删除类豁免，否则会出现「同一个命令在对话里放行、在面板里被问」
            policy.TurnContext(boot_id=m._system_boot_id(),
                               full_access=(m.PERMISSION_MODE == "full")),
            m.PERMISSION_MODE, m.CUSTOM_PERM)
        if verdict.deny:
            return {"ok": False, "denied": True, "reason": verdict.reason,
                    "output": f"已拒绝执行（{verdict.label}）：{verdict.reason}"}
        if verdict.ask:
            # 挂进和对话同一份 pending_action，之后由对话里那句「确认」放行
            args = {"command": cmd, "reason": "终端面板手动执行"}
            pending = {"name": "run_command", "args": args,
                       "description": tools.describe("run_command", args),
                       "hard": verdict.hard,
                       "sid": store.current_sid, "epoch": store.epoch,
                       "turn_id": store.turn.get("turn_id") or "",
                       "client": store.turn.get("client") or "",
                       "device": store.turn.get("device") or ""}
            m._pending_set(pending)
            return {"ok": False, "pending": True,
                    "description": pending["description"]}
        # 串行执行：面板同一时间只有一条 PTY 会话
        lock = _term_lock_get()
        if lock.locked():
            raise HTTPException(status_code=409, detail="上一条命令还在执行")
        async with lock:
            try:
                cols = int(payload.get("cols") or 120)
                rows = int(payload.get("rows") or 36)
                sess = await tools.start_pty_session(
                    cmd, cols=cols, rows=rows, reason="面板手动执行")
            except Exception as e:
                return {"ok": False, "output": f"执行失败：{e}"}
        # 返回会话 id（用 pid），前端拿它连 WebSocket
        return {"ok": True, "pid": sess.proc.pid,
                "state": {k: v for k, v in tools.CURRENT_CMD.items() if k != "_pty"}}

    @app.websocket("/ws/terminal")
    async def ws_terminal(ws: WebSocket):
        """终端专用 WebSocket：PTY ↔ xterm.js 双向字节流透传

        协议（JSON 帧）：
          前端 → 后端：
            {"type":"input","data":"<base64 字节>"}   按键写入 PTY stdin
            {"type":"resize","cols":N,"rows":N}        窗口尺寸变化
            {"type":"stop"}                            终止当前 PTY 会话
          后端 → 前端：
            {"type":"output","data":"<base64 字节>"}   PTY stdout 推流
            {"type":"exited","code":N}                 PTY 进程退出
            {"type":"error","msg":"..."}               出错

        用 base64 传字节：WebSocket 文本帧不能塞任意字节，二进制帧又不好混 JSON 头。
        """
        import base64

        m = _m()
        client_ip = ws.client.host if ws.client else ""
        if not m._ws_authorized(ws):
            audit.record("auth", "ws_terminal", "fail", ip=client_ip, reason="unpaired")
            await ws.accept()
            await ws.close(code=4401)
            return
        await ws.accept()
        sess = tools.get_active_pty()
        if sess is None:
            await ws.send_json({"type": "error", "msg": "没有活跃的终端会话"})
            await ws.close()
            return

        async def _push_output():
            """从 PTY 输出队列取字节，推给前端"""
            while sess.is_alive() or not sess.out_q.empty():
                chunk = await sess.out_q.get()
                if not chunk:
                    # 哨兵：PTY 已退出
                    break
                await ws.send_json({"type": "output",
                                    "data": base64.b64encode(chunk).decode("ascii")})
            # 推退出码
            await ws.send_json({"type": "exited", "code": sess.exit_code})

        push_task = asyncio.create_task(_push_output())
        try:
            while True:
                data = await ws.receive_json()
                t = data.get("type")
                if t == "input":
                    raw = base64.b64decode(data.get("data") or "")
                    if raw:
                        sess.write(raw)
                elif t == "resize":
                    cols = int(data.get("cols") or 120)
                    rows = int(data.get("rows") or 36)
                    sess.resize(cols, rows)
                elif t == "stop":
                    sess.stop()
                    break
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            devlog.debug("routes_files.ws_terminal", error=repr(exc))
            pass
        finally:
            push_task.cancel()
            try:
                await push_task
            except asyncio.CancelledError:
                pass

    @router.get("/files/{path:path}")
    async def api_workspace_file(path: str):
        """下载工作区里的文件（网页端附件卡片用）

        路径必须落在工作区内：模型给的链接是相对工作区的路径，这里只认这个根，
        防止 ../ 一路穿到工作区外面去。
        """
        return FileResponse(_workspace_file(path), filename=Path(path).name)

    @router.post("/api/file_stat_batch")
    async def api_file_stat_batch(payload: dict):
        """附件卡片补大小（批量）：{paths: [...]} → {sizes: {path: 字节数}}

        一条消息里可能有十几张卡片，逐张发请求在手机上就是十几次往返，
        这里一次问完；查不到的路径直接不出现在结果里，前端就不显示大小。
        """
        paths_ = payload.get("paths") or []
        if not isinstance(paths_, list):
            raise HTTPException(status_code=400, detail="paths must be a list")
        sizes: dict[str, int] = {}
        for rel in paths_[:200]:          # 条数封顶：这是一次 UI 补全，不是文件浏览器
            try:
                sizes[str(rel)] = _workspace_file(str(rel)).stat().st_size
            except (HTTPException, OSError):
                continue
        return {"sizes": sizes}

    @router.get("/api/zip")
    async def api_zip(path: list[str] | None = Query(default=None),
                      name: str = Query(default="files.zip")):
        """把若干文件/文件夹打成一个 zip 下载

        两种用法：
        - 相对工作区的路径（模型给的附件卡片）：只认工作区，越界跳过；
        - 绝对路径（文件面板里对任意位置点「打包下载」）：按分区放行，
          目录递归打包，只有 /proc、/sys、/dev 这类禁止浏览的区域会拒绝。

        先统计总量再打：目录递归可能拖进几个 G，全塞内存会把后端打挂。
        超过上限就返回 413 并说明实际大小，让用户自己进去挑。
        """
        path = path or []          # 不用 [] 作默认值：可变对象会被所有请求共享
        if not path:
            raise HTTPException(status_code=400, detail="no files")
        roots = _fs_workspace_roots()

        def resolve(raw: str) -> Path | None:
            """把请求里的路径落到真实文件；不允许的一律 None"""
            raw = str(raw or "").strip()
            if not raw:
                return None
            if os.path.isabs(raw):
                target = Path(os.path.normpath(raw))
                # 只拦「禁止浏览」的区域（/proc、/sys、/dev 等），和 /api/preview 同一套：
                # 危险区/谨慎区是「不可写」，读和下载仍然允许
                if fszones.browse_denied(target):
                    return None
                return target if target.exists() else None
            try:
                return _workspace_file(raw)
            except HTTPException:
                return None

        targets = [t for t in (resolve(p) for p in path[:200]) if t is not None]
        if not targets:
            raise HTTPException(status_code=404, detail="file not found")
        safe = re.sub(r"[^\w\-.]+", "_", Path(name).name)[:60] or "files.zip"
        if not safe.lower().endswith(".zip"):
            safe += ".zip"
        # 压缩是纯 CPU + 磁盘 IO，几个 G 的目录会卡住整个事件循环
        # （WebSocket 心跳、其它请求全停），所以整段丢到线程里跑。
        # 结果写临时文件再用 FileResponse 流式发：内存占用和目录大小无关，
        # 下载完由 BackgroundTask 删掉。不设总量上限——「把 sd 卡上的东西
        # 拷回电脑」正是这个功能的用法，只要求临时目录放得下。
        tmp_dir = paths.data_dir() / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkstemp(prefix="zip_", suffix=".zip", dir=tmp_dir)[1])
        try:
            ok, size = await asyncio.to_thread(_zip_pack, targets, tmp)
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail=f"打包失败：{exc}") from exc
        if not ok:
            tmp.unlink(missing_ok=True)
            raise HTTPException(status_code=404, detail="file not found")

        def _cleanup() -> None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

        return FileResponse(
            tmp, media_type="application/zip", filename=safe,
            background=BackgroundTask(_cleanup),
            headers={"Content-Length": str(size)},
        )

    @router.post("/api/file_stat")
    async def api_file_stat(payload: dict):
        """附件卡片补大小：{path: 相对工作区的路径} → {size, name}"""
        rel = str(payload.get("path", "")).strip()
        if not rel:
            raise HTTPException(status_code=404, detail="file not found")
        target = _workspace_file(rel)
        try:
            return {"size": target.stat().st_size, "name": target.name}
        except OSError:
            raise HTTPException(status_code=404, detail="file not found")

    app.include_router(router)

    _REGISTERED.update({name: locals()[name] for name in __all_routes__})
    globals().update(_REGISTERED)
