#!/usr/bin/env bash
# DeckMeido 一键部署：同步后端/客户端 → 重启后端 → 接口自检 → 插件 dist 有变化就重载 Decky
#
# 用法（在仓库根目录，Git Bash / WSL / Linux 均可）：
#   DECK_SUDO_PASS=<deck 的 sudo 密码> tools/deploy_deck.sh
#
# 可选环境变量：
#   DECK_HOST        默认 deck@192.168.124.14
#   DECK_DIR         默认 /home/deck/DeckMeido
#   DECK_PLUGIN_DIR  默认 /home/deck/homebrew/plugins/DeckMeido
#   DECK_SUDO_PASS   Deck 上的 sudo 密码（必须，脚本不内置密码）
#   DECK_PORT        默认 18765
#   DECK_BACKUP_KEEP 每类备份保留份数，默认 3
#   DECK_BACKUP_MAX_MB 每类备份总量上限（MB），默认 200，超出删最旧
set -euo pipefail

HOST="${DECK_HOST:-deck@192.168.124.14}"
REMOTE="${DECK_DIR:-/home/deck/DeckMeido}"
PLUGIN_REMOTE="${DECK_PLUGIN_DIR:-/home/deck/homebrew/plugins/DeckMeido}"
PORT="${DECK_PORT:-18765}"
SUDO_PASS="${DECK_SUDO_PASS:-}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# 备份固定落在数据根的 userdata/backups/deploy/，不再往 /home/deck 根目录丢文件
BACKUP_DIR="$REMOTE/userdata/backups/deploy"
BACKUP_KEEP="${DECK_BACKUP_KEEP:-3}"
BACKUP_MAX_MB="${DECK_BACKUP_MAX_MB:-200}"

log() { printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf '\n[失败] %s\n' "$*" >&2; exit 1; }

# ---------- 前置检查 ----------
[ -f "$ROOT/server/main.py" ] || die "请在仓库根目录运行：找不到 server/main.py"
[ -f "$ROOT/plugin/dist/index.js" ] || die "找不到 plugin/dist/index.js，先执行 cd plugin && npm run build"

log "检查 ssh 与 sudo 权限"
ssh -o BatchMode=yes -o ConnectTimeout=6 "$HOST" true || die "ssh 连不上 $HOST"
if [ -z "$SUDO_PASS" ]; then
  ssh "$HOST" 'sudo -n true' 2>/dev/null || die "需要 sudo 密码：请设置 DECK_SUDO_PASS=..."
fi

sudo_remote() {
  # 只用于固定命令，参数由调用方拼好；密码通过 stdin 传给 sudo -S
  ssh "$HOST" "echo '$SUDO_PASS' | sudo -S -p '' $1"
}

# ---------- 部署前备份 ----------
# 每次部署前把当前 server/ client/ 打包一份到 userdata/backups/deploy/，
# 备份完成后校验归档可读；再按「每类最近 N 份 + 单类总量上限」清理最旧的。
log "备份现有 server/ client/ 到 $BACKUP_DIR"
TS=$(date +%Y%m%d-%H%M%S)
BACKUP_TAR="$BACKUP_DIR/DeckMeido-server-backup-$TS.tar.gz"
ssh "$HOST" "mkdir -p $BACKUP_DIR" || die "创建备份目录失败"
BACKUP_SRC="server client"
if ssh "$HOST" "cd $REMOTE && [ -f VERSION ] && [ -f COMMIT ]"; then
  BACKUP_SRC="server client VERSION COMMIT"
fi
ssh "$HOST" "cd $REMOTE && tar -czf $BACKUP_TAR $BACKUP_SRC" || die "备份失败"
ssh "$HOST" "tar -tzf $BACKUP_TAR > /dev/null" || die "备份校验失败：归档不可读（$BACKUP_TAR）"
BACKUP_SIZE=$(ssh "$HOST" "stat -c %s $BACKUP_TAR" || echo 0)
printf '  %s（%s 字节，归档可读）\n' "$(basename "$BACKUP_TAR")" "$BACKUP_SIZE"

# 保留策略：按 DeckMeido-<类>-backup-<时间戳>.tar.gz 分组，每组只留最近 N 份，
# 总量超过上限就继续删最旧；永远保留每组最新的一份。
ssh "$HOST" "python3 - '$BACKUP_DIR' '$BACKUP_KEEP' '$BACKUP_MAX_MB'" <<'PY' || printf '  保留策略执行失败（不影响本次部署）\n'
import glob, os, re, sys
d, keep, cap = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]) * 1024 * 1024
pat = re.compile(r"^(.*-backup-)\d{8}-\d{6}\.tar\.gz$")
groups = {}
for p in glob.glob(os.path.join(d, "*.tar.gz")):
    m = pat.match(os.path.basename(p))
    if m:
        groups.setdefault(m.group(1), []).append(p)
