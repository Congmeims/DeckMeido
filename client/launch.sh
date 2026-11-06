#!/usr/bin/env bash
# 客户端守护：切游戏模式时 gamescope 会重建会话，掐断客户端的 Wayland 连接，
# Qt 于是在 C 层 abort()（日志里只有 "The Wayland connection broke"）。
# Python 拦不住这种死法，桌面快捷方式又不会自动重启，所以外面套一层壳：
# 正常退出（托盘「退出」/切出桌面会话）立即结束，异常退出才按退避重启。
#
# 用法：client/launch.sh        由桌面快捷方式、install.sh 与系统托盘调用
#      CLIENT_CRASH_RELAUNCH=0 关掉自动重启（排查问题时用）
#
# 托盘「重启」的约定：客户端以退出码 75 退出，表示「客户端与核心整体重来」。
# 这里不按崩溃处理（不退避、不等 Wayland），直接重新拉起主程序。
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
PY="$ROOT/.venv/bin/python"
MAIN="$HERE/main.py"

# 告诉客户端「外面有壳」：重启时只交退出码，不必再自己派生延迟启动器
export DECKMEIDO_CLIENT_WRAPPER=1

[ -x "$PY" ] || { echo "找不到 $PY，请重跑 install.sh" >&2; exit 1; }
[ -f "$MAIN" ] || { echo "找不到 $MAIN" >&2; exit 1; }

relaunch="${CLIENT_CRASH_RELAUNCH:-1}"
delay=2

while :; do
  "$PY" "$MAIN"
  rc=$?
  # 0 = 用户主动退出；130/143 = Ctrl-C / SIGTERM，都属于正常收摊
  if [ "$rc" = "0" ] || [ "$rc" = "130" ] || [ "$rc" = "143" ]; then
    exit "$rc"
  fi
  # 75 = 托盘「重启」：旧进程已完整退出（核心也停了），立刻起新实例。
  # 不走下面的崩溃退避，否则用户点一次重启要干等好几秒
  if [ "$rc" = "75" ]; then
    delay=2
    echo "[launch] 客户端请求重启，立即重新拉起" >&2
    continue
  fi
  [ "$relaunch" = "1" ] || exit "$rc"

  # 切模式时 Wayland 会消失几秒，等它回来再拉起，否则只会连环失败
  for _ in $(seq 1 30); do
    if [ -n "${WAYLAND_DISPLAY:-}" ] && [ -e "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/$WAYLAND_DISPLAY" ]; then
      break
    fi
    if [ -z "${WAYLAND_DISPLAY:-}" ] && [ -n "${DISPLAY:-}" ]; then
      break
    fi
    sleep 1
  done

  echo "[launch] 客户端异常退出（code $rc），${delay}s 后重启" >&2
  sleep "$delay"
  # 连续崩溃时翻倍退避，别把 CPU 占满，上限 30 秒
  delay=$(( delay * 2 > 30 ? 30 : delay * 2 ))
done
