#!/bin/bash
# hub-mem-log.sh -- one journal line per run with poe-data-hub's real memory:
# the process's own RSS/peak/swap (what the kernel's OOM table reports) AND
# the cgroup's counters (what MemoryMax enforces), so a cap hit is visible
# and RSS growth can be measured instead of guessed (2026-10-15 postmortem:
# a "7-day leak" turned out to be a one-shot spike that nothing recorded).
#
#   journalctl -t hub-mem --since today
#
# cg_peak includes reclaimable page cache, so it can read near MemoryMax
# without the process being short of memory; `max`/`oom_kill` in
# memory.events are the real signal that the cap bit.
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

logger -t hub-mem -p daemon.info -- \
  "pid=$PID rss_mb=$(( ${rss:-0} / 1024 )) hwm_mb=$(( ${hwm:-0} / 1024 )) swap_mb=$(( ${swp:-0} / 1024 )) cg_current_mb=$(mb "$cur") cg_peak_mb=$(mb "$peak") events_max=$(ev max) events_oom_kill=$(ev oom_kill) sys_avail_mb=$(( ${avail:-0} / 1024 ))"
exit 0
