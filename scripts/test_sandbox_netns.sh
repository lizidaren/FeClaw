#!/bin/bash
# test_sandbox_netns.sh
# 验证 sandbox 网络隔离效果
# 需要以 root 运行：sudo bash test_sandbox_netns.sh

set -euo pipefail

PASS=0
FAIL=0

check() {
    local desc="$1" result="$2"
    if [ "$result" = "PASS" ]; then
        echo "  ✅ $desc"
        PASS=$((PASS + 1))
    else
        echo "  ❌ $desc $result"
        FAIL=$((FAIL + 1))
    fi
}

echo "═══════════════════════════════════════════"
echo "  FeClaw 沙箱网络隔离验证"
echo "$(date)"
echo "═══════════════════════════════════════════"
echo ""

# ── 前置检查 ──
echo "【前置检查】"
if ip netns list | grep -q feclaw-sandbox; then
    echo "  ✅ netns feclaw-sandbox 存在"
else
    echo "  ❌ netns feclaw-sandbox 不存在，请先运行 init_sandbox_netns.sh"
    exit 1
fi

if ip link show fl-host &>/dev/null; then
    echo "  ✅ veth fl-host 存在"
else
    echo "  ❌ veth fl-host 不存在"
    exit 1
fi

echo ""

# ── Test 1: INPUT 阻断（沙箱 → 宿主机 10.200.0.1:8080）──
echo "【Test 1: INPUT 阻断】沙箱 → 宿主机 FeClaw API"
if ip netns exec feclaw-sandbox timeout 4 bash -c \
    'echo "" | nc -w 3 10.200.0.1 8080 2>/dev/null' 2>/dev/null; then
    check "10.200.0.1:8080（INPUT DROP）" "FAIL (连接未阻断)"
else
    check "10.200.0.1:8080（INPUT DROP）" "PASS"
fi

# ── Test 2: FORWARD 阻断（沙箱 → 192.168.x.x）──
echo "【Test 2: FORWARD 阻断】沙箱 → 192.168.1.1"
if ip netns exec feclaw-sandbox timeout 4 bash -c \
    'echo "" | nc -w 3 192.168.1.1 80 2>/dev/null' 2>/dev/null; then
    check "192.168.1.1:80（FORWARD DROP）" "FAIL (连接未阻断)"
else
    check "192.168.1.1:80（FORWARD DROP）" "PASS"
fi

# ── Test 3: FORWARD 阻断（沙箱 → 172.25.x.x）──
WSL_IP=$(ip addr show eth0 2>/dev/null | grep "inet " | awk '{print $2}' | cut -d/ -f1 || echo "172.25.0.1")
echo "【Test 3: FORWARD 阻断】沙箱 → WSL2 eth0 ($WSL_IP)"
if ip netns exec feclaw-sandbox timeout 4 bash -c \
    "echo '' | nc -w 3 $WSL_IP 80 2>/dev/null" 2>/dev/null; then
    check "$WSL_IP:80（FORWARD DROP）" "FAIL (连接未阻断)"
else
    check "$WSL_IP:80（FORWARD DROP）" "PASS"
fi

# ── Test 4: 阻止 127.0.0.0/8 ──
echo "【Test 4: FORWARD 阻断】沙箱 → 127.0.0.1"
if ip netns exec feclaw-sandbox timeout 4 bash -c \
    'echo "" | nc -w 3 127.0.0.1 80 2>/dev/null' 2>/dev/null; then
    check "127.0.0.1:80（FORWARD DROP）" "FAIL (连接未阻断)"
else
    check "127.0.0.1:80（FORWARD DROP）" "PASS"
fi

# ── Test 5: 阻止 10.x.x.x ──
echo "【Test 5: FORWARD 阻断】沙箱 → 10.0.0.1"
if ip netns exec feclaw-sandbox timeout 4 bash -c \
    'echo "" | nc -w 3 10.0.0.1 80 2>/dev/null' 2>/dev/null; then
    check "10.0.0.1:80（FORWARD DROP）" "FAIL (连接未阻断)"
