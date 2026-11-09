#!/usr/bin/env bash
# DeckMeido 一键安装 / 升级 / 卸载
#
#   bash install.sh              无参数时自动判断：没装就直接装，装了则询问「升级 / 卸载 / 退出」（须手动选择）
#   bash install.sh menu         弹出交互菜单，手动选「安装 / 升级 / 卸载」
#   bash install.sh install       直接全新安装（跳过菜单）
#   bash install.sh install --deps      顺带用 sudo 安装控制类插件（不再询问）
#   bash install.sh install --gui       只补装桌面客户端（PySide6），不动别的
#   bash install.sh install --stt       只补装语音输入依赖（sherpa-onnx，可选）
#   bash install.sh install --tts       只补装语音合成依赖（piper-plus，可选）
#   bash install.sh install --minimal   最小安装：后端 + Decky 插件 + 桌面客户端，不装语音/系统依赖，不下载模型
#   bash install.sh install --standard  标准安装（默认）：最小 + STT/TTS 引擎与所需模型
#   bash install.sh install --full      完整安装：标准 + 系统依赖 + 唤醒词模型 + 音箱模式所需组件
#   bash install.sh install --custom    自定义：逐项询问，默认全装
#   bash install.sh upgrade        升级代码与依赖，保留配置（默认逐项询问）
#   bash install.sh upgrade --minimal|--standard|--full   升级并按对应档位补齐组件
#   bash install.sh upgrade --deps      升级后顺带检查并安装控制类插件
#   bash install.sh check          自检：逐个实测控制类控件是否真的能用
#   bash install.sh uninstall     卸载：userdata 先搬到 ~/MeidoSpace，其余全部移入回收站
#   bash install.sh uninstall --dry-run  只列计划，零改动（系统包也只列不删）
#   bash install.sh uninstall --purge    连工作区、userdata、日志、模型、配置备份一起移入回收站
#   bash install.sh from-mirror   从国内镜像站下载最新版并安装（GitHub 不通时用）
#   bash install.sh speedtest     只测各 pip 源的下载速率，挑最快的那个
#
# 控制类插件（ydotool / kdotool / wtype / espeak-ng / fakeroot）是系统包，
# 装它们需要 root：按档位处理，自定义档逐项询问（默认装），都走 sudo。
#
# 为什么装完还要自检：SteamOS 换镜像（BUILD_ID 变动）时，/usr 下由 pacman
# 装的包会被整片冲掉，而 pacman 的数据库记录不一定同步，所以只查
# `pacman -Q` 会出现「记录在、命令没了」的假象。install.sh check 一律实测。
# 同理 ydotoold 必须由 systemd 用户单元托管（enabled），裸跑的进程重启即失效。
#
# 怎么装（最傻瓜的一条命令，装完能反复用来自动升级 / 卸载）：
#   curl -sL https://meido.meagames.cn/deckmeido/install.sh | bash
#   —— 镜像站优先；镜像不可用或校验失败时自动回落到 GitHub Release
#   —— 第一次跑：全新安装；之后再跑同一条：询问「升级 / 卸载」，选完才继续
#   想手动选操作（含卸载）：下载脚本后 bash install.sh menu
#   git 方式（需要本地源码目录时）：git clone ... && cd DeckMeido && bash install.sh
#   已有脚本、只想走镜像拉最新版：bash install.sh from-mirror

# 再跑第一条 git clone 时若报「目标路径已存在，并且不是一个空目录」，
# 说明 DeckMeido 目录已存在——直接 cd DeckMeido && bash install.sh 升级即可，
# 不用删目录重新 clone。
#
# 环境变量：
#   DECKMEIDO_DIR        安装目录，默认 ~/DeckMeido（旧版 ~/deckmeido 会被识别并兜底）
#   DECKMEIDO_WORKSPACE  工作区目录，默认 ~/MeidoSpace（与配置里的 [workspace].dir 同源）
#   DECKMEIDO_PORT       服务端口，默认 18765
#   DECKMEIDO_MIRROR     镜像站目录，默认 https://meido.meagames.cn/deckmeido
#   DECKMEIDO_PIP_INDEX  pip 源；留空则实测各源下载速率，自动用最快的那个
#   DECKMEIDO_SKIP_GUI   设为 1 则跳过 PySide6（只要网页版 / 后端时用）
#   DECKMEIDO_INSTALL_STT 语音依赖：1 装 / 0 跳过；不设时交互询问、非交互跳过
#   DECKMEIDO_PROFILE     安装档位：minimal / standard / full / custom（同命令行 --minimal 等）
#
# 注意：不要用 sudo 执行。脚本会把程序装到当前用户家目录、注册用户级桌面项，
#       用 root 装会装到 /root 下，桌面会话里起不来；也不注册开机自启服务。
#
# 用户可修改数据统一放 <安装目录>/userdata（可用 DECKMEIDO_DATA_DIR 覆盖）：
# config.toml、persona/meidosoul.json、memory.json、sessions/、uploads/、
# meidobot/、models/sensevoice/、logs/、core.stopped。只保留这一套标准布局；
# 升级同步排除整个 userdata/，卸载按清单删除代码、userdata/ 整目录保留。
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
# verify_tts / prefetch_voice_models 会写入，安装收尾汇总按它单列一行——避免
# 「语音依赖已就绪」被误读成「音色可用」，上次装完才发现模型根本没下载就栽在这。
TTS_MODEL_STATE="unknown"
# 三次下载都失败时的最后一次原因，写进收尾汇总
TTS_MODEL_ERROR=""
# 通用下载重试：最近一次失败的次数与原因（由 _retry_download 写入）
DOWNLOAD_ATTEMPTS=0
DOWNLOAD_ERR=""
# 自定义安装里被用户显式跳过的可选项。收尾汇总必须列出来，
# 否则「没装」和「用户不要」分不清。
SKIPPED_ITEMS=()
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

# 可执行位自愈：Windows/NTFS 打的包或 git 导出常把脚本记成 0644，用 bash 调用
# 不受影响，但桌面快捷方式是直接执行 client/launch.sh 的——缺可执行位时 Steam
# 会弹「缺少运行权限」，表现为热更新之后点图标起不来。每次运行先补一遍，
# 单个文件补不动就算了，不影响后面的安装/升级。
heal_exec_bits() {
  local root="$1" f
  for f in install.sh uninstall.sh client/launch.sh plugin/deploy.sh deploy/DeckMeido.desktop; do
    [ -f "$root/$f" ] && chmod +x "$root/$f" 2>/dev/null || true
  done
}
heal_exec_bits "$HERE"

# 安装器写下的标记与清单：只有带标记的目录才允许卸载；清单记录本次安装
# 实际铺进安装目录的条目，卸载时只按清单逐项删除，清单外的文件一律不动。
# 这些路径都由 INSTALL_DIR 推导，统一在 refresh_paths 里刷新。
DATA_DIR_ENV="${DECKMEIDO_DATA_DIR:-}"
refresh_paths() {
  INSTALL_MARKER="$INSTALL_DIR/.deckmeido-install"
  INSTALL_MANIFEST="$INSTALL_DIR/.deckmeido-manifest"
  CONFIG_MARKER="$CONFIG_DIR/.deckmeido-config"
  CONFIG_MANIFEST="$CONFIG_DIR/.deckmeido-config-manifest"
}

# ---------- 用户数据根（userdata/） ----------
# 所有用户可修改的数据统一放在数据根下：默认 $INSTALL_DIR/userdata，可用
# DECKMEIDO_DATA_DIR 显式覆盖（客户端 / 插件拉起后端时也读这个变量）。卸载永远整目录
# 保留，安装清单里也不会出现这里面的任何路径。
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
  # 安装器亲自装过的系统包清单：卸载时只对这份清单里的包提供「可选卸载」，
  # 绝不动 SteamOS 自带组件与用户原本就装好的包
  DEP_RECORD="$DATA_DIR/.deckmeido-deps"
  # 首次安装前的机器状态快照（路径是否存在、包是否已装、ydotool.service 原状态）。
  # 放在 userdata/ 里是为了升级时不丢；卸载时它会随卸载一起移入回收站。
  BASELINE_FILE="$DATA_DIR/.deckmeido-baseline"
}
refresh_paths
refresh_data_paths

# 端口解析：已有配置优先，没有配置才用 DECKMEIDO_PORT，最后是 18765。
# 升级/重装时用户可能已经在 config.toml 或设置页里改过端口，预检、卸载都
# 必须跟着这个值，不能一律拿环境变量覆盖掉。
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
# 注意：start_install_log 之后 stdout 会变成接给 tee 的管道，`[ -t 1 ]` 当场失效。
# 所以终端与否必须在这里先记下来，后面所有排版/进度判断都用 OUT_IS_TTY，
# 否则一开日志，颜色和下载进度条就全没了。
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
  # 失败时只把日志最后几行贴出来（完整日志在 userdata/logs/install-*.log），
  # 让用户能立刻看到真正的报错，而不是被几十行安装输出淹没
  if [ -n "${INSTALL_LOG:-}" ] && [ -s "$INSTALL_LOG" ]; then
    # 输出是经 tee 转发的，tail 可能赶在 tee 落盘之前，停一下再读
    sleep 0.2
    printf '%s    完整日志：%s%s\n' "$C_DIM" "$INSTALL_LOG" "$C_END" >&2
    printf '%s    最后 8 行：%s\n' "$C_DIM" "$C_END" >&2
    tail -n 8 "$INSTALL_LOG" >&2 || true
  fi
  exit 1
}
dim()  { printf '%s    %s%s\n' "$C_DIM" "$*" "$C_END"; }

# ---------- 安装步骤版式 ----------
# 参考 apt / Homebrew：一步一行结果，不把原始日志糊到屏幕上。
# 原始输出统一进 userdata/logs/install-<时间>.log，失败时由 die 贴最后几行。
STEP_NO=0
STEP_TOTAL=6
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

