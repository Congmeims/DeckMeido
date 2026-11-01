#!/usr/bin/env bash
# DeckMeido 的 Decky 插件部署脚本。
#
# 插件装到 ~/homebrew/plugins/DeckMeido（复制，不是软链接），改完代码重跑一次即可。
# 前端（dist/index.js）在发版时就编译好了，随发布包一起分发，用户不需要 npm。
#
# 用法：
#   bash plugin/deploy.sh               部署（复制本目录的 main.py / plugin.json / dist）
#   bash plugin/deploy.sh --detect      只查装没装 Decky 框架（install.sh 用）
#   bash plugin/deploy.sh --remove      卸载插件
#   bash plugin/deploy.sh --check       只报状态，不改任何东西（install.sh 用）
#
# 退出码：0 = 已就绪 / 成功；1 = 失败；2 = 环境不具备（没装 Decky 等），
# 这种情况 install.sh 不该当错误处理，只提示一句。
#
# 被 install.sh 调用时（DECKMEIDO_NONINTERACTIVE=1）不打印颜色和多余提示。
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGINS_ROOT="${DECKY_PLUGINS_DIR:-$HOME/homebrew/plugins}"
TARGET="$PLUGINS_ROOT/DeckMeido"
NONINTERACTIVE="${DECKMEIDO_NONINTERACTIVE:-0}"

C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_DIM=$'\033[2m'; C_END=$'\033[0m'
if [ ! -t 1 ] || [ "$NONINTERACTIVE" = "1" ]; then C_OK=""; C_WARN=""; C_ERR=""; C_DIM=""; C_END=""; fi
info() { printf '%s==>%s %s\n' "$C_OK" "$C_END" "$*"; }
warn() { printf '%s[!]%s %s\n' "$C_WARN" "$C_END" "$*"; }
die()  { printf '%s[x]%s %s\n' "$C_ERR" "$C_END" "$*" >&2; exit 1; }
dim()  { printf '%s    %s%s\n' "$C_DIM" "$*" "$C_END"; }

case "${1:-}" in
  --remove) MODE=remove ;;
  --check)  MODE=check ;;
  --detect) MODE=detect ;;
  *)        MODE=deploy ;;
esac

# ---- 检测 Decky 框架 ----
# 判断「装没装 Decky」。最可靠的信号是插件根目录 ~/homebrew/plugins 是否存在：
# 这个目录由 Decky 自己创建（属 root:root），没装 Decky 的机器上不会有。
# 目录空着不算没装 —— 可能刚重装还没装其它插件。再补两条兜底信号：
# 前端 bundle 目录存在、或 systemd 里有 decky 单元。
decky_status() {
  local root="$PLUGINS_ROOT" hit=1 why=""
  # 1. 插件根目录本身：Decky 装好后它一定存在（属 root:root），
  #    只有「连目录都没有」才是真的没装。目录空着也可能是刚更新完还没装插件
  if [ -d "$root" ]; then
    hit=0; why="$root"
    # 顺手记下里面有没有别的插件，只是给提示用
    local others
    others=$(find "$root" -mindepth 1 -maxdepth 1 ! -name 'DeckMeido' 2>/dev/null | head -n1)
    [ -n "$others" ] && why="$root（含其他插件）"
  fi
  # 2. Decky 的前端 bundle / 配置目录
  if [ "$hit" != 0 ]; then
    local d
    for d in "$HOME/homebrew/services" "$HOME/.local/share/decky" "$HOME/.config/decky"; do
      if [ -e "$d" ]; then hit=0; why="$d"; break; fi
    done
  fi
  # 3. systemd 里有 decky 相关单元
  if [ "$hit" != 0 ] && command -v systemctl >/dev/null 2>&1; then
    if systemctl list-unit-files 2>/dev/null | grep -qi '^decky'; then
      hit=0; why="systemd 里有 decky 单元"
    fi
  fi
  [ "$hit" = 0 ] && { DECKY_WHY="$why"; return 0; }
  return 1
}

# 只看状态：给 install.sh 判断要不要部署插件用。
# 退出码：0 = 框架在（不论插件装没装），2 = 没装 Decky，1 = 框架在但插件没就绪
if [ "$MODE" = "detect" ]; then
  if ! decky_status; then
    dim "没装 Decky Loader，跳过插件部署"
    exit 2
  fi
  dim "检测到 Decky Loader（$DECKY_WHY）"
  exit 0
fi

# ---- 只看状态 ----
if [ "$MODE" = "check" ]; then
  decky_status || { dim "没装 Decky Loader（$PLUGINS_ROOT 不存在），跳过插件"; exit 2; }
  [ -f "$TARGET/index.js" ] || { warn "Decky 插件未部署（$TARGET/index.js 不存在）"; exit 1; }
  [ -f "$TARGET/main.py" ] || { warn "Decky 插件缺 main.py"; exit 1; }
  [ -f "$TARGET/plugin.json" ] || { warn "Decky 插件缺 plugin.json"; exit 1; }
  exit 0
