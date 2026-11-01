import os
#!/usr/bin/env bash
# 把 DeckMeido 官网 + 镜像站部署到服务器。
#
# 用法（在仓库根目录执行）：
#   bash tools/deploy_mirror.sh user@host:/var/www/meido.meagames.cn
#
# 参数：目标 ssh 地址 + 站点 web 根目录（宝塔里该站点的网站目录）。
# 目录结构：
#   <webroot>/index.html                    官网首页（单页）
#   <webroot>/deckmeido/install.sh          镜像引导脚本（curl|bash 用）
#   <webroot>/deckmeido/latest.json         版本指针
#   <webroot>/deckmeido/SHA256SUMS          校验和汇总
#   <webroot>/deckmeido/deckmeido-*.tar.gz  安装包
#   <webroot>/deckmeido/deckmeido-plugin-*.zip  Decky 插件
#
# 依赖本地 dist/ 已经由 release.py 生成好（含 latest.json、插件 zip）。
# 若 dist 缺产物，会提示先跑打包。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${1:-}"
WEBROOT="${2:-/var/www/meido.meagames.cn}"

if [ -z "$DEST" ]; then
  echo "用法：bash tools/deploy_mirror.sh user@host[:port] [webroot]"
  echo "示例：bash tools/deploy_mirror.sh root@<服务器IP> /www/wwwroot/meido.meagames.cn"
  exit 1
fi

MIRROR_DIR="$ROOT/dist"

# ---- 前置检查：dist 产物是否齐全 ----
need=()
[ -f "$MIRROR_DIR/latest.json" ] || need+=("latest.json")
[ -f "$ROOT/install.sh" ] || need+=("install.sh")
ls "$MIRROR_DIR"/deckmeido-*.tar.gz >/dev/null 2>&1 || need+=("deckmeido-*.tar.gz")
if [ ${#need[@]} -gt 0 ]; then
  echo "[x] 缺少镜像站产物：${need[*]}"
  echo "    先跑：python tools/release.py 0.4.3"
  exit 1
fi

echo "==> 部署到 $DEST:$WEBROOT"

# ---- 确保远端目录存在 ----
ssh "$DEST" "mkdir -p '$WEBROOT' '$WEBROOT/deckmeido'"

# ---- 官网首页 ----
rsync -av "$ROOT/website/index.html" "$DEST:$WEBROOT/"

# ---- 镜像站：固定文件 ----
rsync -av \
  "$ROOT/install.sh" \
  "$MIRROR_DIR/latest.json" \
  "$MIRROR_DIR/SHA256SUMS" \
  "$DEST:$WEBROOT/deckmeido/"

# ---- 镜像站：安装包 + 插件（通配上传全部版本，也可只传最新） ----
rsync -av "$MIRROR_DIR"/deckmeido-*.tar.gz "$MIRROR_DIR"/deckmeido-*.tar.gz.sha256 \
  "$DEST:$WEBROOT/deckmeido/" 2>/dev/null || true
rsync -av "$MIRROR_DIR"/deckmeido-plugin-*.zip "$MIRROR_DIR"/deckmeido-plugin-*.zip.sha256 \
  "$DEST:$WEBROOT/deckmeido/" 2>/dev/null || true

echo
echo "==> 完成。校验远端："
ssh "$DEST" "ls -la '$WEBROOT' '$WEBROOT/deckmeido'"
