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

# 系统 Python 大版本漂移（SteamOS 滚动更新 3.14 → 3.15）会让本地 venv 作废：
# .venv/bin/python3 是指向 /usr/bin/python3 的软链，系统升级后软链照样能执行，
# 但装好的 PySide6 等 C 扩展还留在 lib/python3.14/ 下，import 直接失败。
# 表现就是「点快捷方式什么都不出来」，或者每 30 秒一次崩溃重启、刷 coredump。
# 这种死法在没有终端的地方根本看不出原因，所以开跑前先认一次版本，给一条
# 能照着做的提示就退出 —— 不要进下面的崩溃重试循环。
venv_drift_check() {
  local cfg="$ROOT/.venv/pyvenv.cfg" want got
  [ -f "$cfg" ] || return 0
  want="$(sed -n 's/^version *= *//p' "$cfg" 2>/dev/null | head -1 | cut -d. -f1,2)"
  [ -n "$want" ] || return 0
  got="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || true)"
  [ -n "$got" ] || return 0
  # 只比「主.次」：补丁号升级不影响 ABI，不该让用户白重建一遍
  [ "$want" = "$got" ] && return 0
  echo "[launch] 虚拟环境是按 Python $want 建的，系统 Python 已升到 $got，装好的依赖全部失效" >&2
  echo "[launch] 请运行：bash $ROOT/install.sh upgrade" >&2
  if command -v notify-send >/dev/null 2>&1; then
    notify-send "DeckMeido 需要修复" "系统 Python 已从 $want 升到 $got，请运行 install.sh upgrade" >/dev/null 2>&1 || true
  fi
  return 1
}
venv_drift_check || exit 1

relaunch="${CLIENT_CRASH_RELAUNCH:-1}"
delay=2

# 显示环境是否真的可用。切模式后旧会话的 DISPLAY 可能还留在环境里，
# 只判断变量存在会在「有 DISPLAY 但连不上」时误判可用，随后 Qt 平台
# 初始化失败会 abort 并产生 coredump；这里对 X 也做一次真实探测。
#
# 环境里的 WAYLAND_DISPLAY 常是残留值：手动从 SSH 拉起时会被设成
# gamescope-0，切回桌面模式后这个 socket 已关闭，但 wayland-0 是好的。
# 所以先按优先级在 socket 目录里找现有的连接点，找到了就 export 回去。
pick_display() {
  local rt="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  local wd="${WAYLAND_DISPLAY:-}" c
  # gamescope-0 这个 socket 文件在切回桌面后仍然留在目录里（旧会话的残留），
  # 文件存在不代表能连上：实际连会 Connection refused。桌面模式的合成器
  # 是 kwin，socket 名字固定是 wayland-0，所以先认它。
  for c in wayland-0 wayland-1 "$wd" gamescope-0; do
    [ -n "$c" ] || continue
    [ -e "$rt/$c" ] && { echo "$c"; return 0; }
  done
  # 没有 Wayland 就退回 X：优先用 Xwayland 真在监听的那个
  if [ -e "/tmp/.X11-unix/X0" ]; then
    echo "DISPLAY=:0"
  elif [ -e "/tmp/.X11-unix/X1" ]; then
    echo "DISPLAY=:1"
  else
    return 1
  fi
}

display_ready() {
  if [ -n "${WAYLAND_DISPLAY:-}" ]; then
    [ -e "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/$WAYLAND_DISPLAY" ]
    return
  fi
  [ -n "${DISPLAY:-}" ] || return 1
  case "${DISPLAY}" in
    :*)
      local num="${DISPLAY#:}"
      num="${num%%.*}"
      [ -e "/tmp/.X11-unix/X$num" ] || return 1
      ;;
  esac
  # 有 socket 不代表 cookie 有效（切会话后常见 Invalid MIT-MAGIC-COOKIE）。
  # 有探测工具就实际查一次；都没有时退回只看 socket。
  if command -v xdpyinfo >/dev/null 2>&1; then
    xdpyinfo >/dev/null 2>&1
  elif command -v xset >/dev/null 2>&1; then
    xset q >/dev/null 2>&1
  else
    return 0
  fi
}

while :; do
  # 每轮起客户端前按现存 socket 校正显示环境：残留的 gamescope-0 会让
  # Qt 平台插件初始化失败 → abort → 每 30 秒一个 coredump
  pick="$(pick_display)" || {
    echo "[launch] 找不到可用的显示环境（没有任何 Wayland socket / X display），停止重试" >&2
    exit 1
  }
  case "$pick" in
    DISPLAY=*) export DISPLAY="${pick#DISPLAY=}"; unset WAYLAND_DISPLAY ;;
    *) export WAYLAND_DISPLAY="$pick" ;;
  esac
  [ -n "${XDG_RUNTIME_DIR:-}" ] || export XDG_RUNTIME_DIR="/run/user/$(id -u)"
  # 上一次的崩溃如果留下 Wayland 连接打不开的痕迹，这几行会写进 /tmp/deckmeido_client.log
  "$PY" "$MAIN"
  rc=$?
  if [ "$rc" = "134" ]; then
    echo "[launch] 平台插件初始化失败（rc 134）：本轮 WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-<空>} DISPLAY=${DISPLAY:-<空>}，socket 目录：" >&2
    ls "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}" 2>/dev/null | grep -E '^(wayland|gamescope)' >&2
  fi
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

  # 切模式时显示服务会消失几秒，等它回来再拉起；等不到就明确退出，
  # 不要无限重试——每次重试都会让 Qt 平台初始化失败并 abort，刷 coredump
  ready=0
  for _ in $(seq 1 30); do
    if pick_display >/dev/null; then
      ready=1
      break
    fi
    sleep 1
  done
  if [ "$ready" != "1" ]; then
    echo "[launch] 显示环境不可用（WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-<空>} DISPLAY=${DISPLAY:-<空>}），已等待 30 秒，停止重试" >&2
    exit 1
  fi

  echo "[launch] 客户端异常退出（code $rc），${delay}s 后重启" >&2
  sleep "$delay"
  # 连续崩溃时翻倍退避，别把 CPU 占满，上限 30 秒
  delay=$(( delay * 2 > 30 ? 30 : delay * 2 ))
done
