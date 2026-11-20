"""文件名不是 UTF-8（GBK 老文件）时，文件管理器列表和预览都要认得出"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import routes_files  # noqa: E402

POSIX = pytest.mark.skipif(os.name == "nt", reason="Windows 的 fs 编码不是 UTF-8，复现不出这个问题")
# 真实踩到的那两个文件名：GBK 字节里含着「0xb3 0xc7 - 」这样的组合
GBK = "恶魔城 - 晓月圆舞曲.gba".encode("gbk")


def _surr(raw: bytes) -> str:
    return raw.decode("utf-8", errors="surrogateescape")


def _bytes(s: str) -> bytes:
    """测试里自己拼字节：不用 os.fsencode（Windows 上会把 surrogate 编成 UTF-8）"""
    out = bytearray()
    for ch in s:
        cp = ord(ch)
        if 0xdc80 <= cp <= 0xdcff:
            out.append(cp - 0xdc00)
        else:
            out += ch.encode("utf-8")
    return bytes(out)


def test_fs_name_gbk():
    assert routes_files._fs_name(_surr(GBK)) == "恶魔城 - 晓月圆舞曲.gba"


def test_fs_name_utf8_untouched():
    assert routes_files._fs_name("恶魔城.gba") == "恶魔城.gba"


def test_fs_name_undecodable():
    out = routes_files._fs_name(_surr(b"\xff\xfe\x00bad"))
    assert all(not ("\ud800" <= ch <= "\udfff") for ch in out)   # surrogate 不能丢给前端


def test_name_roundtrip_exact():
    # 显示名必须能一字不差地编回原字节，否则点进去就 404
    for raw in (GBK, b"\xd6\xd0\xce\xc4.gba", b"\xc7\xfa.gba", b"\xff\xfe\x00bad",
                b"\xb3\xc7-abc.gba"):
        assert routes_files._gbk_bytes(routes_files._fs_name(_surr(raw))) == raw


def test_wire_name_always_json_safe():
    # 响应体是 UTF-8 编码的：带 surrogate 直接 500，走线路格式后必须能编码
    for raw in (b"\xff\xfe\x00bad", GBK, b"ok.gba"):
        routes_files._wire_name(_surr(raw)).encode("utf-8")


def test_emit_keeps_undecodable_recoverable():
    out = routes_files._emit(_surr(b"a\xff\xfe.gz"))
    assert "\\udcff" in out and "\\udcfe" in out


def test_emit_plain_text_untouched():
    assert routes_files._emit("/home/deck/FreedeckGames/126") == "/home/deck/FreedeckGames/126"
    assert routes_files._emit("") == ""


def test_fs_path_roundtrip_browser():
    # 浏览器把 %B6 解成 U+00B6、%E5%A5%BD 解成「好」：前者按单字节还原，
    # 后者保持多字节原样，拼出来的才是磁盘上的原名
    p = "/home/deck/FreedeckGames/126/" + GBK.decode("latin-1")
    assert _bytes(routes_files._fs_path(p)) == b"/home/deck/FreedeckGames/126/" + GBK


def test_fs_path_roundtrip_shown_name():
    # 前端直接用列表里的中文显示名拼路径（中文目录 + GBK 文件名混排）
    shown = routes_files._wire_name(_surr(GBK))
    p = "/home/deck/游戏/" + shown
    assert _bytes(routes_files._fs_path(p)) == "/home/deck/游戏/".encode() + GBK


def test_fs_path_roundtrip_udc():
    raw = b"a\xff\xfe.gz"
    assert routes_files._fs_path(_surr(raw)) == _surr(raw)


def test_fs_path_leaves_utf8_and_ascii_alone():
    assert routes_files._fs_path("/home/deck/游戏/x.txt") == "/home/deck/游戏/x.txt"
    assert routes_files._fs_path("/home/deck/a b.txt") == "/home/deck/a b.txt"
    assert routes_files._fs_path("") == ""


@POSIX
def test_scan_and_reopen_real_file(tmp_path):
    # 端到端：GBK 名建文件 -> 转成线路格式 -> 前端还原后拼路径 -> 仍指向同一个文件
    d = os.fsencode(tmp_path)
    with open(os.path.join(d, GBK), "wb") as f:
        f.write(b"x")
    names = [os.fsdecode(e.name) for e in os.scandir(d)]
    wire = routes_files._wire_name(names[0])
    full = os.path.join(str(tmp_path), wire)
    assert os.path.isfile(os.fsencode(_bytes(routes_files._fs_path(full))))
