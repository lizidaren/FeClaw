#!/bin/bash
# FeClaw 部署脚本 —— 同步代码 + 用 systemd 重启服务
#
# ⚠️ 2026-10-01 重写。旧版有两个致命做法，当天在生产上出过事故：
#   1) `pkill -f "uvicorn main:app"` —— 按命令行 pattern 杀进程，会误杀无关进程（含自己）
#   2) `nohup python -m uvicorn ... &` —— 手起的进程 systemd 管不到，会变成「野生进程」：
#      systemctl restart 杀不掉它，线上实际由谁在扛流量和 systemd 记录完全脱节
#      （2026-08-22 起，线上整整 40 天是一个 nohup 野生进程在扛，systemd 那个是半死的壳）
#
# ⚠️ 2026-10-25（T02/T21）：健康闸门从「/health 通了就算成功」改成 **/ready 真实就绪**。
#   旧闸门只看 curl 退出码：/health 只是 liveness（进程还在），DB 挂了、schema 没迁移、
#   存储没配置它照样回 200，于是部署把坏版本切成「已上线」。
#   现在必须同时满足三条，缺一即失败（空输出、unhealthy 一律失败，不猜成功）：
#     ① curl 退出码 == 0
#     ② HTTP status == 200
#     ③ 响应体是非空 JSON，且 status == "ready"
#
# 纪律：服务只允许通过 systemctl 管理，不允许 nohup / 不允许按 pattern 杀进程。
#
# 用法：
#   ./deploy.sh                # 同步 + schema 门禁 + 重启 + 验收
#   ./deploy.sh --dry-run      # 只看会同步哪些文件，不动远端
#   ./deploy.sh --migrate      # 先执行 schema 迁移（--apply）再重启（需运维批准）
#
# 可用环境变量覆盖：FECLAW_SERVER FECLAW_SSH_KEY FECLAW_REMOTE_DIR
#                   FECLAW_LOCAL_DIR FECLAW_SERVICE FECLAW_PORT
#                   FECLAW_PYTHON（解析 /ready JSON 用，默认 python3/python）
#                   FECLAW_REMOTE_PYTHON（远端跑迁移脚本用，默认 python3）
#                   FECLAW_READY_TIMEOUT（单次 /ready 超时秒数，默认 5）

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
READY_TIMEOUT="${FECLAW_READY_TIMEOUT:-5}"

# /ready JSON 解析器：显式选，选不到就 fail-closed（不靠 grep 假过）
READY_PY="${FECLAW_PYTHON:-}"
if [ -z "$READY_PY" ]; then
  if command -v python3 >/dev/null 2>&1; then
    READY_PY="python3"
  elif command -v python >/dev/null 2>&1; then
    READY_PY="python"
  else
    echo "FAIL: 找不到 python3/python 用于解析 /ready JSON —— 拒绝部署（不猜成功）" >&2
    exit 1
  fi
fi

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

# /ready 闸门：退出码 + HTTP status + JSON status=="ready" 三条全过才算就绪。
# 输出人类可读结果；返回 0=就绪，非 0=未就绪。
ready_gate() {
  local out code body
  if ! out="$("${SSH[@]}" "$SERVER" \
      "curl -sS --max-time ${READY_TIMEOUT} -w '\n%{http_code}' http://127.0.0.1:${PORT}/ready" 2>/dev/null)"; then
    echo "  ✗ /ready 请求失败：curl 退出码非 0（连不上或超时）"
    return 1
  fi
  code="${out##*$'\n'}"
  body="${out%$'\n'*}"

  # ① 空输出不得当成功（旧脚本 curl -sf 成功 + 空 body 也算通过）
  if [ -z "${body//[[:space:]]/}" ]; then
    echo "  ✗ /ready 空响应 —— 不得视为健康"
    return 1
  fi
  # ② HTTP status
  if [ "${code}" != "200" ]; then
    echo "  ✗ /ready HTTP status=${code}（必须 200）"
    echo "    body: $(printf '%s' "$body" | head -c 500)"
    return 1
  fi
  # ③ JSON status == "ready"（解析而不是 grep，坏 JSON 直接失败）
  #    注意：这段 python 在操作者本机跑（可能是 Windows/Git Bash，locale=GBK），
  #    所以先固定 UTF-8 输出并用 ASCII 标记，避免「门禁自己编码失败」被误读成服务坏了。
  if ! printf '%s' "$body" | "${READY_PY}" -c '
import json
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    payload = json.load(sys.stdin)
except Exception as exc:
    print("  FAIL /ready JSON parse error: %s" % type(exc).__name__)
    sys.exit(1)
if not isinstance(payload, dict):
    print("  FAIL /ready response is not a JSON object")
    sys.exit(1)
status = payload.get("status")
if status != "ready":
    print("  FAIL /ready status=%r (must be \"ready\"; unhealthy always fails)" % (status,))
    print("       checks: %s" % json.dumps(payload.get("checks"), ensure_ascii=False))
    sys.exit(1)
print("  OK /ready -> status=ready")
'; then
    return 1
  fi
  printf '    %s\n' "$(printf '%s' "$body" | head -c 800)"
  return 0
}