# 安装日志：把后续 stdout/stderr 同时抄一份到 userdata/logs/install-<时间>.log。
# 用 tee 进程替换而不是重开文件，屏幕上的输出保持原样。
INSTALL_LOG=""
start_install_log() {
  [ -n "${DECKMEIDO_LOG_STARTED:-}" ] && return 0
  DECKMEIDO_LOG_STARTED=1
  local log_dir="$DATA_LOGS" fallback=0
  # 安装目录一建好就先建 userdata/logs（mkdir -p 会一并建出安装目录）。
  # 只有连这个目录都建不出来时，才回退到家目录；日志不能因为目录问题丢掉。
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
# 2026-11-04 事故：卸载验证把源码工作区当成安装目录，一条 rm -rf 清空了整个仓库
#（含 .git）。之后所有删除动作都必须先过 safe_rm_target：
#   · 目标是源码工作区（含 .git）、或就是本脚本所在目录、或是它的上级 → 拒绝
#   · 目标是空、/、$HOME、或路径层级过浅 → 拒绝
#   · 真机确需强删时显式设置 DECKMEIDO_ALLOW_UNSAFE_UNINSTALL=1
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
    # 通过安装目录里的 uninstall.sh 转发进来：HERE 就是安装目录，属于预期情况。
    # 仍然拒绝 .git 源码工作区（开发机上的仓库不能当安装目录卸）。
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
# 2026-11-04 事故后再加一层：卸载只认安装器自己写的清单，不再整块删目录。
# 清单行格式：
#   server/xxx            安装目录内的单个文件（相对路径）
#   dir:client            安装目录内的整目录（安装器完全管理的部分）
#   ext:/abs/path         安装目录之外、由安装器创建的路径（桌面项/插件）
# 清单外的任何条目都不动；userdata/ 与标记文件永远不会出现在清单里。

# 相对条目校验：拒绝空、绝对路径、.. 逃逸，以及 userdata 与标记文件本身
manifest_rel_ok() {
  local rel="${1:-}"
  [ -n "$rel" ] || return 1
  case "$rel" in
    /*|*..*) return 1 ;;
    userdata|userdata/*|.deckmeido-install|.deckmeido-manifest) return 1 ;;
  esac
  return 0
}

# Decky Loader 的三个插件专属目录。Decky 会给每个插件自动建
#   ~/homebrew/settings/<插件名>   Decky 的环境变量与插件自己写的状态（device_id）
#   ~/homebrew/data/<插件名>       插件运行时数据
#   ~/homebrew/logs/<插件名>       插件日志
# 它们都在插件目录之外，卸载插件时 Decky 不会清理，所以要单独记账、单独移走。
# 只认 <插件名>，绝不碰 ~/homebrew 本身和 Decky 自己的配置。
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

# 外部条目白名单：只允许删安装器自己创建的那几个固定路径
ext_path_allowed() {
  local p="${1%/}" root d
  case "$p" in
    "${DESKTOP_DIR%/}/DeckMeido.desktop"|"${DESKTOP_FOLDER%/}/DeckMeido.desktop")
      return 0 ;;
  esac
  for root in "${DECKY_PLUGINS_DIR:-}" "$HOME/homebrew/plugins"; do
    [ -n "$root" ] || continue
    [ "$p" = "${root%/}/DeckMeido" ] && return 0
  done
  while IFS= read -r d; do
    [ "$p" = "$d" ] && return 0
  done < <(decky_plugin_dirs)
  return 1
}

# ---------- 回收站 ----------
# 所有卸载模式（含 --purge）都只把文件移入回收站，绝不永久删除：
#   1. 有 gio 且没指定 DECKMEIDO_TRASH_DIR 时，优先 gio trash（系统回收站）
#   2. 否则手工放进 <Trash>/files/，并写 <Trash>/info/<name>.trashinfo
#      （<Trash> = $XDG_DATA_HOME/Trash，默认 ~/.local/share/Trash）
#   3. DECKMEIDO_TRASH_DIR 可把回收站整个指到别处（沙盒测试用，绝不碰真实回收站）
# dry-run 时只打印「计划」，不创建目录、不移动任何东西。
TRASH_FALLBACK_DIR=""   # 本次实际使用的回收站根（手工模式）
TRASH_COUNT=0           # 移走（或 dry-run 计划移走）的条目数
TRASH_FAILED=0          # 权限不足、没能移走的条目数
TRASH_VIA_XDG=0         # 1 = 至少有一条走了 gio 系统回收站
TRASH_LAST_DIR=""       # 最近一次使用的回收站根，结尾提示用
DRY_RUN=0               # 1 = 只列计划，零改动
DRY_RUN_COUNT=0         # dry-run 计划移走的条目数
# 卸载系统包的结果。收尾汇总必须如实写出来，否则以后分不清是「按选择保留」
# 还是「以为删了其实没删」。
UNINSTALL_KEPT_PKGS=""  # 用户显式选择保留的包
UNINSTALL_LEFT_PKGS=""  # 回答卸载后复检仍在的包（sudo 失败 / 被依赖挡住）

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
  # 同一个父目录下可能有多个同名项（比如 homebrew/settings/DeckMeido 与
  # homebrew/plugins/DeckMeido），回收站里只按文件名平铺，撞名时带父目录名区分
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
    DRY_RUN_COUNT=$((DRY_RUN_COUNT + 1))
    TRASH_COUNT=$((TRASH_COUNT + 1))
    return 0
  fi
  # 指定回收站目录时永远走手工路径，保证测试/沙盒里不碰真实回收站
  if [ -z "${DECKMEIDO_TRASH_DIR:-}" ] && need_cmd gio && \
     gio trash -- "$p" >/dev/null 2>&1 && [ ! -e "$p" ]; then
    echo "[回收站] $p"
    TRASH_COUNT=$((TRASH_COUNT + 1))
    TRASH_VIA_XDG=1
    TRASH_LAST_DIR="$(trash_root)"
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
  TRASH_LAST_DIR="$root"
  return 0
}

# 清单行（format=2）：<类型>:<路径><TAB>preexisting=<0|1>
M_KIND=""; M_PATH=""; M_PRE="0"
manifest_split_line() {
  local line="${1:-}" pre
  M_KIND=file; M_PATH="$line"; M_PRE=0
  case "$line" in
    *$'\t'preexisting=*)
      pre="${line##*$'\t'}"
      line="${line%%$'\t'*}"
      M_PRE="${pre#preexisting=}"
      ;;
  esac
  case "$line" in
    dir:*) M_KIND=dir; M_PATH="${line#dir:}" ;;
    ext:*) M_KIND=ext; M_PATH="${line#ext:}" ;;
    unit:*) M_KIND=unit; M_PATH="" ;;
    format=*|app=*|version=*|installed_at=*|refreshed_at=*|installer_path=*|install_dir=*)
      M_KIND=header; M_PATH="" ;;
  esac
}

# 按清单逐项删除安装目录内条目。返回通过 LAST_REMOVED 报告实际删除条数。
# PURGE_ROOT=1 时把安装目录本身（dir:.）也整体移入回收站；默认卸载只把它
# 留成空目录，保证 userdata/ 一定还在——由调用方通过第二个参数控制。
delete_install_manifest() {
  local manifest="$1" purge_root="${2:-0}" line abs pass
  LAST_REMOVED=0
  LAST_SKIPPED=0
  LAST_PREEXISTING=0
  [ -f "$manifest" ] || return 1
  # dir:. 是安装目录本身，必须最后处理，否则后面按路径找条目全都落空
  for pass in 1 2; do
  while IFS= read -r line || [ -n "$line" ]; do
    [ -n "$line" ] || continue
    case "$line" in \#*) continue ;; esac
    manifest_split_line "$line"
    case "$M_KIND" in
      header|unit) continue ;;
    esac
    if [ "$M_KIND" = dir ] && [ "$M_PATH" = "." ]; then
      [ "$pass" = 2 ] || continue
      # 默认卸载保留安装目录本身（userdata/ 在里面，删了就不算「保留用户数据」）
      if [ "$purge_root" != 1 ]; then
        dim "默认卸载保留安装目录：$INSTALL_DIR（--purge 才会一起移走）"
        continue
      fi
    else
      [ "$pass" = 1 ] || continue
    fi
    # 安装前就存在的条目一律跳过：不是安装器带来的，不能带走
    if [ "$M_PRE" = "1" ]; then
      dim "跳过（安装前已存在）：${M_PATH:-$line}"
      LAST_PREEXISTING=$((LAST_PREEXISTING + 1))
      continue
    fi
    case "$M_KIND" in
      dir)
        # 安装目录本身写成 dir:.（"." 不是相对条目，单独放行）
        if [ "$M_PATH" = "." ]; then
          abs="$INSTALL_DIR"
        elif manifest_rel_ok "$M_PATH"; then
          abs="$INSTALL_DIR/$M_PATH"
        else
          warn "跳过清单中的异常目录条目：$line"
          LAST_SKIPPED=$((LAST_SKIPPED + 1))
          continue
        fi
        ;;
      ext)
        if ! ext_path_allowed "$M_PATH"; then
          warn "跳过清单中的外部条目：$M_PATH"
          LAST_SKIPPED=$((LAST_SKIPPED + 1))
          continue
        fi
        abs="$M_PATH"
        ;;
      *)
        if ! manifest_rel_ok "$M_PATH"; then
          warn "跳过清单中的异常文件条目：$line"
          LAST_SKIPPED=$((LAST_SKIPPED + 1))
          continue
        fi
        abs="$INSTALL_DIR/$M_PATH"
        ;;
    esac
    [ -e "$abs" ] || [ -L "$abs" ] || continue
    if trash_path "$abs"; then
      LAST_REMOVED=$((LAST_REMOVED + 1))
    elif [ "$M_KIND" = dir ] && [ "$M_PATH" = "." ]; then
      # 整个安装目录没搬动时（跨文件系统/权限），退化成把里面剩下的东西逐个移走：
      # 同样是进回收站，绝不 rm -rf
      warn "整个目录没能移入回收站，改为逐项移走：$abs"
      while IFS= read -r -d '' sub; do
        trash_path "$sub" || true
      done < <(find "$abs" -mindepth 1 -maxdepth 1 -print0 2>/dev/null)
      rmdir "$abs" 2>/dev/null || true
    else
      LAST_SKIPPED=$((LAST_SKIPPED + 1))
    fi
  done < "$manifest"
  done
  return 0
}

# 删完文件后，只收掉「因清单文件被删而变空」的父目录；rmdir 非空目录会失败，
# 所以用户放在这些目录里的文件不会被牵连。
cleanup_empty_manifest_dirs() {
  local manifest="$1" line rel dirs=""
  [ "$DRY_RUN" = 1 ] && return 0
  [ -f "$manifest" ] || return 0
  while IFS= read -r line; do
    manifest_split_line "$line"
    [ "$M_KIND" = file ] || continue
    [ "$M_PRE" = "0" ] || continue
    rel="$M_PATH"
    manifest_rel_ok "$rel" || continue
    while [ "${rel%/*}" != "$rel" ]; do
      rel="${rel%/*}"
      dirs="$dirs$rel"$'\n'
    done
  done < "$manifest"
  printf '%s' "$dirs" | sort -u -r | while IFS= read -r rel; do
    [ -n "$rel" ] || continue
    rmdir "$INSTALL_DIR/$rel" 2>/dev/null || true
  done
}

# ---------- 安装前基线 ----------
# 第一次运行新安装器时抓一份「机器原本是什么样」，之后升级不覆盖：
#   install_dir_existed=0/1
#   ext:<0|1>:<安装目录外的路径>
#   pkg:<0|1>:<包名>          （0=安装器装上的，1=安装前就有）
#   unit_enabled / unit_active（ydotool.service 安装前的状态）
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
    # Decky 托管的插件专属目录：安装前就有的（比如老版本留下的）不归本次安装
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

# 外部条目「安装前是否已存在」：有基线就以基线为准；没有基线时只有内容
# 确实是 DeckMeido 的才认作我们的，否则一律按用户原有文件跳过。
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
      # Decky 托管的插件专属目录：目录名就是本插件名，里面的东西都归本插件
      printf 0; return 0 ;;
  esac
  printf 1
}

# 写入/刷新 .deckmeido-install 与 .deckmeido-manifest。只记录安装器负责的条目：
#   · 整目录管理的：.venv client assets deploy tools plugin
#   · server 逐文件列出，跳过日志与运行时缓存
#   · 顶层文件：requirements、README、install.sh、VERSION、uninstall.sh 等
#   · 安装目录之外的固定路径（桌面项、Decky 插件）
# format=2 起每项都带 preexisting 标记；安装目录内部条目都是安装器铺进去的，
# 真正需要判断「原来有没有」的是桌面项与插件目录。
write_install_records() {
  [ -d "$INSTALL_DIR" ] || return 0
  local ver tmp="$INSTALL_MANIFEST.tmp" it rel root
  local now prev_at="" en="" ac="" pre=""
  now="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  ver="$(head -n1 "$INSTALL_DIR/VERSION" 2>/dev/null || true)"
  [ -n "$ver" ] || ver="unknown"
  # 升级不能改写首次安装时间，否则 pacman 取证的时间窗就跑到最后一次升级了
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

# 配置备份目录（~/.config/deckmeido）的标记与清单：--purge 只按清单逐项删，
# 没有 .deckmeido-config 标记的目录一律拒绝删除。
write_config_records() {
  mkdir -p "$CONFIG_DIR" 2>/dev/null || return 1
  {
    printf 'format=1\n'
    printf 'app=%s\n' "$APP"
    printf 'created_at=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    printf 'installer_path=%s\n' "$HERE/install.sh"
  } > "$CONFIG_MARKER" 2>/dev/null || return 1
  : > "$CONFIG_MANIFEST" 2>/dev/null || return 0
  local f
  for f in meidosoul.json memory.json; do
    [ -f "$CONFIG_DIR/$f" ] && printf '%s\n' "$f" >> "$CONFIG_MANIFEST"
  done
  return 0
}

# ---------- 文本宽度 ----------
# 终端里中文占 2 列，按字符数排版框线一定会歪，这里按显示宽度算。
# 只覆盖中日韩 + 全角标点，够本脚本用了（纯 bash，不依赖 wc -L）
char_cols() {
  local cp
  printf -v cp '%d' "'$1"
  # 制表/框线字符（U+2500–U+257F）在终端是单宽；其余非 ASCII 一律按双宽。
  # 宁可多算一列（留白多一点），也不让框线被顶出终端。
  if [ "$cp" -ge 9472 ] && [ "$cp" -le 9599 ]; then
    printf 1
  elif [ "$cp" -ge 128 ]; then
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

# 表格一行：左列定宽 14，右列自动换行不缩进过头
table_row() {
  local left="$1" right="$2" pad
  pad=$((14 - $(disp_len "$left")))
  [ "$pad" -lt 1 ] && pad=1
  printf '  %s%s%s%*s%s\n' "$C_OK" "$left" "$C_END" "$pad" "" "$right"
}

# 完成卡片：带边框的结果汇总
# 宽度跟着终端走；单行超过内宽时按显示宽度折行，右边的 │ 不会再被顶出屏幕。
card() {
  if card_fits; then
    local inner n
    inner=$(card_inner); n=$((inner + 1))
    printf '\n%s┌%s┐%s\n' "$C_C" "$(printf '─%.0s' $(seq 1 "$n"))" "$C_END"
  else
    echo
  fi
}
card_row() {
  # $1=文本 $2=内宽 $3=是否加粗
  local text="$1" inner="$2" bold="${3:-0}" pad
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
    # 写日志/管道：保持原样一行，不额外插入换行
    if [ "$bold" = 1 ]; then printf '%s\n' "$text"; else printf '  %s\n' "$text"; fi
    return 0
  fi
  for ((i = 0; i < ${#text}; i++)); do
    ch="${text:i:1}"
    w=$(char_cols "$ch")
    if [ $((cw + w)) -gt "$inner" ] && [ -n "$cur" ]; then
      if [ "$box" = 1 ]; then
        card_row "$cur" "$inner" "$bold"
      else
        printf '  %s\n' "$cur"
      fi
      cur="$ch"; cw=$w
    else
      cur+="$ch"; cw=$((cw + w))
    fi
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
    printf '%s└%s┘%s\n' "$C_C" "$(printf '─%.0s' $(seq 1 "$n"))" "$C_END"
  fi
}

# 开场：DeckMeido 大字（figlet "Big" 字体），下面接标题与简介
print_banner() {
  ui_fits || return 0
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

# SteamOS 镜像号：换镜像时 /usr 下 pacman 装的包会被整片冲掉，这是本脚本
# 一切「装完还要实测」逻辑的根源。之前 BUILD_ID 从未赋值，导致镜像变动
# 检测（system_notes 里的提示）永远是死的，这里统一从 os-release 读。
BUILD_ID="$( { sed -n 's/^BUILD_ID=//p' /etc/os-release 2>/dev/null || true; } | tr -d '"' | head -n1 || true)"

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
  # 1. 磁盘空间：PySide6 解包后约 700MB，venv 最坏按 1.5G 留余量
  local need_mb=1500 avail_mb=""
  avail_mb=$(df -Pm "$HOME" 2>/dev/null | awk 'NR==2 {print $4}')
  if [ -n "$avail_mb" ] && [ "$avail_mb" -lt "$need_mb" ]; then
    warn "家目录剩余 ${avail_mb}MB，安装建议至少 ${need_mb}MB（PySide6 体积较大）"
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
  need_cmd espeak-ng || miss+=("espeak-ng（语音播报回复）")
  need_cmd kdotool   || miss+=("kdotool（按窗口名切换/聚焦窗口）")
  need_cmd ydotool   || miss+=("ydotool（模拟鼠标点击、键盘输入）")
  need_cmd wtype     || miss+=("wtype（Wayland 原生打字，ydotool 的备选）")
  need_cmd spectacle || miss+=("spectacle（KWin 截图，一般自带）")
  if [ ${#miss[@]} -gt 0 ]; then
    warn "以下系统控件没装，相关功能会受限："
    for m in "${miss[@]}"; do dim "$m"; done
    dim "想自动安装：bash install.sh install --deps"
    # SteamOS 换镜像会冲掉 /usr 下 pacman 装的包，这情况最常见
    if [ -n "${BUILD_ID:-}" ] && [ -f "$HOME/.config/deckmeido/.deps-build-id" ]; then
      local prev; prev="$(cat "$HOME/.config/deckmeido/.deps-build-id" 2>/dev/null || true)"
      [ "$prev" = "$BUILD_ID" ] || dim "检测到 SteamOS 镜像已更新（$prev → ${BUILD_ID}），系统包可能被冲掉，重装即可恢复"
    fi
  fi
  # ydotool 装了但 ydotoold 没跑，点击/输入会直接报 socket 错误，这里提前点出来
  if need_cmd ydotool && ! pgrep -x ydotoold >/dev/null 2>&1; then
    warn "ydotool 已装但 ydotoold 守护进程没在运行，模拟点击/输入会失败"
    dim "修一次：bash install.sh check --fix   （会配置成开机自启）"
  fi
}

# ---------- 可选系统依赖 ----------
# 这些是「替你在桌面上动手」的核心控件，装它们必须 root。
# 安装时按档位处理（自定义档逐项询问，默认装），或加 --deps 直接装。
# fakeroot 不是运行时控件，但用 paru/yay 编译 AUR 包（kdotool）必须要它，
# 缺了会在 makepkg 阶段报「fakeroot not found」，所以一起装。
DEPS_PACMAN=(espeak-ng ydotool wtype fakeroot)
# AUR 里叫 kdotool，编译出来很慢；优先用预编译的 kdotool-bin，
# 找不到 AUR helper 才提示手动装。
DEPS_AUR=(kdotool-bin)
DEPS_AUR_FALLBACK=(kdotool)
# install_pacman_deps 跑完后仍然找不到命令的包：安装汇总靠它说实话，
# 不再一律打勾。2026-11-08 的装机就是「提示 ✓ 已处理，设置页却全红」。
DEPS_MISSING_AFTER=()

# 随发布包自带的离线包集合：只放「小、依赖浅、缺了直接让设置页变红」的三个。
# ydotool 16.6KB + wtype 10.0KB + fakeroot 78.7KB ≈ 105KB（压缩后）。
# espeak-ng（7.2MB，且要 libsonic / pcaudiolib 两个 SteamOS 没自带的依赖）不自带，
# 走本机缓存或镜像站；kdotool 是 AUR 包，官方仓库里没有，只能放镜像站。
# 具体下载清单在 tools/fetch_offline_deps.sh 里，两边要一起改。

# Release 里附带的离线包目录（deploy/deps/，随发布包一起分发）。
# SteamOS 换镜像会重建 /usr，pacman 缓存也会失效，所以不能指望
# /var/cache/pacman/pkg 里还有东西；这些 .pkg.tar.zst 是独立一份。
OFFLINE_DIR="$HERE/deploy/deps"
# 离线包哈希清单：只覆盖 deploy/deps/ 下的 .pkg.tar.zst，
# 与 dist/SHA256SUMS（覆盖发布 tar.gz）是两套东西，互不替代。
OFFLINE_MANIFEST="$OFFLINE_DIR/SHA256SUMS"

# 离线包的安装记录。值是一组包名，记录「上次凭离线包装成功的都有谁」。
# 用途有两个：镜像更新后知道该重装哪些，以及 offline_stale 判断
# 离线包是不是已经过期（比系统仓库还老就别硬装）。
offline_stamp() { printf '%s' "$CONFIG_DIR/.deps-offline"; }

# 离线包哈希校验：必须能在 SHA256SUMS 里查到这一行，且实算哈希一致。
# 清单缺失 / 文件没列 / 哈希不符，一律当不可信——宁可不装，也不装一个
# 来源不明的系统包（pacman -U 以 root 身份跑，脚本被换掉就是提权入口）。
offline_hash_ok() {
  local f="$1" base want got
  [ -f "$OFFLINE_MANIFEST" ] || return 1
  base="$(basename "$f")"
  want="$(awk -v n="$base" '($2==n)||($2=="*" n){print $1}' "$OFFLINE_MANIFEST" 2>/dev/null | head -n1)"
  [ -n "$want" ] || return 1
  got="$(sha256sum "$f" 2>/dev/null | awk '{print $1}')"
  [ "$got" = "$want" ]
}

# 已装版本比这个包文件新：装回去等于降级，跳过，留给联网仓库版本。
# SteamOS 换镜像后 holo 仓库会滚动升级，旧离线包不该把系统拽回去。
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
      dim "断网时装它仍可用；联网时建议让它走仓库：bash install.sh install --deps"
    fi
  done
}

# ---------- 数据库同步失败时的三层兜底 ----------
# ① 本机 /var/cache/pacman/pkg ② 发布包自带 deploy/deps ③ 镜像站 $MIRROR/deps
# 每层都只装「这层找得到的包」，装完实测命令，缺的继续往下一层要；
# 三层都拿不到就如实报缺，不再假装装好。

# pacman -U 一批本地包文件。needed=1 时不重复装已装版本；
# needed=0 用于「记录在、命令丢了」的强制覆盖重装。
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
  for p in "${names[@]}"; do need_cmd "$p" || left+=("$p"); done
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
  for p in "${names[@]}"; do need_cmd "$p" || left+=("$p"); done
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
  for p in "${names[@]}"; do need_cmd "$p" || left+=("$p"); done
  [ ${#left[@]} -eq 0 ] && return 0
  return 1
}

deps_intro() {
  # 安装前把每个控件是干什么的说清楚，用户才知道为什么要给 sudo
  local miss_pacman=() miss_aur=() miss_other=()
  for p in "${DEPS_PACMAN[@]}"; do need_cmd "$p" || miss_pacman+=("$p"); done
  need_cmd kdotool   || miss_aur+=("${DEPS_AUR[0]}")
  need_cmd spectacle || miss_other+=("spectacle")

  section "控制类系统控件（推荐安装）"
  dim "DeckMeido 的「帮你在桌面上操作」能力依赖下面这些系统包；"
  dim "不装也能聊天、截图、执行命令，但没法替你点鼠标、敲键盘、念出回复。"
  echo
  local st
  for p in ydotool kdotool wtype espeak-ng fakeroot spectacle; do
    if need_cmd "$p"; then st="${C_OK}已装${C_END}"; else st="${C_WARN}待装${C_END}"; fi
    case "$p" in
      ydotool)   table_row "$p" "$st  模拟鼠标点击 / 键盘输入（搭配 ydotoold 守护进程）" ;;
      kdotool)   table_row "$p" "$st  按窗口标题查找并聚焦窗口，配合 ydotool 点击正确的位置" ;;
      wtype)     table_row "$p" "$st  Wayland 原生打字，ydotool type 不支持中文时的备选" ;;
      espeak-ng) table_row "$p" "$st  把回复念出来（语音播报）" ;;
      fakeroot)  table_row "$p" "$st  编译 AUR 包（kdotool）时 makepkg 必需的构建依赖" ;;
      spectacle) table_row "$p" "$st  KDE 截图工具，用于「看看当前屏幕」" ;;
    esac
  done
  echo
  dim "它们分工成三类："
  dim "  · 动手（ydotool / kdotool / wtype）——替你点鼠标、敲键盘、切窗口"
  dim "  · 出声（espeak-ng）——把女仆的回复念出来"
  dim "  · 看屏（spectacle）——截图，让女仆「看到」当前屏幕"
  echo
  [ ${#miss_pacman[@]} -gt 0 ] && dim "待安装（pacman）：${miss_pacman[*]}"
  [ ${#miss_aur[@]}    -gt 0 ] && dim "待安装（AUR）：${miss_aur[*]}（需要 paru 或 yay）"
  [ ${#miss_other[@]}  -gt 0 ] && dim "未找到：${miss_other[*]}（KDE 一般自带，可忽略）"
  # 有离线包就先说一声，用户能预期「不用等下载」
  if [ -d "$OFFLINE_DIR" ]; then
    local n; n=$(find "$OFFLINE_DIR" -maxdepth 1 -name '*.pkg.tar.zst' 2>/dev/null | wc -l)
    if [ "${n:-0}" -gt 0 ]; then
      dim "包内自带离线依赖 ${n} 个（已逐个校验哈希），优先本地安装；装不上再依次退到本机缓存、镜像站、联网"
      offline_check_fresh
    fi
  fi
  dim "安装过程会用 sudo 关闭 SteamOS 根分区只读保护并安装系统包，不会改动你的游戏与个人文件。"
  dim "其中 ydotool / wtype 会注册一个用户级守护进程（ydotoold），装完自动开机自启，无需你手动配置。"
}

# 卸载只有一个方向（删），所以「终端里默认答 yes」，避免在掌机上多按一次 A；
# 没有终端时一律视为取消，绝不在无人确认时动手。
# DECKMEIDO_ASSUME_YES=1 是给自动化测试用的：跳过询问直接视为确认。
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
  if [ -t 0 ]; then
    printf '%s[?]%s %s %s（掌机：A 键=回车，默认 %s）' \
      "$C_WARN" "$C_END" "$q" "$tag" "$def"
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
  ans=${ans:-$def}
  case "$ans" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

# 「按 A 或回车继续」：所有需要停一下看结果的地方都走这里。
# 按键语义（只是暂停，不承担选择）：
#   按 A / 回车 / 其它任意键 —— 都结束等待，继续往下；
#   Ctrl-C —— 照常中断脚本；stdin 已到 EOF（管道）—— 也直接继续。
# 以前用 `read -r _` 读整行，只按 A 没有回车就会一直卡着，这正是
# 「按 A 无法退出」的原因；这里改成 -n1，读到一个字符就返回。
# 非交互（管道/日志）不能停住等人，直接说明「自动继续」。
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
# 所有要联网拉取的项集中在这里，逐条确认；已存在的直接跳过并说明。
# 自定义档逐项确认时默认全装（[Y/n]，回车即装），只有显式输入 n 才跳过。

# 已装好的判定：$1 = venv python，$2 = 模块名
have_py_module() {
  [ -x "$1" ] && "$1" -c "import $2" >/dev/null 2>&1
}

# 打印一条可选项：$1 名称，$2 用途，$3 体积，$4 已存在说明（空=不存在），
# $5 默认值 y/n，$6 跳过后的影响（写进日志与收尾汇总）。
# 返回 0 = 要装，1 = 跳过。
opt_download() {
  local name="$1" purpose="$2" size="$3" present="$4" def="${5:-n}" \
        impact="${6:-该功能不可用，之后可单独补装}"
  if [ -n "$present" ]; then
    dim "$name：已存在（$present），跳过"
    return 1
  fi
  if [ "${DECKMEIDO_NONINTERACTIVE:-0}" = "1" ] || { [ ! -t 0 ] && [ ! -r /dev/tty ]; }; then
    if [ "$def" = "y" ]; then
      dim "$name：非交互，默认安装（$purpose，$size）"
      return 0
    fi
    SKIPPED_ITEMS+=("$name")
    warn "已跳过：$name（影响：$impact）"
    return 1
  fi
  if ask_yn "$name（$purpose，$size）现在下载安装？" "$def"; then
    return 0
  fi
  SKIPPED_ITEMS+=("$name")
  warn "已跳过：$name（影响：$impact）"
  return 1
}

# ---------- 安装档位 ----------
# 三档：minimal（最小）/ standard（标准）/ full（完整），外加 custom（逐项询问）。
# 命令行 --minimal / --standard / --full / --custom（或 --profile=xxx）直接指定。
# 没指定时：全新安装弹一次菜单（默认标准）；升级默认自定义（逐项询问，默认全装；
# 不想装的项显式输入 n）。
# 非交互模式（管道安装、无 tty）绝不询问，直接按传入的默认值继续。
choose_install_profile() {
  local def="${1:-standard}" cur="${DECKMEIDO_PROFILE:-}"
  case "$cur" in
    ""|minimal|standard|full|custom) ;;
    *) die "未知安装档位：$cur（可选 minimal / standard / full / custom）" ;;
  esac
  if [ -n "$cur" ]; then
    return 0
  fi

  local dflt
  case "$def" in
    minimal) dflt=1 ;; standard) dflt=2 ;; full) dflt=3 ;; *) dflt=4 ;;
  esac
  if [ "${DECKMEIDO_NONINTERACTIVE:-0}" = "1" ] || { [ ! -t 0 ] && [ ! -r /dev/tty ]; }; then
    DECKMEIDO_PROFILE="$def"
    export DECKMEIDO_PROFILE
    dim "非交互模式，按默认档位继续：$def"
    return 0
  fi

  echo
  echo "  请选择安装档位："
  echo "  ${C_B}1${C_END}  最小安装  ${C_DIM}后端 + Decky 插件 + 桌面客户端；不装语音、不下载模型、不装系统依赖${C_END}"
  echo "  ${C_B}2${C_END}  标准安装  ${C_DIM}最小内容 + STT/TTS 引擎与所需模型（不含音箱模式）${C_END}"
  echo "  ${C_B}3${C_END}  完整安装  ${C_DIM}标准内容 + 系统依赖 + 唤醒词模型 + 音箱模式所需组件${C_END}"
  echo "  ${C_B}4${C_END}  自定义    ${C_DIM}逐项询问服务端 / 桌面客户端 / 语音 / 系统依赖${C_END}"
  echo
  local choice=""
  case "$dflt" in
    1) printf '%s[?]%s 请输入序号 [1]（最小）' "$C_WARN" "$C_END" ;;
    2) printf '%s[?]%s 请输入序号 [2]（标准，直接回车即可）' "$C_WARN" "$C_END" ;;
    3) printf '%s[?]%s 请输入序号 [3]（完整）' "$C_WARN" "$C_END" ;;
    *) printf '%s[?]%s 请输入序号 [4]（自定义）' "$C_WARN" "$C_END" ;;
  esac
  if [ -t 0 ]; then
    read -r choice || choice=""
  else
    read -r choice < /dev/tty || choice=""
  fi
  case "${choice:-$dflt}" in
    1) DECKMEIDO_PROFILE=minimal ;;
    2) DECKMEIDO_PROFILE=standard ;;
    3) DECKMEIDO_PROFILE=full ;;
    4) DECKMEIDO_PROFILE=custom ;;
    *) warn "无效选择：$choice，按默认档位继续"; DECKMEIDO_PROFILE="$def" ;;
  esac
  export DECKMEIDO_PROFILE
}

optional_downloads() {
  local py="$INSTALL_DIR/.venv/bin/python" nonint=0
  if [ "${DECKMEIDO_NONINTERACTIVE:-0}" = "1" ] || { [ ! -t 0 ] && [ ! -r /dev/tty ]; }; then
    nonint=1
  fi
  section "可选下载"
  dim "下面每一项都会单独确认；已有内容会跳过，不会重复下载。"
  [ "$nonint" = 1 ] && dim "当前是非交互模式：按默认值自动继续/跳过。"

  # 三档安装：档位给定后不再逐项询问。已有依赖 / 模型照旧跳过，不会重复下载；
  # 升级时也不会因为选了 minimal 就卸载已经装好的组件。
  #
  #   最小：后端 + Decky 插件 + 桌面客户端；不装 STT/TTS、不下载语音模型、
  #         不装系统依赖，不含音箱模式（智能音响）组件
  #   标准：最小 + STT/TTS 引擎与所需模型（SenseVoice、つくよみちゃん）；仍不含音箱模式
  #   完整：标准 + 系统依赖 + 唤醒词模型 + 音箱模式所需组件
  case "${DECKMEIDO_PROFILE:-}" in
    minimal|standard|full)
      export DECKMEIDO_SKIP_SERVER=0
      export DECKMEIDO_INSTALL_STT=0 DECKMEIDO_INSTALL_TTS=0 DECKMEIDO_INSTALL_DEPS=0
      # 桌面客户端三档都装；已经装好就不重装，用户显式 DECKMEIDO_SKIP_GUI=1 也照办
      if have_py_module "$py" "PySide6" || [ "${DECKMEIDO_SKIP_GUI:-}" = "1" ]; then
        export DECKMEIDO_SKIP_GUI=1
      else
        unset DECKMEIDO_SKIP_GUI
      fi
      if [ "$DECKMEIDO_PROFILE" = "minimal" ]; then
        dim "最小安装：后端 + Decky 插件 + 桌面客户端；不装语音组件、不下载模型、不装系统依赖（不含音箱模式）"
      else
        export DECKMEIDO_INSTALL_STT=1 DECKMEIDO_INSTALL_TTS=1
        # 已经装好模块和模型就不再重装，避免每次升级都白跑一遍
        if have_py_module "$py" "sherpa_onnx" && [ -f "$DATA_MODELS/sensevoice/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/model.int8.onnx" ]; then
          export DECKMEIDO_INSTALL_STT=0
        fi
        if have_py_module "$py" "piper_plus" && [ -f "$DATA_MODELS/tts/tsukuyomi/tsukuyomi-chan-6lang-fp16.onnx" ]; then
          export DECKMEIDO_INSTALL_TTS=0
        fi
        if [ "$DECKMEIDO_PROFILE" = "full" ]; then
          export DECKMEIDO_INSTALL_DEPS=1
          dim "完整安装：标准内容 + 系统依赖 + 唤醒词模型 + 音箱模式所需组件"
        else
          dim "标准安装：最小内容 + STT/TTS 引擎与所需模型（不含音箱模式）"
        fi
      fi
      if decky_ready 2>/dev/null; then
        export DECKMEIDO_INSTALL_PLUGIN=1
      else
        export DECKMEIDO_INSTALL_PLUGIN=0
      fi
      return 0
      ;;
  esac

  # 1. 服务端运行依赖（必装，但允许用户明确取消）
  if have_py_module "$py" "fastapi"; then
    dim "Python 运行依赖（服务端）：已存在，跳过"
    export DECKMEIDO_SKIP_SERVER=0
  elif opt_download "Python 运行依赖（服务端）" "FastAPI/uvicorn 等，缺了后端跑不起来" \
       "约 30MB" "" y "后端无法启动，网页与桌面客户端都不可用"; then
    export DECKMEIDO_SKIP_SERVER=0
  else
    export DECKMEIDO_SKIP_SERVER=1
    warn "跳过后端依赖，程序可能无法启动；可随时重跑 bash install.sh upgrade"
  fi

  # 2. 桌面客户端（PySide6 + WebEngine）
  if have_py_module "$py" "PySide6"; then
    dim "桌面客户端 PySide6：已存在，跳过"
    export DECKMEIDO_SKIP_GUI=1
  elif opt_download "桌面客户端（PySide6）" "桌面窗口与内置浏览器，不要也能用网页版" \
       "约 700MB" "" y "没有桌面窗口，只能用网页版"; then
    unset DECKMEIDO_SKIP_GUI
  else
    export DECKMEIDO_SKIP_GUI=1
  fi

  # 3. 语音输入（sherpa-onnx + 首次使用自动下载 SenseVoice 模型）
  local stt_model="$DATA_MODELS/sensevoice/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/model.int8.onnx"
  if have_py_module "$py" "sherpa_onnx" && [ -f "$stt_model" ]; then
    dim "语音输入（sherpa-onnx + 模型）：已存在，跳过"
    export DECKMEIDO_INSTALL_STT=0
  elif opt_download "语音输入 STT" "sherpa-onnx 运行库；首次录音再自动下模型（约 240MB）" \
       "约 30MB" "" y "语音输入不可用，聊天与其余功能不受影响"; then
    export DECKMEIDO_INSTALL_STT=1
  else
    export DECKMEIDO_INSTALL_STT=0
  fi

  # 4. 语音合成（piper-plus + つくよみちゃん模型）
  local tts_model="$DATA_MODELS/tts/tsukuyomi/tsukuyomi-chan-6lang-fp16.onnx"
  if have_py_module "$py" "piper_plus" && [ -f "$tts_model" ]; then
    dim "语音合成（piper-plus + 模型）：已存在，跳过"
    export DECKMEIDO_INSTALL_TTS=0
  elif opt_download "语音合成 TTS" "piper-plus + onnxruntime；首次播报再自动下模型（约 38MB）" \
       "约 60MB" "" y "月读酱语音不可用（会退回系统机器人音的替代方案）"; then
    export DECKMEIDO_INSTALL_TTS=1
  else
    export DECKMEIDO_INSTALL_TTS=0
  fi

  # 5. 系统控件依赖（代替用户动手的 ydotool / wtype 等）
  local deps_present=""
  if need_cmd ydotool && need_cmd wtype; then
    deps_present="ydotool/wtype 已在 PATH"
  fi
  if opt_download "系统控件依赖（--deps）" "模拟点击/输入、截图等系统能力，需 sudo 走 pacman/AUR" \
       "约 100MB" "$deps_present" y "模拟点击/输入等系统控制能力不可用"; then
    export DECKMEIDO_INSTALL_DEPS=1
  else
    export DECKMEIDO_INSTALL_DEPS=0
  fi

  # 6. Decky 插件包（已安装时由 install_plugin 自动替换，不再问）
  if decky_ready 2>/dev/null; then
    local pver=""
    pver="$(plugin_installed_version 2>/dev/null)" || pver=""
    if [ -n "$pver" ]; then
      dim "Decky 插件：已安装 v$pver，升级时会自动替换为新版本"
      export DECKMEIDO_INSTALL_PLUGIN=1
    elif opt_download "Decky 插件包" "游戏模式 QAM 面板，需先装 Decky Loader" \
         "约 2MB" "" y "游戏模式 QAM 面板不装（会保留 zip 供手动安装）"; then
      export DECKMEIDO_INSTALL_PLUGIN=1
    else
      export DECKMEIDO_INSTALL_PLUGIN=0
    fi
  else
    dim "Decky 插件：没有检测到 Decky Loader，跳过"
    export DECKMEIDO_INSTALL_PLUGIN=0
  fi
}

# 读一行输入并回显提示，返回用户敲的内容。
#
# 和 ask_yes 不一样，这里**没有默认值**：管道执行（curl | bash）时 stdin 是脚本本身，
# 直接 read 会读到还没执行的脚本文本，所以固定从 /dev/tty 读。
# 拿不到终端（无人值守、ssh 没分配 tty）就返回非 0，由调用方决定怎么退。
#
# 提示一律写 stderr（或 /dev/tty），stdout 只放答案：调用方用的是
# choice=$(ask_choice ...)，提示若走 stdout 会被一起捕获，case 永远匹配不上，
# 还会被 die 再打一遍（照片里的「提示打两遍 + 无效选择」就是这个）。
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
  # 不做任何免密探测：sudo -v 自己会在需要时提示输入密码，预判只会多读一次策略
  info "安装系统依赖需要 root 权限，接下来会提示输入密码"
  sudo -v || { warn "sudo 授权失败，跳过系统依赖安装"; return 1; }
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

# 从本机 pacman 缓存里挑出某个包最新的那份 .pkg.tar.*
# 数据库同步失败（SteamOS 镜像 TLS 过期 / 断网）时，-Sy 必然失败，
# 但包经常已经躺在 /var/cache/pacman/pkg 里：直接 pacman -U 就能装，
# 不依赖数据库也不联网。2026-11-08 装机日志里 ydotool / wtype 就是这样
# 卡住的（设置页后续还报 file:// 地址不受支持）。
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
      # 记录在：再确认命令真的能用。SteamOS 换镜像后 /usr 被重建，
      # 包记录还留着但文件没了，这种情况必须「重装」而不是跳过
      if need_cmd "$p"; then installed+=("$p"); else reinstall+=("$p"); fi
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
  # SteamOS 的根分区是只读的，装包前要先关掉只读保护。
  # 以前这里是 `|| true` 静默吞掉，结果 pacman 报一个跟只读毫无关系的错，
  # 用户完全看不出真正原因。现在失败就直接说清楚。
  if [ -f /etc/steamos-readonly.conf ] || need_cmd steamos-readonly; then
    if ! sudo steamos-readonly disable </dev/null 2>/dev/null; then
      warn "关闭 SteamOS 只读保护失败，系统包无法安装"
      dim "手动执行：sudo steamos-readonly disable   然后再重跑本次命令"
      return 1
    fi
    # 只读是「关掉才能装包」的临时状态：装完（无论成败）都恢复成 enabled，
    # 否则用户的系统会一直停在可写状态，直到自己想起来手动开回去。
    # 用 trap 兜住函数中途返回和 Ctrl-C 的情况。
    trap 'sudo steamos-readonly enable >/dev/null 2>&1 || \
          printf "\033[33m⚠ 恢复 SteamOS 只读保护失败，请手动执行：sudo steamos-readonly enable\033[0m\n" >&2; \
          trap - EXIT INT TERM' EXIT INT TERM
  fi
  # 先装缺的（--needed）；再对「记录在、文件没了」的强制重装（不能带 --needed，
  # 否则 pacman 会认为已装而直接跳过）。-Sy 而不 -Syu，避免顺带滚系统
  local log ok=1 pkgs=()
  [ ${#todo[@]} -gt 0 ] && pkgs+=("${todo[@]}")

  # 离线优先：Release 自带 deploy/deps/ 时先吃本地包，不碰网络。
  # 断网、壁纸引擎抢占带宽、holo 仓库抽风这几种情况下能不能装上，
  # 差别就在这一步。装不上（包不全/依赖对不上）再走原来的联网流程。
  local still=()
  if [ ${#pkgs[@]} -gt 0 ] && [ -d "$OFFLINE_DIR" ]; then
    if offline_pacman_install "${pkgs[@]}"; then
      local p
      for p in "${pkgs[@]}"; do need_cmd "$p" || still+=("$p"); done
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
      # 最常见失败原因：SteamOS 换镜像后 keyring 对不上，签名校验直接报错
      warn "pacman 安装失败，尝试修复 keyring 后重试"
      dim "$(printf '%s' "$log" | tail -5)"
      # keyring 修不好不中止（后面还有离线兜底），但失败原因要留进安装日志：
      # 以前是纯 `|| true`，签名问题只能靠猜
      sudo pacman-key --init </dev/null >/dev/null 2>&1 || warn "pacman-key --init 失败"
      sudo pacman-key --populate archlinux holo </dev/null >/dev/null 2>&1 \
        || warn "pacman-key --populate 失败（keyring 可能损坏，离线包也不一定能验签）"
      if log=$(sudo pacman -Sy --needed --noconfirm "${pkgs[@]}" </dev/null 2>&1); then
        info "修复 keyring 后安装成功"
      else
        # 数据库同步失败（镜像 TLS 过期 / 断网）时 -Sy 必然失败。
        # 三层兜底：本机缓存 → 发布包自带离线包 → 镜像站；
        # 三层都拿不全就如实报缺，绝不在这里打 ✓。
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
  # 装完立刻实测，避免「pacman 说装了但命令不在」的假成功
  local bad=()
  for p in "${todo[@]}" "${reinstall[@]}"; do need_cmd "$p" || bad+=("$p"); done
  if [ ${#bad[@]} -gt 0 ]; then
    warn "以下包装完仍找不到命令，可能是镜像更新冲掉了 /usr：${bad[*]}"
    dim "重跑一次：bash install.sh install --deps"
    DEPS_MISSING_AFTER+=("${bad[@]}")
  fi
  # 只有这次新装上的才记进依赖清单（重装的老包不算「安装器引入」）
  local done_now=()
  for p in "${todo[@]}"; do need_cmd "$p" && done_now+=("$p"); done
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
    info "设置 ydotool 开机自启（之前：${en:-未知}）"
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
# 只查 pacman -Q 不够：SteamOS 换镜像会冲掉 /usr 下的包而数据库仍留记录。
# 所以这里逐项实测「命令在不在 + 真的能不能跑」。
probe_ydotool() {
  need_cmd ydotool || { echo "未安装（模拟鼠标点击、键盘输入）"; return 1; }
  need_cmd ydotoold || { echo "守护进程缺失，需重装 ydotool"; return 1; }
  local sock="/run/user/$(id -u)/.ydotool_socket"
  [ -S "$sock" ] || { echo "ydotoold 守护进程没在跑（$sock 不存在），点击/输入会失败"; return 1; }
  # 只连 socket 验活，不发 mousemove 之类的命令——那会把用户的光标真的挪走。
  # ydotoold 用 SOCK_DGRAM，用 SOCK_STREAM 连会报 Protocol wrong type for socket
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
  # 只查得到二进制不够，还要确认它能跑起来。这里必须设 offscreen：
  # 在无 DISPLAY 的环境（systemd 单元、ssh、cron）下 Qt6 会直接 abort 并写 core dump。
  # 用 --version 而不是 --help，避开 GUI 初始化路径。
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
  echo
  # ydotoold 是否被 systemd 托管，单独一行看清楚：这是「重启后还能不能用」的关键
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
# 清单式同步：rsync 只管「源里有 → 目标也要有」，源里已经删掉的文件会
# 永远留在安装目录里。升级几次之后目录里堆满早已废弃的模块，最坑的是
# 那些文件名还在、但早就没人 import 的旧文件，排查时很容易看错。
#
# 这里反向再走一遍：安装目录里存在、源码目录里已不存在的代码文件，删掉。
# 只处理下面这些已知代码目录，*.log / VERSION 与运行时缓存一概不碰。
CODE_DIRS=(server client assets deploy tools plugin)
# deploy/deps/ 里的离线依赖包也必须放过：它们不在源码目录里，照常删的话
# 每次升级都会把 Release 自带的离线包清空，下次断网就装不上依赖了。
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
  # 删完之后可能留下空目录，一并收掉（只收代码目录内的）
  for d in "${CODE_DIRS[@]}"; do
    [ -d "$INSTALL_DIR/$d" ] || continue
    find "$INSTALL_DIR/$d" -mindepth 1 -type d -empty -delete 2>/dev/null || true
  done
  # updater 升级前会打包一份 update-backup-*.tar.gz，成功后会自己删；
  # 进程半路被杀才会剩下，这里兜底清理，只留最新一份
  local old
  # 文件名可能带空格（安装目录不是我们的名字规则），不能靠 $(ls) 分词
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

# 探一下哪个源最快，返回索引；都连不上返回 -1。
# 不用 ping：ICMP 只能证明「能到」，跟 pip 走 HTTPS 下载快不快没必然关系，
# 而且 mirror 源一般禁 ICMP。这里直接向每个源拉同一段真实文件，
# 按实测下载速率（字节/秒）排，用 curl 的量测数据，不用额外依赖。
PROBE_BYTES=65536      # 每个源只取 64KB，够分辨快慢又不费流量
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

pick_pip_index() {
  local i n=${#PIP_INDEXES[@]} idx=-1
  local -a spd
  need_cmd curl || { printf '%s' 0; return 0; }
  # 注意：本函数靠 stdout 返回下标，所有提示必须走 stderr，否则会污染返回值
  if [ "$n" -gt 1 ]; then
    dim "测速中（各源取 ${PROBE_BYTES} 字节，最多 ${PROBE_MAX}s）" >&2
  fi
  for i in "${!PIP_INDEXES[@]}"; do
    spd[$i]=$(probe_speed "${PIP_INDEXES[$i]}" || echo 0)
    dim "$(human_rate "${spd[$i]}")  ${PIP_INDEXES[$i]}" >&2
    if [ "${spd[$i]}" -gt 0 ] && { [ "$idx" = "-1" ] || [ "${spd[$i]}" -gt "${spd[$idx]}" ]; }; then
      idx=$i
    fi
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

# 字节数 → 「1.2GB」「350MB」「11kB」这类友好体积
human_size() {
  local b="${1:-0}"
  if [ "$b" -ge 1073741824 ]; then
    printf '%.1fGB' "$(awk "BEGIN{print $b/1073741824}")"
  elif [ "$b" -ge 1048576 ]; then
    printf '%.1fMB' "$(awk "BEGIN{print $b/1048576}")"
  elif [ "$b" -ge 1024 ]; then
    printf '%.1fkB' "$(awk "BEGIN{print $b/1024}")"
  else
    printf '%dB' "$b"
  fi
}

# 唤醒词生成（sherpa-onnx-cli text2token）在 sherpa-onnx 之外还要
# sentencepiece / pypinyin，只有完整档需要：缺了就补上，失败只警告。
install_wake_text_deps() {
  local py="$1" start="${2:-0}"
  [ "${DECKMEIDO_PROFILE:-}" = "full" ] || return 0
  [ -x "$py" ] || return 0
  if have_py_module "$py" "sentencepiece" && have_py_module "$py" "pypinyin"; then
    return 0
  fi
  dim "补齐唤醒词分词依赖（sentencepiece / pypinyin）"
  if "$py" -m pip install -q sentencepiece pypinyin -i "${PIP_INDEXES[$start]}" >/dev/null 2>&1 ||
     "$py" -m pip install -q sentencepiece pypinyin >/dev/null 2>&1; then
    dim "唤醒词分词依赖已就绪"
  else
    warn "sentencepiece / pypinyin 没装上：生成唤醒词会失败，可手动 pip 补装"
  fi
  return 0
}

# 语音输入依赖（可选安装项）。$1 python，$2 起始源下标
#
# 策略说明（避免「装到一半卡住」）：
#   DECKMEIDO_INSTALL_STT=1  强制安装
#   DECKMEIDO_INSTALL_STT=0  强制跳过
#   未设置时：交互安装会问一句（默认装，回车=装，输入 n 才跳过）；非交互默认跳过，绝不阻塞。
# 装不上只警告，不影响后端、网页和桌面客户端 —— /api/stt 会如实报 runtime=false。
install_stt_deps() {
  local py="$1" start="${2:-0}"
  [ -x "$py" ] || return 0
  if "$py" -c "import sherpa_onnx" >/dev/null 2>&1; then
    dim "语音输入依赖已就绪（sherpa-onnx）"
    return 0
  fi
  local mode="${DECKMEIDO_INSTALL_STT:-ask}"
  case "$mode" in
    1|yes|on|true) ;;
    0|no|off|false)
      dim "已跳过语音输入依赖（DECKMEIDO_INSTALL_STT=$mode）"
      return 0 ;;
    *)
      if [ ! -t 0 ] && [ ! -r /dev/tty ]; then
        dim "非交互安装：默认跳过语音输入依赖"
        dim "需要时运行：bash $INSTALL_DIR/install.sh install --stt"
        return 0
      fi
      if ! ask_yes "现在安装语音输入依赖（sherpa-onnx，约 30MB）？不装也能正常聊天，之后随时可补"; then
        dim "已跳过语音输入依赖；补装：bash $INSTALL_DIR/install.sh install --stt"
        return 0
      fi
      ;;
  esac
  section "语音输入依赖（可选）"
  if pip_install_with_switch "$py" requirements-stt.txt "$start"; then
    dim "语音输入依赖已就绪"
    return 0
  fi
  warn "语音输入依赖没装上：不影响其他功能，稍后重跑 install --stt 即可"
  return 0
}

# 语音合成依赖（可选安装项）。$1 python，$2 起始源下标
#
# 策略说明（与语音输入一致，避免「装到一半卡住」）：
#   DECKMEIDO_INSTALL_TTS=1  强制安装
#   DECKMEIDO_INSTALL_TTS=0  强制跳过
#   未设置时：交互安装会问一句（默认装，回车=装，输入 n 才跳过）；非交互
#   （无 tty 或 DECKMEIDO_NONINTERACTIVE=1）默认跳过，绝不阻塞。
# piper-plus 2.x 要求 Python 3.11+，版本不够就直接跳过并说明原因。
# 装不上只警告，不影响后端、网页和桌面客户端 —— /api/tts 会如实报 runtime=false。
install_tts_deps() {
  local py="$1" start="${2:-0}"
  [ -x "$py" ] || return 0
  if "$py" -c "import piper_plus" >/dev/null 2>&1; then
    dim "语音合成依赖已存在（piper-plus）"
    install_tts_nltk_data "$py"
    if ! verify_tts "$py"; then
      [ "$TTS_MODEL_STATE" = "missing" ] ||
        warn "语音合成自检未通过（原因见上一条）；不影响其他功能"
    fi
    return 0
  fi
  # 版本不够时硬装只会得到「找不到匹配版本」的报错，先判断再决定
  if ! "$py" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    local pv
    pv=$("$py" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "未知")
    dim "跳过语音合成依赖：piper-plus 需要 Python 3.11+，当前虚拟环境是 $pv"
    dim "升级系统 Python 后重跑：bash $INSTALL_DIR/install.sh install --tts"
    return 0
  fi
  local mode="${DECKMEIDO_INSTALL_TTS:-ask}"
  case "$mode" in
    1|yes|on|true) ;;
    0|no|off|false)
      dim "已跳过语音合成依赖（DECKMEIDO_INSTALL_TTS=$mode）"
      return 0 ;;
    *)
      if [ "${DECKMEIDO_NONINTERACTIVE:-0}" = "1" ] || { [ ! -t 0 ] && [ ! -r /dev/tty ]; }; then
        dim "非交互安装：默认跳过语音合成依赖"
        dim "需要时运行：bash $INSTALL_DIR/install.sh install --tts"
        return 0
      fi
      if ! ask_yes "现在安装语音合成依赖（piper-plus + onnxruntime）？不装也能正常聊天，之后随时可补"; then
        dim "已跳过语音合成依赖；补装：bash $INSTALL_DIR/install.sh install --tts"
        return 0
      fi
      ;;
  esac
  section "语音合成依赖（可选）"
  if pip_install_with_switch "$py" requirements-tts.txt "$start"; then
    # 依赖装上 ≠ 音色可用：模型不随包分发，这里必须如实说清楚缺什么
    if tts_model_present; then
      dim "语音合成依赖已就绪（月读酱模型已存在）"
    else
      warn "语音合成依赖已就绪，但月读酱模型未下载（约 38MB，可在设置页「下载模型」）"
    fi
    install_tts_nltk_data "$py"
    if ! verify_tts "$py"; then
      [ "$TTS_MODEL_STATE" = "missing" ] ||
        warn "语音合成自检未通过（原因见上一条）；不影响其他功能"
    fi
    return 0
  fi
  warn "语音合成依赖没装上：不影响其他功能，稍后重跑 install --tts 即可"
  dim "手动重试：$py -m pip install -r $INSTALL_DIR/requirements-tts.txt"
  return 0
}

# 英文 G2P（piper-plus 自带的 g2p_en）需要 nltk 数据：cmudict + 词性标注器。
# 安装阶段一次性下到 <数据根>/models/nltk_data；运行时联网会被 SSRF 拦截，
# 所以这一步不能留到首次播报再做。
#
# 实测（Steam Deck，DNS 走代理返回 198.18.x）：nltk.download 会被 nltk 自身的
# SSRF 保护拒绝（pathsec.urlopen: SSRF attempt to restricted IP），目录建了但
# 一个文件都没下到，随后英文段整段合成失败。因此先试官方下载器，失败后再用
# curl 直接取官方 zip 解压（不经过 nltk 的网络层），最后按目录校验。
install_tts_nltk_data() {
  local py="$1" dir="$DATA_MODELS/nltk_data"
  [ -x "$py" ] || return 0
  if nltk_data_ok "$dir"; then
    dim "英文语音数据已就绪（$dir）"
    return 0
  fi
  if _retry_download "英文语音数据" _install_tts_nltk_once "$py"; then
    dim "英文语音数据已就绪（$dir）"
  else
    warn "英文语音数据下载失败（已尝试 ${DOWNLOAD_ATTEMPTS} 次）：${DOWNLOAD_ERR}"
    warn "英文段会用 espeak-ng 兜底（中文/日文播报不受影响），重跑 install --tts 可再试"
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
  if nltk_data_ok "$dir"; then
    printf '%s\n' "$out" | sed 's/^/    /'
    dim "英文语音数据已就绪（$dir）"
    return 0
  fi
  printf '%s\n' "$out" | sed 's/^/    /'
  dim "官方下载器没下成功（代理/镜像网络下常见），改用 curl 直取数据包"
  local item sub pkg
  for item in "corpora cmudict" "taggers averaged_perceptron_tagger" "taggers averaged_perceptron_tagger_eng"; do
    sub="${item%% *}"
    pkg="${item#* }"
    [ -d "$dir/$sub/$pkg" ] && continue
    if fetch_nltk_package "$py" "$dir/$sub" "$sub" "$pkg"; then
      dim "已下载 $pkg"
    else
      warn "英文语音数据没下到：$pkg（中文/日文播报不受影响）"
      dim "手动补：curl -L https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/$sub/$pkg.zip -o /tmp/$pkg.zip && unzip -o /tmp/$pkg.zip -d $dir/$sub"
    fi
  done
  nltk_data_ok "$dir" && return 0
  echo "英文语音数据未齐（cmudict / averaged_perceptron_tagger_eng 缺文件）"
  return 1
}

# 安装期所有下载都走这里：最多 3 次，间隔 2s / 4s，逐次打印第几次和失败原因。
# 不静默：最后一次失败的原因留在 DOWNLOAD_ERR，供调用方写进收尾汇总。
# 用法：_retry_download "月读酱模型" <命令> [参数...]
_retry_download() {
  local label="$1"; shift
  local attempt=1 delay=2 out
  DOWNLOAD_ATTEMPTS=0
  DOWNLOAD_ERR=""
  while [ "$attempt" -le 3 ]; do
    DOWNLOAD_ATTEMPTS=$attempt
    if out=$("$@" 2>&1); then
      [ -n "$out" ] && printf '%s\n' "$out" | sed 's/^/    /'
      return 0
    fi
    DOWNLOAD_ERR=$(printf '%s\n' "$out" | tail -n 2 | tr '\n' ' ' | cut -c1-120)
    [ -n "$DOWNLOAD_ERR" ] || DOWNLOAD_ERR="退出码非 0，无输出"
    warn "$label 第 $attempt/3 次尝试失败：$DOWNLOAD_ERR"
    if [ "$attempt" -lt 3 ]; then
      dim "${delay}s 后重试"
      sleep "$delay"
      delay=$((delay * 2))
    fi
    attempt=$((attempt + 1))
  done
  return 1
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
# 模型未下载时无法自检，如实说明，不算失败。
verify_tts() {
  local py="$1"
  local root="$DATA_MODELS/tts/tsukuyomi"
  local onnx="$root/tsukuyomi-chan-6lang-fp16.onnx" cfg="$root/config.json"
  [ -x "$py" ] || return 1
  if [ ! -f "$onnx" ] || [ ! -f "$cfg" ]; then
    warn "语音合成自检：月读酱模型未下载（约 38MB），无法试合成；设置页「下载模型」后可试听"
    TTS_MODEL_STATE="missing"
    return 1
  fi
  local out
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
  ) || {
    warn "语音合成自检未通过："
    printf '%s\n' "$out" | sed 's/^/    /'
    dim "中文可用的前提下，英文失败通常是 nltk 数据缺失；重跑 install --tts 可修复"
    TTS_MODEL_STATE="broken"
    return 1
  }
  printf '%s\n' "$out" | sed 's/^/    /'
  dim "语音合成自检通过（中文 + 英文）"
  TTS_MODEL_STATE="ready"
  return 0
}

# 建 venv + 装依赖。$1 = gui 时只装桌面客户端（PySide6），stt / tts 时只补对应可选依赖。
#
# 依赖分成两份：requirements.txt（服务端，必装）和 requirements-gui.txt
# （PySide6 + WebEngine，解包后 700MB 上下）。以前合成一份，PySide6 一超时
# 整个安装就报「装不上」，网页端其实早就能用了。现在拆开：桌面客户端装失败
# 只警告，后端与网页版照常可用；之后想补装跑 install --gui。
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
      dim "已跳过服务端依赖（可选下载里选择了不装）"
    else
      section "Python 依赖（服务端）"
      if ! pip_install_with_switch "$py" requirements.txt "$start"; then
        warn "服务端依赖没装全，可稍后重跑：bash $INSTALL_DIR/install.sh upgrade"
        dim "手动指定源：DECKMEIDO_PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple bash install.sh upgrade"
        return 1
      fi
      # ptyprocess 是远程终端 / 真 PTY 面板的硬依赖，requirements.txt 里已经列了，
      # 但镜像缺包或老 venv 升级时仍可能漏装。这里单独补一次；终端只是可选能力，
      # 装不上只给明确提示与手动命令，不让整个安装失败，也不影响后端启动。
      if ! have_py_module "$py" "ptyprocess"; then
        dim "补装 ptyprocess（远程终端依赖）"
        if ! "$py" -m pip install -q 'ptyprocess>=0.7,<0.8' \
             -i "${PIP_INDEXES[$start]}" >"$INSTALL_DIR/pip-ptyprocess.log" 2>&1; then
          warn "ptyprocess 没装上：远程终端 / 终端面板将不可用，后端其余功能不受影响"
          dim "安装日志：$INSTALL_DIR/pip-ptyprocess.log"
          dim "手动补齐：$py -m pip install 'ptyprocess>=0.7,<0.8'"
          dim "或重跑：bash $INSTALL_DIR/install.sh upgrade"
        fi
      fi
    fi
    # 语音输入是可选能力：单独询问，失败不影响主流程
    install_stt_deps "$py" "$start"
    # 语音合成同理，且会先检查 Python 3.11+ 再决定是否安装
    install_tts_deps "$py" "$start"
  fi

  if [ -n "${DECKMEIDO_SKIP_GUI:-}" ]; then
    dim "已跳过桌面客户端（DECKMEIDO_SKIP_GUI=1）"
    return 0
  fi
  section "桌面客户端（PySide6，体积较大）"
  if pip_install_with_switch "$py" requirements-gui.txt "$start"; then
    dim "PySide6 已就绪"
    return 0
  fi
  warn "PySide6 没装上：网页版可以正常用，桌面上不会有客户端窗口"
  dim "稍后补装：bash $INSTALL_DIR/install.sh install --gui"
  return 0
}

# 安装阶段的语音模型预取。月读酱模型所有档位都会尝试下载（含 custom），
# 每个下载最多 3 次；三次都失败只警告并写进收尾汇总，不影响安装完成。
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

TTS(enable=False, model_root=paths.tts_model_dir()).download_model()
PY
  )
}

prefetch_voice_models() {
  local profile="${DECKMEIDO_PROFILE:-}" py="$INSTALL_DIR/.venv/bin/python"
  case "$profile" in
    standard|full|custom) ;;
    *) return 0 ;;
  esac
  [ -x "$py" ] || return 0
  mkdir -p "$DATA_MODELS" 2>/dev/null || true

  # 语音识别模型（SenseVoice，约 250MB）：音量太大，仍只在标准/完整档预取
  if [ "$profile" != "custom" ] && have_py_module "$py" "sherpa_onnx" &&
     [ ! -f "$DATA_MODELS/sensevoice/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/model.int8.onnx" ]; then
    section "语音识别模型（SenseVoice）"
    dim "正在下载约 250MB，可能需要几分钟（最多尝试 3 次）"
    if _retry_download "语音识别模型" _prefetch_stt_once; then
      dim "语音识别模型已就绪"
    else
      warn "语音识别模型下载失败（已尝试 ${DOWNLOAD_ATTEMPTS} 次）：${DOWNLOAD_ERR}"
      dim "首次录音会再试，也可重跑 install --stt"
    fi
  fi

  # 语音合成模型（つくよみちゃん，约 40MB）：所有档位都下，装完就能听到女仆音
  local tts_onnx="$DATA_MODELS/tts/tsukuyomi/tsukuyomi-chan-6lang-fp16.onnx"
  if have_py_module "$py" "piper_plus" && [ ! -f "$tts_onnx" ]; then
    section "语音合成模型（つくよみちゃん）"
    dim "正在下载约 40MB（最多尝试 3 次）"
    if _retry_download "月读酱模型" _prefetch_tts_once; then
      dim "语音合成模型已就绪"
      TTS_MODEL_STATE="ready"
    else
      warn "语音合成模型下载失败（已尝试 ${DOWNLOAD_ATTEMPTS} 次）：${DOWNLOAD_ERR}"
      dim "不影响安装；稍后可在设置页「下载模型」重试"
      TTS_MODEL_STATE="failed"
      TTS_MODEL_ERROR="$DOWNLOAD_ERR"
    fi
  fi

  # 唤醒词模型（智能音响模式）只有完整档预下载
  if [ "$profile" = "full" ]; then
    # 唤醒词生成还要 sentencepiece / pypinyin（text2token 依赖）
    install_wake_text_deps "$py" 0
    section "唤醒词模型（智能音响模式）"
    if _retry_download "唤醒词模型" _prefetch_wake_once; then
      dim "唤醒词模型已就绪"
    else
      warn "唤醒词模型下载失败（已尝试 ${DOWNLOAD_ATTEMPTS} 次）：${DOWNLOAD_ERR}"
      dim "首次启用智能音响模式会再试"
    fi
  fi
  return 0
}

_prefetch_wake_once() {
  local py="$INSTALL_DIR/.venv/bin/python"
  ( cd "$INSTALL_DIR" && "$py" - <<'PY'
from server import paths, wake

wake.ensure_model(paths.wake_model_dir())
PY
  )
}

# ---------- 音箱模式（智能音响）前置检查 ----------
# 该模式只在完整档预置（唤醒词模型 + 录音工具 + busctl + Decky Music）。
# 其它档位不预置，但安装结尾会明确列出缺什么，避免用户在设置页打开开关后
# 只看到「起不来」却不知道原因。
wake_mode_notes() {
  local profile="${DECKMEIDO_PROFILE:-}" py="$INSTALL_DIR/.venv/bin/python" miss=() m
  have_py_module "$py" "sherpa_onnx" || miss+=("sherpa-onnx 运行时（唤醒检测）")
  [ -n "$(find "$DATA_MODELS/wake" -maxdepth 3 -name 'encoder*.onnx' -print -quit 2>/dev/null || true)" ] ||
    miss+=("唤醒词模型（KWS + silero VAD，约 38MB）")
  { need_cmd parec || need_cmd pw-record; } || miss+=("parec / pw-record（常驻录音工具）")
  need_cmd busctl || miss+=("busctl（systemd，音量与媒体控制）")

  if [ ${#miss[@]} -eq 0 ]; then
    dim "智能音响模式所需组件齐全（唤醒词模型、录音工具、busctl）"
  else
    if [ "$profile" = "full" ]; then
      warn "智能音响模式组件仍缺以下项，启用时会提示原因："
    else
      dim "智能音响模式未预置（${profile:-自定义}档不含音箱模式组件），缺少："
    fi
    for m in "${miss[@]}"; do dim "  - $m"; done
    dim "补齐方式：bash install.sh install --full（完整档）"
  fi

  # Decky Music 是独立插件，安装器不打它；只做提示，不影响唤醒与问答
  if [ "$profile" = "full" ]; then
    local found_music=0 root d
    for root in "${DECKY_PLUGINS_DIR:-}" "$HOME/homebrew/plugins"; do
      [ -n "$root" ] || continue
      for d in "$root"/*usic*; do
        if [ -d "$d" ]; then found_music=1; break; fi
      done
    done
    if [ "$found_music" = 1 ]; then
      dim "Decky Music 插件已检测到（音乐播控 / 点歌可用）"
    else
      dim "音乐播控 / 点歌还需 Decky Music 插件（到 Decky 商店安装并登录，不影响唤醒问答）"
    fi
  fi
  return 0
}

# 带进度条与换源的 pip install。
#   $1 python，$2 requirements 文件名，$3 起始源下标（默认 0）
# 为什么要包一层：pip 下大包时很久不吐一行输出，以前又用了 -q，用户看到的
# 就是「卡在 pip 镜像源那一步」。这里让 pip 把输出写进日志、放后台跑，
# 前台每秒重画一次状态行：当前包 / 已下载字节 / 总字节 / 下载速度 / 剩余时间。
pip_install_with_switch() {
  local py="$1" req="$2" start="${3:-0}"
  local n=${#PIP_INDEXES[@]} i k ok=0 log="$INSTALL_DIR/pip-install.log"
  local ret=0 pid t0 cur got total rate prev_got base_got=0
  # pip 下载的 wheel 先落缓存目录、下载完才装进 venv：下载阶段 venv 体积
  # 不动、缓存增长，只盯 venv 会把下载速度误判成 0。所以「已下载」取
  # venv + 缓存 两者体积之和，任何阶段都有增量可测。
  local cache_dir="${XDG_CACHE_HOME:-$HOME/.cache}/pip"
  # 进度按「本次新增」算：先把起点体积记下来，百分比才不会从 60% 起步
  base_got=$(du -sb "$INSTALL_DIR/.venv" "$cache_dir" 2>/dev/null | awk '{s += $1} END {print s+0}')
  base_got=${base_got:-0}
  for ((i = 0; i < n; i++)); do
    k=$(((start + i) % n))
    if [ "$i" -gt 0 ]; then dim "换源重试：${PIP_INDEXES[$k]}"; fi
    # --progress-bar off：进度由我们自己画，不让 pip 的 \r 刷屏搅乱
    "$py" -m pip install -r "$INSTALL_DIR/$req" \
      --progress-bar off --timeout 60 --retries 3 \
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
      dim "$req 装好了（venv $(human_size "$(du -sb "$INSTALL_DIR/.venv" 2>/dev/null | awk '{print $1}')")）"
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
# 用 venv 体积增量当「下载速度」，用日志里 Downloading 行累加出的总大小
# 减已落盘字节当「剩余量」，两者相除得「剩余时间」。
# 只在真终端里用 \r 原地重画；输出被重定向到文件/管道时只打普通行，不刷屏。
_spin='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
_progress_on=0
_progress_last=""

progress_clear() {
  if [ "$_progress_on" = 1 ]; then printf '\r\033[K'; fi
  _progress_on=0
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
  # 速度 0 不代表「连不上」：pip 下载大包时先落缓存、还没装进 venv，
  # 体积采样可能还没动，这里统一叫「测速中」，别吓用户
  if [ "$rate" -gt 0 ]; then
    spd_text="$(human_rate "$rate")"
  else
    spd_text="测速中"
  fi
  if [ "$OUT_IS_TTY" != 1 ]; then
    # 非终端：当前包变了才打一行，重定向到日志时也能看出进度
    if [ -n "$cur" ] && [ "$cur" != "$_progress_last" ]; then
      printf '    [%s] %s  %s  %s  %s\n' "$pct_text" "$size_text" "$spd_text" "$eta_text" "$cur"
      _progress_last="$cur"
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

# 工作区：安装/升级都保证它存在，幂等；已存在时一律不动里面内容。
# 首次创建时顺手放两个说明文件，让用户在 Dolphin 里一眼看懂它是什么：
#   .directory  KDE 文件夹备注（标准 Desktop Entry，右键属性里能看到）
#   说明.txt    给用户看的纯文本
# 这两个文件只在目录首次创建时写；已存在就完全不动，避免升级覆盖用户改过的内容。
# 写入失败只记一行日志，绝不让安装失败。
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

# 后端只由插件（游戏模式）和桌面客户端拉起，安装器不再生成或启用任何
# systemd 服务单元，也不提供需要用户手动 enable 的入口。

# 停掉后端进程本身（不是客户端）。
#
# 为什么单独停：后端是用 start_new_session 拉起的，客户端被杀不会带走它；
# 而且客户端启动时如果后端已经在跑（例如游戏模式下 Decky 插件先拉起了核心），
# 它退出时会刻意不碰那个进程。旧后端还占着端口时，新后端在 singleton.guard
# 里会直接退出，客户端只能报「后端核心启动失败」——表现就是升级后第一次
# 打不开、再走一次升级（此时旧进程已经自己退出）才正常。
# 只认 `python -m uvicorn server.main:app`，避免误杀 runuser 包装或安装脚本。
# 端口上监听的进程 pid（ss 优先，退化到 lsof）
_port_pids() {
  local port="$1"
  if need_cmd ss; then
    ss -ltnp "sport = :$port" 2>/dev/null | sed -nE 's/.*pid=([0-9]+).*/\1/p' | sort -u
  elif need_cmd lsof; then
    lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | sort -u
  fi
}

# 进程判据只有两条，故意不要求路径匹配：
#   1. 同一个 UID（不是 root、也不是别的用户的进程）；
#   2. 命令行含 server.main:app 或 client/main.py。
# 为什么不再要求 cwd == $INSTALL_DIR：用户可能用 nohup 从旧目录拉起过一份，
# 安装目录改名/删除后，那个进程的 cwd 会显示成 "/path (deleted)"，命令行里
# 也可能只有相对模块名 —— 旧判据认不出它，于是第 2 步静默跳过，新后端又被
# singleton 挡在端口外，安装器只看到「v未知」（0.7.3 首次安装起不来的根因）。
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
# 装完仍跑着旧版本，多半是安装前就有一个脱管的后端没停干净，
# 该由第 2 步（停机）负责；这里只是最后的兜底，报一句能马上执行的话。
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
# 插件只在装了 Decky Loader 的机器上有意义。先问框架在不在，不在就整段跳过，
# 不构建、不报错 —— 大部分桌面模式用户根本没装 Decky。
#   deploy.sh --detect：0 = 框架在，2 = 没装（1 当没装处理，宁可跳过也别乱装）
PLUGIN_SKIP_REASON=""

decky_ready() {
  local d="$INSTALL_DIR/plugin/deploy.sh" rc=0
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
    dim "跳过 Decky 插件：${PLUGIN_SKIP_REASON:-没装 Decky Loader}"
    dim "想用游戏模式面板，先装 Decky Loader（可选依赖），再跑：bash $INSTALL_DIR/plugin/deploy.sh"
    plugin_handoff_zip
    return 0
  fi
  # 动手之前先体检 Decky。已经卡在重启循环时再去重启它，只会把循环坐实，
  # 而这一步完全可以在复制文件之前就判断出来——先提示，用户可以先重启 Deck。
  local hrc=0
  decky_health || hrc=$?
  case "$hrc" in
    1)
      warn "Decky 当前异常：$DECKY_HEALTH_NOTE"
      warn "本次不会重启 Decky；插件文件仍会正常部署，装完请先重启 Deck，再到 设置 → Decky Loader 点「重新载入」" ;;
    2)
      dim "Decky 状态未知：$DECKY_HEALTH_NOTE" ;;
    3)
      dim "Decky 已安装但服务没在运行；插件会先部署，装完自动启动 Decky" ;;
    *)
      dim "Decky 状态正常（$DECKY_HEALTH_NOTE）" ;;
  esac
  # 已经装过插件就是升级：直接替换，不再问。以前这里走 ask_yes（默认 N），
  # 用户在终端按回车就把插件跳过了，升级后插件一直停在旧版本（0.6.11 就是这么来的）。
  local installed=""
  installed="$(plugin_installed_version 2>/dev/null)" || installed=""
  if [ -n "$installed" ]; then
    info "检测到已安装 Decky 插件 v${installed}，升级自动替换"
    if plugin_install_zip; then
      local ver=""
      ver="$(plugin_installed_version)" || ver=""
      if [ -n "$ver" ]; then
        if [ "$ver" = "$installed" ]; then
          dim "插件版本仍是 v${ver}（zip 与已装版本相同）"
        else
          info "插件已更新：v${installed} → v${ver}"
        fi
      fi
      plugin_reload || true
    else
      warn "插件自动更新失败，可手动重装：bash $INSTALL_DIR/plugin/deploy.sh"
    fi
    return 0
  fi
  # 内联升级（updater.py 用 start_new_session + stdin=/dev/null 拉起本脚本）
  # 拿不到终端，ask_yes 永远回答「不」，插件就被静默跳过、版本停在旧的。
  # 这类无人值守的升级必须自动装：用户既然装过插件，升级时就该跟着换。
  if [ "${DECKMEIDO_NONINTERACTIVE:-0}" = "1" ] || { [ ! -t 0 ] && [ ! -r /dev/tty ]; }; then
    info "非交互升级：自动更新 Decky 插件"
    if plugin_install_zip; then
      local ver
      ver="$(plugin_installed_version)" && info "插件已安装 v${ver}"
      plugin_reload || true
    else
      PLUGIN_RELOAD_STATE="install_failed"
      warn "插件自动更新失败，可手动重装：bash $INSTALL_DIR/plugin/deploy.sh"
    fi
    return 0
  fi
  # 可选下载里已经确认过（=0 跳过 / =1 装）；没确认过才在这里问
  case "${DECKMEIDO_INSTALL_PLUGIN:-ask}" in
    0|no|off|false)
      dim "已跳过 Decky 插件（可选下载里选择了不装）"
      PLUGIN_RELOAD_STATE="skipped"
      plugin_handoff_zip
      return 0 ;;
    1|yes|on|true)
      info "安装 QAM 插件（zip 解包，不使用软链接）"
      if plugin_install_zip; then
        local ver
        ver="$(plugin_installed_version)" && info "插件已安装 v${ver}"
        plugin_reload || true
      else
        PLUGIN_RELOAD_STATE="install_failed"
        warn "插件安装失败，可手动重装：bash $INSTALL_DIR/plugin/deploy.sh"
      fi
      return 0 ;;
  esac
  # 检测到 Decky，但装不装让用户定：插件要写进 ~/homebrew/plugins，属于系统级改动
  if ask_yes "检测到 Decky Loader，现在就在游戏模式里装上 QAM 面板吗？（输入 y 后按回车；直接按 A/回车=不装）"; then
    info "安装 QAM 插件（zip 解包，不使用软链接）"
    # 直接解 zip 到 ~/homebrew/plugins/DeckMeido：和 Decky 自己的安装器同一套产物，
    # 插件目录自洽，删掉安装目录也不影响它加载。
    # deploy.sh 保留给「装完还想重新解一次」的场景，这里不再走它。
    if plugin_install_zip; then
      local ver
      ver="$(plugin_installed_version)" && info "插件已安装 v${ver}"
      # 首次安装不主动重启 Decky（游戏模式里正开着，重启会打断用户），
      # 但状态要记成「待重载」，收尾卡片不能写成「已生效」
      PLUGIN_RELOAD_STATE="${PLUGIN_RELOAD_STATE:-pending}"
      dim "插件已就绪，到 设置 → Decky Loader 点「重新载入」即可在 QAM 里看到"
    else
      PLUGIN_RELOAD_STATE="install_failed"
      warn "插件安装失败，可手动重装：bash $INSTALL_DIR/plugin/deploy.sh"
    fi
  else
    PLUGIN_RELOAD_STATE="skipped"
    plugin_handoff_zip
  fi
  return 0
}

