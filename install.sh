#!/usr/bin/env bash
# DeckMeido 一键安装 / 升级 / 卸载
#
#   bash install.sh              无参数时自动判断：没装就直接装，装了则询问「升级 / 卸载 / 退出」（须手动选择）
#   bash install.sh menu         弹出交互菜单，手动选「安装 / 升级 / 卸载」
#   bash install.sh install       直接全新安装（跳过菜单）
#   bash install.sh install --deps      顺带用 sudo 安装控制类插件（不再询问）
#   bash install.sh install --gui       只补装桌面客户端（PySide6），不动别的
#   bash install.sh upgrade        升级代码与依赖，保留配置
#   bash install.sh upgrade --deps      升级后顺带检查并安装控制类插件
#   bash install.sh check          自检：逐个实测控制类控件是否真的能用
#   bash install.sh uninstall     卸载（删代码与依赖，保留人设/记忆，下次安装自动恢复）
#   bash install.sh uninstall --purge   额外删安装器创建的配置备份（userdata/ 仍保留）
#   bash install.sh service       渲染 systemd 用户单元（只生成不启用，后端由客户端拉起）
#   bash install.sh from-mirror   从国内镜像站下载最新版并安装（GitHub 不通时用）
#   bash install.sh speedtest     只测各 pip 源的下载速率，挑最快的那个
#
# 控制类插件（ydotool / kdotool / wtype / espeak-ng / fakeroot）是系统包，
# 装它们需要 root：默认只在结尾提示并询问一次（y/N），回答 y 才走 sudo。
#
# 为什么装完还要自检：SteamOS 换镜像（BUILD_ID 变动）时，/usr 下由 pacman
# 装的包会被整片冲掉，而 pacman 的数据库记录不一定同步，所以只查
# `pacman -Q` 会出现「记录在、命令没了」的假象。install.sh check 一律实测。
# 同理 ydotoold 必须由 systemd 用户单元托管（enabled），裸跑的进程重启即失效。
#
# 怎么装（最傻瓜的一条命令，装完能反复用来自动升级 / 卸载）：
#   curl -sL https://meido.meagames.cn/deckmeido/install.sh | bash
#   —— 第一次跑：全新安装；之后再跑同一条：询问「升级 / 卸载」，选完才继续
#   想手动选操作（含卸载）：下载脚本后 bash install.sh menu
#   git 方式（需要本地源码目录时）：git clone ... && cd deckmeido && bash install.sh
#   已有脚本、只想走镜像拉最新版：bash install.sh from-mirror
#
# 再跑第一条 git clone 时若报「目标路径已存在，并且不是一个空目录」，
# 说明 deckmeido 目录已存在——直接 cd deckmeido && bash install.sh 升级即可，
# 不用删目录重新 clone。
#
# 环境变量：
#   DECKMEIDO_DIR        安装目录，默认 ~/deckmeido
#   DECKMEIDO_PORT       服务端口，默认 18765
#   DECKMEIDO_MIRROR     镜像站目录，默认 https://meido.meagames.cn/deckmeido
#   DECKMEIDO_PIP_INDEX  pip 源；留空则实测各源下载速率，自动用最快的那个
#   DECKMEIDO_SKIP_GUI   设为 1 则跳过 PySide6（只要网页版 / 后端时用）
#
# 注意：不要用 sudo 执行。脚本会把程序装到当前用户家目录、注册用户级桌面项，
#       用 root 装会装到 /root 下，桌面会话里起不来；也不注册开机自启服务。
#
# 2026-11-04 事故：卸载验证误把源码工作区当安装目录，rm -rf 清空了整个仓库（含
# .git）。此后卸载/清理必须先过 safe_rm_target，拒绝源码工作区与危险路径。
# 2026-11-04 进一步改造：卸载不再整块删除安装目录，改为按 .deckmeido-manifest
# 逐项删除；目录必须带 .deckmeido-install 标记才算「安装器创建的」，否则一律
# 拒绝卸载（即使设置了 DECKMEIDO_ALLOW_UNSAFE_UNINSTALL=1 也只删清单文件、
# 绝不 rmdir），userdata/ 永远保留。
# 用户可修改数据统一放 <安装目录>/userdata（可用 DECKMEIDO_DATA_DIR 覆盖）：
# config.toml、persona/meidosoul.json、memory.json、sessions/、uploads/、
# meidobot/。只保留这一套标准布局；升级同步排除整个 userdata/，卸载按清单
# 删除代码、userdata/ 整目录保留。
set -euo pipefail

APP=deckmeido
INSTALL_DIR="${DECKMEIDO_DIR:-$HOME/$APP}"
PORT="${DECKMEIDO_PORT:-18765}"
# 国内镜像站（纯静态目录：latest.json / deckmeido-<ver>.tar.gz / SHA256SUMS / install.sh）
MIRROR="${DECKMEIDO_MIRROR:-https://meido.meagames.cn/deckmeido}"
MIRROR="${MIRROR%/}"
GITHUB_REPO="Congmeims/deckmeido"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/$APP"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="deckmeido.service"
DESKTOP_DIR="$HOME/.local/share/applications"
# 真正的「桌面」目录：可能是 Desktop 也可能是 桌面，取 xdg 配置，没配置就默认 Desktop
DESKTOP_FOLDER="$(command -v xdg-user-dir >/dev/null 2>&1 && xdg-user-dir DESKTOP 2>/dev/null || true)"
[ -n "$DESKTOP_FOLDER" ] && [ "$DESKTOP_FOLDER" != "$HOME" ] || DESKTOP_FOLDER="$HOME/Desktop"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# 安装器写下的标记与清单：只有带标记的目录才允许卸载；清单记录本次安装
# 实际铺进安装目录的条目，卸载时只按清单逐项删除，清单外的文件一律不动。
INSTALL_MARKER="$INSTALL_DIR/.deckmeido-install"
INSTALL_MANIFEST="$INSTALL_DIR/.deckmeido-manifest"
CONFIG_MARKER="$CONFIG_DIR/.deckmeido-config"
CONFIG_MANIFEST="$CONFIG_DIR/.deckmeido-config-manifest"

# ---------- 用户数据根（userdata/） ----------
# 所有用户可修改的数据统一放在数据根下：默认 $INSTALL_DIR/userdata，可用
# DECKMEIDO_DATA_DIR 显式覆盖（服务单元也按这个变量注入）。卸载永远整目录
# 保留，安装清单里也不会出现这里面的任何路径。
DATA_DIR="${DECKMEIDO_DATA_DIR:-$INSTALL_DIR/userdata}"
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

# 是否通过「curl | sh -」管道执行。管道执行时脚本不在磁盘上，$BASH_SOURCE[0]
# 不是真实文件，sync_files 依赖的 $HERE/* 会指向错误位置；此时自动走
# from-mirror（下载完整发布包再安装），让「一行命令来回用」成为可能。
_is_piped() {
  local src="${BASH_SOURCE[0]:-}"
  # 管道执行时 BASH_SOURCE[0] 通常是空 / "bash" / "sh" / "-"，不是真实文件
  [ -n "$src" ] && [ -f "$src" ] && return 1
  return 0
}

# ---------- 输出 ----------
if [ -t 1 ]; then
  C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_DIM=$'\033[2m'
  C_B=$'\033[1m'; C_C=$'\033[36m'; C_END=$'\033[0m'
else
  C_OK=""; C_WARN=""; C_ERR=""; C_DIM=""; C_B=""; C_C=""; C_END=""
fi
info() { printf '%s==>%s %s\n' "$C_OK" "$C_END" "$*"; }
warn() { printf '%s[!]%s %s\n' "$C_WARN" "$C_END" "$*"; }
die()  { printf '%s[x]%s %s\n' "$C_ERR" "$C_END" "$*" >&2; exit 1; }
dim()  { printf '%s    %s%s\n' "$C_DIM" "$*" "$C_END"; }

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

# 守卫 + 打印具体目标 + 真正删除；任何一步失败都以非 0 退出
remove_tree() {
  local target="${1:-}"
  if ! safe_rm_target "$target"; then
    die "拒绝删除：$target（疑似源码工作区或危险路径）"
  fi
  echo "[删除] $target"
  rm -rf -- "$target"
}

# ---------- 安装标记与文件清单 ----------
# 2026-11-04 事故后再加一层：卸载只认安装器自己写的清单，不再整块删目录。
# 清单行格式：
#   server/xxx            安装目录内的单个文件（相对路径）
#   dir:client            安装目录内的整目录（安装器完全管理的部分）
#   ext:/abs/path         安装目录之外、由安装器创建的路径（服务单元/桌面项/插件）
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

