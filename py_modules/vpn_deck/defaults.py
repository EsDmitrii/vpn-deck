"""Default values for plugin behaviour, in one place.

Values a user may reasonably want to change are read through Settings
(settings.json) with these as fallbacks; the rest are internal tuning.
"""

# ── Import ──────────────────────────────────────────────────────────
# Real configs are a few KB; the cap stops a mis-picked file or a
# decompression bomb inside a vpn:// link from eating memory.
MAX_IMPORT_BYTES = 1024 * 1024

# awg-quick picks ~1420 when MTU is absent. AWG adds junk/padding to
# packets, so on many networks that ends in "sendmmsg: message too long"
# (issue #6). AmneziaVPN's own client uses a lower value.
RECOMMENDED_AWG_MTU = 1280
MTU_MIN = 576
MTU_MAX = 1500

# AmneziaVPN fills $PRIMARY_DNS/$SECONDARY_DNS with these when the link
# carries no usable dns1/dns2; we do the same.
AMNEZIA_FALLBACK_DNS = ("1.1.1.1", "1.0.0.1")

# Panels pick the response format by User-Agent; a v2ray client gets the
# base64 list of share links.
SUBSCRIPTION_USER_AGENT = "v2rayN/7.0"

# ── sing-box (VLESS) ────────────────────────────────────────────────
# DoH server queried *through the proxy*, so local DNS blocking doesn't apply.
SINGBOX_DNS_SERVER = "1.1.1.1"
# Most VLESS servers have no IPv6 egress; AAAA answers would black-hole.
SINGBOX_IPV4_ONLY = True
SINGBOX_TUN_MTU = 1500
# "system" uses the kernel stack; "gvisor"/"mixed" are alternatives.
SINGBOX_TUN_STACK = "system"
# Private /30 and /126 for the TUN device; any unused private range works.
SINGBOX_TUN_ADDRESSES = ("172.19.0.1/30", "fdfe:dcba:9876::1/126")
SINGBOX_START_TIMEOUT_SEC = 15
SINGBOX_STOP_TIMEOUT_SEC = 10
SINGBOX_RUN_DIR = "/run/vpn-deck"

# ── Tunnel watcher (sleep / network change) ─────────────────────────
WATCH_INTERVAL_SEC = 15
# WireGuard rekeys every 2 min under traffic; with keepalive a handshake
# older than this means the peer is unreachable.
STALE_HANDSHAKE_SEC = 180
# Extra wait after a stale handshake is seen, so a brief outage doesn't
# trigger a restart.
RESTART_GRACE_SEC = 60
MIN_RESTART_INTERVAL_SEC = 300
RETRY_UP_INTERVAL_SEC = 60

# ── Connection status ───────────────────────────────────────────────
EXIT_IP_URL = "https://cloudflare.com/cdn-cgi/trace"
EXIT_IP_REFRESH_SEC = 120

# ── Restore after reboot ────────────────────────────────────────────
RESTORE_ON_BOOT = True
RESTORE_WAIT_SEC = 180