# 把发布包里的插件 zip 解到 ~/homebrew/plugins/DeckMeido。
# zip 由 release.py 打（前端已编译，无需 npm），顶层是 DeckMeido/ 目录。
plugin_install_zip() {
  # 优先用发布包里带的那份（deploy/deckmeido-plugin.zip，由 release.py 从 dist/ 拷进来）
  local src="$INSTALL_DIR/deploy/deckmeido-plugin.zip"
  local root="${DECKY_PLUGINS_DIR:-$HOME/homebrew/plugins}"
  local target="$root/DeckMeido"
  [ -f "$src" ] || { dim "安装包里没带插件包（$src），跳过"; return 1; }
  need_cmd unzip || { dim "缺少 unzip，跳过"; return 1; }
  # 旧版本可能是软链接装进来的：先删链接本身，别顺着链接把安装目录删了
  if [ -L "$target" ]; then
    rm -f "$target" 2>/dev/null || sudo rm -f "$target"
  fi
  # 插件根目录属 root:root，普通用户建不了/删不掉，借一次 sudo 动本插件自己的目录
  sudo -n mkdir -p "$target" 2>/dev/null ||
    sudo mkdir -p "$target" 2>/dev/null || { dim "建不动 $target"; return 1; }
  # 清旧文件再解：zip 里没有的旧文件（改过名的模块）留着会让 Decky 加载到过期代码
  safe_rm_target "$target" || { dim "拒绝清理插件目录（$target）"; return 1; }
  echo "[删除] $target"
  sudo rm -rf "$target" 2>/dev/null || true
  sudo mkdir -p "$target" || return 1
  sudo unzip -oq "$src" -d "$root" || return 1
  # zip 顶层目录名是 DeckMeido，解到 $root 下正好生成 $target
  if ! plugin_verify_layout "$target"; then
    dim "插件目录校验失败：$PLUGIN_LAYOUT_ERROR"
    return 1
  fi
  return 0
}