# 外部条目白名单：只允许删安装器自己创建的那几个固定路径
ext_path_allowed() {
  local p="${1%/}" root
  case "$p" in
    "${UNIT_DIR%/}/$UNIT"|"${DESKTOP_DIR%/}/DeckMeido.desktop"|"${DESKTOP_FOLDER%/}/DeckMeido.desktop")
      return 0 ;;
  esac
  root="${DECKY_PLUGINS_DIR:-$HOME/homebrew/plugins}"
  [ "$p" = "${root%/}/DeckMeido" ] && return 0
  return 1
}

# 删除清单里的单个具体路径（目录递归、文件单删）；插件目录属 root 时补一次 sudo
remove_manifest_path() {
  local abs="$1"
  echo "[删除] $abs"
  if [ -d "$abs" ] && [ ! -L "$abs" ]; then
    rm -rf -- "$abs" 2>/dev/null || sudo -n rm -rf -- "$abs" 2>/dev/null || true
  else
    rm -f -- "$abs" 2>/dev/null || sudo -n rm -f -- "$abs" 2>/dev/null || true
  fi
}

# 按清单逐项删除安装目录内条目。返回通过 LAST_REMOVED 报告实际删除条数。
delete_install_manifest() {
  local manifest="$1" line rel abs
  LAST_REMOVED=0
  LAST_SKIPPED=0
  [ -f "$manifest" ] || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    [ -n "$line" ] || continue
    case "$line" in
      \#*) continue ;;
      dir:*)
        rel="${line#dir:}"
        if ! manifest_rel_ok "$rel"; then
          warn "跳过清单中的异常目录条目：$line"
          LAST_SKIPPED=$((LAST_SKIPPED + 1))
          continue
        fi
        abs="$INSTALL_DIR/$rel"
        [ -e "$abs" ] || [ -L "$abs" ] || continue
        remove_manifest_path "$abs"
        LAST_REMOVED=$((LAST_REMOVED + 1))
        ;;
      ext:*)
        abs="${line#ext:}"
        if ! ext_path_allowed "$abs"; then
          warn "跳过清单中的外部条目：$abs"
          LAST_SKIPPED=$((LAST_SKIPPED + 1))
          continue
        fi
        [ -e "$abs" ] || [ -L "$abs" ] || continue
        remove_manifest_path "$abs"
        LAST_REMOVED=$((LAST_REMOVED + 1))
        ;;
      *)
        rel="$line"
        if ! manifest_rel_ok "$rel"; then
          warn "跳过清单中的异常文件条目：$line"
          LAST_SKIPPED=$((LAST_SKIPPED + 1))
          continue
        fi
        abs="$INSTALL_DIR/$rel"
        [ -e "$abs" ] || [ -L "$abs" ] || continue
        echo "[删除] $abs"
        rm -f -- "$abs" 2>/dev/null || true
        LAST_REMOVED=$((LAST_REMOVED + 1))
        ;;
    esac
  done < "$manifest"
  return 0
}