DRY_RUN=""
MIGRATE=""
for _arg in "$@"; do
  case "$_arg" in
    --dry-run) DRY_RUN="--dry-run" ;;
    --migrate) MIGRATE="1" ;;
    *) echo "FAIL: 未知参数 $_arg（支持 --dry-run / --migrate）" >&2; exit 1 ;;
  esac
done

REMOTE_PY="${FECLAW_REMOTE_PYTHON:-python3}"

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
echo "--- 2. schema 迁移门禁（T20a：迁移必须显式；默认只检查不执行）---"
if [ -n "$MIGRATE" ]; then
  echo "  模式：--migrate（先 apply 再重启；已获运维批准）"
  "${SSH[@]}" "$SERVER" "cd '$REMOTE_DIR' && '$REMOTE_PY' scripts/migrate_learning_runtime.py --apply" \
    || { echo "FAIL: schema 迁移失败，拒绝重启（旧版本仍在运行）" >&2; exit 1; }
else
  echo "  模式：只检查（未通过则拒绝重启；如需迁移请显式加 --migrate）"
fi
# 无论哪种模式，重启前都要求“没有 pending/blocked”，否则新代码会在旧 schema 上带病启动。
"${SSH[@]}" "$SERVER" "\
  cd '$REMOTE_DIR' || exit 1; \
  '$REMOTE_PY' scripts/migrate_learning_runtime.py --plan --json > /tmp/feclaw_schema_plan.json || _rc=\$?; \
  '$REMOTE_PY' - <<'PY'
import json
import sys

try:
    rows = json.load(open('/tmp/feclaw_schema_plan.json', encoding='utf-8'))
except Exception as exc:
    print('FAIL: 无法读取迁移预览（%s）' % type(exc).__name__)
    sys.exit(1)
bad = [r for r in rows if r.get('state') in ('pending', 'blocked', 'missing_table')]
if bad:
    print('FAIL: schema 仍未就绪：%d 项待处理' % len(bad))
    for row in bad[:20]:
        print('  - %-38s %-14s %s' % (row.get('name'), row.get('state'), row.get('ddl') or ''))
    print('请先在服务器上执行（或重跑 ./deploy.sh --migrate）：')
    print('  cd %s && python3 scripts/migrate_learning_runtime.py --plan')
    print('  cd %s && python3 scripts/migrate_learning_runtime.py --apply')
    sys.exit(2)
print('OK: schema 无待处理项')
PY
" || { echo "FAIL: schema 门禁未通过，拒绝重启（旧版本仍在运行）" >&2; exit 1; }

echo
echo "--- 3. 重启服务（systemd）---"
"${SSH[@]}" "$SERVER" "sudo systemctl restart $SERVICE"

echo "--- 4. 等待 /ready 真的就绪（最多 60 秒；退出码 + HTTP status + status==ready）---"
ok=""
last_report=""
for _ in $(seq 1 30); do
  if last_report="$(ready_gate 2>&1)"; then
    ok=1
    break
  fi
  sleep 2
done
if [ -z "$ok" ]; then
  echo "FAIL: 启动后 /ready 一直未通过（最后一次：）"
  echo "$last_report"
  echo "  最后 50 行日志："
  "${SSH[@]}" "$SERVER" "sudo journalctl -u $SERVICE -n 50 --no-pager"
  exit 1
fi
echo "$last_report"

echo "--- 5. 验收（关键：监听端口的必须就是 systemd 那个主进程 + /ready 复验）---"
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
# 仅供人工查看；门禁已在步骤 3 严格校验（退出码 + status + JSON status==ready）
echo -n "/health: "; curl -s --max-time 5 "http://127.0.0.1:$PORT/health"; echo
echo -n "/ready : "; curl -s --max-time 5 "http://127.0.0.1:$PORT/ready"; echo
REMOTE

echo
echo "=== 部署完成 ==="