# 插件目录自检：dist/index.js（前端产物）、plugin.json（Decky 清单）、
# package.json（版本）三者必须齐全且版本一致。
# 为什么要查版本一致性：release.py 分开更新这两个文件，只要漏一个，Decky 面板
# 显示的版本和实际跑的代码就对不上，用户报障时完全没法判断装的是哪一版。
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
  # 版本号以 package.json 为准：Decky 自己只读 package.json 的 version 显示在
  # QAM 里，官方模板的 plugin.json 只有 name/author/flags/api_version。
  # 2026-11-08 装机日志：把「plugin.json 没有 version」当成校验失败，卸载旧插件
  # 之后又把自家的 zip 判死，结果插件目录被删干净、QAM 里再也打不开。
  if [ -z "$vp" ]; then
    PLUGIN_LAYOUT_ERROR="package.json 里没有 version 字段：$pkg"
    return 1
  fi
  if [ -n "$vj" ] && [ "$vj" != "$vp" ]; then
    PLUGIN_LAYOUT_ERROR="版本不一致：plugin.json=$vj，package.json=$vp（打包漏更新了一个）"
    return 1
  fi
  # 权限：Decky 以 root 加载，用户读不到也不影响运行，但说明文件被人改成了 0600，
  # 顺手报出来，免得以后排查时又多一层迷雾
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
  local pid cmd out=""
  command -v pgrep >/dev/null 2>&1 || return 0
  for pid in $(pgrep -f 'PluginLoader|plugin_loader' 2>/dev/null || true); do
    [ "$pid" = "$$" ] && continue
    cmd="$( { tr '\0' ' ' < "/proc/$pid/cmdline"; } 2>/dev/null || true )"
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

