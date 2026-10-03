#!/bin/bash
# FeClaw 部署脚本 —— 同步代码 + 用 systemd 重启服务
#
# ⚠️ 2026-10-01 重写。旧版有两个致命做法，当天在生产上出过事故：
#   1) `pkill -f "uvicorn main:app"` —— 按命令行 pattern 杀进程，会误杀无关进程（含自己）
#   2) `nohup python -m uvicorn ... &` —— 手起的进程 systemd 管不到，会变成「野生进程」：
#      systemctl restart 杀不掉它，线上实际由谁在扛流量和 systemd 记录完全脱节
#      （2026-08-22 起，线上整整 40 天是一个 nohup 野生进程在扛，systemd 那个是半死的壳）
#
# 纪律：服务只允许通过 systemctl 管理，不允许 nohup / 不允许按 pattern 杀进程。
#
# 用法：
#   ./deploy.sh                # 同步 + 重启 + 验收
#   ./deploy.sh --dry-run      # 只看会同步哪些文件，不动远端
#
# 可用环境变量覆盖：FECLAW_SERVER FECLAW_SSH_KEY FECLAW_REMOTE_DIR
#                   FECLAW_LOCAL_DIR FECLAW_SERVICE FECLAW_PORT

set -euo pipefail

SERVER="${FECLAW_SERVER:-ubuntu@139.199.89.129}"
# SSH 私钥：优先 FECLAW_SSH_KEY，其次 ~/.ssh/feclaw-server.pem，再次 ~/feclaw-server.pem
if [ -n "${FECLAW_SSH_KEY:-}" ]; then
  SSH_KEY="$FECLAW_SSH_KEY"
elif [ -f "$HOME/.ssh/feclaw-server.pem" ]; then
  SSH_KEY="$HOME/.ssh/feclaw-server.pem"
else
  SSH_KEY="$HOME/feclaw-server.pem"
fi
REMOTE_DIR="${FECLAW_REMOTE_DIR:-/home/ubuntu/FeClaw}"
LOCAL_DIR="${FECLAW_LOCAL_DIR:-$(cd "$(dirname "$0")" && pwd)}"
SERVICE="${FECLAW_SERVICE:-feclaw-backend}"
SSH=(ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no)

# FIX-E/P0-2：端口不再写死默认值（58080/8080 都不是单一真相源 —— 生产实测听 58080，
# 而仓库 unit 写 8080，两处默认值会让 watchdog 探测一个没人听的端口）。
# 单一真相源 = 远端 systemd unit 的 ExecStart；取不到退 FECLAW_PORT；仍取不到 ⇒ 报错退出（fail-closed，不猜）。
resolve_port() {
  local p
  p="$("${SSH[@]}" "$SERVER" "systemctl show '${SERVICE}' -p ExecStart --value 2>/dev/null | grep -oE -- '--port[= ]+[0-9]+' | grep -oE '[0-9]+' | head -n1" 2>/dev/null || true)"
  if [ -z "${p}" ]; then p="${FECLAW_PORT:-}"; fi
  if [ -z "${p}" ]; then
    echo "FAIL: 无法从 systemd ExecStart 或 FECLAW_PORT 解析端口 —— 拒绝继续（不猜默认值）" >&2
    exit 1
  fi
  echo "${p}"
}
PORT="$(resolve_port)"

DRY_RUN=""
[ "${1:-}" = "--dry-run" ] && DRY_RUN="--dry-run"

echo "=== 部署 FeClaw ==="
echo "  本地   : $LOCAL_DIR"
echo "  远端   : $SERVER:$REMOTE_DIR"
echo "  服务   : $SERVICE  (127.0.0.1:$PORT)"
[ -n "$DRY_RUN" ] && echo "  模式   : dry-run（不会动远端）"
echo

# 为什么不加 --delete：远端有几个本地没有的文件（如 scripts/package*.json），
# 加 --delete 会把它们删掉。宁可留下旧文件，也不要误删远端资产。
# 排除规则直接复用仓库的 .gitignore（rsync 不读它，得显式引入），避免把 dev 产物传上去。
echo "--- 1. 同步代码（rsync）---"
rsync -az $DRY_RUN --itemize-changes \
  --exclude '.git/' --exclude 'venv/' --exclude '.venv/' \
  --filter=':- .gitignore' \
  -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=no" \
  "$LOCAL_DIR/" "$SERVER:$REMOTE_DIR/"

if [ -n "$DRY_RUN" ]; then
  echo "=== dry-run 结束（未重启服务）==="
  exit 0
fi

echo
echo "--- 2. 重启服务（systemd）---"
"${SSH[@]}" "$SERVER" "sudo systemctl restart $SERVICE"

echo "--- 3. 等待启动完成（最多 60 秒）---"
ok=""
for _ in $(seq 1 30); do
  if "${SSH[@]}" "$SERVER" "curl -sf -o /dev/null --max-time 5 http://127.0.0.1:$PORT/health"; then ok=1; break; fi
  sleep 2
done
if [ -z "$ok" ]; then
  echo "FAIL: 启动后 /health 一直不通，最后 50 行日志："
  "${SSH[@]}" "$SERVER" "sudo journalctl -u $SERVICE -n 50 --no-pager"
  exit 1
fi

echo "--- 4. 验收（关键：监听端口的必须就是 systemd 那个主进程）---"
"${SSH[@]}" "$SERVER" "SERVICE='$SERVICE' PORT='$PORT' bash -s" <<'REMOTE'
set -e
systemctl is-active "$SERVICE"
mp=$(systemctl show "$SERVICE" -p MainPID --value)
owner=$(sudo ss -ltnp 2>/dev/null | awk -v p=":$PORT" '$1=="LISTEN" && $4 ~ p {print $NF; exit}')
echo "MainPID=$mp   port-owner=$owner"
case "$owner" in
  *"pid=$mp,"*) echo "OK: 监听进程 = systemd 管的进程" ;;
  *) echo "FAIL: :$PORT 的监听者不是 systemd 管的那个 —— 疑似有「野生进程」！"
     echo "      用 sudo ss -ltnp | grep :$PORT 看它是谁，用 sudo kill <pid> 清掉后再 systemctl restart"
     exit 1 ;;
esac
curl -s "http://127.0.0.1:$PORT/health"; echo
REMOTE

echo
echo "=== 部署完成 ==="
