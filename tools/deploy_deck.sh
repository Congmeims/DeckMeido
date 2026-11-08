#!/usr/bin/env bash
# DeckMeido 一键部署：同步后端/客户端/install.sh → 重启后端 → 接口自检 → 插件 dist 有变化就重载 Decky
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
[ -f "$ROOT/install.sh" ] || die "找不到 install.sh，先确认仓库完整"
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
# 每次部署前把 server/ client/ install.sh 和用户数据目录一起打包到 userdata/backups/deploy/。
# 代码与数据分成两类归档（DeckMeido-server-backup-* / DeckMeido-data-backup-*），
# 数据再大也不会把 server 备份撑大；两类各自按「最近 N 份 + 单类总量上限」清理（见下方保留策略）。
log "备份现有 server/ client/ install.sh 与数据目录到 $BACKUP_DIR"
TS=$(date +%Y%m%d-%H%M%S)
BACKUP_TAR="$BACKUP_DIR/DeckMeido-server-backup-$TS.tar.gz"
ssh "$HOST" "mkdir -p $BACKUP_DIR" || die "创建备份目录失败"
BACKUP_SRC="server client"
if ssh "$HOST" "cd $REMOTE && [ -f VERSION ] && [ -f COMMIT ]"; then
  BACKUP_SRC="$BACKUP_SRC VERSION COMMIT"
fi
if ssh "$HOST" "cd $REMOTE && [ -f install.sh ]"; then
  BACKUP_SRC="$BACKUP_SRC install.sh"
fi
ssh "$HOST" "cd $REMOTE && tar -czf $BACKUP_TAR $BACKUP_SRC" || die "备份失败"
ssh "$HOST" "tar -tzf $BACKUP_TAR > /dev/null" || die "备份校验失败：归档不可读（$BACKUP_TAR）"
BACKUP_SIZE=$(ssh "$HOST" "stat -c %s $BACKUP_TAR" || echo 0)
printf '  %s（%s 字节，归档可读）\n' "$(basename "$BACKUP_TAR")" "$BACKUP_SIZE"

# 数据目录：会话、预设、配置。此前只备份代码，会话被误删时没有任何恢复点。
# 路径不存在就跳过（全新安装可能还没有 presets/）。
DATA_TAR="$BACKUP_DIR/DeckMeido-data-backup-$TS.tar.gz"
DATA_SRC=$(ssh "$HOST" "cd $REMOTE && for p in userdata/sessions userdata/presets userdata/config.toml; do [ -e \"\$p\" ] && printf '%s ' \"\$p\"; done" || true)
if [ -n "$DATA_SRC" ]; then
  ssh "$HOST" "cd $REMOTE && tar -czf $DATA_TAR $DATA_SRC" || die "数据目录备份失败"
  ssh "$HOST" "tar -tzf $DATA_TAR > /dev/null" || die "数据目录备份校验失败：归档不可读（$DATA_TAR）"
  DATA_SIZE=$(ssh "$HOST" "stat -c %s $DATA_TAR" || echo 0)
  printf '  %s（%s 字节，归档可读）\n' "$(basename "$DATA_TAR")" "$DATA_SIZE"
else
  printf '  跳过数据目录备份（sessions/presets/config.toml 均不存在）\n'
fi

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

# ---------- 1. 同步后端、客户端与安装脚本 ----------
log "1/5 同步 server/、client/ 与 install.sh 到 $REMOTE"
tar --exclude='__pycache__' --exclude='*.pyc' -czf - -C "$ROOT" server client install.sh \
  | ssh "$HOST" "tar -xzf - -C $REMOTE" || die "同步后端失败"

# 安装脚本也要校验：Deck 安装目录里的 install.sh 是旧版时，
# 下次命令行安装/修复还会带过期文案和检查项；和插件 dist 一样用 sha256 对账
LOCAL_INSTALL_SHA=$(sha256sum "$ROOT/install.sh" | cut -d' ' -f1)
REMOTE_INSTALL_SHA=$(ssh "$HOST" "sha256sum $REMOTE/install.sh 2>/dev/null | cut -d' ' -f1" || true)
[ "$REMOTE_INSTALL_SHA" = "$LOCAL_INSTALL_SHA" ] \
  || die "install.sh 哈希不一致（本地 $LOCAL_INSTALL_SHA / 远端 ${REMOTE_INSTALL_SHA:-无}）"