# 动手前给 Decky 体检。返回 0 = 可用；1 = 已异常（绝不能再去重启它）；
# 2 = 查不了（没有 systemctl / 没装 Decky，不算故障）；
# 3 = 装了但服务没在跑（没启动 / 被停用），不是故障，start 一下就行。
# 判据三条一起看：is-active 是不是 active、journal 近 2 分钟有没有 1337 bind 失败、
# 1337 是否真的能连上。只看第一条会被重启循环骗过去（那期间 is-active 一直是 active）。
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

# Decky 重启循环探测（安装与卸载共用）：重启后必须回读 NRestarts，不能只看 is-active。
#
# 为什么必须查：plugin_loader.service 用的是 KillMode=process，systemctl restart 只杀主进程，
# 旧子进程继续占着 127.0.0.1:1337；新实例绑不上端口就退出，Restart=always 又把它拉起来，
# 于是变成每 7 秒一轮的重启循环（2026-11-08 实测 271 次，还把 Decky Music 拖到 10GB）。
# 这期间 is-active 一直显示 active，只看它就会报「已重载」，完全误导。
#
# 判定：15 秒内 NRestarts 增长，或近 30 秒 journal 里出现 1337 的 address already in use。
# 命中只置 DECKY_LOOP_NOTE 并返回 1，由调用方记进各自的失败清单——绝不中断流程，
# 因为这时安装/卸载可能已经做了一半，半途停下比继续更糟。
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
#
# 为什么不用 systemctl restart：unit 是 KillMode=process，restart 只杀 PyInstaller
# 主进程，十几个子进程还留在旧 cgroup 里继续持有 1337；systemd 立刻拉起新实例，
# 新实例绑不上端口就退出，Restart=always 再拉起来 —— 直接进入无限重启循环
# （2026-11-08 实测 271 次，Decky 全挂、Decky Music 涨到 10GB）。
# 所以必须 stop（让 systemd 按自己的 TimeoutStopSec 收尾）→ 确认残留真的没了
# → 才 start。残留没清干净就绝不再 start，宁可让用户重启 Deck。
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
    # 等它真正停干净。上限取服务自己的 TimeoutStopSec（15s），再长也没意义
    tmo="$(systemctl show -p TimeoutStopSec --value "$unit" 2>/dev/null || true)"
    tmo="${tmo%s}"
    case "$tmo" in ''|*[!0-9]*) tmo=15 ;; esac
    [ "$tmo" -gt 20 ] && tmo=20
    [ "$tmo" -lt 3 ] && tmo=3
    if ! decky_wait_stopped "$unit" "$tmo"; then
      DECKY_SAFE_STATE="failed"
      DECKY_SAFE_NOTE="停止 Decky 后仍有残留进程（PID: $(decky_residual_procs)）占着 1337；已放弃重载，避免触发重启循环。请重启 Deck 后再继续"
      return 1
    fi
  else
    # 服务本来就没在跑（没启动 / 被停用）：不需要 stop，但必须先确认没有残留子进程
    # 占着 1337——否则 start 一上来就会撞端口，直接进重启循环。
    local res
    res="$(decky_residual_procs)"
    if [ -n "$res" ]; then
      DECKY_SAFE_STATE="failed"
      DECKY_SAFE_NOTE="Decky 服务已停止，但仍有残留进程（PID: $res）占着 1337；已放弃重载，请重启 Deck 后再继续"
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
  # 动手前先体检：已经卡在循环里还去重启它，只会把循环坐实。
  # 注意 3 是「装了但没在跑」——那不是故障，后面 start 一下就行，不能跳过。
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
# DeckMeido 卸载入口（由 install.sh 自动生成，逻辑只有 install.sh 的 do_uninstall 一份）
#
#   bash uninstall.sh                  卸载：安装器文件全部移入回收站，保留 userdata/ 与工作区
#   bash uninstall.sh --dry-run        只列计划，零改动（系统包也只列不删）
#   bash uninstall.sh --purge          连工作区、userdata、日志、模型、配置备份一起移入回收站
#
# 所有模式都只把东西丢进回收站，绝不永久删除；userdata/ 只有 --purge 才移走。
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
  # 客户端冷启动要依次：起 Qt、拉起后端、uvicorn 加载、第一次渲染页面。
  # 以前只等 3×0.3 秒，新装/大版本升级（venv 冷启动、PySide6 首次解压缓存）
  # 几乎必然超时，用户看到一条「未能自动启动」，其实界面几秒后就出来了。
  # 这里给到 30 秒，进程已经退出（启动失败）就提前放弃，不白等。
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
    ready)  card_line "语音模型     已就绪（月读酱）" ;;
    failed) card_line "语音模型     下载失败（已尝试 3 次，原因：$(printf '%s' "${TTS_MODEL_ERROR:-未知}" | cut -c1-60)）" ;;
    broken) card_line "语音模型     校验失败（设置页可重新下载）" ;;
    *)      card_line "语音模型     未下载（约 38MB，设置页「下载模型」）" ;;
  esac
  # 用户显式跳过的可选项：一行列全，不留「怎么少了东西」的疑问
  if [ ${#SKIPPED_ITEMS[@]} -gt 0 ]; then
    skipped=$(IFS='、'; printf '%s' "${SKIPPED_ITEMS[*]}")
    card_line "已跳过：$skipped（相应功能降级，可之后单独补装）"
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
  # 系统控件缺了就是缺了：这一行必须和设置页「可补装组件」显示的一致，
  # 不能再出现「上面打勾、下面全红」的矛盾
  if [ ${#DEPS_MISSING_AFTER[@]} -gt 0 ]; then
    card_line "系统控件     缺 $(IFS='、'; printf '%s' "${DEPS_MISSING_AFTER[*]}")（联网后重跑 check --fix）"
  fi
  card_line "本机控制台   http://127.0.0.1:$PORT/"
  card_line "局域网访问   http://$ip:$PORT/（新设备点「配对新设备」，按掌机上的 PIN 配对一次）"
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
# 六步版式：检查环境 → 停止旧进程 → 下载并校验主程序 → 可选依赖 →
# 部署插件与桌面入口 → 启动后端（并校验运行版本）。
# 每一步只留一行结论；原始输出全进 userdata/logs/install-<时间>.log。
do_install() {
  local deps="" gui_only=0 stt_only=0 tts_only=0
  for a in "$@"; do
    case "$a" in
      --deps) deps="--deps" ;;
      --gui)  gui_only=1 ;;   # 只补装桌面客户端（PySide6），不动别的
      --stt)  stt_only=1 ;;   # 只补装语音输入依赖（sherpa-onnx）
      --tts)  tts_only=1 ;;   # 只补装语音合成依赖（piper-plus）
      # 安装档位：三档 + 自定义（逐项询问）
      --minimal|--standard|--full)  DECKMEIDO_PROFILE="${a#--}" ;;
      --custom|--advanced)          DECKMEIDO_PROFILE=custom ;;
      --profile=*)                  DECKMEIDO_PROFILE="${a#--profile=}" ;;
    esac
  done
  case "${DECKMEIDO_PROFILE:-}" in
    ""|minimal|standard|full|custom) ;;
    *) die "未知安装档位：$DECKMEIDO_PROFILE（可选 minimal / standard / full / custom）" ;;
  esac
  export DECKMEIDO_PROFILE="${DECKMEIDO_PROFILE:-}"
  # 只补装某一项依赖：不换代码、不停机，走原来的轻量路径
  check_user
  if [ "$tts_only" = 1 ] || [ "$stt_only" = 1 ] || [ "$gui_only" = 1 ]; then
    check_user
    start_install_log
    sync_files
    ensure_data_layout
    ensure_workspace || true
    if [ "$tts_only" = 1 ]; then
      DECKMEIDO_INSTALL_TTS=1 venv_install tts
    elif [ "$stt_only" = 1 ]; then
      DECKMEIDO_INSTALL_STT=1 venv_install stt
    else
      venv_install gui
    fi
    write_install_records
    return 0
  fi

  check_user
  print_banner
  start_install_log
  # 全新安装：交互时弹三档菜单（默认标准）；命令行/环境变量给了档位就不再问
  choose_install_profile standard
  STEP_QUIET=1
  reset_steps
  local disk_ver="" had_old=0

  # [1/6] 检查环境
  step "检查环境"
  check_prereq
  preflight
  step_ok "安装目录 $INSTALL_DIR，端口 $PORT"

  # [2/6] 停止旧进程
  # 换文件前必须没有旧后端占着端口：用户可能用 nohup 手动起过一份，
  # 它不是安装器拉起的 pid，只按 pid 记录停不掉（0.7.1 装完仍是 0.7.0 的根因）。
  #
  # 动手改任何东西之前先抓一份「机器原本是什么样」（只在第一次安装时抓，
  # 升级不覆盖）。卸载时靠它判断哪些路径/包是我们带来的、ydotool.service
  # 安装前是什么状态，做到「除了回收站以外，机器没做任何其他改动」。
  capture_install_baseline
  step "停止旧进程"
  [ -n "$(_backend_pids)" ] && had_old=1
  [ -n "$(_client_pids)" ] && had_old=1
  if ! stop_runtime; then
    step_fail "旧进程没停掉，为安全起见没有替换任何文件"
    die "请先按上面的提示手动停掉旧进程，再重跑安装"
  fi
  if [ "$had_old" = 1 ]; then
    step_ok "旧后端与客户端已停止"
  else
    step_skip "没有运行中的后端 / 客户端"
  fi

  # [3/6] 下载并校验主程序（本地源码安装就是同步文件）
  step "下载并校验主程序"
  sync_files
  disk_ver="$(head -n1 "$INSTALL_DIR/VERSION" 2>/dev/null || true)"
  step_ok "程序文件已就位：$INSTALL_DIR（v${disk_ver:-未知}）"
  ensure_data_layout
  prepare_config
  step_ok "配置已就绪：$DATA_CONFIG"
  # 之前卸载时备份的人设/记忆搬回来，让「她」还是原来的她
  restore_soul
  ensure_workspace || true

  # [4/6] 可选依赖
  step "可选依赖"
  optional_downloads
  # 依赖没装全不该让整个安装中断：代码已经同步好了，联网问题用户之后能自行重试
  if venv_install; then
    step_ok "Python 运行环境已就绪"
  else
    step_note "运行依赖没装全，可重跑：bash $INSTALL_DIR/install.sh upgrade"
  fi
  # 标准 / 完整档：把语音模型一起下好（最小档什么都不下）
  prefetch_voice_models
  if [ "$deps" = "--deps" ] || [ "${DECKMEIDO_INSTALL_DEPS:-0}" = "1" ]; then
    deps_intro
    maybe_autorepair_after_image_update
    if install_deps; then
      step_ok "系统控件依赖已处理"
    else
      local _drc=$?
      if [ "$_drc" = 2 ]; then
        step_skip "系统控件依赖（没拿到 sudo 权限，一个都没装；之后补装：bash $INSTALL_DIR/install.sh check --fix）"
      else
        # 以前这里是 install_deps || true + 无条件 step_ok：装不上也打勾，
        # 用户进设置页却看到一片「未安装 / 点修复也修不好」，无从判断信谁。
        step_fail "系统控件没装全（缺：${DEPS_MISSING_AFTER[*]:-见上}）"
        next_hint "设置页的缺失项就是这里没装上的；联网后重跑：bash $INSTALL_DIR/install.sh check --fix"
      fi
    fi
  else
    system_notes
    step_skip "系统控件依赖（之后想装：bash install.sh install --deps）"
  fi
  # 音箱模式（智能音响）只在完整档预置；其它档位把缺的组件列清楚
  wake_mode_notes

  # [5/6] 部署插件与桌面入口
  step "部署插件与桌面入口"
  install_desktop
  install_uninstaller
  install_plugin
  # Decky 会在插件加载时自动建 settings/data/logs/<插件名>；装完先占上，
  # 保证清单里一定记着这三处（否则卸载时它们会成为「插件没卸干净」）。
  # 没装 Decky 时不能建：会在没有 Decky 的机器上凭空造出 ~/homebrew/{settings,data,logs}，
  # 看起来像装过 Decky，卸载时还得再清一遍。
  if decky_ready 2>/dev/null; then
    local _dd
    while IFS= read -r _dd; do
      mkdir -p "$_dd" 2>/dev/null || true
    done < <(decky_plugin_dirs)
  fi
  # 所有安装动作结束后再落标记/清单，确保 venv、桌面项、插件都记录在内
  write_install_records
  step_ok "桌面入口、Decky 插件与安装清单已写入"

  # [6/6] 启动后端并校验运行版本
  step "启动后端"
  # 服务端依赖被显式跳过时后端必然起不来；这里跳过启动而不是报错中断，
  # 否则「用户选择不装」会被当成安装失败（汇总里有「已跳过」一行说明影响）。
  if [ "${DECKMEIDO_SKIP_SERVER:-0}" = "1" ]; then
    step_skip "已跳过启动后端（服务端依赖未安装）"
    dim "补装依赖后重跑：bash $INSTALL_DIR/install.sh upgrade"
    print_done
    return 0
  fi
  if ! relaunch_runtime; then
    step_fail "后端没能启动"
    next_hint "手动启动：cd $INSTALL_DIR && .venv/bin/python -m uvicorn server.main:app --host 127.0.0.1 --port $PORT"
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
  local deps="" gui_only=0
  for a in "$@"; do
    case "$a" in
      --deps) deps="--deps" ;;
      --gui)  gui_only=1 ;;
      --minimal|--standard|--full)  DECKMEIDO_PROFILE="${a#--}" ;;
      --custom|--advanced)          DECKMEIDO_PROFILE=custom ;;
      --profile=*)                  DECKMEIDO_PROFILE="${a#--profile=}" ;;
    esac
  done
  if [ "$gui_only" = 1 ]; then deps=""; fi
  case "${DECKMEIDO_PROFILE:-}" in
    ""|minimal|standard|full|custom) ;;
    *) die "未知安装档位：$DECKMEIDO_PROFILE（可选 minimal / standard / full / custom）" ;;
  esac
  export DECKMEIDO_PROFILE="${DECKMEIDO_PROFILE:-}"
  [ -d "$INSTALL_DIR" ] || die "$INSTALL_DIR 不存在，请先执行安装"
  check_user
  print_banner
  # 升级只换代码和依赖，下面这些一律保留，用户不用担心升级丢数据
  dim "升级会保留：userdata/ 下的配置、人设、长期记忆、会话与上传文件，"
  dim "以及工作区；Decky 插件已装过会自动替换为新版本。"
  start_install_log
  # 升级默认进逐项询问（自定义）：没装过的组件也会按默认值补上，
  # 不想装的项显式输入 n；显式给了 --minimal / --standard / --full 才按档位执行
  [ "$gui_only" = 1 ] || choose_install_profile custom
  STEP_QUIET=1
  reset_steps
  local disk_ver="" had_old=0

  # [1/6] 检查环境
  step "检查环境"
  check_prereq
  preflight
  step_ok "安装目录 $INSTALL_DIR，端口 $PORT"

  # [2/6] 停止旧进程
  # 顺序要紧：先停客户端（否则它的 watchdog 会把旧后端拉回来），
  # 再把可能已经「脱管」的后端进程也收掉，保证换文件时没有旧进程占着端口
  step "停止旧进程"
  [ -n "$(_backend_pids)" ] && had_old=1
  [ -n "$(_client_pids)" ] && had_old=1
  if ! stop_runtime; then
    step_fail "旧进程没停掉，为安全起见没有替换任何文件"
    die "请先按上面的提示手动停掉旧进程，再重跑升级"
  fi
  if [ "$had_old" = 1 ]; then
    step_ok "旧后端与客户端已停止"
  else
    step_skip "没有运行中的后端 / 客户端"
  fi

  # [3/6] 下载并校验主程序
  step "下载并校验主程序"
  sync_files
  disk_ver="$(head -n1 "$INSTALL_DIR/VERSION" 2>/dev/null || true)"
  step_ok "程序文件已就位：$INSTALL_DIR（v${disk_ver:-未知}）"
  ensure_data_layout
  ensure_workspace || true

  # [4/6] 可选依赖
  step "可选依赖"
  if [ "$gui_only" = 1 ]; then
    venv_install gui || true
    step_ok "桌面客户端依赖已处理（只补装 PySide6，其余未动）"
  else
    optional_downloads
    if venv_install; then
      step_ok "Python 运行环境已就绪"
    else
      step_note "运行依赖没装全，可重跑：bash $INSTALL_DIR/install.sh upgrade"
    fi
    # 升级时补齐语音模型（已有就跳过；自定义档下 STT 模型不预取，TTS 模型照下）
    prefetch_voice_models
    if [ "$deps" = "--deps" ] || [ "${DECKMEIDO_INSTALL_DEPS:-0}" = "1" ]; then
      maybe_autorepair_after_image_update
      if install_deps; then
        step_ok "系统控件依赖已处理"
      else
        local _drc=$?
        if [ "$_drc" = 2 ]; then
          step_skip "系统控件依赖（没拿到 sudo 权限，一个都没装；之后补装：bash $INSTALL_DIR/install.sh check --fix）"
        else
          step_fail "系统控件没装全（缺：${DEPS_MISSING_AFTER[*]:-见上}）"
          next_hint "设置页的缺失项就是这里没装上的；联网后重跑：bash $INSTALL_DIR/install.sh check --fix"
        fi
      fi
    else
      system_notes
      step_skip "系统控件依赖（之后想装：bash install.sh upgrade --deps）"
    fi
    # 音箱模式（智能音响）只在完整档预置；其它档位把缺的组件列清楚
    wake_mode_notes
  fi

  # [5/6] 部署插件与桌面入口
  step "部署插件与桌面入口"
  if [ "$gui_only" = 1 ]; then
    write_install_records
    step_skip "只补装桌面客户端，插件与桌面入口未改动"
  else
    install_desktop
    install_uninstaller
    install_plugin
    write_install_records
    step_ok "桌面入口、Decky 插件与安装清单已更新"
  fi

  # [6/6] 启动后端并校验运行版本
  step "启动后端"
  # 与全新安装一致：服务端依赖被显式跳过时不把「起不来」当升级失败
  if [ "${DECKMEIDO_SKIP_SERVER:-0}" = "1" ]; then
    step_skip "已跳过启动后端（服务端依赖未安装）"
    dim "补装依赖后重跑：bash $INSTALL_DIR/install.sh upgrade"
    print_done
    return 0
  fi
  if ! relaunch_runtime; then
    step_fail "后端没能启动"
    next_hint "手动启动：cd $INSTALL_DIR && .venv/bin/python -m uvicorn server.main:app --host 127.0.0.1 --port $PORT"
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
# 边下边报：文件名 / 已下载 / 总大小 / 百分比 / 速度。裸 curl -o 在 Deck 的
# 小屏上等于「没有任何反馈」，几十 MB 的包会被当成卡死。
# 非终端（重定向/日志）时只在百分比跨过 10% 时打一行，避免刷屏。
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

