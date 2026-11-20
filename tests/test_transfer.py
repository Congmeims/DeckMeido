"""transfer.parse / 进度统计：只验认命令和量体积这两件容易出错的事"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import transfer  # noqa: E402


def test_parse_move_dir():
    got = transfer.parse("mv ~/Amanatsu ~/MeidoSpace")
    assert got and got["kind"] == "move"
    assert got["sources"] == ["~/Amanatsu"] and got["dest"] == "~/MeidoSpace"


def test_parse_rsync_is_copy():
    got = transfer.parse("rsync -a --info=progress2 ~/src ~/dst")
    assert got and got["kind"] == "copy" and got["program"] == "rsync"


def test_parse_cp_dir_only():
    assert transfer.parse("cp -r ~/src ~/dst")["kind"] == "copy"
    # 单个文件复制太快，不登记
    assert transfer.parse("cp ~/a.txt ~/b.txt") is None


def test_parse_skips_other_commands():
    assert transfer.parse("ls -la ~/") is None
    assert transfer.parse("echo mv a b") is None
    assert transfer.parse("grep -r foo /tmp") is None


def test_parse_ignores_remote_path():
    assert transfer.parse("rsync -a host:/data ~/local") is None


def test_parse_sudo_prefix():
    got = transfer.parse("sudo mv /a /b")
    assert got and got["kind"] == "move"


def test_parse_counts_copies_on_jump():
    got = transfer.parse("cd /tmp ; rsync -a src dst ; echo done")
    assert got and got["sources"] == ["src"]


def test_done_bytes_for_move(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "f").write_bytes(b"x" * 100)
    dst = tmp_path / "dst"
    task = {"kind": "move", "sources": [str(src)], "dest": str(dst),
            "total": 100, "cancel": False}
    assert transfer._done_bytes(task) == 0
    os.replace(src, dst)   # 同分区 mv 就是 rename
    assert transfer._done_bytes(task) == 100


def test_finish_marks_done(tmp_path):
    src = tmp_path / "a"
    src.mkdir()
    (src / "f").write_bytes(b"y" * 10)
    task = transfer.start(f"rsync -a {src.as_posix()} {(tmp_path / 'b').as_posix()}", 0)
    assert task and task["status"] == "running"
    transfer.finish(task["id"])
    snap = [t for t in transfer.list_tasks() if t["id"] == task["id"]][0]
    assert snap["status"] == "done"
    assert time.time() - snap["started"] < 5


def test_cancel_unknown():
    assert transfer.cancel("nope")["ok"] is False