printf '  install.sh sha256 -> %s\n' "$REMOTE_INSTALL_SHA"

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
# 线上客户端实际由 Decky 的 app-DeckMeido@<哈希>.service 实例单元启动，
# 旧的安装方式才是 deckmeido-backend；按实际存在的单元自动选，别重启错目标。
UNIT=$(ssh "$HOST" "systemctl --user list-units --all --no-legend --plain 'app-DeckMeido@*.service' 2>/dev/null | sed 's/^[^a-zA-Z]*//' | awk '{print \$1}' | grep -E '^app-DeckMeido@.+\.service$' | head -n 1" || true)
if [ -z "$UNIT" ] && ssh "$HOST" "systemctl --user cat deckmeido-backend >/dev/null 2>&1"; then
  UNIT=deckmeido-backend
fi
[ -n "$UNIT" ] || die "找不到 DeckMeido 的 systemd 用户单元（app-DeckMeido@*.service 或 deckmeido-backend）"
log "2/5 重启 $UNIT"
ssh "$HOST" "systemctl --user restart $UNIT" || die "重启 $UNIT 失败"

# ---------- 3. 最小自检 ----------
# 接口 200 / 文件哈希一致 ≠ 页面可用：曾出现 index.html 顶层 JS 报错，
# 页面能画出来但 8187 行之后的全部初始化都没执行，表现成“客户端连不上、网页掉线”。
# 冒烟约定（人工或自动化都必须遵守）：
#   1) 必须在浏览器里加载页面并检查控制台：零 ReferenceError / Uncaught 才算过；
#   2) 自动化冒烟禁止操作会话列表的删除类控件，也不要在列表里“清理空会话”——
#      一次误触就会真删 userdata/sessions（删除接口只要设备鉴权通过就立即生效，
#      没有二次确认，也没有回收站）；
#   3) 需要发消息只用专用会话；先确认设备配对状态（未配对时
#      POST /api/sessions/create 返回 401），否则只能证明 UI 提交，不能证明端到端落盘。
log "3/5 等待后端就绪并自检 /api/status"
CODE=""
for _ in $(seq 1 20); do
  CODE=$(ssh "$HOST" "curl -s -o /dev/null -m 2 -w '%{http_code}' http://127.0.0.1:$PORT/api/status" || true)
  [ "$CODE" = "200" ] && break
  sleep 1
done
if [ "$CODE" != "200" ]; then
  printf '\n最近 40 行后端日志：\n'
  ssh "$HOST" "journalctl --user -u $UNIT --since '2 min ago' --no-pager | tail -40" || true
  die "后端没有在 20 秒内返回 200（当前 $CODE）"
fi
printf '  /api/status -> 200\n'

# ---------- 4. 插件 dist / main.py 有变化才同步并重载 ----------
LOCAL_SHA=$(sha256sum "$ROOT/plugin/dist/index.js" | cut -d' ' -f1)
REMOTE_SHA=$(ssh "$HOST" "sha256sum $PLUGIN_REMOTE/dist/index.js 2>/dev/null | cut -d' ' -f1" || true)
# 插件后端 main.py 也必须跟着走：只更新 dist 会出现「界面有按钮、插件后端没这个方法」
# （save_preset 就是这么失败的），Decky 只会报 'Plugin' object has no attribute
LOCAL_MAIN_SHA=$(sha256sum "$ROOT/plugin/main.py" | cut -d' ' -f1)
REMOTE_MAIN_SHA=$(ssh "$HOST" "sha256sum $PLUGIN_REMOTE/main.py 2>/dev/null | cut -d' ' -f1" || true)

if [ "$LOCAL_SHA" = "$REMOTE_SHA" ] && [ "$LOCAL_MAIN_SHA" = "$REMOTE_MAIN_SHA" ] \
   && [ "${FORCE_PLUGIN:-0}" != "1" ]; then
  log "4/5 插件 dist / main.py 未变化（$LOCAL_SHA），跳过重载"
