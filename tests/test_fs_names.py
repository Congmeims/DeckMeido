"""文件名不是 UTF-8（GBK 老文件）时，列表接口不能整层 500"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import routes_files  # noqa: E402


def test_fs_name_gbk():
    # 「恶魔城 - 晓月圆舞曲」的 GBK 字节，按 surrogateescape 还原后再转中文
    title = "恶魔城 - 晓月圆舞曲"
    name = title.encode("gbk").decode("utf-8", errors="surrogateescape")
    assert routes_files._fs_name(name) == title


def test_fs_name_utf8_untouched():
    assert routes_files._fs_name("恶魔城.gba") == "恶魔城.gba"


def test_fs_name_undecodable():
    name = b"\xff\xfe\x00bad".decode("utf-8", errors="surrogateescape")
    out = routes_files._fs_name(name)
    assert all(not ("\ud800" <= ch <= "\udfff") for ch in out)   # surrogate 不能丢给前端


@pytest.mark.skipif(os.name == "nt", reason="Windows 的 fs 编码不是 UTF-8，复现不出这个问题")
def test_scandir_bytes_path_works(tmp_path):
    raw_name = "恶魔城.gba".encode("gbk")
    with open(os.path.join(os.fsencode(tmp_path), raw_name), "wb") as f:
        f.write(b"x")
    names = [os.fsdecode(e.name) for e in os.scandir(os.fsencode(tmp_path))]
    assert routes_files._fs_name(names[0]) == "恶魔城.gba"
