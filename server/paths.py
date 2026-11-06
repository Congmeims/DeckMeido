"""用户数据根解析（唯一入口，别在别的文件里另写一套）。

解析顺序：
  1. 环境变量 DECKMEIDO_DATA_DIR（显式指定，优先）
  2. <安装目录>/userdata/（install.sh 创建）

两者都不可用就明确报错并提示 DECKMEIDO_DATA_DIR，不再静默回退到别处。

目录约定：
  <data>/config.toml
  <data>/persona/meidosoul.json
  <data>/memory.json
  <data>/sessions/
  <data>/uploads/
  <data>/meidobot/{images,inbox_index.jsonl,text_log.txt}
"""
from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path

ENV_VAR = "DECKMEIDO_DATA_DIR"
USERDATA = "userdata"

# 探针失败再写日志时可能又走到 data_dir()/logs_dir()，用这个标记防递归
_probe_logging = False


def install_root() -> Path:
    """安装目录（server/ 的上一级）。测试/部署可用 DECKMEIDO_INSTALL_DIR 覆盖"""
    env = (os.environ.get("DECKMEIDO_INSTALL_DIR") or "").strip()
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parent.parent


def _probe_failed(path: Path, exc: OSError) -> None:
    """写探针失败时留一条（仅 dev_mode 落盘），不再静默吞掉"""
    global _probe_logging
    if _probe_logging:
        return
    _probe_logging = True
    try:
        from . import devlog

        devlog.log("paths.probe_failed", path=str(path),
                   error=f"{type(exc).__name__}: {exc}")
    except Exception:
        pass
    finally:
        _probe_logging = False


def _writable(path: Path) -> bool:
    """用一个唯一名的探针文件试写；并发下多个调用不会互相踩"""
    if not path.is_dir():
        return False
    probe = path / (
        f".deckmeido_write_test.{os.getpid()}."
        f"{threading.get_ident()}.{uuid.uuid4().hex}"
    )
    try:
        probe.write_text("", encoding="utf-8")
        return True
    except OSError as exc:
        _probe_failed(path, exc)
        return False
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError as exc:
            _probe_failed(path, exc)


def data_dir() -> Path:
    """当前生效的数据根；不可用时抛 RuntimeError，由启动日志直接告诉用户"""
    env = (os.environ.get(ENV_VAR) or "").strip()
    if env:
        path = Path(env).expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RuntimeError(
                f"数据根不可用：{ENV_VAR}={path} 无法创建（{exc}）；"
                f"请修正 {ENV_VAR} 或改为可写目录") from exc
        if _writable(path):
            return path
        raise RuntimeError(
            f"数据根不可写：{ENV_VAR}={path}；请给该目录写权限或改 {ENV_VAR}")
    primary = install_root() / USERDATA
    if _writable(primary):
        return primary
    raise RuntimeError(
        f"数据根不存在或不可写：{primary}；请先运行 install.sh 创建 userdata/，"
        f"或用环境变量 {ENV_VAR} 指定数据目录")


def data_root_label() -> str:
    return "env" if (os.environ.get(ENV_VAR) or "").strip() else "userdata"


# ---------- 各类文件位置 ----------

def config_path() -> Path:
    return data_dir() / "config.toml"


def persona_path() -> Path:
    return data_dir() / "persona" / "meidosoul.json"


def memory_path() -> Path:
    return data_dir() / "memory.json"


def sessions_dir() -> Path:
    path = data_dir() / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def uploads_dir() -> Path:
    path = data_dir() / "uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def voice_dir() -> Path:
    """QQ 语音条的本地副本：<data>/voice，供网页 / 客户端 / 插件回放"""
    path = data_dir() / "voice"
    path.mkdir(parents=True, exist_ok=True)
    return path


def meidobot_dir() -> Path:
    path = data_dir() / "meidobot"
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_dir() -> Path:
    """运行日志目录（backend.log / overlay.log）；权限 700"""
    path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def stt_model_dir() -> Path:
    """语音识别模型目录：<data>/models/sensevoice（首次下载时才创建）"""
    return data_dir() / "models" / "sensevoice"


def tts_model_dir() -> Path:
    """语音合成模型目录：<data>/models/tts/tsukuyomi（首次下载时才创建）"""
    return data_dir() / "models" / "tts" / "tsukuyomi"
