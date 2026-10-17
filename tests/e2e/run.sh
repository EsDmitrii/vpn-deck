#!/usr/bin/env bash
# End-to-end tests: real awg-quick / amneziawg-go / sing-box, real routing.
# Runs inside the builder image (holo-base + /binaries) as a privileged
# container; see infra/test-e2e.sh.
#
#   default netns ("deck")                netns srv ("internet")
#   wlan0 10.99.0.1/24 (home Wi-Fi)  <-->  s0 10.99.0.2/24 + 10.98.0.2/24
#         10.98.0.1/24 (hotspot)            lo 203.0.113.1  AWG server :51820
#                                           lo 203.0.113.2  VLESS server :8443 (tcp), :8444 (ws)
#                                           lo 198.51.100.1 echo server :8080
# Switching networks keeps the same wlan0 and swaps its address and gateway,
# as NetworkManager does on the Deck when you join another Wi-Fi.
set -euo pipefail

SRC="${SRC:-/src}"
WORK=/work
CFG=$WORK/e2e-configs
mkdir -p "$WORK" "$CFG"
tar -C "$SRC" --exclude=node_modules --exclude=.git --exclude=out -cf - . | tar -C "$WORK" -xf -
cp /binaries/* "$WORK/bin/"
export PATH="$WORK/bin:$PATH"

# ── topology ────────────────────────────────────────────────────────
ip netns add srv
ip link add wlan0 type veth peer name s0 netns srv
ip addr add 10.99.0.1/24 dev wlan0 && ip link set wlan0 up
ip -n srv addr add 10.99.0.2/24 dev s0
ip -n srv addr add 10.98.0.2/24 dev s0
ip -n srv link set s0 up
ip -n srv link set lo up
# Keep 10.99.0.254 when the "old router" 10.99.0.2 is removed (switch_router).
ip netns exec srv sysctl -qw net.ipv4.conf.all.promote_secondaries=1 net.ipv4.conf.s0.promote_secondaries=1
for a in 203.0.113.1 203.0.113.2 198.51.100.1; do ip -n srv addr add "$a/32" dev lo; done
# Like a real server, answer from the address the client connected to;
# otherwise WireGuard roams the endpoint to the on-link 10.9x.0.2.
ip -n srv route replace 10.99.0.0/24 dev s0 src 203.0.113.1
ip -n srv route replace 10.98.0.0/24 dev s0 src 203.0.113.1
ip route del default 2>/dev/null || true
ip route add default via 10.99.0.2 dev wlan0 metric 100
echo "203.0.113.2 vless.test" >> /etc/hosts

# ── AWG server ──────────────────────────────────────────────────────
SRV_KEY=$(awg genkey); SRV_PUB=$(echo "$SRV_KEY" | awg pubkey)
CLI_KEY=$(awg genkey); CLI_PUB=$(echo "$CLI_KEY" | awg pubkey)
OBFS="Jc = 4
Jmin = 40
Jmax = 70
S1 = 15
S2 = 25
H1 = 1111111
H2 = 2222222
H3 = 3333333
H4 = 4444444"
cat > "$CFG/srv.conf" <<CONF
[Interface]
PrivateKey = $SRV_KEY
ListenPort = 51820
$OBFS

[Peer]
PublicKey = $CLI_PUB
AllowedIPs = 10.66.66.2/32
CONF
ip netns exec srv amneziawg-go awg-srv
ip netns exec srv awg setconf awg-srv "$CFG/srv.conf"
ip -n srv addr add 10.66.66.1/24 dev awg-srv && ip -n srv link set awg-srv up

client_conf() {  # $1 = AllowedIPs
	cat <<CONF
[Interface]
PrivateKey = $CLI_KEY
Address = 10.66.66.2/32
MTU = 1280
$OBFS

[Peer]
PublicKey = $SRV_PUB
Endpoint = 203.0.113.1:51820
AllowedIPs = $1
PersistentKeepalive = 5
CONF
}
client_conf "0.0.0.0/0" > "$CFG/full.conf"
client_conf "10.66.66.0/24, 203.0.113.0/24" > "$CFG/split.conf"

# ── VLESS server (sing-box) ─────────────────────────────────────────
UUID=$(sing-box generate uuid)
cat > "$CFG/vless-server.json" <<JSON
{
  "log": {"level": "warn"},
  "inbounds": [
    {"type": "vless", "listen": "203.0.113.2", "listen_port": 8443, "users": [{"uuid": "$UUID"}]},
    {"type": "vless", "listen": "203.0.113.2", "listen_port": 8444, "users": [{"uuid": "$UUID"}],
     "transport": {"type": "ws", "path": "/ws", "max_early_data": 2048,
                   "early_data_header_name": "Sec-WebSocket-Protocol"}}
  ],
  "outbounds": [{"type": "direct"}]
}
JSON
ip netns exec srv sing-box run -c "$CFG/vless-server.json" > "$CFG/vless-server.log" 2>&1 &
echo "vless://$UUID@vless.test:8443?type=tcp&security=none#tcp" > "$CFG/vless-tcp.txt"
echo "vless://$UUID@203.0.113.2:8444?type=ws&path=%2Fws%3Fed%3D2048&security=none#ws" > "$CFG/vless-ws.txt"

ip netns exec srv python3 "$WORK/tests/e2e/echo_server.py" 198.51.100.1 8080 &
sleep 1

cd "$WORK"
E2E_CONFIGS="$CFG" python3 -m unittest -v tests.e2e.scenarios
