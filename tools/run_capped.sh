#!/bin/bash
# run_capped.sh -- run a one-off job on poe-bot-vm under a hard memory cap, and stop it OURSELVES before the
# kernel would have to kill it (2026-10-15: an uncapped job turned into a 2.6GB thrash and a 72h outage).
#
#   sudo tools/run_capped.sh <unit-name> <cap-MB> <stop-%> -- <command> [args...]
#   e.g. sudo tools/run_capped.sh meta-recompute 460 90 -- /opt/poe-data-hub/.venv/bin/python -m src.main --run-meta-once --only slot
#
# - a transient systemd service (MemoryMax=<cap>M, MemorySwapMax=0) so a runaway can never touch swap or the
#   other services; same user/dir/credentials as poe-data-hub.service
# - every 5 s the job's ANONYMOUS memory (memory.stat `anon`, i.e. real RSS; page cache is reclaimable and would
#   cause false alarms) is compared with <stop-%> of the cap; over it -> `systemctl stop` (clean SIGTERM) and exit 3
# - an OOM kill in memory.events also reports exit 4. `max` in memory.events is NOT treated as a cap hit: it counts
#   every reclaim at the limit, page cache included (a job reading a large file hits it at 36MB anon), so it is only
#   a number in the log line; the anon check above is what stops a job that really grows.
# Exit: the job's own status when it finishes by itself; 3 stopped by this script; 4 the kernel OOM-killed it.
set -u
UNIT="${1:?unit name}"; CAP_MB="${2:?cap in MB}"; STOP_PCT="${3:?stop percent}"
[ "${4:-}" = "--" ] || { echo "usage: $0 <unit> <cap-MB> <stop-%> -- <command...>" >&2; exit 2; }
shift 4
SERVICE_USER="${RUN_CAPPED_USER:-Winai}"
WORKDIR="${RUN_CAPPED_WORKDIR:-/opt/poe-data-hub}"
CREDS="${RUN_CAPPED_CREDS:-/opt/poe-data-hub/data/service-account.json}"
INTERVAL="${RUN_CAPPED_INTERVAL:-5}"
CG="/sys/fs/cgroup/system.slice/$UNIT.service"
STOP_AT=$(( CAP_MB * STOP_PCT / 100 ))

log() { echo "$(date -u +%FT%TZ) run_capped[$UNIT] $*"; }

systemctl reset-failed "$UNIT" 2>/dev/null
systemd-run --quiet --unit="$UNIT" --property=User="$SERVICE_USER" --property=WorkingDirectory="$WORKDIR" \
    --property=MemoryMax="${CAP_MB}M" --property=MemorySwapMax=0 --property=Nice=10 \
    --setenv=GOOGLE_APPLICATION_CREDENTIALS="$CREDS" "$@" || { log "systemd-run failed"; exit 2; }
log "started: cap=${CAP_MB}MB, will stop at anon>=${STOP_AT}MB ($STOP_PCT%): $*"

peak=0
while systemctl is-active --quiet "$UNIT"; do
    if [ -r "$CG/memory.stat" ]; then
        anon_mb=$(awk '$1 == "anon" {printf "%d", $2 / 1048576}' "$CG/memory.stat")
        [ "${anon_mb:-0}" -gt "$peak" ] && peak=$anon_mb
        max_hits=$(awk '$1 == "max" {print $2}' "$CG/memory.events" 2>/dev/null)
        oom=$(awk '$1 == "oom_kill" {print $2}' "$CG/memory.events" 2>/dev/null)
        if [ "${anon_mb:-0}" -ge "$STOP_AT" ]; then
            log "STOPPING: anon ${anon_mb}MB >= ${STOP_AT}MB (cap ${CAP_MB}MB); peak so far ${peak}MB"
            systemctl stop "$UNIT"
            exit 3
        fi
        if [ "${oom:-0}" -gt 0 ]; then
            log "OOM KILL: memory.events oom_kill=$oom max=$max_hits (anon ${anon_mb}MB)"
            systemctl stop "$UNIT" 2>/dev/null
            exit 4
        fi
    fi
    sleep "$INTERVAL"
done
result=$(systemctl show "$UNIT" -p Result --value 2>/dev/null); status=$(systemctl show "$UNIT" -p ExecMainStatus --value 2>/dev/null)
systemctl reset-failed "$UNIT" 2>/dev/null
log "finished: result=${result:-?} exit=${status:-?} peak anon ${peak}MB of ${CAP_MB}MB"
[ "${result:-success}" = "success" ] && exit 0
exit "${status:-1}"
