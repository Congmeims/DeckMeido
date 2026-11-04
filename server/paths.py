"""用户数据根解析（唯一入口，别在别的文件里另写一套）。

解析顺序：
  1. 环境变量 DECKMEIDO_DATA_DIR（显式指定，优先）
  2. <安装目录>/userdata/（install.sh 创建；不存在或不可写就回退）
  3. ~/.local/share/deckmeido/（回退目录，日志里标 fallback）

目录约定（meidobot 是 QQ 归档的新名字，旧的 qq/ 只读兼容）：
  <data>/config.toml
  <data>/persona/meidosoul.json
  <data>/memory.json
  <data>/sessions/
  <data>/uploads/
  <data>/meidobot/{images,inbox_index.jsonl,text_log.txt}
  <data>/qq/                   旧布局，存在时只读回退

写一律写新位置；读旧位置时记录「已迁移读取」，旧文件不删除、不移动。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

ENV_VAR = "DECKMEIDO_DATA_DIR"
APP_DIR = "deckmeido"
USERDATA = "userdata"
FALLBACK_HOME = Path(".local") / "share" / APP_DIR

_logged: set[str] = set()


def _log_once(key: str, message: str) -> None:
    if key in _logged:
        return
    _logged.add(key)
    print(f"[DeckMeido] {message}", flush=True)


def install_root() -> Path:
    """安装目录（server/ 的上一级）。测试/部署可用 DECKMEIDO_INSTALL_DIR 覆盖"""
    env = (os.environ.get("DECKMEIDO_INSTALL_DIR") or "").strip()
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parent.parent


def _writable(path: Path) -> bool:
    """目录存在且真的能写；不存在不算可用（按约定回退）"""
    try:
        if not path.is_dir():
            return False
        probe = path / ".deckmeido_write_test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def fallback_dir() -> Path:
    path = Path.home() / FALLBACK_HOME
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


def data_dir() -> Path:
    """当前生效的数据根；每次调用重新解析，环境变量改了立即生效"""
    env = (os.environ.get(ENV_VAR) or "").strip()
    if env:
        path = Path(env).expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
            ok = _writable(path)
        except OSError:
            ok = False
        if ok:
            _log_once(f"env:{path}", f"数据根 {path}（{ENV_VAR}）")
            return path
        _log_once("fallback-env", f"数据根回退 fallback：{ENV_VAR} 不可写，改用 {fallback_dir()}")
        return fallback_dir()
    primary = install_root() / USERDATA
    if _writable(primary):
        _log_once(f"primary:{primary}", f"数据根 {primary}（userdata）")
        return primary
    path = fallback_dir()
    _log_once(f"fallback:{path}", f"数据根回退 fallback：{path}（userdata 不存在或不可写）")
    return path


def data_root_label() -> str:
    """env / userdata / fallback，排障和测试用"""
    env = (os.environ.get(ENV_VAR) or "").strip()
    if env:
        path = Path(env).expanduser()
        try:
            if path.is_dir() and _writable(path):
                return "env"
        except OSError:
            pass
        return "fallback"
    return "userdata" if _writable(install_root() / USERDATA) else "fallback"


# ---------- 各类文件位置 ----------

def config_path() -> Path:
    return data_dir() / "config.toml"


def legacy_config_path() -> Path:
    return install_root() / "server" / "config.toml"


def persona_path() -> Path:
    return data_dir() / "persona" / "meidosoul.json"


def legacy_persona_path() -> Path:
    return install_root() / "server" / "meidosoul.json"


def memory_path() -> Path:
    return data_dir() / "memory.json"


def legacy_memory_path() -> Path:
    return install_root() / "server" / "memory.json"


def sessions_dir() -> Path:
    path = data_dir() / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def legacy_sessions_dir() -> Path:
    return install_root() / "server" / "sessions"


def uploads_dir() -> Path:
    path = data_dir() / "uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def meidobot_dir() -> Path:
    """新归档目录；首次创建时把旧 qq/ 的索引与文本补过来（旧目录保留）"""
    path = data_dir() / "meidobot"
    existed = path.exists()
    path.mkdir(parents=True, exist_ok=True)
    legacy = data_dir() / "qq"
    if not existed and legacy.exists():
        _log_once(f"legacy-dir:{legacy}",
                  f"发现旧归档目录 legacy_dir=qq：{legacy}（只读兼容，新写入 {path}）")
        for name in ("inbox_index.jsonl", "text_log.txt"):
            old, new = legacy / name, path / name
            if old.is_file() and not new.exists():
                try:
                    shutil.copy2(old, new)
                except OSError:
                    pass
    return path


def meidobot_read_dir() -> Path:
    """读归档时用：新目录优先；不存在而旧 qq/ 存在时只读回退"""
    new = data_dir() / "meidobot"
    legacy = data_dir() / "qq"
    if new.exists():
        return new
    if legacy.exists():
        _log_once(f"legacy-read:{legacy}", f"读取旧归档 legacy_dir=qq：{legacy}（只读）")
        return legacy
    return new


def uploads_read_dir() -> Path:
    new = data_dir() / "uploads"
    legacy = install_root() / "server" / "uploads"
    if new.exists():
        return new
    if legacy.exists():
        _log_once(f"legacy-uploads:{legacy}", f"读取旧上传目录：{legacy}（只读）")
        return legacy
    return new


# ---------- 新旧布局读取 ----------

def read_path(new: Path, legacy: Path) -> Path:
    """新位置优先；旧位置存在时只读回退并记一次「已迁移读取」"""
    if new.exists():
        return new
    if legacy.exists():
        _log_once(f"read:{legacy}", f"已迁移读取：{legacy}（只读，后续写入 {new}）")
        return legacy
    return new


def ensure_migrated(new: Path, legacy: Path) -> None:
    """第一次写新位置前，把旧文件复制过去（旧文件保留，不删）"""
    if new.exists() or not legacy.exists():
        return
    try:
        new.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy, new)
        _log_once(f"write:{legacy}", f"已迁移读取：{legacy} → {new}（旧文件保留）")
    except OSError:
        pass