for prefix, files in sorted(groups.items()):
    files.sort(key=lambda p: (os.path.getmtime(p), p))
    total = sum(os.path.getsize(p) for p in files)
    count = len(files)
    while count > 1 and (count > keep or total > cap):
        p = files.pop(0)
        size = os.path.getsize(p)
        os.remove(p)
        total -= size
        count -= 1
        print("  prune:", os.path.basename(p))
PY

# ---------- 1. 同步后端与客户端 ----------
log "1/5 同步 server/ 与 client/ 到 $REMOTE"
tar --exclude='__pycache__' --exclude='*.pyc' -czf - -C "$ROOT" server client \
  | ssh "$HOST" "tar -xzf - -C $REMOTE" || die "同步后端失败"

# 构建标记：安装目录没有 .git（git archive 部署），把短提交号写进 COMMIT，
# 后端据此把 /api/version 的 version_display 显示成 0.7.5-xxxxxxx
COMMIT=$(git -C "$ROOT" rev-parse --short=7 HEAD 2>/dev/null || true)
if [ -n "$COMMIT" ]; then
  printf '%s\n' "$COMMIT" | ssh "$HOST" "cat > $REMOTE/COMMIT" || die "写入 COMMIT 失败"
  printf '  COMMIT -> %s\n' "$COMMIT"
else
  printf '  跳过 COMMIT（当前目录不是 git 仓库）\n'
fi

# ---------- 2. 重启后端 ----------
log "2/5 重启 deckmeido-backend"
ssh "$HOST" "systemctl --user restart deckmeido-backend" || die "重启后端失败"

# ---------- 3. 最小自检 ----------
log "3/5 等待后端就绪并自检 /api/status"
CODE=""
for _ in $(seq 1 20); do
  CODE=$(ssh "$HOST" "curl -s -o /dev/null -m 2 -w '%{http_code}' http://127.0.0.1:$PORT/api/status" || true)
  [ "$CODE" = "200" ] && break
  sleep 1
done
if [ "$CODE" != "200" ]; then
  printf '\n最近 40 行后端日志：\n'
  ssh "$HOST" "journalctl --user -u deckmeido-backend --since '2 min ago' --no-pager | tail -40" || true
  die "后端没有在 20 秒内返回 200（当前 $CODE）"
fi
printf '  /api/status -> 200\n'

# ---------- 4. 插件 dist 有变化才同步并重载 ----------
LOCAL_SHA=$(sha256sum "$ROOT/plugin/dist/index.js" | cut -d' ' -f1)
REMOTE_SHA=$(ssh "$HOST" "sha256sum $PLUGIN_REMOTE/dist/index.js 2>/dev/null | cut -d' ' -f1" || true)

if [ "$LOCAL_SHA" = "$REMOTE_SHA" ] && [ "${FORCE_PLUGIN:-0}" != "1" ]; then
  log "4/5 插件 dist 未变化（$LOCAL_SHA），跳过重载"
else
  log "4/5 同步插件 dist（本地 $LOCAL_SHA / 远端 ${REMOTE_SHA:-无}）"
  scp -q "$ROOT/plugin/dist/index.js" "$HOST:/tmp/deckmeido-index.js" || die "上传插件失败"
  sudo_remote "cp /tmp/deckmeido-index.js $PLUGIN_REMOTE/dist/index.js" \
    || die "写入插件目录失败"
  sudo_remote "chown root:root $PLUGIN_REMOTE/dist/index.js" \
    || die "修正插件文件属主失败"

  log "5/5 重载 plugin_loader（QAM 会短暂关闭，游戏不受影响）"
  sudo_remote "systemctl restart plugin_loader" || die "重启 plugin_loader 失败"

  ACTIVE=""
  for _ in $(seq 1 20); do
    ACTIVE=$(ssh "$HOST" "systemctl is-active plugin_loader" || true)
    [ "$ACTIVE" = "active" ] && break
    sleep 1
  done
  [ "$ACTIVE" = "active" ] || die "plugin_loader 没有回到 active（当前 $ACTIVE）"
  printf '  plugin_loader -> active\n'

  # Decky 日志里确认插件确实被加载；有明确错误就打印出来
  sleep 3
  LOADLOG=$(ssh "$HOST" "journalctl -u plugin_loader --since '90 seconds ago' --no-pager | tail -80" || true)
  if printf '%s\n' "$LOADLOG" | grep -qiE 'error loading|failed to load|traceback'; then
    printf '%s\n' "$LOADLOG"
    die "Decky 日志里出现加载错误（见上）"
  fi
  printf '%s\n' "$LOADLOG" | grep -i 'DeckMeido' | tail -5 || printf '  （日志里没有 DeckMeido 字样，但 loader 已 active）\n'
  OUT_SHA=$(ssh "$HOST" "sha256sum $PLUGIN_REMOTE/dist/index.js | cut -d' ' -f1")
  [ "$OUT_SHA" = "$LOCAL_SHA" ] || die "部署后 dist 哈希不一致（$OUT_SHA）"
  printf '  插件 dist sha256 -> %s\n' "$OUT_SHA"
fi

log "完成：后端已同步并重启，插件按需重载"
