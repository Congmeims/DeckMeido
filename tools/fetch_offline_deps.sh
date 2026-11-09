#!/usr/bin/env bash
# 汇总离线依赖包到 deploy/deps/，供 release.py 打进发布包。
#
#   bash tools/fetch_offline_deps.sh
#
# 必须在 SteamOS 上、并且已联网时执行：它要用 pacman 把包下载到本地
# （--downloadonly，不真正安装），同时把依赖树一并拉下来，否则
# install.sh 在断网机器上 pacman -U 会因为缺依赖失败。
#
# 注意：SteamOS 换镜像（A/B 分区）后 holo 仓库的版本会变，旧包可能
# 比仓库还旧。每次发版前重跑一遍这个脚本，包才跟得上。
#
# 自带清单只放「小、依赖浅」的包：
#   ydotool  16.6KB（仅依赖 glibc）
#   wtype    10.0KB（依赖 libxkbcommon / wayland，SteamOS 自带）
#   fakeroot 78.7KB（依赖 glibc / sed / bash / util-linux，SteamOS 自带）
# 合计约 105KB，对 1.8MB 的发布包几乎无感。
# espeak-ng 故意不自带：压缩后 7.2MB，还要 libsonic / pcaudiolib 两个
# SteamOS 没有的依赖，放上去等于让每次发版都多背 7MB；它走本机缓存 /
# 镜像站。kdotool 是 AUR 包，pacman 下载不到，也只能放镜像站。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/deploy/deps"
MIRROR="https://meido.meagames.cn/deckmeido"

# 与 install.sh 顶部「随发布包自带的离线包集合」注释保持一致
PKGS=(ydotool wtype fakeroot)

mkdir -p "$OUT"

echo "==> 刷新仓库索引"
sudo pacman -Sy

echo "==> 下载依赖树（不安装）"
# --downloadonly 只下载；--needed 避免已装过的版本被当成「不需要下载」
sudo pacman -S --needed --noconfirm --downloadonly \
  --cachedir "$OUT" "${PKGS[@]}"

# pacman 的 cachedir 会在里面再建一层 pkg/，摊平到 deploy/deps 根目录，
# install.sh 只按 deploy/deps/*.pkg.tar.zst 一层匹配
if [ -d "$OUT/pkg" ]; then
  find "$OUT/pkg" -maxdepth 1 -name '*.pkg.tar.zst' -exec mv -f {} "$OUT/" \;
  rmdir "$OUT/pkg" 2>/dev/null || true
fi

# 校验：核心包都在、能解开、包名对得上
echo
bad=0
for p in "${PKGS[@]}"; do
  hit=$(find "$OUT" -maxdepth 1 -name "$p-*.pkg.tar.zst" | sort -V | tail -n1)
  if [ -z "$hit" ]; then
    echo "[!] 缺少 $p"
    bad=1
    continue
  fi
  # pacman -Qp 会真的解开包读元数据，顺带验证包没被截断
  name=$(pacman -Qp "$hit" 2>/dev/null | awk 'NR==1{print $1}')
  [ "$name" = "$p" ] || { echo "[!] $hit 打不开或包名是 $name，不是 $p"; bad=1; }
  echo "    $p → $(basename "$hit")"
done
[ "$bad" = 0 ] || { echo "[!] 有包缺失或损坏，检查上面的输出"; exit 1; }

# 哈希清单：install.sh 在 Deck 上逐个校验它，release.py 打包前也会复核。
# 只覆盖本目录的 .pkg.tar.zst，与 dist/SHA256SUMS（覆盖发布 tar.gz）无关。
echo
echo "==> 生成 SHA256SUMS"
(
  cd "$OUT"
  # 文本模式是默认值（两空格分隔）；BSD/部分环境会给出 `*` 前缀，install.sh 两种都认
  sha256sum *.pkg.tar.zst | sort -k2 > SHA256SUMS
)
echo "    $(wc -l < "$OUT/SHA256SUMS") 个包已登记"

echo
echo "共 $(find "$OUT" -maxdepth 1 -name '*.pkg.tar.zst' | wc -l) 个包，合计 $(du -sh "$OUT" | cut -f1)，位置 $OUT"
echo "下一步：python tools/release.py <版本号>"
echo "第三层兜底（镜像站）还需要把这些文件放到 ${MIRROR}/deps/："
echo "  *.pkg.tar.zst 与 SHA256SUMS（缺 espeak-ng / kdotool 的机器就靠这一层）"
