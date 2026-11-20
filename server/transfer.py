"""文件搬运任务：mv / cp / rsync 的进度登记。

以前模型跑 `mv ~/A ~/B` 这类搬家命令时，用户在界面上什么都看不到 ——
命令要跑好几分钟才有输出，期间只有终端面板一行「正在执行」。这里把
「从哪搬到哪、搬了多少、能不能停」记成任务，文件管理器面板轮询它，
画成带进度条和取消按钮的卡片（与上传卡片同一套样式）。

只在命令通道（tools._run_command）里登记，终端面板敲的命令不碰。
识别范围刻意收得很窄：只认 mv / cp / rsync 三条搬运类命令，其它命令
一律不登记，避免影响现有终端面板。

边界（回答用户时的口径也按这里）：
  - 用 `nohup ... &` 之类自己甩到后台的，拿不到我们的进程号，
    只能靠 du 估算已搬字节；
  - 同分区的 mv 是 rename，瞬间完成，根本没有中间态；
  - 目录里有硬链接时 du 会重复计，总量只能显示「约」。
"""
from __future__ import annotations

import os
import shlex
import threading
import time
from pathlib import Path

# 已完成的任务保留多久（秒）：给界面留时间把「100% / 已取消」画出来，
# 再久就会越堆越多，用户也不关心了
KEEP_DONE = 120
# 进度刷新间隔：du 要遍历目录，Deck 的 eMMC 上太勤会拖慢命令本身
POLL_MS = 1.5

_lock = threading.Lock()
_tasks: dict[str, dict] = {}
_seq = 0


def _fmt(n: int) -> str:
    value = float(max(0, int(n or 0)))
    units = ("B", "KB", "MB", "GB", "TB")
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024.0
        idx += 1
    return f"{int(value)} B" if idx == 0 else f"{value:.1f} {units[idx]}"


def _split_args(text: str) -> list[str]:
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _is_transfer_word(word: str) -> bool:
    base = os.path.basename(word)
    return base in ("mv", "cp", "rsync")


def _paths_of(args: list[str]) -> list[str]:
    """挑出命令里的路径参数：跳过 -flag，跳过 rsync 的 host:path（那是对端）"""
    out: list[str] = []
    after_dashdash = False
    for a in args:
        if not after_dashdash:
            if a == "--":
                after_dashdash = True
                continue
            # `--target-directory=DIR` 也要认
            if a.startswith("--"):
                if a.startswith("--target-directory="):
                    out.append(a.split("=", 1)[1])
                continue
            if a.startswith("-") and a != "-":
                continue
        if ":" in a and "/" not in a.split(":", 1)[0]:
            # user@host:/path —— 对端路径，本机 du 量不到，跳过。
            # 盘符（C:/...）里的冒号紧跟在单个字母后面，不是远端，放行
            head = a.split(":", 1)[0]
            if not (len(head) == 1 and head.isalpha()):
                continue
        out.append(a)
    return out


def parse(command: str) -> dict | None:
    """从一条命令里认出搬运动作，认不出返回 None。

    只处理简单形状：搬运命令是命令链里的一段，段内第一条就是 mv/cp/rsync。
    复合写法（for 循环里搬、管道接 xargs）一律不认 —— 猜错了画出来的卡片
    比没有更让人困惑。
    """
    for seg in command.replace("\n", ";").split(";"):
        words = _split_args(seg.strip())
        # 去掉 `sudo -u ru` 这类前缀，找到真正的程序名
        idx = 0
        while idx < len(words) and words[idx] in ("sudo", "nohup", "time", "nice", "env"):
            idx += 1
            while idx < len(words) and words[idx].startswith("-"):
                idx += 1
        if idx >= len(words):
            continue
        prog = os.path.basename(words[idx])
        if not _is_transfer_word(prog):
            continue
        args = words[idx + 1:]
        if prog == "cp" and not any(a in ("-r", "-R", "-a", "--recursive", "--archive")
                                    for a in args):
            # 只复制单个文件，快得很，不值得单独占一张卡
            continue
        paths = _paths_of(args)
        if len(paths) < 2:
            continue
        dst = paths[-1]
        srcs = paths[:-1]
        if prog == "cp" and any(a in ("-r", "-R", "-a", "--recursive", "--archive")
                                for a in args):
            kind = "copy"
        else:
            kind = "move" if prog == "mv" else "copy"
        return {"kind": kind, "sources": srcs, "dest": dst, "program": prog}
    return None


def _size_of(path: Path) -> int:
    try:
        if path.is_file():
            return path.stat().st_size
    except OSError:
        return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def _total_bytes(sources: list[str], dest: str, kind: str) -> int:
    """总量：源里还在的按实际大小算；移动时源已经没了（rename 已完成），
    改量目标 —— 这样同分区 mv 也不会显示成 0/0。"""
    total = 0
    for s in sources:
        p = Path(s).expanduser()
        total += _size_of(p)
    if total == 0 and kind == "move":
        total = _size_of(Path(dest).expanduser())
    return total