fi

# ---- 卸载 ----
if [ "$MODE" = "remove" ]; then
  if [ -d "$TARGET" ]; then
    # 目录属 root 时普通用户删不动，先收回属主再删
    if ! rm -rf "$TARGET" 2>/dev/null; then
      if sudo chown -R "$(id -un):$(id -gn)" "$TARGET"; then
        rm -rf "$TARGET"
      else
        die "删不掉 $TARGET（sudo chown 也失败）"
      fi
    fi
    info "已移除 $TARGET"
    dim "重启 Decky（设置 → Decky Loader → 重新载入）后插件从 QAM 消失"
  else
    info "插件本来就没装"
  fi
  exit 0
fi

# ---- 前置检查 ----
# 先确认框架装没装，再谈部署；没装就直接退出 2，install.sh 不会当错误处理
if ! decky_status; then
  warn "没装 Decky Loader，跳过插件部署"
  dim "装好 Decky Loader（https://github.com/SteamDeckHomebrew/decky-loader）后再跑一次：bash plugin/deploy.sh"
  exit 2
fi
dim "检测到 Decky Loader（$DECKY_WHY）"

if [ "$(id -u)" = "0" ]; then
  die "不要用 sudo/root 执行：插件要装在当前用户家目录下"
fi

# ---- 目录权限 ----
# Decky 自己建的 ~/homebrew/plugins 属 root:root（早期版本是 deck），
# 普通用户既删不掉也建不了插件目录，直接 rm -rf 会报「权限不够」并留下半个目录，
# Decky 下次加载卡在那半个目录上。所以先把这一层交还给当前用户：
#   - 目录属主不是自己 → 尝试 sudo chown（有免密 sudo 时无提示，没有就输一次密码）
#   - 插件目录里如果有 root 留下的文件，也一并收回来，否则 rm 删不动
# 全程只动 ~/homebrew/plugins 这一层和本插件自己的目录，不碰别的插件。
ME="$(id -un)"
if [ -d "$TARGET" ] || [ "$(stat -c %U "$PLUGINS_ROOT" 2>/dev/null)" != "$ME" ]; then
  sudo -n true 2>/dev/null ||
    warn "需要一次 sudo：$PLUGINS_ROOT 属 root，普通用户删不掉插件目录"
  sudo chown "$ME:$(id -gn)" "$PLUGINS_ROOT" 2>/dev/null ||
    warn "改不动 $PLUGINS_ROOT 的属主，下面可能失败"
  if [ -d "$TARGET" ]; then
    sudo chown -R "$ME:$(id -gn)" "$TARGET" 2>/dev/null || true
  fi
fi

# ---- 部署（复制，不软链接）----
# 前端 dist/index.js 在发版时就编译好了，这里只需要把插件要用的文件复制过去。
# 复制而非软链接：软链接指回安装目录，一旦安装目录被删/移动插件就失效，
# 复制则插件目录是自洽的，Decky 重载也不依赖安装目录还在。
[ -f "$PLUGIN_DIR/dist/index.js" ] || die "缺少 dist/index.js（发布包里应自带，不需要 npm 编译）"
[ -f "$PLUGIN_DIR/main.py" ] || die "缺少 main.py"
[ -f "$PLUGIN_DIR/plugin.json" ] || die "缺少 plugin.json"

# 目录可能已存在（重装/升级），清掉旧的再复制，避免残留旧文件。
# 删不掉时退回「原地覆盖」：Decky 按文件名加载，多出来的旧文件无害，
# 比整次安装失败要好
if [ -d "$TARGET" ] && ! rm -rf "$TARGET" 2>/dev/null; then
  warn "删不掉旧目录 $TARGET，改为原地覆盖"
fi
mkdir -p "$TARGET"
cp "$PLUGIN_DIR/main.py" "$TARGET/main.py"
cp "$PLUGIN_DIR/plugin.json" "$TARGET/plugin.json"
rm -rf "$TARGET/dist" 2>/dev/null || true
cp -r "$PLUGIN_DIR/dist" "$TARGET/dist"
info "已部署插件到 $TARGET"

# ---- Python 依赖：插件直接用系统 python，不建 venv ----
# Decky 自带 python 环境，main.py 只用到标准库（tomllib/base64/json），
# 不需要额外装包。将来要加依赖必须写进 plugin/requirements.txt，
# 由用户手动 pip 装到 Decky 的 python 里。
if [ -f "$PLUGIN_DIR/requirements.txt" ]; then
  warn "plugin/requirements.txt 存在，但本脚本不会自动 pip 安装"
  dim "Decky 的 python 环境比较敏感，装包请手动确认后再执行"
fi

info "部署完成"
dim "到 设置 → Decky Loader 里点「重新载入」，或重启游戏模式"
dim "之后 QAM 里会出现 DeckMeido 面板"
