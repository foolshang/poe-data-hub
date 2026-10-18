#!/bin/bash
# net-watchdog.sh -- restore networking on poe-bot-vm when the NIC loses its
# address or DNS stops working.
#
# Why this exists: on 2026-10-15 memory thrash made systemd-networkd miss a
# DHCP renewal ("ens4: Could not set DHCPv4 address: Connection timed out",
# then "ens4: Failed"). The kernel and journald kept running, but the VM had
# no network for ~72 hours until someone reset it. See logs/ (local only) and
# CLAUDE.md. Nothing restarted networkd on its own.
#
# Deliberately plain bash (no python, no pip deps): it must still be able to
# run when RAM is tight. The unit gives it OOMScoreAdjust/MemoryLow/Nice.
#
# Probes, in order (all independent of the public internet):
#   1. a global IPv4 address on some interface
#   2. an IPv4 default route
#   3. TCP connect to the GCE metadata server (169.254.169.254:80)
#   4. DNS: resolve metadata.google.internal
# 1-3 failing = "L3 down", only 4 failing = "DNS down".
#
# Escalation (consecutive failed runs, one run every ~2 min):
#   1st      log only (a single miss is not an outage)
#   2nd      L3: networkctl reconfigure <if> | DNS: restart systemd-resolved
#   4th      restart systemd-networkd + systemd-resolved
#   8th      restart google-guest-agent too, then log CRITICAL
#   16th,... log CRITICAL only (needs an external reset; this script never
#            reboots the machine)
#
# Test knobs (env): NET_WATCHDOG_DRY_RUN=1 (log what it would do, change
# nothing), NET_WATCHDOG_PROBE_IP / _PROBE_PORT / _DNS_NAME (point a probe
# at something that fails), NET_WATCHDOG_STATE_DIR.
set -u

STATE_DIR="${NET_WATCHDOG_STATE_DIR:-/run/net-watchdog}"
DRY_RUN="${NET_WATCHDOG_DRY_RUN:-0}"
PROBE_IP="${NET_WATCHDOG_PROBE_IP:-169.254.169.254}"
PROBE_PORT="${NET_WATCHDOG_PROBE_PORT:-80}"
DNS_NAME="${NET_WATCHDOG_DNS_NAME:-metadata.google.internal}"
TAG="net-watchdog"

mkdir -p "$STATE_DIR" 2>/dev/null
exec 9>"$STATE_DIR/lock"
flock -n 9 || exit 0   # a previous run is still going

log() {  # log <priority> <message>
    logger -t "$TAG" -p "daemon.$1" -- "$2"
    echo "$TAG [$1] $2"
}

act() {  # act <description> <command...>: run it, or just say so in dry-run
    local what="$1"; shift
    if [ "$DRY_RUN" = "1" ]; then
        log warning "DRY-RUN would: $what"
    else
        log warning "ACTION: $what"
        "$@" >/dev/null 2>&1 || log err "action failed: $what"
    fi
}

iface="$(ip -4 route show default 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "dev") {print $(i + 1); exit}}')"
if [ -z "$iface" ]; then
    iface="$(ls /sys/class/net 2>/dev/null | grep -v '^lo$' | head -n 1)"
fi

l3_ok=1
ip -4 -o addr show scope global 2>/dev/null | grep -q . || l3_ok=0
ip -4 route show default 2>/dev/null | grep -q . || l3_ok=0
timeout 4 bash -c "exec 3<>/dev/tcp/$PROBE_IP/$PROBE_PORT" 2>/dev/null || l3_ok=0

dns_ok=1
timeout 5 getent hosts "$DNS_NAME" >/dev/null 2>&1 || dns_ok=0

fails=0
[ -r "$STATE_DIR/fails" ] && fails="$(cat "$STATE_DIR/fails" 2>/dev/null)"
case "$fails" in ''|*[!0-9]*) fails=0 ;; esac

if [ "$l3_ok" = 1 ] && [ "$dns_ok" = 1 ]; then
    if [ "$fails" -gt 0 ]; then
        log notice "recovered after $fails consecutive failed check(s) (iface=${iface:-?})"
    fi
    echo 0 > "$STATE_DIR/fails"
    date -u +%s > "$STATE_DIR/last_ok"
    exit 0
fi

fails=$((fails + 1))
echo "$fails" > "$STATE_DIR/fails"
if [ "$l3_ok" = 0 ]; then kind="L3"; else kind="DNS"; fi
log warning "check failed ($kind down, iface=${iface:-?}, consecutive=$fails, addr+route+tcp=$l3_ok dns=$dns_ok)"

case "$fails" in
    1) ;;
    2)
        if [ "$kind" = "L3" ]; then
            if command -v networkctl >/dev/null 2>&1 && [ -n "$iface" ]; then
                act "networkctl reconfigure $iface" networkctl reconfigure "$iface"
            else
                act "restart systemd-networkd" systemctl restart systemd-networkd
            fi
        else
            act "restart systemd-resolved" systemctl restart systemd-resolved
        fi
        ;;
    4)
        act "restart systemd-networkd" systemctl restart systemd-networkd
        act "restart systemd-resolved" systemctl restart systemd-resolved
        ;;
    8)
        act "restart google-guest-agent" systemctl restart google-guest-agent
        log crit "network still down after $fails checks (~$((fails * 2)) min); an external reset may be needed"
        ;;
    *)
        if [ "$fails" -ge 16 ] && [ $((fails % 16)) -eq 0 ]; then
            log crit "network still down after $fails checks (~$((fails * 2)) min); needs an external reset"
        fi
        ;;
esac
exit 0