else
  log "4/5 同步插件 dist 与 main.py（dist 本地 $LOCAL_SHA / 远端 ${REMOTE_SHA:-无}）"
  scp -q "$ROOT/plugin/dist/index.js" "$HOST:/tmp/deckmeido-index.js" || die "上传插件失败"
  sudo_remote "cp /tmp/deckmeido-index.js $PLUGIN_REMOTE/dist/index.js" \
    || die "写入插件目录失败"
  sudo_remote "chown root:root $PLUGIN_REMOTE/dist/index.js" \
    || die "修正插件文件属主失败"
  scp -q "$ROOT/plugin/main.py" "$HOST:/tmp/deckmeido-plugin-main.py" || die "上传插件后端失败"
  sudo_remote "cp /tmp/deckmeido-plugin-main.py $PLUGIN_REMOTE/main.py" \
    || die "写入插件后端失败"
  sudo_remote "chown root:root $PLUGIN_REMOTE/main.py" \
    || die "修正插件后端属主失败"

  # 绝不 systemctl restart：Decky 的 unit 是 KillMode=process + Restart=always，
  # restart 只杀主进程，旧子进程继续占着 127.0.0.1:1337，新实例绑不上就退出又被拉起，
  # 直接进无限重启循环（2026-11-08 实测 271 次）。这里走 install.sh 同一套：
  # 体检 → stop → 等残留退出 → 有残留就放弃 → 无残留才 start → 回读 NRestarts/1337。
  log "5/5 安全重载 Decky（stop → 等残留退出 → start，QAM 会短暂关闭，游戏不受影响）"
  DECKY_UNIT="$(ssh "$HOST" "systemctl list-unit-files 2>/dev/null | sed -nE 's/^((decky|plugin_loader)[^[:space:]]*\.service).*/\1/p' | head -n1" || true)"
  [ -n "$DECKY_UNIT" ] || die "没检测到 Decky 服务（decky / plugin_loader 都没有）"

  # ---- 体检：已经卡在循环里就不动手 ----
  STATE="$(ssh "$HOST" "systemctl is-active $DECKY_UNIT 2>/dev/null" || true)"
  BIND_HITS="$(ssh "$HOST" "journalctl -u $DECKY_UNIT --since '2 minutes ago' --no-pager 2>/dev/null | grep -c '1337.*address already in use' || true" || true)"
  case "$BIND_HITS" in ''|*[!0-9]*) BIND_HITS=0 ;; esac
  PROBE="$(ssh "$HOST" "curl -s -o /dev/null -m 3 -w '%{http_code}' http://127.0.0.1:1337/ 2>/dev/null" || true)"
  case "$PROBE" in ''|000|0000) PORT_OK=0 ;; *) PORT_OK=1 ;; esac
  if [ "$STATE" = "active" ] && { [ "$BIND_HITS" -gt 0 ] || [ "$PORT_OK" = 0 ]; }; then
    die "Decky 当前异常（state=$STATE，近 2 分钟 1337 bind 失败 $BIND_HITS 次，1337 探测=${PROBE:-无应答}）：已放弃重载，请先重启 Deck 再部署"
  fi
  printf '  Decky 体检：state=%s，bind 失败=%s，1337=%s\n' "${STATE:-unknown}" "$BIND_HITS" "${PROBE:-无应答}"

  # ---- stop → 等残留退出（有残留就放弃，绝不 start）----
  if [ "$STATE" = "active" ]; then
    sudo_remote "systemctl stop $DECKY_UNIT" || die "停止 Decky 失败（$DECKY_UNIT）"
    TMO="$(ssh "$HOST" "systemctl show -p TimeoutStopSec --value $DECKY_UNIT 2>/dev/null" || true)"
    TMO="${TMO%s}"
    case "$TMO" in ''|*[!0-9]*) TMO=15 ;; esac
    [ "$TMO" -gt 20 ] && TMO=20
    [ "$TMO" -lt 3 ] && TMO=3
    MPID="1"; RESIDUAL=""
    for _ in $(seq 1 $((TMO * 2))); do
      MPID="$(ssh "$HOST" "systemctl show -p MainPID --value $DECKY_UNIT 2>/dev/null" || true)"
      RESIDUAL="$(ssh "$HOST" "pgrep -f '[P]luginLoader|[p]lugin_loader' 2>/dev/null | tr '\n' ' '" || true)"
      if { [ -z "$MPID" ] || [ "$MPID" = "0" ]; } && [ -z "$RESIDUAL" ]; then break; fi
      sleep 0.5
    done
    if ! { { [ -z "$MPID" ] || [ "$MPID" = "0" ]; } && [ -z "$RESIDUAL" ]; }; then
      die "停止 Decky 后仍有残留进程（MainPID=$MPID，PID: ${RESIDUAL:-无}）占着 1337；已放弃重载，请重启 Deck 后再部署"
    fi
  else
    # 服务本来就没在跑：不用 stop，但必须先确认没有残留子进程占着 1337，
    # 否则 start 一上来就撞端口，直接进重启循环
    RESIDUAL="$(ssh "$HOST" "pgrep -f '[P]luginLoader|[p]lugin_loader' 2>/dev/null | tr '\n' ' '" || true)"
    [ -z "$RESIDUAL" ] || die "Decky 服务已停止，但仍有残留进程（$RESIDUAL）占着 1337；已放弃重载，请重启 Deck 后再部署"
  fi

  # ---- start → 回读 ----
  sudo_remote "systemctl start $DECKY_UNIT" || die "启动 Decky 失败（$DECKY_UNIT）"
  ACTIVE=""
  for _ in $(seq 1 20); do
    ACTIVE=$(ssh "$HOST" "systemctl is-active $DECKY_UNIT" || true)
    [ "$ACTIVE" = "active" ] && break
    sleep 0.5
  done
  [ "$ACTIVE" = "active" ] || die "Decky 启动后不是 active（当前 ${ACTIVE:-unknown}）"

  NR_BEFORE="$(ssh "$HOST" "systemctl show -p NRestarts --value $DECKY_UNIT 2>/dev/null" || true)"
  sleep 15
  NR_AFTER="$(ssh "$HOST" "systemctl show -p NRestarts --value $DECKY_UNIT 2>/dev/null" || true)"
  BIND_HITS="$(ssh "$HOST" "journalctl -u $DECKY_UNIT --since '30 seconds ago' --no-pager 2>/dev/null | grep -c '1337.*address already in use' || true" || true)"
  case "$BIND_HITS" in ''|*[!0-9]*) BIND_HITS=0 ;; esac
  LOOP_WHY=""
  case "${NR_BEFORE}${NR_AFTER}" in
    *[!0-9]*|'') ;;   # 取不到数字就不比较，交给下面的 1337 探测兜底
    *) [ "$NR_AFTER" -gt "$NR_BEFORE" ] && LOOP_WHY="NRestarts ${NR_BEFORE} → ${NR_AFTER}" ;;
  esac
  [ "$BIND_HITS" -gt 0 ] && LOOP_WHY="${LOOP_WHY:+$LOOP_WHY，}近 30 秒 bind 1337 失败 ${BIND_HITS} 次"
  [ -z "$LOOP_WHY" ] || die "Decky 卡在重启循环（$LOOP_WHY）：请重启 Deck 后再部署"
  PROBE="$(ssh "$HOST" "curl -s -o /dev/null -m 3 -w '%{http_code}' http://127.0.0.1:1337/ 2>/dev/null" || true)"
  case "$PROBE" in
    ''|000|0000) die "Decky 已启动但 1337 不应答（探测：${PROBE:-无应答}）：插件没加载，请重启 Deck 后再部署" ;;
  esac
  printf '  Decky 已安全重载：NRestarts %s → %s，1337 探测 %s\n' "${NR_BEFORE:-?}" "${NR_AFTER:-?}" "$PROBE"

  # Decky 日志里确认插件确实被加载；有明确错误就打印出来
  sleep 3
  LOADLOG=$(ssh "$HOST" "journalctl -u $DECKY_UNIT --since '90 seconds ago' --no-pager | tail -80" || true)
  if printf '%s\n' "$LOADLOG" | grep -qiE 'error loading|failed to load|traceback'; then
    printf '%s\n' "$LOADLOG"
    die "Decky 日志里出现加载错误（见上）"
  fi
  printf '%s\n' "$LOADLOG" | grep -i 'DeckMeido' | tail -5 || printf '  （日志里没有 DeckMeido 字样，但 loader 已 active）\n'
  OUT_SHA=$(ssh "$HOST" "sha256sum $PLUGIN_REMOTE/dist/index.js | cut -d' ' -f1")
  [ "$OUT_SHA" = "$LOCAL_SHA" ] || die "部署后 dist 哈希不一致（$OUT_SHA）"
  printf '  插件 dist sha256 -> %s\n' "$OUT_SHA"
  OUT_MAIN_SHA=$(ssh "$HOST" "sha256sum $PLUGIN_REMOTE/main.py | cut -d' ' -f1")
  [ "$OUT_MAIN_SHA" = "$LOCAL_MAIN_SHA" ] || die "部署后插件后端哈希不一致（$OUT_MAIN_SHA）"
  printf '  插件 main.py sha256 -> %s\n' "$OUT_MAIN_SHA"
fi

log "完成：后端已同步并重启，插件按需重载"