# 删完文件后，只收掉「因清单文件被删而变空」的父目录；rmdir 非空目录会失败，
# 所以用户放在这些目录里的文件不会被牵连。
cleanup_empty_manifest_dirs() {
  local manifest="$1" line rel dirs=""
  [ -f "$manifest" ] || return 0
  while IFS= read -r line; do
    case "$line" in
      ""|\#*|dir:*|ext:*) continue ;;
    esac
    rel="$line"
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

# 写入/刷新 .deckmeido-install 与 .deckmeido-manifest。只记录安装器负责的条目：
#   · 整目录管理的：.venv client assets deploy tools plugin
#   · server 逐文件列出，跳过日志与运行时缓存
#   · 顶层文件：requirements、README、install.sh、VERSION、uninstall.sh 等
#   · 安装目录之外的固定路径（服务单元、桌面项、Decky 插件）
write_install_records() {
  [ -d "$INSTALL_DIR" ] || return 0
  local ver tmp="$INSTALL_MANIFEST.tmp" it rel root
  ver="$(head -n1 "$INSTALL_DIR/VERSION" 2>/dev/null || true)"
  [ -n "$ver" ] || ver="unknown"
  {
    printf 'format=1\n'
    printf 'app=%s\n' "$APP"
    printf 'version=%s\n' "$ver"
    printf 'installed_at=%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    printf 'installer_path=%s\n' "$HERE/install.sh"
    printf 'install_dir=%s\n' "$INSTALL_DIR"
  } > "$INSTALL_MARKER" 2>/dev/null || return 0

  : > "$tmp" 2>/dev/null || return 0
  for it in .venv client assets deploy tools plugin; do
    [ -d "$INSTALL_DIR/$it" ] && printf 'dir:%s\n' "$it" >> "$tmp"
  done
  if [ -d "$INSTALL_DIR/server" ]; then
    while IFS= read -r rel; do
      [ -n "$rel" ] || continue
      printf 'server/%s\n' "$rel" >> "$tmp"
    done < <(cd "$INSTALL_DIR/server" && find . -type f \
      ! -path './userdata/*' ! -path '*/__pycache__/*' ! -name '*.log' \
      -printf '%P\n' 2>/dev/null)
  fi
  for it in requirements.txt requirements-gui.txt README.md install.sh RELEASE-NOTES.md VERSION uninstall.sh pip-install.log; do
    [ -f "$INSTALL_DIR/$it" ] && printf '%s\n' "$it" >> "$tmp"
  done
  [ -f "$UNIT_DIR/$UNIT" ] && printf 'ext:%s\n' "$UNIT_DIR/$UNIT" >> "$tmp"
  [ -f "$DESKTOP_DIR/DeckMeido.desktop" ] && printf 'ext:%s\n' "$DESKTOP_DIR/DeckMeido.desktop" >> "$tmp"
  [ -f "$DESKTOP_FOLDER/DeckMeido.desktop" ] && printf 'ext:%s\n' "$DESKTOP_FOLDER/DeckMeido.desktop" >> "$tmp"
  root="${DECKY_PLUGINS_DIR:-$HOME/homebrew/plugins}"
  [ -d "$root/DeckMeido" ] && printf 'ext:%s\n' "$root/DeckMeido" >> "$tmp"
  mv -f "$tmp" "$INSTALL_MANIFEST" 2>/dev/null || rm -f "$tmp"
  dim "安装清单：$INSTALL_MANIFEST（$(wc -l < "$INSTALL_MANIFEST" 2>/dev/null | tr -d ' ') 项）"
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

purge_config_dir() {
  local line rel abs
  if [ ! -f "$CONFIG_MARKER" ]; then
    warn "拒绝删除配置目录：$CONFIG_DIR 缺少安装器标记 .deckmeido-config"
    return 0
  fi
  if [ -f "$CONFIG_MANIFEST" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
      [ -n "$line" ] || continue
      case "$line" in
        /*|*..*)
          warn "跳过配置清单中的异常条目：$line"
          continue
          ;;
      esac
      abs="$CONFIG_DIR/$line"
      [ -e "$abs" ] || [ -L "$abs" ] || continue
      echo "[删除] $abs"
      rm -f -- "$abs" 2>/dev/null || true
    done < "$CONFIG_MANIFEST"
    echo "[删除] $CONFIG_MANIFEST"
    rm -f -- "$CONFIG_MANIFEST" 2>/dev/null || true
  fi
  echo "[删除] $CONFIG_MARKER"
  rm -f -- "$CONFIG_MARKER" 2>/dev/null || true
  if [ -z "$(find "$CONFIG_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
    echo "[删除] $CONFIG_DIR"
    rmdir "$CONFIG_DIR" 2>/dev/null || true
  else
    info "保留目录：$CONFIG_DIR（含未识别文件）"
  fi
}


# ---------- 文本宽度 ----------
# 终端里中文占 2 列，按字符数排版框线一定会歪，这里按显示宽度算。
# 只覆盖中日韩 + 全角标点，够本脚本用了（纯 bash，不依赖 wc -L）
disp_len() {
  local s="$1" i cp w=0
  for ((i = 0; i < ${#s}; i++)); do
    printf -v cp '%d' "'${s:i:1}"
    # UTF-8 首字节之外的位置 "'x" 拿到的是字节值，汉字会 >127，按 2 列处理
    if [ "$cp" -ge 9472 ] || { [ "$cp" -ge 128 ] && [ "$cp" -lt 256 ]; }; then
      w=$((w + 2))
    else
      w=$((w + 1))
    fi
  done
  printf '%s' "$w"
}

# 终端够不够宽：中文按 2 列算，banner 最宽约 70 列
term_cols() {
  local c="${COLUMNS:-}"
  [ -n "$c" ] || c=$(tput cols 2>/dev/null || echo 80)
  printf '%s' "${c:-80}"
}
ui_fits() { [ -t 1 ] && [ "$(term_cols)" -ge 72 ]; }

HRULE="────────────────────────────────────────────────────────────"

# 分区标题：┌─ 名称 ───────
section() {
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
card() {
  if ui_fits; then printf '\n%s┌%s┐%s\n' "$C_C" "$HRULE" "$C_END"; else echo; fi
}
card_line() {
  local text="$1" pad
  if ui_fits; then
    pad=$((58 - $(disp_len "$text")))
    [ "$pad" -lt 0 ] && pad=0
    printf '%s│%s %s%*s%s│%s\n' "$C_C" "$C_END" "$text" "$pad" "" "$C_C" "$C_END"
  else
    printf '  %s\n' "$text"
  fi
}
card_title() {
  local text="$1" pad
  if ui_fits; then
    pad=$((58 - $(disp_len "$text")))
    [ "$pad" -lt 0 ] && pad=0
    printf '%s│%s %s%s%s%*s%s│%s\n' "$C_C" "$C_END" "$C_B" "$text" "$C_END" "$pad" "" "$C_C" "$C_END"
  else
    printf '%s\n' "$text"
  fi
}
card_end() {
  if ui_fits; then printf '%s└%s┘%s\n' "$C_C" "$HRULE" "$C_END"; fi
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
BUILD_ID="$(sed -n 's/^BUILD_ID=//p' /etc/os-release 2>/dev/null | tr -d '"' | head -n1)"

# ---------- 前置检查 ----------
check_user() {
  if [ "$(id -u)" = "0" ]; then
    die "不要用 sudo / root 运行。请直接执行：bash install.sh（会自动装到 $HOME/$APP）"
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
  need_cmd systemctl || warn "没有 systemctl，将不会注册开机自启服务"
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

  # 2. 端口占用：只看是不是别人占着。已经被 DeckMeido 自己占着是正常情况
  #    （升级时后端就在跑），不能因此拦住安装
  if need_cmd ss; then
    local holder
    # 按冒号切开取最后一段精确比对，避免 :18765 顺带匹配到 187650 这类别的端口
    holder=$(ss -ltnp 2>/dev/null | awk -v p="$PORT" '
      { n = split($4, a, ":"); if (n > 1 && a[n] == p) { print $6; exit } }')
    if [ -n "$holder" ]; then
      if printf '%s' "$holder" | grep -qi "$APP\|python"; then
        dim "端口 $PORT 由 DeckMeido 自己在用（升级时正常）"
      else
        warn "端口 $PORT 被别的进程占用：$holder"
        dim "先关掉它，或换端口：DECKMEIDO_PORT=18766 bash install.sh"
        problems=1
      fi
    else
      dim "端口 $PORT 空闲"
    fi
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
# 默认在安装结尾说明用途并询问一次，或加 --deps 直接装。
# fakeroot 不是运行时控件，但用 paru/yay 编译 AUR 包（kdotool）必须要它，
# 缺了会在 makepkg 阶段报「fakeroot not found」，所以一起装。
DEPS_PACMAN=(espeak-ng ydotool wtype fakeroot)
# AUR 里叫 kdotool，编译出来很慢；优先用预编译的 kdotool-bin，
# 找不到 AUR helper 才提示手动装。
DEPS_AUR=(kdotool-bin)
DEPS_AUR_FALLBACK=(kdotool)

# Release 里附带的离线包目录（deploy/deps/，随发布包一起分发）。
# SteamOS 换镜像会重建 /usr，pacman 缓存也会失效，所以不能指望
# /var/cache/pacman/pkg 里还有东西；这些 .pkg.tar.zst 是独立一份。
OFFLINE_DIR="$HERE/deploy/deps"

# 离线包的安装记录。值是一组包名，记录「上次凭离线包装成功的都有谁」。
# 用途有两个：镜像更新后知道该重装哪些，以及 offline_stale 判断
# 离线包是不是已经过期（比系统仓库还老就别硬装）。
offline_stamp() { printf '%s' "$CONFIG_DIR/.deps-offline"; }

# 用本地包直接装，不碰网络。缺点是不会自动补齐依赖树以外的版本约束，
# 所以装完仍然实测一遍命令在不在。
offline_pacman_install() {
  local want=("$@") files=() p
  [ -d "$OFFLINE_DIR" ] || return 1
  for p in "${want[@]}"; do
    local hit
    hit=$(find "$OFFLINE_DIR" -maxdepth 1 -name "$p-*.pkg.tar.zst" 2>/dev/null | sort | tail -n1)
    [ -n "$hit" ] && files+=("$hit")
  done
  [ ${#files[@]} -gt 0 ] || return 1
  info "从离线包安装：$(printf '%s ' "${files[@]##*/}")"
  local log
  if log=$(sudo pacman -U --noconfirm --needed "${files[@]}" 2>&1); then
    info "离线包安装完成"
  else
    warn "离线包安装失败，稍后回退联网"
    dim "$(printf '%s' "$log" | tail -12)"
    return 1
  fi
  # 记录装成的那几个（用包名前缀文件名去掉版本号）
  local names=() base
  for p in "${files[@]}"; do
    base="$(basename "$p")"
    names+=("${base%%-[0-9]*}")
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
      dim "包内自带离线依赖 ${n} 个，将优先本地安装，失败再联网"
      offline_check_fresh
    fi
  fi
  dim "安装过程会用 sudo 关闭 SteamOS 根分区只读保护并安装系统包，不会改动你的游戏与个人文件。"
  dim "其中 ydotool / wtype 会注册一个用户级守护进程（ydotoold），装完自动开机自启，无需你手动配置。"
}

ask_yes() {
  # 只有在 stdin 不是终端（例如被别处管道/程序调用）时才默认不装；
  # 直接粘贴多行命令时 stdin 仍是终端，正常询问
  #
  # 提示符用 [?] 而不是裸 ?：裸问号在部分字体里会落到 emoji 码位，
  # 渲染成黄色的 ❓，和整屏的字符画对不上
  if [ ! -t 0 ]; then
    if [ -r /dev/tty ]; then
      printf '%s[?]%s %s [y/N] ' "$C_WARN" "$C_END" "$1" > /dev/tty
      read -r ans < /dev/tty || return 1
    else
      return 1
    fi
  else
    printf '%s[?]%s %s [y/N] ' "$C_WARN" "$C_END" "$1"
    read -r ans || return 1
  fi
  case "$ans" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

# 读一行输入并回显提示，返回用户敲的内容。
#
# 和 ask_yes 不一样，这里**没有默认值**：管道执行（curl | bash）时 stdin 是脚本本身，
# 直接 read 会读到还没执行的脚本文本，所以固定从 /dev/tty 读。
# 拿不到终端（无人值守、ssh 没分配 tty）就返回非 0，由调用方决定怎么退。
ask_choice() {
  local prompt="$1" ans=""
  if [ -t 0 ]; then
    printf '%s[?]%s %s' "$C_WARN" "$C_END" "$prompt"
    read -r ans || return 1
  elif [ -r /dev/tty ]; then
    printf '%s[?]%s %s' "$C_WARN" "$C_END" "$prompt" > /dev/tty
    read -r ans < /dev/tty || return 1
  else
    return 1
  fi
  printf '%s' "$ans"
}

sudo_warmup() {
  need_cmd sudo || { warn "没有 sudo，跳过系统依赖安装"; return 1; }
  # 不做任何免密探测：sudo -v 自己会在需要时提示输入密码，预判只会多读一次策略
  info "安装系统依赖需要 root 权限，接下来会提示输入密码"
  sudo -v || { warn "sudo 授权失败，跳过系统依赖安装"; return 1; }
}

install_pacman_deps() {
  local todo=() reinstall=() installed=()
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
    if ! sudo steamos-readonly disable 2>/dev/null; then
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
    if log=$(sudo pacman -Sy --needed --noconfirm "${pkgs[@]}" 2>&1); then
      info "pacman 安装完成"
    else
      # 最常见失败原因：SteamOS 换镜像后 keyring 对不上，签名校验直接报错
      warn "pacman 安装失败，尝试修复 keyring 后重试"
      dim "$(printf '%s' "$log" | tail -5)"
      sudo pacman-key --init >/dev/null 2>&1 || true
      sudo pacman-key --populate archlinux holo >/dev/null 2>&1 || true
      if log=$(sudo pacman -Sy --needed --noconfirm "${pkgs[@]}" 2>&1); then
        info "修复 keyring 后安装成功"
      else
        warn "pacman 安装仍然失败："
        dim "$(printf '%s' "$log" | tail -20)"
        dim "手动排查：sudo pacman -Sy ${pkgs[*]}"
        ok=0
      fi
    fi
  fi
  if [ ${#reinstall[@]} -gt 0 ] && [ "$ok" = 1 ]; then
    if sudo pacman -S --noconfirm "${reinstall[@]}" >/dev/null 2>&1; then
      info "重装完成：${reinstall[*]}"
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
  fi
  record_deps_build
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
    sudo pacman -S --needed --noconfirm fakeroot >/dev/null 2>&1 || true
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
  sudo_warmup || return 0
  install_pacman_deps
  install_aur_deps
  setup_ydotoold
  # 装完跑一遍实测，把「记录在、命令没了」的情况直接暴露出来
  local broken; broken="$(deps_broken)"
  if [ -n "$broken" ]; then
    warn "以下控件装完仍不可用：$broken"
    dim "逐个看原因：bash install.sh check"
  fi
  system_notes
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
USER_DATA_RE='\.log$|(^|/)VERSION$|(^|/)__pycache__(/|$)|^deploy/deps/'

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
    [ -n "$old" ] && rm -f -- "$old"
  done < <(find "$INSTALL_DIR" -maxdepth 1 -name 'update-backup-*.tar.gz' -type f \
             -printf '%T@ %p\n' 2>/dev/null | sort -rn | tail -n +2 | cut -d' ' -f2-)
  [ "$removed" -gt 0 ] && dim "清理了 $removed 个新版本中已不存在的旧文件"
  return 0
}

sync_files() {
  [ -f "$HERE/install.sh" ] || die "当前目录不像 DeckMeido 源码目录（缺 install.sh）"
  if [ "$HERE" = "$(cd "$INSTALL_DIR" 2>/dev/null && pwd -P)" ]; then
    info "已在使用目录 $INSTALL_DIR"
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
  local items=(server client assets deploy tools plugin requirements.txt requirements-gui.txt README.md install.sh)
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
  chmod +x "$INSTALL_DIR/install.sh" 2>/dev/null || true
  chmod +x "$INSTALL_DIR/client/launch.sh" 2>/dev/null || true
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

# 「11 kB」「1.9 GB」「512」这类 pip 日志里的体积文本 → 字节数
parse_bytes() {
  local s="$1" num unit
  num=$(printf '%s' "$s" | grep -oE '[0-9.]+' | head -1)
  [ -n "$num" ] || { printf '0'; return 0; }
  unit=$(printf '%s' "$s" | grep -oE '[a-zA-Z]+' | head -1 | tr 'A-Z' 'a-z')
  case "$unit" in
    gb|g) printf '%d' "$(awk "BEGIN{print int($num*1073741824)}")" ;;
    mb|m) printf '%d' "$(awk "BEGIN{print int($num*1048576)}")" ;;
    kb|k) printf '%d' "$(awk "BEGIN{print int($num*1024)}")" ;;
    b|"") printf '%d' "$(awk "BEGIN{print int($num)}")" ;;
    *)    printf '0' ;;
  esac
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
    printf '%.0fMB' "$(awk "BEGIN{print $b/1048576}")"
  elif [ "$b" -ge 1024 ]; then
    printf '%.0fkB' "$(awk "BEGIN{print $b/1024}")"
  else
    printf '%dB' "$b"
  fi
}

# 建 venv + 装依赖。$1 = gui 时只装桌面客户端（PySide6），不动服务端依赖。
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

  if [ "$only_gui" != "gui" ]; then
    section "Python 依赖（服务端）"
    if ! pip_install_with_switch "$py" requirements.txt "$start"; then
      warn "服务端依赖没装全，可稍后重跑：bash $INSTALL_DIR/install.sh upgrade"
      dim "手动指定源：DECKMEIDO_PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple bash install.sh upgrade"
      return 1
    fi
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

# 带进度条与换源的 pip install。
#   $1 python，$2 requirements 文件名，$3 起始源下标（默认 0）
# 为什么要包一层：pip 下大包时很久不吐一行输出，以前又用了 -q，用户看到的
# 就是「卡在 pip 镜像源那一步」。这里让 pip 把输出写进日志、放后台跑，
# 前台每秒重画一次状态行：当前包 / 已下载字节 / 总字节 / 下载速度 / 剩余时间。
pip_install_with_switch() {
  local py="$1" req="$2" start="${3:-0}"
  local n=${#PIP_INDEXES[@]} i k ok=0 log="$INSTALL_DIR/pip-install.log"
  local ret=0 pid t0 cur got total rate prev_got
  # pip 下载的 wheel 先落缓存目录、下载完才装进 venv：下载阶段 venv 体积
  # 不动、缓存增长，只盯 venv 会把下载速度误判成 0。所以「已下载」取
  # venv + 缓存 两者体积之和，任何阶段都有增量可测。
  local cache_dir="${XDG_CACHE_HOME:-$HOME/.cache}/pip"
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
  local eta_text spd_text
  if [ "$rate" -gt 0 ] && [ "$total" -gt 0 ] && [ "$got" -lt "$total" ]; then
    eta_text="剩余 $(human_dur $(((total - got) / rate)))"
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
  if [ ! -t 1 ]; then
    # 非终端：当前包变了才打一行，重定向到日志时也能看出进度
    if [ -n "$cur" ] && [ "$cur" != "$_progress_last" ]; then
      printf '    [%s] %s\n' "$eta_text" "$cur"
      _progress_last="$cur"
    fi
    return 0
  fi
  spin_c="${_spin:$((got % ${#_spin})):1}"
  # 只显示 当前包 + 速度 + 剩余时间。不显示「已下载/总量」：pip 在
  # --progress-bar off 下不落盘当前包的部分进度，venv 体积和下载字节又是
  # 两个量纲（解压后膨胀），硬凑一对数字只会误导。
  printf '\r\033[K%s%s%s  %s  %s  %s' \
    "$C_C" "$spin_c" "$C_END" "$cur" "$spd_text" "$eta_text"
  _progress_on=1
}

# ---------- 数据目录 ----------
# 数据根权限：userdata 及子目录 0700，配置 0600（Linux 凭据不外泄）
ensure_data_layout() {
  ( umask 077; mkdir -p "$DATA_DIR" "$DATA_PERSONA_DIR" "$DATA_SESSIONS" "$DATA_UPLOADS" "$DATA_BOT" ) 2>/dev/null || true
  chmod 700 "$DATA_DIR" "$DATA_PERSONA_DIR" "$DATA_SESSIONS" "$DATA_UPLOADS" "$DATA_BOT" 2>/dev/null || true
}

# 首次安装生成 config.toml；已有则原样保留
prepare_config() {
  ensure_data_layout
  local cfg="$DATA_CONFIG"
  if [ -f "$cfg" ]; then
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
    info "生成配置 $cfg"
  fi
  # 不论新旧，配置里都可能有令牌，统一收紧到 0600
  chmod 600 "$cfg" 2>/dev/null || true
}

install_service() {
  # 不再安装 systemd 开机自启服务。
  #
  # 历史原因：以前 install.sh 装一个 deckmeido.service，客户端启动时又自己拉一份后端，
  # 两份抢同一个端口——谁先绑定谁对外服务，而它们各有一份内存历史，
  # 重启时被另一半抢到，用户看到的就是「聊天记录丢了」。
  # 现在后端只由客户端（桌面快捷方式）拉起，退出时一并收走。
  # 这里仅做清理：把老版本装过的单元停掉删掉，避免升级后还有残留一起抢端口。
  need_cmd systemctl || return 0
  systemctl --user disable --now "$UNIT" >/dev/null 2>&1 || true
  rm -f "$UNIT_DIR/$UNIT"
  systemctl --user daemon-reload || true
  # 兜底：万一还有裸跑的后端占着端口，停掉它，把入口让回客户端
  if need_cmd fuser; then
    fuser -k -TERM "$PORT/tcp" >/dev/null 2>&1 || true
    sleep 1
  fi
}

# 用 --purge 卸载前先停服务，否则进程会边跑边重建目录；从外部脚本升级时同样先停
stop_service() {
  need_cmd systemctl || return 0
  systemctl --user stop "$UNIT" >/dev/null 2>&1 || true
}

# 停掉后端进程本身（不是客户端）。
#
# 为什么单独停：后端是用 start_new_session 拉起的，客户端被杀不会带走它；
# 而且客户端启动时如果后端已经在跑（例如游戏模式下 Decky 插件先拉起了核心），
# 它退出时会刻意不碰那个进程。旧后端还占着端口时，新后端在 singleton.guard
# 里会直接退出，客户端只能报「后端核心启动失败」——表现就是升级后第一次
# 打不开、再走一次升级（此时旧进程已经自己退出）才正常。
# 只认 `python -m uvicorn server.main:app`，避免误杀 runuser 包装或安装脚本。
stop_backend() {
  local d pid pids="" i alive dir
  local -a args
  # 只收本安装目录下的后端；同机可能还有别的 deckmeido 源码目录在跑
  dir="$(cd "$INSTALL_DIR" 2>/dev/null && pwd -P)" || return 0
  for d in /proc/[0-9]*; do
    [ -r "$d/cmdline" ] || continue
    args=()
    mapfile -d '' -t args < "$d/cmdline" 2>/dev/null || continue
    [ "${#args[@]}" -ge 4 ] || continue
    case "${args[0]}" in *python*) ;; *) continue ;; esac
    [ "${args[1]}" = "-m" ] || continue
    [ "${args[2]}" = "uvicorn" ] || continue
    [ "${args[3]}" = "server.main:app" ] || continue
    [ "$(readlink "$d/cwd" 2>/dev/null)" = "$dir" ] || continue
    pid="${d#/proc/}"
    [ "$pid" = "$$" ] && continue
    pids="$pids $pid"
  done
  [ -n "$pids" ] || return 0
  dim "关闭旧后端（升级后由新客户端重新拉起）"
  # shellcheck disable=SC2086  # 这里就是要按空格拆成多个 pid
  kill -TERM $pids 2>/dev/null || true
  for i in $(seq 1 25); do
    alive=0
    for pid in $pids; do
      kill -0 "$pid" 2>/dev/null && alive=1
    done
    [ "$alive" = "0" ] && return 0
    sleep 0.2
  done
  # shellcheck disable=SC2086
  kill -KILL $pids 2>/dev/null || true
  sleep 0.2
}