def _done_bytes(task: dict) -> int:
    """已搬字节。移动类命令搬完一个源就消失一个，用「总量 - 还在的源」
    最准；复制类命令源一直占着，只能量目标里这部分的现状。"""
    if task["cancel"]:
        return task.get("done", 0)
    if task["kind"] == "move":
        remain = sum(_size_of(Path(s).expanduser()) for s in task["sources"])
        return max(0, task["total"] - remain)
    dest = Path(task["dest"]).expanduser()
    if len(task["sources"]) == 1:
        src = Path(task["sources"][0]).expanduser()
        # 目标就是把源搬进去的那个名字（目标是已存在目录时，落在目录里）
        cand = dest / src.name if dest.is_dir() else dest
        if cand.exists():
            return _size_of(cand)
    # 多个源 / 猜不到落点：只能报「已开始」，别给假的百分比
    return 0


def _live(pid: int) -> bool:
    """命令进程是否还活着。没有 /proc 的系统退回 os.kill(pid, 0)"""
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _poller(task_id: str) -> None:
    while True:
        time.sleep(POLL_MS)
        with _lock:
            task = _tasks.get(task_id)
            if task is None or task["status"] != "running":
                return
            alive = _live(task["pid"])
            # 取消：等信号生效那一两秒，把最后一次量到的字节定住
            if task.get("cancel"):
                task["done"] = max(task.get("done", 0), _done_bytes(task))
                task["status"] = "cancelled"
                task["ended"] = time.time()
                return
        done = _done_bytes(task)
        with _lock:
            task = _tasks.get(task_id)
            if task is None or task["status"] != "running":
                return
            task["done"] = max(task.get("done", 0), done)
            if task["total"] > 0:
                task["percent"] = min(99.9, round(task["done"] / task["total"] * 1000) / 10)
            if not alive:
                task["seen_dead"] += 1
                # 进程没了也不急着收尾：mv 的 rename 之后 du 还要再量一次
                if task["seen_dead"] >= 2:
                    task["status"] = "done"
                    if task["total"] > 0:
                        task["percent"] = 100.0
                    task["ended"] = time.time()
                    return


def _prune() -> None:
    now = time.time()
    for tid in list(_tasks):
        t = _tasks[tid]
        if t["status"] != "running" and now - (t.get("ended") or 0) > KEEP_DONE:
            _tasks.pop(tid, None)


def start(command: str, pid: int, cwd: str = "") -> dict | None:
    """命令刚要执行时登记一条搬运任务（认不出搬运动作就不登记）"""
    info = parse(command)
    if not info:
        return None
    base = cwd or str(Path.home())
    srcs = []
    for s in info["sources"]:
        p = Path(s).expanduser()
        srcs.append(str(p if p.is_absolute() else Path(base) / p))
    d = Path(info["dest"]).expanduser()
    dest = str(d if d.is_absolute() else Path(base) / d)
    total = _total_bytes(srcs, dest, info["kind"])
    global _seq
    with _lock:
        _prune()
        _seq += 1
        tid = f"tr{_seq}"
        task = {
            "id": tid,
            "kind": info["kind"],
            "program": info["program"],
            "sources": srcs,
            "dest": dest,
            "total": total,
            "done": 0,
            "percent": 0.0,
            "status": "running",
            "cancel": False,
            "pid": pid,
            "seen_dead": 0,
            "started": time.time(),
            "ended": 0,
            "command": command,
        }
        _tasks[tid] = task
        snap = dict(task)
    threading.Thread(target=_poller, args=(tid,), daemon=True,
                     name=f"transfer-{tid}").start()
    return snap


def list_tasks() -> list[dict]:
    """给前端轮询用的快照（新任务在前）"""
    with _lock:
        _prune()
        return [dict(t) for t in _tasks.values()]


def cancel(tid: str) -> dict:
    """取消：本进程 fork 出来的任务直接杀进程组；外部甩到后台的只记一笔"""
    with _lock:
        task = _tasks.get(tid)
        if task is None:
            return {"ok": False, "detail": "任务不存在"}
        if task["status"] != "running":
            return {"ok": False, "detail": "任务已结束"}
        task["cancel"] = True
        task["cancel_at"] = time.time()
        pid = task["pid"]
    killed = False
    if pid > 0:
        try:
            os.killpg(pid, 15)
            killed = True
        except OSError:
            try:
                os.kill(pid, 15)
                killed = True
            except OSError:
                pass
    return {"ok": True, "killed": killed,
            "detail": "已发送终止信号" if killed else "外部后台任务，无法直接终止"}


def finish(tid: str) -> None:
    """命令退出时收尾：把最后一次量到的字节写进任务"""
    with _lock:
        task = _tasks.get(tid)
        if task is None or task["status"] != "running":
            return
    done = _done_bytes(task)
    with _lock:
        task = _tasks.get(tid)
        if task is None or task["status"] != "running":
            return
        task["done"] = max(task.get("done", 0), done)
        task["status"] = "cancelled" if task["cancel"] else "done"
        task["percent"] = 100.0 if not task["cancel"] and task["total"] > 0 else task["percent"]
        task["ended"] = time.time()


def describe(tid: str) -> str:
    """给模型看的一句话状态（用户问「搬到哪了」时能用上）"""
    with _lock:
        t = _tasks.get(tid)
        if t is None:
            return ""
        src = " ".join(t["sources"])
        word = "移动" if t["kind"] == "move" else "复制"
        return (f"{word} {src} → {t['dest']}：{_fmt(t['done'])} / {_fmt(t['total'])}"
                f"（{t['percent']:.0f}%，{t['status']}）")
