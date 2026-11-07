"""会话的进程内状态与落盘。

原来这些都在 main.py 里，读写的是 main 的模块级全局（current_sid / _turn /
_sess_epoch ...）。那种写法一旦想拆文件就会踩两个坑：`from main import current_sid`
只在导入那一刻取值（会话切走后仍然是旧值），而 `global current_sid` 在别的模块里
不生效（赋值只改自己模块的名字）。

所以这里把状态收进一个对象，谁用谁 `from .session_store import store`，
读写的永远是同一份。main.py 只负责在合适的时候改 store 的字段。
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import paths

BASE_DIR = Path(__file__).resolve().parent


def sessions_dir() -> Path:
    """会话目录：数据根下的 sessions/（惰性解析，导入本模块时不碰磁盘）"""
    return paths.sessions_dir()

# 所有会话 IO 都排在这一个线程里跑。
# 以前 read_text / write_text / os.replace 直接躺在事件循环里：Deck 的 eMMC
# 抖动一下就是几十上百毫秒，这期间 WebSocket 收发、/api/* 全部停摆，
# 表现就是「流式输出一顿」「心跳超时被判成掉线」。
# 单线程而不是线程池，是为了天然保住「同一时刻只有一次写」——
# 临时文件名相同、读-改-写的先后顺序，都靠这点串行来保证。
_IO = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sess-io")

# 会话文件的副档案后缀：主文件只有 {id,created,updated,title,history,compact}，
# 流式正文和文件指纹存在旁边的这个单独文件里。
# 为什么单独存：一轮回答要落几十上百次半截正文（每 0.6 秒一次），
# 以前每次都「把整份会话读出来 → 换掉 _streaming 字段 → 整份写回」，
# 200 轮以上的会话一轮下来就是几百次「读几 MB + 写几 MB」，Deck 的 eMMC
# 和那条单 IO 线程全被拖满，表现就是流式输出一顿一顿的。
# 拆出去以后流式落盘只写这一个几 KB 到几十 KB 的小文件，与主文件大小无关。
_SIDE_SUFFIX = ".side.json"


def _side_path(p: Path) -> Path:
    return p.with_name(p.stem + _SIDE_SUFFIX)


def all_session_files() -> list[Path]:
    """数据根里 sessions/ 下的会话文件，按 mtime 新到旧"""
    seen: dict[str, Path] = {}
    try:
        for p in sessions_dir().glob("*.json"):
            if p.name == "index.json" or p.name.endswith(_SIDE_SUFFIX):
                continue
            seen.setdefault(p.stem, p)
    except OSError:
        pass
    return sorted(seen.values(), key=lambda p: p.stat().st_mtime, reverse=True)


# 软删除保留份数：误删后还能从 userdata/trash/ 里找回最近这些会话。
# 超出的按 mtime 从旧到新清掉，避免回收站无限增长。
TRASH_KEEP = 20


def trash_session(sid: str) -> Path | None:
    """把会话主文件移进 userdata/trash/（软删除），返回落点。

    只在同一数据根内 os.replace，原子且不复制内容；会话文件不存在时返回 None。
    """
    src = sessions_dir() / f"{sid}.json"
    if not src.exists():
        return None
    target_dir = paths.trash_dir()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    target = target_dir / f"{sid}-{stamp}.json"
    n = 1
    while target.exists():
        target = target_dir / f"{sid}-{stamp}-{n}.json"
        n += 1
    os.replace(src, target)
    prune_trash(target_dir)
    return target


def prune_trash(target_dir: Path | None = None) -> list[str]:
    """只保留最近 TRASH_KEEP 份回收站会话，返回被清掉的文件名"""
    d = target_dir or paths.trash_dir()
    try:
        files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    removed: list[str] = []
    for old in files[TRASH_KEEP:]:
        try:
            old.unlink()
            removed.append(old.name)
        except OSError:
            pass
    return removed


def _atomic_write(p: Path, body: str) -> None:
    """原子写：先写临时文件再 replace，进程中途被杀也不会留下半个 JSON"""
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, p)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def io_submit(fn, *args):
    """把一次磁盘操作丢给 IO 线程，返回 Future（调用方不关心结果就忽略它）"""
    return _IO.submit(fn, *args)


class SessionStore:
    """当前会话的内存状态 + 会话文件的读写"""

    def __init__(self) -> None:
        self.current_sid = "default"
        self.history: list[dict] = []
        # 会话代次：新建 / 切换 / 删除会话时 +1。
        # 一轮对话开始时快照 epoch，中途被别的端切走会话也能认出来，
        # 从而把结果写回原会话、不污染新会话（否则会出现「新会话回了上一个问题」）
        self.epoch = 0
        # 当前这一轮对话的归属：sid/epoch + 发起端，供 save / compact / 确认操作判断
        self.turn: dict = {"sid": "", "epoch": 0, "active": False, "client": "", "device": ""}
        # 已被用户删除的会话号。删除动作只删文件，而「正在跑的那一轮」手里还捏着这个
        # sid：收尾时 save_session_snapshot(turn_sid) / 流式节流的 _write_streaming
        # 都会把文件原样写回来 —— 用户看到的就是「删了一个，冒出两个会话」。
        # 这里记一笔，所有落盘入口先查名单，删掉的会话绝不允许复活。
        # 只在启动时为空（重启后文件确实不存在了，也没人在跑老那一轮）。
        self.deleted_sids: set[str] = set()
        # 会话文件的全部落盘操作都在这一把锁里串行执行。
        # 流式回调（每 1 秒一次）、正式写入、_streaming 清理可能是并发的：
        #   1. 共用同一个 "<sid>.json.tmp" 临时文件 —— 两个线程同时写它，A 刚写完
        #      B 又覆盖，A 再 os.replace 就是把 B 的半截内容换上去，文件损坏；
        #   2. 更常见的是「读到旧数据再整体写回」：A 读到 54 条，B 在同一时刻写入
        #      55 条，A 随后把自己手里的 54 条写回去 —— 刚生成的回复凭空消失。
        #      _write_streaming 正是「读-改-写 _streaming 字段」，最容易踩第 2 条。
        # 加锁后这些读-改-写变成原子序列，最多是内容旧一点，绝不会互相抹掉。
        self.lock = threading.RLock()
        # 压缩游标 {sid: {"summary": str, "upto": int}}：
        # 只压缩「发给模型的上下文」，绝不删界面历史
        self.compact_map: dict[str, dict] = {}
        # 会话的创建时间：{sid: created}。以前 write_session 为了拿它在内存里
        # 把整份文件读回来（读→写），收尾时再读一遍再写一遍，一轮要过两遍盘。
        # 现在读会话文件时顺手记一笔，写的时候直接用。
        self.created_map: dict[str, float] = {}
        # 每个会话的显式挂起意图 {sid: pending_intent}。
        # 不能用「历史里有句话」当授权：历史里残留的重启请求会被模型当成
        # 当前指令；这里把「用户到底想干什么」单独存成有状态、会过期的记录。
        self.intent_map: dict[str, dict] = {}
        # 每个会话最近一次整份写入的指纹 {sid: (updated, len(history))}。
        # 目前只用于侧栏索引与排障，不再参与「有没有 _streaming 残留」的判断
        # （流式正文已经搬到副档案，清理就是删一个文件）
        self.wrote_map: dict[str, tuple[float, int]] = {}
        # 侧栏索引 {sid: {id,title,updated,messages}}，启动时由 load_index 填充
        self._index: dict[str, dict] = {}
        # 待落盘的索引定时器（防抖用，见 _index_schedule）
        self._index_timer: threading.Timer | None = None
        # 已经往 IO 队列里发过「整份写入」的会话（记录性信息，流式落盘不再依赖它）
        self.flushed_sids: set[str] = set()

    # ---------- 代次与轮次 ----------

    def bump_epoch(self) -> None:
        """会话被新建 / 切换 / 删除时调用：让在跑的老一轮认得自己被切走了"""
        self.epoch += 1

    def turn_detached(self, epoch: int) -> bool:
        """这轮开始时快照的 epoch 是否已过期（会话被切走或重建）"""
        return epoch != self.epoch

    def turn_tag(self) -> dict:
        """给广播消息挂上本轮所属会话号；前端据此丢弃不属于当前会话的流式片段。

        发起端自带的 turn_id 也一并带上：插件要认出「哪些 δ 是我这一轮的」，
        只看 sid 不行——同一会话里别的端提问也是这个 sid。
        """
        tag: dict = {}
        if self.turn.get("active") and self.turn.get("sid"):
            tag["sid"] = self.turn["sid"]
        if self.turn.get("turn_id"):
            tag["turn_id"] = self.turn["turn_id"]
        return tag

    # ---------- 压缩游标 ----------

    def compact_for(self, sid: str) -> dict:
        """取某个会话的压缩游标（没有就返回空游标）"""
        cur = self.compact_map.get(sid or "")
        if not isinstance(cur, dict):
            return {"summary": "", "upto": 0}
        return {"summary": cur.get("summary") or "",
                "upto": max(0, int(cur.get("upto") or 0))}

    def set_compact(self, sid: str, summary: str, upto: int) -> None:
        self.compact_map[sid or self.current_sid] = {
            "summary": summary or "", "upto": max(0, int(upto or 0))}

    def drop_compact(self, sid: str) -> None:
        self.compact_map.pop(sid, None)

    def forget_session(self, sid: str) -> None:
        """删除会话时把内存里所有按 sid 记录的引用一次清干净

        只删文件是不够的：compact_map 里的摘要、intent_map 里的挂起意图、
        written/created 指纹、流式缓冲都还攥着这个 sid。QQ 通道的 sid 是按
        uin/群号算出来的固定值，删掉之后同一个人的下一条消息又落回同一个 sid，
        旧摘要就会被当成「他刚才说过什么」重新注入 —— 表现就是删了还在复读。
        """
        if not sid:
            return
        with self.lock:
            self.deleted_sids.add(sid)
            self.compact_map.pop(sid, None)
            self.intent_map.pop(sid, None)
            self.created_map.pop(sid, None)
            self.wrote_map.pop(sid, None)
            self.flushed_sids.discard(sid)

    def allow_fresh(self, sid: str, active_sid: str = "") -> bool:
        """已删除的固定 sid 是否可以当「全新会话」再开

        QQ 的 sid 是固定算法，删掉之后不放开的话，这个人以后每句话都写不进
        文件（deleted_sids 会拦掉所有落盘）。这里只在「没有仍在跑的同 sid 轮次」
        时撤掉墓碑，保证旧内容绝不回来，但新对话能正常存。
        """
        if not sid:
            return False
        with self.lock:
            if sid not in self.deleted_sids:
                return False
            if active_sid and active_sid == sid:
                return False
            self.deleted_sids.discard(sid)
            return True

    # ---------- 挂起意图 ----------

    def get_intent(self, sid: str) -> dict | None:
        """取某个会话的 pending_intent（没有就 None）"""
        with self.lock:
            it = self.intent_map.get(sid or "")
            return dict(it) if isinstance(it, dict) else None

    def set_intent(self, sid: str, intent: dict | None) -> None:
        """更新 / 清空某个会话的 pending_intent"""
        with self.lock:
            if intent:
                self.intent_map[sid or self.current_sid] = dict(intent)
            else:
                self.intent_map.pop(sid or self.current_sid, None)

    # ---------- 路径 ----------

    def sess_path(self, sid: str) -> Path:
        """会话文件路径：永远是数据根里的 sessions/<sid>.json"""
        return sessions_dir() / f"{sid}.json"

    def fresh_sid(self) -> str:
        """生成一个目录里绝对不存在的会话号。

        直接 token_hex(4) 理论上够随机，但启动路径里出现过「老文件改名 .broken 后
        又用同一 sid 重建」的情况，新会话写回去正好覆盖掉还没救回来的历史。
        这里显式重试，撞号就换一个。
        """
        for _ in range(20):
            sid = secrets.token_hex(4)
            if not self.sess_path(sid).exists() and sid not in self.deleted_sids:
                return sid
        return secrets.token_hex(8)

    # ---------- 落盘 ----------

    def save_current(self, touch: bool = True) -> None:
        """把当前会话历史写盘，标题取第一条用户消息

        touch=False 用于「只是离开这个会话」的收尾写入：内容照存，但不刷新
        updated，侧栏排序因此不会因为点开别的会话而跳动。
        """
        self.write_session(self.current_sid, self.history, touch=touch)

    def save_snapshot(self, sid: str, hist: list[dict]) -> bool:
        """把指定快照写回指定会话文件（会话中途被切走时，用这招把结果送回原会话）"""
        self.write_session(sid or self.current_sid, hist)
        return True

    def write_session(self, sid: str, hist: list[dict], touch: bool = True) -> None:
        """原子写入一个会话文件。

        磁盘历史比本次要写的多时怎么办？答案是照写。以前这里做「缩水写合并」：
        用磁盘老历史做底、把内存里多出来的消息追加到末尾。它直接导致删不掉消息——
        用户删掉会话里几条，落盘时历史条数减半，触发合并，磁盘上被删的那几条
        又被拼了回来。合并还要求内存里至少有一条消息能在磁盘上找到锚点，
        删得多一点（比如清空重开）就直接判成「无法合并」并保留磁盘内容，
        于是界面上删干净了、重启后又全回来了。

        内存里的 history 才是唯一真相：用户删除、编辑、重排都发生在它上面，
        它写什么磁盘就存什么。真正需要防的只是「拿空历史覆盖掉有内容的文件」，
        那是启动时序上的竞态，不是合并能解决的问题——留着那道空写保护即可。

        IO 已移到单线程 _IO 里：内存状态（标题、创建时间、写入指纹）当场定下来，
        真正的读写排队执行。写盘失败只打日志，不再抛给调用方 —— 调用方在事件
        循环里，等不到这个异常，看到了也只能吞掉。
        """
        with self.lock:
            if sid in self.deleted_sids:
                # 会话已删除：迟到的落盘一律作废，否则等于把删掉的会话又变出来
                return
            # touch=False 的写入沿用索引里已有的 updated：查看会话不算真实活动，
            # 不能把它顶到列表最前面。索引里没有（第一次写）才退回当前时间
            prev = (self._index.get(sid) or {}).get("updated") or 0
            if not prev:
                prev = (self.wrote_map.get(sid) or (0, 0))[0]
            data = {"id": sid, "created": self.created_map.get(sid) or time.time(),
                    "updated": time.time() if touch else (prev or time.time()),
                    "history": hist}
            set_title(data, hist)
            # 压缩游标必须一起落盘：否则重启后模型重新吃到全量历史，
            # 白白撑爆上下文窗口，又要再压缩一遍
            comp = self.compact_for(sid)
            if comp["upto"] or comp["summary"]:
                data["compact"] = comp
            # pending_intent 随会话落盘：重启后按 boot_id 对账，
            # 老密码 / 老确认不会在几十轮之后又被当成有效授权
            intent = self.intent_map.get(sid)
            if isinstance(intent, dict) and intent:
                data["pending_intent"] = dict(intent)
            self.created_map[sid] = data["created"]
            # 指纹里带上这次写入的 updated（排障用：能看出最近一次整份写入是什么时候）
            self.wrote_map[sid] = (data["updated"], len(hist))
            created = data["created"]
        io_submit(self._write_file, sid, data, created)
        # 记一笔「这个会话已经发过整份写入」（排障用：能一眼看出磁盘上是否已有文件）
        with self.lock:
            self.flushed_sids.add(sid)

    def _write_file(self, sid: str, data: dict, created: float,
                    stamp: float | None = None) -> None:
        """真正的磁盘写入（在 IO 线程里跑）

        原子写：先写临时文件再 replace。直接 write_text 覆盖时如果进程被杀或
        连接中断，会留下半个 JSON，下次解析失败就等于整轮对话丢了。
        空写保护在这里就地检查：读到了磁盘上的旧条数，而这次什么都没写，
        说明是启动时序竞态，宁可跳过也不能把磁盘内容抹掉。
        """
        try:
            with self.lock:
                if sid in self.deleted_sids:
                    return
                p = self.sess_path(sid)
                if not data.get("history") and self._disk_count(sid, p) > 0:
                    print(f"[DeckMeido] 拒绝空写入：会话 {sid} 磁盘有 "
                          f"{self._disk_count(sid, p)} 条，内存为空，保持磁盘内容不变",
                          file=sys.stderr)
                    return
                body = json.dumps(data, ensure_ascii=False)
                # 临时文件名带 pid：万一还有别的进程（旧的孤儿后端）在写同一份会话，
                # 至少不会互相踩坏对方正在写的临时文件
                _atomic_write(p, body)
                self._index_put(sid, data)
                # dev_mode 埋点：落盘的思考字符数 / 文件 / 大小，与 reasoning.end、
                # ui.render 三条长度直接对比，谁短一眼能看出来
                try:
                    from . import devlog

                    if devlog.enabled():
                        n = sum(
                            len(str(ev.get("text") or ""))
                            for m in (data.get("history") or [])
                            for ev in (m.get("events") or [])
                            if isinstance(ev, dict) and ev.get("type") == "thought"
                        )
                        devlog.log("session.save", session=sid, file=str(p),
                                   size=p.stat().st_size,
                                   history=len(data.get("history") or []),
                                   reasoning_chars=n)
                except Exception:
                    pass
        except Exception as e:
            print(f"[DeckMeido] 会话 {sid} 落盘失败：{e}", file=sys.stderr)

    # ---------- 流式正文副档案 ----------
    #
    # 一轮回答的「屏幕上已出现过什么」每 0.6 秒落一次。这部分以前写在主文件的
    # _streaming 字段里，每次都要整份读、整份写 —— 开销和会话长度成正比，
    # 200 轮以后一轮回答就能把 IO 线程堵死。现在它单独存一份：
    #   {id, updated, _streaming:{text, ts, parts}}
    # 主文件只在真正收尾时写一次，两次之间的所有流式落盘都只碰这个小文件。

    def write_stream_side(self, sid: str, stamp: float, streaming: dict) -> None:
        """把半截正文写进副档案（在 IO 线程里跑）

        不读主文件：调用方已经把要存的内容算好了，这里纯粹是一次小文件覆盖写。
        """
        with self.lock:
            if sid in self.deleted_sids:
                return
            p = _side_path(self.sess_path(sid))
            _atomic_write(p, json.dumps({"id": sid, "updated": stamp,
                                         "_streaming": streaming}, ensure_ascii=False))

    def read_stream_side(self, sid: str) -> dict:
        """读副档案里的 _streaming（没有或损坏返回空 dict）

        启动恢复、收尾清理都要用它替代原来那个「整份读主文件再取字段」的动作。
        """
        p = _side_path(self.sess_path(sid))
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        stream = data.get("_streaming")
        return stream if isinstance(stream, dict) else {}

    def drop_stream_side(self, sid: str) -> None:
        """删掉副档案：正式内容已经落进主文件，这份半截正文没有意义了"""
        with self.lock:
            try:
                _side_path(self.sess_path(sid)).unlink()
            except OSError:
                pass

    def _disk_count(self, sid: str, p: Path) -> int:
        """磁盘上这个会话有几条历史（读失败按 0 算，让写入继续）"""
        try:
            return len(json.loads(p.read_text(encoding="utf-8")).get("history") or [])
        except Exception:
            return 0

    def _index_put(self, sid: str, data: dict) -> None:
        """更新侧栏索引（每个会话一行，正文不进索引）

        只在内存里改，并约一次延迟合并写：以前每写一次会话文件就同步重写一次
        index.json —— 一轮对话里「用户消息 + 收尾」至少两次，和会话写入叠在
        同一条 IO 线程上，纯属重复劳动。这里改成 2 秒内的多次改动合并成一次。
        """
        with self.lock:
            self._index[sid] = {"id": sid,
                                "title": data.get("title") or "会话",
                                "updated": data.get("updated") or time.time(),
                                "messages": len(data.get("history") or [])}
        self._index_schedule()

    def _index_schedule(self) -> None:
        """安排一次索引落盘（同一时刻最多挂一个，防抖 2 秒）"""
        with self.lock:
            if self._index_timer is not None:
                return
            t = threading.Timer(2.0, self._index_save)
            t.daemon = True
            self._index_timer = t
            t.start()

    def flush_index(self) -> None:
        """立即把索引落盘（进程退出前用；正常路径走延迟合并）"""
        with self.lock:
            if self._index_timer is not None:
                self._index_timer.cancel()
                self._index_timer = None
        self._index_save()

    def _index_save(self) -> None:
        """把当前索引落盘（线程安全；可在 IO 线程或定时器线程里跑）"""
        with self.lock:
            self._index_timer = None
            body = json.dumps(list(self._index.values()), ensure_ascii=False)
        try:
            _atomic_write(sessions_dir() / "index.json", body)
        except Exception:
            pass

    def read_session(self, sid: str) -> dict:
        """读一个会话文件，读不到/解析失败返回空 dict

        返回值里带 `_from_disk_mtime`（文件修改时间）：调用方据此判断
        「磁盘上的内容和内存里的是不是同一份」，避免多余的整份重写。
        """
        p = self.sess_path(sid)
        try:
            mtime = p.stat().st_mtime
        except OSError:
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        if data.get("created"):
            with self.lock:
                self.created_map.setdefault(sid, float(data["created"]))
        intent = data.get("pending_intent")
        with self.lock:
            if isinstance(intent, dict) and intent:
                self.intent_map[sid] = dict(intent)
            else:
                self.intent_map.pop(sid, None)
        data["_from_disk_mtime"] = mtime
        return data

    def rewrite_file(self, sid: str, data: dict) -> None:
        """把改过的会话 dict 写回（在 IO 线程里跑）"""
        self._write_file(sid, data, data.get("created") or time.time())

    def mark_written(self, sid: str) -> None:
        """把写入指纹的 updated 推到当前时刻

        用在「本次落拍之后旧数据可视为过期」的场合（如判断 _streaming 残留）：
        磁盘 mtime 的精度可能比这里记的时间粗，差几毫秒就会被误判成「文件更新了」，
        于是整份白读一遍。
        """
        with self.lock:
            cur = self.wrote_map.get(sid)
            if cur:
                self.wrote_map[sid] = (time.time(), cur[1])

    def index(self):
        """侧栏索引：{sid: {id,title,updated,messages}}"""
        return self._index

    def load_index(self) -> None:
        """启动时读一次 index.json；缺失就从会话文件补建（老版本没这个文件）"""
        path = sessions_dir() / "index.json"
        items: list[dict] = []
        try:
            items = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            items = []
        if isinstance(items, list) and items:
            self._index = {str(i.get("id")): i for i in items if i.get("id")}
            return
        # 没有索引（首次升级到这个版本）：扫一遍会话文件重建。
        # 只做一次，之后每次落盘都会顺带更新
        for p in all_session_files():
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            sid = d.get("id") or p.stem
            if d.get("created"):
                self.created_map.setdefault(sid, float(d["created"]))
            self._index[sid] = {"id": sid, "title": d.get("title") or "会话",
                                "updated": d.get("updated") or 0,
                                "messages": len(d.get("history") or [])}
        try:
            path.write_text(json.dumps(list(self._index.values()), ensure_ascii=False),
                            encoding="utf-8")
        except Exception:
            pass

    def index_drop(self, sid: str) -> None:
        """会话被删：从索引里摘掉并重写索引文件（删除是低频操作，同步写没问题）"""
        with self.lock:
            self._index.pop(sid, None)
            self.created_map.pop(sid, None)
            self.intent_map.pop(sid, None)
            self.wrote_map.pop(sid, None)
            self.flushed_sids.discard(sid)
        self._index_save()

    def index_put_mem(self, sid: str, title: str, updated: float, messages: int) -> None:
        """只更新内存里的索引（改标题这类小改动，落盘交给会话文件那次写入）"""
        with self.lock:
            self._index[sid] = {"id": sid, "title": title or "会话",
                                "updated": updated, "messages": messages}


store = SessionStore()


def session_order(items) -> list[dict]:
    """侧栏排序：updated 倒序；同一时间戳再按 id 倒序

    只加次级键就能保证刷新 / 强刷前后顺序固定，不会因为两轮写入落在
    同一秒而互相换位。查看会话不刷新 updated，所以点开不会把谁顶到首位。
    """
    return sorted(items, key=lambda d: (d.get("updated") or 0,
                                        str(d.get("id") or "")), reverse=True)


def attach_voice(hist: list[dict], voice: dict) -> float | None:
    """把这一轮语音的元数据挂到最后一条助手消息上

    元数据里只有 URL / 时长 / 发音人 / 字节数，音频文件单独放在 userdata/voice，
    不把 base64 塞进历史。没有助手消息（比如只发了语音没有文字）就返回 None，
    调用方据此决定要不要再写盘；老消息本来就没有 voice 字段，不受影响。
    """
    if not isinstance(hist, list) or not isinstance(voice, dict) or not voice:
        return None
    for m in reversed(hist):
        if isinstance(m, dict) and m.get("role") == "assistant":
            m["voice"] = dict(voice)
            return float(m.get("ts") or 0)
    return None


def qq_session_title(sid: str) -> str:
    """QQ 会话的固定标题：QQ + 触发方标识后 5 位

    群聊取群号（qq-g-<群号>），私聊取对方 QQ 号（qq-p-<QQ号>）。
    老数据 qq-g-<群号>-<QQ号> 同样只认群号：标题以会话标识为准，
    不参与任何判定，所以群内换了发言人也不会改名。
    不足 5 位按实际位数，不补零；取不到数字返回空串，由调用方回退。
    """
    parts = str(sid or "").split("-")
    if len(parts) < 3 or parts[0] != "qq" or parts[1] not in ("g", "p"):
        return ""
    digits = "".join(ch for ch in parts[2] if ch.isdigit())
    if not digits:
        return ""
    return "QQ" + digits[-5:]


def set_title(data: dict, hist: list[dict]) -> None:
    """给没有标题的空会话补一个标题，避免侧栏出现两个一模一样的「新会话」

    优先用 brief：截屏询问那轮真正的 content 是整段提示词（一百多字），
    拿它当标题，侧栏里就出现「看我卡在哪一步，说说现在这个画面里我该做什么。需…」，
    跟用户自己说的话毫无关系，还容易和别的同类会话撞成一样的名字

    QQ 会话例外：标题完全由会话标识（群号 / QQ 号）算出，与首条消息无关。
    write_session 每轮都会重建 data 再调这里，算出来的值始终相同，
    因此相当于只在首次创建时写了一次，后续消息不会把它改成别的名字。
    """
    fixed = qq_session_title(str(data.get("id") or ""))
    if fixed:
        data["title"] = fixed
        return
    if data.get("title") and data["title"] != "新会话":
        return
    first_user = next((m for m in hist if m.get("role") == "user"),
                      None)
    text = ""
    if first_user:
        text = str(first_user.get("brief") or first_user.get("content") or "").strip()
    if text:
        data["title"] = text.replace("\n", " ")[:24]
    else:
        data["title"] = "新会话 " + time.strftime("%H:%M", time.localtime(data.get("created") or time.time()))