else
    check "10.0.0.1:80（FORWARD DROP）" "PASS"
fi

echo ""

# ── Test 6: 公网可达 ──
echo "【Test 6: 公网可达】"
HTTP_CODE=$(ip netns exec feclaw-sandbox curl -s -o /dev/null -w "%{http_code}" \
    --connect-timeout 5 https://api.github.com 2>/dev/null || echo "000")
if [ "$HTTP_CODE" != "000" ]; then
    check "api.github.com (HTTP $HTTP_CODE)" "PASS"
else
    check "api.github.com" "FAIL (不可达)"
fi

# ── Test 7: DNS 解析 ──
echo "【Test 7: DNS 解析】"
DNS_OK=$(ip netns exec feclaw-sandbox nslookup github.com 119.29.29.29 \
    2>/dev/null | grep -c "Name\|address" || true)
if [ "$DNS_OK" -gt 0 ]; then
    check "DNS (119.29.29.29)" "PASS"
else
    check "DNS (119.29.29.29)" "FAIL"
fi

echo ""

# ── Test 8: bwrap 内部完整测试 ──
echo "【Test 8: bwrap 内部测试】（模拟生产执行路径）"
ip netns exec feclaw-sandbox bwrap --dev /dev --proc /proc --tmpfs /tmp \
  --ro-bind /usr /usr --ro-bind /bin /bin --ro-bind /lib /lib --ro-bind-try /lib64 /lib64 \
  --ro-bind /etc/ssl /etc/ssl --ro-bind-try /etc/resolv.conf /etc/resolv.conf \
  --ro-bind-try /usr/local /usr/local --ro-bind-try /usr/local/bin /usr/local/bin \
  --ro-bind-try /etc/alternatives /etc/alternatives \
  --ro-bind-try /usr/share/zoneinfo /usr/share/zoneinfo \
  --setenv HOME /tmp --setenv PYTHONDONTWRITEBYTECODE 1 \
  python3 -c "
import socket, urllib.request, sys

ok, fail = 0, 0

# INPUT 阻断
s = socket.socket(); s.settimeout(3)
try:
    s.connect(('10.200.0.1', 8080))
    print('  ❌ INPUT: 10.200.0.1:8080 应有阻断')
    fail += 1
except: 
    print('  ✅ INPUT: 10.200.0.1:8080 正确阻断')
    ok += 1

# FORWARD 阻断
s = socket.socket(); s.settimeout(3)
try:
    s.connect(('192.168.1.1', 80))
    print('  ❌ FORWARD: 192.168.1.1:80 应有阻断')
    fail += 1
except:
    print('  ✅ FORWARD: 192.168.1.1:80 正确阻断')
    ok += 1

# 公网
try:
    r = urllib.request.urlopen('https://api.github.com', timeout=5)
    print(f'  ✅ INTERNET: api.github.com ({r.status})')
    ok += 1
except Exception as e:
    print(f'  ❌ INTERNET: {e}')
    fail += 1

# DNS
import subprocess
r = subprocess.run(['nslookup', 'github.com', '119.29.29.29'],
    capture_output=True, text=True, timeout=5)
if 'Name:' in r.stdout:
    print('  ✅ DNS: 119.29.29.29 解析成功')
    ok += 1
else:
    print(f'  ❌ DNS: {r.stderr[:100]}')
    fail += 1

print(f'\\nbwrap 内部: {ok} PASS / {fail} FAIL')
sys.exit(0 if fail == 0 else 1)
" 2>&1

echo ""

# ── 汇总 ──
echo "═══════════════════════════════════════════"
echo "  汇总: $PASS PASS / $FAIL FAIL"
echo "═══════════════════════════════════════════"

# 清理
rm -f /tmp/sandbox_test_*.py 2>/dev/null || true
exit $FAIL