# 管道执行时「装 / 升 / 卸」怎么定：
#   没装   → 什么都不问，直接装（一行命令的默认用途）
#   装了   → 必须手动选「升级」还是「卸载」，不设默认值
# 之所以不给默认值：回车就升级的话，想卸载的人一旦手滑就直接覆盖安装，
# 卸载入口等于藏起来了。这里强制敲 1 或 2。
# 结果写进 REMOTE_ACTION（不用命令替换捕获：横幅与提示都走 stdout，捕获会把它们混进来）
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
  # 子命令明确传下去：上面已经问过「升级还是卸载」，不能让子脚本再猜一次；
  # 额外的档位参数（--minimal / --full 等）也一并转发
  DECKMEIDO_MIRROR="$MIRROR" bash "$src/install.sh" "$REMOTE_ACTION" "$@"
}

# 管道执行 / 一行命令自举：先镜像，镜像不可用再 GitHub。
do_from_piped() {
  need_cmd curl || die "需要 curl：先装 curl（sudo pacman -S curl）"
  remote_action_or_exit

  print_banner
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
  # 额外参数（--minimal / --standard / --full 等）一并转发给子脚本
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

# 卸载前把「她这个人」的数据备份到 ~/.config/deckmeido/，普通卸载只删代码、
# 不丢人设和记忆；下次安装时自动恢复。--purge 才会连这份备份一起删，
# 但无论哪种模式，userdata/ 都整目录保留。
# 备份两份文件：meidosoul.json（人设）+ memory.json（长期记忆/偏好）。
backup_soul() {
  local dst="$CONFIG_DIR"
  mkdir -p "$dst"
  local saved=0
  local soul="$DATA_PERSONA" mem="$DATA_MEMORY"
  if [ -f "$soul" ]; then
    cp -f "$soul" "$dst/meidosoul.json" 2>/dev/null && saved=1
  fi
  if [ -f "$mem" ]; then
    cp -f "$mem" "$dst/memory.json" 2>/dev/null && saved=1
  fi
  # 只有安装器创建的配置目录才允许日后 --purge 删除，必须留下标记与清单
  write_config_records || true
  [ "$saved" = 1 ] && dim "已备份人设与记忆到 $dst（下次安装自动恢复）"
  # 没有可备份文件时上面的 && 列表返回 1，不能让 set -e 把卸载中断
  return 0
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

# ---------- 卸载辅助 ----------
DATA_KEEP_NOTE="$DATA_DIR/README-KEPT.txt"

# 在保留的数据目录里留一份说明，避免用户以后对着一堆文件不知道能不能删
write_keep_note() {
  [ -d "$DATA_DIR" ] || return 0
  cat > "$DATA_KEEP_NOTE" <<EOF 2>/dev/null || return 0
DeckMeido 已经卸载，这个目录是特意保留的用户数据。

里面保存的是：
  · config.toml            设置（含 API Key）
  · persona/               人设（meidosoul.json）
  · memory.json            长期记忆
  · sessions/              对话历史
  · uploads/  logs/        上传与日志
  · models/                语音识别模型（如果有）
  · meidobot/              QQ 通道归档

为什么保留：下次重新安装时会直接接上，不用重新配置、也不会丢记忆。
怎么彻底删除：确认不再使用后，把整个目录丢进回收站即可，例如：
  gio trash "$DATA_DIR"
如果本目录位于安装目录内，连安装目录一起丢进回收站也可以。
EOF
  info "保留说明已写入 $DATA_KEEP_NOTE"
}

# server/ 里运行期生成的东西（__pycache__、日志、临时分片）不在安装清单里，
# 单独清一遍，否则卸载完 server/ 还会剩一堆「未识别文件」
cleanup_generated_leftovers() {
  local d="$INSTALL_DIR/server" p
  [ -d "$d" ] || return 0
  while IFS= read -r p; do
    [ -n "$p" ] || continue
    trash_path "$p" >/dev/null || true
  done < <(find "$d" -path "$d/userdata" -prune -o -type d -name '__pycache__' -print 2>/dev/null)
  while IFS= read -r p; do
    [ -n "$p" ] || continue
    trash_path "$p" >/dev/null || true
  done < <(find "$d" -path "$d/userdata" -prune -o -type f \
    \( -name '*.log' -o -name '*.part' -o -name '*.tmp' -o -name '*.pyc' \) -print 2>/dev/null)
}

# ---------- 系统包取证（pacman.log + pacman -Qi + 包目录签名） ----------
# 没有安装清单的老安装，只能从 /var/log/pacman.log 取证。按安装时间窗分三类：
#   W 窗口内新装        → 候选，可删（还要过「三方交叉验证」）
#   U 窗口内升级但原本已有 → 只记录，永远不删（那是用户的包）
#   O 早于窗口的老包      → 永远不删（SteamOS 基线 / 用户自己装的）
# 交叉验证：log 事件落在窗口内 + pacman -Qi 的 Install Date 不早于窗口 + 包落盘的
# 文件确实是 DeckMeido 签名。三者都成立才允许删，少一条只提示、不动手。
# 不写死任何 SteamOS 包名表。
# 直接用上面 DEPS_PACMAN / DEPS_AUR / DEPS_AUR_FALLBACK 那份清单：以前这里
# 又抄了一份，加包时两处漏一处就会「装了却不在可卸清单里」。
PAC_DEPS=("${DEPS_PACMAN[@]}" "${DEPS_AUR[@]}" "${DEPS_AUR_FALLBACK[@]}")

# 包落盘里是否有 DeckMeido 痕迹。系统包（espeak-ng 之类）本身不会装我们的文件，
# 所以只看「这个包提供的文件里有没有落在安装目录/配置目录里的、并且确实存在」
# 那就说明它确有文件落在我们地盘上。没有痕迹不代表不能删——只表示证据少一条。
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
# 绝不碰 SteamOS 自带组件或用户原本就有的包，
# 也不用 -Rns 级联删依赖。删包前必须列清单并询问，--purge 也不静默删。
# requires_confirm=0 时只在 dry-run 下调用（只打印，不问）。
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
# 卸载要把 enable 还原：只有「安装前不是 enabled、本次由我们 enable」的才 disable；
# 原本就 enabled 的不动（那是用户自己的设置）。
# 证据优先级：基线快照 > 清单里的 unit: 行（旧清单没有 → 认为是安装器 enable 的）。
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

# 一次卸载的回收站汇总：把「丢哪儿了、丢了多少、要不要自己清」说清楚
print_trash_summary() {
  local n="$TRASH_COUNT" root
  root="$(trash_root)"
  if [ "$DRY_RUN" = 1 ]; then
    info "以上是计划（--dry-run），没有改动任何文件"
    return 0
  fi
  if [ "$n" -eq 0 ]; then
    dim "本次没有文件需要移走"
  else
    info "已把 $n 项移入回收站：$root"
    dim "回收站里的内容需要你自己清空（在文件管理器里清空回收站即可）。"
    dim "不清空也不影响以后重新安装。"
  fi
  if [ "${TRASH_FAILED:-0}" -gt 0 ]; then
    warn "有 $TRASH_FAILED 项没能移走（多半是权限），见上方提示"
  fi
  return 0
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
    # </dev/null：piped 执行时 stdin 还挂着外层管道，子进程（pacman / paru）
    # 若去读 stdin 会把脚本剩下的部分吃掉，表现为「静默退出、提示符直接回来」
    sudo -n "$@" </dev/null; rc=$?
  elif [ -t 0 ] || { true </dev/tty; } 2>/dev/null; then
    # 密码框由 sudo 自己从 /dev/tty 读，不占 stdin，所以这里重定向是安全的
    sudo "$@" </dev/null; rc=$?
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
  local rc=0
  [ "${#UNINSTALL_FAILS[@]}" -gt "$UN_STEP_FAIL_BASE" ] && rc=1
  un_log "== step $UN_STEP exit=$rc =="
}
un_step() {
  un_step_close
  UN_STEP=$((UN_STEP + 1))
  UN_STEP_TITLE="$1"
  UN_STEP_FAIL_BASE=${#UNINSTALL_FAILS[@]}
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
# 这里只用免密 sudo：trap 里不能弹密码框，否则退出流程会卡住。
# 2026-11-08 实测卸载停在 [6/9] 之后静默退出：没有报错、没有汇总，
# 用户完全不知道发生了什么。所以 trap 里必须把「哪一步没跑完」说出来。
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
  TRASH_LAST_DIR="$root"
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

# 全机的后端进程（不限安装目录、不限用户）。为什么不能用安装目录匹配：
# 上次卸载日志写「已停止」，但 uvicorn 其实是 root 从旧目录拉起的，
# 命令行里根本没有当前 $INSTALL_DIR，按目录匹配就漏了，端口一直被占到卸载结束。
# 模式写成 [s]erver.main:app 是为了让 pkill 自己的命令行不匹配自己。
_all_backend_pids() {
  command -v pgrep >/dev/null 2>&1 || return 0
  pgrep -f '[s]erver\.main:app' 2>/dev/null | tr '\n' ' ' | sed 's/ $//' || true
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
  # 还有残留（典型是 root 拉起的那份）就提权再杀一轮
  if [ -n "$left" ]; then
    un_note "仍有后端进程（$left），提权结束"
    run_root pkill -TERM -f '[s]erver\.main:app' >/dev/null 2>&1 || true
    for i in $(seq 1 10); do
      left="$(_all_backend_pids)"
      [ -z "$left" ] && break
      sleep 0.3
    done
  fi
  if [ -n "$left" ]; then
    run_root pkill -KILL -f '[s]erver\.main:app' >/dev/null 2>&1 || true
    sleep 0.5
    left="$(_all_backend_pids)"
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
  un_ok "已结束插件进程（回读确认）"
  UN_PLUGIN_STATE="已结束（回读确认）"
  return 0
}

# 卸载时的 Decky 重载：绝不用 systemctl restart（KillMode=process 会把 Decky
# 打进重启循环，就是 2026-11-08 那次事故），一律走 stop → 等残留退出 → start。
# 而且先体检：卸载过程中 Decky 若已经卡在循环里，直接跳过重载并提示，
# 不能让卸载成为压垮 Decky 的最后一根稻草。
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
  TRASH_LAST_DIR=""

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
  # 2026-11-08 实测：这一步是整条卸载流程里唯一会去动 Decky 服务的地方，
  # 那次执行停在 stop 之后、start 之前，脚本静默退出，终端的提示符直接回来，
  # 汇总没打印、只读层也没恢复。放在最后 + 不让失败中断，才能保证
  # 「卸载要么全做完，要么只剩这一步没做」。
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
