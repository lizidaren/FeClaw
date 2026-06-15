#!/bin/bash
# cleanup_sandbox_netns.sh
# Tear down the FeClaw sandbox network namespace and all associated
# iptables rules, veth pair, and netns.

set -euo pipefail

NETNS_NAME="feclaw-sandbox"
HOST_VETH="fl-host"
SANDBOX_VETH="fl-sbx"
CHAIN="FECLAW-SBX-FWD"
SANDBOX_NET="10.200.0.0/24"

echo "Cleaning up FeClaw sandbox network isolation..."

# ── 1. Delete custom chain rules ──
iptables -F "$CHAIN" 2>/dev/null || true

# ── 2. Delete FORWARD jump ──
iptables -D FORWARD -i "$HOST_VETH" -j "$CHAIN" 2>/dev/null || true

# ── 3. Delete custom chain ──
iptables -X "$CHAIN" 2>/dev/null || true

# ── 4. Delete INPUT rule ──
iptables -D INPUT -i "$HOST_VETH" -j DROP 2>/dev/null || true

# ── 5. Delete MASQUERADE NAT rule ──
iptables -t nat -D POSTROUTING -s "$SANDBOX_NET" ! -o "$HOST_VETH" -j MASQUERADE 2>/dev/null || true

# ── 6. Delete veth pair (deleting host side removes both peers) ──
ip link delete "$HOST_VETH" 2>/dev/null || true

# ── 7. Delete netns ──
ip netns delete "$NETNS_NAME" 2>/dev/null || true

echo "✅ Sandbox netns cleaned up"
