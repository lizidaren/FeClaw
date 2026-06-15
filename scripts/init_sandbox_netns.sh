#!/bin/bash
# init_sandbox_netns.sh — FeClaw 沙箱网络命名空间初始化
# Called by systemd oneshot at boot (runs as root)
# Idempotent: safe to re-run without breaking existing configuration
#
# NEVER uses iptables -F FORWARD (breaks Docker/K8S)

set -euo pipefail

NETNS_NAME="feclaw-sandbox"
HOST_IP="10.200.0.1/24"
SANDBOX_IP="10.200.0.2/24"
HOST_VETH="fl-host"
SANDBOX_VETH="fl-sbx"
CHAIN="FECLAW-SBX-FWD"
SANDBOX_NET="10.200.0.0/24"
PRIVATE_NETS=("10.0.0.0/8" "172.16.0.0/12" "192.168.0.0/16" "127.0.0.0/8" "100.64.0.0/10")

# ── 1. Create netns ──
if ! ip netns list | grep -q "$NETNS_NAME"; then
    ip netns add "$NETNS_NAME"
fi

# ── 2. Create veth pair ──
if ! ip link show "$HOST_VETH" &>/dev/null; then
    ip link add "$HOST_VETH" type veth peer name "$SANDBOX_VETH"
fi

# ── 3. Move sandbox veth into netns ──
ip link set "$SANDBOX_VETH" netns "$NETNS_NAME" 2>/dev/null || true

# ── 4. Assign IPs ──
ip addr add "$HOST_IP" dev "$HOST_VETH" 2>/dev/null || true
ip netns exec "$NETNS_NAME" ip addr add "$SANDBOX_IP" dev "$SANDBOX_VETH" 2>/dev/null || true

# ── 5. Bring up all links ──
ip link set "$HOST_VETH" up
ip netns exec "$NETNS_NAME" ip link set "$SANDBOX_VETH" up
ip netns exec "$NETNS_NAME" ip link set lo up

# ── 6. Default route in sandbox netns ──
ip netns exec "$NETNS_NAME" ip route add default via 10.200.0.1 2>/dev/null || true

# ── 7. Enable IP forwarding ──
echo 1 > /proc/sys/net/ipv4/ip_forward

# ── 8. Create/refresh custom iptables chain ──
iptables -N "$CHAIN" 2>/dev/null || iptables -F "$CHAIN"

# ── 9. Insert FORWARD jump (check before insert) ──
if ! iptables -C FORWARD -i "$HOST_VETH" -j "$CHAIN" 2>/dev/null; then
    iptables -I FORWARD 1 -i "$HOST_VETH" -j "$CHAIN"
fi

# ── 10. Ingress filter (anti-spoofing) ──
iptables -A "$CHAIN" ! -s "$SANDBOX_NET" -j DROP

# ── 11. INPUT: block sandbox -> host ──
if ! iptables -C INPUT -i "$HOST_VETH" -j DROP 2>/dev/null; then
    iptables -A INPUT -i "$HOST_VETH" -j DROP
fi

# ── 12. DROP private nets with rate-limited LOG ──
for NET in "${PRIVATE_NETS[@]}"; do
    iptables -A "$CHAIN" -d "$NET" -m limit --limit 10/min \
        -j LOG --log-prefix "FECLAW-BLOCK: " --log-level 4 2>/dev/null || true
    iptables -A "$CHAIN" -d "$NET" -j DROP
done

# ── 13. ACCEPT established/related ──
iptables -A "$CHAIN" -m state --state ESTABLISHED,RELATED -j ACCEPT

# ── 14. ACCEPT rest (public internet) ──
iptables -A "$CHAIN" -j ACCEPT

# ── 15. MASQUERADE NAT ──
if ! iptables -t nat -C POSTROUTING -s "$SANDBOX_NET" ! -o "$HOST_VETH" -j MASQUERADE 2>/dev/null; then
    iptables -t nat -A POSTROUTING -s "$SANDBOX_NET" ! -o "$HOST_VETH" -j MASQUERADE
fi

# ── 16. Disable IPv6 inside sandbox netns ──
ip netns exec "$NETNS_NAME" sysctl -w net.ipv6.conf.all.disable_ipv6=1 >/dev/null 2>&1 || true

# ── 17. Configure DNS ──
mkdir -p /etc/netns/"$NETNS_NAME"
cat > /etc/netns/"$NETNS_NAME"/resolv.conf << 'EOF'
nameserver 119.29.29.29
nameserver 223.5.5.5
nameserver 8.8.8.8
options timeout:1 attempts:2
EOF

echo "✅ Sandbox netns initialized: $NETNS_NAME"
echo "   DNS: 119.29.29.29 (DNSPod) / 223.5.5.5 (AliDNS)"
echo "   Blocked: private nets + INPUT host"