# 把字符串转成可以安全塞进 sed 的 s|...| 替换段：
# 替换段里的 & 会展开成「整个匹配」，\ 和分隔符 | 会破坏语法
sed_repl() {
  printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'
}

# 手动安装 systemd 单元（默认不装：后端由客户端拉起，见 install_service 注释）
render_service() {
  sed "s|__INSTALL_DIR__|$(sed_repl "$INSTALL_DIR")|g; s|__PORT__|$(sed_repl "$PORT")|g" \
    "$INSTALL_DIR/deploy/$UNIT" > "$UNIT_DIR/$UNIT"
}

# ---------- Decky 插件 ----------
# 插件只在装了 Decky Loader 的机器上有意义。先问框架在不在，不在就整段跳过，
# 不构建、不报错 —— 大部分桌面模式用户根本没装 Decky。
#   deploy.sh --detect：0 = 框架在，2 = 没装（1 当没装处理，宁可跳过也别乱装）
PLUGIN_SKIP_REASON=""

decky_ready() {
  local d="$INSTALL_DIR/plugin/deploy.sh"
  [ -f "$d" ] || { PLUGIN_SKIP_REASON="安装包里没有 plugin/deploy.sh"; return 1; }
  DECKMEIDO_NONINTERACTIVE=1 bash "$d" --detect >/dev/null 2>&1
}

