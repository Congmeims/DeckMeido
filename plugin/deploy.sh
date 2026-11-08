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
  # decky-loader 的 unit 实际叫 plugin_loader.service（不是 decky*.service），
  # 只匹配 '^decky' 会漏掉这个最常见的情况：~/homebrew/plugins 还没建出来、
  # 服务又不在跑时，整机明明装了 Decky 却被判成「没装」，插件静默跳过。
  if [ "$hit" != 0 ] && command -v systemctl >/dev/null 2>&1; then
    if systemctl list-unit-files 2>/dev/null | grep -qiE '^(decky|plugin_loader)'; then
      hit=0; why="systemd 里有 decky / plugin_loader 单元"
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
  # 前端 bundle 落在子目录：部署段 cp -r "$PLUGIN_DIR/dist" "$TARGET/dist"，
  # 所以入口是 dist/index.js，不是插件根目录下的 index.js。
  # 以前这里查的是根目录，部署明明成功也恒报「未部署」。
  [ -f "$TARGET/dist/index.js" ] || { warn "Decky 插件未部署（$TARGET/dist/index.js 不存在）"; exit 1; }
  [ -f "$TARGET/main.py" ] || { warn "Decky 插件缺 main.py"; exit 1; }
  # 装成了软链接（旧版部署方式）不算就绪：安装目录一挪或删掉，插件就整个失效。
  # 现在只认「zip 解出来的实体目录」，这里顺手把残留的软链接报出来
  if [ -L "$TARGET" ]; then
    warn "Decky 插件是软链接（指向 $(readlink "$TARGET")），请在 Decky 里卸载后重装 zip"
    exit 1
  fi
  # package.json 必须保留 "type": "module"。Decky 读这个字段决定前端产物按
  # ESM 还是 CJS 处理；删掉它，产物末尾那行 `export { index as default }`
  # 在 eval 里就变成 SyntaxError: Unexpected token 'export'（以前踩过这个坑）
  if ! grep -q '"type"[[:space:]]*:[[:space:]]*"module"' "$TARGET/package.json" 2>/dev/null; then
    warn "Decky 插件 package.json 缺 \"type\": \"module\"，面板会报 Unexpected token 'export'"
    exit 1
  fi
  # 反过来的情况：ESMODULE_V1 的机器要 import 这份，缺了 default export 会拿不到组件
  if ! grep -qE 'export[[:space:]]*\{[^}]*[[:space:]]as[[:space:]]default[[:space:]]*\}' "$TARGET/dist/index.js" 2>/dev/null; then
    warn "Decky 插件的 dist/index.js 没有 default export，Decky 会报「插件没有导出默认组件」"
    exit 1
  fi
  [ -f "$TARGET/plugin.json" ] || { warn "Decky 插件缺 plugin.json"; exit 1; }
  [ -f "$TARGET/package.json" ] || { warn "Decky 插件缺 package.json（Decky 不会加载它）"; exit 1; }
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
# Decky 自己建的 ~/homebrew/plugins 属 root:root，普通用户既删不掉也建不了插件目录，
# 直接 rm -rf 会报「权限不够」并留下半个目录，Decky 下次加载卡在那半个目录上。
# 注意：不要 chown 插件根目录本身 —— Decky 及机器上其它插件都按 root:root 工作，
# 把根目录改成 deck 会让整个 ~/homebrew/plugins 的属主和 Decky 的预期不一致。
# 只借一次 sudo 动本插件自己的目录，根目录保持原样。
ME="$(id -un)"
if [ -e "$TARGET" ] || [ "$(stat -c %U "$PLUGINS_ROOT" 2>/dev/null)" != "$ME" ]; then
  sudo -n true 2>/dev/null ||
    warn "需要一次 sudo：$PLUGINS_ROOT 属 root，普通用户删不掉插件目录"
  # 旧版可能是软链接装进来的：先删链接本身，别顺着链接把安装目录删了
  if [ -L "$TARGET" ]; then
    sudo rm -f "$TARGET" 2>/dev/null || rm -f "$TARGET" 2>/dev/null || true
  fi
  sudo mkdir -p "$PLUGINS_ROOT" 2>/dev/null || warn "建不动 $PLUGINS_ROOT，下面可能失败"
fi

# ---- 部署（解 zip，不软链接）----
# 唯一认可的安装形态是 Decky 安装器解出来的实体目录。这里不再「复制 + 改 package.json」，
# 而是现场打一份和 release 完全一致的 zip 再解开：两条路径（Decky 装 / 脚本装）
# 出来的文件逐字节相同，不会出现「脚本装的能用、Decky 装的打不开」这种分裂。
[ -f "$PLUGIN_DIR/dist/index.js" ] || die "缺少 dist/index.js（发布包里应自带，不需要 npm 编译）"
[ -f "$PLUGIN_DIR/main.py" ] || die "缺少 main.py"
[ -f "$PLUGIN_DIR/plugin.json" ] || die "缺少 plugin.json"
[ -f "$PLUGIN_DIR/package.json" ] || die "缺少 package.json（Decky 靠它取 name/version，缺了插件不会出现在 QAM）"

TMP_ZIP="$(mktemp -t deckmeido-plugin-XXXXXX.zip)"
trap 'rm -f "$TMP_ZIP"' EXIT
# 打包规则与 tools/release.py 的 build_plugin_zip 保持一致：顶层 DeckMeido/，
# package.json 原样保留 "type": "module"（Decky 靠它决定按 ESM 加载），
# main 指向 dist/index.js
"${PYTHON:-python3}" - "$PLUGIN_DIR" "$TMP_ZIP" <<'PY'
import json, sys, zipfile, pathlib
plugin, out = pathlib.Path(sys.argv[1]), sys.argv[2]
top = "DeckMeido/"
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    z.write(plugin / "main.py", top + "main.py")
    z.write(plugin / "plugin.json", top + "plugin.json")
    pkg = json.loads((plugin / "package.json").read_text(encoding="utf-8"))
    pkg["main"] = "dist/index.js"
    z.writestr(top + "package.json", json.dumps(pkg, ensure_ascii=False, indent=2) + "\n")
    for name in ("README.md", "LICENSE"):
        if (plugin / name).is_file():
            z.write(plugin / name, top + name)
    for sub in ("py_modules", "dist"):
        base = plugin / sub
        if base.is_dir():
            for q in sorted(base.rglob("*")):
                if q.is_file() and "__pycache__" not in q.parts:
                    z.write(q, top + q.relative_to(plugin).as_posix())
PY

# 先删后解：zip 里没有的旧文件（改过名的模块）留着会让 Decky 加载到过期代码
rm -rf "$TARGET" 2>/dev/null || sudo rm -rf "$TARGET" 2>/dev/null || true
sudo unzip -oq "$TMP_ZIP" -d "$PLUGINS_ROOT"
[ -f "$TARGET/dist/index.js" ] && [ -f "$TARGET/plugin.json" ] || die "解包结果不完整（$TARGET）"
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
