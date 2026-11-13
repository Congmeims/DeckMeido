#!/usr/bin/env bash
# DeckMeido 一键安装 / 升级 / 卸载
#   bash install.sh [install|upgrade|check|uninstall|from-mirror|speedtest|menu] [--deps|--no-deps|--gui]
#   无参数：未装则安装，已装则询问（升级/卸载/退出）
#   默认全装且不再询问档位；模块统一「检测→下载→安装→复查」，已装的自动跳过，
#   半路失败重跑同一命令即可补齐，无单独补装开关。
#
# 系统包清单见下方 DEPS_PACMAN，需 root，默认一起装，统一走 sudo。
# 依赖检测一律实测命令而非 `pacman -Q`：SteamOS 换镜像会重建 /usr，记录会脱节。
# ydotool 必须由 systemd 用户单元（ydotoold）托管，否则点击/输入失效。
#
# 一键（镜像优先，失败回落 GitHub Release）：
#   curl -sL https://meido.meagames.cn/deckmeido/install.sh | bash
#   本地源码：cd DeckMeido && bash install.sh（不必删目录重 clone）
#
# 环境变量：
#   DECKMEIDO_DIR / DECKMEIDO_WORKSPACE / DECKMEIDO_PORT / DECKMEIDO_MIRROR
#   DECKMEIDO_PIP_INDEX  留空则实测各源速率取最快
#   DECKMEIDO_SKIP_GUI=1 / DECKMEIDO_NO_DEPS=1 / DECKMEIDO_NO_SPEAKER=1  显式跳过对应项
#
# 禁止 sudo 执行：会装到 /root，桌面会话起不来。
# 用户数据统一在 <安装目录>/userdata（DECKMEIDO_DATA_DIR 可覆盖），升级排除、卸载保留。
set -euo pipefail

APP=deckmeido                 # 配置目录、数据目录沿用这个稳定标识（与安装目录大小写无关）
APP_DIR=DeckMeido             # 安装目录名；Linux 路径区分大小写
DECKMEIDO_DIR_ENV="${DECKMEIDO_DIR:-}"
INSTALL_DIR="${DECKMEIDO_DIR_ENV:-$HOME/$APP_DIR}"
PORT="${DECKMEIDO_PORT:-18765}"
# 国内镜像站（纯静态目录：latest.json / deckmeido-<ver>.tar.gz / SHA256SUMS / install.sh）
MIRROR="${DECKMEIDO_MIRROR:-https://meido.meagames.cn/deckmeido}"
MIRROR="${MIRROR%/}"
GITHUB_REPO="Congmeims/DeckMeido"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/$APP"
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/$APP_DIR"
DESKTOP_DIR="$HOME/.local/share/applications"
# 真正的「桌面」目录：可能是 Desktop 也可能是 桌面，取 xdg 配置，没配置就默认 Desktop
DESKTOP_FOLDER="$(command -v xdg-user-dir >/dev/null 2>&1 && xdg-user-dir DESKTOP 2>/dev/null || true)"
[ -n "$DESKTOP_FOLDER" ] && [ "$DESKTOP_FOLDER" != "$HOME" ] || DESKTOP_FOLDER="$HOME/Desktop"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

# 月读酱模型状态：unknown / ready / missing / failed / broken。
# stage_verify_all 写入，安装收尾汇总按它单列一行——避免「语音依赖已就绪」
# 被误读成「音色可用」，上次装完才发现模型根本没下载就栽在这。
TTS_MODEL_STATE="unknown"
# 语音识别模型状态：unknown / ready / failed（stage_verify_all 写入）
STT_MODEL_STATE="unknown"
# 系统控件在本次安装里没装好（DEPS_FAILED=1 时收尾汇总会提示重跑）
DEPS_FAILED=0
# 三次下载都失败时的最后一次原因，写进收尾汇总
TTS_MODEL_ERROR=""
# 通用下载重试：最近一次失败的次数与原因（由 _retry_download 写入）
DOWNLOAD_ATTEMPTS=0
DOWNLOAD_ERR=""
# 用户显式跳过的可选项（如 DECKMEIDO_SKIP_GUI）：收尾汇总必须列出来，
# 否则「没装」和「用户不要」分不清。
SKIPPED_ITEMS=()
# 系统控件装完后的实测复核结果（deps_verified 填充），只记录本次复核缺什么
DEPS_PROBE_FAILED=()
# Decky 重启后被判定卡在重启循环时的说明（decky_restart_loop_note 写入）。
# 必须单列进收尾汇总：否则「已重载」会让用户以为插件已生效，实际上一轮都没加载成功。
DECKY_LOOP_NOTE=""
# 动手前的 Decky 体检结果：ok / broken / unknown（decky_health 写入）
DECKY_HEALTH="unknown"
DECKY_HEALTH_NOTE=""
DECKY_UNIT=""
# 安全重载结果：ok / failed / pending（decky_safe_reload 写入）
DECKY_SAFE_STATE=""
DECKY_SAFE_NOTE=""
# 安装侧插件状态：ok / pending / failed，收尾卡片据此说明「到底生没生效」
PLUGIN_RELOAD_STATE=""
# 卸载侧三项关键状态：后端真停了吗 / 插件进程真没了吗 / Decky 还健康吗
UN_BACKEND_STATE=""
UN_PLUGIN_STATE=""
UN_DECKY_STATE=""

# 可执行位自愈：Windows/NTFS 打包或 git 导出常丢 0644，桌面项直接执行时会
# 「缺少运行权限」；用 bash 调用不受影响。补不动就算了。
heal_exec_bits() {
  local root="$1" f
  for f in install.sh uninstall.sh client/launch.sh plugin/deploy.sh deploy/DeckMeido.desktop; do
    [ -f "$root/$f" ] && chmod +x "$root/$f" 2>/dev/null || true
  done
}
heal_exec_bits "$HERE"

# 安装器标记与清单：只有带标记的目录才允许卸载；清单记录实际铺进安装目录的
# 条目，卸载只按清单删，清单外不动。路径由 INSTALL_DIR 推导，见 refresh_paths。
DATA_DIR_ENV="${DECKMEIDO_DATA_DIR:-}"
refresh_paths() {
  INSTALL_MARKER="$INSTALL_DIR/.deckmeido-install"
  INSTALL_MANIFEST="$INSTALL_DIR/.deckmeido-manifest"
}

# ---------- 用户数据根（userdata/） ----------
# 默认 $INSTALL_DIR/userdata，可用 DECKMEIDO_DATA_DIR 覆盖（客户端/插件拉起后端
# 时同读此变量）。卸载整目录保留，安装清单里不出现其下任何路径。
refresh_data_paths() {
  DATA_DIR="${DATA_DIR_ENV:-$INSTALL_DIR/userdata}"
  case "$DATA_DIR" in
    "~"/*) DATA_DIR="$HOME/${DATA_DIR#\~/}" ;;
    /*) ;;
    *) DATA_DIR="$PWD/$DATA_DIR" ;;
  esac
  DATA_CONFIG="$DATA_DIR/config.toml"
  DATA_PERSONA_DIR="$DATA_DIR/persona"
  DATA_PERSONA="$DATA_PERSONA_DIR/meidosoul.json"
  DATA_MEMORY="$DATA_DIR/memory.json"
  DATA_SESSIONS="$DATA_DIR/sessions"        # 会话目录固定叫 sessions（复数）
  DATA_UPLOADS="$DATA_DIR/uploads"
  DATA_BOT="$DATA_DIR/meidobot"             # meidobot（QQ）通道归档
  DATA_LOGS="$DATA_DIR/logs"                # backend.log / overlay.log / client-crash.log
  DATA_MODELS="$DATA_DIR/models"            # 语音识别模型（sensevoice/）
  # 安装器亲自装的系统包：卸载仅对这份清单提供可选卸载，不动自带包与用户原有包
  DEP_RECORD="$DATA_DIR/.deckmeido-deps"
  # 首装前机器快照（路径/包/ydotool.service 原状态）；放 userdata 以便升级不丢
  BASELINE_FILE="$DATA_DIR/.deckmeido-baseline"
  # 本次安装用的 pip 下载缓存：固定目录，装完自动清掉（不在用户 ~/.cache/pip 里乱删）
  PIP_CACHE_DIR="$DATA_DIR/.pip-cache"
}
refresh_paths
refresh_data_paths

# 端口：config.toml 优先 > DECKMEIDO_PORT > 18765。用户可能在设置页改过，
# 预检/卸载都要跟着配置走，不能用环境变量覆盖。
config_port() {
  local cfg="${1:-}"
  [ -f "$cfg" ] || return 1
  command -v python3 >/dev/null 2>&1 || return 1
  python3 - "$cfg" <<'PY' 2>/dev/null || return 1
import sys, tomllib
try:
    with open(sys.argv[1], "rb") as f:
        port = int((tomllib.load(f).get("server") or {}).get("port"))
except Exception:
    raise SystemExit(1)
if not 1024 <= port <= 65535:
    raise SystemExit(1)
print(port)
PY
}
_existing_port="$(config_port "$DATA_CONFIG" || true)"
if [ -n "$_existing_port" ]; then
  PORT="$_existing_port"
fi

# 是否通过「curl | sh -」管道执行。管道执行时脚本不在磁盘上，$BASH_SOURCE[0]
# 不是真实文件，sync_files 依赖的 $HERE/* 会指向错误位置；此时自动走
# 远程发布包安装（镜像优先、GitHub 兜底），让「一行命令来回用」成为可能。
_is_piped() {
  local src="${BASH_SOURCE[0]:-}"
  # 管道执行时 BASH_SOURCE[0] 通常是空 / "bash" / "sh" / "-"，不是真实文件
  [ -n "$src" ] && [ -f "$src" ] && return 1
  return 0
}

# ---------- 输出 ----------
# start_install_log 之后 stdout 接 tee，`[ -t 1 ]` 会失效；终端与否只认 OUT_IS_TTY，
# 否则开日志后颜色与进度条全丢。
OUT_IS_TTY=0
[ -t 1 ] && OUT_IS_TTY=1
if [ "$OUT_IS_TTY" = 1 ]; then
  C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_DIM=$'\033[2m'
  C_B=$'\033[1m'; C_C=$'\033[36m'; C_END=$'\033[0m'
else
  C_OK=""; C_WARN=""; C_ERR=""; C_DIM=""; C_B=""; C_C=""; C_END=""
fi
info() { printf '%s==>%s %s\n' "$C_OK" "$C_END" "$*"; }
warn() { printf '%s[!]%s %s\n' "$C_WARN" "$C_END" "$*"; }
die()  {
  printf '%s[x]%s %s\n' "$C_ERR" "$C_END" "$*" >&2
  # 失败时贴日志尾部；输出经 tee 转发，sleep 0.2 等它落盘
  if [ -n "${INSTALL_LOG:-}" ] && [ -s "$INSTALL_LOG" ]; then
    # tee 可能还没落盘
    sleep 0.2
    printf '%s    完整日志：%s%s\n' "$C_DIM" "$INSTALL_LOG" "$C_END" >&2
    printf '%s    最后 8 行：%s\n' "$C_DIM" "$C_END" >&2
    tail -n 8 "$INSTALL_LOG" >&2 || true
  fi
  exit 1
}
dim()  { printf '%s    %s%s\n' "$C_DIM" "$*" "$C_END"; }

# ---------- 安装步骤版式 ----------
# 原始输出只进 userdata/logs/install-<时间>.log，屏幕一步一行结果，失败由 die 贴尾部。
STEP_NO=0
STEP_TOTAL=8
reset_steps() { STEP_NO=0; }
step() {
  STEP_NO=$((STEP_NO + 1))
  printf '\n%s[%d/%d]%s %s%s%s\n' "$C_B" "$STEP_NO" "$STEP_TOTAL" "$C_END" "$C_B" "$1" "$C_END"
}
step_ok()   { printf '      %s✓%s %s\n' "$C_OK" "$C_END" "$*"; }
step_skip() { printf '      %s-%s 跳过：%s\n' "$C_DIM" "$C_END" "$*"; }
step_note() { printf '      %s%s%s\n' "$C_DIM" "$*" "$C_END"; }
step_fail() { printf '      %s✗%s %s\n' "$C_ERR" "$C_END" "$*"; }
next_hint() { printf '      %s下一步：%s\n' "$C_WARN" "$*"; }

# ---------- 模块版式：检测 → 下载 → 安装 → 复查 ----------
# 所有模块只允许走这一套版式，不得另写输出；这里只管排版，结论由模块给。
# 阶段名只用这六个：检测 / 下载 / 解包 / 安装 / 复查 / 配置。
# 解包单列是有原因的：模型下载完是解压，写成「安装 解包到 …」读起来像装了两遍。
mod_begin() { printf '      %s%s%s\n' "$C_B" "$1" "$C_END"; }
phase() {
  # $1 = 阶段，$2 = 内容
  printf '        %s· %s%s  %s\n' "$C_DIM" "$1" "$C_END" "$2"
}
phase_ok()   { printf '        %s· %s%s  %s✓%s %s\n' "$C_DIM" "$1" "$C_END" "$C_OK" "$C_END" "$2"; }
phase_skip() { printf '        %s· %s  跳过：%s%s\n' "$C_DIM" "$1" "$2" "$C_END"; }
phase_fail() { printf '        %s· %s%s  %s✗%s %s\n' "$C_DIM" "$1" "$C_END" "$C_ERR" "$C_END" "$2"; }

# 半路失败唯一话术：没有补装参数，重跑同一命令（幂等，已就绪模块自动跳过）。
retry_hint() {
  printf '      %s安装未完成。问题解决后重跑同一条命令即可，已装好的模块会自动跳过：%s\n' \
    "$C_WARN" "$C_END"
  printf '      %s  bash %s/install.sh install%s\n' "$C_WARN" "$INSTALL_DIR" "$C_END"
}

# 安装日志：后续 stdout/stderr 复制一份到 userdata/logs/install-<时间>.log，
# 用 tee 进程替换保证屏幕输出不变。
INSTALL_LOG=""
start_install_log() {
  [ -n "${DECKMEIDO_LOG_STARTED:-}" ] && return 0
  DECKMEIDO_LOG_STARTED=1
  local log_dir="$DATA_LOGS" fallback=0
  # 建不出来才退到家目录；日志不能因目录问题丢失
  if ! mkdir -p "$log_dir" 2>/dev/null; then
    fallback=1
    log_dir="$HOME"
    if ! mkdir -p "$log_dir" 2>/dev/null; then
      warn "无法创建日志目录（$DATA_LOGS 与 $HOME 都不可写），本次不写安装日志"
      return 0
    fi
  fi
  INSTALL_LOG="$log_dir/install-$(date -u +%Y%m%d-%H%M%S).log"
  if ! : > "$INSTALL_LOG" 2>/dev/null; then
    warn "无法写入日志文件：$INSTALL_LOG，本次不写安装日志"
    INSTALL_LOG=""
    return 0
  fi
  # 开头用自然语言写清楚这是什么、能不能删；用户以后翻到文件也知道能直接清掉
  {
    printf '%s\n' '这是 DeckMeido 的安装日志。'
    printf '%s\n' '它记录了安装的每一步做了什么；安装完成后可以随时删除，不影响程序运行。'
    printf '%s\n' "时间：$(date '+%Y-%m-%d %H:%M:%S')"
    printf '%s\n' "安装目录：$INSTALL_DIR"
    printf '\n'
  } >> "$INSTALL_LOG" 2>/dev/null || true
  exec > >(tee -a "$INSTALL_LOG") 2>&1
  if [ "$fallback" = 1 ]; then
    warn "无法在 $DATA_LOGS 创建日志目录，本次安装日志改写到 $HOME"
  fi
  dim "安装日志：$INSTALL_LOG"
}

need_cmd() { command -v "$1" >/dev/null 2>&1; }

# ---------- 删除守卫 ----------
# 2026-11-04 事故：卸载把源码工作区当安装目录，rm -rf 清空整个仓库（含 .git）。
# 所有删除前必须过 safe_rm_target：拒绝源码工作区（含 .git）、$HERE 及其上级、
# 空、/、$HOME、路径层级 <3。真机强删用 DECKMEIDO_ALLOW_UNSAFE_UNINSTALL=1。
safe_rm_target() {
  local target="${1:-}" abs depth
  if [ "${DECKMEIDO_ALLOW_UNSAFE_UNINSTALL:-0}" = "1" ]; then
    echo "[删除守卫] 已按 DECKMEIDO_ALLOW_UNSAFE_UNINSTALL=1 放行：$target"
    return 0
  fi
  if [ -z "$target" ]; then
    echo "[删除守卫] 拒绝：删除目标是空字符串" >&2
    return 1
  fi
  if [ "$target" = "/" ]; then
    echo "[删除守卫] 拒绝：删除目标是 /" >&2
    return 1
  fi
  target="${target%/}"
  case "$target" in
    "~"*) target="$HOME/${target#\~/}" ;;
  esac
  # 解析成绝对路径；父目录不存在时保留原样（真实删除会失败，不会误删）
  if abs="$(cd -P "$(dirname "$target")" 2>/dev/null && pwd)/$(basename "$target")"; then
    :
  else
    abs="$target"
  fi
  abs="${abs%/}"
  case "$abs" in
    ""|"/"|"$HOME"|"$HOME/")
      echo "[删除守卫] 拒绝：目标是空、/ 或家目录（$abs）" >&2
      return 1
      ;;
  esac
  depth="$(printf '%s' "$abs" | awk -F/ '{print NF-1}')"
  if [ "${depth:-0}" -lt 3 ]; then
    echo "[删除守卫] 拒绝：路径层级过浅（$abs）" >&2
    return 1
  fi
  if [ "${DECKMEIDO_INSTALL_DIR_UNINSTALL:-0}" = "1" ] && [ "$abs" = "$INSTALL_DIR" ] &&
     [ ! -d "$abs/.git" ] && [ ! -f "$abs/.git" ]; then
    # 由安装目录内 uninstall.sh 转发：HERE 即安装目录，属预期；.git 仓库仍拒
    return 0
  fi
  if [ "$abs" = "$HERE" ]; then
    echo "[删除守卫] 拒绝：目标就是脚本所在目录（源码工作区）" >&2
    return 1
  fi
  if [ -d "$abs/.git" ] || [ -f "$abs/.git" ]; then
    echo "[删除守卫] 拒绝：目标是 Git 源码工作区（$abs）" >&2
    return 1
  fi
  case "$HERE/" in
    "$abs"/*)
      echo "[删除守卫] 拒绝：目标是脚本所在目录的上级（$abs）" >&2
      return 1
      ;;
  esac
  return 0
}

# ---------- 安装标记与文件清单 ----------
# 卸载只按安装器清单删，不整块删目录。清单行格式（清单外一律不动，
# userdata/ 与标记文件永不入清单）：
#   server/xxx      安装目录内单文件（相对路径）
#   dir:client      安装目录内整目录
#   ext:/abs/path   安装目录外由安装器创建的路径（桌面项/插件）

# Decky 每个插件的三个外置目录（settings/data/logs），卸载插件时 Decky 不清理，
# 需单独记账。只认 <插件名>，不碰 ~/homebrew 本身。
decky_home() {
  local h="${DECKY_HOME:-}"
  if [ -z "$h" ]; then
    h="$HOME/homebrew"
    case "${DECKY_PLUGINS_DIR:-}" in
      */plugins) h="${DECKY_PLUGINS_DIR%/plugins}" ;;
    esac
  fi
  printf '%s\n' "${h%/}"
}
decky_plugin_dirs() {
  local h
  h="$(decky_home)"
  printf '%s\n' "$h/settings/DeckMeido" "$h/data/DeckMeido" "$h/logs/DeckMeido"
}

# Decky Music 插件（音乐播控/点歌）是否已装。插件目录名大小写不统一
# （Decky-Music / DeckMusic 都有），按目录名含 music 判定。
decky_music_present() {
  local root d
  root="$(decky_home)/plugins"
  [ -d "$root" ] || return 1
  for d in "$root"/*; do
    [ -d "$d" ] || continue
    case "${d##*/}" in
      *[Mm]usic*) return 0 ;;
    esac
  done
  return 1
}

# ---------- 回收站 ----------
# 一切卸载（含 --purge）只移入回收站，不永久删除：有 gio 且未指定
# DECKMEIDO_TRASH_DIR 时用 gio trash，否则手工写 <Trash>/files + .trashinfo。
# DECKMEIDO_TRASH_DIR 可整体重定向（沙盒测试用，绝不碰真实回收站）。
# dry-run 只打印计划，不建目录不移动。
TRASH_FALLBACK_DIR=""   # 本次实际使用的回收站根（手工模式）
TRASH_COUNT=0           # 移走（或 dry-run 计划移走）的条目数
TRASH_FAILED=0          # 权限不足、没能移走的条目数
DRY_RUN=0               # 1 = 只列计划，零改动
# 系统包卸载结果，收尾汇总必须如实区分「按选择保留」与「以为删了其实没删」
UNINSTALL_KEPT_PKGS=""  # 用户显式选择保留的包
UNINSTALL_LEFT_PKGS=""  # 卸载后复检仍在的包（sudo 失败 / 被依赖挡住）

trash_root() {
  if [ -n "${DECKMEIDO_TRASH_DIR:-}" ]; then
    printf '%s\n' "${DECKMEIDO_TRASH_DIR%/}"
    return 0
  fi
  printf '%s\n' "${XDG_DATA_HOME:-$HOME/.local/share}/Trash"
}

# trashinfo 里的 Path 需要百分号编码；只处理常见字符，中文路径也能落盘
urlencode_path() {
  local s="$1" out="" c i code
  for ((i = 0; i < ${#s}; i++)); do
    c="${s:i:1}"
    case "$c" in
      [A-Za-z0-9/._~-]) out="$out$c" ;;
      *)
        printf -v code '%d' "'$c" 2>/dev/null || code=63
        printf -v c '%%%02X' "$code"
        out="$out$c"
        ;;
    esac
  done
  printf '%s' "$out"
}

_trash_dest_for() {
  local p="$1" dest n=2 base
  base="$(basename -- "$p")"
  # 回收站按文件名平铺，同名项（如 homebrew/{settings,plugins}/DeckMeido）带父目录名区分
  [ -d "$p" ] && [ ! -L "$p" ] && base="$(basename -- "$(dirname -- "$p")")-$base"
  dest="$TRASH_FALLBACK_DIR/files/$base"
  while [ -e "$dest" ] || [ -L "$dest" ]; do
    dest="$TRASH_FALLBACK_DIR/files/$base.$n"
    n=$((n + 1))
  done
  printf '%s' "$dest"
}

trash_path() {
  local p="${1:-}" root dest info
  [ -n "$p" ] || return 1
  [ -e "$p" ] || [ -L "$p" ] || return 0
  if [ "$DRY_RUN" = 1 ]; then
    printf '[计划] 移入回收站：%s\n' "$p"
    TRASH_COUNT=$((TRASH_COUNT + 1))
    return 0
  fi
  # 指定回收站目录时永远走手工路径，保证测试/沙盒里不碰真实回收站
  if [ -z "${DECKMEIDO_TRASH_DIR:-}" ] && need_cmd gio && \
     gio trash -- "$p" >/dev/null 2>&1 && [ ! -e "$p" ]; then
    echo "[回收站] $p"
    TRASH_COUNT=$((TRASH_COUNT + 1))
    return 0
  fi
  root="$(trash_root)"
  TRASH_FALLBACK_DIR="$root"
  mkdir -p "$root/files" "$root/info" 2>/dev/null || {
    warn "无法创建回收站目录：$root（未删除 $p）"
    TRASH_FAILED=$((TRASH_FAILED + 1))
    return 1
  }
  dest="$(_trash_dest_for "$p")"
  info="$root/info/$(basename -- "$dest").trashinfo"
  if mv -- "$p" "$dest" 2>/dev/null; then
    :
  elif [ ! -w "$(dirname -- "$p")" ] && need_cmd sudo && sudo -n mv -- "$p" "$dest" 2>/dev/null; then
    :
  else
    # Windows 的 git-bash 等环境里 mv 会假成功（目标目录建了、源没动），
    # 用「源还在不在」判定，别在没真移走时报告成功
    if [ -e "$p" ] || [ -L "$p" ]; then
      # 兜底：cp 完整搬过去再移除源（跨文件系统、mv 不支持的情况）
      if cp -a -- "$p" "$dest" 2>/dev/null && [ -e "$dest" ]; then
        if [ -d "$p" ] && [ ! -L "$p" ]; then rm -rf -- "$p" 2>/dev/null || true
        else rm -f "$p" 2>/dev/null || true; fi
      fi
    fi
    if [ -e "$p" ] || [ -L "$p" ]; then
      warn "未能移走：$p"
      # 收掉可能建了一半的目标
      [ -e "$dest" ] && [ -z "$(find "$dest" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ] && rmdir "$dest" 2>/dev/null || true
      TRASH_FAILED=$((TRASH_FAILED + 1))
      return 1
    fi
  fi
  {
    printf '[Trash Info]\n'
    printf 'Path=%s\n' "$(urlencode_path "$p")"
    printf 'DeletionDate=%s\n' "$(date '+%Y-%m-%dT%H:%M:%S')"
  } > "$info" 2>/dev/null || true
  echo "[已移入] $dest"
  TRASH_COUNT=$((TRASH_COUNT + 1))
  return 0
}

# ---------- 安装前基线 ----------
# 首次运行抓机器原始状态，升级不覆盖：
#   install_dir_existed=0/1
#   ext:<0|1>:<路径>   0=安装器创建的
#   pkg:<0|1>:<包名>   0=安装器装上的，1=安装前就有
#   unit_enabled / unit_active（ydotool.service 原状态）
capture_install_baseline() {
  [ -f "$BASELINE_FILE" ] && grep -q '^format=2$' "$BASELINE_FILE" 2>/dev/null && return 0
  mkdir -p "$DATA_DIR" 2>/dev/null || return 0
  local tmp="$BASELINE_FILE.tmp" p root existed=0 en="" ac=""
  [ -d "$INSTALL_DIR" ] && existed=1
  {
    printf 'format=2\n'
    printf 'captured_at=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    printf 'install_dir=%s\n' "$INSTALL_DIR"
    printf 'install_dir_existed=%s\n' "$existed"
    for p in "$DESKTOP_DIR/DeckMeido.desktop" "$DESKTOP_FOLDER/DeckMeido.desktop"; do
      if [ -e "$p" ] || [ -L "$p" ]; then
        printf 'ext:1:%s\n' "$p"
      else
        printf 'ext:0:%s\n' "$p"
      fi
    done
    for root in "${DECKY_PLUGINS_DIR:-}" "$HOME/homebrew/plugins"; do
      [ -n "$root" ] || continue
      if [ -e "${root%/}/DeckMeido" ] || [ -L "${root%/}/DeckMeido" ]; then
        printf 'ext:1:%s\n' "${root%/}/DeckMeido"
      else
        printf 'ext:0:%s\n' "${root%/}/DeckMeido"
      fi
    done
    # Decky 插件专属目录：安装前就有的不归本次安装
    while IFS= read -r p; do
      if [ -e "$p" ] || [ -L "$p" ]; then
        printf 'ext:1:%s\n' "$p"
      else
        printf 'ext:0:%s\n' "$p"
      fi
    done < <(decky_plugin_dirs)
    for p in "${DEPS_PACMAN[@]}" "${DEPS_AUR[@]}" "${DEPS_AUR_FALLBACK[@]}"; do
      if need_cmd pacman && pacman -Qq -- "$p" >/dev/null 2>&1; then
        printf 'pkg:1:%s\n' "$p"
      else
        printf 'pkg:0:%s\n' "$p"
      fi
    done
    if need_cmd systemctl; then
      en=$(systemctl --user is-enabled ydotool.service 2>/dev/null || true)
      ac=$(systemctl --user is-active  ydotool.service 2>/dev/null || true)
      [ -n "$en" ] || en=missing
      [ -n "$ac" ] || ac=inactive
    else
      en=unavailable; ac=unavailable
    fi
    printf 'unit_enabled=%s\n' "$en"
    printf 'unit_active=%s\n' "$ac"
  } > "$tmp" 2>/dev/null && mv -f "$tmp" "$BASELINE_FILE" 2>/dev/null || rm -f "$tmp" 2>/dev/null || true
}

baseline_value() {
  local key="$1"
  [ -f "$BASELINE_FILE" ] || return 1
  sed -n "s/^${key}=//p" "$BASELINE_FILE" 2>/dev/null | head -n1
}

baseline_ext_state() {
  local p="$1" line
  [ -f "$BASELINE_FILE" ] || return 1
  line=$(grep -F -m1 -e "ext:0:$p" -e "ext:1:$p" "$BASELINE_FILE" 2>/dev/null || true)
  [ -n "$line" ] || return 1
  case "$line" in
    "ext:0:$p") printf 0 ;;
    *)          printf 1 ;;
  esac
}

# 外部条目安装前是否存在：有基线按基线；无基线时只有内容确属 DeckMeido 才认作
# 我们的，否则按用户原有文件跳过。
ext_preexisting() {
  local p="$1" v
  if v="$(baseline_ext_state "$p")"; then
    printf '%s' "$v"
    return 0
  fi
  case "$p" in
    *.desktop)
      grep -q '^Name=DeckMeido$' "$p" 2>/dev/null && { printf 0; return 0; } ;;
    */DeckMeido)
      grep -q 'DeckMeido' "$p/plugin.json" 2>/dev/null && { printf 0; return 0; } ;;
    */settings/DeckMeido|*/data/DeckMeido|*/logs/DeckMeido)
      # 目录名即插件名，内容全归本插件
      printf 0; return 0 ;;
  esac
  printf 1
}

# 写 .deckmeido-install / .deckmeido-manifest，只记安装器负责的条目：
#   整目录：.venv client assets deploy tools plugin；server 逐文件（跳日志与缓存）；
#   顶层文件（requirements/README/install.sh/VERSION/uninstall.sh 等）；
#   安装目录外固定路径（桌面项、Decky 插件）。format=2 起带 preexisting 标记。
write_install_records() {
  [ -d "$INSTALL_DIR" ] || return 0
  local ver tmp="$INSTALL_MANIFEST.tmp" it rel root
  local now prev_at="" en="" ac="" pre=""
  now="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  ver="$(head -n1 "$INSTALL_DIR/VERSION" 2>/dev/null || true)"
  [ -n "$ver" ] || ver="unknown"
  # 升级不得改写首装时间，否则 pacman 取证时间窗会跑到最后一次升级
  if [ -f "$INSTALL_MARKER" ]; then
    prev_at=$(sed -n 's/^installed_at=//p' "$INSTALL_MARKER" 2>/dev/null | head -n1)
  fi
  [ -n "$prev_at" ] || prev_at="$(baseline_value captured_at 2>/dev/null || true)"
  [ -n "$prev_at" ] || prev_at="$now"
  en="$(baseline_value unit_enabled 2>/dev/null || true)"
  ac="$(baseline_value unit_active 2>/dev/null || true)"
  [ -n "$en" ] || en="unknown"
  [ -n "$ac" ] || ac="unknown"

  {
    printf 'format=2\n'
    printf 'app=%s\n' "$APP"
    printf 'version=%s\n' "$ver"
    printf 'installed_at=%s\n' "$prev_at"
    printf 'refreshed_at=%s\n' "$now"
    printf 'installer_path=%s\n' "$HERE/install.sh"
    printf 'install_dir=%s\n' "$INSTALL_DIR"
    printf 'unit_ydotool_enabled_before=%s\n' "$en"
    printf 'unit_ydotool_active_before=%s\n' "$ac"
  } > "$INSTALL_MARKER" 2>/dev/null || return 0

  : > "$tmp" 2>/dev/null || return 0
  {
    printf 'format=2\n'
    printf 'app=%s\n' "$APP"
    printf 'version=%s\n' "$ver"
    printf 'installed_at=%s\n' "$prev_at"
    printf 'refreshed_at=%s\n' "$now"
    printf 'unit:ydotool.service enabled_before=%s active_before=%s\n' "$en" "$ac"
  } > "$tmp"
  for it in .venv client assets deploy tools plugin; do
    [ -d "$INSTALL_DIR/$it" ] && printf 'dir:%s\tpreexisting=0\n' "$it" >> "$tmp"
  done
  # 安装目录本身也记账：默认卸载只删它里面的条目（用户数据一并保留），
  # --purge 时整目录移入回收站。数据目录若被 DECKMEIDO_DATA_DIR 指到外面，
  # do_uninstall 会单独移走它。
  printf 'dir:.\tpreexisting=0\n' >> "$tmp"
  if [ -d "$INSTALL_DIR/server" ]; then
    # 覆盖 server/ 下全部落盘文件（含运行期生成的 __pycache__、日志、分片），
    # 只排除 userdata/；否则卸载后 server/ 会剩下一堆「未识别文件」
    while IFS= read -r rel; do
      [ -n "$rel" ] || continue
      printf 'server/%s\tpreexisting=0\n' "$rel" >> "$tmp"
    done < <(cd "$INSTALL_DIR/server" && find . -type f \
      ! -path './userdata/*' -printf '%P\n' 2>/dev/null)
  fi
  for it in requirements.txt requirements-gui.txt requirements-stt.txt requirements-tts.txt README.md install.sh RELEASE-NOTES.md VERSION uninstall.sh pip-install.log; do
    [ -f "$INSTALL_DIR/$it" ] && printf '%s\tpreexisting=0\n' "$it" >> "$tmp"
  done
  for it in "$DESKTOP_DIR/DeckMeido.desktop" "$DESKTOP_FOLDER/DeckMeido.desktop"; do
    [ -f "$it" ] || continue
    pre="$(ext_preexisting "$it")"
    printf 'ext:%s\tpreexisting=%s\n' "$it" "$pre" >> "$tmp"
  done
  # Decky 插件目录可能落在 DECKY_PLUGINS_DIR，也可能就是默认的
  # ~/homebrew/plugins；两个都记上，卸载时才不会「插件没卸干净」
  for root in "${DECKY_PLUGINS_DIR:-}" "$HOME/homebrew/plugins"; do
    [ -n "$root" ] || continue
    if [ -d "$root/DeckMeido" ]; then
      pre="$(ext_preexisting "${root%/}/DeckMeido")"
      printf 'ext:%s\tpreexisting=%s\n' "${root%/}/DeckMeido" "$pre" >> "$tmp"
    fi
  done
  # Decky 侧的三个插件专属目录（settings/data/logs）：插件目录卸掉之后 Decky
  # 不会自己清理，必须由卸载器带走，否则「等同从未安装」不成立
  while IFS= read -r it; do
    [ -d "$it" ] || continue
    pre="$(ext_preexisting "$it")"
    printf 'ext:%s\tpreexisting=%s\n' "$it" "$pre" >> "$tmp"
  done < <(decky_plugin_dirs)
  mv -f "$tmp" "$INSTALL_MANIFEST" 2>/dev/null || rm -f "$tmp"
  dim "安装清单：$INSTALL_MANIFEST（$(grep -c -v -e '^format=' -e '^app=' -e '^version=' -e '^installed_at=' -e '^refreshed_at=' -e '^unit:' "$INSTALL_MANIFEST" 2>/dev/null || echo 0) 项）"
  return 0
}

# ---------- 文本宽度 ----------
# 终端里中文占 2 列，按字符数排版框线一定会歪，这里按显示宽度算。
# 纯 bash，不依赖 wc -L。判据与终端实际渲染保持一致：
#   ASCII 1 列；CJK 区、全角标点、emoji 2 列；其余（框线、✓✗★⚠ 等杂项符号）
#   按 1 列——Deck 的 Konsole 就是这么画的，多算会让右框线整行提前缩进。
char_cols() {
  local cp
  printf -v cp '%d' "'$1"
  if [ "$cp" -lt 128 ]; then
    printf 1
  elif { [ "$cp" -ge 4352 ] && [ "$cp" -le 4607 ]; } ||     # 谚文字母
       { [ "$cp" -ge 12288 ] && [ "$cp" -le 12351 ]; } ||    # CJK 标点（、。「」）
       { [ "$cp" -ge 12352 ] && [ "$cp" -le 13247 ]; } ||    # 假名
       { [ "$cp" -ge 13312 ] && [ "$cp" -le 19903 ]; } ||    # CJK 扩展 A
       { [ "$cp" -ge 19968 ] && [ "$cp" -le 40959 ]; } ||    # CJK 统一表意
       { [ "$cp" -ge 44032 ] && [ "$cp" -le 55203 ]; } ||    # 谚文音节
       { [ "$cp" -ge 65024 ] && [ "$cp" -le 65039 ]; } ||    # 变体选择符
       { [ "$cp" -ge 65280 ] && [ "$cp" -le 65519 ]; } ||    # 全角形式（：；（）等）
       { [ "$cp" -ge 127744 ] && [ "$cp" -le 129791 ]; } ||  # emoji
       { [ "$cp" -ge 65536 ] && [ "$cp" -le 1114111 ]; }; then
    printf 2
  else
    printf 1
  fi
}
disp_len() {
  local s="$1" i w=0
  for ((i = 0; i < ${#s}; i++)); do
    w=$((w + $(char_cols "${s:i:1}")))
  done
  printf '%s' "$w"
}

# 终端实际宽度：优先 $COLUMNS，其次直接问 /dev/tty。
# 安装过程中 stdout 会被 tee 接走，这时 tput 读不到真实行宽、会退回 80，
# 窄终端上框线就会越界，所以 stty 走 /dev/tty 而不是 stdout。
term_cols() {
  local c="${COLUMNS:-}" sz
  if [ -z "$c" ] || ! [[ "$c" =~ ^[0-9]+$ ]]; then
    sz=$(stty size < /dev/tty 2>/dev/null | awk '{print $2}')
    c="${sz:-}"
  fi
  [ -n "$c" ] || c=$(tput cols < /dev/tty 2>/dev/null || echo 80)
  printf '%s' "${c:-80}"
}
# 开场大字/分区标题需要 72 列；结果卡片窄一点也能画（>=44 列）
ui_fits() { [ "$OUT_IS_TTY" = 1 ] && [ "$(term_cols)" -ge 72 ]; }
card_fits() { [ "$OUT_IS_TTY" = 1 ] && [ "$(term_cols)" -ge 44 ]; }

# 卡片内文字可用列数：外框总宽 = 内宽 + 3（左框 + 空格 + 右框）。
# 宽屏上限 59（总宽 62，沿用旧版视觉宽度），窄终端按实际宽度收窄。
card_inner() {
  local c inner
  c=$(term_cols)
  inner=$((c - 5))
  [ "$inner" -gt 59 ] && inner=59
  [ "$inner" -lt 24 ] && inner=24
  printf '%s' "$inner"
}

# 分区标题：┌─ 名称 ───────
# 六步版式下（STEP_QUIET=1）不再插分区标题，避免把「每步一行结果」冲散。
section() {
  [ "${STEP_QUIET:-0}" = "1" ] && return 0
  local title="$1"
  if ui_fits; then
    local head="── $title " used fill
    used=$(disp_len "$head")
    fill=$((62 - used))
    [ "$fill" -lt 2 ] && fill=2
    printf '\n%s┌%s%s%s\n' "$C_C" "$head" "$(printf '─%.0s' $(seq 1 "$fill"))" "$C_END"
  else
    printf '\n==> %s\n' "$title"
  fi
}

# 表格一行：左列包名定宽 18（最长的 gst-plugin-pipewire 是 18 列），
# 不够宽时至少留 2 格间隔，别把右列贴上去
table_row() {
  local left="$1" right="$2" pad
  pad=$((18 - $(disp_len "$left")))
  [ "$pad" -lt 2 ] && pad=2
  printf '  %s%s%s%*s%s\n' "$C_OK" "$left" "$C_END" "$pad" "" "$right"
}

# 清掉本次安装的 pip 下载缓存。装完自动调，用户不用管「结束后可清」这句。
# 只删 PIP_CACHE_DIR（安装器自己建的），绝不碰用户 ~/.cache/pip；
# 失败也不影响安装结果，最多下次重下同一个 wheel。
purge_pip_cache() {
  local before=0
  [ -d "$PIP_CACHE_DIR" ] || return 0
  before=$(du -sm "$PIP_CACHE_DIR" 2>/dev/null | awk '{print $1}')
  before=${before:-0}
  rm -rf -- "$PIP_CACHE_DIR" 2>/dev/null || true
  if [ -d "$PIP_CACHE_DIR" ]; then
    step_note "下载缓存没删干净（$PIP_CACHE_DIR），不影响使用，可手动删除"
  elif [ "$before" -ge 1 ]; then
    step_note "已自动清理下载缓存，腾出约 $(mb_text "$before")"
  fi
}

# 完成卡片：带边框的结果汇总
# 宽度跟着终端走；单行超过内宽时按显示宽度折行，右边的 │ 不会再被顶出屏幕。
card() {
  CARD_COL=0
  if card_fits; then
    local inner n
    inner=$(card_inner); n=$((inner + 1))
    printf '\n%s┌%s┐%s\n' "$C_C" "$(printf '─%.0s' $(seq 1 "$n"))" "$C_END"
  else
    echo
  fi
}
# 一行卡片：文字 + 补齐到内宽 + 右框线。
# 宽度估算再准，也架不住终端把某些字按别的宽度渲染（中英混排、符号、字体回退），
# 一行算错就把后面所有行的 │ 推歪。所以真终端里不靠空格补，直接「移到固定列」：
# 每次重画先回到行首、清掉整行，再在算好的列上画右框线，物理对齐。
# $1=文本 $2=内宽 $3=是否加粗
CARD_COL=0
card_row() {
  local text="$1" inner="$2" bold="${3:-0}"
  if [ "$OUT_IS_TTY" = 1 ]; then
    # 光标定位不依赖宽度估算：先回行首清行，内容照写，再跳到固定列画 │。
    # 中英混排/符号宽度哪怕算错，右框线也还是齐的。
    printf '\r\033[K'
    if [ "$bold" = 1 ]; then
      printf '%s│%s %s%s%s' "$C_C" "$C_END" "$C_B" "$text" "$C_END"
    else
      printf '%s│%s %s' "$C_C" "$C_END" "$text"
    fi
    printf '\033[%dG%s│%s\n' "$((CARD_COL + inner + 3))" "$C_C" "$C_END"
    CARD_COL=$((CARD_COL + inner + 3))
    return 0
  fi
  local pad
  pad=$((inner - $(disp_len "$text")))
  [ "$pad" -lt 0 ] && pad=0
  if [ "$bold" = 1 ]; then
    printf '%s│%s %s%s%s%*s%s│%s\n' \
      "$C_C" "$C_END" "$C_B" "$text" "$C_END" "$pad" "" "$C_C" "$C_END"
  else
    printf '%s│%s %s%*s%s│%s\n' \
      "$C_C" "$C_END" "$text" "$pad" "" "$C_C" "$C_END"
  fi
}
card_text() {
  local text="$1" bold="${2:-0}" inner i ch w cur="" cw=0 box=1
  if card_fits; then
    inner=$(card_inner)
  elif [ "$OUT_IS_TTY" = 1 ]; then
    # 太窄画不了框，但长行仍要折行，否则纯文本也会冲出终端
    box=0
    inner=$(term_cols); inner=$((inner - 3))
    [ "$inner" -lt 12 ] && inner=12
  else
    # 写日志/管道：不画框，但仍按终端宽度折行。以前这里原样输出一行，
    # 拼成卡片时右框线被长行顶出去，整张卡看起来就是歪的。
    local wrap=$(( $(term_cols) - 3 )); [ "$wrap" -lt 24 ] && wrap=24
    inner=$wrap; box=0
  fi
  # 折行：优先在空格和中文标点后断，避免把 URL、单词从中间劈开；
  # 一整行没有任何可断点时（比如超长 URL）才硬断。
  # 续行缩进两格：终端自己折行会把中文断在任意位置，主动折断至少能看出是续行。
  local cont="  " contw=2 cur="" cw=0 ch w seg lastbreak=-1
  for ((i = 0; i < ${#text}; i++)); do
    ch="${text:i:1}"
    w=$(char_cols "$ch")
    if [ $((cw + w)) -gt "$inner" ] && [ -n "$cur" ]; then
      if [ "$lastbreak" -ge 0 ] && [ "$lastbreak" -lt "${#cur}" ]; then
        # 在最近的可断点切开：断点前的部分输出，剩下的字符回退给下一轮处理
        seg="${cur:0:lastbreak}"
        i=$((i - (${#cur} - lastbreak)))
        cur=""
        cw=0
      else
        seg="$cur"
        cur=""
        cw=0
      fi
      if [ "$box" = 1 ]; then
        card_row "$seg" "$inner" "$bold"
      else
        printf '  %s\n' "$seg"
      fi
      cur="$cont"; cw=$contw; lastbreak=-1
      continue
    fi
    cur+="$ch"; cw=$((cw + w))
    # 记「断点之前的字符数」：cur 开头可能有续行缩进，用长度直接当偏移会错位
    case "$ch" in
      " "|"、"|"，"|"。"|"；"|"：") lastbreak=$((${#cur} - 1)) ;;
    esac
  done
  if [ "$box" = 1 ]; then
    card_row "$cur" "$inner" "$bold"
  else
    printf '  %s\n' "$cur"
  fi
}
card_line() { card_text "$1" 0; }
card_title() { card_text "$1" 1; }
card_end() {
  if card_fits; then
    local inner n
    inner=$(card_inner); n=$((inner + 1))
    # 底框和顶框一样，按上面记下的左边距对齐（真终端里 CARD_COL 恒为 0）
    if [ "$OUT_IS_TTY" = 1 ]; then
      printf '\033[%dG%s└%s┘%s\n' "$((CARD_COL + 1))" "$C_C" \
        "$(printf '─%.0s' $(seq 1 "$n"))" "$C_END"
    else
      printf '%s└%s┘%s\n' "$C_C" "$(printf '─%.0s' $(seq 1 "$n"))" "$C_END"
    fi
  fi
}

# 开场：DeckMeido 大字（figlet "Big" 字体），下面接标题与简介
print_banner() {
  ui_fits || return 0
  # 自举/菜单链路会先后调用两次（外层菜单一次、子脚本一次），
  # 用环境变量把「已打过」传给子进程，否则首屏会被顶出可视区。
  [ "${DECKMEIDO_BANNER_SHOWN:-0}" = "1" ] && return 0
  export DECKMEIDO_BANNER_SHOWN=1
  printf '%s\n' "$C_C"
  cat <<'ART'
 _____            _    __  __      _     _
|  __ \          | |  |  \/  |    (_)   | |
| |  | | ___  ___| | _| \  / | ___ _  __| | ___
| |  | |/ _ \/ __| |/ / |\/| |/ _ \ |/ _` |/ _ \
| |__| |  __/ (__|   <| |  | |  __/ | (_| | (_) |
|_____/ \___|\___|_|\_\_|  |_|\___|_|\__,_|\___/
ART
  printf '%s\n' "$C_END"
  printf '\n  %s住在 Steam Deck 里的 AI 女仆%s\n' "$C_B" "$C_END"
  printf '  %s替你聊天、查攻略、开游戏、点屏幕——这一台掌机的女仆%s\n' "$C_DIM" "$C_END"
  printf '  %s%s%s\n\n' "$C_DIM" "$(printf '─%.0s' $(seq 1 54))" "$C_END"
}

# ---------- 安装容量总表 ----------
# 全脚本唯一体积来源：预估与各模块实际占用都读这张表。单位 MB（解包后磁盘占用，
# pip 缓存/临时文件余量见 EST_SCRATCH_MB）。格式：<键>|<显示名>|<MB>
DEP_TABLE=(
  "server|服务端运行依赖（FastAPI 等）|90"
  "gui|桌面客户端（PySide6 + WebEngine）|900"
  "stt_mod|语音输入运行库（sherpa-onnx）|60"
  "tts_mod|语音合成运行库（piper-plus）|120"
  "wake_mod|唤醒词分词依赖（sentencepiece / pypinyin）|30"
  "sys|系统控件（ydotool / wtype / kdotool 等）|60"
  "stt_model|语音识别模型（SenseVoice）|250"
  "tts_model|语音合成模型（つくよみちゃん）|38"
  "wake_model|唤醒词模型（KWS + silero VAD）|40"
  "nltk|英文语音数据（nltk_data）|15"
  "plugin|Decky 插件包|3"
  "app|程序文件（server / client / plugin）|80"
)
# pip 下载缓存 + 解包临时空间：装完可以清掉，但安装过程中必须留得下
EST_SCRATCH_MB=1200
# 安装完成后仍应保持的最小空闲空间，低于这个值就提前拦下来
EST_MIN_FREE_MB=2048

dep_mb() {
  local want="$1" row
  for row in "${DEP_TABLE[@]}"; do
    case "$row" in
      "$want"\|*) printf '%s' "${row##*|}"; return 0 ;;
    esac
  done
  printf '0'
}

dep_label() {
  local want="$1" row
  for row in "${DEP_TABLE[@]}"; do
    case "$row" in
      "$want"\|*)
        row="${row#*|}"
        printf '%s' "${row%|*}"
        return 0 ;;
    esac
  done
  printf '%s' "$want"
}

# MB → 「1.2GB」「350MB」
mb_text() {
  local mb="${1:-0}"
  if [ "$mb" -ge 1024 ]; then
    # 纯 bash 取一位小数，不依赖 awk（缺 awk 时旧写法会输出空串）
    printf '%d.%dGB' $((mb / 1024)) $((mb % 1024 * 10 / 1024))
  else
    printf '%sMB' "$mb"
  fi
}

# 目录实际占用（MB，向上取整）；不存在输出 0
path_mb() {
  local p="$1"
  [ -e "$p" ] || { printf '0'; return 0; }
  du -sm "$p" 2>/dev/null | awk '{print $1+0}' || printf '0'
}

# 安装前容量预估：在一开始就把「总共要占多少、大概要多久」说清楚，并把每个模块的
# 预估占用列出来。选中的模块由调用方通过 $1 传入（空格分隔的键名）。
# 耗时是同一条命令里唯一会变的量，所以先实测一下网速（取最快的 pip 源），
# 一个都测不通就按经验值 2MB/s 算，并在表里如实写明是按这个速率估的。
# 测速放在打印表格之前，测完表立刻出来（之前表先出、测速跟在后面，像卡住）。
# 结果写进 PLAN_TOTAL_MB / PLAN_SECONDS，供 step_ok 复述同一套数字。
EST_FALLBACK_RATE=2097152
# 按 2MB/s 折算下来的耗时（秒）：超过这个量就别在主流程里等测速了
EST_SLOW_SECONDS=900
# 实测到的网速（字节/秒）、是不是真测出来的、各源速率
EST_RATE=0
EST_RATE_MEASURED=0
declare -A _PIP_SPD=()
# 测速：在容量表之前跑完，用户先看到「在测网速 / 各源多快」，随后立刻出带耗时的成品表。
# $1=1 表示安静模式（整体要装的量很大时不测，避免为几秒的测速多等）。
est_net_rate() {
  local rate=0 quiet="${1:-0}" best_url
  if need_cmd curl; then
    [ "$quiet" = 1 ] || dim "测一下网速，好估算本次安装大概要多久"
    # 必须在当前 shell 里直接调：套 $( ) 会落在子 shell，_PIP_SPD 缓存带不回来，
    # 紧接着装依赖时还得重新测一遍
    pick_pip_index >/dev/null || true
    # 取最快的那个源
    best_url=$(pick_pip_index 2>/dev/null | tail -n1)
    if [[ "${best_url:-}" =~ ^[0-9]+$ ]]; then
      rate=${_PIP_SPD[${PIP_INDEXES[$best_url]}]:-0}
    fi
  fi
  # 记下是不是真测出来的：连不上时表里要写「按经验值估」，不能假装是实测
  EST_RATE_MEASURED=0
  if [ "$rate" -le 0 ]; then
    rate=$EST_FALLBACK_RATE
  else
    EST_RATE_MEASURED=1
  fi
  # 走全局变量返回：调用方用 $(...) 取会落在子 shell，EST_RATE_MEASURED 就丢了
  EST_RATE=$rate
}

# 下载耗时估算（秒）：体积 ÷ 速率。纯 bash 整数运算，不依赖 awk
# $1 = MB  $2 = 速率(字节/秒)
est_seconds() {
  local mb="${1:-0}" r="${2:-1}"
  [ "$r" -gt 0 ] || r=1
  printf '%s' $((mb * 1048576 / r))
}

print_capacity_plan() {
  local total=0 k free_mb rate secs big="" big_mb=0 rows=0
  local -a names=() sizes=()
  for k in $1; do
    total=$((total + $(dep_mb "$k")))
    names+=("$(dep_label "$k")")
    sizes+=("$(dep_mb "$k")")
    if [ "$(dep_mb "$k")" -gt "$big_mb" ]; then big_mb=$(dep_mb "$k"); big="$k"; fi
  done
  free_mb=$(df -Pm "$HOME" 2>/dev/null | sed -n '2s/.* //p')
  free_mb=${free_mb//[!0-9]/}
  free_mb=${free_mb:-0}
  # 只有模型 + 程序包是纯下载；pip 依赖一半下载、一半本机解包编译，按 50% 折算
  local models=0 pkgs=0 net_mb=0 dl_secs=0
  for k in $1; do
    case "$k" in
      app|plugin|stt_model|tts_model|wake_model|nltk) models=$((models + $(dep_mb "$k"))) ;;
      *) pkgs=$((pkgs + $(dep_mb "$k") / 2)) ;;
    esac
  done
  net_mb=$((models + pkgs))
  # 测速必须在表格之前：先让用户看到「在测网速、各源多快」，表出来就是带耗时的成品表，
  # 不会再出现「表打完了才慢慢测速」的停顿。要装的量很大时就跳过测速——
  # 那点时间在整体安装里可以忽略，为它多等几秒不划算。
  if [ $((net_mb * 1048576 / EST_FALLBACK_RATE)) -ge "$EST_SLOW_SECONDS" ]; then
    est_net_rate 1
  else
    est_net_rate
  fi
  rate=$EST_RATE
  dl_secs=$(est_seconds "$net_mb" "$rate")
  # 下载之外还要装包、建 venv、解包模型：固定再留 2 分钟
  secs=$((dl_secs + 120))

  # 标签宽度跟着最长的一项走，体积列才对得齐（之前定宽 14，标签基本都超，
  # 结果体积直接贴在标签后面）
  local lw=10
  for k in "${!names[@]}"; do
    local w=$(disp_len "${names[$k]}")
    [ "$w" -gt "$lw" ] && lw=$w
  done
  local gap
  printf '      %s【安装前请看】%s\n' "$C_B" "$C_END"
  for k in "${!names[@]}"; do
    gap=$((lw - $(disp_len "${names[$k]}") + 2))
    printf '  %s%s%s%*s%6s\n' "$C_OK" "${names[$k]}" "$C_END" "$gap" "" "$(mb_text "${sizes[$k]}")"
    rows=$((rows + 1))
  done
  [ "$rows" -gt 0 ] && echo
  printf '      %s合计约 %s%s' "$C_B" "$(mb_text "$total")" "$C_END"
  printf '%s（安装时另需约 %s 下载缓存，装完自动清理）%s\n' "$C_DIM" "$(mb_text $EST_SCRATCH_MB)" "$C_END"
  printf '      %s预计耗时 %s%s' "$C_B" "$(human_dur "$secs")" "$C_END"
  if [ "${EST_RATE_MEASURED:-1}" = "1" ]; then
    printf '%s（按实测网速 %s，约需下载 %s）%s\n' \
      "$C_DIM" "$(human_rate "$rate")" "$(human_size $((net_mb * 1048576)))" "$C_END"
  else
    printf '%s（没测出可用网速，按 2MB/s 估，约需下载 %s；实际可能更久）%s\n' \
      "$C_DIM" "$(human_size $((net_mb * 1048576)))" "$C_END"
  fi
  printf '      %s其中 %s 最费时（%s）%s\n' \
    "$C_DIM" "$(dep_label "$big")" "$(mb_text "$big_mb")" "$C_END"
  printf '      %s当前 %s 可用 %s%s\n' "$C_DIM" "$HOME" "$(mb_text "$free_mb")" "$C_END"
  if [ "$free_mb" -gt 0 ] &&
     [ "$free_mb" -lt $((total + EST_SCRATCH_MB + EST_MIN_FREE_MB)) ]; then
    warn "磁盘空间偏紧：建议先腾出 $(mb_text $((total + EST_SCRATCH_MB + EST_MIN_FREE_MB))) 空闲空间"
  fi
  PLAN_TOTAL_MB=$total
  PLAN_SECONDS=$secs
}

# SteamOS 镜像号：换镜像时 /usr 下 pacman 装的包会被整片冲掉，这是本脚本
# 一切「装完还要实测」逻辑的根源。之前 BUILD_ID 从未赋值，导致镜像变动
# 检测（system_notes 里的提示）永远是死的，这里统一从 os-release 读。
BUILD_ID="$( { sed -n 's/^BUILD_ID=//p' /etc/os-release 2>/dev/null || true; } | tr -d '"' | head -n1 || true)"

# 机型：Jupiter = Steam Deck LCD，Galileo = Steam Deck OLED。
# 脚本对两种机型走同一套逻辑（都是 SteamOS 3、holo 仓库、同样的系统包），
# 这里只记一行进安装日志——排障时要先分清是哪台机器装的。
deck_model() {
  local name
  name="$(cat /sys/devices/virtual/dmi/id/product_name 2>/dev/null || true)"
  case "$name" in
    *Jupiter*) printf 'Steam Deck LCD（Jupiter）' ;;
    *Galileo*) printf 'Steam Deck OLED（Galileo）' ;;
    "") printf '未知' ;;
    *) printf '%s' "$name" ;;
  esac
}

# ---------- 前置检查 ----------
check_user() {
  if [ "$(id -u)" = "0" ]; then
    die "不要用 sudo / root 运行。请直接执行：bash install.sh（会自动装到 $HOME/$APP_DIR）"
  fi
}

check_prereq() {
  check_user
  need_cmd python3 || die "找不到 python3，请先在系统设置里开启开发者模式并安装 Python"
  python3 - <<'PY' || die "需要 Python 3.11 或更高版本"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
  if ! python3 -c "import venv, ensurepip" >/dev/null 2>&1; then
    die "缺少 python3-venv，请先安装（SteamOS: sudo steamos-readonly disable && sudo pacman -S python）"
  fi
}

# 装前预检：提前把「装到一半才炸」的情况暴露出来，而不是等 venv 建好、
# 依赖下完之后才因为磁盘满/端口被占而失败
preflight() {
  local problems=0
  dim "机型：$(deck_model)"
  # 1. 磁盘空间：具体要多少由 print_capacity_plan 按本次实际要装的项逐项算，
  #    这里只兜一个硬底线
  local avail_mb=""
  avail_mb=$(df -Pm "$HOME" 2>/dev/null | awk 'NR==2 {print $4}')
  if [ -n "$avail_mb" ] && [ "$avail_mb" -lt "$EST_MIN_FREE_MB" ]; then
    warn "家目录剩余 ${avail_mb}MB，低于最低要求 ${EST_MIN_FREE_MB}MB"
    problems=1
  else
    dim "磁盘空间：剩余 ${avail_mb:-未知}MB"
  fi

  # 2. 端口占用：与 stop_backend 共用 _port_owned_by_us / _pid_is_ours 这一套
  #    判据。以前预检用 grep 猜「holder 里有没有 python」，停机却要求路径匹配，
  #    结果一边说「自己人在用」、一边停不掉（0.7.3 首次安装就栽在这）。
  if ! _port_has_listener "$PORT"; then
    dim "端口 $PORT 空闲"
  elif _port_owned_by_us "$PORT"; then
    dim "端口 $PORT 由 DeckMeido 自己在用（升级时正常）"
  else
    warn "端口 $PORT 被占用，且不是本程序的后端"
    _port_holder_report "$PORT"
    dim "先关掉它，或换端口：DECKMEIDO_PORT=18766 bash install.sh"
    problems=1
  fi

  # 3. 图形会话：客户端要靠它把窗口开出来，缺了就只能从桌面手动启动
  if [ -z "${XDG_RUNTIME_DIR:-}" ] || [ ! -d "${XDG_RUNTIME_DIR:-}" ]; then
    warn "XDG_RUNTIME_DIR 不存在，装完后请从桌面图标启动客户端"
    problems=1
  fi
  [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] || \
    warn "当前环境没有 DISPLAY / WAYLAND_DISPLAY，装完不会自动打开界面"

  [ "$problems" = 1 ] || dim "预检通过"
}

system_notes() {
  local miss=()
  dep_scan_missing miss
  if [ ${#miss[@]} -gt 0 ]; then
    warn "以下系统包缺失，相关功能不可用：${miss[*]}"
    dim "重跑安装命令即可补上：bash install.sh install"
    # SteamOS 换镜像会冲掉 /usr 里 pacman 装的包
    if [ -n "${BUILD_ID:-}" ] && [ -f "$HOME/.config/deckmeido/.deps-build-id" ]; then
      local prev; prev="$(cat "$HOME/.config/deckmeido/.deps-build-id" 2>/dev/null || true)"
      [ "$prev" = "$BUILD_ID" ] || dim "SteamOS 镜像已更新（$prev → ${BUILD_ID}），系统包可能被冲掉，重装即可恢复"
    fi
  fi
  # ydotool 在但 ydotoold 没跑 → socket 报错
  if need_cmd ydotool && ! pgrep -x ydotoold >/dev/null 2>&1; then
    warn "ydotoold 未运行，模拟点击/输入会失败；重跑 bash install.sh install"
  fi
}

# ---------- 系统依赖 ----------
# 包名≠命令名的项在 dep_cmd 里映射；检测一律按命令名（need_cmd），不用 pacman -Q。
# 取舍：base 自带（bash/systemd/procps/iproute2/glib2/sudo）不列；
# 纯兜底（ffmpeg/paplay/aplay/lsof/pdftotext）不列，缺了主路径仍可用。
#   tmux       命令面板/Agent 终端整条链路依赖（非 base，SteamOS 无）
#   qt6-tools  qdbus6 写 Klipper 剪贴板，中文输入唯一通道
#   gstreamer / gst-plugin-pipewire  gst-launch-1.0 与 pipewiresrc 抓帧
#   gst-plugins-base/good  videoconvert/pngenc/jpegenc
#   wireplumber  wpctl 音量（缺了退 pactl，故属降级可用）
#   pulseaudio-utils  parec：唤醒词/录音首选采集，缺了才退 pw-record
DEPS_PACMAN=(espeak-ng ydotool wtype fakeroot spectacle libpulse pipewire wireplumber
             pulseaudio-utils gstreamer gst-plugin-pipewire gst-plugins-base
             gst-plugins-good tmux qt6-tools)
# 部分发行版 qt6-tools 拆包，qdbus6 也可能在 qt5-tools（DEPS_PACMAN 装不上时补这个）
# AUR 无 kdotool-bin 时回落到源码包 kdotool
DEPS_AUR=(kdotool-bin)
DEPS_AUR_FALLBACK=(kdotool)
# 装完仍缺的包名 → 汇总如实报缺，不假打勾
DEPS_MISSING_AFTER=()

# $1=包名 → 打印一个「已装则必有」的命令名（无映射则同名）
dep_cmd() {
  case "$1" in
    libpulse)         echo pactl ;;
    pipewire)         echo pw-record ;;
    pulseaudio-utils) echo parec ;;
    wireplumber)      echo wpctl ;;
    gstreamer)        echo gst-launch-1.0 ;;
    gst-plugin-pipewire) echo gst-inspect-1.0 ;;
    gst-plugins-base) echo gst-inspect-1.0 ;;
    gst-plugins-good) echo gst-inspect-1.0 ;;
    qt6-tools|qt5-tools) echo qdbus6 ;;
    kdotool-bin)      echo kdotool ;;
    *)                echo "$1" ;;
  esac
}

# gst 插件类无独立可执行文件，改用 gst-inspect 查元素是否存在
dep_present() {
  local p="$1"
  case "$p" in
    gst-plugin-pipewire) gst-inspect-1.0 pipewiresrc >/dev/null 2>&1 ;;
    gst-plugins-base)    gst-inspect-1.0 pngenc >/dev/null 2>&1 ;;
    gst-plugins-good)    gst-inspect-1.0 jpegenc >/dev/null 2>&1 ;;
    qt6-tools|qt5-tools) need_cmd qdbus6 || need_cmd qdbus ;;
    *)                   need_cmd "$(dep_cmd "$p")" ;;
  esac
}

# 全部系统包是否就位：缺的追加到 $1 数组
dep_scan_missing() {
  local -n _miss="$1"; local p
  for p in "${DEPS_PACMAN[@]}" "${DEPS_AUR_FALLBACK[0]}"; do
    dep_present "$p" || _miss+=("$p")
  done
}

# 离线包：仅随包带小且依赖浅的 ydotool/wtype/fakeroot。
# espeak-ng（依赖 libsonic/pcaudiolib）与 AUR 的 kdotool 走镜像站。
# 清单见 tools/fetch_offline_deps.sh，需同步。SteamOS 换镜像会重建 /usr 并清空
# pacman 缓存，故另带一份。
OFFLINE_DIR="$HERE/deploy/deps"
# 只覆盖 deploy/deps/*.pkg.tar.zst，与 dist/SHA256SUMS 互不替代
OFFLINE_MANIFEST="$OFFLINE_DIR/SHA256SUMS"

# 离线包装成功的包名记录（供 offline_stale / 镜像更新后重装判断）
offline_stamp() { printf '%s' "$CONFIG_DIR/.deps-offline"; }

# 哈希校验：清单必须列出该文件且实算一致。不符一律拒装：
# pacman -U 以 root 运行，来源不明包 = 提权入口。
offline_hash_ok() {
  local f="$1" base want got
  [ -f "$OFFLINE_MANIFEST" ] || return 1
  base="$(basename "$f")"
  want="$(awk -v n="$base" '($2==n)||($2=="*" n){print $1}' "$OFFLINE_MANIFEST" 2>/dev/null | head -n1)"
  [ -n "$want" ] || return 1
  got="$(sha256sum "$f" 2>/dev/null | awk '{print $1}')"
  [ "$got" = "$want" ]
}

# 已装版本更新时跳过错离线包，避免降级
pkg_is_downgrade() {
  local f="$1" p="$2" fv iv
  fv="$(pacman -Qp "$f" 2>/dev/null | awk '{print $2}')"
  iv="$(pacman -Q "$p" 2>/dev/null | awk '{print $2}')"
  [ -n "$fv" ] && [ -n "$iv" ] || return 1
  need_cmd vercmp || return 1
  [ "$(vercmp "$fv" "$iv" 2>/dev/null)" -lt 0 ]
}

# 从发布包自带的 deploy/deps 挑出某个包最新的那一份。
# 三条硬条件：包名回读一致（防 kdotool / kdotool-bin 这类前缀串包）、
# 哈希对得上清单、不构成降级。不满足就当没有这一层。
offline_bundle_file() {
  local p="$1" f best="" base
  [ -d "$OFFLINE_DIR" ] || return 0
  for f in "$OFFLINE_DIR/$p"-*.pkg.tar.zst; do
    [ -f "$f" ] || continue
    base="$(basename "$f")"
    [ "$(pacman -Qp "$f" 2>/dev/null | awk 'NR==1{print $1}')" = "$p" ] || continue
    if ! offline_hash_ok "$f"; then
      warn "离线包哈希校验失败，已跳过：$base"
      continue
    fi
    pkg_is_downgrade "$f" "$p" && { dim "已装版本比离线包新，跳过 $p"; continue; }
    if [ -z "$best" ] || [ "$(printf '%s\n%s\n' "$(basename "$best")" "$base" | sort -V | tail -n1)" = "$base" ]; then
      best="$f"
    fi
  done
  [ -n "$best" ] && printf '%s' "$best"
  return 0
}

# 用本地包直接装，不碰网络。缺点是不会自动补齐依赖树以外的版本约束，
# 所以装完仍然实测一遍命令在不在。
offline_pacman_install() {
  local want=("$@") files=() p hit
  [ -d "$OFFLINE_DIR" ] || return 1
  for p in "${want[@]}"; do
    hit="$(offline_bundle_file "$p")"
    [ -n "$hit" ] && files+=("$hit")
  done
  [ ${#files[@]} -gt 0 ] || return 1
  info "从发布包自带离线包安装：$(printf '%s ' "${files[@]##*/}")"
  local log
  if log=$(sudo pacman -U --noconfirm --needed "${files[@]}" </dev/null 2>&1); then
    info "离线包安装完成"
  else
    warn "离线包安装失败，稍后回退联网"
    dim "$(printf '%s' "$log" | tail -12)"
    return 1
  fi
  # 记录装成的那几个（包名从包元数据回读，不靠文件名猜）
  local names=() base
  for p in "${files[@]}"; do
    base="$(pacman -Qp "$p" 2>/dev/null | awk 'NR==1{print $1}')"
    [ -n "$base" ] && names+=("$base")
  done
  mkdir -p "$CONFIG_DIR" 2>/dev/null || true
  printf '%s\n' "${names[*]}" > "$(offline_stamp)" 2>/dev/null || true
  return 0
}

# 离线包是不是比系统仓库还旧。SteamOS 换镜像后 holo 仓库里的 espeak-ng
# 之类可能已经升级过一轮，再用旧包装回去会被 pacman 判成降级。
# 这里只做提示，不拦着装——离线优先本来就是用在断网场景。
offline_check_fresh() {
  [ -d "$OFFLINE_DIR" ] || return 0
  need_cmd expac || return 0
  local p f lv
  for f in "$OFFLINE_DIR"/*.pkg.tar.zst; do
    [ -e "$f" ] || continue
    p=$(basename "$f")
    p="${p%%-[0-9]*}"
    lv=$(expac -S '%v' "$p" 2>/dev/null | head -n1)
    [ -n "$lv" ] || continue
    local have
    have=$(basename "$f" .pkg.tar.zst)
    have="${have#"$p"-}"
    have="${have%-*}"
    if [ "$(printf '%s\n%s\n' "$have" "$lv" | sort -V | tail -n1)" = "$lv" ] && [ "$have" != "$lv" ]; then
      warn "离线包 $p $have 比系统仓库的 $lv 旧"
      dim "断网时装它仍可用；联网时建议让它走仓库：重跑 bash install.sh install"
    fi
  done
}

# ---------- 数据库同步失败时的三层兜底 ----------
# ① /var/cache/pacman/pkg ② deploy/deps ③ $MIRROR/deps。每层只装能拿到的，
# 装完实测命令，缺的继续下一层；全拿不到就如实报缺，不打勾。

# pacman -U 一批本地包。needed=1 跳过已装版本；needed=0 强制覆盖重装。
pacman_u_batch() {
  local needed="$1"; shift
  local log
  local -a opts=(--noconfirm)
  [ "$needed" = 1 ] && opts+=(--needed)
  if log=$(sudo pacman -U "${opts[@]}" "$@" </dev/null 2>&1); then
    return 0
  fi
  warn "pacman -U 失败："
  dim "$(printf '%s' "$log" | tail -12)"
  return 1
}

# 从镜像站 deps/ 取某个包：先拿清单（一次任务只下一次），再下包，
# 哈希与包名都校验通过才返回路径；任何一步不放心就返回空。
mirror_fetch_pkg() {
  local p="$1" dest="$2" man="$2/SHA256SUMS" base want got
  if [ ! -s "$man" ]; then
    curl -fsSL --connect-timeout 8 --max-time 25 "$MIRROR/deps/SHA256SUMS" -o "$man" 2>/dev/null || return 0
  fi
  # 清单行是 `sha256 名称`；Windows 生成的清单会带 `*` 前缀（sha256sum -b），
  # 这里先剥掉再按前缀挑版本最新的一份
  base="$(awk -v p="$p" '{n=$2; sub(/^\*/, "", n); if (n ~ ("^" p "-")) print n}' "$man" | sort -V | tail -n1)"
  [ -n "$base" ] || return 0
  case "$base" in
    *.pkg.tar.zst|*.pkg.tar.xz|*.pkg.tar.gz) ;;
    *) return 0 ;;
  esac
  # 只接受平铺文件名：清单若被改成 ../ 之类，不能让它写/读到别处
  case "$base" in */*|*..*) return 0 ;; esac
  curl -fsSL --connect-timeout 10 --max-time 300 "$MIRROR/deps/$base" -o "$dest/$base" 2>/dev/null || return 0
  want="$(awk -v n="$base" '($2==n)||($2=="*" n){print $1}' "$man" | head -n1)"
  got="$(sha256sum "$dest/$base" 2>/dev/null | awk '{print $1}')"
  if [ -z "$want" ] || [ "$got" != "$want" ]; then
    warn "镜像站的 $base 哈希对不上清单，已丢弃"
    rm -f "$dest/$base"
    return 0
  fi
  if [ "$(pacman -Qp "$dest/$base" 2>/dev/null | awk 'NR==1{print $1}')" != "$p" ]; then
    warn "镜像站的 $base 包名对不上（要的是 $p），已丢弃"
    rm -f "$dest/$base"
    return 0
  fi
  pkg_is_downgrade "$dest/$base" "$p" && { dim "已装版本比镜像包新，跳过 $p"; rm -f "$dest/$base"; return 0; }
  printf '%s' "$dest/$base"
  return 0
}

# 三层兜底入口。needed=1 补装缺失；needed=0 强制重装「记录在、命令没了」。
# 全部满足才返回 0，否则 1（调用方负责把缺的包名报给用户）。
pacman_rescue_install() {
  local needed="${1:-1}"; shift
  local -a names=("$@") files=() left=()
  local p f tmp=""
  [ ${#names[@]} -gt 0 ] || return 1

  # ① 本机 pacman 缓存
  for p in "${names[@]}"; do
    f="$(pacman_cache_file "$p")"
    [ -n "$f" ] && files+=("$f")
  done
  if [ ${#files[@]} -gt 0 ] && pacman_u_batch "$needed" "${files[@]}"; then
    dim "已用本机 pacman 缓存装上 ${#files[@]} 个包"
  fi
  left=(); files=()
  for p in "${names[@]}"; do dep_present "$p" || left+=("$p"); done
  [ ${#left[@]} -eq 0 ] && return 0
  names=("${left[@]}")

  # ② 发布包自带离线包（哈希清单 + 包名回读 + 防降级）
  for p in "${names[@]}"; do
    f="$(offline_bundle_file "$p")"
    [ -n "$f" ] && files+=("$f")
  done
  if [ ${#files[@]} -gt 0 ] && pacman_u_batch "$needed" "${files[@]}"; then
    dim "已用发布包自带离线包装上 ${#files[@]} 个包"
  fi
  left=(); files=()
  for p in "${names[@]}"; do dep_present "$p" || left+=("$p"); done
  [ ${#left[@]} -eq 0 ] && return 0
  names=("${left[@]}")

  # ③ 镜像站 deps/
  if need_cmd curl; then
    tmp="$(mktemp -d)"
    for p in "${names[@]}"; do
      f="$(mirror_fetch_pkg "$p" "$tmp")"
      [ -n "$f" ] && files+=("$f")
    done
    if [ ${#files[@]} -gt 0 ] && pacman_u_batch "$needed" "${files[@]}"; then
      dim "已用镜像站离线包装上 ${#files[@]} 个包"
    fi
    rm -rf "$tmp"
  else
    dim "没有 curl，跳过镜像站这一层"
  fi
  left=()
  for p in "${names[@]}"; do dep_present "$p" || left+=("$p"); done
  [ ${#left[@]} -eq 0 ] && return 0
  return 1
}

deps_intro() {
  local miss=()
  dep_scan_missing miss

  section "系统控件"
  local st
  for p in "${DEPS_PACMAN[@]}" "${DEPS_AUR_FALLBACK[0]}"; do
    if dep_present "$p"; then st="${C_OK}已装${C_END}"; else st="${C_WARN}待装${C_END}"; fi
    case "$p" in
      ydotool)          table_row "$p" "$st  模拟鼠标/键盘（需 ydotoold）" ;;
      kdotool)          table_row "kdotool" "$st  按窗口名查找并聚焦（AUR）" ;;
      wtype)            table_row "$p" "$st  Wayland 打字，ydotool 不支持中文时兜底" ;;
      espeak-ng)        table_row "$p" "$st  espeak-ng TTS 兜底" ;;
      fakeroot)         table_row "$p" "$st  makepkg 编 AUR（kdotool）" ;;
      spectacle)        table_row "$p" "$st  截图（tools.py / depcheck）" ;;
      libpulse)         table_row "$p" "$st  pactl（音量）" ;;
      pipewire)         table_row "$p" "$st  pw-record（录音兜底 / pw-play 播放）" ;;
      pulseaudio-utils) table_row "$p" "$st  parec（唤醒词 / 录音首选）" ;;
      wireplumber)      table_row "$p" "$st  wpctl（音量，优先 pactl）" ;;
      gstreamer)        table_row "$p" "$st  gst-launch-1.0（抓帧）" ;;
      gst-plugin-pipewire) table_row "$p" "$st  pipewiresrc（游戏模式抓帧）" ;;
      gst-plugins-base) table_row "$p" "$st  pngenc / videoconvert" ;;
      gst-plugins-good) table_row "$p" "$st  jpegenc / videoscale" ;;
      tmux)             table_row "$p" "$st  命令面板 / Agent 终端" ;;
      qt6-tools)        table_row "$p" "$st  qdbus6 写剪贴板（中文输入）" ;;
    esac
  done
  echo
  [ ${#miss[@]} -gt 0 ] && dim "待安装：${miss[*]}（AUR 项需 paru/yay）"
  if [ -d "$OFFLINE_DIR" ]; then
    local n; n=$(find "$OFFLINE_DIR" -maxdepth 1 -name '*.pkg.tar.zst' 2>/dev/null | wc -l)
    if [ "${n:-0}" -gt 0 ]; then
      dim "包内自带离线依赖 ${n} 个（已逐个校验哈希），优先本地安装；装不上再依次退到本机缓存、镜像站、联网"
      offline_check_fresh
    fi
  fi
  dim "安装系统包需 sudo 解除 SteamOS 根分区只读，不改动游戏与个人文件。"
  dim "ydotool 会注册用户级 ydotoold 守护进程并开机自启，无需手动配置。"
}

# 卸载只有一个方向（删），终端里默认 yes；无终端一律视为取消。
# DECKMEIDO_ASSUME_YES=1 仅给自动化测试用。
ask_yes() {
  local ans=""
  if [ "${DECKMEIDO_ASSUME_YES:-0}" = "1" ]; then
    dim "（测试模式）视为确认：$1"
    return 0
  fi
  if [ -t 0 ]; then
    printf '%s[?]%s %s [Y/n] ' "$C_WARN" "$C_END" "$1"
    read -r ans || return 1
  elif { exec 3<>/dev/tty; } 2>/dev/null; then
    printf '%s[?]%s %s [Y/n] ' "$C_WARN" "$C_END" "$1" >&3
    read -r ans <&3 2>/dev/null || ans=""
    exec 3<&- 3>&-
  else
    dim "没有可用的终端，无法确认，按取消处理"
    return 1
  fi
  case "$ans" in
    y|Y|yes|YES) return 0 ;;
    "") return 0 ;;
    *) return 1 ;;
  esac
}

# 掌机友好的确认：$1=问题，$2=默认值（y / n），$3=非交互时默认动作的说明
# Deck 在游戏模式下用屏幕键盘，A 键等价于回车。提示里把两种操作都写清楚，
# 不再出现「按任意键」这种没人能按的句子；非交互时直接取默认值并说明。
ask_yn() {
  local q="$1" def="${2:-n}" ans="" tag="[y/N]" yes_label="${3:-安装}"
  [ "$def" = "y" ] && tag="[Y/n]"
  if [ "${DECKMEIDO_NONINTERACTIVE:-0}" = "1" ] || { [ ! -t 0 ] && [ ! -r /dev/tty ]; }; then
    if [ "$def" = "y" ]; then
      dim "非交互，默认${yes_label}"
      return 0
    fi
    dim "非交互，默认跳过"
    return 1
  fi
  # 提示必须走 stderr：stdout 经 tee 进安装日志，提示留在屏幕上等回车时不会归行，
  # 几项连着问就会在日志里挤成一整行，事后完全看不出哪句是问题、用户答了什么。
  # 问完再补一行「回答：x」，日志里每一项都是独立可读的两行。
  if [ -t 0 ]; then
    printf '%s[?]%s %s %s（掌机：A 键=回车，默认 %s）' \
      "$C_WARN" "$C_END" "$q" "$tag" "$def" >&2
    read -r ans || ans=""
  elif { exec 3<>/dev/tty; } 2>/dev/null; then
    printf '%s[?]%s %s %s（掌机：A 键=回车，默认 %s）' \
      "$C_WARN" "$C_END" "$q" "$tag" "$def" >&3
    read -r ans <&3 2>/dev/null || ans=""
    exec 3<&- 3>&-
  else
    dim "没有可用的终端，按默认值处理"
    ans="$def"
  fi
  if [ -z "$ans" ]; then
    case "$def" in
      y|Y) dim "  ${q}回答：回车（取默认：是）" ;;
      *)   dim "  ${q}回答：回车（取默认：否）" ;;
    esac
  else
    case "$ans" in
      y|Y|yes|YES) dim "  ${q}回答：是" ;;
      *)           dim "  ${q}回答：否" ;;
    esac
  fi
  ans=${ans:-$def}
  case "$ans" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

# 暂停等按键。必须用 read -n1：旧实现 `read -r _` 要求回车，导致「按 A 退不出」。
# 任意键继续；Ctrl-C 中断；stdin EOF 继续；DECKMEIDO_NONINTERACTIVE=1 直接过。
press_continue() {
  local hint="${1:-按 A 或回车继续}" key=""
  if [ "${DECKMEIDO_NONINTERACTIVE:-0}" = "1" ]; then
    dim "非交互，自动继续"
    return 0
  fi
  if [ -t 0 ]; then
    printf '%s[·]%s %s ' "$C_DIM" "$C_END" "$hint"
    IFS= read -r -n 1 -s key || true
    printf '\n'
  elif { exec 3<>/dev/tty; } 2>/dev/null; then
    printf '%s[·]%s %s ' "$C_DIM" "$C_END" "$hint" >&3
    IFS= read -r -n 1 -s key <&3 2>/dev/null || true
    printf '\n' >&3
    exec 3<&- 3>&-
  else
    dim "非交互，自动继续"
  fi
}

# ---------- 可选下载 ----------
# 所有要联网拉取的项集中在这里：默认全装，已存在的直接跳过并说明。

# 已装好的判定：$1 = venv python，$2 = 模块名
have_py_module() {
  [ -x "$1" ] && "$1" -c "import $2" >/dev/null 2>&1
}

# 音箱模式（智能音响）所需组件是否齐全：唤醒词模型 + 录音工具 + busctl。
deckmeido_speaker_ready() {
  local py="$INSTALL_DIR/.venv/bin/python"
  have_py_module "$py" "sherpa_onnx" || return 1
  [ -n "$(find "$DATA_MODELS/wake" -maxdepth 3 -name 'encoder*.onnx' -print -quit 2>/dev/null || true)" ] || return 1
  { need_cmd parec || need_cmd pw-record; } || return 1
  need_cmd busctl || return 1
  return 0
}

# 打印一条可选项：$1 名称，$2 用途，$3 体积，$4 已存在说明（空=不存在）。
# 默认全装；返回 0 = 要装，1 = 已存在，不用再装。
opt_download() {
  local name="$1" purpose="$2" size="$3" present="$4"
  if [ -n "$present" ]; then
    dim "$name：已存在（$present），跳过"
    return 1
  fi
  dim "$name：待安装（$purpose，$size）"
  return 0
}

optional_downloads() {
  local py="$INSTALL_DIR/.venv/bin/python"
  section "组件清单"
  dim "默认全装：能用到的组件全部装上；已存在的自动跳过，不会重复下载。"

  # 1. 服务端运行依赖
  # 末尾的 "|| true" 不能省：opt_download 对已存在的项返回 1，而这里是
  # set -e 下的函数最后一条命令，函数会以 1 结束，整个安装直接静默退出
  # （表现就是「检测到 venv 已装 fastapi → 打印跳过 → 安装结束」）。
  opt_download "Python 运行依赖（服务端）" "FastAPI/uvicorn 等，缺了后端跑不起来" \
    "$(mb_text "$(dep_mb server)")" \
    "$(have_py_module "$py" "fastapi" && echo "模块已在 venv 中" || true)" || true
  export DECKMEIDO_SKIP_SERVER=0

  # 2. 桌面客户端（PySide6 + WebEngine）
  if have_py_module "$py" "PySide6"; then
    opt_download "桌面客户端（PySide6）" "" "" "已存在" || true
    export DECKMEIDO_SKIP_GUI=1
  elif [ "${DECKMEIDO_SKIP_GUI:-}" = "1" ]; then
    dim "桌面客户端 PySide6：DECKMEIDO_SKIP_GUI=1，显式跳过"
  else
    opt_download "桌面客户端（PySide6）" "桌面窗口与内置浏览器，不要也能用网页版" \
      "$(mb_text "$(dep_mb gui)")" "" || true
    unset DECKMEIDO_SKIP_GUI
  fi

  # 3. 语音输入（sherpa-onnx + SenseVoice 模型）
  local stt_model="$DATA_MODELS/sensevoice/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/model.int8.onnx"
  opt_download "语音输入 STT" "sherpa-onnx 运行库 + SenseVoice 识别模型" \
    "$(mb_text "$(( $(dep_mb stt_mod) + $(dep_mb stt_model) ))")" \
    "$(have_py_module "$py" sherpa_onnx && [ -f "$stt_model" ] && echo "运行库与模型齐全" || true)" || true
  export DECKMEIDO_INSTALL_STT=1

  # 4. 语音合成（piper-plus + つくよみちゃん模型）
  local tts_model="$DATA_MODELS/tts/tsukuyomi/tsukuyomi-chan-6lang-fp16.onnx"
  opt_download "语音合成 TTS" "piper-plus 运行库 + つくよみちゃん模型" \
    "$(mb_text "$(( $(dep_mb tts_mod) + $(dep_mb tts_model) + $(dep_mb nltk) ))")" \
    "$(have_py_module "$py" piper_plus && [ -f "$tts_model" ] && echo "运行库与模型齐全" || true)" || true
  export DECKMEIDO_INSTALL_TTS=1

  # 5. 系统控件依赖（代替用户动手的 ydotool / wtype 等）
  if [ "${DECKMEIDO_NO_DEPS:-}" = "1" ]; then
    dim "系统控件依赖：DECKMEIDO_NO_DEPS=1，显式跳过"
    export DECKMEIDO_INSTALL_DEPS=0
  else
    local deps_present=""
    need_cmd ydotool && need_cmd wtype && deps_present="ydotool/wtype 已在 PATH"
    opt_download "系统控件依赖" "模拟点击/输入、截图等系统能力，需 sudo 走 pacman/AUR" \
      "$(mb_text "$(dep_mb sys)")" "$deps_present" || true
    export DECKMEIDO_INSTALL_DEPS=1
  fi

  # 6. 智能音响模式（音箱模式）组件
  if [ "${DECKMEIDO_NO_SPEAKER:-}" = "1" ]; then
    dim "智能音响模式：DECKMEIDO_NO_SPEAKER=1，显式跳过"
    export DECKMEIDO_INSTALL_SPEAKER=0
  else
    opt_download "智能音响模式（唤醒词）" "局域网语音音箱：唤醒词模型 + 分词依赖" \
      "$(mb_text "$(( $(dep_mb wake_mod) + $(dep_mb wake_model) ))")" \
      "$(deckmeido_speaker_ready && echo "组件齐全" || true)" || true
    export DECKMEIDO_INSTALL_SPEAKER=1
  fi

  # 7. Decky 插件包（已安装时由 install_plugin 自动替换）
  # 没装 Decky Loader 不等于「不装插件」：装好 zip 放过去，等用户装好 Decky
  # 后再一键部署——开箱即用不能因为前置框架缺失就静默放弃，那样用户永远不知道
  # 有这个面板。
  if decky_ready 2>/dev/null; then
    local pver=""
    pver="$(plugin_installed_version 2>/dev/null)" || pver=""
    if [ -n "$pver" ]; then
      dim "Decky 插件：已安装 v$pver，升级时会自动替换为新版本"
    else
      opt_download "Decky 插件包" "游戏模式 QAM 面板" \
        "$(mb_text "$(dep_mb plugin)")" "" || true
    fi
  fi
  export DECKMEIDO_INSTALL_PLUGIN=1
  # 显式关掉的项不能被上面的 export 1 覆盖，统一在这里收口
  [ "${DECKMEIDO_NO_SPEAKER:-}" = "1" ] && DECKMEIDO_INSTALL_SPEAKER=0
  [ "${DECKMEIDO_NO_DEPS:-}" = "1" ] && DECKMEIDO_INSTALL_DEPS=0
  return 0
}

# 读一行输入返回到 stdout。无默认值；curl|bash 时 stdin 是脚本本身，固定读 /dev/tty。
# 提示必须走 stderr/tty：stdout 只放答案，否则 choice=$(ask_choice) 会把提示一起捕获。
ask_choice() {
  local prompt="$1" ans="" tty_ok=0
  # 1) stdin 就是终端：正常交互
  if [ -t 0 ]; then
    printf '%s[?]%s %s' "$C_WARN" "$C_END" "$prompt" >&2
    read -r ans || return 1
    printf '%s' "$ans"
    return 0
  fi
  # 2) 脚本在磁盘上、stdin 是调用方给的管道（echo 1 | bash install.sh）
  if ! _is_piped; then
    printf '%s[?]%s %s' "$C_WARN" "$C_END" "$prompt" >&2
    if read -r ans 2>/dev/null; then
      printf '%s' "$ans"
      return 0
    fi
  fi
  # 3) curl | bash 或 stdin 没数据：读控制终端。
  #    先 exec 打开一次，失败时不会有 /dev/tty 报错糊屏。
  if { exec 3<>/dev/tty; } 2>/dev/null; then
    tty_ok=1
    printf '%s[?]%s %s' "$C_WARN" "$C_END" "$prompt" >&3
    if read -r ans <&3 2>/dev/null; then
      exec 3<&- 3>&-
      printf '%s' "$ans"
      return 0
    fi
  fi
  [ "$tty_ok" = 1 ] && exec 3<&- 3>&-
  return 1
}

sudo_warmup() {
  need_cmd sudo || { warn "没有 sudo，跳过系统依赖安装"; return 1; }
  # 已授权就直接用，别重复要密码
  sudo -n true 2>/dev/null && return 0
  # 密码必须从终端读：curl | bash 时 stdin 是脚本本身，sudo 从 stdin 读会直接失败。
  # 装系统包、写 Decky 插件目录都靠它，这里没拿到就一路静默失败。
  if (: < /dev/tty) 2>/dev/null; then
    info "安装系统依赖需要 root 权限，请输入密码"
    ( exec 0</dev/tty; sudo -v ) || { warn "sudo 授权失败，跳过系统依赖安装"; return 1; }
  elif [ -n "${SUDO_ASKPASS:-}" ] && [ -x "${SUDO_ASKPASS}" ]; then
    # 没有终端但有 askpass（SSH 远程装、自动化）：sudo 从 SUDO_ASKPASS 取密码
    info "没有终端，改用 SUDO_ASKPASS 获取 root 权限"
    sudo -A -v || { warn "sudo 授权失败，跳过系统依赖安装"; return 1; }
  else
    warn "当前没有终端，无法输入密码；系统依赖与 Decky 插件将跳过"
    dim "装完可补：bash $INSTALL_DIR/install.sh check --fix"
    return 1
  fi
  # 安装过程可能超过 sudo 默认的 5 分钟缓存，后台续期，避免中途再次要密码
  ( while kill -0 "$$" 2>/dev/null; do sudo -n true 2>/dev/null || exit; sleep 50; done ) &
  return 0
}

# 记下「这次真的由安装器装上」的系统包：只有缺包时才会进 todo，
# 用户原本就装好的、SteamOS 自带的都不入清单。卸载时按这份清单询问。
record_deps() {
  local p
  [ ${#@} -gt 0 ] || return 0
  mkdir -p "$DATA_DIR" 2>/dev/null || return 0
  for p in "$@"; do
    [ -n "$p" ] || continue
    grep -qxF -- "$p" "$DEP_RECORD" 2>/dev/null && continue
    printf '%s\n' "$p" >> "$DEP_RECORD" 2>/dev/null || true
  done
}

# 数据库同步失败（SteamOS 镜像 TLS 过期 / 断网）时 -Sy 必败，但包往往已在
# /var/cache/pacman/pkg：直接 pacman -U 即可，不依赖数据库也不联网。
# （2026-11-08 装机 ydotool/wtype 就卡在这里。）
pacman_cache_file() {
  local p="$1" f best=""
  for f in /var/cache/pacman/pkg/"$p"-*; do
    [ -f "$f" ] || continue
    case "$f" in
      *.pkg.tar.zst|*.pkg.tar.xz|*.pkg.tar.gz) ;;
      *) continue ;;
    esac
    # 前缀匹配会串到 kdotool-bin，必须回读包名确认
    [ "$(pacman -Qp "$f" 2>/dev/null | awk '{print $1}')" = "$p" ] || continue
    if [ -z "$best" ] || [ "$f" -nt "$best" ]; then best="$f"; fi
  done
  [ -n "$best" ] && printf '%s' "$best"
  return 0
}

install_pacman_deps() {
  local todo=() reinstall=() installed=()
  DEPS_MISSING_AFTER=()
  for p in "${DEPS_PACMAN[@]}"; do
    if pacman -Qq "$p" >/dev/null 2>&1; then
      # 记录在 ≠ 文件在：SteamOS 换镜像会重建 /usr，须再验命令
      if dep_present "$p"; then installed+=("$p"); else reinstall+=("$p"); fi
    else
      todo+=("$p")
    fi
  done
  [ ${#todo[@]} -gt 0 ] || [ ${#reinstall[@]} -gt 0 ] || {
    dim "pacman 包已齐全：${installed[*]}"; return 0; }
  if [ ${#reinstall[@]} -gt 0 ]; then
    warn "以下包记录在但命令找不到（镜像更新冲掉了 /usr），重装：${reinstall[*]}"
  fi
  [ ${#todo[@]} -gt 0 ] && info "用 pacman 安装：${todo[*]}"
  # SteamOS 根分区只读，装包前必须关；旧代码 `|| true` 吞错导致 pacman 报无关错
  if [ -f /etc/steamos-readonly.conf ] || need_cmd steamos-readonly; then
    if ! sudo steamos-readonly disable </dev/null 2>/dev/null; then
      warn "关闭 SteamOS 只读保护失败，系统包无法安装"
      dim "手动执行：sudo steamos-readonly disable   然后再重跑本次命令"
      return 1
    fi
    # 装完（含失败/Ctrl-C）必须恢复只读，trap 兜住
    trap 'sudo steamos-readonly enable >/dev/null 2>&1 || \
          printf "\033[33m⚠ 恢复 SteamOS 只读保护失败，请手动执行：sudo steamos-readonly enable\033[0m\n" >&2; \
          trap - EXIT INT TERM' EXIT INT TERM
  fi
  # -Sy 不 -Syu：避免顺带滚系统。reinstall 不能带 --needed
  local log ok=1 pkgs=()
  [ ${#todo[@]} -gt 0 ] && pkgs+=("${todo[@]}")

  # 离线优先，断网/仓库抽风时靠它
  local still=()
  if [ ${#pkgs[@]} -gt 0 ] && [ -d "$OFFLINE_DIR" ]; then
    if offline_pacman_install "${pkgs[@]}"; then
      local p
      for p in "${pkgs[@]}"; do dep_present "$p" || still+=("$p"); done
      if [ ${#still[@]} -eq 0 ]; then
        pkgs=()
      else
        warn "离线包装完仍缺：${still[*]}，回退联网"
        pkgs=("${still[@]}")
      fi
    fi
  fi

  if [ ${#pkgs[@]} -gt 0 ]; then
    if log=$(sudo pacman -Sy --needed --noconfirm "${pkgs[@]}" </dev/null 2>&1); then
      info "pacman 安装完成"
    else
      # 最常见失败：换镜像后 keyring 对不上；修不好不中止（后面有离线兜底）
      warn "pacman 安装失败，尝试修复 keyring 后重试"
      dim "$(printf '%s' "$log" | tail -5)"
      sudo pacman-key --init </dev/null >/dev/null 2>&1 || warn "pacman-key --init 失败"
      sudo pacman-key --populate archlinux holo </dev/null >/dev/null 2>&1 \
        || warn "pacman-key --populate 失败（keyring 可能损坏，离线包也不一定能验签）"
      if log=$(sudo pacman -Sy --needed --noconfirm "${pkgs[@]}" </dev/null 2>&1); then
        info "修复 keyring 后安装成功"
      else
        # 数据库同步失败（镜像 TLS 过期/断网）时 -Sy 必败，走三层兜底
        if pacman_rescue_install 1 "${pkgs[@]}"; then
          info "数据库同步失败，已改用离线包安装"
        else
          warn "pacman 安装仍然失败，三层离线兜底也凑不齐：${pkgs[*]}"
          dim "$(printf '%s' "$log" | tail -20)"
          dim "手动排查：sudo pacman -Sy ${pkgs[*]}"
          ok=0
        fi
      fi
    fi
  fi
  if [ ${#reinstall[@]} -gt 0 ] && [ "$ok" = 1 ]; then
    if sudo pacman -S --noconfirm "${reinstall[@]}" </dev/null >/dev/null 2>&1; then
      info "重装完成：${reinstall[*]}"
    elif pacman_rescue_install 0 "${reinstall[@]}"; then
      info "改用离线包重装完成：${reinstall[*]}"
    else
      warn "重装失败：${reinstall[*]}"
      dim "手动排查：sudo pacman -S ${reinstall[*]}"
    fi
  fi
  # 装完实测，防「pacman 说装了但命令不在」
  local bad=()
  for p in "${todo[@]}" "${reinstall[@]}"; do dep_present "$p" || bad+=("$p"); done
  # qdbus6 被拆包到 qt5-tools 的发行版：按需补装
  if printf '%s\n' "${bad[@]}" | grep -qx 'qt6-tools'; then
    if sudo pacman -Sy --needed --noconfirm qt5-tools </dev/null >/dev/null 2>&1 \
       && dep_present qt6-tools; then
      info "qdbus6 来自 qt5-tools，已补装"
      bad=("${bad[@]/qt6-tools/}"); bad=(${bad[@]+"${bad[@]}"})
    fi
  fi
  if [ ${#bad[@]} -gt 0 ]; then
    warn "以下包装完仍找不到命令，可能是镜像更新冲掉了 /usr：${bad[*]}"
    dim "重跑同一条安装命令即可补上：bash install.sh install"
    DEPS_MISSING_AFTER+=("${bad[@]}")
  fi
  # 只记本次新装的（重装的老包不算安装器引入）
  local done_now=()
  for p in "${todo[@]}"; do dep_present "$p" && done_now+=("$p"); done
  [ ${#done_now[@]} -gt 0 ] && record_deps "${done_now[@]}"
  record_deps_build
  [ ${#bad[@]} -eq 0 ] || return 1
}

install_aur_deps() {
  local todo=() helper
  need_cmd kdotool || todo=("${DEPS_AUR[@]}")
  [ ${#todo[@]} -gt 0 ] || return 0
  helper=""
  for h in paru yay; do
    need_cmd "$h" && { helper="$h"; break; }
  done
  if [ -z "$helper" ]; then
    warn "kdotool 在 AUR 里，但没找到 paru / yay，跳过"
    dim "手动装法（推荐预编译版，不用编译）："
    dim "  git clone https://aur.archlinux.org/kdotool-bin.git && cd kdotool-bin && makepkg -si"
    dim "或先装个 AUR helper：sudo pacman -S --needed base-devel git && git clone https://aur.archlinux.org/paru-bin.git"
    return 0
  fi
  # 编译 AUR 包需要 fakeroot，缺了会在 makepkg 阶段直接失败
  if ! need_cmd fakeroot; then
    warn "缺少 fakeroot，AUR 编译会失败，先补装"
    # 必须喂 </dev/null：piped 安装时 stdin 还挂着外层管道，sudo 会把脚本尾巴吃掉
    if ! sudo pacman -S --needed --noconfirm fakeroot </dev/null >/dev/null 2>&1; then
      warn "补装 fakeroot 失败，AUR 编译大概率会失败（稍后会给出手动命令）"
    fi
  fi
  info "用 $helper 安装：${todo[*]}"
  local log
  if log=$("$helper" -S --needed --noconfirm "${todo[@]}" 2>&1); then
    info "$helper 安装完成"
  else
    warn "$helper 安装失败，改用源码版（需要编译，耗时较久）：${DEPS_AUR_FALLBACK[*]}"
    dim "$(printf '%s' "$log" | tail -8)"
    if log=$("$helper" -S --needed --noconfirm "${DEPS_AUR_FALLBACK[@]}" 2>&1); then
      info "$helper 安装源码版完成"
    else
      warn "$helper 安装失败："
      dim "$(printf '%s' "$log" | tail -15)"
      dim "手动装法：git clone https://aur.archlinux.org/kdotool-bin.git && cd kdotool-bin && makepkg -si"
    fi
  fi
  # AUR 包也属于「安装器装上的」，但只在确认落地后记录
  if need_cmd kdotool; then
    if pacman -Qq kdotool-bin >/dev/null 2>&1; then
      record_deps kdotool-bin
    elif pacman -Qq kdotool >/dev/null 2>&1; then
      record_deps kdotool
    fi
  fi
}

# ydotoold 是 ydotool 的守护进程，不跑的话点击/输入用不了。
# 注意：不能用 `ydotoold &` 裸跑——那种进程重启就没了，用户会以为装了却不能用。
# 包自带 /usr/lib/systemd/user/ydotool.service（Restart=always），交给 systemd 托管。
setup_ydotoold() {
  need_cmd ydotool || return 0
  if ! need_cmd systemctl; then
    # 没有 systemd 的极端情况才退回裸跑
    pgrep -x ydotoold >/dev/null 2>&1 && return 0
    setsid ydotoold >/dev/null 2>&1 < /dev/null &
    sleep 1
    pgrep -x ydotoold >/dev/null 2>&1 \
      && dim "ydotoold 已启动（无 systemd，重启后需手动再启动）" \
      || warn "ydotoold 启动失败，模拟点击/输入可能不可用"
    return 0
  fi
  # 已有裸跑进程时先停掉，否则 systemd 抢占 socket 会失败
  if pgrep -x ydotoold >/dev/null 2>&1 && \
     ! systemctl --user is-active ydotool.service >/dev/null 2>&1; then
    dim "停掉手动启动的 ydotoold，改由 systemd 托管"
    pkill -x ydotoold 2>/dev/null || true
    sleep 1
    rm -f "/run/user/$(id -u)/.ydotool_socket"
  fi
  # 注意：is-enabled 对 static/indirect 等状态也返回 0，必须比对输出字符串，
  # 否则会把「没设自启」误判成已设好
  local en ac
  en=$(systemctl --user is-enabled ydotool.service 2>/dev/null || true)
  ac=$(systemctl --user is-active  ydotool.service 2>/dev/null || true)
  if [ "$en" = enabled ] && [ "$ac" = active ]; then
    dim "ydotoold 已由 systemd 托管并开机自启"
    return 0
  fi
  if [ "$en" != enabled ]; then
    info "设置 ydotoold 开机自启（ydotool.service，之前：${en:-未知}）"
    systemctl --user enable ydotool.service >/dev/null 2>&1 || \
      warn "enable ydotool.service 失败，重启后可能要手动启动"
  fi
  # 已 enabled 但没跑（手动 stop 过 / 刚开机还没拉起）时也要拉起来
  if [ "$ac" != active ]; then
    info "启动 ydotool 守护进程（之前：${ac:-未知}）"
    systemctl --user start ydotool.service >/dev/null 2>&1 || true
    sleep 1
  fi
  if systemctl --user is-active ydotool.service >/dev/null 2>&1; then
    dim "ydotoold 已由 systemd 托管，重启后自动恢复"
  else
    warn "ydotool.service 没起来，模拟点击/输入可能不可用"
    dim "手动排查：systemctl --user status ydotool.service"
  fi
}

# 记下这次装依赖时的 SteamOS 镜像号，下次镜像变动就能提醒用户重装
record_deps_build() {
  mkdir -p "$CONFIG_DIR"
  printf '%s\n' "${BUILD_ID:-unknown}" > "$CONFIG_DIR/.deps-build-id" 2>/dev/null || true
}

# ---------- 控件自检 ----------
# 只查 pacman -Q 不够：SteamOS 换镜像会冲掉 /usr，数据库仍有记录，须实测。
probe_ydotool() {
  need_cmd ydotool || { echo "未安装（模拟鼠标点击、键盘输入）"; return 1; }
  need_cmd ydotoold || { echo "守护进程缺失，需重装 ydotool"; return 1; }
  local sock="/run/user/$(id -u)/.ydotool_socket"
  [ -S "$sock" ] || { echo "ydotoold 没在跑（$sock 不存在），点击/输入会失败"; return 1; }
  # 只 connect 验活，不发命令（会真移动光标）。ydotoold 用 SOCK_DGRAM，用 STREAM 会报错
  if python3 - "$sock" <<'PY' >/dev/null 2>&1
import socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
s.settimeout(1.0)
try:
    s.connect(sys.argv[1])
finally:
    s.close()
PY
  then
    return 0
  fi
  echo "socket 通信失败（ydotoold 可能在重启中）"; return 1
}
# ydotoold 的托管状态：enabled 决定重启后还在不在，active 决定现在能不能用
ydotoold_state() {
  if ! need_cmd systemctl; then
    pgrep -x ydotoold >/dev/null 2>&1 && echo "运行中（无 systemd，重启需手动）" || echo "未运行"
    return 0
  fi
  local en ac
  en=$(systemctl --user is-enabled ydotool.service 2>/dev/null || echo unknown)
  ac=$(systemctl --user is-active  ydotool.service 2>/dev/null || echo unknown)
  if [ "$ac" = active ] && [ "$en" = enabled ]; then
    echo "运行中 · 已设开机自启"
  elif [ "$ac" = active ]; then
    echo "运行中 · 未设开机自启（重启会失效）"
  elif [ "$en" = enabled ]; then
    echo "已设开机自启 · 当前未运行（手动 stop 过，或刚开机还没拉起）"
  else
    echo "未运行 · 未设开机自启"
  fi
}
probe_kdotool() {
  need_cmd kdotool || { echo "未安装（按窗口名聚焦）"; return 1; }
  KDE_SESSION_VERSION=6 kdotool getactivewindow >/dev/null 2>&1 && return 0
  echo "调用失败（需要 KDE Plasma 6 会话）"; return 1
}
probe_wtype() {
  need_cmd wtype || { echo "未安装（Wayland 原生打字）"; return 1; }
  wtype >/dev/null 2>&1
  # 无参数时 wtype 返回 1 并打印用法；返回 127/命令不存在才是真坏
  [ $? -ne 127 ] && return 0
  echo "命令无法执行"; return 1
}
probe_espeak() {
  need_cmd espeak-ng || { echo "未安装（语音播报）"; return 1; }
  espeak-ng --version >/dev/null 2>&1 && return 0
  echo "调用失败"; return 1
}
probe_fakeroot() {
  need_cmd fakeroot || { echo "未安装（编译 AUR 包需要）"; return 1; }
  fakeroot true >/dev/null 2>&1 && return 0
  echo "调用失败"; return 1
}
probe_spectacle() {
  need_cmd spectacle || { echo "未安装（KWin 截图，KDE 一般自带）"; return 1; }
  # 必须 offscreen：无 DISPLAY 时 Qt6 会 abort 并 core dump。用 --version 避开 GUI 初始化
  QT_QPA_PLATFORM=offscreen DISPLAY= WAYLAND_DISPLAY= \
    spectacle --version >/dev/null 2>&1 || { echo "调用异常（可能在无图形环境下崩溃）"; return 1; }
  return 0
}

# 返回缺失/异常的控件名列表（空格分隔）
deps_broken() {
  local broken=() f
  for f in probe_ydotool probe_kdotool probe_wtype probe_espeak probe_fakeroot probe_spectacle; do
    "$f" >/dev/null 2>&1 || broken+=("${f#probe_}")
  done
  echo "${broken[*]:-}"
}

# 装完实测复核 + ydotoold 托管状态；缺的写进 DEPS_PROBE_FAILED
deps_verified() {
  DEPS_PROBE_FAILED=()
  local miss=()
  dep_scan_missing miss
  DEPS_PROBE_FAILED+=("${miss[@]}")
  # ydotool 在但 ydotool.service 未 enable → 重启后点击/输入失效
  if need_cmd ydotool; then
    setup_ydotoold >/dev/null 2>&1 || true
    [ "$(systemctl --user is-enabled ydotool.service 2>/dev/null || echo unknown)" = enabled ] || \
      DEPS_PROBE_FAILED+=("ydotoold 未设开机自启")
  fi
  [ ${#DEPS_PROBE_FAILED[@]} -eq 0 ]
}

do_check() {
  local fix=0
  [ "${1:-}" = "--fix" ] && fix=1
  check_user
  info "控制类控件自检"
  local broken=() name
  for name in ydotool kdotool wtype espeak fakeroot spectacle; do
    local msg
    if msg=$("probe_$name" 2>&1); then
      printf '  %s● OK %s %-10s %s\n' "$C_OK" "$C_END" "$name" "$(probe_label "$name")"
    else
      printf '  %s○ BAD%s %-10s %s\n' "$C_ERR" "$C_END" "$name" "$msg"
      broken+=("$name")
    fi
  done
  # 无专用 probe 的包统一走 dep_present（含 gst 元素级判定）
  local p c
  for p in "${DEPS_PACMAN[@]}" "${DEPS_AUR_FALLBACK[0]}"; do
    case "$p" in
      ydotool|kdotool|wtype|espeak-ng|fakeroot|spectacle) continue ;;
    esac
    c="$(dep_cmd "$p")"
    if dep_present "$p"; then
      printf '  %s● OK %s %-14s %s\n' "$C_OK" "$C_END" "$c" "$p"
    else
      printf '  %s○ BAD%s %-14s 缺 %s\n' "$C_ERR" "$C_END" "$c" "$p"
      broken+=("$p")
    fi
  done
  echo
  # ydotoold 托管状态决定「重启后还能不能用」
  need_cmd ydotool && { printf '  %-10s %s\n' "ydotoold" "$(ydotoold_state)"; echo; }
  if [ ${#broken[@]} -eq 0 ]; then
    info "全部正常"
    return 0
  fi
  warn "异常项：${broken[*]}"
  if [ "$fix" = 1 ]; then
    info "尝试自动修复"
    sudo_warmup || return 0
    install_pacman_deps
    install_aur_deps
    setup_ydotoold
    echo
    do_check
  else
    dim "自动修复：bash install.sh check --fix"
  fi
}

probe_label() {
  case "$1" in
    ydotool)  echo "模拟鼠标点击 / 键盘输入" ;;
    kdotool)  echo "按窗口标题聚焦窗口" ;;
    wtype)    echo "Wayland 原生打字（备选）" ;;
    espeak)   echo "语音播报回复" ;;
    fakeroot) echo "编译 AUR 包的构建依赖" ;;
    spectacle) echo "KWin 截图" ;;
  esac
}

# 镜像更新后自动补齐：SteamOS 换镜像会冲掉 /usr 下 pacman 装的包
# （wtype 就这么消失过一次）。已有 sudo 授权时不必再问用户，直接重装。
maybe_autorepair_after_image_update() {
  [ -n "$BUILD_ID" ] || return 0
  local stamp="$CONFIG_DIR/.deps-build-id"
  [ -f "$stamp" ] || return 0
  local prev; prev="$(cat "$stamp" 2>/dev/null || true)"
  [ "$prev" = "$BUILD_ID" ] && return 0
  info "检测到 SteamOS 镜像已更新（${prev:-未知} → $BUILD_ID）"
  dim "镜像更新会重建 /usr，之前装的控件可能已被冲掉，现在重新检查一遍"
  local broken; broken="$(deps_broken)"
  if [ -z "$broken" ]; then
    dim "实测控件都还在，无需重装"
    record_deps_build
    return 0
  fi
  warn "以下控件已被冲掉：$broken"
  sudo_warmup || return 0
  install_pacman_deps
  install_aur_deps
  setup_ydotoold
  record_deps_build
  local left; left="$(deps_broken)"
  if [ -n "$left" ]; then
    warn "重装后仍不可用：$left"
    dim "逐个看原因：bash install.sh check"
  else
    info "控件已全部恢复"
  fi
}

install_deps() {
  info "检查可选系统依赖（模拟点击/输入、截图、语音播报）"
  # 拿不到 sudo 时返回 2（跳过），不能返回 0：否则调用方会照旧打「✓ 已处理」，
  # 用户进设置页却发现控件全缺——和「装上了但没用」是同一类假成功。
  sudo_warmup || return 2
  local pk_rc=0
  install_pacman_deps || pk_rc=1
  install_aur_deps
  setup_ydotoold
  # 装完跑一遍实测，把「记录在、命令没了」的情况直接暴露出来
  local broken; broken="$(deps_broken)"
  if [ -n "$broken" ]; then
    warn "以下控件装完仍不可用：$broken"
    dim "逐个看原因：bash install.sh check"
  fi
  system_notes
  [ "$pk_rc" = 0 ] || return 1
  [ -z "$broken" ] || return 1
  return 0
}

# ---------- 代码同步 ----------
# 反向对照：安装目录里有、源码里已无的代码文件要删，否则废弃模块会一直堆积。
# 只处理 CODE_DIRS，*.log / VERSION / 缓存不动；deploy/deps 必须放过，
# 否则升级会清空离线包。
CODE_DIRS=(server client assets deploy tools plugin)
USER_DATA_RE='\.log$|(^|/)VERSION$|(^|/)__pycache__(/|$)|^deploy/deps/|(^|/)static/shots/[^/]+$'

prune_stale() {
  [ -d "$INSTALL_DIR" ] || return 0
  local removed=0 rel d
  for d in "${CODE_DIRS[@]}"; do
    [ -d "$INSTALL_DIR/$d" ] || continue
    # 只在安装目录里扫，逐个比对源码里是否有同名文件
    while IFS= read -r rel; do
      [ -n "$rel" ] || continue
      printf '%s' "$rel" | grep -Eq "$USER_DATA_RE" && continue
      [ -e "$HERE/$rel" ] && continue
      rm -f "$INSTALL_DIR/$rel" && removed=$((removed + 1))
    done < <(cd "$INSTALL_DIR" && find "$d" -type f 2>/dev/null)
  done
  # 删完留下的空目录一并收掉（仅代码目录内）
  for d in "${CODE_DIRS[@]}"; do
    [ -d "$INSTALL_DIR/$d" ] || continue
    find "$INSTALL_DIR/$d" -mindepth 1 -type d -empty -delete 2>/dev/null || true
  done
  # updater 成功会自删 update-backup-*；进程被杀才残留，只留最新一份。
  # 文件名可能带空格，不能用 $(ls) 分词。
  local old
  while IFS= read -r old; do
    [ -n "$old" ] && rm -f "$old"
  done < <(find "$INSTALL_DIR" -maxdepth 1 -name 'update-backup-*.tar.gz' -type f \
             -printf '%T@ %p\n' 2>/dev/null | sort -rn | tail -n +2 | cut -d' ' -f2-)
  [ "$removed" -gt 0 ] && dim "清理了 $removed 个新版本中已不存在的旧文件"
  return 0
}

sync_files() {
  [ -f "$HERE/install.sh" ] || die "当前目录不像 DeckMeido 源码目录（缺 install.sh）"
  if [ "$HERE" = "$(cd "$INSTALL_DIR" 2>/dev/null && pwd -P)" ]; then
    info "已在使用目录 $INSTALL_DIR"
    # 热更新只换文件不重跑安装流程，这里也必须补权限，否则 launch.sh 会残留 0644
    heal_exec_bits "$INSTALL_DIR"
    write_version_file
    return
  fi
  if [ -d "$INSTALL_DIR" ]; then
    info "同步代码到 $INSTALL_DIR"
    prune_stale
  else
    info "安装到 $INSTALL_DIR"
  fi
  mkdir -p "$INSTALL_DIR"
  local items=(server client assets deploy tools plugin requirements.txt requirements-gui.txt requirements-stt.txt requirements-tts.txt README.md install.sh)
  # RELEASE-NOTES.md 只在发布包里存在，有就一起带上，方便用户在安装目录里查更新内容
  [ -f "$HERE/RELEASE-NOTES.md" ] && items+=(RELEASE-NOTES.md)
  # git clone 出来的源码里没有 plugin/dist/（插件自己的 .gitignore 排除），
  # 仓库里带的是 deploy/deckmeido-plugin.zip 这份 build 产物——有它就够了，
  # 插件是按 zip 解包的，不需要 dist。两者都缺时才想办法现构建。
  local build_plugin=0
  [ -f "$HERE/deploy/deckmeido-plugin.zip" ] || [ -f "$HERE/plugin/dist/index.js" ] || build_plugin=1
  if [ "$build_plugin" = 1 ] && command -v npm >/dev/null 2>&1; then
    dim "源码里既没插件包也没有前端产物，先用 npm 构建（首次要装 node_modules）"
    if ( cd "$HERE/plugin" && { [ -d node_modules ] || npm install --no-audit --no-fund >/dev/null 2>&1; } &&
         npm run build >/dev/null 2>&1 ); then
      dim "插件前端已构建：plugin/dist/index.js"
    else
      warn "插件前端构建失败；Decky 插件这轮装不上，可稍后手动 bash $INSTALL_DIR/plugin/deploy.sh"
    fi
  elif [ "$build_plugin" = 1 ]; then
    warn "源码里既没插件包也没有前端产物，机上也没有 npm（用发布包安装可避免）"
  fi
  for it in "${items[@]}"; do
    [ -e "$HERE/$it" ] || continue
    if [ -d "$HERE/$it" ]; then
      # userdata/ 是用户数据根，永远不从源码包覆盖
      rsync -a --exclude 'userdata' --exclude '__pycache__' --exclude '*.log' \
            "$HERE/$it/" "$INSTALL_DIR/$it/" 2>/dev/null ||
        cp -r "$HERE/$it/." "$INSTALL_DIR/$it/"
    else
      cp -f "$HERE/$it" "$INSTALL_DIR/$it"
    fi
  done
  heal_exec_bits "$INSTALL_DIR"
  write_version_file
}

# pip 源：国内默认走镜像，PySide6 这类大包从官方 PyPI 拉经常超时。
# 装之前会实测每个源的下载速率，从最快的开始；失败再按顺序换下一个。
PIP_INDEXES=(
  "https://pypi.tuna.tsinghua.edu.cn/simple"
  "https://mirrors.aliyun.com/pypi/simple"
  "https://mirrors.ustc.edu.cn/pypi/simple"
  "https://pypi.org/simple"
)

# 探最快 pip 源，返回索引；全不通返回 -1。不用 ping：ICMP 只证明能到，
# 与 HTTPS 下载速率无关且多数源禁 ICMP。直接拉同一段真实文件实测速率。
PROBE_BYTES=65536      # 每源取 64KB，够分辨快慢又不费流量
PROBE_MAX=4            # 单个源最多等 4 秒

# 返回「字节/秒」，失败返回 0
probe_speed() {
  local url="$1" out
  out=$(curl -sS -L -m "$PROBE_MAX" -r 0-$((PROBE_BYTES - 1)) \
        -o /dev/null -w '%{speed_download} %{size_download}' \
        "${url%/}/" 2>/dev/null) || return 1
  local spd size
  spd=${out%% *}
  size=${out##* }
  size=${size%%.*}
  # 只回了几个字节的说明是错误页，不算数
  if [ -n "$size" ] && [ "$size" -lt 1024 ] 2>/dev/null; then
    printf '0'
    return 0
  fi
  spd=${spd%%.*}
  printf '%s' "${spd:-0}"
}

# 容量预估时实测过网速（est_net_rate 调的就是本函数，结果表见 _PIP_SPD）。
# 同一进程里已经测过的源速率记在 _PIP_SPD 里（声明在容量预估那一节），
# 紧接着装依赖时直接复用，不再重复测一遍白等十几秒。
pick_pip_index() {
  local i n=${#PIP_INDEXES[@]} idx=0 best=0 v url
  need_cmd curl || { printf '%s' 0; return 0; }
  # 注意：本函数靠 stdout 返回下标，所有提示必须走 stderr，否则会污染返回值
  if [ "$n" -gt 1 ] && [ "${#_PIP_SPD[@]}" = 0 ]; then
    dim "测速中（各源取 ${PROBE_BYTES} 字节，最多 ${PROBE_MAX}s）" >&2
  fi
  for i in "${!PIP_INDEXES[@]}"; do
    # 用 url 中转：_PIP_SPD 是关联数组，但历史 bash 会把 $_PIP_SPD[...] 里的 [0] 当
    # 普通数组下标解析，下标是 URL 时直接报 arithmetic syntax error
    url=${PIP_INDEXES[$i]}
    if [ -z "${_PIP_SPD[$url]:-}" ]; then
      _PIP_SPD[$url]=$(probe_speed "$url" || echo 0)
      dim "$(human_rate "${_PIP_SPD[$url]}")  $url" >&2
    fi
    v=${_PIP_SPD[$url]}
    if [ "$v" -gt "$best" ]; then best=$v; idx=$i; fi
  done
  printf '%s' "$idx"
}

# 字节/秒 → 人看得懂的速率
human_rate() {
  local r="${1:-0}"
  if [ "$r" -ge 1048576 ]; then
    printf '%d.%dMB/s' $((r / 1048576)) $((r % 1048576 * 10 / 1048576))
  elif [ "$r" -ge 1024 ]; then
    printf '%dKB/s' $((r / 1024))
  elif [ "$r" -gt 0 ]; then
    printf '%dB/s' "$r"
  else
    printf '连不上'
  fi
}

# 秒 → 「1m23s」「45s」
human_dur() {
  local s="${1:-0}"
  if [ "$s" -ge 3600 ]; then
    printf '%dh%02dm' $((s / 3600)) $((s % 3600 / 60))
  elif [ "$s" -ge 60 ]; then
    printf '%dm%02ds' $((s / 60)) $((s % 60))
  else
    printf '%ds' "$s"
  fi
}

# 字节数 → 「1.2GB」「350MB」「11kB」这类友好体积。
# 纯 bash 取一位小数：以前走 awk，环境里 awk 缺失时会静默输出空串（表里整行空白）
human_size() {
  local b="${1:-0}" unit=1 label=B
  if [ "$b" -ge 1073741824 ]; then
    unit=1073741824; label=GB
  elif [ "$b" -ge 1048576 ]; then
    unit=1048576; label=MB
  elif [ "$b" -ge 1024 ]; then
    unit=1024; label=kB
  fi
  if [ "$unit" = 1 ]; then
    printf '%dB' "$b"
  else
    # 「776.0MB」这种 .0 不写出来，省一列也更自然
    local whole=$((b / unit)) frac=$((b % unit * 10 / unit))
    if [ "$frac" = 0 ]; then
      printf '%d%s' "$whole" "$label"
    else
      printf '%d.%d%s' "$whole" "$frac" "$label"
    fi
  fi
}

# 唤醒词分词依赖（sentencepiece / pypinyin）与 sherpa-onnx 同属语音输入模块。
# 必须独立判定：sherpa-onnx 在装时也可能漏它们，早退会漏装。
_wake_text_deps() {
  local py="$1" start="${2:-0}"
  [ "${DECKMEIDO_WAKE_MODE:-install}" = "install" ] || return 0
  [ -x "$py" ] || return 0
  have_py_module "$py" sentencepiece && have_py_module "$py" pypinyin && return 0
  phase "补装" "唤醒词分词 sentencepiece / pypinyin"
  if "$py" -m pip install -q sentencepiece pypinyin -i "${PIP_INDEXES[$start]}" >/dev/null 2>&1 \
  || "$py" -m pip install -q sentencepiece pypinyin >/dev/null 2>&1; then
    phase_ok "补装" "唤醒词分词"
  else
    phase_fail "补装" "sentencepiece / pypinyin 未装上，生成唤醒词会失败"
  fi
  return 0
}

# 语音输入依赖。$1 python，$2 起始源下标。
# 调用前 optional_downloads 已把 DECKMEIDO_INSTALL_STT 定为 1（默认全装）；
# 这里的 0 / ask 分支只是兜底，正常流程不会走到询问。
install_stt_deps() {
  local py="$1" start="${2:-0}"
  [ -x "$py" ] || return 0
  if "$py" -c "import sherpa_onnx" >/dev/null 2>&1; then
    phase_skip "安装" "语音输入依赖已装（sherpa-onnx）"
    _wake_text_deps "$py" "$start"
    return 0
  fi
  local mode="${DECKMEIDO_INSTALL_STT:-ask}"
  case "$mode" in
    1|yes|on|true) ;;
    0|no|off|false)
      phase_skip "安装" "语音输入依赖（DECKMEIDO_INSTALL_STT=$mode）"
      return 0 ;;
    *)
      if [ ! -t 0 ] && [ ! -r /dev/tty ]; then
        phase_skip "安装" "非交互环境，跳过语音输入依赖"
        return 0
      fi
      if ! ask_yes "现在安装语音输入依赖（sherpa-onnx，约 $(mb_text "$(dep_mb stt_mod)")）？不装也能正常聊天，之后随时可补"; then
        phase_skip "安装" "语音输入依赖（重跑 install.sh 可补装）"
        return 0
      fi
      ;;
  esac
  if pip_install_with_switch "$py" requirements-stt.txt "语音输入依赖（sherpa-onnx）" "$start"; then
    _wake_text_deps "$py" "$start"
    return 0
  fi
  phase_fail "安装" "语音输入依赖未装上，重跑 install.sh 可补装"
  return 0
}

# 语音合成依赖。$1 python，$2 起始源下标。
# piper-plus 2.x 要求 Python 3.11+，版本不够直接跳过并说明原因。
# 调用前 DECKMEIDO_INSTALL_TTS 已被定为 1；0 / ask 分支只是兜底。
install_tts_deps() {
  local py="$1" start="${2:-0}"
  [ -x "$py" ] || return 0
  if "$py" -c "import piper_plus" >/dev/null 2>&1; then
    phase_skip "安装" "语音合成依赖已装（piper-plus）"
    install_tts_nltk_data "$py"
    return 0
  fi
  # 版本不够时硬装只会得到「找不到匹配版本」的报错，先判断再决定
  if ! "$py" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    local pv
    pv=$("$py" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "未知")
    phase_skip "安装" "语音合成依赖（piper-plus 需 Python 3.11+，当前 $pv）"
    return 0
  fi
  local mode="${DECKMEIDO_INSTALL_TTS:-ask}"
  case "$mode" in
    1|yes|on|true) ;;
    0|no|off|false)
      phase_skip "安装" "语音合成依赖（DECKMEIDO_INSTALL_TTS=$mode）"
      return 0 ;;
    *)
      if [ "${DECKMEIDO_NONINTERACTIVE:-0}" = "1" ] || { [ ! -t 0 ] && [ ! -r /dev/tty ]; }; then
        phase_skip "安装" "非交互环境，跳过语音合成依赖"
        return 0
      fi
      if ! ask_yes "现在安装语音合成依赖（piper-plus + onnxruntime，约 $(mb_text "$(dep_mb tts_mod)")）？不装也能正常聊天，之后随时可补"; then
        phase_skip "安装" "语音合成依赖（重跑 install.sh 可补装）"
        return 0
      fi
      ;;
  esac
  if pip_install_with_switch "$py" requirements-tts.txt "语音合成依赖（piper-plus）" "$start"; then
    # 依赖装上 ≠ 音色可用：模型与试合成统一由 [6/8] 复查负责，这里只报运行库。
    install_tts_nltk_data "$py"
    return 0
  fi
  phase_fail "安装" "语音合成依赖未装上，重跑 install.sh 可补装"
  return 0
}

# nltk cmudict+averaged_perceptron 须在安装期落到 $DATA_MODELS/nltk_data；运行时联网会被 SSRF 拦。
# 事故：Deck 代理 DNS 回 198.18.x，nltk.download 被自身 SSRF 保护拒（目录空），英文段合成失败。
# 故 downloader 失败后用 curl 直取 zip 绕过 nltk 网络层，最后按目录校验。
install_tts_nltk_data() {
  local py="$1" dir="$DATA_MODELS/nltk_data"
  [ -x "$py" ] || return 0
  if nltk_data_ok "$dir"; then
    phase_skip "安装" "英文语音数据（nltk_data）"
    return 0
  fi
  # 容量与「在 GitHub、慢时换源」并入标签，交给 _retry_download 统一打进度行，
  # 免得同一件事先「下载」说一遍、再「下载 第 1/3 次」说第二遍。
  if _retry_download "英文语音数据（nltk_data），约 $(mb_text "$(dep_mb nltk)")" _install_tts_nltk_once "$py" && nltk_data_ok "$dir"; then
    phase_ok "安装" "英文语音数据（nltk_data）"
  else
    phase_fail "安装" "英文语音数据没下全（英文段会用 espeak-ng 兜底，重跑 install.sh 可再补）"
  fi
  return 0
}

# 单次尝试：先试 nltk 官方下载器，失败再按包 curl 直取。返回 0 表示数据已齐。
_install_tts_nltk_once() {
  local py="$1" dir="$DATA_MODELS/nltk_data"
  mkdir -p "$dir/corpora" "$dir/taggers" 2>/dev/null || {
    warn "无法创建 $dir，英文段只能用 espeak-ng 兜底"
    return 0
  }
  local out
  out=$("$py" - "$dir" <<'PY' 2>&1
import sys
target = sys.argv[1]
try:
    import nltk
except Exception as exc:
    print(f"SKIP {type(exc).__name__}: {exc}")
    raise SystemExit(0)
ok_all = True
for pkg in ("cmudict", "averaged_perceptron_tagger", "averaged_perceptron_tagger_eng"):
    try:
        ok = nltk.download(pkg, download_dir=target, quiet=True)
        print(("OK " if ok else "FAIL ") + pkg)
    except Exception as exc:
        ok_all = False
        print(f"FAIL {pkg}: {type(exc).__name__}: {exc}")
raise SystemExit(0 if ok_all else 1)
PY
  ) || true
  # 逐包结果压成一行：nltk 的异常里带多行堆栈，原样打出来会把日志切成一段段
  # 错位的碎片（上一份日志里就是 10 行乱缩进 + 中间夹 FAIL cmudict）。
  local summary
  summary=$(printf '%s\n' "$out" | grep -E '^(OK|FAIL|SKIP)' | cut -c1-120 | tr '\n' ';' | sed 's/;$//')
  if nltk_data_ok "$dir"; then
    return 0
  fi
  phase "下载" "官方下载器未成功（代理/镜像网络下常见），改用 curl 直取数据包"
  local item sub pkg
  for item in "corpora cmudict" "taggers averaged_perceptron_tagger" "taggers averaged_perceptron_tagger_eng"; do
    sub="${item%% *}"
    pkg="${item#* }"
    [ -d "$dir/$sub/$pkg" ] && continue
    fetch_nltk_package "$py" "$dir/$sub" "$sub" "$pkg" || true
  done
  nltk_data_ok "$dir" && return 0
  return 1
}

# 下载器输出里人能看懂的一句原因：优先「不可用 / 失败 / 中断」这类自带说明的行，
# 再去掉 Python 的 Traceback、代码行和异常类名，只留冒号后面的人话。
_dl_reason() {
  local line
  line=$(printf '%s\n' "$1" \
    | grep -E 'TTSError|不可用|失败|中断|不足|超时|timed out|HTTP|URLError|连接' \
    | grep -v '^[[:space:]]*\(File\|raise \|from \|import \|for \|if \|else\|try\|with\|def \)' \
    | tail -n 1)
  [ -n "$line" ] || line=$(printf '%s\n' "$1" | grep -v '^[[:space:]]*$' | tail -n 1)
  # 去掉异常类名，长句截断：屏幕上只要能看出「哪儿坏了」
  line=$(printf '%s' "$line" \
    | sed -E 's/^[a-zA-Z_][a-zA-Z0-9_.]*(\.[a-zA-Z_][a-zA-Z0-9_.]*)*: //; s/^[[:space:]]+//')
  printf '%s' "$(printf '%s' "$line" | cut -c1-96)"
}

# 安装期所有下载都走这里：最多 3 次，间隔 2s / 4s，逐次打印第几次和失败原因。
# 子进程输出落临时日志、不吞掉：每 DL_TICK 秒透传一行「已收 / 速度 / 已用」，
# 连续 DL_HEARTBEAT 秒没新字节就补一行心跳——以前整段静默，用户以为卡死
# （实测月读酱模型出过 5 分钟无任何输出）。失败原因留在 DOWNLOAD_ERR 供收尾汇总。
# 用法：_retry_download "月读酱模型，约 1.2GB" <命令> [参数...]
# 单次上限 5 分钟：慢到 5 分钟还没下完的源，换一次源比继续等更划算；
# 三次都失败会提示改 DECKMEIDO_MIRROR，而不是让用户干等 45 分钟。
DOWNLOAD_TIMEOUT=300   # 单次尝试硬上限（秒）
DL_TICK=10             # 进度行刷新间隔（秒）：每秒一行会刷屏
DL_HEARTBEAT=45        # 完全没新数据多久补一行「还在等」
_retry_download() {
  local label="$1"; shift
  local attempt=1 delay=2 log pid t0 now bytes last_bytes idle tick ret=0 spd
  DOWNLOAD_ATTEMPTS=0
  DOWNLOAD_ERR=""
  while [ "$attempt" -le 3 ]; do
    DOWNLOAD_ATTEMPTS=$attempt
    log=$(mktemp "${TMPDIR:-/tmp}/deckmeido-dl.XXXXXX" 2>/dev/null || echo "/tmp/deckmeido-dl.$$.$attempt")
    # 标签只在每轮开头出现一次；后面的重试行不再重复「下载」「第 N/3 次」两遍
    phase "下载" "$label  第 $attempt/3 次，单次最多 $((DOWNLOAD_TIMEOUT / 60)) 分钟"
    "$@" > "$log" 2>&1 &
    pid=$!
    t0=$SECONDS
    last_bytes=0
    idle=0
    tick=0
    timed_out=0
    # 前台每秒看一次日志长度：变了说明还在收数据；到点才打一行，不刷屏。
    while kill -0 "$pid" 2>/dev/null; do
      sleep 1
      now=$SECONDS
      bytes=$(wc -c < "$log" 2>/dev/null | tr -d ' ')
      bytes=${bytes:-0}
      tick=$((tick + 1))
      if [ "$bytes" -gt "$last_bytes" ]; then
        spd=$(((bytes - last_bytes) / (tick > 0 ? tick : 1)))
        last_bytes=$bytes
        idle=0
        if [ "$tick" -ge "$DL_TICK" ]; then
          tick=0
          if [ "$spd" -gt 0 ]; then
            phase "下载" "$label  已收 $(human_size "$bytes")  速度 $(human_rate "$spd")  已用 $(human_dur $((now - t0)))"
          else
            phase "下载" "$label  已收 $(human_size "$bytes")  正在解析内容  已用 $(human_dur $((now - t0)))"
          fi
        fi
      else
        idle=$((idle + 1))
        if [ "$idle" -ge "$DL_HEARTBEAT" ]; then
          phase "下载" "$label  已收 $(human_size "$bytes")  还在等数据（已用 $(human_dur $((now - t0)))）"
          idle=0
          tick=0
        fi
      fi
      if [ $((now - t0)) -ge "$DOWNLOAD_TIMEOUT" ]; then
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
        timed_out=1
        DOWNLOAD_ERR="超过 $((DOWNLOAD_TIMEOUT / 60)) 分钟没下完，已中止本次尝试"
        # 超时算一次失败，照样往下重试；三次都超时才给换源提示
        phase_fail "下载" "$label  第 $attempt/3 次超时"
        if [ "$attempt" -lt 3 ]; then
          phase "下载" "$label  ${delay}s 后重试"
          sleep "$delay"
          delay=$((delay * 2))
        fi
        attempt=$((attempt + 1))
        break
      fi
    done
    # set -e 下不能写 `[ ... ] && continue`：超时那次条件为假时整行返回 1，
    # 直接把函数带走（正常失败那次就永远走不到下面的重试）。
    if [ "$timed_out" = 1 ]; then
      rm -f "$log" 2>/dev/null || true
      continue
    fi
    ret=0
    wait "$pid" || ret=$?
    if [ "$ret" = 0 ]; then
      # 成功时不回放子命令输出：各下载器自己的格式和 phase 版式不一致，
      # 会把屏幕刷花；结论统一由调用方用 phase_ok 输出。
      rm -f "$log"
      return 0
    fi
    DOWNLOAD_ERR=$(_dl_reason "$(tail -n 40 "$log" 2>/dev/null)")
    [ -n "$DOWNLOAD_ERR" ] || DOWNLOAD_ERR="下载器没有输出就退出了（退出码 $ret）"
    rm -f "$log"
    phase_fail "下载" "$label  第 $attempt/3 次失败：$DOWNLOAD_ERR"
    if [ "$attempt" -lt 3 ]; then
      phase "下载" "$label  ${delay}s 后换源重试（多数情况下一两次就能接上）"
      sleep "$delay"
      delay=$((delay * 2))
    fi
    attempt=$((attempt + 1))
  done
  _download_give_up_hint
  return 1
}

# 三次都失败后唯一的出路：换个镜像源。只在这里打一次，避免每轮都重复。
_download_give_up_hint() {
  phase "下载" "换源：DECKMEIDO_MIRROR=<镜像地址>，或直接用当前源：${MIRROR:-未设置}"
}

# 英文数据是否齐（只看目录，与 server/tts.py 的 english_data_ready 同一判据）
nltk_data_ok() {
  local dir="$1"
  [ -d "$dir/corpora/cmudict" ] && [ -d "$dir/taggers/averaged_perceptron_tagger_eng" ]
}

# 下载并解压一个官方 nltk_data 包；nltk 自带下载器被 SSRF 挡住时走这条路。
# 源按顺序试：GitHub raw → jsDelivr → ghfast 代理。$2 = 目标分类目录，$3 = corpora/taggers
fetch_nltk_package() {
  local py="$1" dest="$2" sub="$3" pkg="$4" tmp="/tmp/deckmeido-nltk-$pkg.zip" base url
  for base in \
    "https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages" \
    "https://cdn.jsdelivr.net/gh/nltk/nltk_data@gh-pages/packages" \
    "https://ghfast.top/https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages"; do
    url="$base/$sub/$pkg.zip"
    if curl -fsSL --max-time 120 -o "$tmp" "$url" 2>/dev/null &&
       "$py" - "$tmp" "$dest" <<'PY' >/dev/null 2>&1
import sys, zipfile
zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])
PY
    then
      rm -f "$tmp"
      return 0
    fi
  done
  rm -f "$tmp"
  return 1
}

# 月读酱模型（onnx + config）是否都已落盘。只查存在性，内容校验交给 tts.py。
tts_model_present() {
  local root="$DATA_MODELS/tts/tsukuyomi"
  [ -f "$root/tsukuyomi-chan-6lang-fp16.onnx" ] && [ -f "$root/config.json" ]
}

# 真正合成一小段中文 + 一小段英文来判定「已就绪」，不再只看包在不在。
# 只在模型已经落盘之后调用；模型没下就是真失败（调用方一定先跑过下载阶段）。
verify_tts() {
  local py="$1"
  local root="$DATA_MODELS/tts/tsukuyomi"
  local onnx="$root/tsukuyomi-chan-6lang-fp16.onnx" cfg="$root/config.json"
  [ -x "$py" ] || return 1
  if [ ! -f "$onnx" ] || [ ! -f "$cfg" ]; then
    TTS_MODEL_STATE="missing"
    TTS_MODEL_ERROR="模型文件不在盘上"
    return 1
  fi
  local out
  # 管道内的 2>&1 已把 Python 的 stderr（onnxruntime / JPCommonLabel 告警）
  # 收进 $out；这里的 2>/dev/null 是把结论行也丢掉，屏幕只由 stage_verify_all 出。
  out=$(NLTK_DATA="$DATA_MODELS/nltk_data" "$py" - "$onnx" "$cfg" <<'PY' 2>&1
import os, sys, tempfile, wave

onnx, cfg_path = sys.argv[1], sys.argv[2]
try:
    import nltk
    nltk.download = lambda *a, **k: True
    from piper_plus import PiperVoice
    voice = PiperVoice.load(onnx, config_path=cfg_path)
except Exception as exc:
    print(f"FAIL 加载模型失败：{type(exc).__name__}: {exc}")
    raise SystemExit(1)

mapping = getattr(getattr(voice, "config", None), "language_id_map", {}) or {}
results = {}
for lang, text in (("zh", "你好，这是语音自检。"),
                   ("en", "This is a short speech check.")):
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        kwargs = {"length_scale": 1.0, "noise_scale": 0.667}
        if isinstance(mapping.get(lang), int):
            kwargs["language_id"] = mapping[lang]
        with wave.open(path, "wb") as wav:
            try:
                voice.synthesize(text, wav, **kwargs)
            except TypeError:
                voice.synthesize(text, wav)
        size = os.path.getsize(path)
        results[lang] = size if size > 10000 else 0
    except Exception as exc:
        results[lang] = 0
        print(f"FAIL {lang}: {type(exc).__name__}: {exc}")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

ok = True
for lang, label in (("zh", "中文"), ("en", "英文")):
    size = results.get(lang)
    if isinstance(size, int) and size > 10000:
        print(f"OK {label} {size} 字节")
    else:
        ok = False
        print(f"FAIL {label} 合成结果为空")
raise SystemExit(0 if ok else 1)
PY
  ) 2>/dev/null || {
    # 结论统一由 stage_verify_all 用 step_fail 输出，这里只回填状态和原因
    TTS_MODEL_STATE="broken"
    TTS_MODEL_ERROR="$(printf '%s\n' "$out" | grep -E '^(FAIL|WARNING)' | head -1 | cut -c1-90)"
    return 1
  }
  TTS_MODEL_STATE="ready"
  TTS_MODEL_ERROR=""
  return 0
}

# 建 venv + 装依赖。$1 = gui 只装桌面客户端，stt / tts 只补对应可选依赖，空=全装。
# requirements 分三份：服务端（必装）、gui（PySide6，失败只警告，保留网页版可用）、
# stt / tts（可选能力）。拆开的目的是 PySide6 超时不会让整个安装失败。
venv_install() {
  local only_gui="${1:-}"
  local py="$INSTALL_DIR/.venv/bin/python" idx=0 start=0
  if [ ! -x "$py" ]; then
    info "创建虚拟环境 $INSTALL_DIR/.venv"
    python3 -m venv "$INSTALL_DIR/.venv"
  fi
  if [ -n "${DECKMEIDO_PIP_INDEX:-}" ]; then
    PIP_INDEXES=("$DECKMEIDO_PIP_INDEX")
    dim "使用指定的 pip 源：$DECKMEIDO_PIP_INDEX"
  else
    idx=$(pick_pip_index)
    if [ "$idx" = "-1" ]; then
      idx=0
      warn "没测到可用的 pip 源，按默认顺序逐个尝试"
    else
      dim "选最快的源：${PIP_INDEXES[$idx]}"
      start="$idx"
    fi
  fi
  "$py" -m pip install --upgrade pip -q -i "${PIP_INDEXES[$start]}" >/dev/null 2>&1 ||
    "$py" -m pip install --upgrade pip -q >/dev/null 2>&1 || true

  if [ "$only_gui" = "stt" ]; then
    # 只补语音依赖：别顺手重装服务端/桌面客户端
    install_stt_deps "$py" "$start"
    return 0
  fi

  if [ "$only_gui" = "tts" ]; then
    # 只补语音合成依赖，同样不碰服务端/桌面客户端
    install_tts_deps "$py" "$start"
    return 0
  fi

  if [ "$only_gui" != "gui" ]; then
    if [ "${DECKMEIDO_SKIP_SERVER:-0}" = "1" ]; then
      phase_skip "安装" "服务端依赖（DECKMEIDO_SKIP_SERVER=1）"
    else
      if ! pip_install_with_switch "$py" requirements.txt "服务端依赖" "$start"; then
        phase_fail "安装" "服务端依赖没装全，重跑 install.sh 可补齐"
        dim "手动指定源：DECKMEIDO_PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple bash install.sh upgrade"
        return 1
      fi
    fi
    # 语音输入是可选能力：单独询问，失败不影响主流程
    install_stt_deps "$py" "$start"
    # 语音合成同理，且会先检查 Python 3.11+ 再决定是否安装
    install_tts_deps "$py" "$start"
  fi

  if [ -n "${DECKMEIDO_SKIP_GUI:-}" ]; then
    phase_skip "安装" "桌面客户端（DECKMEIDO_SKIP_GUI=1）"
    return 0
  fi
  if pip_install_with_switch "$py" requirements-gui.txt "桌面客户端（PySide6）" "$start"; then
    return 0
  fi
  phase_fail "安装" "桌面客户端（PySide6）未装上：网页版仍可用，重跑 install.sh 可补装"
  return 0
}

# 模块：系统控件（清单见 DEPS_PACMAN / DEPS_AUR_FALLBACK）
# $1=1 本次要装；其它只打「跳过」。只负责安装，复查由 stage_verify_all 统一做。
module_sys() {
  local want="${1:-0}" miss=()
  mod_begin "系统控件（pacman / AUR）"
  if [ "$want" != "1" ]; then
    phase_skip "检测" "本次未选择系统控件"
    return 0
  fi
  dep_scan_missing miss
  if [ ${#miss[@]} -eq 0 ]; then
    phase_ok "检测" "全部已装"
    phase_skip "下载" "无需下载"
    phase_skip "安装" "无需重复安装"
    return 0
  fi
  phase "检测" "缺：${miss[*]}"
  phase "下载" "从 pacman / AUR 取包，约 $(mb_text "$(dep_mb sys)")"
  deps_intro
  maybe_autorepair_after_image_update
  local _drc=0
  install_deps || _drc=$?
  if [ "$_drc" = 2 ]; then
    phase_fail "安装" "没拿到 sudo 权限，一个都没装"
    DEPS_FAILED=1
    return 1
  fi
  phase_ok "安装" "pacman / AUR 已处理"
  # 装完只回填实际结果，结论留给全局复查
  deps_verified || { DEPS_FAILED=1; return 1; }
  return 0
}

# 检查 venv 里各功能所需的 Python 模块是否真能 import；缺的追加到 $2 数组。
# 调用方常写 _py_dep_check "$py" miss，nameref 取名 _missing 才不会和调用方的
# 变量同名——同名时 bash 会抱「循环的名称引用」警告，且赋值行为未定义。
_py_dep_check() {
  local py="$1"
  local -n _missing="$2"
  have_py_module "$py" fastapi || _missing+=("fastapi")
  [ "${DECKMEIDO_STT_MODE:-install}" = "install" ] && { have_py_module "$py" sherpa_onnx || _missing+=("sherpa-onnx"); }
  [ "${DECKMEIDO_TTS_MODE:-install}" = "install" ] && { have_py_module "$py" piper_plus  || _missing+=("piper-plus"); }
  [ "${DECKMEIDO_WAKE_MODE:-install}" = "install" ] && {
    have_py_module "$py" sentencepiece || _missing+=("sentencepiece")
    have_py_module "$py" pypinyin || _missing+=("pypinyin")
  }
  [ "${DECKMEIDO_SKIP_GUI:-0}" != "1" ] && { have_py_module "$py" PySide6 || _missing+=("PySide6"); }
  return 0
}

# 模块：桌面客户端（PySide6）——只在 install --gui / upgrade --gui 时单独走
module_gui() {
  local py="$1"
  mod_begin "桌面客户端（PySide6）"
  if have_py_module "$py" PySide6; then
    phase_ok "检测" "已装"
    phase_skip "下载" "无需下载"
    phase_skip "安装" "无需重复安装"
  else
    phase "检测" "缺 PySide6"
    phase "下载" "PySide6 + WebEngine，约 $(mb_text "$(dep_mb gui)")"
    if ! venv_install gui; then
      phase_fail "安装" "PySide6 没装上"
      phase_fail "复查" "桌面窗口不可用"
      retry_hint
      return 1
    fi
    phase_ok "安装" "PySide6 已装"
  fi
  if have_py_module "$py" PySide6; then
    phase_ok "复查" "PySide6 可导入（占用 $(mb_text "$(path_mb "$INSTALL_DIR/.venv")")）"
    return 0
  fi
  phase_fail "复查" "PySide6 导入失败"
  retry_hint
  return 1
}

# ---------- 全局三阶段：检测 → 安装 → 复查 ----------
# 全流程只跑一次，各扫全部依赖。禁止在模块内单独复查：
# 装一个查一个会让「缺 parec」这类结论拖到安装结束后才出现，届时已无法补装。
DETECT_MISS=()      # 人类可读的缺失项（检测阶段填充）
DETECT_NEED_SYS=0   # 系统包有缺
DETECT_NEED_PY=0    # Python 模块有缺
DETECT_NEED_PY_NAMES=()
DETECT_NEED_STT_MODEL=0
DETECT_NEED_TTS_MODEL=0
DETECT_NEED_TTS_DATA=0
DETECT_NEED_WAKE_MODEL=0
DETECT_NEED_WAKE_TEXT=0

# 音箱可用的常驻录音通道：parec（pulse）或 pw-record（pipewire）
audio_recorder_present() { need_cmd parec || need_cmd pw-record; }

stage_detect_all() {
  local py="$1" want_deps="${2:-}" miss=()
  DETECT_MISS=(); DETECT_NEED_SYS=0; DETECT_NEED_PY=0; DETECT_NEED_PY_NAMES=()
  DETECT_NEED_STT_MODEL=0; DETECT_NEED_TTS_MODEL=0; DETECT_NEED_TTS_DATA=0
  DETECT_NEED_WAKE_MODEL=0; DETECT_NEED_WAKE_TEXT=0

  # 1) 系统包（含 gst 元素级判定）
  if [ -n "$want_deps" ]; then
    dep_scan_missing miss
    if [ ${#miss[@]} -gt 0 ]; then
      DETECT_NEED_SYS=1
      DETECT_MISS+=("系统控件：${miss[*]}")
    fi
  fi
  # 2) 音箱的常驻录音通道也归系统层，和 ydotool 一样属于「必须有才能用」
  if [ "${DECKMEIDO_WAKE_MODE:-install}" = "install" ]; then
    if ! audio_recorder_present; then
      DETECT_NEED_SYS=1
      DETECT_MISS+=("parec / pw-record（音箱常驻录音）")
    fi
    need_cmd busctl || { DETECT_NEED_SYS=1; DETECT_MISS+=("busctl（音箱音量与媒体控制）"); }
  fi
  # 3) Python 模块：一次 import 判定，不再逐个模块各查一遍
  _py_dep_check "$py" DETECT_NEED_PY_NAMES
  if [ ${#DETECT_NEED_PY_NAMES[@]} -gt 0 ]; then
    DETECT_NEED_PY=1
    DETECT_MISS+=("Python 模块：${DETECT_NEED_PY_NAMES[*]}")
  fi
  # 4) 模型与训练数据
  if [ "${DECKMEIDO_STT_MODE:-install}" = "install" ]; then
    [ -f "$(stt_model_file)" ] || { DETECT_NEED_STT_MODEL=1; DETECT_MISS+=("SenseVoice 识别模型"); }
  fi
  if [ "${DECKMEIDO_TTS_MODE:-install}" = "install" ]; then
    tts_model_present || { DETECT_NEED_TTS_MODEL=1; DETECT_MISS+=("月读酱合成模型"); }
    nltk_data_ok "$DATA_MODELS/nltk_data" || { DETECT_NEED_TTS_DATA=1; DETECT_MISS+=("英文语音数据 nltk_data"); }
  fi
  if [ "${DECKMEIDO_WAKE_MODE:-install}" = "install" ]; then
    wake_model_present || { DETECT_NEED_WAKE_MODEL=1; DETECT_MISS+=("唤醒词模型 KWS+VAD"); }
    if ! have_py_module "$py" sentencepiece || ! have_py_module "$py" pypinyin; then
      DETECT_NEED_WAKE_TEXT=1
      DETECT_MISS+=("唤醒词分词 sentencepiece/pypinyin")
    fi
  fi

  if [ ${#DETECT_MISS[@]} -eq 0 ]; then
    step_ok "全部依赖已就位，无需安装"
  else
    step_note "待处理 ${#DETECT_MISS[@]} 项："
    local m; for m in "${DETECT_MISS[@]}"; do step_note "  - $m"; done
  fi
  return 0
}

# 按检测结果一次装完。中途只报进度，不做复查（复查属 [6/8]）。
stage_install_all() {
  local py="$1" want_deps="${2:-}" rc=0
  # 系统层：包（含 parec/pw-record、busctl）一次装完
  if [ "$DETECT_NEED_SYS" = 1 ]; then
    module_sys "$want_deps" || rc=1
  fi
  # Python 运行依赖：服务端 + STT + TTS + 唤醒词分词 + PySide6，一次装完。
  # 分词依赖由 venv_install 内部补，模型下载必须在它之后。
  if [ "$DETECT_NEED_PY" = 1 ] || [ "$DETECT_NEED_WAKE_TEXT" = 1 ]; then
    mod_begin "程序运行依赖（服务端 / 语音 / 桌面客户端）"
    # 各子项（服务端/语音输入/语音合成/桌面客户端）自己打 phase 结论，这里只收结果
    if ! venv_install "" 0; then
      rc=1
    fi
  fi
  # 模型：依赖全部就绪后再下，避免「包没装就下模型」
  [ "$DETECT_NEED_STT_MODEL" = 1 ] && { module_stt_download "$py" || rc=1; }
  [ "$DETECT_NEED_TTS_MODEL" = 1 ] && { module_tts_download "$py" || rc=1; }
  [ "$DETECT_NEED_WAKE_MODEL" = 1 ] && { module_wake_download "$py" || rc=1; }
  # nltk 数据要在 TTS 试合成之前，属于「安装」而非「复查」
  [ "$DETECT_NEED_TTS_DATA" = 1 ] && { install_tts_nltk_data "$py" || true; }
  return "$rc"
}

# 只查不装：任何补装动作都不允许出现在这里。
stage_verify_all() {
  local py="$1" want_deps="${2:-}" bad=0
  # 系统控件：只读 deps_verified 的结论，不在这里另写一套判定
  if [ -n "$want_deps" ]; then
    if deps_verified; then
      step_ok "系统控件全部就位"
    else
      step_fail "系统控件未就位：${DEPS_PROBE_FAILED[*]}"; bad=1
    fi
  fi
  # 音箱录音通道
  if [ "${DECKMEIDO_WAKE_MODE:-install}" = "install" ]; then
    if audio_recorder_present; then step_ok "音箱录音通道（parec / pw-record）"
    else step_fail "音箱录音通道缺失：parec 与 pw-record 都不可用"; bad=1; fi
  fi
  # Python 模块逐个 import
  local miss=()
  _py_dep_check "$py" miss
  if [ ${#miss[@]} -eq 0 ]; then step_ok "Python 模块均可导入"
  else step_fail "Python 模块导入失败：${miss[*]}"; bad=1; fi
  # 语音三链只在对应依赖已导入时才有意义，缺依赖不必再跑一遍 Python 栈回溯
  local py_ok=0
  [ ${#miss[@]} -eq 0 ] && py_ok=1
  # 模型真实加载 / 试合成（模型不在盘上时直接判失败，不进 Python 避免栈回溯）
  if [ "${DECKMEIDO_STT_MODE:-install}" = "install" ] && { [ "$py_ok" = 1 ] || have_py_module "$py" sherpa_onnx; }; then
    if [ ! -f "$(stt_model_file)" ]; then
      step_fail "语音识别：模型未就位"; STT_MODEL_STATE="failed"; bad=1
    elif ( cd "$INSTALL_DIR" && "$py" - <<'PY' >/dev/null 2>&1
from server import paths
from server.stt import STT
STT(paths.stt_model_dir())
PY
    ); then step_ok "语音识别：识别器加载通过"; STT_MODEL_STATE="ready"
    else step_fail "语音识别：识别器加载失败"; STT_MODEL_STATE="failed"; bad=1; fi
  fi
  if [ "${DECKMEIDO_TTS_MODE:-install}" = "install" ] && { [ "$py_ok" = 1 ] || have_py_module "$py" piper_plus; }; then
    if ! tts_model_present; then
      step_fail "语音合成：月读酱模型未就位"
      TTS_MODEL_STATE="missing"; TTS_MODEL_ERROR="模型未下载"; bad=1
    elif verify_tts "$py"; then
      step_ok "语音合成：中英试合成通过"
    else
      step_fail "语音合成：试合成未通过${TTS_MODEL_ERROR:+（${TTS_MODEL_ERROR}）}"; bad=1
    fi
  fi
  if [ "${DECKMEIDO_WAKE_MODE:-install}" = "install" ] && { [ "$py_ok" = 1 ] || have_py_module "$py" sentencepiece; }; then
    if ! wake_model_present; then
      step_fail "智能音响：唤醒词模型未就位"; bad=1
    elif ( cd "$INSTALL_DIR" && "$py" - <<'PY' >/dev/null 2>&1
from server import paths, wake
wake.ensure_model(paths.wake_model_dir())
PY
    ); then step_ok "智能音响：唤醒词模型加载通过"
    else step_fail "智能音响：唤醒词模型加载失败"; bad=1; fi
  fi
  # 音箱模式的音乐播控/点歌要另装 Decky Music，这不属于安装失败，
  # 只在复查阶段引导一次，免得用户以为装完就能点歌。
  if [ "${DECKMEIDO_WAKE_MODE:-install}" = "install" ]; then
    if decky_music_present; then
      step_ok "音箱模式播控/点歌：Decky Music 插件已就位"
    elif decky_ready 2>/dev/null; then
      step_note "音箱模式播控/点歌还需 Decky Music 插件，装法："
      step_note "  1) 游戏模式按 ··· 打开 Decky 面板，进「商店」"
      step_note "  2) 搜索 Decky Music 并安装"
      step_note "  3) 回到面板给 Decky Music 填上你的音乐账号"
    else
      step_note "音箱模式播控/点歌需要两层插件，装法："
      step_note "  1) 先装 Decky Loader：桌面模式运行"
      step_note "     curl -L https://github.com/SteamDeckHomebrew/decky-installer/releases/latest/download/install_release.sh | sh"
      step_note "  2) 游戏模式按 ··· 打开 Decky 面板，进「商店」搜 Decky Music 并安装"
      step_note "  3) 回到面板给 Decky Music 填上你的音乐账号"
    fi
  fi
  return "$bad"
}

# ---------- 语音模型：就位判定 + 纯下载 ----------
# 本段只提供「模型在不在」的判定与「下载解包」，检测与复查统一由全局三阶段负责。
TTS_MODEL_STATE="${TTS_MODEL_STATE:-}"

stt_model_file() {
  printf '%s/sensevoice/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/model.int8.onnx' "$DATA_MODELS"
}

wake_model_present() {
  [ -n "$(find "$DATA_MODELS/wake" -maxdepth 3 -name 'encoder*.onnx' -print -quit 2>/dev/null || true)" ]
}

# 以下三个只做「下载 + 解包」，不含检测与复查（由全局三阶段负责）。
module_stt_download() {
  local py="$INSTALL_DIR/.venv/bin/python"
  mod_begin "语音识别模型（SenseVoice）"
  mkdir -p "$DATA_MODELS" 2>/dev/null || true
  if ! _retry_download "SenseVoice 模型，约 $(mb_text "$(dep_mb stt_model)")" _prefetch_stt_once; then
    # 失败原因已由 _retry_download 逐次打印，这里只回填状态给收尾汇总
    STT_MODEL_STATE="failed"
    return 1
  fi
  phase_ok "解包" "SenseVoice 模型已就绪（$DATA_MODELS/sensevoice）"
  return 0
}

module_tts_download() {
  local py="$INSTALL_DIR/.venv/bin/python"
  mod_begin "语音合成模型（月读酱）"
  mkdir -p "$DATA_MODELS" 2>/dev/null || true
  if ! _retry_download "つくよみちゃん模型，约 $(mb_text "$(dep_mb tts_model)")" _prefetch_tts_once; then
    # 失败原因已由 _retry_download 逐次打印，这里只回填状态给收尾汇总
    TTS_MODEL_STATE="failed"
    TTS_MODEL_ERROR="$DOWNLOAD_ERR"
    return 1
  fi
  phase_ok "解包" "つくよみちゃん模型已就绪（$DATA_MODELS/tts/tsukuyomi）"
  return 0
}

module_wake_download() {
  local py="$INSTALL_DIR/.venv/bin/python"
  mod_begin "唤醒词模型（KWS + VAD）"
  mkdir -p "$DATA_MODELS" 2>/dev/null || true
  if ! _retry_download "KWS + silero VAD，约 $(mb_text "$(dep_mb wake_model)")" _prefetch_wake_once; then
    # 失败原因已由 _retry_download 逐次打印
    return 1
  fi
  phase_ok "解包" "唤醒词模型已就绪（$DATA_MODELS/wake）"
  return 0
}

# 单个模型的下载动作（由 _retry_download 负责重试与报错）。
_prefetch_stt_once() {
  local py="$INSTALL_DIR/.venv/bin/python"
  ( cd "$INSTALL_DIR" && "$py" - <<'PY'
from server import paths
from server.stt import STT

last = [-1]

def progress(done, total, phase, file):
    if not total or total <= 0:
        return
    pct = int(done * 100 / total)
    if pct // 10 != last[0] // 10:
        last[0] = pct
        print(f"    {phase} {pct}%", flush=True)

STT(paths.stt_model_dir()).download_model(progress=progress)
PY
  )
}

_prefetch_tts_once() {
  local py="$INSTALL_DIR/.venv/bin/python"
  ( cd "$INSTALL_DIR" && "$py" - <<'PY'
from server import paths
from server.tts import TTS

last = [-1]

def progress(done, total, phase, file=None):
    # 每 10% 一行，与 _prefetch_stt_once 一致（_retry_download 靠它判断「还在动」）
    if not total or total <= 0:
        return
    pct = int(done * 100 / total)
    if pct // 10 != last[0] // 10:
        last[0] = pct
        print(f"    {phase} {pct}%", flush=True)

tts = TTS(enable=False, model_root=paths.tts_model_dir())
tts.download_model(progress=progress)
PY
  )
}

_prefetch_wake_once() {
  local py="$INSTALL_DIR/.venv/bin/python"
  ( cd "$INSTALL_DIR" && "$py" - <<'PY'
from server import paths, wake

wake.ensure_model(paths.wake_model_dir())
PY
  )
}

# ---------- 带进度条与换源的 pip install ----------
#   $1 python，$2 requirements 文件名，$3 中文名（屏幕上只显示它，不显示文件名），$4 起始源下标（默认 0）
# pip 下大包时久不吐输出，旧版 -q 让人以为卡在选源。改为日志后台跑，
# 前台每秒重画：当前包 / 已下载 / 总量 / 速度 / 剩余时间。
pip_install_with_switch() {
  local py="$1" req="$2" label="${3:-$2}" start="${4:-0}"
  local n=${#PIP_INDEXES[@]} i k ok=0 log="$INSTALL_DIR/pip-install.log"
  local ret=0 pid t0 cur got total rate prev_got base_got=0
  # wheel 先落 pip 缓存再装进 venv：下载期 venv 不增长，必须两者体积之和。
  # 缓存固定放 PIP_CACHE_DIR，装完由 purge_pip_cache 一次删掉，不碰用户自己的 ~/.cache/pip
  local cache_dir="$PIP_CACHE_DIR"
  export PIP_CACHE_DIR="$cache_dir"
  mkdir -p "$cache_dir" 2>/dev/null || true
  # 进度按「本次新增」算，否则百分比从 60% 起步
  base_got=$(du -sb "$INSTALL_DIR/.venv" "$cache_dir" 2>/dev/null | awk '{s += $1} END {print s+0}')
  base_got=${base_got:-0}
  for ((i = 0; i < n; i++)); do
    k=$(((start + i) % n))
    if [ "$i" -gt 0 ]; then dim "换源重试：${PIP_INDEXES[$k]}"; fi
    # 进度自绘；老 pip 不认 off 且可能仍输出颜色，一并关掉
    PIP_PROGRESS_OPTS=(--progress-bar off --no-color)
    "$py" -m pip install -r "$INSTALL_DIR/$req" \
      "${PIP_PROGRESS_OPTS[@]}" --timeout 60 --retries 3 \
      -i "${PIP_INDEXES[$k]}" > "$log" 2>&1 &
    pid=$!
    t0=$SECONDS
    ret=0
    prev_got=0
    while kill -0 "$pid" 2>/dev/null; do
      # 已下载字节：venv + pip 缓存 的落盘体积（单调递增）
      got=$(du -sb "$INSTALL_DIR/.venv" "$cache_dir" 2>/dev/null | awk '{s += $1} END {print s+0}')
      got=${got:-0}
      got=$((got - base_got))
      [ "$got" -lt 0 ] && got=0
      # 总下载字节：日志里出现过的所有 Downloading (SIZE) 累加
      total=$(grep -oE '^  Downloading[^(]*\([0-9.]+ ?[kKmMgG]?i?B?\)' "$log" 2>/dev/null \
                | grep -oE '\([0-9.]+ ?[kKmMgG]?i?B?\)' \
                | sed 's/[()]//g' \
                | awk '{ sum += $1 * m($2) } END { print int(sum) }
                       function m(u){
                         u=tolower(u); sub(/^[0-9.]+[[:space:]]*/,"",u);
                         if (u ~ /^g/) return 1073741824;
                         if (u ~ /^m/) return 1048576;
                         if (u ~ /^k/) return 1024;
                         return 1 }')
      total=${total:-0}
      # 「已下载」钳在总大小以内：venv+缓存 的全局增量把解压/装包也算了进去，
      # 分母却是各包下载大小之和，不钳就会写出 647.6MB/158.6MB 这类失真数字。
      if [ "$total" -gt 0 ] && [ "$got" -gt "$total" ]; then
        got="$total"
      fi
      # 当前下载的包名（最后一行 Downloading）
      cur=$(grep '^  Downloading' "$log" 2>/dev/null | tail -1 | sed -E 's/^  Downloading //; s/ \(.*//' | cut -c1-40)
      # 速度：本秒 (venv + 缓存) 体积增量（首秒视为 0，避免误报）
      rate=$((got - prev_got))
      prev_got=$got
      progress_bar "$got" "$total" "$rate" "$cur"
      sleep 1
    done
    wait "$pid" || ret=$?
    progress_clear
    if [ "$ret" = 0 ]; then
      ok=1
      phase_ok "安装" "$label（虚拟环境 $(human_size "$(du -sb "$INSTALL_DIR/.venv" 2>/dev/null | awk '{print $1}')")）"
      break
    fi
    if [ -s "$log" ]; then
      dim "失败原因：$(grep -v '^[[:space:]]*$' "$log" | tail -1 | cut -c1-90)"
    fi
  done
  if [ "$ok" != 1 ]; then
    # 最后一招：不吃任何源配置，走 pip 默认（有代理 / 内网源时反而能成）
    warn "镜像源都没装成功，再用 pip 默认源试一次"
    dim "pip 默认源（同步执行，请稍候）"
    ret=0
    "$py" -m pip install -r "$INSTALL_DIR/$req" --timeout 60 --retries 3 \
      --progress-bar off > "$log" 2>&1 || ret=$?
    [ "$ret" = 0 ] && ok=1
    if [ "$ok" != 1 ]; then
      dim "完整日志：$log"
      dim "失败原因：$(grep -v '^[[:space:]]*$' "$log" | tail -1 | cut -c1-90)"
    fi
  fi
  [ "$ok" = 1 ]
}

# ---------- 进度显示 ----------
# pip 不把实时速度/eta 落盘（它只在 \r 里画，写不进日志），所以这里自己算：
# 速度用 venv 体积增量，剩余量用日志 Downloading 累加总大小减已落盘字节。
# \r 原地重画只在真终端；重定向时打普通行。
_spin='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
_progress_on=0
_progress_last=""

progress_clear() {
  if [ "$_progress_on" = 1 ]; then printf '\r\033[K'; fi
  _progress_on=0
  # 每轮 pip 重来，百分比要从 0 重新记，否则第二轮 10% 档位对不上就不打了
  _progress_last=""
}

# progress_bar <已下载字节> <总字节> <速度(字节/秒)> <当前包名>
progress_bar() {
  local got="${1:-0}" total="${2:-0}" rate="${3:-0}" cur="${4:-}" spin_c
  local eta_text spd_text pct=0 pct_text="--" size_text
  if [ "$total" -gt 0 ] && [ "$got" -gt 0 ]; then
    pct=$((got * 100 / total))
    [ "$pct" -gt 100 ] && pct=100
  fi
  if [ "$total" -gt 0 ]; then
    size_text="$(human_size "$got")/$(human_size "$total")"
    pct_text="${pct}%"
  else
    # 拿不到总大小时不能显示 0%：给已下载量 + 速度，剩余写「未知」
    size_text="$(human_size "$got")（总大小未知）"
  fi
  if [ "$rate" -gt 0 ] && [ "$total" -gt 0 ] && [ "$got" -lt "$total" ]; then
    eta_text="剩余 $(human_dur $(((total - got) / rate)))"
  elif [ "$total" -le 0 ]; then
    eta_text="剩余时间未知"
  else
    eta_text="剩余 --"
  fi
  # 速度 0 ≠ 连不上：pip 先落缓存，体积采样可能还没动
  if [ "$rate" -gt 0 ]; then
    spd_text="$(human_rate "$rate")"
  else
    spd_text="测速中"
  fi
  if [ "$OUT_IS_TTY" != 1 ]; then
    # 非终端（安装日志）：进度条不刷屏，百分比只作为 phase 行里的一个字段，
    # 每 10% 更新一次同一条「下载」行，格式和其它阶段完全一致。
    local step_pct=$((pct / 10 * 10))
    if [ "$step_pct" != "${_progress_last:-}" ]; then
      phase "下载" "$cur  $pct_text  ${spd_text}"
      _progress_last="$step_pct"
    fi
    return 0
  fi
  spin_c="${_spin:$((got % ${#_spin})):1}"
  # 文件名 / 已下载 / 总大小 / 百分比 / 速度 / 剩余时间：六项都在，避免「看着像卡住」
  printf '\r\033[K%s%s%s  %s  %s  %s  %s  %s' \
    "$C_C" "$spin_c" "$C_END" "$cur" "$size_text" "$pct_text" "$spd_text" "$eta_text"
  _progress_on=1
}

# ---------- 数据目录 ----------
# 数据根权限：userdata 及子目录 0700，配置 0600（Linux 凭据不外泄）
ensure_data_layout() {
  ( umask 077; mkdir -p "$DATA_DIR" "$DATA_PERSONA_DIR" "$DATA_SESSIONS" "$DATA_UPLOADS" "$DATA_BOT" "$DATA_LOGS" "$DATA_MODELS" ) 2>/dev/null || true
  chmod 700 "$DATA_DIR" "$DATA_PERSONA_DIR" "$DATA_SESSIONS" "$DATA_UPLOADS" "$DATA_BOT" "$DATA_LOGS" "$DATA_MODELS" 2>/dev/null || true
}

# 工作区：幂等保证存在，已存在绝不动内容。首次创建时放 .directory（KDE 文件夹备注）
# 与 说明.txt；写入失败只记日志，不影响安装。
seed_workspace_notes() {
  local ws="$1" f failed=0
  f="$ws/.directory"
  if [ ! -e "$f" ]; then
    if ! cat > "$f" <<'EOF' 2>/dev/null
[Desktop Entry]
Type=Directory
Icon=folder-documents
Comment=DeckMeido 的工作区：AI 生成的文件都保存在这里
EOF
    then
      failed=1
    fi
  fi
  f="$ws/说明.txt"
  if [ ! -e "$f" ]; then
    if ! cat > "$f" <<'EOF' 2>/dev/null
DeckMeido 的工作区
==================

这是 DeckMeido 的工作区：AI 生成、读取、编辑的文件都保存在这里。

- 想让她处理文件时，把东西放进这个目录最方便；
- 不要随意把整个目录删掉：里面可能有你要保留的成果；
- 删除前请先确认没有还需要的内容（截图、文档、代码等）。

工作区路径可以在设置页的「工作区」里改成别的目录。
EOF
    then
      failed=1
    fi
  fi
  if [ "$failed" = 1 ]; then
    step_note "工作区说明文件写入失败（不影响使用，可稍后手动补建）"
  fi
  return 0
}

# 归属当前用户（deck）：脚本本身就以 deck 身份运行，新建目录天然属于 deck；
# 这里只纠正历史遗留（之前被 sudo 跑过、属主是 root）的情况，失败不致命。
ensure_workspace_owner() {
  local ws="$1" cur owner
  [ -d "$ws" ] || return 0
  cur="$(id -u)"
  owner="$(stat -c %u "$ws" 2>/dev/null || true)"
  [ "$owner" = "$cur" ] && return 0
  if need_cmd sudo && sudo -n chown "$(id -u):$(id -g)" "$ws" 2>/dev/null; then
    step_note "已把工作区归属修正为当前用户：$ws"
  else
    step_note "工作区属主不是当前用户，且无法免密修正：$ws（可能需要 sudo chown）"
  fi
  return 0
}

ensure_workspace() {
  local ws; ws="$(workspace_dir)"
  local fresh=0
  if [ -d "$ws" ]; then
    step_note "工作区已存在：$ws（内容未改动）"
  else
    if mkdir -p "$ws" 2>/dev/null; then
      fresh=1
      step_ok "已创建工作区：$ws"
    else
      step_fail "工作区创建失败：$ws（检查上一级目录是否有写权限）"
      return 1
    fi
  fi
  ensure_workspace_owner "$ws"
  [ "$fresh" = 1 ] && seed_workspace_notes "$ws"
  return 0
}

# 解析校验：坏配置绝不能留给用户（后端启动时会直接退出）。
# 没有 python3 时跳过，让后端自己报语法错。
validate_config() {
  local cfg="$1" out=""
  command -v python3 >/dev/null 2>&1 || return 0
  if ! out="$(python3 - "$cfg" <<'PY' 2>&1
import sys
import tomllib

try:
    with open(sys.argv[1], "rb") as fh:
        tomllib.load(fh)
except Exception as exc:
    print(exc)
    sys.exit(1)
PY
)"; then
    warn "配置解析失败：$out"
    return 1
  fi
  return 0
}

# 首次安装生成 config.toml；已有则原样保留
prepare_config() {
  ensure_data_layout
  local cfg="$DATA_CONFIG" backup="" existed=0
  if [ -f "$cfg" ]; then
    existed=1
    if ! validate_config "$cfg"; then
      die "已有配置无法解析：$cfg（本次未改动任何文件，请先修好再安装）"
    fi
    backup="$cfg.installbak"
    cp -p "$cfg" "$backup"
    info "保留已有配置 $cfg"
    # 老配置没有 [update] 段：补上，并写入当前脚本用的镜像地址，
    # 免得以后换域名时新旧两批机器指向不同地方
    if ! grep -q '^\[update\]' "$cfg"; then
      {
        echo ""
        echo "# 更新源（网页「设置 → 关于」可改）"
        echo "[update]"
        echo "mirror = \"$MIRROR\""
        echo "github_repo = \"$GITHUB_REPO\""
      } >> "$cfg"
      dim "已为旧配置补上 [update] 段"
    fi
  else
    cp "$INSTALL_DIR/server/config.example.toml" "$cfg"
    # 新装把本次安装使用的端口写进配置：运行时（客户端/插件）读的就是它，
    # 避免出现「安装脚本用 DECKMEIDO_PORT=18766，配置里还写着 18765」
    sed -i -E "s/^port *= *[0-9]+/port = $PORT/" "$cfg"
    info "生成配置 $cfg（端口 $PORT）"
  fi
  # 写完端口后再解析一次；失败就回滚到改动前，不把坏配置留给用户
  if ! validate_config "$cfg"; then
    if [ "$existed" -eq 1 ]; then
      mv -f "$backup" "$cfg"
    else
      rm -f "$cfg"
    fi
    die "生成的配置无法解析，已回滚：$cfg"
  fi
  if [ -n "$backup" ]; then
    rm -f "$backup"
  fi
  # 配置里有 API Key 等敏感项，统一收紧到 0600
  chmod 600 "$cfg" 2>/dev/null || true
}

# 后端只由插件（游戏模式）和桌面客户端拉起，安装器不生成/启用任何 systemd
# 单元，也不提供需手动 enable 的入口。

# 单独停后端（不含客户端）：后端由 start_new_session 拉起，杀客户端带不走它；
# 客户端启动时若后端已在跑（如 Decky 先拉了核心），退出时也刻意不碰它。
# 旧后端占端口 → 新后端被 singleton.guard 挡下直接退出 → 客户端报
# 「后端核心启动失败」，表现为升级后首次打不开、再升一次才好。
# 只认 `python -m uvicorn server.main:app`，避免误杀 runuser 包装或安装脚本。
# 端口上监听的进程 pid（ss 优先，lsof 次之，都没有则翻 /proc）
_port_pids() {
  local port="$1"
  if need_cmd ss; then
    ss -ltnp "sport = :$port" 2>/dev/null | sed -nE 's/.*pid=([0-9]+).*/\1/p' | sort -u
    return 0
  fi
  if need_cmd lsof; then
    lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | sort -u
    return 0
  fi
  _proc_socket_pids "$port"
}

# 没有 ss/lsof 时的兜底：按 /proc/net/tcp{,6} 里 LISTEN(0A) 的 inode 反查 fd。
# SteamOS 一般都有 ss，但卸载是一次性操作，宁可多一条不依赖额外命令的路。
# 路径用 _PROC（默认 /proc）拼，测试时可以指到别处。
_PROC="/proc"
_proc_socket_pids() {
  local port="$1" hex f line laddr st inode pid found="" d fd
  hex="$(printf '%04X' "$port")"
  for f in "$_PROC/net/tcp" "$_PROC/net/tcp6"; do
    [ -r "$f" ] || continue
    while read -r line; do
      # 列：sl local_address rem_address st ...
      laddr="$(printf '%s' "$line" | awk '{print $2}')"
      st="$(printf '%s' "$line" | awk '{print $4}')"
      inode="$(printf '%s' "$line" | awk '{print $10}')"
      [ "${laddr##*:}" = "$hex" ] || continue
      [ "$st" = "0A" ] || continue
      [ -n "$inode" ] && [ "$inode" != "0" ] || continue
      for d in "$_PROC"/[0-9]*; do
        pid="${d##*/}"
        case " $found " in *" $pid "*) continue ;; esac
        for fd in "$d"/fd/*; do
          [ -L "$fd" ] || continue
          if [ "$(readlink "$fd" 2>/dev/null)" = "socket:[$inode]" ]; then
            found="$found $pid"
            break
          fi
        done
      done
    done < "$f"
  done
  for pid in $found; do printf '%s\n' "$pid"; done
}

# 进程判据只有两条：同 UID + 命令行含 server.main:app 或 client/main.py。
# 不要求 cwd 匹配：nohup 从旧目录拉起的进程 cwd 会变成 "<path> (deleted)"，
# 旧判据认不出 → 停止步骤静默跳过 → 新后端被 singleton 挡在端口外，
# 表现为「v未知」（0.7.3 首装起不来的根因）。
_proc_uid() {
  local pid="$1" uid=""
  uid="$(awk '/^Uid:/{print $2; exit}' "/proc/$pid/status" 2>/dev/null || true)"
  [ -n "$uid" ] || uid="$(stat -c %u "/proc/$pid" 2>/dev/null || true)"
  printf '%s' "$uid"
}

_proc_cmdline() {
  local pid="$1"
  { tr '\0' ' ' < "/proc/$pid/cmdline"; } 2>/dev/null || true
}

_pid_is_ours() {
  local pid="$1" cmd uid me
  [ -r "/proc/$pid/cmdline" ] || return 1
  uid="$(_proc_uid "$pid")"
  me="$(id -u)"
  [ -n "$uid" ] && [ "$uid" = "$me" ] || return 1
  cmd="$(_proc_cmdline "$pid")"
  [ -n "$cmd" ] || return 1
  case "$cmd" in
    *server.main:app*|*client/main.py*) return 0 ;;
  esac
  return 1
}

# 命令行像本程序，但不分用户、不要求 cwd 还在：它就是「我们自己拉起的」，
# 只是可能是 root 起的、或安装目录已经进回收站（cwd 变成 "(deleted)"）。
# 卸载时必须用它——_pid_is_ours 只认同 UID，root 那份 uvicorn 永远杀不掉。
_pid_is_deckmeido() {
  local pid="$1" cmd
  [ -r "/proc/$pid/cmdline" ] || return 1
  cmd="$(_proc_cmdline "$pid")"
  [ -n "$cmd" ] || return 1
  case "$cmd" in
    *server.main:app*|*client/main.py*|*server/overlay.py*|*client/launch.sh*) return 0 ;;
  esac
  return 1
}

# 本程序的后端进程：命令行特征 + 端口占用双通道，全部复用 _pid_is_ours。
# 用户可能用 `nohup .venv/bin/python -m uvicorn server.main:app ...` 手动起过，
# 它不是安装器拉起的 pid，只按 pid 记录根本停不掉。
_backend_pids() {
  local d pid seen="" pids cmd
  for d in /proc/[0-9]*; do
    pid="${d#/proc/}"
    [ "$pid" = "$$" ] && continue
    # 进程可能在扫描过程中退出：重定向失败也别刷屏
    cmd="$( { tr '\0' ' ' < "$d/cmdline"; } 2>/dev/null )" || continue
    [ -n "$cmd" ] || continue
    case "$cmd" in *server.main*) ;; *) continue ;; esac
    _pid_is_ours "$pid" || continue
    seen="$seen $pid"
  done
  for pid in $(_port_pids "$PORT"); do
    [ "$pid" = "$$" ] && continue
    _pid_is_ours "$pid" || continue
    case " $seen " in *" $pid "*) ;; *) seen="$seen $pid" ;; esac
  done
  printf '%s' "$seen"
}

# 本程序的客户端进程：与后端同一判据，也兼容旧目录 / 已删除 cwd。
_client_pids() {
  local d pid cmd
  for d in /proc/[0-9]*; do
    pid="${d#/proc/}"
    [ "$pid" = "$$" ] && continue
    cmd="$( { tr '\0' ' ' < "$d/cmdline"; } 2>/dev/null )" || continue
    case "$cmd" in *client/main.py*) ;; *) continue ;; esac
    _pid_is_ours "$pid" || continue
    printf '%s\n' "$pid"
  done
}

# 端口是否被任何进程监听（ss/lsof 都查不到时退回 TCP 握手）
_port_answers() {
  local port="$1"
  ( exec 3<>"/dev/tcp/127.0.0.1/$port" ) 2>/dev/null && return 0
  return 1
}

_port_has_listener() {
  local port="$1" out=""
  if need_cmd ss; then
    out="$(ss -ltn 2>/dev/null | awk -v p="$port" '
      { n = split($4, a, ":"); if (n > 1 && a[n] == p) { print "yes"; exit } }')"
    [ -n "$out" ] && return 0
  elif need_cmd lsof; then
    lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1 && return 0
  fi
  _port_answers "$port"
}

# 预检与停机共用的判据：端口上的监听者是不是「全是本程序自己的进程」。
# 返回 0 = 空闲或全是自己人；1 = 有无法识别的占用者。
_port_owned_by_us() {
  local port="$1" pids pid
  _port_has_listener "$port" || return 0
  pids="$(_port_pids "$port")"
  [ -n "$pids" ] || return 1
  for pid in $pids; do
    _pid_is_ours "$pid" || return 1
  done
  return 0
}

# 端口占用者明细：pid / 命令行 / cwd / exe。
# cwd 可能带 " (deleted)"（目录已被删），exe 指向当时的 venv 解释器；
# 这两行是这次事故里唯一能直接看出「旧进程从哪个目录起来的」的证据。
_port_holder_report() {
  local port="$1" pids pid cmd cwd exe
  pids="$(_port_pids "$port")"
  if [ -z "$pids" ]; then
    dim "端口 $port 有监听，但查不到持有者 pid（可能属于其他用户）"
    return 0
  fi
  for pid in $pids; do
    cmd="$(_proc_cmdline "$pid")"
    cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || true)"
    exe="$(readlink "/proc/$pid/exe" 2>/dev/null || true)"
    dim "端口 $port 持有者：PID $pid"
    dim "  命令行：${cmd:-（读不到）}"
    dim "  工作目录：${cwd:-（读不到）}"
    dim "  可执行文件：${exe:-（读不到）}"
  done
}

_wait_port_free() {
  local i
  for i in $(seq 1 20); do
    _port_has_listener "$PORT" || return 0
    sleep 0.15
  done
  return 1
}

# 停掉后端进程本身（不是客户端）。停不掉返回 1，由调用方明确报错，
# 绝不在「旧进程还占着端口」的情况下继续换文件。
stop_backend() {
  local pids pid i alive
  pids="$(_backend_pids)"
  if [ -n "$pids" ]; then
    dim "关闭旧后端（PID:$pids）"
    # shellcheck disable=SC2086  # 这里就是要按空格拆成多个 pid
    kill -TERM $pids 2>/dev/null || true
    for i in $(seq 1 25); do
      alive=0
      for pid in $pids; do
        kill -0 "$pid" 2>/dev/null && alive=1
      done
      [ "$alive" = "0" ] && break
      sleep 0.2
    done
    if [ "$alive" != "0" ]; then
      # shellcheck disable=SC2086
      kill -KILL $pids 2>/dev/null || true
      sleep 0.5
      for pid in $pids; do
        if kill -0 "$pid" 2>/dev/null; then
          warn "后端进程 $pid 停不掉（可能属于其他用户）"
          return 1
        fi
      done
    fi
  fi
  # 最后一道闸：不管刚才有没有找到 pid，都复核端口是否真的释放。
  # 端口仍被占时不静默继续 —— 继续换文件只会让新后端被 singleton 挡住，
  # 表现成「安装成功但服务起不来」。
  if ! _wait_port_free; then
    step_fail "端口 $PORT 仍被占用，新后端会被单实例检查挡住"
    _port_holder_report "$PORT"
    next_hint "先手动结束上面的进程（sudo kill <PID>），再重跑安装；不要只替换文件"
    return 1
  fi
  return 0
}

# 换文件前统一停机：客户端（watchdog 会补拉后端）+ 后端本身。
stop_runtime() {
  stop_client || true
  if ! stop_backend; then
    step_fail "旧后端还在运行，无法安全替换文件"
    next_hint "手动执行：sudo pkill -f 'server.main:app'  或重启掌机后再装"
    return 1
  fi
  return 0
}

# ---------- 运行版本闸门 ----------
# 只比磁盘文件不算通过：必须问「运行中的进程」自己报什么版本。
running_version() {
  local raw
  raw="$(curl -fsS -m 5 "http://127.0.0.1:$PORT/api/version" 2>/dev/null)" || return 0
  [ -n "$raw" ] || return 0
  if need_cmd python3; then
    printf '%s' "$raw" | python3 -c \
      'import json,sys; d=json.load(sys.stdin); print(d.get("running_version") or d.get("version") or "")' \
      2>/dev/null && return 0
  fi
  printf '%s' "$raw" | grep -oE '"running_version"[[:space:]]*:[[:space:]]*"[^"]*"' |
    head -n1 | sed -E 's/.*"([^"]*)"$/\1/' || true
  return 0
}

# 没有客户端（无 GUI / 客户端没起来）时的兜底：直接拉后端，只为让闸门能验
launch_backend() {
  local py="$INSTALL_DIR/.venv/bin/python"
  [ -x "$py" ] || return 1
  mkdir -p "$DATA_LOGS" 2>/dev/null || true
  ( cd "$INSTALL_DIR" && setsid nohup "$py" -m uvicorn server.main:app \
      --host 127.0.0.1 --port "$PORT" >> "$DATA_LOGS/backend.log" 2>&1 & ) \
    2>/dev/null || return 1
  return 0
}

relaunch_runtime() {
  launch_client >/dev/null 2>&1 && return 0
  launch_backend
}

# verify_running_version <期望版本>
# 只比对一次：不一致就直接失败退出，不自动杀进程重试、不回滚。
# 装完仍跑旧版本：多半是脱管后端未停干净（应由第 2 步负责），此处仅兜底。
verify_running_version() {
  local want="$1" got
  wait_ready || true
  got="$(running_version)"
  if [ -n "$got" ] && [ "$got" = "$want" ]; then
    step_ok "运行版本 v$got（与磁盘一致）"
    return 0
  fi
  step_fail "运行中的后端报 v${got:-未知}，磁盘上是 v$want"
  next_hint "先执行：pkill -f 'uvicorn server.main:app'，再重跑：bash $INSTALL_DIR/install.sh upgrade"
  return 1
}

# 把字符串转成可以安全塞进 sed 的 s|...| 替换段：
# 替换段里的 & 会展开成「整个匹配」，\ 和分隔符 | 会破坏语法
sed_repl() {
  printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'
}

# ---------- Decky 插件 ----------
# 插件只在装了 Decky Loader 的机器上有意义，先探在不在，不在就整段跳过。
# 不构建、不报错 —— 多数桌面模式用户没装 Decky。
#   deploy.sh --detect：0 = 框架在，2 = 没装（1 按没装处理，宁可跳过别乱装）
PLUGIN_SKIP_REASON=""

decky_ready() {
  local d="$INSTALL_DIR/plugin/deploy.sh" rc=0
  PLUGIN_SKIP_REASON=""
  [ -f "$d" ] || { PLUGIN_SKIP_REASON="安装包里没有 plugin/deploy.sh"; return 1; }
  # 保留退出码：2 = 机器上没装 Decky（正常跳过），其它非 0 = 检测本身出错
  DECKMEIDO_NONINTERACTIVE=1 bash "$d" --detect >/dev/null 2>&1 || rc=$?
  case "$rc" in
    0) return 0 ;;
    2) PLUGIN_SKIP_REASON="没检测到 Decky Loader"; return 1 ;;
    *) PLUGIN_SKIP_REASON="检测 Decky 失败（deploy.sh --detect 退出码 $rc）"; return 1 ;;
  esac
}

install_plugin() {
  if ! decky_ready; then
    PLUGIN_RELOAD_STATE="absent"
    phase_skip "安装" "Decky 插件：${PLUGIN_SKIP_REASON:-没装 Decky Loader}，插件包已备好"
    dim "装好 Decky Loader 后一键部署：bash $INSTALL_DIR/plugin/deploy.sh"
    plugin_handoff_zip
    return 0
  fi
  # 动手之前先体检 Decky。已经卡在重启循环时再去重启它，只会把循环坐实，
  # 而这一步完全可以在复制文件之前就判断出来——先提示，用户可以先重启 Deck。
  local hrc=0
  decky_health || hrc=$?
  case "$hrc" in
    1)
      phase "检测" "Decky 当前异常（${DECKY_HEALTH_NOTE}），本次不重启，装完需重启 Deck" ;;
    2)
      phase "检测" "Decky 状态未知（${DECKY_HEALTH_NOTE}）" ;;
    3)
      phase "检测" "Decky 已装但服务没在运行，装完会自动启动" ;;
    *)
      phase "检测" "Decky 状态正常（${DECKY_HEALTH_NOTE}）" ;;
  esac
  # 已装插件 = 升级，直接替换不再询问。旧实现走 ask_yes（默认 N），
  # 终端回车即跳过，插件停在旧版（0.6.11 事故）。
  local installed=""
  installed="$(plugin_installed_version 2>/dev/null)" || installed=""
  if [ -n "$installed" ]; then
    if plugin_install_zip; then
      local ver=""
      ver="$(plugin_installed_version)" || ver=""
      if [ -n "$ver" ] && [ "$ver" != "$installed" ]; then
        phase_ok "安装" "Decky 插件 v${installed} → v${ver}"
      else
        phase_skip "安装" "Decky 插件已是 v${installed}（包内版本相同）"
      fi
      plugin_reload || true
    else
      PLUGIN_RELOAD_STATE="install_failed"
      phase_fail "安装" "Decky 插件自动更新失败，可手动重装：bash $INSTALL_DIR/plugin/deploy.sh"
    fi
    return 0
  fi
  # 内联升级（updater.py 以 start_new_session + stdin=/dev/null 拉起）无终端，
  # ask_yes 恒答不 → 插件被静默跳过。无人值守必须自动装。
  if [ "${DECKMEIDO_NONINTERACTIVE:-0}" = "1" ] || { [ ! -t 0 ] && [ ! -r /dev/tty ]; }; then
    if plugin_install_zip; then
      local ver
      ver="$(plugin_installed_version)" || ver=""
      phase_ok "安装" "Decky 插件${ver:+ v$ver}"
      plugin_reload || true
    else
      PLUGIN_RELOAD_STATE="install_failed"
      phase_fail "安装" "Decky 插件未装上，可手动重装：bash $INSTALL_DIR/plugin/deploy.sh"
    fi
    return 0
  fi
  # 可选下载里已经确认过（=0 跳过 / =1 装）；没确认过才在这里问
  case "${DECKMEIDO_INSTALL_PLUGIN:-ask}" in
    0|no|off|false)
      phase_skip "安装" "Decky 插件（DECKMEIDO_INSTALL_PLUGIN=0）"
      PLUGIN_RELOAD_STATE="skipped"
      plugin_handoff_zip
      return 0 ;;
    1|yes|on|true)
      if plugin_install_zip; then
        local ver
        ver="$(plugin_installed_version)" || ver=""
        phase_ok "安装" "Decky 插件${ver:+ v$ver}"
        plugin_reload || true
      else
        PLUGIN_RELOAD_STATE="install_failed"
        phase_fail "安装" "Decky 插件未装上，可手动重装：bash $INSTALL_DIR/plugin/deploy.sh"
      fi
      return 0 ;;
  esac
  # 检测到 Decky，但装不装让用户定：插件要写进 ~/homebrew/plugins，属于系统级改动
  if ask_yes "检测到 Decky Loader，现在就在游戏模式里装上 QAM 面板吗？（输入 y 后按回车；直接按 A/回车=不装）"; then
    # 直接解 zip 到 ~/homebrew/plugins/DeckMeido（与 Decky 安装器同产物，
    # 删安装目录不影响加载）；deploy.sh 仅供手动重解。
    if plugin_install_zip; then
      local ver
      ver="$(plugin_installed_version)" || ver=""
      # 不主动重启 Decky（会打断游戏模式），只记「待重载」，收尾卡片不得写已生效
      PLUGIN_RELOAD_STATE="${PLUGIN_RELOAD_STATE:-pending}"
      phase_ok "安装" "Decky 插件${ver:+ v$ver}，待 Decky 重新载入后生效"
    else
      PLUGIN_RELOAD_STATE="install_failed"
      phase_fail "安装" "Decky 插件未装上，可手动重装：bash $INSTALL_DIR/plugin/deploy.sh"
    fi
  else
    PLUGIN_RELOAD_STATE="skipped"
    plugin_handoff_zip
  fi
  return 0
}

# 解包 deploy/deckmeido-plugin.zip 到 ~/homebrew/plugins/DeckMeido。
# zip 由 release.py 产出（前端已编译），顶层为 DeckMeido/。
plugin_install_zip() {
  local src="$INSTALL_DIR/deploy/deckmeido-plugin.zip"
  local root="${DECKY_PLUGINS_DIR:-$HOME/homebrew/plugins}"
  local target="$root/DeckMeido"
  local tmpdir="" stage="" tmpzip=""
  # 优先用安装目录里带的那份（release.py 从 dist/ 拷进 deploy/）；
  # git 源码目录不带这个 zip，但有 plugin/dist/index.js 时当场压一个
  if [ ! -f "$src" ]; then
    if [ ! -f "$INSTALL_DIR/plugin/dist/index.js" ] || [ ! -f "$INSTALL_DIR/plugin/package.json" ]; then
      dim "安装包里没带插件包（$src），源码里也没有 plugin/dist/index.js，跳过"
      return 1
    fi
    need_cmd zip || { dim "缺少 zip，无法就地打包插件"; return 1; }
    # zip 顶层目录必须是 DeckMeido/（Decky 按目录名识别插件），先拷成 DeckMeido/ 再压
    tmpdir="$(mktemp -d)" || return 1
    stage="$tmpdir/DeckMeido"
    tmpzip="$tmpdir/deckmeido-plugin.zip"
    if ! cp -a "$INSTALL_DIR/plugin" "$stage" ||
       ! rm -rf "$stage/node_modules" "$stage/src" ||
       ! ( cd "$tmpdir" && zip -qr "$tmpzip" DeckMeido ) >/dev/null 2>&1; then
      dim "就地打包插件失败（$tmpdir）"
      rm -rf "$tmpdir"
      return 1
    fi
    dim "源码目录不带插件包，已就地打包：$tmpzip"
    src="$tmpzip"
  fi
  need_cmd unzip || { dim "缺少 unzip，跳过"; return 1; }
  # 旧版可能是软链接：先删链接本身，别顺着链接删到安装目录
  if [ -L "$target" ]; then
    rm -f "$target" 2>/dev/null || sudo rm -f "$target"
  fi
  # 插件根目录 root:root，普通用户建不了，借一次 sudo
  sudo -n mkdir -p "$target" 2>/dev/null ||
    sudo mkdir -p "$target" 2>/dev/null || { dim "建不动 $target"; [ -n "$tmpdir" ] && rm -rf "$tmpdir"; return 1; }
  # zip 里没有的旧文件（改名模块）会让 Decky 加载过期代码，先清空
  safe_rm_target "$target" || { dim "拒绝清理插件目录（$target）"; [ -n "$tmpdir" ] && rm -rf "$tmpdir"; return 1; }
  dim "清理旧插件目录（只删本插件自己的目录，不会动别的插件与用户数据）：$target"
  sudo rm -rf "$target" 2>/dev/null || true
  sudo mkdir -p "$target" || { [ -n "$tmpdir" ] && rm -rf "$tmpdir"; return 1; }
  sudo unzip -oq "$src" -d "$root"; local _zrc=$?
  [ -n "$tmpdir" ] && rm -rf "$tmpdir"
  [ "$_zrc" = 0 ] || return 1
  # zip 顶层目录名是 DeckMeido，解到 $root 下正好生成 $target
  if ! plugin_verify_layout "$target"; then
    dim "插件目录校验失败：$PLUGIN_LAYOUT_ERROR"
    return 1
  fi
  return 0
}

# 插件目录自检：dist/index.js（前端产物）、plugin.json（Decky 清单）、
# package.json（版本）三者必须齐全且版本一致。
# 版本一致性必须查：release.py 分开更新 plugin.json / package.json，漏一个就会
# Decky 面板版本与实跑代码不符，用户报障时无法判断版本。
PLUGIN_LAYOUT_ERROR=""
plugin_verify_layout() {
  local dir="${1:-${DECKY_PLUGINS_DIR:-$HOME/homebrew/plugins}/DeckMeido}"
  local pj="$dir/plugin.json" pkg="$dir/package.json" dist="$dir/dist/index.js"
  local vj vp mode
  PLUGIN_LAYOUT_ERROR=""
  if [ ! -d "$dir" ]; then
    PLUGIN_LAYOUT_ERROR="插件目录不存在：$dir"
    return 1
  fi
  if [ ! -f "$pj" ]; then
    PLUGIN_LAYOUT_ERROR="缺少 Decky 清单 plugin.json：$pj"
    return 1
  fi
  if [ ! -f "$dist" ]; then
    PLUGIN_LAYOUT_ERROR="缺少前端产物 dist/index.js：$dist（zip 不完整或打包时没编译前端）"
    return 1
  fi
  if [ ! -f "$pkg" ]; then
    PLUGIN_LAYOUT_ERROR="缺少 package.json：$pkg"
    return 1
  fi
  vj="$(sed -nE 's/.*"version"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p' "$pj" | head -n1)"
  vp="$(sed -nE 's/.*"version"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p' "$pkg" | head -n1)"
  # 版本号以 package.json 为准：Decky 只读它的 version 显示在 QAM，官方模板
  # plugin.json 只有 name/author/flags/api_version。2026-11-08 装机日志把
  # 「plugin.json 无 version」判成失败，删掉旧插件后又判死自家 zip，
  # 插件目录被清空、QAM 打不开。故 vj 缺失可以，vp 缺失才失败。
  if [ -z "$vp" ]; then
    PLUGIN_LAYOUT_ERROR="package.json 里没有 version 字段：$pkg"
    return 1
  fi
  if [ -n "$vj" ] && [ "$vj" != "$vp" ]; then
    PLUGIN_LAYOUT_ERROR="版本不一致：plugin.json=$vj，package.json=$vp（打包漏更新了一个）"
    return 1
  fi
  # 不可读不影响出包（Decky 以 root 加载），仅提示
  if [ ! -r "$pj" ] || [ ! -r "$dist" ]; then
    mode="$(stat -c '%a' "$pj" 2>/dev/null || echo '?')"
    dim "提示：插件文件当前用户不可读（plugin.json 权限 $mode）；Decky 以 root 运行通常仍能加载"
  fi
  return 0
}

# 已安装插件的版本号：解包后写进日志，升没升上去一眼能看出来
plugin_installed_version() {
  local p="${DECKY_PLUGINS_DIR:-$HOME/homebrew/plugins}/DeckMeido/package.json"
  [ -f "$p" ] || return 1
  sed -nE 's/.*"version"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p' "$p" | head -n1
}

# Decky 的 systemd unit 名（decky / plugin_loader 两种命名都可能）
decky_service_unit() {
  systemctl list-unit-files 2>/dev/null |
    sed -nE 's/^((decky|plugin_loader)[^[:space:]]*\.service).*/\1/p' | head -n1 || true
}

# 1337 是否「真的能用」：不是看 LISTEN，而是真发一次请求。
# 为什么必须真连：卡在重启循环时，旧子进程一直占着 1337，LISTEN 永远正常，
# 但新实例早就绑不上退出了——只看 LISTEN 会得出「Decky 好好的」这个错误结论。
decky_port_probe() {
  local code
  if need_cmd curl; then
    code="$(curl -s -o /dev/null -m 3 -w '%{http_code}' 'http://127.0.0.1:1337/' 2>/dev/null || true)"
    case "$code" in ''|000|0000) ;; *) return 0 ;; esac
  fi
  if need_cmd timeout; then
    timeout 2 bash -c 'exec 3<>/dev/tcp/127.0.0.1/1337' 2>/dev/null && return 0
  else
    ( exec 3<>/dev/tcp/127.0.0.1/1337 ) 2>/dev/null && return 0
  fi
  return 1
}

decky_nrestarts() {
  local unit="$1" nr
  nr="$(systemctl show -p NRestarts --value "$unit" 2>/dev/null || true)"
  case "$nr" in ''|*[!0-9]*) nr="" ;; esac
  printf '%s' "$nr"
}

# journal 里「1337 已被占用」的次数：重启循环唯一留下来的硬证据
decky_bind_fail_hits() {
  local unit="$1" since="$2" n
  command -v journalctl >/dev/null 2>&1 || { printf '0'; return 0; }
  n="$(journalctl -u "$unit" --since "$since" --no-pager 2>/dev/null |
    grep -c '1337.*address already in use' 2>/dev/null || true)"
  case "$n" in ''|*[!0-9]*) n=0 ;; esac
  printf '%s' "$n"
}

# stop 之后还活着的 Decky 进程。KillMode=process 下这正是死循环的来源：
# 主进程被杀，子进程继续持有 1337，所以必须在 start 之前确认它们真的没了。
decky_residual_procs() {
  local pid cmd out="" cands
  cands="$(_port_pids 1337)"
  # 除了按进程名找，还必须找「真正占着 1337 的人」：stop 之后仍持有端口的子进程
  # 未必有 plugin_loader 字样（2026-11-10 卸载实测，残留 PID 1734/1909/1910 就
  # 全是这样）。只按名字找会漏掉它们 → start 一上来就撞端口 → 重启循环。
  if command -v pgrep >/dev/null 2>&1; then
    cands="$cands $(pgrep -f 'PluginLoader|plugin_loader' 2>/dev/null || true)"
  fi
  for pid in $(printf '%s\n' $cands | sort -u); do
    [ "$pid" = "$$" ] && continue
    [ "$pid" = "${PPID:-}" ] && continue
    cmd="$(_proc_cmdline "$pid")"
    # 我们自己的探针命令（pgrep/systemctl 的命令行里也带这些字样）不算残留
    case "$cmd" in
      *pgrep*|*systemctl*|*install.sh*|*uninstall.sh*) continue ;;
    esac
    out="$out $pid"
  done
  printf '%s' "${out# }"
}

# 等到「主进程退出 + 没有残留子进程」，最多等 timeout 秒；返回 1 表示还有残留
decky_wait_stopped() {
  local unit="$1" tmo="$2" i pid res
  for i in $(seq 1 $((tmo * 2))); do
    pid="$(systemctl show -p MainPID --value "$unit" 2>/dev/null || true)"
    res="$(decky_residual_procs)"
    if { [ -z "$pid" ] || [ "$pid" = "0" ]; } && [ -z "$res" ]; then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

# Decky 体检。0=可用；1=已异常（绝不能重启它）；2=查不了（无 systemctl/
# 没装 Decky，不算故障）；3=装了但没在跑（start 即可）。
# 三条判据同看：is-active、journal 近 2 分钟 1337 bind 失败、1337 能否连通。
# 只看 is-active 会被重启循环骗过（循环期它一直是 active）。
decky_health() {
  local unit state nr hits
  DECKY_HEALTH="unknown"
  DECKY_HEALTH_NOTE=""
  if ! need_cmd systemctl; then
    DECKY_HEALTH_NOTE="没有 systemctl，无法检查 Decky"
    return 2
  fi
  unit="$(decky_service_unit)"
  if [ -z "$unit" ]; then
    DECKY_HEALTH_NOTE="没检测到 Decky 服务（decky/plugin_loader）"
    return 2
  fi
  DECKY_UNIT="$unit"
  state="$(systemctl is-active "$unit" 2>/dev/null || true)"
  nr="$(decky_nrestarts "$unit")"
  hits="$(decky_bind_fail_hits "$unit" '2 minutes ago')"
  if [ "$state" != "active" ]; then
    # 只是没在跑（没启动 / 被停用），不等于坏了：文件照常部署，随后 start 即可
    DECKY_HEALTH="down"
    DECKY_HEALTH_NOTE="Decky 服务没有在运行（${state:-unknown}）"
    return 3
  fi
  if [ "$hits" -gt 0 ]; then
    DECKY_HEALTH="broken"
    DECKY_HEALTH_NOTE="近 2 分钟 bind 1337 失败 ${hits} 次（NRestarts=${nr:-未知}），正卡在重启循环"
    return 1
  fi
  if ! decky_port_probe; then
    DECKY_HEALTH="broken"
    DECKY_HEALTH_NOTE="服务显示 active，但 127.0.0.1:1337 不应答（NRestarts=${nr:-未知}），疑似卡在重启循环"
    return 1
  fi
  DECKY_HEALTH="ok"
  DECKY_HEALTH_NOTE="active，1337 可连，NRestarts=${nr:-未知}"
  return 0
}

# 重启循环探测（安装/卸载共用）：重启后必须回读 NRestarts，不能只看 is-active。
# plugin_loader.service 是 KillMode=process，systemctl restart 只杀主进程，旧子进程
# 仍占 127.0.0.1:1337；新实例绑不上就退出，Restart=always 再拉起 → 每 7 秒一轮循环
# （2026-11-08 实测 271 次，还把 Decky Music 拖到 10GB），期间 is-active 恒为 active。
# 判定：15 秒内 NRestarts 增长，或近 30 秒 journal 出现 1337 address already in use。
# 命中只置 DECKY_LOOP_NOTE 并返回 1，不中断流程（此时流程可能已做了一半）。
decky_restart_loop_note() {
  local unit="$1" before after hits why
  case "$unit" in ''|*' '*) return 0 ;; esac
  command -v systemctl >/dev/null 2>&1 || return 0
  before="$(systemctl show -p NRestarts --value "$unit" 2>/dev/null || true)"
  case "$before" in ''|*[!0-9]*) return 0 ;; esac
  sleep 15
  after="$(systemctl show -p NRestarts --value "$unit" 2>/dev/null || true)"
  case "$after" in ''|*[!0-9]*) return 0 ;; esac
  hits="$(journalctl -u "$unit" --since '30 seconds ago' --no-pager 2>/dev/null |
    grep -c '1337.*address already in use' 2>/dev/null || true)"
  case "$hits" in ''|*[!0-9]*) hits=0 ;; esac
  why=""
  [ "$after" -gt "$before" ] && why="NRestarts ${before} → ${after}"
  if [ "$hits" -gt 0 ]; then
    if [ -n "$why" ]; then
      why="$why，近 30 秒 bind 1337 失败 ${hits} 次"
    else
      why="近 30 秒 bind 1337 失败 ${hits} 次"
    fi
  fi
  [ -n "$why" ] || return 0
  DECKY_LOOP_NOTE="Decky 卡在重启循环（${unit}：${why}）：旧插件进程还占着 127.0.0.1:1337，新实例绑不上端口。请先重启 Deck 再继续"
  return 1
}

# 安全重载 Decky：stop → 等残留退出 → start → 回读验证。
# 不用 systemctl restart：unit 是 KillMode=process，restart 只杀主进程，子进程仍持
# 1337，systemd 立刻拉起新实例 → 绑不上 → 无限重启循环（2026-11-08 实测 271 次）。
# 因此必须 stop（等 TimeoutStopSec 收尾）→ 确认残留消失 → 才 start；
# 残留未清绝不 start，宁可让用户重启 Deck。
decky_safe_reload() {
  local unit tmo state i did_stop=0
  DECKY_SAFE_STATE=""
  DECKY_SAFE_NOTE=""
  DECKY_LOOP_NOTE=""
  if ! need_cmd systemctl; then
    DECKY_SAFE_NOTE="没有 systemctl，重启游戏模式后插件才会用上新版本"
    return 1
  fi
  [ -n "${DECKY_UNIT:-}" ] || DECKY_UNIT="$(decky_service_unit)"
  unit="$DECKY_UNIT"
  if [ -z "$unit" ]; then
    DECKY_SAFE_NOTE="没检测到 Decky 服务（decky/plugin_loader），无法自动重载"
    return 1
  fi
  # 先确认拿得到 root 再停：停得下来却起不来，会把 Decky 直接留在停止状态
  if ! run_root systemctl is-active "$unit" >/dev/null 2>&1; then
    DECKY_SAFE_STATE="pending"
    DECKY_SAFE_NOTE="没有 root 权限，无法安全重载；请到 设置 → Decky Loader 点「重新载入」"
    return 1
  fi
  state="$(systemctl is-active "$unit" 2>/dev/null || true)"
  if [ "$state" = "active" ]; then
    did_stop=1
    if ! run_root systemctl stop "$unit" >/dev/null 2>&1; then
      DECKY_SAFE_STATE="failed"
      DECKY_SAFE_NOTE="停止 Decky 失败（$unit）：sudo 被拒绝或服务不存在"
      return 1
    fi
    # Decky 停下后，插件在 Decky 环境里拉起的客户端/后端就成孤儿了：它们仍然
    # 占着 1337（以及后端端口），名字里没有 plugin_loader，不做处理就会被当成
    # 「残留进程」把重载卡死。先补一刀：本程序进程 + 1337 的实际持有者。
    local orph pid
    orph="$(_deckmeido_pids_all)"
    for pid in $(_port_pids 1337); do
      [ "$pid" = "$$" ] && continue
      case " $orph " in *" $pid "*) ;; *) orph="$orph $pid" ;; esac
    done
    orph="${orph# }"
    if [ -n "$orph" ]; then
      un_stop_pids "$orph" >/dev/null 2>&1 || true
    fi
    # 等它真正停干净。上限取服务自己的 TimeoutStopSec（15s），再长也没意义
    tmo="$(systemctl show -p TimeoutStopSec --value "$unit" 2>/dev/null || true)"
    tmo="${tmo%s}"
    case "$tmo" in ''|*[!0-9]*) tmo=15 ;; esac
    [ "$tmo" -gt 20 ] && tmo=20
    [ "$tmo" -lt 3 ] && tmo=3
    if ! decky_wait_stopped "$unit" "$tmo"; then
      local res cur
      res="$(decky_residual_procs)"
      DECKY_SAFE_STATE="failed"
      cur=""
      for i in $res; do
        case "$( _proc_cmdline "$i" )" in
          *server.main:app*|*client/main.py*|*server/overlay.py*) cur="$cur $i" ;;
        esac
      done
      if [ -n "$cur" ]; then
        DECKY_SAFE_NOTE="停止 Decky 后仍有残留进程（PID:$res），其中$cur 是本程序的客户端/后端（它们在这里跑在 Decky 环境里）。已放弃重载，避免触发重启循环。重启 Deck 即可；急着用可先执行：kill${cur}，再重新运行卸载"
      else
        DECKY_SAFE_NOTE="停止 Decky 后仍有残留进程（PID: $res）占着 1337；已放弃重载，避免触发重启循环。请重启 Deck 后再继续"
      fi
      return 1
    fi
  else
    # 服务本来就没在跑（没启动 / 被停用）：不需要 stop，但必须先确认没有残留子进程
    # 占着 1337——否则 start 一上来就会撞端口，直接进重启循环。
    local res
    res="$(decky_residual_procs)"
    if [ -n "$res" ]; then
      un_stop_pids "$res" >/dev/null 2>&1 || true
      res="$(decky_residual_procs)"
    fi
    if [ -n "$res" ]; then
      DECKY_SAFE_STATE="failed"
      DECKY_SAFE_NOTE="Decky 服务已停止，但仍有残留进程（PID: $res）占着 1337，且结束不掉；已放弃重载，请重启 Deck 后再继续"
      return 1
    fi
  fi
  if ! run_root systemctl start "$unit" >/dev/null 2>&1; then
    DECKY_SAFE_STATE="failed"
    DECKY_SAFE_NOTE="启动 Decky 失败（$unit）：请重启 Deck 后在 设置 → Decky Loader 点「重新载入」"
    return 1
  fi
  state=""
  for i in $(seq 1 20); do
    state="$(systemctl is-active "$unit" 2>/dev/null || true)"
    [ "$state" = "active" ] && break
    sleep 0.5
  done
  if [ "$state" != "active" ]; then
    DECKY_SAFE_STATE="failed"
    DECKY_SAFE_NOTE="Decky 启动后状态是「${state:-unknown}」，不是 active；请重启 Deck"
    return 1
  fi
  # 回读一：NRestarts 15 秒内不得增长，journal 不能有新的 1337 bind 失败
  if ! decky_restart_loop_note "$unit"; then
    DECKY_SAFE_STATE="failed"
    DECKY_SAFE_NOTE="$DECKY_LOOP_NOTE"
    return 1
  fi
  # 回读二：1337 必须真的应答（不是「有 LISTEN」就算数）
  for i in $(seq 1 6); do
    decky_port_probe && { DECKY_SAFE_STATE="ok"; break; }
    sleep 0.5
  done
  if [ "$DECKY_SAFE_STATE" != "ok" ]; then
    DECKY_SAFE_STATE="failed"
    DECKY_SAFE_NOTE="Decky 已启动但 127.0.0.1:1337 不应答：插件加载失败，请重启 Deck 后再试"
    return 1
  fi
  local how="stop→start"
  [ "$did_stop" = 1 ] || how="原来没在运行，已直接 start"
  DECKY_SAFE_NOTE="Decky 已安全重载（$unit：$how，1337 可连）"
  return 0
}

# Decky 的 journal 里有没有加载到本插件（只在「可读」时作为辅助证据）
decky_plugin_logged() {
  local unit="${DECKY_UNIT:-}" n
  [ -n "$unit" ] && need_cmd journalctl || return 1
  n="$(journalctl -u "$unit" --since '60 seconds ago' --no-pager 2>/dev/null |
    grep -ci 'deckmeido' 2>/dev/null || true)"
  case "$n" in ''|*[!0-9]*) return 1 ;; esac
  [ "$n" -gt 0 ]
}

# 重载 Decky：插件是启动时一次性 import 的，换了磁盘上的文件不重载还是旧代码
plugin_reload() {
  local hrc=0
  DECKY_LOOP_NOTE=""
  DECKY_SAFE_STATE=""
  DECKY_SAFE_NOTE=""
  # 先体检再动手：已卡在重启循环里还去重启只会坐实循环。
  # 3 = 装了但没在跑，不算故障，后面 start 即可，不能跳过。
  decky_health || hrc=$?
  if [ "$hrc" = 1 ]; then
    PLUGIN_RELOAD_STATE="pending"
    DECKY_LOOP_NOTE="$DECKY_HEALTH_NOTE"
    warn "Decky 当前异常：$DECKY_HEALTH_NOTE"
    warn "为避免把 Decky 弄坏，本次不重启它；插件文件已部署，请先重启 Deck，再到 设置 → Decky Loader 点「重新载入」"
    return 1
  fi
  if [ "$hrc" = 2 ]; then
    PLUGIN_RELOAD_STATE="pending"
    warn "$DECKY_HEALTH_NOTE"
    warn "无法自动重载；请到 设置 → Decky Loader 点「重新载入」使插件生效"
    return 1
  fi
  [ "$hrc" = 3 ] && dim "Decky 服务当前没在运行，接下来会先启动它"
  if decky_safe_reload; then
    PLUGIN_RELOAD_STATE="ok"
    info "$DECKY_SAFE_NOTE"
    if decky_plugin_logged; then
      info "Decky 日志里已看到 DeckMeido，插件已加载"
    else
      dim "Decky 日志里暂时没有 DeckMeido 的加载记录，进 QAM 确认一下是否出现"
    fi
    return 0
  fi
  PLUGIN_RELOAD_STATE="failed"
  DECKY_LOOP_NOTE="$DECKY_SAFE_NOTE"
  warn "[!] $DECKY_SAFE_NOTE"
  return 1
}

# 用户跳过插件安装时，把发布包里的插件 zip 复制到工作区，并说明手动安装流程。
# zip 由 release.py 放进插件 zip（前端已编译，无需 npm）。
plugin_handoff_zip() {
  local src="$INSTALL_DIR/deploy/deckmeido-plugin.zip" ws dst
  if [ ! -f "$src" ]; then
    dim "安装包里没带插件包，跳过。想装时可单独下载 deckmeido-plugin-<版本>.zip"
    return 0
  fi
  ws="$(workspace_dir)"
  dst="$ws/deckmeido-plugin.zip"
  mkdir -p "$ws"
  cp -f "$src" "$dst"
  dim "已把插件包放到工作区：$dst"
  dim "之后想装，手动执行："
  dim "  sudo mkdir -p ~/homebrew/plugins"
  dim "  sudo unzip -o '$dst' -d ~/homebrew/plugins   # zip 顶层已经是 DeckMeido/"
  dim "  # 然后到 设置 → Decky Loader 点「重新载入」"
  return 0
}

install_desktop() {
  mkdir -p "$DESKTOP_DIR"
  sed "s|__INSTALL_DIR__|$(sed_repl "$INSTALL_DIR")|g" \
    "$INSTALL_DIR/deploy/DeckMeido.desktop" > "$DESKTOP_DIR/DeckMeido.desktop"
  chmod +x "$DESKTOP_DIR/DeckMeido.desktop"
  need_cmd update-desktop-database && update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true

  # 同时放一份到桌面
  if [ -d "$DESKTOP_FOLDER" ]; then
    cp "$DESKTOP_DIR/DeckMeido.desktop" "$DESKTOP_FOLDER/DeckMeido.desktop"
    chmod +x "$DESKTOP_FOLDER/DeckMeido.desktop"
    # GNOME 系桌面需要标记为可信，否则双击会提示不安全
    need_cmd gio && gio set "$DESKTOP_FOLDER/DeckMeido.desktop" metadata::trusted true >/dev/null 2>&1 || true
  fi

  dim "快捷方式已放到应用菜单和桌面（游戏模式下可加到非 Steam 游戏）"
}

# 在安装目录留一个卸载入口，用户不必记住 install.sh 的路径和参数。
# 逻辑只有 do_uninstall 一份，这里只做转发；install.sh 被删了才走兜底分支。
install_uninstaller() {
  cat > "$INSTALL_DIR/uninstall.sh" <<'EOS'
#!/usr/bin/env bash
# 生成的卸载入口。逻辑只有 install.sh 的 do_uninstall 一份，此处仅转发。
#   bash uninstall.sh [--dry-run] [--purge]
# 一律只移入回收站，不永久删除；userdata/ 仅 --purge 时移走。
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="$HERE/install.sh"
if [ ! -f "$SELF" ]; then
  echo "[!] 找不到安装脚本 $SELF —— 请从安装包重新执行：bash install.sh uninstall" >&2
  exit 1
fi
# 本脚本住在安装目录里，install.sh 的删除守卫会把它当「脚本所在目录」拒绝。
# 这个变量是唯一的口子：只放行安装目录本身，仍然拒绝 .git 源码工作区。
export DECKMEIDO_INSTALL_DIR_UNINSTALL=1
export DECKMEIDO_UNINSTALL_ENTRY="$HERE/uninstall.sh"
exec bash "$SELF" uninstall "$@"
EOS
  chmod +x "$INSTALL_DIR/uninstall.sh"
}

# 版本号写进安装目录，网页设置页与升级检查都读它
write_version_file() {
  # 发布包里带一份 VERSION（release.py 按 tag 写入），tar 安装时没有
  # git 仓库，只能靠它；git 安装则用 describe，能反映「比 tag 多几个提交」
  local ver=""
  if [ -f "$HERE/VERSION" ]; then
    ver=$(head -n1 "$HERE/VERSION" 2>/dev/null || true)
  fi
  if [ -z "$ver" ]; then
    ver=$(git -C "$INSTALL_DIR" describe --tags --always 2>/dev/null || true)
  fi
  [ -n "$ver" ] || ver=$(git -C "$HERE" describe --tags --always 2>/dev/null || true)
  # 手工解包/无 git 的部署：退回插件 package.json 的 version，
  # 避免 VERSION 被写成 0.0.0 导致界面显示 v0.0.0
  [ -n "$ver" ] || ver=$(sed -nE 's/^  "version": "([^"]*)".*/\1/p' "$INSTALL_DIR/plugin/package.json" 2>/dev/null | head -n1)
  [ -n "$ver" ] || ver="0.0.0"
  printf '%s\n' "${ver#v}" > "$INSTALL_DIR/VERSION"
  # 构建标记：短提交号。有 git 就现算；没有 git 则沿用发布包/安装目录里已有的
  # COMMIT（release.py 或部署脚本写入）；都没有就跳过，界面只显示纯版本号
  local commit=""
  if [ -d "$INSTALL_DIR/.git" ]; then
    commit=$(git -C "$INSTALL_DIR" rev-parse --short=7 HEAD 2>/dev/null || true)
  fi
  if [ -z "$commit" ] && [ -d "$HERE/.git" ]; then
    commit=$(git -C "$HERE" rev-parse --short=7 HEAD 2>/dev/null || true)
  fi
  if [ -z "$commit" ] && [ -f "$HERE/COMMIT" ]; then
    commit=$(head -n1 "$HERE/COMMIT" 2>/dev/null | tr -d '[:space:]' || true)
  fi
  if [ -n "$commit" ]; then
    printf '%s\n' "$commit" > "$INSTALL_DIR/COMMIT"
  fi
  dim "版本：$(cat "$INSTALL_DIR/VERSION")　构建：$(cat "$INSTALL_DIR/COMMIT" 2>/dev/null || echo 无)"
}

wait_ready() {
  # 冷启动要依次起 Qt、拉后端、uvicorn 加载、首次渲染。旧版只等 0.9 秒，
  # 新装/大版本升级几乎必超时（误报「未能自动启动」）。给到 30 秒，
  # 进程已退出（启动失败）则提前放弃。
  local url="http://127.0.0.1:$PORT/"
  local i
  for i in $(seq 1 60); do
    if curl -fsS -o /dev/null "$url" 2>/dev/null; then return 0; fi
    if [ "$i" -gt 6 ] && ! pgrep -f "$INSTALL_DIR/client/main.py" >/dev/null 2>&1 \
       && ! pgrep -f "uvicorn server.main:app" >/dev/null 2>&1; then
      return 1
    fi
    sleep 0.5
  done
  return 1
}

launch_client() {
  # 后端由客户端拉起，所以装完直接帮用户开一次客户端，服务就齐了。
  # 走 launch.sh 而不是直接跑 main.py：客户端在切游戏模式时会被 gamescope
  # 掐断 Wayland 连接而 abort，靠外壳脚本负责拉起和写崩溃栈。
  local launcher="$INSTALL_DIR/client/launch.sh"
  [ -x "$INSTALL_DIR/.venv/bin/python" ] && [ -f "$INSTALL_DIR/client/main.py" ] || return 1
  if [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then
    chmod +x "$launcher" 2>/dev/null || true
    setsid bash "$launcher" >/dev/null 2>&1 &
    return 0
  fi
  return 1
}

lan_ip() {
  # 优先挑私有网段的物理网卡：开了 Clash 等代理后，默认路由出口会是
  # 198.18.x.x 的虚拟地址，别的设备连不上
  "$INSTALL_DIR/.venv/bin/python" - <<'PY' 2>/dev/null || true
import socket

try:
    import psutil
except ImportError:
    psutil = None

best = None
if psutil:
    for name, addrs in psutil.net_if_addrs().items():
        low = name.lower()
        if any(k in low for k in ("meta", "tun", "tap", "docker", "veth",
                                  "br-", "virbr", "vmnet", "wg", "zt")):
            continue
        for a in addrs:
            if a.family != socket.AF_INET or not a.address:
                continue
            ip = a.address
            if ip.startswith(("192.168.", "10.")) or ip.startswith("172."):
                rank = 0 if low.startswith(("wlan", "wlp", "wl")) else 1
                if best is None or rank < best[0]:
                    best = (rank, ip)

if best:
    print(best[1])
else:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        print(s.getsockname()[0])
    finally:
        s.close()
PY
}

print_done() {
  local ip ve rv skipped pv
  ip=$(lan_ip)
  [ -n "$ip" ] || ip=$(hostname -I 2>/dev/null | awk '{print $1}')
  [ -n "$ip" ] || ip="<deck-ip>"
  ve=$(cat "$INSTALL_DIR/VERSION" 2>/dev/null || echo '未知')
  rv="$(running_version)"
  pv="$(plugin_installed_version 2>/dev/null || true)"
  card
  card_title "DeckMeido 已就绪  ·  v$ve"
  card_line ""
  card_line "运行版本     v${rv:-$ve}"
  if [ "$TTS_MODEL_STATE" = "unknown" ]; then
    if tts_model_present; then TTS_MODEL_STATE="ready"; else TTS_MODEL_STATE="missing"; fi
  fi
  case "$TTS_MODEL_STATE" in
    ready)   card_line "语音合成     已就绪（月读酱，中英试合成通过）" ;;
    failed)  card_line "语音合成     模型下载失败（已尝试 ${DOWNLOAD_ATTEMPTS} 次，原因：$(printf '%s' "${TTS_MODEL_ERROR:-未知}" | cut -c1-60)）" ;;
    broken)  card_line "语音合成     试合成失败（重跑安装即可再试）" ;;
    missing) card_line "语音合成     模型缺失（重跑安装即可补下）" ;;
    *)       card_line "语音合成     本次未选" ;;
  esac
  case "${STT_MODEL_STATE:-unknown}" in
    ready)  card_line "语音识别     已就绪（SenseVoice）" ;;
    failed) card_line "语音识别     模型未就绪（重跑安装可再试）" ;;
    *)
      if [ "${DECKMEIDO_STT_MODE:-install}" = "install" ]; then
        card_line "语音识别     未复查（重跑安装可补齐）"
      else
        card_line "语音识别     本次未选"
      fi ;;
  esac
  # 用户显式跳过的可选项（DECKMEIDO_SKIP_GUI 等）：一行列全
  if [ ${#SKIPPED_ITEMS[@]} -gt 0 ]; then
    skipped=$(IFS='、'; printf '%s' "${SKIPPED_ITEMS[*]}")
    card_line "已跳过：$skipped（相应功能降级，重跑安装即可补上）"
  fi
  # Decky 重启循环告警：plugin_reload 只能在当场 warn，这里收尾再单列一行，
  # 否则用户翻回几十行前的 [!] 才能看见，甚至直接漏掉
  if [ -n "$DECKY_LOOP_NOTE" ]; then
    card_line "⚠ Decky     $DECKY_LOOP_NOTE"
  fi
  # 插件到底有没有真的生效，必须在这里说清楚：「已安装」不等于「已加载」。
  # absent/skipped 也要单列：不然用户以为装好了，进 QAM 找不到入口却不知道该怪谁。
  case "${PLUGIN_RELOAD_STATE:-}" in
    ok)      card_line "Decky 插件   v${pv:-?} 已重载，游戏模式面板可用" ;;
    pending) card_line "Decky 插件   v${pv:-?} 已部署，待重载（请先重启 Deck，再到 Decky 设置点「重新载入」）" ;;
    failed)  card_line "Decky 插件   v${pv:-?} 已部署，但重载失败（请重启 Deck 后再重新载入）" ;;
    install_failed)
             card_line "Decky 插件   安装失败（文件没解开，QAM 里不会有入口）" ;;
    absent)  card_line "Decky 插件   未安装：没检测到 Decky Loader（可选依赖）" ;;
    skipped) card_line "Decky 插件   未安装：按你的选择跳过，zip 已放到工作区" ;;
    *)       [ -n "$pv" ] && card_line "Decky 插件   v${pv} 已部署" ;;
  esac
  # 音箱模式：装没装齐一句话说清，免得用户在设置页打开开关后才猜原因
  if deckmeido_speaker_ready; then
    card_line "音箱模式     已就绪（唤醒词 + 录音 + 媒体控制）"
  else
    card_line "音箱模式     组件不全（详见上文「智能音响模式」缺项，重跑安装即可补上）"
  fi
  # 系统控件缺了就是缺了：这一行必须和设置页「可补装组件」显示的一致，
  # 不能再出现「上面打勾、下面全红」的矛盾
  if [ ${#DEPS_MISSING_AFTER[@]} -gt 0 ] || [ "$DEPS_FAILED" = 1 ]; then
    card_line "系统控件     缺 $(IFS='、'; printf '%s' "${DEPS_MISSING_AFTER[*]:-见上文}")（联网后重跑安装即可补上）"
  fi
  card_line "本机控制台   http://127.0.0.1:$PORT/"
  card_line "局域网访问   http://$ip:$PORT/（本次掌机 IP；换了网络会变，以设置页显示为准）"
  card_line "配置文件     $DATA_CONFIG"
  card_line "用户数据     $DATA_DIR（卸载保留）"
  card_line ""
  card_line "首次打开地址会弹出设置页，填 API 地址与 Key 即可对话"
  card_line "以后升级     bash $INSTALL_DIR/install.sh upgrade"
  card_line "卸载         bash $INSTALL_DIR/uninstall.sh"
  card_line "             （只删安装器文件；userdata/ 永远保留）"
  card_end
  echo
  press_continue "按 A 或回车结束"
}

# ---------- 子命令 ----------
# 七步顺序按依赖排死，不可颠倒；每步只输出结论行，原始输出进
# userdata/logs/install-<时间>.log。依赖必须早于模型早于复查（试合成/试唤醒）。
do_install() {
  local deps="--deps" gui_only=0
  for a in "$@"; do
    case "$a" in
      --deps) deps="--deps" ;;
      --no-deps) deps="" ;;   # 仅调试用：显式不装系统控件
      --gui)  gui_only=1 ;;   # 只补装桌面客户端（PySide6），不动别的
    esac
  done
  check_user
  print_banner
  start_install_log
  # 默认全装：不再询问档位，能用到的组件一律装上。
  # 各模块先在清单里「检测」，已装好的到时自动跳过，不会重复下载。
  optional_downloads
  export DECKMEIDO_STT_MODE=$([ "${DECKMEIDO_INSTALL_STT:-1}" = "1" ] && echo install || echo skip)
  export DECKMEIDO_TTS_MODE=$([ "${DECKMEIDO_INSTALL_TTS:-1}" = "1" ] && echo install || echo skip)
  export DECKMEIDO_WAKE_MODE=$([ "${DECKMEIDO_INSTALL_SPEAKER:-1}" = "1" ] && echo install || echo skip)
  # 系统控件同时受命令行开关与逐项勾选约束，两者取「都同意才装」
  [ "${DECKMEIDO_INSTALL_DEPS:-1}" = "1" ] || deps=""
  STEP_QUIET=1
  reset_steps
  local disk_ver="" had_old=0

  # [1/8] 检查环境
  step "检查环境"
  check_prereq
  preflight
  step_ok "安装目录 $INSTALL_DIR，端口 $PORT"
  # 容量预估：把本次要占多少、每项多少先说清楚，再动手。
  # 只算真正会装的模块——未勾选的项不会下载，报了反而让人以为非要那么大空间。
  plan_keys="app server gui stt_mod tts_mod"
  [ -n "$deps" ] && plan_keys="$plan_keys sys"
  [ "${DECKMEIDO_SKIP_GUI:-0}" = "1" ] && plan_keys="${plan_keys/ gui/}"
  [ "$DECKMEIDO_STT_MODE" = "install" ] && plan_keys="$plan_keys stt_model"
  [ "$DECKMEIDO_TTS_MODE" = "install" ] && plan_keys="$plan_keys tts_model nltk"
  [ "$DECKMEIDO_WAKE_MODE" = "install" ] && plan_keys="$plan_keys wake_mod wake_model"
  plan_keys="$plan_keys plugin"
  print_capacity_plan "$plan_keys"
  step_ok "合计约 $(mb_text "$PLAN_TOTAL_MB")，预计 $(human_dur "$PLAN_SECONDS")；中途可随时 Ctrl+C，重跑会接着装"

  # [2/8] 停止旧进程
  # 换文件前必须没有旧后端占着端口。capture_install_baseline 只在第一次安装时抓，
  # 升级不覆盖；卸载靠它判断哪些东西是我们带来的。
  capture_install_baseline
  step "停止旧进程"
  [ -n "$(_backend_pids)" ] && had_old=1
  [ -n "$(_client_pids)" ] && had_old=1
  if ! stop_runtime; then
    step_fail "旧进程没停掉，为安全起见没有替换任何文件"
    retry_hint
    die "请先按上面的提示手动停掉旧进程，再重跑安装"
  fi
  if [ "$had_old" = 1 ]; then
    step_ok "旧后端与客户端已停止"
  else
    step_skip "没有运行中的后端 / 客户端"
  fi

  # [3/8] 安装程序文件
  step "安装程序文件"
  sync_files
  disk_ver="$(head -n1 "$INSTALL_DIR/VERSION" 2>/dev/null || true)"
  ensure_data_layout
  prepare_config
  restore_soul
  ensure_workspace || true
  local app_mb
  app_mb=$(path_mb "$INSTALL_DIR/server")
  step_ok "已就位：$INSTALL_DIR（v${disk_ver:-未知}，server $(mb_text "$app_mb")）"
  step_ok "配置已就绪：$DATA_CONFIG"

  # [4/8] 检测依赖：一次性扫完全部依赖，只报缺什么，不装任何东西
  step "检测依赖"
  local py="$INSTALL_DIR/.venv/bin/python"
  stage_detect_all "$py" "$deps"

  # [5/8] 安装依赖与模型：按 [4/8] 的结果一次装完，中途不复查
  step "安装依赖与模型"
  if stage_install_all "$py" "$deps"; then
    step_ok "依赖与模型已装完"
  else
    step_fail "有项目没装成功，明细见上"
  fi

  # [6/8] 功能复查：只查不装，缺什么在这里如实列出
  step "功能复查"
  if stage_verify_all "$py" "$deps"; then
    step_ok "复查通过"
  else
    step_fail "复查有未通过项"
    retry_hint
  fi
  # 依赖已装完、也不再需要下载：立刻清缓存，别把 1GB 临时文件留在硬盘上
  purge_pip_cache

  # [7/8] 部署插件与桌面入口；[8/8] 启动后端
  step "部署插件与桌面入口"
  install_desktop
  install_uninstaller
  install_plugin
  if decky_ready 2>/dev/null; then
    local _dd
    while IFS= read -r _dd; do
      mkdir -p "$_dd" 2>/dev/null || true
    done < <(decky_plugin_dirs)
  fi
  write_install_records
  step_ok "桌面入口、Decky 插件与安装清单已写入（插件 $(mb_text "$(dep_mb plugin)")）"

  step "启动后端"
  if [ "${DECKMEIDO_SKIP_SERVER:-0}" = "1" ]; then
    step_skip "已跳过启动后端（服务端依赖未安装）"
    retry_hint
    print_done
    return 0
  fi
  if ! relaunch_runtime; then
    step_fail "后端没能启动"
    next_hint "手动启动：cd $INSTALL_DIR && .venv/bin/python -m uvicorn server.main:app --host 127.0.0.1 --port $PORT"
    retry_hint
    die "安装已完成，但后端没有起来"
  fi
  if ! verify_running_version "${disk_ver:-0.0.0}"; then
    die "安装已完成，但运行中的版本不是 v$disk_ver"
  fi
  print_done
}

# 停掉客户端：客户端是后端的父进程，还会在 watchdog 里自动补拉后端。
# 升级过程中它要是还活着，会一边同步代码一边被重新拉起旧后端，
# 端口和内存里的历史都对不上。升级完成后由 launch_client 重新开。
stop_client() {
  local pids pid i
  # 与后端同一判据：同一 UID + 命令行含 client/main.py，不要求路径匹配，
  # 这样从旧目录（甚至已被删除的目录）拉起的客户端也能收掉。
  pids="$(_client_pids)"
  [ -n "$pids" ] || return 0
  dim "关闭客户端（升级后自动重开）"
  # 先收掉 launch.sh 的自动重启，否则它看到客户端被杀会立刻再拉起一个
  pkill -f "$INSTALL_DIR/client/launch.sh" 2>/dev/null || true
  # shellcheck disable=SC2086
  kill -TERM $pids 2>/dev/null || true
  for i in $(seq 1 20); do
    local alive=0
    for pid in $pids; do
      kill -0 "$pid" 2>/dev/null && alive=1
    done
    [ "$alive" = "0" ] && return 0
    sleep 0.2
  done
  # shellcheck disable=SC2086
  kill -KILL $pids 2>/dev/null || true
}

do_upgrade() {
  local deps="--deps" gui_only=0
  for a in "$@"; do
    case "$a" in
      --deps) deps="--deps" ;;
      --no-deps) deps="" ;;
      --gui)  gui_only=1 ;;
    esac
  done
  if [ "$gui_only" = 1 ]; then deps=""; fi
  [ -d "$INSTALL_DIR" ] || die "$INSTALL_DIR 不存在，请先执行安装"
  check_user
  print_banner
  # 升级只换代码和依赖，下面这些一律保留，用户不用担心升级丢数据
  dim "升级会保留：userdata/ 下的配置、人设、长期记忆、会话与上传文件，"
  dim "以及工作区；Decky 插件已装过会自动替换为新版本。"
  start_install_log
  # 默认全装：缺什么补什么，已装好的自动跳过；--gui 只补桌面客户端，不动别的
  [ "$gui_only" = 1 ] || optional_downloads
  export DECKMEIDO_STT_MODE=$([ "${DECKMEIDO_INSTALL_STT:-1}" = "1" ] && echo install || echo skip)
  export DECKMEIDO_TTS_MODE=$([ "${DECKMEIDO_INSTALL_TTS:-1}" = "1" ] && echo install || echo skip)
  export DECKMEIDO_WAKE_MODE=$([ "${DECKMEIDO_INSTALL_SPEAKER:-1}" = "1" ] && echo install || echo skip)
  [ "${DECKMEIDO_INSTALL_DEPS:-1}" = "1" ] || deps=""
  STEP_QUIET=1
  reset_steps
  local disk_ver="" had_old=0

  # [1/8] 检查环境
  step "检查环境"
  check_prereq
  preflight
  step_ok "安装目录 $INSTALL_DIR，端口 $PORT"
  if [ "$gui_only" = 0 ]; then
    # 只算真正会装机 / 会下载的模块
    plan_keys="server gui stt_mod tts_mod"
    [ -n "$deps" ] && plan_keys="$plan_keys sys"
    [ "${DECKMEIDO_SKIP_GUI:-0}" = "1" ] && plan_keys="${plan_keys/ gui/}"
    [ "$DECKMEIDO_STT_MODE" = "install" ] && plan_keys="$plan_keys stt_model"
    [ "$DECKMEIDO_TTS_MODE" = "install" ] && plan_keys="$plan_keys tts_model nltk"
    [ "$DECKMEIDO_WAKE_MODE" = "install" ] && plan_keys="$plan_keys wake_mod wake_model"
    print_capacity_plan "$plan_keys"
    step_ok "合计约 $(mb_text "$PLAN_TOTAL_MB")，预计 $(human_dur "$PLAN_SECONDS")；中途可随时 Ctrl+C，重跑会接着装"
  fi

  # [2/8] 停止旧进程
  # 顺序要紧：先停客户端（否则它的 watchdog 会把旧后端拉回来），
  # 再把可能已经「脱管」的后端进程也收掉，保证换文件时没有旧进程占着端口
  step "停止旧进程"
  [ -n "$(_backend_pids)" ] && had_old=1
  [ -n "$(_client_pids)" ] && had_old=1
  if ! stop_runtime; then
    step_fail "旧进程没停掉，为安全起见没有替换任何文件"
    retry_hint
    die "请先按上面的提示手动停掉旧进程，再重跑升级"
  fi
  if [ "$had_old" = 1 ]; then
    step_ok "旧后端与客户端已停止"
  else
    step_skip "没有运行中的后端 / 客户端"
  fi

  # [3/8] 安装程序文件
  step "安装程序文件"
  sync_files
  disk_ver="$(head -n1 "$INSTALL_DIR/VERSION" 2>/dev/null || true)"
  ensure_data_layout
  ensure_workspace || true
  local app_mb
  app_mb=$(path_mb "$INSTALL_DIR/server")
  step_ok "已就位：$INSTALL_DIR（v${disk_ver:-未知}，server $(mb_text "$app_mb")）"

  # [4/8] 检测依赖（与全新安装同一套顺序）
  step "检测依赖"
  local py="$INSTALL_DIR/.venv/bin/python"
  if [ "$gui_only" = 1 ]; then
    module_gui "$py"
    write_install_records
    print_done
    return 0
  fi
  stage_detect_all "$py" "$deps"

  # [5/8] 安装依赖与模型：一次装完，中途不复查
  step "安装依赖与模型"
  if stage_install_all "$py" "$deps"; then
    step_ok "依赖与模型已装完"
  else
    step_fail "有项目没装成功，明细见上"
  fi

  # [6/8] 功能复查：只查不装
  step "功能复查"
  if stage_verify_all "$py" "$deps"; then
    step_ok "复查通过"
  else
    step_fail "复查有未通过项"
    retry_hint
  fi
  # 依赖已装完、也不再需要下载：立刻清缓存
  purge_pip_cache

  # [7/8] 部署插件与桌面入口；[8/8] 启动后端
  step "部署插件与桌面入口"
  install_desktop
  install_uninstaller
  install_plugin
  write_install_records
  step_ok "桌面入口、Decky 插件与安装清单已更新"

  step "启动后端"
  if [ "${DECKMEIDO_SKIP_SERVER:-0}" = "1" ]; then
    step_skip "已跳过启动后端（服务端依赖未安装）"
    retry_hint
    print_done
    return 0
  fi
  if ! relaunch_runtime; then
    step_fail "后端没能启动"
    next_hint "手动启动：cd $INSTALL_DIR && .venv/bin/python -m uvicorn server.main:app --host 127.0.0.1 --port $PORT"
    retry_hint
    die "升级已完成，但后端没有起来"
  fi
  if ! verify_running_version "${disk_ver:-0.0.0}"; then
    die "升级已完成，但运行中的版本不是 v$disk_ver"
  fi
  print_done
}

# ---------- 远程发布包安装（镜像优先，GitHub 兜底） ----------
# 管道执行 / GitHub 不通 / 没有现成源码目录时：拉一份最新的发布包，解到临时目录，
# 再交给正常的 install 流程（等价于自己下载解压后跑 bash install.sh）。
GH_RELEASES="https://github.com/$GITHUB_REPO/releases"
GH_LATEST="$GH_RELEASES/latest/download"

json_tag() {
  curl -fsSL -m 15 "$1" 2>/dev/null |
    grep -oE '"tag"[[:space:]]*:[[:space:]]*"[^"]+"' | head -n1 |
    sed -E 's/.*"([^"]+)"$/\1/' | sed 's/^v//'
}

github_latest_tag() { json_tag "$GH_LATEST/latest.json"; }
mirror_latest_tag() { json_tag "$MIRROR/latest.json"; }

# curl_progress <url> <输出文件> [显示名]
# 边下边报：文件名 / 已下载 / 总大小 / 百分比 / 速度。裸 curl -o 在 Deck 小屏上
# 毫无反馈，几十 MB 的包会被当成卡死。
# 非终端（重定向/日志）时每跨 10% 打一行，避免刷屏。
curl_progress() {
  local url="$1" dest="$2" label="${3:-$(basename "$2")}"
  local total pid rc got pct=-1 rate=0 eta=0 t0 now dt last_t last_got=0
  local stall=0 stall_warned=0 last_report=-15 reported=0
  total=$(curl -fsSIL -m 20 "$url" 2>/dev/null | tr -d '\r' \
    | awk 'tolower($1)=="content-length:"{v=$2} END{print v+0}')
  total=${total:-0}
  : > "$dest" 2>/dev/null || true
  # 大包（PySide6 等）用更长上限，避免 600 秒被硬截断
  # -sS：关掉 curl 自带的进度条，进度统一由下面的循环输出；出错仍会打印原因
  curl -fL -sS --retry 2 -m 3600 -o "$dest" "$url" &
  pid=$!
  t0=$SECONDS
  last_t=$t0
  while kill -0 "$pid" 2>/dev/null; do
    got=$(stat -c %s "$dest" 2>/dev/null || echo 0)
    got=${got:-0}
    now=$SECONDS
    dt=$((now - last_t))
    # 速度按真实秒数算：以前用的 0.5 秒增量被当成字节/秒，显示速度只有实际的一半
    if [ "$dt" -ge 1 ]; then
      rate=$(((got - last_got) / dt))
      [ "$rate" -lt 0 ] && rate=0
      last_got=$got
      last_t=$now
      if [ "$rate" -eq 0 ] && [ "$got" -gt 0 ]; then
        stall=$((stall + dt))
      else
        stall=0
        stall_warned=0
      fi
      # 15 秒没有任何新数据：明确说一次卡住，数据恢复后重置，避免逐秒刷屏
      if [ "$stall" -ge 15 ] && [ "$stall_warned" = 0 ]; then
        warn "下载似乎卡住了：${stall} 秒没有新数据（$label）"
        stall_warned=1
      fi
    fi
    if [ "$total" -gt 0 ]; then
      pct=$((got * 100 / total)); [ "$pct" -gt 100 ] && pct=100
      if [ "$rate" -gt 0 ] && [ "$got" -lt "$total" ]; then
        eta=$(((total - got) / rate))
      else
        eta=0
      fi
    else
      pct=-1
      eta=0
    fi
    local size_text eta_text spd_text pct_text stall_text=""
    size_text="$(human_size "$got")"
    if [ "$total" -gt 0 ]; then
      size_text="$(human_size "$got")/$(human_size "$total")"
      pct_text="${pct}%"
    else
      size_text="$(human_size "$got")（总大小未知）"
      pct_text="--"
    fi
    if [ "$rate" -gt 0 ]; then
      spd_text="$(human_rate "$rate")"
    else
      spd_text="0MB/s"
    fi
    if [ "$eta" -gt 0 ]; then
      eta_text="剩余 $(human_dur "$eta")"
    elif [ "$total" -gt 0 ]; then
      eta_text="剩余 --"
    else
      eta_text="剩余时间未知"
    fi
    # 15 秒没有任何新数据：明确提示卡住，而不是继续假装在跑
    [ "$stall" -ge 15 ] && stall_text="  [下载似乎卡住了：${stall} 秒没有新数据]"
    if [ "$OUT_IS_TTY" = 1 ]; then
      printf '\r\033[K%s%s%s  %s  %s  %s  %s%s  %s%s' \
        "$C_C" '.' "$C_END" "$label" "$size_text" "$pct_text" \
        "$spd_text" "" "$eta_text" "$stall_text"
    elif [ "$((pct / 10))" != "$((reported / 10))" ] && [ "$pct" -ge 0 ]; then
      printf '    [%s%%] %s  %s  %s  %s%s\n' "$pct" "$size_text" "$spd_text" \
        "$eta_text" "$label" "$stall_text"
      reported=$pct
      last_report=$now
    elif [ "$pct" -lt 0 ] && [ "$got" -gt 0 ] && [ $((now - last_report)) -ge 5 ]; then
      # 总大小未知时没有百分比可跨：每 5 秒报一次已下载量与速度，避免看起来没动；
      # 还没拿到数据（0B）那一瞬间不刷屏
      printf '    已下载 %s  %s  %s  %s%s\n' "$size_text" "$spd_text" "$eta_text" "$label" "$stall_text"
      last_report=$now
    fi
    sleep 0.5
  done
  wait "$pid"; rc=$?
  if [ "$OUT_IS_TTY" = 1 ]; then printf '\r\033[K'; fi
  [ "$rc" = 0 ] || return "$rc"
  if [ ! -s "$dest" ]; then
    warn "下载得到的文件是空的：$url"
    return 1
  fi
  return 0
}

# 下载发布包并校验。$4=1 表示必须有 .sha256（GitHub 路径），校验缺失或失败都算失败；
# 镜像路径沿用旧行为：拿不到校验文件就跳过，只打提示。
fetch_pkg() {
  local base="$1" tag="$2" tmp="$3" require_sha="$4"
  local url="$base/deckmeido-$tag.tar.gz"
  rm -f "$tmp/pkg.tar.gz" "$tmp/pkg.sha256"
  info "下载 $url"
  curl_progress "$url" "$tmp/pkg.tar.gz" "deckmeido-$tag.tar.gz" || {
    warn "下载失败：$url"
    return 1
  }
  if curl -fsSL -m 15 -o "$tmp/pkg.sha256" "$url.sha256" 2>/dev/null; then
    ( cd "$tmp" && sed "s|deckmeido-$tag.tar.gz|pkg.tar.gz|" pkg.sha256 | sha256sum -c - >/dev/null 2>&1 ) || {
      warn "sha256 校验不通过：$url"
      return 1
    }
    dim "sha256 校验通过"
  elif [ "$require_sha" = 1 ]; then
    warn "拿不到校验文件：$url.sha256"
    return 1
  else
    warn "没有拿到校验文件，跳过校验：$url.sha256"
  fi
  return 0
}

# 管道执行时装/升/卸怎么定：没装 → 直接装；装了 → 必须手选 1/2，不设默认。
# 不给默认值：回车就升级会让想卸载的人手滑覆盖安装，卸载入口等于藏起来。
# 结果写 REMOTE_ACTION（不用命令替换捕获：横幅与提示都走 stdout 会被混进来）。
REMOTE_ACTION=""
remote_pick_action() {
  print_banner
  if [ ! -f "$INSTALL_DIR/VERSION" ]; then
    info "未检测到已安装的 $APP，直接开始全新安装（$INSTALL_DIR）"
    REMOTE_ACTION=install
    return 0
  fi
  local ver; ver=$(head -n1 "$INSTALL_DIR/VERSION" 2>/dev/null || true)
  info "检测到已安装：$APP${ver:+ v$ver}（$INSTALL_DIR）"
  echo
  echo "  请选择要执行的操作："
  echo "  ${C_B}1${C_END}  升级到最新版  ${C_DIM}（保留配置、人设与记忆）${C_END}"
  echo "  ${C_B}2${C_END}  卸载          ${C_DIM}（删代码，人设与记忆会备份）${C_END}"
  echo "  ${C_B}0${C_END}  退出"
  echo
  local choice
  # 不写默认值：拿不到终端或读失败一律退出，绝不替用户选升级
  choice=$(ask_choice "请输入序号（1 升级 / 2 卸载 / 0 退出；掌机用屏幕键盘输入后按 A（回车）确认）：") || {
    echo >&2
    warn "没有可用的终端，无法确认要升级还是卸载"
    dim "带子命令直接指定：bash install.sh upgrade / bash install.sh uninstall"
    return 3
  }
  case "$choice" in
    1) REMOTE_ACTION=upgrade ;;
    2) REMOTE_ACTION=uninstall ;;
    0|""|q|Q) REMOTE_ACTION=quit ;;
    *) return 4 ;;
  esac
}

# 处理「装 / 升 / 卸」选择；卸载与退出在这里直接结束，其余返回时 REMOTE_ACTION 已就绪。
remote_action_or_exit() {
  local rc=0
  REMOTE_ACTION=""
  remote_pick_action || rc=$?
  if [ "$rc" != 0 ]; then
    [ "$rc" = 3 ] && exit 0
    die "无效选择，已退出（输入 1 升级 / 2 卸载 / 0 退出）"
  fi
  case "$REMOTE_ACTION" in
    quit) info "已取消"; exit 0 ;;
    uninstall)
      # 卸载不需要安装包：直接用这张脚本自带的 do_uninstall，
      # 免得为了删代码先下载几十 MB
      do_uninstall
      exit 0 ;;
  esac
}

# 显式指定 from-mirror 时只走镜像站。
do_from_mirror() {
  need_cmd curl || die "需要 curl：先装 curl（sudo pacman -S curl）"
  remote_action_or_exit

  print_banner
  info "镜像站：$MIRROR"
  local tag; tag="$(mirror_latest_tag || true)"
  [ -n "$tag" ] || die "读不到镜像站的版本信息（$MIRROR/latest.json），请检查网络或稍后再试"
  info "最新版本：v$tag"
  # 临时目录用脚本级变量，原因见 do_from_piped 里的说明
  PIPED_TMP="$(mktemp -d)"
  local tmp="$PIPED_TMP"
  trap 'rm -rf "${PIPED_TMP:-}"' EXIT
  fetch_pkg "$MIRROR" "$tag" "$tmp" 0 || die "下载失败：$MIRROR/deckmeido-$tag.tar.gz"
  tar -xzf "$tmp/pkg.tar.gz" -C "$tmp" || die "解压失败"
  local src; src="$(find "$tmp" -maxdepth 2 -name install.sh -printf '%h\n' | head -n1)"
  [ -n "$src" ] || die "安装包结构不对：里面没有 install.sh"
  info "开始执行：$REMOTE_ACTION（来源：$src）"
  # 让子进程沿用这次用的镜像地址，写进配置的就是它。
  # 子命令明确传下去：上面已经问过「升级还是卸载」，不能让子脚本再猜一次。
  # DECKMEIDO_BANNER_SHOWN 一并导出：大字已经在本进程打过了，子脚本再打一遍
  # 会把首屏顶出可视区（用户看到的就是「logo 滚走了」）。
  export DECKMEIDO_BANNER_SHOWN=1
  DECKMEIDO_MIRROR="$MIRROR" bash "$src/install.sh" "$REMOTE_ACTION" "$@"
}

# 管道执行 / 一行命令自举：先镜像，镜像不可用再 GitHub。
do_from_piped() {
  need_cmd curl || die "需要 curl：先装 curl（sudo pacman -S curl）"

  print_banner
  # 动作先问（会再调一次 print_banner，已由上面的去重挡掉），再报网络进度，
  # 这样首屏先出大字+菜单，而不是先刷两行版本信息再把 logo 顶走。
  remote_action_or_exit
  # 临时目录用脚本级变量：trap 要等脚本退出才执行，那时函数里的 local
  # 已经出栈，set -u 下展开 $tmp 会报「tmp: 未绑定的变量」（用户看到的
  # 就是按 A 结束后闪出的一条 bash 报错）。改用全局变量 + :- 兜底。
  PIPED_TMP="$(mktemp -d)"
  local tmp="$PIPED_TMP"
  trap 'rm -rf "${PIPED_TMP:-}"' EXIT

  local tag="" origin=""
  # 1) 镜像主路径：沿用原有 latest.json + tar.gz + sha256 逻辑
  tag="$(mirror_latest_tag || true)"
  if [ -n "$tag" ]; then
    info "镜像站最新版本：v$tag"
    if fetch_pkg "$MIRROR" "$tag" "$tmp" 0; then
      origin="镜像站"
    else
      warn "镜像站安装包不可用，改用 GitHub Release"
    fi
  else
    warn "读不到镜像站的版本信息（$MIRROR/latest.json），改用 GitHub Release"
  fi

  # 2) GitHub 兜底：必须拿到 latest.json，并且 tar.gz 与 .sha256 都校验通过
  if [ -z "$origin" ]; then
    tag="$(github_latest_tag || true)"
    if [ -n "$tag" ]; then
      info "GitHub 最新版本：v$tag"
      if fetch_pkg "$GH_LATEST" "$tag" "$tmp" 1; then
        origin="GitHub"
      else
        die "镜像站与 GitHub 都下载失败：$GH_LATEST/deckmeido-$tag.tar.gz"
      fi
    else
      die "镜像站与 GitHub 都读不到版本信息（$MIRROR/latest.json / $GH_LATEST/latest.json），请检查网络或稍后再试"
    fi
  fi

  tar -xzf "$tmp/pkg.tar.gz" -C "$tmp" || die "解压失败"
  local src; src="$(find "$tmp" -maxdepth 2 -name install.sh -printf '%h\n' | head -n1)"
  [ -n "$src" ] || die "安装包结构不对：里面没有 install.sh"
  info "开始执行：$REMOTE_ACTION（来源：$src，包来自$origin）"
  # 镜像地址仍写进配置，供软件内更新作为兜底源；本次安装包来源由 origin 决定。
  # 大字已在本进程打过，导出标记让子脚本别再打一次（否则首屏被顶出可视区）。
  export DECKMEIDO_BANNER_SHOWN=1
  DECKMEIDO_MIRROR="$MIRROR" bash "$src/install.sh" "$REMOTE_ACTION" "$@"
}

# 正在跑的任务要先取消：卸载时把后端直接杀掉，正在执行的命令会变成孤儿进程
cancel_running_turn() {
  need_cmd curl || return 0
  curl -fsS -m 2 -X POST "http://127.0.0.1:$PORT/api/cancel" >/dev/null 2>&1 || true
}

# 工作区真实路径：优先读配置里的 [workspace].dir，没有才退回默认目录。
# 以前这里 grep 的是 `workspace = "..."`，而配置里真正的键是 [workspace] 下的 dir，
# 所以自定义工作区从来没被认到过。
workspace_dir() {
  local cfg="$DATA_CONFIG" ws=""
  if [ -f "$cfg" ] && need_cmd python3; then
    ws=$(python3 - "$cfg" <<'PY' 2>/dev/null || true
import sys, tomllib
try:
    with open(sys.argv[1], "rb") as f:
        d = tomllib.load(f)
except Exception:
    raise SystemExit(0)
print((d.get("workspace") or {}).get("dir") or "")
PY
)
  fi
  if [ -z "$ws" ] && [ -f "$cfg" ]; then
    ws=$(awk '/^\[workspace\]/{f=1;next} /^\[/{f=0} f && /^[[:space:]]*dir[[:space:]]*=/{print; exit}' \
      "$cfg" 2>/dev/null | sed -E 's/^[^=]*=[[:space:]]*"([^"]*)".*/\1/' || true)
  fi
  [ -n "$ws" ] || ws="${DECKMEIDO_WORKSPACE:-$HOME/MeidoSpace}"
  # 配置里可能写的是相对路径或 ~，统一展开
  case "$ws" in
    "~"/*) ws="$HOME/${ws#\~/}" ;;
    /*) ;;
    *) ws="$HOME/$ws" ;;
  esac
  printf '%s\n' "$ws"
}

# 安装完成后，若 ~/.config/deckmeido/ 里有之前卸载时留下的人设/记忆备份，
# 就搬回 server/ 目录，让「她」还是原来的她。
restore_soul() {
  local src="$CONFIG_DIR"
  local restored=0
  ensure_data_layout
  if [ -f "$src/meidosoul.json" ] && [ ! -f "$DATA_PERSONA" ]; then
    cp -f "$src/meidosoul.json" "$DATA_PERSONA" 2>/dev/null && restored=1
  fi
  if [ -f "$src/memory.json" ] && [ ! -f "$DATA_MEMORY" ]; then
    cp -f "$src/memory.json" "$DATA_MEMORY" 2>/dev/null && restored=1
  fi
  if [ "$restored" = 1 ]; then
    dim "已恢复之前的人设与记忆（$src）"
  fi
  return 0
}

# ---------- 系统包取证（pacman.log + pacman -Qi + 包目录签名） ----------
# 无安装清单的老安装只能从 pacman.log 取证，按安装时间窗分三类：
#   W 窗口内新装 → 候选可删（仍需三方交叉验证）
#   U 窗口内升级但原本已有 → 只记录，永不删
#   O 早于窗口 → 永不删（SteamOS 基线 / 用户自装）
# 交叉验证三条全中才删：log 在窗内 + pacman -Qi Install Date 不早于窗口 + 落盘文件带签名。
# 不写死 SteamOS 包名表。清单直接复用 DEPS_*，避免两处漏同步。
PAC_DEPS=("${DEPS_PACMAN[@]}" "${DEPS_AUR[@]}" "${DEPS_AUR_FALLBACK[@]}")

# 包落盘文件里是否有落在安装/配置目录且存在的 → 说明它写过我们的地盘。
# 无痕迹只表示证据少一条，不代表不能删。
pac_pkg_has_signature() {
  local p="$1" f
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    case "$f" in
      "$INSTALL_DIR"/*|"$CONFIG_DIR"/*) [ -e "$f" ] && { printf '%s' "$f"; return 0; } ;;
    esac
  done < <(pacman -Qlq -- "$p" 2>/dev/null)
  return 1
}

pac_log_file() {
  local f
  # PACMAN_LOG 只给沙盒测试用，正常路径固定看系统日志
  if [ -n "${PACMAN_LOG:-}" ] && [ -r "${PACMAN_LOG:-}" ]; then
    printf '%s\n' "$PACMAN_LOG"; return 0
  fi
  for f in /var/log/pacman.log /var/log/pacman.log.1; do
    [ -r "$f" ] && { printf '%s\n' "$f"; return 0; }
  done
  return 1
}

# 安装时间窗起点：清单里的 installed_at，没有就退回 .git 的首次提交时间
uninstall_window_start() {
  local v=""
  v="$(sed -n 's/^installed_at=//p' "$INSTALL_MARKER" 2>/dev/null | head -n1)"
  if [ -z "$v" ] && [ -d "$INSTALL_DIR/.git" ] && need_cmd git; then
    v="$(git -C "$INSTALL_DIR" log --reverse --format=%cI 2>/dev/null | head -n1)"
  fi
  if [ -z "$v" ] && [ -d "$HERE/.git" ] && need_cmd git; then
    v="$(git -C "$HERE" log --reverse --format=%cI 2>/dev/null | head -n1)"
  fi
  printf '%s\n' "$v"
}

# pacman -Qi 的 Install Date → 可比较的时间串（YYYY-MM-DDTHH:MM:SS）
pac_install_date() {
  pacman -Qi -- "$1" 2>/dev/null | sed -n 's/^Install Date *: *//p' | head -n1
}

# 输出每个候选包的判定，一行一条：<分类>|<包名>|<版本>|<原因>
# 分类：W=窗口内新装 U=窗口内升级 O=窗口外 E=查不到
pac_classify_deps() {
  local logfile="$1" t0="$2" p ver ev t action
  local -A first_ever=() inst_in_win=() ver_of=() ever=()
  [ -n "$logfile" ] || { return 0; }
  while IFS= read -r line; do
    case "$line" in
      \[*\]\ \[ALPM\]*) ;;
      *) continue ;;
    esac
    t="$(printf '%s\n' "$line" | sed -nE 's/^\[([^]]+)\] \[ALPM\] .*/\1/p')"
    [ -n "$t" ] || continue
    action="$(printf '%s\n' "$line" | sed -nE 's/^\[[^]]+\] \[ALPM\] (installed|upgraded|reinstalled|removed) .*/\1/p')"
    [ -n "$action" ] || continue
    p="$(printf '%s\n' "$line" | sed -nE 's/^\[[^]]+\] \[ALPM\] (installed|upgraded|reinstalled|removed) ([^ ]+) .*/\2/p')"
    ver="$(printf '%s\n' "$line" | sed -nE 's/^\[[^]]+\] \[ALPM\] (installed|upgraded|reinstalled|removed) [^ ]+ \(([^)]*)\).*/\2/p')"
    [ -n "$p" ] || continue
    # 只关心安装器签名的那几个包，日志可以很大
    case " ${PAC_DEPS[*]} " in
      *" $p "*) ;;
      *) continue ;;
    esac
    ever[$p]=1
    case "$action" in
      installed)
        # first_ever 只认 installed：窗口内出现 installed 说明是这回新装的；
        # 窗口内只出现 upgraded 说明安装前就已经有了
        [ -n "${first_ever[$p]:-}" ] || first_ever[$p]="$t"
        [ -n "$t0" ] && [[ ! "$t" < "$t0" ]] && inst_in_win[$p]=1
        ;;
    esac
    [ -n "$ver" ] && ver_of[$p]="$ver"
  done < "$logfile"
  for p in "${PAC_DEPS[@]}"; do
    need_cmd pacman && pacman -Qq -- "$p" >/dev/null 2>&1 || continue
    ver="$(pacman -Q -- "$p" 2>/dev/null | awk '{print $2}')"
    ev="${first_ever[$p]:-}"
    if [ -z "${ever[$p]:-}" ]; then
      printf 'E|%s|%s|pacman.log 里没有记录\n' "$p" "${ver:-?}"
      continue
    fi
    # 时间窗比较：日志是 2026-11-01T12:00:00+0800 这种，按字典序比较即可
    if [ -n "${inst_in_win[$p]:-}" ]; then
      printf 'W|%s|%s|%s 新装\n' "$p" "${ver:-?}" "$ev"
    elif [ -n "$ev" ] && [[ "$ev" < "$t0" ]]; then
      printf 'O|%s|%s|%s 就已在系统里\n' "$p" "${ver:-?}" "$ev"
    else
      # 窗口内只有 upgraded / reinstalled 记录：安装前就已经有了，只是被我们升级过
      printf 'U|%s|%s|窗口内只被升级，安装前已存在\n' "$p" "${ver:-?}"
    fi
  done
}

# 可选：卸载安装器亲自装过的系统包。默认卸载（回车=删，显式 n 才保留）；
# 只认 DEP_RECORD 清单与 pacman 取证里「窗口内新装」的包，
# 绝不碰 SteamOS 自带组件或用户原有的包，不用 -Rns 级联删。
# 删包前列清单并询问，--purge 也不静默删。do_ask=0 只在 dry-run 下用（只打印）。
uninstall_optional_deps() {
  local t0="${1:-}" do_ask="${2:-1}"
  local p ev line cls reason ver src
  local -a cand_pkg=() cand_why=() keep_pkg=() keep_why=()
  local -a picked=() helper pkg_del=() aur_del=()
  local -A seen=()

  if [ -f "$DEP_RECORD" ]; then
    while IFS=$'\t' read -r p ev _ || [ -n "$p" ]; do
      [ -n "$p" ] || continue
      case "$p" in *[!A-Za-z0-9@._+-]*) continue ;; esac
      seen[$p]=1
    done < "$DEP_RECORD"
  fi
  local logfile=""
  logfile="$(pac_log_file || true)"
  if [ -n "$logfile" ] && [ -n "$t0" ]; then
    while IFS='|' read -r cls p ver reason; do
      [ -n "$p" ] || continue
      case "$cls" in W) seen[$p]=1 ;; esac
      [ "$cls" = W ] && case "$p" in kdotool*) seen[kdotool-bin]=1 ;; esac
    done < <(pac_classify_deps "$logfile" "$t0")
  fi

  if ! need_cmd pacman; then
    dim "没有 pacman（不是 Arch 系），跳过系统包检查"
    return 0
  fi
  for p in $(printf '%s\n' "${!seen[@]}" | sort); do
    pacman -Qq -- "$p" >/dev/null 2>&1 || continue
    ver="$(pacman -Q -- "$p" 2>/dev/null | awk '{print $2}')"
    # 只有六个签名包在范围内，别的一律不碰
    case " ${PAC_DEPS[*]} " in *" $p "*) ;; *) continue ;; esac
    # 交叉验证 1：日志事件分类（W/U/O/E）
    cls="E"
    reason="pacman.log 里查不到安装记录"
    if [ -n "$logfile" ]; then
      line="$(pac_classify_deps "$logfile" "$t0" | grep -F -m1 -- "|$p|" || true)"
      if [ -n "$line" ]; then
        cls="${line%%|*}"
        reason="${line##*|}"
      fi
    fi
    # 交叉验证 2：pacman -Qi 的 Install Date 早于时间窗 → 原本就有，永不删
    ev="$(pac_install_date "$p")"
    if [ -n "$t0" ] && [ -n "$ev" ] && [[ "$ev" < "${t0:0:19}" ]]; then
      keep_pkg+=("$p"); keep_why+=("Install Date $ev 早于安装时间，原本就有"); continue
    fi
    case "$cls" in
      O) keep_pkg+=("$p"); keep_why+=("$reason"); continue ;;
      U) keep_pkg+=("$p"); keep_why+=("$reason"); continue ;;
    esac
    # 交叉验证 3：包落盘里有没有 DeckMeido 痕迹（少这条只降级提示，不直接排除）
    src="$(pac_pkg_has_signature "$p" || true)"
    if [ -z "$src" ] && [ "$cls" != W ]; then
      # 既查不到日志事件、又没有落盘痕迹：不碰
      keep_pkg+=("$p"); keep_why+=("$reason，也没有落盘痕迹，无法确认来源"); continue
    fi
    if [ -n "$src" ]; then
      reason="$reason；落盘签名 ${src#"$INSTALL_DIR"/}"
    fi
    cand_pkg+=("$p"); cand_why+=("$reason")
  done

  if [ ${#keep_pkg[@]} -gt 0 ]; then
    echo
    info "这些系统包 保留 不动（不是本次安装带来的）："
    for p in "${!keep_pkg[@]}"; do
      dim "  · ${keep_pkg[$p]}  ${keep_why[$p]}"
    done
  fi
  if [ ${#cand_pkg[@]} -eq 0 ]; then
    [ ${#keep_pkg[@]} -eq 0 ] && dim "没有需要处理的系统包"
    return 0
  fi
  echo
  info "可以删除的系统包（安装器窗口内新装，已三方核对）："
  local i
  for i in "${!cand_pkg[@]}"; do
    printf '  %s·%s %-12s %s%s%s\n' "$C_B" "$C_END" "${cand_pkg[$i]}" "$C_DIM" "${cand_why[$i]}" "$C_END"
  done
  dim "不会连带删除任何依赖（禁止 -Rns / 孤儿清理）；SteamOS 原有包一律不动。"
  if [ "$do_ask" = 0 ] || [ "$DRY_RUN" = 1 ]; then
    for p in "${cand_pkg[@]}"; do printf '[计划] 询问后卸载系统包：%s\n' "$p"; done
    return 0
  fi
  # 默认值必须是「卸载」：直接回车 / 按 A = 删，只有显式输入 n 才保留。
  # 非交互（无 tty、CI）也按默认值执行，避免自动化里悄悄少做一步，
  # 事后又分不清是「跳过了」还是「忘了删」。
  if ! ask_yn "要卸载上面这些包吗？" y "卸载"; then
    UNINSTALL_KEPT_PKGS="${cand_pkg[*]}"
    warn "已保留：$UNINSTALL_KEPT_PKGS（按你的选择没有卸载，系统与这些包均未改动）"
    # 选择写进保留说明里，以后翻到就知道是主动留下的（userdata 可能已经搬到 MeidoSpace）
    local keep_note="${DATA_AT:-$DATA_DIR}/README-KEPT.txt"
    if [ -f "$keep_note" ]; then
      printf '\n卸载时选择保留的系统包（没有删除）：%s\n' \
        "$UNINSTALL_KEPT_PKGS" >> "$keep_note" 2>/dev/null || true
    fi
    return 0
  fi
  # ydotool 的守护服务必须先停掉、取消自启，再删包：顺序反了会留下指向
  # 已被删掉的 unit 的悬空软链（~/.config/systemd/user/default.target.wants/）
  if printf '%s\n' "${cand_pkg[@]}" | grep -qx ydotool; then
    disable_ydotool_unit
  fi
  # 选中的集合：删预编译版还是源码版，看机器上实际装的是哪个
  picked=()
  for p in "${cand_pkg[@]}"; do
    if [ "${p%kdotool-bin}" != "$p" ] && printf '%s\n' "${cand_pkg[@]}" | grep -qx kdotool; then
      continue
    fi
    picked+=("$p")
  done
  helper=""
  for h in paru yay; do need_cmd "$h" && { helper="$h"; break; }; done
  for p in "${picked[@]}"; do
    case "$p" in
      espeak-ng|ydotool|wtype|fakeroot)
        pkg_del+=("$p") ;;
      *)
        if [ -n "$helper" ]; then aur_del+=("$p"); else pkg_del+=("$p"); fi ;;
    esac
  done
  if [ ${#pkg_del[@]} -gt 0 ]; then
    # 逐个删：一次删多个时 pacman 会把「另一个包需要它」的提示合并成一条，
    # 用户看不出是哪个；逐个删出错时也只影响那一个
    for p in "${pkg_del[@]}"; do
      if run_root pacman -R --noconfirm -- "$p"; then
        info "已卸载：$p"
        UNINSTALL_REMOVED_PKGS="${UNINSTALL_REMOVED_PKGS:+$UNINSTALL_REMOVED_PKGS }$p"
      else
        warn "卸载 $p 失败（可能有别的包依赖它）。手动处理：sudo pacman -R $p"
        UNINSTALL_FAILS+=("系统包卸载失败：$p（手动：sudo pacman -R $p）")
      fi
    done
  fi
  for p in "${aur_del[@]}"; do
    # </dev/null：paru / yay 的确认提示会读 stdin；上一次卸载日志里就出现过
    # 「:: 打算删除这些软件包吗？[Y/n]」——它会跟脚本抢输入。明确喂 EOF。
    if "$helper" -R --noconfirm -- "$p" </dev/null; then
      info "已卸载（$helper）：$p"
      UNINSTALL_REMOVED_PKGS="${UNINSTALL_REMOVED_PKGS:+$UNINSTALL_REMOVED_PKGS }$p"
    else
      warn "卸载 $p 失败。手动处理：$helper -R $p"
      UNINSTALL_FAILS+=("系统包卸载失败：$p（手动：$helper -R $p）")
    fi
  done
  # 选 Y 就等于承诺「包没了」：逐个复检。sudo 失败或被别的包依赖挡住时，
  # 上面只有一条警告，很容易被后面的输出冲掉；这里再确认一次，并写进收尾汇总。
  local -a still=()
  for p in "${picked[@]}"; do
    pacman -Qq -- "$p" >/dev/null 2>&1 && still+=("$p")
  done
  if [ ${#still[@]} -gt 0 ]; then
    UNINSTALL_LEFT_PKGS="${still[*]}"
    warn "复检：这些包仍装在系统里：$UNINSTALL_LEFT_PKGS（原因见上方失败提示）"
    UNINSTALL_FAILS+=("系统包复检仍在：$UNINSTALL_LEFT_PKGS")
  else
    dim "复检通过：上面这些系统包都已不在系统里"
  fi
  return 0
}

# ---------- ydotool.service 状态还原 ----------
# 安装器对系统只做了两件「持久」的事：装包，以及把 ydotool.service enable 起来。
# 卸载是否该 disable ydotool.service：只有「安装前非 enabled、本次由我们 enable」才
# disable，原本就 enabled 的不动（用户自有设置）。
# 证据优先级：基线快照 > 清单 unit: 行（旧清单无 → 视为安装器 enable）。
ydotool_unit_before() {
  local f="$BASELINE_FILE" v=""
  if [ -f "$f" ]; then
    v="$(sed -n 's/^unit_enabled=//p' "$f" | head -n1)"
    [ -n "$v" ] && { printf '%s\n' "$v"; return 0; }
  fi
  if [ -f "$INSTALL_MANIFEST" ]; then
    v="$(sed -n 's/^unit:ydotool\.service .*enabled_before=\([^[:space:]]*\).*/\1/p' "$INSTALL_MANIFEST" | head -n1)"
    [ -n "$v" ] && { printf '%s\n' "$v"; return 0; }
  fi
  printf 'unknown\n'
}

disable_ydotool_unit() {
  local before
  before="$(ydotool_unit_before)"
  if ! need_cmd systemctl; then
    dim "没有 systemd，跳过 ydotool.service 状态还原"
    return 0
  fi
  if [ "$before" = enabled ] || [ "$before" = enabled-runtime ] || [ "$before" = static ]; then
    dim "ydotool.service 安装前就是 ${before}，保持不动"
    return 0
  fi
  case "$before" in
    disabled|masked|not-found)
      dim "ydotool.service 安装前是 ${before}，安装器改成了 enabled，现在还原"
      ;;
    *)
      dim "没有安装前的记录，按安装器 enable 过处理：disable 并停止"
      ;;
  esac
  if [ "$DRY_RUN" = 1 ]; then
    printf '[计划] systemctl --user disable --now ydotool.service\n'
    return 0
  fi
  systemctl --user disable --now ydotool.service >/dev/null 2>&1 || \
    systemctl --user disable ydotool.service >/dev/null 2>&1 || true
  # 删包后 unit 文件消失会留下悬空的 wants 链接，清掉（只删指向 ydotool 的）
  local d
  for d in "$HOME/.config/systemd/user"/*.wants; do
    [ -d "$d" ] || continue
    [ -L "$d/ydotool.service" ] && rm -f "$d/ydotool.service" 2>/dev/null || true
  done
  dim "ydotool.service 已 disable"
}

# ---------- 卸载：提权、结果记账、日志 ----------
# 卸载要结束 root 插件进程、重启 Decky、恢复只读层。三种环境都要能跑：
# 已经是 root、能免密 sudo、只有终端（让 sudo 自己问密码）。都不行就返回 127，
# 由调用方明确记成失败——绝不能静默跳过、假装成功。
run_root() {
  local rc
  if [ "$(id -u)" = "0" ]; then
    "$@"; rc=$?
  elif ! need_cmd sudo; then
    un_log "  RC=127 root: $*（没有 sudo）"
    return 127
  elif sudo -n true >/dev/null 2>&1; then
    # </dev/null 必须：piped 执行时 stdin 挂着外层管道，子进程（pacman/paru）
    # 读 stdin 会吃掉剩余脚本，表现为「静默退出、提示符直接回来」
    sudo -n "$@" </dev/null; rc=$?
  elif [ -t 0 ] || { true </dev/tty; } 2>/dev/null; then
    # 密码框由 sudo 自己从 /dev/tty 读，不占 stdin，所以这里重定向是安全的
    sudo "$@" </dev/null; rc=$?
  elif [ -n "${SUDO_ASKPASS:-}" ] && [ -x "${SUDO_ASKPASS}" ]; then
    # 没有终端但有 askpass（SSH 远程卸载、自动化）
    sudo -A "$@" </dev/null; rc=$?
  else
    un_log "  RC=127 root: $*（没有终端，无法输入 sudo 密码）"
    return 127
  fi
  un_log "  RC=$rc root: $*"
  return "$rc"
}

# 每一步的结果同时进 DONE / FAILS，收尾汇总照实打印，失败不会被后面的输出冲掉。
UNINSTALL_DONE=()
UNINSTALL_FAILS=()
UNINSTALL_LOG=""
UN_STEP=0
UN_STEP_TOTAL=9
UN_STEP_TITLE=""
UN_ABORT=0
UN_STEP_FAIL_BASE=0
UN_STEP_CLOSED=0
DATA_AT=""
UNINSTALL_REMOVED_PKGS=""

un_log() {
  [ -n "$UNINSTALL_LOG" ] || return 0
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$UNINSTALL_LOG" 2>/dev/null || true
  return 0
}
# 每一步结束时把「这一步成功还是失败」以 exit=0/1 写进日志，方便事后核对
un_step_close() {
  [ "$UN_STEP" -gt 0 ] || return 0
  # 同一步只记一次 exit=：汇总后显式关了一次，下一个 un_step 又会关一次
  [ "${UN_STEP_CLOSED:-0}" = "$UN_STEP" ] && return 0
  UN_STEP_CLOSED=$UN_STEP
  local rc=0
  [ "${#UNINSTALL_FAILS[@]}" -gt "$UN_STEP_FAIL_BASE" ] && rc=1
  un_log "== step $UN_STEP exit=$rc =="
}
un_step() {
  un_step_close
  UN_STEP=$((UN_STEP + 1))
  UN_STEP_TITLE="$1"
  UN_STEP_FAIL_BASE=${#UNINSTALL_FAILS[@]}
  UN_STEP_CLOSED=0
  printf '\n%s[%d/%d]%s %s%s%s\n' "$C_B" "$UN_STEP" "$UN_STEP_TOTAL" "$C_END" "$C_B" "$1" "$C_END"
  un_log "== [$UN_STEP/$UN_STEP_TOTAL] $1 =="
}
un_ok()   { printf '      %s✓%s %s\n' "$C_OK" "$C_END" "$*"; UNINSTALL_DONE+=("$*"); un_log "  OK   $*"; }
un_skip() { printf '      %s-%s %s\n' "$C_DIM" "$C_END" "$*"; un_log "  SKIP $*"; }
un_note() { printf '      %s%s%s\n' "$C_DIM" "$*" "$C_END"; un_log "  NOTE $*"; }
un_fail() {
  printf '      %s✗%s %s\n' "$C_ERR" "$C_END" "$*"
  UNINSTALL_FAILS+=("$*")
  un_log "  FAIL $*"
}

# 卸载日志写到 ~/MeidoSpace（这个目录卸载后保留），避免随安装目录一起进回收站。
# --purge 会连工作区一起移走，那种模式写到家目录根下；dry-run 不写字。
start_uninstall_log() {
  local purge="${1:-0}" dir="" fallback=0
  if [ "$DRY_RUN" = 1 ]; then
    dim "dry-run：不写卸载日志"
    return 0
  fi
  if [ "$purge" = 1 ]; then
    dir="$HOME"
  else
    dir="$HOME/MeidoSpace"
    if ! mkdir -p "$dir" 2>/dev/null; then
      dir="$HOME"
      fallback=1
    fi
  fi
  UNINSTALL_LOG="$dir/uninstall-$(date -u +%Y%m%d-%H%M%S).log"
  if ! : > "$UNINSTALL_LOG" 2>/dev/null; then
    UNINSTALL_LOG=""
    warn "无法写卸载日志（$dir 不可写），本次只在屏幕上输出"
    return 0
  fi
  {
    printf '%s\n' '这是 DeckMeido 的卸载日志。'
    printf '%s\n' '记录了卸载每一步做了什么、成功还是失败；可以随时删除，不影响任何东西。'
    printf '%s\n' "时间：$(date '+%Y-%m-%d %H:%M:%S')"
    printf '%s\n' "安装目录：$INSTALL_DIR"
    printf '%s\n' "用户数据：$DATA_DIR"
    printf '\n'
  } >> "$UNINSTALL_LOG" 2>/dev/null || true
  [ "$fallback" = 1 ] && warn "无法在 ~/MeidoSpace 建目录，卸载日志改写到 $HOME"
  dim "卸载日志：$UNINSTALL_LOG"
}

# 只读层的真实状态；不是 SteamOS 或没这个命令时输出为空
readonly_state() {
  command -v steamos-readonly >/dev/null 2>&1 || return 0
  steamos-readonly status 2>/dev/null | head -n1 || true
}

# 恢复 SteamOS 只读层，并回读确认。必须在卸载系统包之后调用。
restore_readonly() {
  local st
  if ! need_cmd steamos-readonly; then
    un_skip "没有 steamos-readonly（不是 SteamOS），跳过只读层恢复"
    return 0
  fi
  st="$(readonly_state)"
  if [ "$st" = "enabled" ]; then
    un_ok "只读层已是 enabled"
    return 0
  fi
  if [ "$DRY_RUN" = 1 ]; then
    printf '[计划] sudo steamos-readonly enable（当前 %s）\n' "${st:-unknown}"
    return 0
  fi
  if ! run_root steamos-readonly enable; then
    un_fail "恢复只读层失败（当前 ${st:-unknown}），请手动执行：sudo steamos-readonly enable"
    return 1
  fi
  st="$(readonly_state)"
  if [ "$st" = "enabled" ]; then
    un_ok "只读层已恢复：enabled"
  else
    un_fail "只读层恢复后状态仍是 ${st:-unknown}"
  fi
  return 0
}

# 异常退出（Ctrl-C、中途失败）也把只读层恢复回去。
# trap 里只用免密 sudo：不能弹密码框，否则退出流程卡死。
# 2026-11-08 实测卸载停在 [6/9] 后静默退出（无报错无汇总），故 trap 必须说出
# 「哪一步没跑完」。
readonly_guard_setup() {
  [ "$DRY_RUN" = 1 ] && return 0
  UN_ABORT=1
  trap 'rc=$?
trap - EXIT INT TERM
if [ "${UN_ABORT:-0}" = 1 ]; then
  un_log "!! 意外退出 rc=$rc 最后一步 [$UN_STEP/$UN_STEP_TOTAL] ${UN_STEP_TITLE:-?}"
  printf "\n\033[33m[!] 卸载没有跑完（退出码 %s，最后一步 [%s/%s] %s）\033[0m\n" \
    "$rc" "$UN_STEP" "$UN_STEP_TOTAL" "${UN_STEP_TITLE:-未知}" >&2
  printf "\033[33m    已完成的步骤不回滚；重新执行 bash install.sh uninstall 会接着清理剩下的。\033[0m\n" >&2
  printf "\033[33m    卸载日志：%s\033[0m\n" "${UNINSTALL_LOG:-（dry-run 不写）}" >&2
fi
if need_cmd steamos-readonly && [ "$(readonly_state)" != "enabled" ]; then
  sudo -n steamos-readonly enable >/dev/null 2>&1 || \
    printf "\033[33m[!] 恢复 SteamOS 只读层失败，请手动执行：sudo steamos-readonly enable\033[0m\n" >&2
fi' EXIT INT TERM
}

# root 拥有的目标（插件目录等）：先走普通回收站，失败再提权搬进回收站。
# 不用 rm -rf —— 搬走的东西仍然能从回收站拿回来。
trash_path_root() {
  local p="${1:-}" root dest info tried=0
  [ -n "$p" ] || return 1
  [ -e "$p" ] || [ -L "$p" ] || return 0
  if [ "$DRY_RUN" = 1 ]; then
    printf '[计划] 移入回收站（需要 root）：%s\n' "$p"
    TRASH_COUNT=$((TRASH_COUNT + 1))
    return 0
  fi
  # 父目录自己不可写（root 所有的插件目录）就别先试普通回收站，
  # 否则会先打印一条「未能移走」的假警告，再提权成功，用户看着矛盾
  if [ -w "$(dirname -- "$p")" ]; then
    tried=1
    trash_path "$p" && return 0
  fi
  # 普通回收站已经记过一次失败，这里改成提权成功后就把它撤销，别让汇总多报一项
  [ "$tried" = 1 ] && [ "${TRASH_FAILED:-0}" -gt 0 ] && TRASH_FAILED=$((TRASH_FAILED - 1))
  root="$(trash_root)"
  TRASH_FALLBACK_DIR="$root"
  mkdir -p "$root/files" "$root/info" 2>/dev/null || return 1
  dest="$(_trash_dest_for "$p")"
  if ! run_root mv -- "$p" "$dest"; then
    TRASH_FAILED=$((TRASH_FAILED + 1))
    return 1
  fi
  # 回收站归当前用户所有：root 搬进来的也交还给他，否则清空回收站时还要再要一次密码
  run_root chown -R "$(id -u):$(id -g)" "$dest" 2>/dev/null || true
  info="$root/info/$(basename -- "$dest").trashinfo"
  {
    printf '[Trash Info]\n'
    printf 'Path=%s\n' "$(urlencode_path "$p")"
    printf 'DeletionDate=%s\n' "$(date '+%Y-%m-%dT%H:%M:%S')"
  } > "$info" 2>/dev/null || true
  echo "[已移入] $dest"
  TRASH_COUNT=$((TRASH_COUNT + 1))
  return 0
}

# 卸载后应该不存在的所有残留路径（不含安装目录本身，它整目录单独处理）
uninstall_residue_paths() {
  local root p
  local -A seen=()
  for root in "${DECKY_PLUGINS_DIR:-}" "$HOME/homebrew/plugins"; do
    [ -n "$root" ] || continue
    p="${root%/}/DeckMeido"
    [ -n "${seen[$p]:-}" ] && continue
    seen[$p]=1
    printf '%s\n' "$p"
  done
  for p in \
    "$HOME/homebrew/settings/DeckMeido" \
    "$HOME/homebrew/data/DeckMeido" \
    "$HOME/homebrew/logs/DeckMeido" \
    "$CONFIG_DIR" \
    "$CACHE_DIR" \
    "$HOME/.deckmeido" \
    "$DESKTOP_DIR/DeckMeido.desktop" \
    "$DESKTOP_FOLDER/DeckMeido.desktop"
  do
    [ -n "$p" ] || continue
    [ -n "${seen[$p]:-}" ] && continue
    seen[$p]=1
    printf '%s\n' "$p"
  done
}

# 把 userdata 搬到工作区下带时间戳的目录，校验文件数与总字节都一致才算成功。
# 失败时返回 1 且不动目标/源，调用方据此中止「删除安装目录」。
move_userdata_to_workspace() {
  local ws="${1:-}" src="$DATA_DIR" dst count_src bytes_src count_dst bytes_dst
  if [ -z "$ws" ]; then
    un_fail "工作区路径为空，无法迁移用户数据"
    return 1
  fi
  if [ ! -e "$src" ]; then
    un_skip "没有用户数据目录（$src），无需搬迁"
    return 0
  fi
  if [ ! -d "$src" ]; then
    un_fail "用户数据路径不是目录：$src"
    return 1
  fi
  dst="$ws/userdata-$(date '+%Y%m%d-%H%M%S')"
  while [ -e "$dst" ]; do
    dst="$ws/userdata-$(date '+%Y%m%d-%H%M%S')-$RANDOM"
  done
  if [ "$DRY_RUN" = 1 ]; then
    printf '[计划] 用户数据搬迁：%s → %s\n' "$src" "$dst"
    printf '[计划] 搬迁后校验文件数与总字节数，通过后才删除安装目录\n'
    DATA_AT="$dst"
    return 0
  fi
  if ! mkdir -p "$ws" 2>/dev/null; then
    un_fail "无法创建工作区目录：$ws（用户数据未动，安装目录不会删除）"
    return 1
  fi
  count_src="$(find "$src" -type f 2>/dev/null | wc -l)"
  bytes_src="$(du -sb "$src" 2>/dev/null | awk '{print $1}')"
  count_src="${count_src:-0}"
  bytes_src="${bytes_src:-0}"
  if mv -- "$src" "$dst" 2>/dev/null; then
    :
  elif cp -a -- "$src" "$dst" 2>/dev/null; then
    un_note "跨文件系统，已复制到目标（源目录随安装目录一起进回收站）"
  else
    un_fail "用户数据搬迁失败：$src → $dst（安装目录不会删除）"
    return 1
  fi
  count_dst="$(find "$dst" -type f 2>/dev/null | wc -l)"
  bytes_dst="$(du -sb "$dst" 2>/dev/null | awk '{print $1}')"
  count_dst="${count_dst:-0}"
  bytes_dst="${bytes_dst:-0}"
  if [ "$count_dst" != "$count_src" ] || [ "$bytes_dst" != "$bytes_src" ]; then
    un_fail "用户数据校验不一致（源 ${count_src} 个文件/${bytes_src} 字节，目标 ${count_dst} 个/${bytes_dst} 字节）"
    un_note "源目录保持原样：$src；安装目录不会删除"
    return 1
  fi
  DATA_AT="$dst"
  un_ok "已保留用户数据：$dst（${count_src} 个文件，已校验）"
  # 依赖清单/安装基线随 userdata 一起搬走了，后面的包判定要读新位置
  DEP_RECORD="$dst/.deckmeido-deps"
  BASELINE_FILE="$dst/.deckmeido-baseline"
  return 0
}

# 保留目录里放一份说明，过几个月翻到还能知道这是什么、怎么恢复
write_kept_note_at() {
  local d="${1:-}" old="${2:-$DATA_DIR}"
  [ -n "$d" ] && [ -d "$d" ] || return 0
  [ "$DRY_RUN" = 1 ] && return 0
  cat > "$d/README-KEPT.txt" <<EOF 2>/dev/null || return 0
DeckMeido 已经卸载，这个目录是特意保留的用户数据（原来在 $old）。
里面是：配置 config.toml、人设 persona/、长期记忆 memory.json、会话 sessions/、
上传文件 uploads/、模型 models/、日志 logs/。

怎么恢复：重新装好 DeckMeido 后，把本目录的内容放回新安装目录的 userdata/，
或者启动时设 DECKMEIDO_DATA_DIR 指向本目录。

怎么彻底删除：gio trash "$d"
EOF
  un_note "保留说明：$d/README-KEPT.txt"
}

# 插件跑在 root 下，deck 用户 pkill 不掉——必须提权，并且回读确认真的没了。
PLUGIN_MAIN_PAT="homebrew/plugins/DeckMeido/main.py"
plugin_pids() {
  command -v pgrep >/dev/null 2>&1 || return 0
  pgrep -f "$PLUGIN_MAIN_PAT" 2>/dev/null | tr '\n' ' ' | sed 's/ $//' || true
}

# 全机后端进程（不限目录/用户）。不能按安装目录匹配：上次卸载日志写「已停止」，
# 实际是 root 从旧目录拉起的 uvicorn，命令行不含当前 $INSTALL_DIR 被漏掉，
# 端口占到卸载结束。模式写成 [s]erver 避免 pkill 匹配到自身命令行。
_all_backend_pids() {
  command -v pgrep >/dev/null 2>&1 || return 0
  pgrep -f '[s]erver\.main:app' 2>/dev/null | tr '\n' ' ' | sed 's/ $//' || true
}

# 全机、不分用户的本程序进程（root 起的 uvicorn / 旧目录里还在跑的客户端）。
# 卸载专用：_backend_pids 只认同 UID，root 那份它永远看不见。
_deckmeido_pids_all() {
  local d pid out=""
  for d in /proc/[0-9]*; do
    pid="${d#/proc/}"
    [ "$pid" = "$$" ] && continue
    [ "$pid" = "${PPID:-}" ] && continue
    _pid_is_deckmeido "$pid" || continue
    out="$out $pid"
  done
  for pid in $(_port_pids "$PORT"); do
    [ "$pid" = "$$" ] && continue
    _pid_is_deckmeido "$pid" || continue
    case " $out " in *" $pid "*) ;; *) out="$out $pid" ;; esac
  done
  printf '%s' "${out# }"
}

# 先 TERM 等一会儿，没退再提权 TERM，最后提权 KILL；返回仍活着的 pid。
# $1 = 要结束的 pid 列表；$2 = 判定「还没死」的探测命令（默认是本程序进程扫描）。
# 卸载各步杀的对象不同（后端 / 插件进程 / 端口持有者），探测必须能换，
# 否则「后端进程杀完了，但插件进程还在」会被误判成 success。
un_stop_pids() {
  local pids="$1" probe="${2:-_deckmeido_pids_all}" i left
  [ -n "$pids" ] || return 0
  # shellcheck disable=SC2086
  kill -TERM $pids 2>/dev/null || true
  for i in $(seq 1 10); do
    left="$($probe)"
    [ -z "$left" ] && return 0
    sleep 0.3
  done
  # shellcheck disable=SC2086
  run_root kill -TERM $pids >/dev/null 2>&1 || true
  for i in $(seq 1 10); do
    left="$($probe)"
    [ -z "$left" ] && return 0
    sleep 0.3
  done
  # shellcheck disable=SC2086
  run_root kill -KILL $pids >/dev/null 2>&1 || true
  sleep 0.5
  left="$($probe)"
  # 父进程被杀后子进程会换新 pid，第二轮按新 pid 再杀一遍（覆盖 root 拉起的孙进程）
  if [ -n "$left" ]; then
    # shellcheck disable=SC2086
    run_root kill -KILL $left >/dev/null 2>&1 || true
    sleep 0.5
    left="$($probe)"
  fi
  printf '%s' "$left"
}

# 卸载时真正停掉后端：pkill → pgrep 回读 → 提权再杀 → 再回读（含端口持有者）。
# 任何一步没杀干净都必须记失败，不能写「已停止」糊过去。
un_stop_backend() {
  local i left port_left
  UN_BACKEND_STATE=""
  cancel_running_turn
  stop_client
  # 杀不到进程时 pkill 返回 1、没有 pgrep/pkill 的系统返回 127：
  # 这些都不是「卸载失败」，不能让 set -e 把流程打断，结果由下面的回读判定
  if need_cmd pkill; then
    pkill -f "$INSTALL_DIR/client/launch.sh" 2>/dev/null || true
    pkill -f "$INSTALL_DIR/client/main.py" 2>/dev/null || true
    pkill -f "$INSTALL_DIR/server/main.py" 2>/dev/null || true
    pkill -f "$INSTALL_DIR/server/overlay.py" 2>/dev/null || true
    pkill -f '[s]erver\.main:app' 2>/dev/null || true
  fi
  left="$(_all_backend_pids)"
  for i in $(seq 1 10); do
    [ -z "$left" ] && break
    sleep 0.3
    left="$(_all_backend_pids)"
  done
  # 还有残留（典型是 root 拉起的那份）就提权再杀一轮。
  # 这里用全机扫描：root 的 uvicorn、从已删目录起的客户端都算，
  # 否则客户端 watchdog 会在卸载途中把这批进程再拉起来；
  # 再把端口持有者一并算进来——名字不像本程序但占着端口的，同样挡住重装。
  left="$(_deckmeido_pids_all)"
  for pid in $(_port_pids "$PORT"); do
    [ "$pid" = "$$" ] && continue
    case " $left " in *" $pid "*) ;; *) left="$left $pid" ;; esac
  done
  left="${left# }"
  if [ -n "$left" ]; then
    un_note "仍有进程占用（$left），提权结束"
    un_stop_pids "$left"
    left="$(_deckmeido_pids_all)"
  fi
  # 端口回读：进程退了但端口还 LISTEN，说明还有别的持有者（可能是 root）
  port_left=0
  _port_has_listener "$PORT" && port_left=1
  if [ -n "$left" ] || [ "$port_left" = 1 ]; then
    if [ -z "$left" ]; then
      un_note "端口 $PORT 仍被监听，查不到持有者 pid（可能属于其他用户）"
      _port_holder_report "$PORT"
    fi
    un_fail "后端仍在运行：${left:-端口 $PORT 被占用}（无法结束，重装会撞端口）"
    UN_BACKEND_STATE="仍在运行（${left:-端口 $PORT 被占用}）"
    return 1
  fi
  un_ok "客户端与后端已停止（pgrep + 端口回读确认）"
  UN_BACKEND_STATE="已停止（pgrep + 端口回读确认）"
  return 0
}

kill_plugin_process() {
  local pids i
  pids="$(plugin_pids)"
  if [ -z "$pids" ]; then
    un_skip "没有发现 Decky 插件进程"
    UN_PLUGIN_STATE="已确认无进程"
    return 0
  fi
  if [ "$DRY_RUN" = 1 ]; then
    printf '[计划] 结束插件进程（需要 root）：%s\n' "$pids"
    UN_PLUGIN_STATE="计划结束（$pids）"
    return 0
  fi
  un_note "结束插件进程（需要 root）：$pids"
  # 按 PID 杀，不用 pkill -f：sudo 自己的命令行里也会带上模式串，容易误伤
  # shellcheck disable=SC2086
  run_root kill -TERM $pids >/dev/null 2>&1 || true
  for i in $(seq 1 10); do
    [ -z "$(plugin_pids)" ] && break
    sleep 0.3
  done
  pids="$(plugin_pids)"
  if [ -n "$pids" ]; then
    # shellcheck disable=SC2086
    run_root kill -KILL $pids >/dev/null 2>&1 || true
    for i in $(seq 1 10); do
      [ -z "$(plugin_pids)" ] && break
      sleep 0.3
    done
  fi
  if [ -n "$(plugin_pids)" ]; then
    un_fail "插件进程仍在运行（$(plugin_pids)）：sudo 被拒绝或权限不足"
    UN_PLUGIN_STATE="仍在运行（$(plugin_pids)）"
    return 1
  fi
  # 插件进程退了，但它以 root 在 Decky 环境里拉起的客户端/后端可能还活着，
  # 继续占着 1337，第 9 步重载时就会被判成残留（2026-11-10 卸载卡死就是这批）。
  # 顺手一并结束，剩下清不掉的交给第 9 步的补刀。
  local orph pid
  orph="$(_deckmeido_pids_all)"
  for pid in $(_port_pids 1337); do
    [ "$pid" = "$$" ] && continue
    case " $orph " in *" $pid "*) ;; *) orph="$orph $pid" ;; esac
  done
  orph="${orph# }"
  if [ -n "$orph" ]; then
    un_note "插件拉起的进程仍在运行（$orph），一并结束"
    un_stop_pids "$orph" >/dev/null 2>&1 || true
  fi
  un_ok "已结束插件进程（回读确认）"
  UN_PLUGIN_STATE="已结束（回读确认）"
  return 0
}

# 卸载时的 Decky 重载：绝不用 systemctl restart（KillMode=process 会把 Decky
# 打进重启循环，就是 2026-11-08 那次事故），一律走 stop → 等残留退出 → start。
# 先体检：卸载中若 Decky 已卡在循环里，跳过重载并提示，不能成为压垮它的最后一击。
restart_decky() {
  local unit
  UN_DECKY_STATE=""
  if ! need_cmd systemctl; then
    un_skip "没有 systemctl，跳过 Decky 重载"
    UN_DECKY_STATE="未检查（没有 systemctl）"
    return 0
  fi
  unit="$(decky_service_unit)"
  if [ -z "$unit" ]; then
    un_skip "没检测到 Decky 服务（decky/plugin_loader），跳过重载"
    UN_DECKY_STATE="未检查（没装 Decky）"
    return 0
  fi
  DECKY_UNIT="$unit"
  if [ "$DRY_RUN" = 1 ]; then
    printf '[计划] 安全重载 %s：stop → 等残留退出 → start → 回读 NRestarts/1337/插件列表\n' "$unit"
    un_skip "dry-run：不执行 Decky 重载"
    return 0
  fi
  local hrc=0
  decky_health || hrc=$?
  if [ "$hrc" = 1 ]; then
    un_note "Decky 当前异常：$DECKY_HEALTH_NOTE"
    un_note "跳过重载，避免把 Decky 弄得更坏；请先重启 Deck"
    DECKY_LOOP_NOTE="$DECKY_HEALTH_NOTE"
    UN_DECKY_STATE="异常，已跳过重载（$DECKY_HEALTH_NOTE）"
    return 0
  fi
  if [ "$hrc" = 3 ]; then
    # 服务本来就没在跑：卸载不需要把它拉起来，拉起反而多一次风险
    un_skip "Decky 服务没在运行（$DECKY_HEALTH_NOTE），跳过重载"
    UN_DECKY_STATE="未运行（跳过重载）"
    return 0
  fi
  decky_safe_reload
  case "$DECKY_SAFE_STATE" in
    ok)
      un_ok "$DECKY_SAFE_NOTE"
      UN_DECKY_STATE="健康（已安全重载，1337 可连）" ;;
    pending)
      un_skip "$DECKY_SAFE_NOTE"
      UN_DECKY_STATE="未重载（$DECKY_SAFE_NOTE）" ;;
    *)
      warn "[!] $DECKY_SAFE_NOTE"
      un_fail "Decky 重载失败：$DECKY_SAFE_NOTE"
      UN_DECKY_STATE="异常（$DECKY_SAFE_NOTE）" ;;
  esac
  return 0
}

# /tmp 里的 socket 与 PyInstaller 解包目录：能删就删，删不掉只记一行，不算失败
cleanup_tmp_leftovers() {
  local p n=0 failed=0 skipped=0
  for p in /tmp/DeckMeidoOverlay-* /tmp/_MEI*; do
    [ -e "$p" ] || [ -L "$p" ] || continue
    if [ "$DRY_RUN" = 1 ]; then
      printf '[计划] 清临时残留：%s\n' "$p"
      n=$((n + 1))
      continue
    fi
    # 自己的直接删；root 的只有确认里面有 DeckMeido 痕迹才提权删，
    # 避免误伤别的插件（别的 Decky 插件也跑 root，也会留 /tmp/_MEI*）
    if [ -O "$p" ]; then
      rm -rf -- "$p" 2>/dev/null || failed=$((failed + 1))
    elif find "$p" -maxdepth 3 -iname '*deckmeido*' 2>/dev/null | grep -q .; then
      run_root rm -rf -- "$p" >/dev/null 2>&1 || true
      [ -e "$p" ] && failed=$((failed + 1))
    else
      skipped=$((skipped + 1))
      continue
    fi
    [ -e "$p" ] || n=$((n + 1))
  done
  if [ "$failed" -gt 0 ]; then
    un_note "有 $failed 项 /tmp 残留删不掉（不算失败，重启后会自动消失）"
  fi
  if [ "$skipped" -gt 0 ]; then
    un_note "有 $skipped 项 /tmp 残留不属于本用户且看不出 DeckMeido 痕迹，未动"
  fi
  if [ "$n" -gt 0 ]; then
    un_ok "已清理 /tmp 残留 $n 项"
  elif [ "$failed" -eq 0 ] && [ "$skipped" -eq 0 ]; then
    un_skip "没有发现 /tmp 残留"
  fi
  return 0
}

# 汇总里单独列一下「系统原有、这次没动的包」，避免用户误以为卸载把系统清空了
sys_kept_pkgs() {
  local p out=""
  command -v pacman >/dev/null 2>&1 || { printf '（没有 pacman）'; return 0; }
  for p in fakeroot espeak-ng spectacle; do
    pacman -Qq -- "$p" >/dev/null 2>&1 && out="${out:+$out }$p"
  done
  printf '%s' "${out:-无}"
}

# 收尾汇总：成功、失败、保留、回收站、日志，一次说清楚
uninstall_summary() {
  local aborted="${1:-0}" l title="卸载完成"
  [ "$aborted" = 1 ] && title="卸载中止"
  # 「卸载完成」绝不能覆盖「关键进程还在跑」：标题里直接标失败项数量，
  # 避免又出现上次那种「日志说完成、uvicorn 还在占 18765」的假象
  if [ "$aborted" = 0 ] && [ ${#UNINSTALL_FAILS[@]} -gt 0 ]; then
    title="卸载完成（有 ${#UNINSTALL_FAILS[@]} 项失败）"
  fi
  echo
  if [ "$DRY_RUN" = 1 ]; then
    if ui_fits; then
      card
      card_title "卸载计划（dry-run）"
      card_line "以上步骤都不会真正执行，没有改动任何文件"
      card_end
      echo
    else
      info "以上是计划（--dry-run），没有改动任何文件"
    fi
    return 0
  fi
  if ui_fits; then
    card
    card_title "$title"
    if [ ${#UNINSTALL_DONE[@]} -gt 0 ]; then
      for l in "${UNINSTALL_DONE[@]}"; do card_line "✓ $l"; done
    fi
    if [ ${#UNINSTALL_FAILS[@]} -gt 0 ]; then
      for l in "${UNINSTALL_FAILS[@]}"; do card_line "✗ $l"; done
    fi
    # 三项关键状态必须单列：它们决定「卸载到底干不干净」，不能埋在上面的 ✓/✗ 里
    [ -n "$UN_BACKEND_STATE" ] && card_line "后端进程     $UN_BACKEND_STATE"
    [ -n "$UN_PLUGIN_STATE" ] && card_line "插件进程     $UN_PLUGIN_STATE"
    [ -n "$UN_DECKY_STATE" ] && card_line "Decky        $UN_DECKY_STATE"
    [ -n "$DATA_AT" ] && card_line "用户数据：$DATA_AT"
    [ -n "${UNINSTALL_KEPT_PKGS:-}" ] && card_line "按你的选择保留的包：$UNINSTALL_KEPT_PKGS"
    card_line "系统原有包未动：$(sys_kept_pkgs)"
    card_line "回收站：$(trash_root)（共 $TRASH_COUNT 项）"
    [ -n "$UNINSTALL_LOG" ] && card_line "卸载日志：$UNINSTALL_LOG"
    card_end
    echo
  else
    info "$title"
    if [ ${#UNINSTALL_DONE[@]} -gt 0 ]; then
      for l in "${UNINSTALL_DONE[@]}"; do printf '  ✓ %s\n' "$l"; done
    fi
    if [ ${#UNINSTALL_FAILS[@]} -gt 0 ]; then
      for l in "${UNINSTALL_FAILS[@]}"; do warn "$l"; done
    fi
    [ -n "$UN_BACKEND_STATE" ] && info "后端进程：$UN_BACKEND_STATE"
    [ -n "$UN_PLUGIN_STATE" ] && info "插件进程：$UN_PLUGIN_STATE"
    [ -n "$UN_DECKY_STATE" ] && info "Decky：$UN_DECKY_STATE"
    [ -n "$DATA_AT" ] && info "用户数据：$DATA_AT"
    [ -n "${UNINSTALL_KEPT_PKGS:-}" ] && info "按你的选择保留的包：$UNINSTALL_KEPT_PKGS"
    info "系统原有包未动：$(sys_kept_pkgs)"
    info "回收站：$(trash_root)（共 $TRASH_COUNT 项）"
    [ -n "$UNINSTALL_LOG" ] && info "卸载日志：$UNINSTALL_LOG"
  fi
  if [ ${#UNINSTALL_FAILS[@]} -gt 0 ]; then
    warn "本次有 ${#UNINSTALL_FAILS[@]} 项失败，见上方 ✗ 行；文件类失败可以直接重跑卸载"
  fi
  return 0
}

do_uninstall() {
  local purge=0 arg p t0="" ws="" have_install=0 residue=0 abort=0 i left=""
  for arg in "$@"; do
    case "$arg" in
      --purge) purge=1 ;;
      --dry-run) DRY_RUN=1 ;;
      -h|--help)
        cat <<'EOF'
用法：bash install.sh uninstall [选项]

  --dry-run   只打印完整步骤，零改动（系统包、只读层也只列计划）
  --purge     彻底清除：userdata、工作区、配置一起移入回收站
              （默认模式会先把 userdata 搬到 ~/MeidoSpace/userdata-<时间> 再删代码）

默认卸载顺序：
  1. 停客户端与后端（pkill → pgrep + 端口回读确认，杀不干净明确报失败）
  2. 结束 root 插件进程（提权，回读确认）
  3. userdata 搬到 ~/MeidoSpace/userdata-<时间>，校验文件数与字节数
  4. 卸载安装器装过的系统包（回车=卸载，输入 n 才保留）
  5. 安装目录整目录、Decky 四个目录、~/.config/deckmeido、~/.cache/DeckMeido、
     桌面快捷方式 → 回收站
  6. 安全重载 Decky：先体检，异常则跳过；正常则 stop → 等残留退出 → start
     → 回读 NRestarts / 1337（绝不 systemctl restart，会触发重启循环）
  7. 清理 /tmp 残留
  8. 恢复 SteamOS 只读层为 enabled
  9. 汇总（含后端是否真停、插件进程是否真没了、Decky 是否健康）

所有文件只进回收站（gio trash / 提权 mv 到回收站），不永久删除。
EOF
        return 0 ;;
      *) warn "忽略未知参数：$arg" ;;
    esac
  done
  check_user

  [ -d "$INSTALL_DIR" ] && have_install=1
  while IFS= read -r p; do
    [ -n "$p" ] || continue
    if [ -e "$p" ] || [ -L "$p" ]; then residue=1; break; fi
  done < <(uninstall_residue_paths)

  if [ "$have_install" = 0 ] && [ "$residue" = 0 ]; then
    if [ "$DRY_RUN" = 1 ]; then
      # 演练模式：没装也照常把完整步骤走一遍，方便核对顺序
      warn "本机没有已安装的 DeckMeido，也没有残留；以下仍按完整流程演示"
    else
      info "没有找到已安装的 DeckMeido，也没有相关残留：$INSTALL_DIR"
      dim "无需卸载。想确认的话：ls -d ~/homebrew/plugins/DeckMeido ~/.config/deckmeido 2>/dev/null"
      return 0
    fi
  fi
  # 危险路径/源码工作区一律在动手前拒绝，保证零改动
  if [ "$have_install" = 1 ] && ! safe_rm_target "$INSTALL_DIR"; then
    warn "拒绝卸载：$INSTALL_DIR 疑似源码工作区或危险路径"
    die "卸载中止（未删除任何文件）"
  fi

  if ui_fits; then
    print_banner
    section "$([ "$purge" = 1 ] && echo '卸载（彻底清除）' || echo '卸载')"
  else
    info "卸载 DeckMeido"
  fi
  start_uninstall_log "$purge"

  # 安装时间窗要在搬走 userdata/清单之前取，否则读不到记录
  [ "$have_install" = 1 ] && t0="$(uninstall_window_start)"
  ws="$(workspace_dir)"
  # 工作区配置成家目录或根目录时不要往里搬东西，退回默认的 ~/MeidoSpace
  if [ -z "$ws" ] || [ "$ws" = "/" ] || [ "$ws" = "$HOME" ]; then
    ws="$HOME/MeidoSpace"
  fi
  # 用户数据固定搬到 ~/MeidoSpace（不跟工作区配置走，卸载后这个目录一定保留）
  local data_ws="$HOME/MeidoSpace"

  # ---------- 先说清楚要发生什么，再确认 ----------
  info "将要发生的事"
  if [ "$have_install" = 1 ]; then
    if [ "$purge" = 1 ]; then
      dim "· 用户数据 $DATA_DIR 随安装目录一起移入回收站（--purge）"
    else
      dim "· 用户数据 $DATA_DIR → $data_ws/userdata-<时间>，校验通过后才删代码"
    fi
    dim "· 安装目录 $INSTALL_DIR 整目录移入回收站（含代码、install.sh、VERSION、清单）"
  else
    warn "安装目录 $INSTALL_DIR 已不存在：只清理残留，不会重建它"
  fi
  dim "· Decky 插件目录、settings/data/logs、$CONFIG_DIR、$CACHE_DIR、桌面快捷方式 → 回收站"
  dim "· 结束 root 插件进程并重启 Decky（都会回读确认，失败会报）"
  local dep_list=""
  if [ -f "$DEP_RECORD" ]; then
    dep_list="$(awk -F'\t' 'NF{print $1}' "$DEP_RECORD" 2>/dev/null | sort -u | tr '\n' ' ')"
  fi
  dim "· 系统包（默认卸载，输入 n 才保留）：${dep_list:-没有安装记录，跳过}"
  dim "· 恢复 SteamOS 只读层为 enabled"
  dim "· 全部走回收站，不永久删除"
  echo
  if [ "$DRY_RUN" = 1 ]; then
    info "dry-run：只列步骤，不询问、不改动"
  elif ! ask_yn "确认开始卸载吗？" y "卸载"; then
    info "已取消，未做任何改动"
    un_log "用户取消，未做任何改动"
    return 0
  fi
  echo
  TRASH_COUNT=0
  TRASH_FAILED=0

  # ---------- 1 停客户端/后端 ----------
  un_step "停止客户端与后端"
  if [ "$DRY_RUN" = 1 ]; then
    printf '[计划] 关闭客户端、后端、overlay 进程，并用 pgrep + 端口回读确认真的没了\n'
  else
    # 上次的教训：这里打「已停止」，实际 uvicorn（root+deck 两份）还在跑并占着 18765。
    # 所以全部判决都交给 un_stop_backend 的回读结果，它说不干净就是失败。
    un_stop_backend || true
  fi

  # ---------- 2 结束 root 插件进程 ----------
  un_step "结束 Decky 插件进程"
  # 失败也要继续：用户可能已经删了一半，半途停下比继续更糟；失败会记进汇总
  kill_plugin_process || true

  # ---------- 3 先保数据 ----------
  un_step "保留用户数据"
  if [ "$purge" = 1 ]; then
    un_skip "--purge：用户数据随安装目录一起进回收站，不单独搬迁"
  elif ! move_userdata_to_workspace "$data_ws"; then
    abort=1
    un_fail "用户数据没有安全迁移，已跳过后续删除（安装目录原样保留）"
  else
    [ -n "$DATA_AT" ] && write_kept_note_at "$DATA_AT" "$DATA_DIR"
  fi
  if [ "$abort" = 1 ]; then
    un_step_close
    uninstall_summary 1
    return 1
  fi

  # ---------- 4 系统包（要在删安装目录之前：来源判定会扫安装目录里的落盘签名） ----------
  un_step "卸载系统包"
  readonly_guard_setup
  uninstall_optional_deps "$t0" "$([ "$DRY_RUN" = 1 ] && echo 0 || echo 1)"
  if [ -n "$UNINSTALL_REMOVED_PKGS" ]; then
    un_ok "系统包已移除：$UNINSTALL_REMOVED_PKGS"
  fi
  if [ -n "${UNINSTALL_KEPT_PKGS:-}" ]; then
    un_skip "已保留系统包（按你的选择）：$UNINSTALL_KEPT_PKGS"
  fi
  if [ -n "${UNINSTALL_LEFT_PKGS:-}" ]; then
    un_fail "系统包复检仍在：$UNINSTALL_LEFT_PKGS"
  fi

  # ---------- 5 移入回收站 ----------
  un_step "移入回收站"
  if [ "$DRY_RUN" = 1 ]; then
    [ "$have_install" = 1 ] && printf '[计划] 移入回收站：%s（整目录）\n' "$INSTALL_DIR"
    while IFS= read -r p; do
      [ -e "$p" ] || [ -L "$p" ] || continue
      printf '[计划] 移入回收站：%s\n' "$p"
    done < <(uninstall_residue_paths)
  else
    if [ "$have_install" = 1 ]; then
      if trash_path "$INSTALL_DIR"; then
        un_ok "已移入回收站：$INSTALL_DIR（整目录）"
      else
        un_fail "没能把 $INSTALL_DIR 移入回收站（权限不足或文件被占用）"
      fi
    else
      un_skip "安装目录已不存在：$INSTALL_DIR"
    fi
    while IFS= read -r p; do
      [ -e "$p" ] || [ -L "$p" ] || continue
      if trash_path_root "$p"; then
        un_ok "已移入回收站：$p"
      else
        un_fail "没能移走：$p"
      fi
    done < <(uninstall_residue_paths)
    # --purge 时数据目录可能被 DECKMEIDO_DATA_DIR 指到安装目录之外
    if [ "$purge" = 1 ]; then
      case "$DATA_DIR" in
        "$INSTALL_DIR"/*|"$INSTALL_DIR") ;;
        *) if [ -d "$DATA_DIR" ]; then
             if trash_path "$DATA_DIR"; then un_ok "已移入回收站：$DATA_DIR（用户数据）"
             else un_fail "没能移走用户数据：$DATA_DIR"; fi
           fi ;;
      esac
    fi
    need_cmd update-desktop-database && update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
    need_cmd kbuildsycoca6 && kbuildsycoca6 >/dev/null 2>&1 || true
    need_cmd kbuildsycoca5 && kbuildsycoca5 >/dev/null 2>&1 || true
  fi

  # ---------- 工作区 ----------
  if [ "$purge" = 1 ]; then
    if [ -z "$ws" ] || [ "$ws" = "/" ] || [ "$ws" = "$HOME" ]; then
      un_skip "工作区路径异常，未处理：${ws:-空}"
    elif [ -e "$ws/.git" ]; then
      un_skip "工作区含 .git，可能是你自己的仓库，跳过：$ws"
    elif [ -d "$ws" ]; then
      if [ "$DRY_RUN" = 1 ]; then
        printf '[计划] 移入回收站：%s（工作区）\n' "$ws"
      elif trash_path "$ws"; then
        un_ok "已移入回收站：$ws（工作区）"
      else
        un_fail "没能移走工作区：$ws"
      fi
    fi
  else
    un_skip "工作区保留：$ws（--purge 才会移走）"
  fi

  # ---------- 6 清 /tmp ----------
  un_step "清理 /tmp 残留"
  cleanup_tmp_leftovers

  # ---------- 7 只读层 ----------
  un_step "恢复只读层"
  restore_readonly

  # ---------- 8 汇总 ----------
  un_step "汇总"
  uninstall_summary 0
  un_step_close
  # 实质工作已经全部结束：即使后面的 Decky 重启把当前终端带走，
  # 也只剩「重启 Decky」这一件可有可无的事没做完。
  UN_ABORT=0
  [ "$DRY_RUN" = 0 ] && trap - EXIT INT TERM

  # ---------- 9 重启 Decky（best-effort，必须放最后） ----------
  # 2026-11-08 实测：此处停在 stop 后、start 前静默退出，汇总未打印、只读层
  # 未恢复。放最后且不中断失败，才能保证「卸载要么全做完，要么只差这一步」。
  un_step "重启 Decky"
  if ! restart_decky; then
    warn "Decky 重启没成功；插件文件已经移走，重启 Deck 后 QAM 入口自然会消失"
  fi
  un_step_close
  if [ "$DRY_RUN" = 0 ]; then
    # 实测插件目录被移走后，Decky 重启又会建出一个空目录来，这里回读一次
    while IFS= read -r p; do
      [ -e "$p" ] || [ -L "$p" ] || continue
      warn "Decky 重启后又被创建：$p（再跑一次卸载即可移走）"
    done < <(uninstall_residue_paths)
  fi
  return 0
}

# 无参数运行时弹出的交互菜单：检测是否已安装，给出带默认值的选项。
# 明确带子命令（install/upgrade/uninstall/...）时跳过菜单，仍可直接批处理。
show_menu() {
  check_user
  print_banner

  local installed=0
  [ -f "$INSTALL_DIR/VERSION" ] && installed=1

  if [ "$installed" = "1" ]; then
    local ver
    ver=$(head -n1 "$INSTALL_DIR/VERSION" 2>/dev/null || true)
    info "检测到已安装：$APP${ver:+ v$ver}（$INSTALL_DIR）"
    echo
    echo "  请选择要执行的操作："
    echo "  ${C_B}1${C_END}  升级到最新版  ${C_DIM}（默认，按 A（回车）即可）${C_END}"
    echo "  ${C_B}2${C_END}  重新安装      ${C_DIM}（覆盖重装，保留配置）${C_END}"
    echo "  ${C_B}3${C_END}  卸载          ${C_DIM}（删代码，保留人设与记忆）${C_END}"
    echo "  ${C_B}0${C_END}  退出"
    echo
    local choice=""
    if [ -t 0 ]; then
      printf '%s[?]%s 请输入序号 [1]（掌机：A 键=回车，直接回车=升级）' "$C_WARN" "$C_END"
      read -r choice || choice="0"
    else
      dim "非交互，自动继续：升级"
      choice="1"
    fi
    # 子流程（do_install / do_upgrade）里还会打一次大字，这里把标记立起来，
    # 让它们跳过，首屏的大字才不会被顶出可视区。
    export DECKMEIDO_BANNER_SHOWN=1
    case "${choice:-1}" in
      1|"") do_upgrade ;;
      2)    do_install ;;
      3)    do_uninstall ;;
      0|q|Q) info "已取消"; exit 0 ;;
      *)    die "无效选择：$choice" ;;
    esac
  else
    info "未检测到已安装的 $APP（$INSTALL_DIR/VERSION 不存在）"
    echo
    echo "  请选择要执行的操作："
    echo "  ${C_B}1${C_END}  全新安装  ${C_DIM}（默认，回车即可）${C_END}"
    echo "  ${C_B}2${C_END}  退出"
    echo
    local choice=""
    if [ -t 0 ]; then
      printf '%s[?]%s 请输入序号 [1] ' "$C_WARN" "$C_END"
      read -r choice || choice="0"
    else
      choice="1"
    fi
    case "${choice:-1}" in
      1|"") do_install ;;
      2|q|Q) info "已取消"; exit 0 ;;
      *)    die "无效选择：$choice" ;;
    esac
  fi
}

# 无参数时：没装就直接装；已装就「升级 / 卸载 / 退出」三选一，不给默认值。
# 想跳过询问，直接带子命令：bash install.sh upgrade / install / uninstall / check / menu
smart_default() {
  check_user
  if [ ! -f "$INSTALL_DIR/VERSION" ]; then
    info "未检测到已安装的 $APP，开始全新安装"
    do_install
    return
  fi
  local ver; ver=$(head -n1 "$INSTALL_DIR/VERSION" 2>/dev/null || true)
  print_banner
  info "检测到已安装：$APP${ver:+ v$ver}（$INSTALL_DIR）"
  echo
  echo "  请选择要执行的操作："
  echo "  ${C_B}1${C_END}  升级到最新版  ${C_DIM}（保留配置、人设与记忆）${C_END}"
  echo "  ${C_B}2${C_END}  卸载          ${C_DIM}（删代码，人设与记忆会备份）${C_END}"
  echo "  ${C_B}0${C_END}  退出"
  echo
  local choice
  if ! choice=$(ask_choice "请输入序号（1 升级 / 2 卸载 / 0 退出）："); then
    warn "没有可用的终端，无法确认要升级还是卸载"
    dim "带子命令直接指定：bash install.sh upgrade / bash install.sh uninstall"
    exit 0
  fi
  # 子流程里还会打一次大字，这里立标记让它们跳过，首屏才不会被顶走。
  export DECKMEIDO_BANNER_SHOWN=1
  case "$choice" in
    1) do_upgrade ;;
    2) do_uninstall ;;
    0|""|q|Q) info "已取消"; exit 0 ;;
    *) die "无效选择：$choice（输入 1 升级 / 2 卸载 / 0 退出）" ;;
  esac
}

# 通过「curl -sL ... | sh -」管道执行时，脚本不在本地磁盘上，这里直接自举：
# 下载最新发布包到临时目录再交给正常的 install 流程。这样用户记住一行命令，
# 不管全新安装还是之后升级，都能用同一条命令反复跑。
if _is_piped; then
  do_from_piped "$@"
  exit $?
fi

case "${1:-}" in
  "")            smart_default ;;
  menu)          show_menu ;;
  install)       do_install "${@:2}" ;;
  upgrade)      do_upgrade "${@:2}" ;;
  from-mirror)  do_from_mirror "${@:2}" ;;
  speedtest)
                # 只测速不安装：看各个 pip 源实打实的下载速率
                check_user
                idx=$(pick_pip_index)
                if [ "$idx" = "-1" ]; then
                  warn "所有源都连不上，检查网络或代理"
                else
                  info "最快：${PIP_INDEXES[$idx]}"
                  dim "想强制用它：DECKMEIDO_PIP_INDEX=${PIP_INDEXES[$idx]} bash install.sh install"
                fi ;;
  check)        do_check "${2:-}" ;;
  uninstall)    do_uninstall "${@:2}" ;;
  -h|--help|help)
    grep '^#' "$0" | sed 's/^# \{0,1\}//' | head -n 36 ;;
  *) die "未知命令：$1（可用：无参数=智能装/升 / menu / install / upgrade / from-mirror / speedtest / check / uninstall）" ;;
esac
