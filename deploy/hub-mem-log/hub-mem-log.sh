#!/bin/bash
# hub-mem-log.sh -- one journal line per run with poe-data-hub's real memory:
# the process's own RSS/peak/swap (what the kernel's OOM table reports) AND
# the cgroup's counters (what MemoryMax enforces), so a cap hit is visible
# and RSS growth can be measured instead of guessed (2026-10-15 postmortem:
# a "7-day leak" turned out to be a one-shot spike that nothing recorded).
#
#   journalctl -t hub-mem --since today
#
# cg_peak and cg_current include reclaimable page cache (SQLite reads fill it to
# MemoryMax on purpose-built workloads), so they read near the cap without the
# process being short of memory. `max` in memory.events counts every reclaim
# round at the limit -- 2,833 of them during the 2026-10-19 skill run while the
# process held 190-210MB -- so it is only information. The danger signals are
# anonymous memory (`anon`, the process's real heap) near the cap and OOM kills.
UNIT="${HUB_MEM_UNIT:-poe-data-hub}"
PID="$(systemctl show "$UNIT" -p MainPID --value 2>/dev/null)"
[ -n "$PID" ] && [ "$PID" -gt 0 ] 2>/dev/null || exit 0
CG="/sys/fs/cgroup/system.slice/$UNIT.service"

kb() { awk -v k="$1" '$1 == k":" {print $2}' "/proc/$PID/status"; }
mb() { echo $(( ${1:-0} / 1048576 )); }
ev() { awk -v k="$1" '$1 == k {print $2}' "$CG/memory.events" 2>/dev/null; }

rss=$(kb VmRSS); hwm=$(kb VmHWM); swp=$(kb VmSwap)
avail=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
cur=$(cat "$CG/memory.current" 2>/dev/null); peak=$(cat "$CG/memory.peak" 2>/dev/null)

hwm_mb=$(( ${hwm:-0} / 1024 ))
max_hits=$(ev max); oom_kills=$(ev oom_kill)
# ALERT (priority warning; find them with: journalctl -t hub-mem -p warning) only for
#   * a NEW OOM kill since the previous line,
#   * current anonymous memory >= ALERT_ANON_MB (default 400 = ~87% of the 460M cap), or
#   * a NEW process peak >= ALERT_HWM_MB (project owner's rule 2026-10-18: ~15% margin under the cap).
# Cap hits (`max`) are logged as new_max=N, never alerting by themselves (owner, 2026-10-20: the
# page-cache hits had turned ALERT into noise for the second time).
#
# NEW SINCE THE PREVIOUS LINE only (2026-10-19): VmHWM, memory.peak and the
# memory.events counters are cumulative for the whole process/cgroup life, so
# testing them directly kept every line an ALERT for hours after one real event
# and buried the next one. State (pid + last hwm / max / oom_kill) is kept in
# STATE_FILE; a different pid (restart) starts a fresh baseline.
ALERT_HWM_MB="${HUB_MEM_ALERT_HWM_MB:-400}"
ALERT_ANON_MB="${HUB_MEM_ALERT_ANON_MB:-400}"
anon=$(awk '$1 == "anon" {print $2}' "$CG/memory.stat" 2>/dev/null)
anon_mb=$(( ${anon:-0} / 1048576 ))
STATE_FILE="${HUB_MEM_STATE:-/var/lib/hub-mem/state}"
prev_pid=""; prev_hwm=0; prev_max=0; prev_oom=0
[ -r "$STATE_FILE" ] && read -r prev_pid prev_hwm prev_max prev_oom < "$STATE_FILE"
if [ "$prev_pid" != "$PID" ]; then prev_hwm=0; prev_max=0; prev_oom=0; fi
new_max=$(( ${max_hits:-0} - ${prev_max:-0} )); new_oom=$(( ${oom_kills:-0} - ${prev_oom:-0} ))
prio=info; tag=""
if { [ "$hwm_mb" -ge "$ALERT_HWM_MB" ] && [ "$hwm_mb" -gt "${prev_hwm:-0}" ]; } || [ "$anon_mb" -ge "$ALERT_ANON_MB" ] || [ "$new_oom" -gt 0 ]; then
    prio=warning; tag="ALERT "
fi
mkdir -p "$(dirname "$STATE_FILE")" 2>/dev/null && echo "$PID $hwm_mb ${max_hits:-0} ${oom_kills:-0}" > "$STATE_FILE" 2>/dev/null

logger -t hub-mem -p "daemon.$prio" --   "${tag}pid=$PID rss_mb=$(( ${rss:-0} / 1024 )) hwm_mb=$hwm_mb swap_mb=$(( ${swp:-0} / 1024 )) anon_mb=$anon_mb cg_current_mb=$(mb "$cur") cg_peak_mb=$(mb "$peak") events_max=${max_hits:-0} events_oom_kill=${oom_kills:-0} new_max=$new_max new_oom_kill=$new_oom sys_avail_mb=$(( ${avail:-0} / 1024 ))"
exit 0