install_plugin() {
  if ! decky_ready; then
    dim "跳过 Decky 插件：${PLUGIN_SKIP_REASON:-没装 Decky Loader}"
    [ -n "$PLUGIN_SKIP_REASON" ] || dim "想用游戏模式面板，先装 Decky Loader，再跑：bash $INSTALL_DIR/plugin/deploy.sh"
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
      warn "插件自动更新失败，可手动重装：bash $INSTALL_DIR/plugin/deploy.sh"
    fi
    return 0
  fi
  # 检测到 Decky，但装不装让用户定：插件要写进 ~/homebrew/plugins，属于系统级改动
  if ask_yes "检测到 Decky Loader，现在就在游戏模式里装上 QAM 面板吗？"; then
    info "安装 QAM 插件（zip 解包，不使用软链接）"
    # 直接解 zip 到 ~/homebrew/plugins/DeckMeido：和 Decky 自己的安装器同一套产物，
    # 插件目录自洽，删掉安装目录也不影响它加载。
    # deploy.sh 保留给「装完还想重新解一次」的场景，这里不再走它。
    if plugin_install_zip; then
      local ver
      ver="$(plugin_installed_version)" && info "插件已安装 v${ver}"
      dim "插件已就绪，到 设置 → Decky Loader 点「重新载入」即可在 QAM 里看到"
    else
      warn "插件安装失败，可手动重装：bash $INSTALL_DIR/plugin/deploy.sh"
    fi
  else
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
  if [ ! -f "$target/dist/index.js" ] || [ ! -f "$target/plugin.json" ]; then
    dim "解包结果不完整（$target）"
    return 1
  fi
  return 0
}

# 已安装插件的版本号：解包后写进日志，升没升上去一眼能看出来
plugin_installed_version() {
  local p="${DECKY_PLUGINS_DIR:-$HOME/homebrew/plugins}/DeckMeido/package.json"
  [ -f "$p" ] || return 1
  sed -nE 's/.*"version"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p' "$p" | head -n1
}

