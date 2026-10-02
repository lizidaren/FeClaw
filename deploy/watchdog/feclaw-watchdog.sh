#!/bin/bash
# FeClaw 健康看门狗 —— 真健康检查（不再只探 /docs）
#
# 背景：旧版只 curl 127.0.0.1:58080/docs。而 /docs 由「谁持有 58080 端口」谁回答；
#       2026-08-22 起线上整整 40 天是一个 nohup 野生进程在扛、systemd 那个半死，
#       看门狗探的 /docs 一直 200 ⇒ 永远不触发重启 ⇒ 半死状态续命 40 天。
#
# 修正：
#   ① 探 /health（业务健康，须含 "healthy"）+ /login（关键业务入口，须 200）
#   ② 校验监听端口的进程必须就是 systemd 管的 MainPID（防「野生进程扛线上」）
#   ③ 失败必须可见：写 syslog（logger -t feclaw-watchdog）+ 退出码非 0，不许静默
#
# 恢复策略：systemctl restart（纪律：不允许 nohup / 不允许按 pattern 杀进程）。
#   若端口 owner ≠ MainPID（疑似野生进程），仅 restart 救不回——记 CRITICAL 并退出 1，
#   留给人工按 deploy.sh 验收段提示的方式清野生进程。
#
# 用法（部署到服务器，见 q6-report）：
#   install -m 0755 deploy/watchdog/feclaw-watchdog.sh /usr/local/bin/feclaw-watchdog.sh
#   install -m 0644 deploy/watchdog/feclaw-watchdog.cron /etc/cron.d/feclaw-watchdog
#
# 可用环境变量覆盖：FECLAW_PORT FECLAW_SERVICE FECLAW_WD_MAX_WAIT FECLAW_WD_RESTART_WAIT

set -uo pipefail

PORT="${FECLAW_PORT:-58080}"
SERVICE="${FECLAW_SERVICE:-feclaw-backend}"
BASE="http://127.0.0.1:${PORT}"
MAX_WAIT="${FECLAW_WD_MAX_WAIT:-5}"
RESTART_WAIT="${FECLAW_WD_RESTART_WAIT:-15}"

log() { logger -t feclaw-watchdog -p "daemon.$1" "$2"; }
fail() { local m="$1"; log err "FAIL: ${m}"; echo "FAIL: ${m}" >&2; }

# ① 关键接口必须健康：/health 含 "healthy"，/login 返回 200
probe() {
  local h code
  h="$(curl -sf --max-time "${MAX_WAIT}" "${BASE}/health" 2>/dev/null)" \
    || { fail "/health 不通"; return 1; }
  echo "${h}" | grep -q '"healthy"' \
    || { fail "/health 响应异常: ${h:0:80}"; return 1; }

  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time "${MAX_WAIT}" "${BASE}/login" 2>/dev/null)" \
    || code="000"
  [ "${code}" = "200" ] || { fail "/login 返回 ${code}（期望 200）"; return 1; }
  return 0
}

# ② 监听端口的进程必须就是 systemd 的 MainPID（防野生进程扛线上）
owner_ok() {
  local mp owner
  mp="$(systemctl show "${SERVICE}" -p MainPID --value 2>/dev/null)"
  owner="$(ss -ltnp 2>/dev/null | awk -v p=":${PORT}" '$1=="LISTEN" && $4 ~ p {print $NF; exit}')"
  if [ -z "${mp}" ] || [ "${mp}" = "0" ]; then
    fail "systemd 单元 ${SERVICE} 无 MainPID（可能已死）"
    return 1
  fi
  case "${owner}" in
    *"pid=${mp},"*) return 0 ;;
    *) fail "端口 ${PORT} 的监听者不是 systemd 管的进程（MainPID=${mp}，实际=${owner}）—— 疑似野生进程" ; return 1 ;;
  esac
}

# 健康则静默退出 0（正常心跳）
if probe && owner_ok; then
  exit 0
fi

# 不健康：先记 WARNING，再 systemctl restart，等恢复后复检
log warning "触发恢复：restart ${SERVICE}"
systemctl restart "${SERVICE}" 2>&1 | logger -t feclaw-watchdog -p daemon.warning || true
sleep "${RESTART_WAIT}"

if probe && owner_ok; then
  log info "恢复成功"
  exit 0
fi

fail "重启后仍未恢复（${BASE}/health 或端口 owner 校验失败）"
exit 1