# 重载 Decky：插件是启动时一次性 import 的，换了磁盘上的文件不重载还是旧代码
plugin_reload() {
  if ! command -v systemctl >/dev/null 2>&1; then
    dim "没有 systemctl，重启游戏模式后插件才会用上新版本"
    return 1
  fi
  local unit
  unit="$(systemctl list-unit-files 2>/dev/null |
    sed -nE 's/^((decky|plugin_loader)[^[:space:]]*\.service).*/\1/p' | head -n1)"
  if [ -n "$unit" ] && { sudo -n systemctl restart "$unit" 2>/dev/null ||
      systemctl restart "$unit" 2>/dev/null; }; then
    info "已重载 Decky（$unit），新插件版本已生效"
    return 0
  fi
  dim "请在 设置 → Decky Loader 点「重新载入」使新插件生效"
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
  dim "  mkdir -p ~/homebrew/plugins/DeckMeido"
  dim "  unzip -o '$dst' -d ~/homebrew/plugins/DeckMeido"
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
# DeckMeido 卸载入口（由 install.sh 自动生成）
#   bash uninstall.sh            按清单删安装器文件，保留人设、记忆与 userdata/
#   bash uninstall.sh --purge    额外删除安装器创建的配置备份目录（逐项）
#
# 2026-11-04 事故后规则：绝不整块删除安装目录；没有 .deckmeido-install 标记的
# 目录一律拒绝卸载；只删 .deckmeido-manifest 里列出的条目；userdata/ 永远保留。
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 注意：这里不再转发给 install.sh。install.sh 的守卫会把「脚本所在目录」
# 当成源码工作区拒绝，而本脚本就住在安装目录里，转发必然失败。下面全部
# 按清单逐项删除，HERE 只作为安装目录基准，绝不对 HERE 本身 rm -rf。
APP=deckmeido
PORT="${DECKMEIDO_PORT:-18765}"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/$APP"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT=deckmeido.service
DESKTOP_DIR="$HOME/.local/share/applications"
DESKTOP_FOLDER="$(command -v xdg-user-dir >/dev/null 2>&1 && xdg-user-dir DESKTOP 2>/dev/null || true)"
[ -n "$DESKTOP_FOLDER" ] && [ "$DESKTOP_FOLDER" != "$HOME" ] || DESKTOP_FOLDER="$HOME/Desktop"
[ "$(id -u)" = "0" ] && { echo "不要用 sudo 运行"; exit 1; }
INSTALL_DIR="$HERE"
MARKER="$HERE/.deckmeido-install"
MANIFEST="$HERE/.deckmeido-manifest"
CONFIG_MARKER="$CONFIG_DIR/.deckmeido-config"
CONFIG_MANIFEST="$CONFIG_DIR/.deckmeido-config-manifest"
# 用户数据根：默认 <安装目录>/userdata，可用 DECKMEIDO_DATA_DIR 覆盖；永不删除
DATA_DIR="${DECKMEIDO_DATA_DIR:-$HERE/userdata}"
case "$DATA_DIR" in "~"/*) DATA_DIR="$HOME/${DATA_DIR#\~/}" ;; esac
DATA_CONFIG="$DATA_DIR/config.toml"

# 2026-11-04 事故：卸载脚本误把源码工作区当安装目录删了。这里同样加删除守卫：
# 含 .git / 就是脚本所在目录 / 空 / / / 家目录 / 路径过浅 一律拒绝。
guard_target() {
  local t="${1:-}"
  [ "${DECKMEIDO_ALLOW_UNSAFE_UNINSTALL:-0}" = "1" ] && return 0
  [ -n "$t" ] && [ "$t" != "/" ] && [ "$t" != "$HOME" ] && [ "$t" != "$HOME/" ] || return 1
  t="${t%/}"
  [ "$(printf '%s' "$t" | awk -F/ '{print NF-1}')" -ge 3 ] || return 1
  [ ! -d "$t/.git" ] || return 1
  # 这里不能拒绝 t == HERE：本脚本就住在安装目录里。只拒绝 HERE 的上级。
  case "$HERE" in "$t"/*) return 1 ;; esac
  return 0
}

rel_ok() {
  local rel="${1:-}"
  [ -n "$rel" ] || return 1
  case "$rel" in
    /*|*..*) return 1 ;;
    userdata|userdata/*|.deckmeido-install|.deckmeido-manifest) return 1 ;;
  esac
  return 0
}
ext_ok() {
  local p="${1%/}" root
  case "$p" in
    "${UNIT_DIR%/}/$UNIT"|"${DESKTOP_DIR%/}/DeckMeido.desktop"|"${DESKTOP_FOLDER%/}/DeckMeido.desktop")
      return 0 ;;
  esac
  root="${DECKY_PLUGINS_DIR:-$HOME/homebrew/plugins}"
  [ "$p" = "${root%/}/DeckMeido" ] && return 0
  return 1
}
remove_path() {
  echo "[删除] $1"
  if [ -d "$1" ] && [ ! -L "$1" ]; then
    rm -rf -- "$1" 2>/dev/null || sudo -n rm -rf -- "$1" 2>/dev/null || true
  else
    rm -f -- "$1" 2>/dev/null || sudo -n rm -f -- "$1" 2>/dev/null || true
  fi
}
delete_manifest() {
  local line rel abs
  while IFS= read -r line || [ -n "$line" ]; do
    [ -n "$line" ] || continue
    case "$line" in
      dir:*) rel="${line#dir:}"; rel_ok "$rel" || continue; abs="$INSTALL_DIR/$rel" ;;
      ext:*) abs="${line#ext:}"; ext_ok "$abs" || continue ;;
      *) rel="$line"; rel_ok "$rel" || continue; abs="$INSTALL_DIR/$rel" ;;
    esac
    [ -e "$abs" ] || [ -L "$abs" ] || continue
    if [ "${line#ext:}" != "$line" ] && [ -d "$abs" ] && [ ! -L "$abs" ]; then
      remove_path "$abs"
    elif [ -d "$abs" ] && [ ! -L "$abs" ]; then
      remove_path "$abs"
    else
      echo "[删除] $abs"; rm -f -- "$abs" 2>/dev/null || true
    fi
  done < "$MANIFEST"
}

# 第一道：危险路径直接拒绝，保证零改动
guard_target "$INSTALL_DIR" || { echo "[删除守卫] 拒绝：$INSTALL_DIR（疑似源码工作区或危险路径）" >&2; exit 1; }
DEGRADED=0
if [ ! -f "$MARKER" ]; then
  if [ "${DECKMEIDO_ALLOW_UNSAFE_UNINSTALL:-0}" != "1" ]; then
    echo "[拒绝] 拒绝卸载：$INSTALL_DIR 不是本安装器创建的目录" >&2
    exit 1
  fi
  DEGRADED=1
  echo "[警告] 无安装器标记：只删清单文件，绝不删除目录本身"
elif [ ! -f "$MANIFEST" ]; then
  echo "[拒绝] 安装标记存在但缺少清单：$MANIFEST" >&2
  exit 1
fi

# 先取消正在跑的任务，再关进程
CFG="$DATA_CONFIG"
if command -v curl >/dev/null 2>&1 && [ -f "$CFG" ]; then
  TOK="$(grep -oP '^token\s*=\s*"\K[^"]*' "$CFG" 2>/dev/null | head -n1 || true)"
  curl -fsS -m 2 -X POST -H "X-Token: $TOK" "http://127.0.0.1:$PORT/api/cancel" >/dev/null 2>&1 || true
fi
pkill -f "$HERE/client/launch.sh" 2>/dev/null || true
pkill -f "$HERE/client/main.py" 2>/dev/null || true
if command -v systemctl >/dev/null 2>&1; then
  systemctl --user disable --now "$UNIT" >/dev/null 2>&1 || true
  systemctl --user daemon-reload || true
fi
pkill -f "$HERE/server/main.py" 2>/dev/null || true
pkill -f "$HERE/server/overlay.py" 2>/dev/null || true

# 普通卸载：把人设和记忆搬到 ~/.config/deckmeido/，并留下标记/清单
mkdir -p "$CONFIG_DIR" 2>/dev/null || true
for f in meidosoul.json memory.json; do
  [ -f "$HERE/server/$f" ] && cp -f "$HERE/server/$f" "$CONFIG_DIR/$f" 2>/dev/null
done
{
  printf 'format=1\napp=%s\ncreated_at=%s\ninstaller_path=%s\n' \
    "$APP" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$HERE/uninstall.sh"
} > "$CONFIG_MARKER" 2>/dev/null || true
: > "$CONFIG_MANIFEST" 2>/dev/null || true
for f in meidosoul.json memory.json; do
  [ -f "$CONFIG_DIR/$f" ] && printf '%s\n' "$f" >> "$CONFIG_MANIFEST"
done

if [ "${1:-}" = "--purge" ]; then
  if [ -f "$CONFIG_MARKER" ]; then
    if [ -f "$CONFIG_MANIFEST" ]; then
      while IFS= read -r line || [ -n "$line" ]; do
        [ -n "$line" ] || continue
        case "$line" in /*|*..*) continue ;; esac
        [ -e "$CONFIG_DIR/$line" ] || continue
        echo "[删除] $CONFIG_DIR/$line"; rm -f -- "$CONFIG_DIR/$line" 2>/dev/null || true
      done < "$CONFIG_MANIFEST"
      echo "[删除] $CONFIG_MANIFEST"; rm -f -- "$CONFIG_MANIFEST" 2>/dev/null || true
    fi
    echo "[删除] $CONFIG_MARKER"; rm -f -- "$CONFIG_MARKER" 2>/dev/null || true
    if [ -z "$(find "$CONFIG_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
      echo "[删除] $CONFIG_DIR"; rmdir "$CONFIG_DIR" 2>/dev/null || true
    else
      echo "保留目录：$CONFIG_DIR（含未识别文件）"
    fi
  else
    echo "[拒绝] 拒绝删除配置目录：$CONFIG_DIR 缺少 .deckmeido-config 标记"
  fi
  echo "拒绝删除工作区：--purge 不再删除工作区"
fi

if [ -f "$MANIFEST" ]; then
  delete_manifest
fi
if [ "$DEGRADED" = 0 ]; then
  echo "[删除] $MANIFEST"; rm -f -- "$MANIFEST" 2>/dev/null || true
  echo "[删除] $MARKER"; rm -f -- "$MARKER" 2>/dev/null || true
  if [ -z "$(find "$HERE" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
    echo "[删除] $HERE"; rmdir "$HERE" 2>/dev/null || true
  else
    echo "保留目录：$HERE（含未识别文件）"
  fi
else
  echo "保留目录：$HERE（降级路径只删清单文件，不 rmdir）"
fi
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
command -v kbuildsycoca6 >/dev/null 2>&1 && kbuildsycoca6 >/dev/null 2>&1 || true
KEPT="$DATA_DIR/meidobot"
if [ -d "$KEPT" ]; then
  echo "meidobot 归档保留在 $KEPT"
fi
if [ "${1:-}" = "--purge" ]; then
  echo "卸载完成：安装器文件与配置备份已按清单删除"
else
  echo "卸载完成：安装器文件已按清单删除，人设与记忆保留在 $CONFIG_DIR"
fi
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
  [ -n "$ver" ] || ver="0.0.0"
  printf '%s\n' "${ver#v}" > "$INSTALL_DIR/VERSION"
  dim "版本：$(cat "$INSTALL_DIR/VERSION")"
}

read_token() {
  local cfg="$DATA_CONFIG"
  [ -f "$cfg" ] || return 0
  grep -oP '^token\s*=\s*"\K[^"]*' "$cfg" 2>/dev/null | head -n1
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
  # 服务由客户端拉起，所以装完直接帮用户开一次客户端，服务就齐了。
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
  local ip token ve
  ip=$(lan_ip)
  [ -n "$ip" ] || ip=$(hostname -I 2>/dev/null | awk '{print $1}')
  [ -n "$ip" ] || ip="<deck-ip>"
  token=$(read_token)
  ve=$(cat "$INSTALL_DIR/VERSION" 2>/dev/null || echo '未知')
  card
  card_title "DeckMeido 已就绪  ·  v$ve"
  card_line ""
  card_line "本机控制台   http://127.0.0.1:$PORT/?token=$token"
  card_line "局域网访问   http://$ip:$PORT/?token=$token"
  card_line "访问令牌     默认 meido，可在客户端设置页修改"
  card_line "配置文件     $DATA_CONFIG"
  card_line "用户数据     $DATA_DIR（卸载保留）"
  card_line ""
  card_line "首次打开地址会弹出设置页，填 API 地址与 Key 即可对话"
  card_line "以后升级     bash $INSTALL_DIR/install.sh upgrade"
  card_line "卸载         bash $INSTALL_DIR/uninstall.sh"
  card_line "             （只删安装器文件；userdata/ 永远保留）"
  card_end
  echo
}

# ---------- 子命令 ----------
do_install() {
  local deps="" gui_only=0
  for a in "$@"; do
    case "$a" in
      --deps) deps="--deps" ;;
      --gui)  gui_only=1 ;;   # 只补装桌面客户端（PySide6），不动别的
    esac
  done
  if [ "$gui_only" = 1 ]; then
    check_user
    sync_files
    ensure_data_layout
    venv_install gui
    write_install_records
    return 0
  fi
  print_banner
  section "环境预检"
  check_prereq
  info "安装目录：$INSTALL_DIR"
  preflight
  section "同步程序文件"
  sync_files
  section "用户数据目录"
  ensure_data_layout
  section "Python 运行环境"
  # 依赖没装全不该让整个安装中断：代码已经同步好了，联网问题用户之后能自行重试
  venv_install || true
  section "写入配置"
  prepare_config
  # 之前卸载时备份的人设/记忆搬回来，让「她」还是原来的她
  restore_soul
  section "桌面入口与服务"
  stop_service
  install_service
  install_desktop
  install_uninstaller
  section "Decky 插件（游戏模式）"
  install_plugin
  # 所有安装动作结束后再落标记/清单，确保 venv、桌面项、插件都记录在内
  write_install_records
  if [ "$deps" = "--deps" ]; then
    deps_intro
    maybe_autorepair_after_image_update
    install_deps
  else
    deps_intro
    if need_cmd sudo && ask_yes "这些控件属于「代替你动手操作」的核心能力，推荐安装。现在就用 sudo 安装吗？"; then
      install_deps
    else
      dim "已跳过。之后想装：bash install.sh install --deps"
    fi
  fi
  section "启动"
  # 服务由客户端拉起：装完顺手开一次客户端，后台服务就一并起来了
  if launch_client && wait_ready; then
    print_done
  else
    warn "客户端未能自动启动，请双击桌面上的 DeckMeido 图标（服务会随之拉起）"
  fi
}

# 停掉客户端：客户端是后端的父进程，还会在 watchdog 里自动补拉后端。
# 升级过程中它要是还活着，会一边同步代码一边被重新拉起旧后端，
# 端口和内存里的历史都对不上。升级完成后由 launch_client 重新开。
stop_client() {
  pgrep -f "$INSTALL_DIR/client/main.py" >/dev/null 2>&1 || return 0
  dim "关闭客户端（升级后自动重开）"
  # 先收掉 launch.sh 的自动重启，否则它看到客户端被杀会立刻再拉起一个
  pkill -f "$INSTALL_DIR/client/launch.sh" 2>/dev/null || true
  pkill -f "$INSTALL_DIR/client/main.py" 2>/dev/null || true
  local i
  for i in $(seq 1 20); do
    pgrep -f "$INSTALL_DIR/client/main.py" >/dev/null 2>&1 || return 0
    sleep 0.2
  done
  pkill -9 -f "$INSTALL_DIR/client/main.py" 2>/dev/null || true
}

do_upgrade() {
  local deps="${1:-}" gui_only=0
  [ -d "$INSTALL_DIR" ] || die "$INSTALL_DIR 不存在，请先执行安装"
  if [ "$deps" = "--gui" ]; then gui_only=1; deps=""; fi
  print_banner
  # 升级只换代码和依赖，下面这些一律保留，用户不用担心升级丢数据
  dim "升级会保留：userdata/ 下的配置、人设、长期记忆、会话与上传文件，"
  dim "以及工作区、Decky 插件（不重复部署）。"
  echo
  section "环境预检"
  check_prereq
  # 顺序要紧：先停客户端（否则它的 watchdog 会把旧后端拉回来），再停服务，
  # 最后把可能已经「脱管」的后端进程也收掉，保证换文件时没有旧进程占着端口
  stop_client
  stop_service
  stop_backend
  section "同步程序文件"
  sync_files
  section "用户数据目录"
  ensure_data_layout
  section "Python 运行环境"
  if [ "$gui_only" = 1 ]; then
    venv_install gui || true
    write_install_records
    launch_client || true
    return 0
  fi
  venv_install || true
  section "桌面入口与服务"
  install_service
  install_desktop
  install_uninstaller
  section "Decky 插件（游戏模式）"
  install_plugin
  write_install_records
  if [ "$deps" = "--deps" ]; then
    maybe_autorepair_after_image_update
    install_deps
  else
    system_notes
  fi
  section "启动"
  # 服务由客户端拉起：升级后关掉旧客户端再开新的，新代码才会生效
  launch_client && wait_ready || true
  if need_cmd curl && wait_ready; then
    print_done
  else
    warn "升级完成。若界面还是旧版本，请关掉托盘里的 DeckMeido 再从桌面图标重开"
  fi
}

# ---------- 从镜像站安装 ----------
# GitHub 不通、也没有现成源码目录时用这个：拉一份最新的发布包，解到临时目录，
# 再交给正常的 install 流程（等价于自己下载解压后跑 bash install.sh）。
mirror_latest_tag() {
  curl -fsSL -m 15 "$MIRROR/latest.json" 2>/dev/null |
    grep -oE '"tag"[[:space:]]*:[[:space:]]*"[^"]+"' | head -n1 |
    sed -E 's/.*"([^"]+)"$/\1/' | sed 's/^v//'
}

# 管道执行时「装 / 升 / 卸」怎么定：
#   没装   → 什么都不问，直接装（一行命令的默认用途）
#   装了   → 必须手动选「升级」还是「卸载」，不设默认值
# 之所以不给默认值：回车就升级的话，想卸载的人一旦手滑就直接覆盖安装，
# 卸载入口等于藏起来了。这里强制敲 1 或 2。
mirror_pick_action() {
  print_banner
  if [ ! -f "$INSTALL_DIR/VERSION" ]; then
    info "未检测到已安装的 $APP，直接开始全新安装（$INSTALL_DIR）"
    printf 'install'
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
  choice=$(ask_choice "请输入序号（1 升级 / 2 卸载 / 0 退出）：") || {
    echo >&2
    warn "没有可用的终端，无法确认要升级还是卸载"
    dim "带子命令直接指定：bash install.sh upgrade / bash install.sh uninstall"
    return 3
  }
  case "$choice" in
    1) printf 'upgrade' ;;
    2) printf 'uninstall' ;;
    0|""|q|Q) printf 'quit' ;;
    *) return 4 ;;
  esac
}

do_from_mirror() {
  need_cmd curl || die "需要 curl：先装 curl（sudo pacman -S curl）"
  local action rc=0
  action="$(mirror_pick_action)" || rc=$?
  if [ "$rc" != 0 ]; then
    [ "$rc" = 3 ] && exit 0
    die "无效选择，已退出（输入 1 升级 / 2 卸载 / 0 退出）"
  fi
  case "$action" in
    quit) info "已取消"; exit 0 ;;
    uninstall)
      # 卸载不需要安装包：直接用这张脚本自带的 do_uninstall，
      # 免得为了删代码先下载几十 MB
      do_uninstall
      exit 0 ;;
  esac

  print_banner
  info "镜像站：$MIRROR"
  local tag; tag="$(mirror_latest_tag || true)"
  [ -n "$tag" ] || die "读不到镜像站的版本信息（$MIRROR/latest.json），请检查网络或稍后再试"
  info "最新版本：v$tag"
  local url="$MIRROR/deckmeido-$tag.tar.gz"
  local tmp; tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  info "下载 $url"
  curl -fL --retry 2 -m 600 -o "$tmp/pkg.tar.gz" "$url" ||
    die "下载失败：$url"
  # 有校验和就核一遍，防止半包 / 被篡改
  if curl -fsSL -m 15 -o "$tmp/pkg.sha256" "$url.sha256" 2>/dev/null; then
    ( cd "$tmp" && sed "s|deckmeido-$tag.tar.gz|pkg.tar.gz|" pkg.sha256 | sha256sum -c - >/dev/null 2>&1 ) ||
      die "安装包校验不通过，可能没下完整，请重试"
    dim "sha256 校验通过"
  fi
  tar -xzf "$tmp/pkg.tar.gz" -C "$tmp" || die "解压失败"
  local src; src="$(find "$tmp" -maxdepth 2 -name install.sh -printf '%h\n' | head -n1)"
  [ -n "$src" ] || die "安装包结构不对：里面没有 install.sh"
  info "开始执行：$action（来源：$src）"
  # 让子进程沿用这次用的镜像地址，写进配置的就是它。
  # 子命令明确传下去：上面已经问过「升级还是卸载」，不能让子脚本再猜一次
  case "$action" in
    upgrade) DECKMEIDO_MIRROR="$MIRROR" bash "$src/install.sh" upgrade ;;
    *)       DECKMEIDO_MIRROR="$MIRROR" bash "$src/install.sh" install ;;
  esac
}

# 正在跑的任务要先取消：卸载时把后端直接杀掉，正在执行的命令会变成孤儿进程
cancel_running_turn() {
  local token
  token=$(read_token)
  need_cmd curl || return 0
  curl -fsS -m 2 -X POST -H "X-Token: $token" \
    "http://127.0.0.1:$PORT/api/cancel" >/dev/null 2>&1 || true
}

# 工作区真实路径：优先读配置里的 workspace，删不到才退回默认目录。
# 以前直接删 $HOME/deckmeido_workspace，而配置里指到别处的工作区根本删不到。
workspace_dir() {
  local cfg="$DATA_CONFIG" ws=""
  if [ -f "$cfg" ]; then
    ws=$(grep -oP '^\s*workspace\s*=\s*"\K[^"]*' "$cfg" 2>/dev/null | head -n1)
  fi
  [ -n "$ws" ] || ws="${DECKMEIDO_WORKSPACE:-$HOME/${APP}_workspace}"
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

do_uninstall() {
  local purge=0 degraded=0 has_marker=0
  local qq_keep="" ws="" kept_dir=0
  [ "${1:-}" = "--purge" ] && purge=1
  check_user
  # 第一道：危险路径/源码工作区一律在动手前拒绝，保证零改动
  if ! safe_rm_target "$INSTALL_DIR"; then
    warn "拒绝卸载：$INSTALL_DIR 疑似源码工作区或危险路径"
    die "卸载中止（未删除任何文件）"
  fi
  # 第二道：目录必须带安装器标记。没有标记就不是我们装的，绝不删。
  [ -f "$INSTALL_MARKER" ] && has_marker=1
  if [ "$has_marker" = 0 ]; then
    if [ "${DECKMEIDO_ALLOW_UNSAFE_UNINSTALL:-0}" != "1" ]; then
      warn "拒绝卸载：$INSTALL_DIR 不是本安装器创建的目录"
      exit 1
    fi
    degraded=1
    warn "无安装器标记：进入降级路径，只删清单文件，绝不删除目录本身"
  elif [ ! -f "$INSTALL_MANIFEST" ]; then
    warn "安装标记存在但缺少清单：$INSTALL_MANIFEST"
    die "卸载中止（未删除任何文件）"
  fi
  if ui_fits; then
    print_banner
    section "$([ "$purge" = 1 ] && echo '卸载（彻底清除）' || echo '卸载')"
  else
    info "卸载 DeckMeido"
  fi
  if [ "$purge" = 1 ]; then
    dim "彻底清除：按清单删除安装器创建的文件，连同配置备份与人设记忆（不可恢复）。"
  else
    dim "本次卸载只删安装器创建的文件；人设、记忆会先备份到 $CONFIG_DIR，下次安装自动恢复。"
  fi
  echo
  # 先把人设和记忆备份走，再动目录，避免删代码时连带丢数据
  backup_soul
  # 先取消正在执行的任务，再停客户端和后端，避免留下跑了一半的命令
  cancel_running_turn
  # 客户端是后端的父进程，先让它退出，后端会跟着收走
  if pgrep -f "$INSTALL_DIR/client/main.py" >/dev/null 2>&1; then
    dim "关闭客户端"
    pkill -f "$INSTALL_DIR/client/launch.sh" 2>/dev/null || true
    pkill -f "$INSTALL_DIR/client/main.py" 2>/dev/null || true
    sleep 1
    pkill -9 -f "$INSTALL_DIR/client/main.py" 2>/dev/null || true
  fi
  stop_service
  if need_cmd systemctl; then
    dim "移除用户服务"
    systemctl --user disable --now "$UNIT" >/dev/null 2>&1 || true
    systemctl --user daemon-reload || true
    # 服务单元本身由清单独条目删除，这里不再直接 rm
  fi
  # 兜底：后端被独立拉起时（旧版本装过 systemd 单元）也要收掉，否则卸载后
  # 它还在 18765 上跑，下次安装会撞端口
  pkill -f "$INSTALL_DIR/server/main.py" 2>/dev/null || true
  pkill -f "$INSTALL_DIR/server/overlay.py" 2>/dev/null || true
  sleep 1

  # 桌面项与 Decky 插件都通过清单里的 ext: 条目删除，刷新缓存放在删完之后
  need_cmd update-desktop-database && update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true

  if [ "$purge" = 1 ]; then
    ws="$(workspace_dir)"
    if [ "$degraded" = 1 ]; then
      # 降级路径只允许按清单删文件，连配置备份也不碰
      warn "降级路径不删除配置备份：$CONFIG_DIR"
    else
      warn "删除安装器创建的配置备份目录（逐项，不可恢复）"
      purge_config_dir
    fi
    # 工作区、家目录、/ 一律拒绝：--purge 不再删除工作区
    case "$ws" in
      ""|"/"|"$HOME") warn "拒绝删除工作区：$ws" ;;
      *) warn "拒绝删除工作区：$ws（含用户文件，--purge 不再删除）" ;;
    esac
  fi

  qq_keep="$DATA_BOT"
  if [ -d "$INSTALL_DIR" ]; then
    if [ -f "$INSTALL_MANIFEST" ]; then
      delete_install_manifest "$INSTALL_MANIFEST"
      cleanup_empty_manifest_dirs "$INSTALL_MANIFEST"
      dim "按清单删除了 $LAST_REMOVED 项（跳过异常条目 $LAST_SKIPPED 项）"
    fi
    if [ "$degraded" = 0 ]; then
      # 标记与清单本身最后处理；降级路径保留它们作为「这里被谁动过」的证据
      echo "[删除] $INSTALL_MANIFEST"
      rm -f -- "$INSTALL_MANIFEST" 2>/dev/null || true
      echo "[删除] $INSTALL_MARKER"
      rm -f -- "$INSTALL_MARKER" 2>/dev/null || true
      # 只有目录真的空了才 rmdir；只剩 userdata/ 也算非空
      if [ -z "$(find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
        echo "[删除] $INSTALL_DIR"
        rmdir "$INSTALL_DIR" 2>/dev/null || kept_dir=1
      else
        kept_dir=1
      fi
      [ "$kept_dir" = 1 ] && info "保留目录：$INSTALL_DIR（含未识别文件）"
    else
      info "保留目录：$INSTALL_DIR（降级路径只删清单文件，不 rmdir）"
    fi
  fi
  if [ -d "$qq_keep" ]; then
    info "meidobot 归档保留在 $qq_keep"
  fi
  need_cmd kbuildsycoca6 && kbuildsycoca6 >/dev/null 2>&1 || true
  need_cmd kbuildsycoca5 && kbuildsycoca5 >/dev/null 2>&1 || true

  # 结果汇总（和安装界面同一套卡片风格）
  if ui_fits; then
    card
    card_title "卸载完成"
    if [ "$purge" = 1 ]; then
      card_line "已按清单删除安装器创建的文件"
      card_line "人设与记忆备份已清除"
    else
      card_line "安装器创建的文件已按清单删除"
      card_line "人设与记忆已保留在 $CONFIG_DIR"
      card_line "下次安装会自动恢复"
    fi
    card_line "用户数据保留在 $DATA_DIR（含 meidobot 归档）"
    card_line "清单外文件未动"
    card_end
    echo
  else
    info "卸载完成"
    if [ "$purge" = 1 ]; then
      dim "已按清单删除（含人设与记忆备份）"
    else
      dim "人设与记忆保留在 $CONFIG_DIR，下次安装自动恢复"
    fi
    dim "用户数据保留在 $DATA_DIR（含 meidobot 归档），清单外文件未动"
  fi
  # 最后几行是条件输出的 && 列表，没有 QQ 归档时返回 1，不能让它影响退出码
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
    echo "  ${C_B}1${C_END}  升级到最新版  ${C_DIM}（默认，回车即可）${C_END}"
    echo "  ${C_B}2${C_END}  重新安装      ${C_DIM}（覆盖重装，保留配置）${C_END}"
    echo "  ${C_B}3${C_END}  卸载          ${C_DIM}（删代码，保留人设与记忆）${C_END}"
    echo "  ${C_B}0${C_END}  退出"
    echo
    local choice=""
    if [ -t 0 ]; then
      printf '%s[?]%s 请输入序号 [1] ' "$C_WARN" "$C_END"
      read -r choice || choice="0"
    else
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
  do_from_mirror "${1:-}"
  exit $?
fi

case "${1:-}" in
  "")            smart_default ;;
  menu)          show_menu ;;
  install)       do_install "${2:-}" ;;
  upgrade|update) do_upgrade "${2:-}" ;;
  from-mirror)  do_from_mirror "${2:-}" ;;
  speedtest|mirror-test)
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
  uninstall)    do_uninstall "${2:-}" ;;
  service)      check_user; mkdir -p "$UNIT_DIR"; render_service
                info "已写入 $UNIT_DIR/$UNIT"
                dim "启用并启动：systemctl --user daemon-reload && systemctl --user enable --now $UNIT" ;;
  -h|--help|help)
    grep '^#' "$0" | sed 's/^# \{0,1\}//' | head -n 36 ;;
  *) die "未知命令：$1（可用：无参数=智能装/升 / menu / install / upgrade / from-mirror / speedtest / check / uninstall / service）" ;;
esac
