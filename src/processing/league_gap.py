"""The "league gap": the stretch between one challenge league ending and the next one opening, when poe.ninja lists
no non-permanent league for a game (`NoActiveLeagueError`). Pure state helpers, no I/O -- `src/main.py` reads and
writes `status.json` around them, `tools/heartbeat_check.py` carries its own small copy of the read side (it is
deliberately independent of the rest of the repo; `tests/test_league_gap.py` pins the two copies together).

Why this exists (2026-11-11..13, PoE1): Allflame ended, Covens had not opened, the prices / meta / skill jobs all
raised "no eligible non-permanent league" for PoE1 -- correct, there was no league -- and that took down every
monitor that treats a failed game as a failed cycle: the Layer-1 `meta` and `skill_meta` pings stopped (the owner got
a "down 1 day 19 hours" mail) and the Layer-2 heartbeat went red for prices, meta and skills of PoE1. The gap is an
expected state with a bounded length, so it gets its own record instead of being read as an outage.

`status.json["league_gap"][game] = {"since", "last_league", "last_checked_at"}`:
- `since` is when the HUB first saw the gap (UTC, ISO), not when the league really ended -- it can only be as early
  as the first job that ran after the end. It is kept across cycles; only `last_checked_at` moves.
- the entry is removed by the skill job (the last of the daily jobs) the first time it gets a league again; the
  slot job removes it when the skill part is not part of the run.
- a gap older than `max_days` is no longer tolerated: Layer 1 stops pinging, Layer 2 goes red, as before.
- a key that is absent means "no gap" everywhere, so an old status.json or an old heartbeat behaves as it always did.

The same module owns the other half of the 2026-11-13 lesson: a game that has just opened its league has no
characters on poe.ninja for a while (Covens' first snapshot, 19:03 UTC, had 0 of them), and that must not read as the
2026-08-04 "silently empty sample" break either -- but only for `launch_grace_hours` after the league's first
snapshot, so a sample that STAYS empty still alerts.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
DEFAULT_MAX_DAYS = 7
DEFAULT_LAUNCH_GRACE_HOURS = 48


@dataclass(frozen=True)
class GapSettings:
    max_days: float = DEFAULT_MAX_DAYS
    launch_grace_hours: float = DEFAULT_LAUNCH_GRACE_HOURS

    @classmethod
    def from_cfg(cls, cfg: Mapping[str, Any]) -> "GapSettings":
        section = cfg.get("league_gap") or {}
        return cls(
            max_days=float(section.get("max_days", DEFAULT_MAX_DAYS)),
            launch_grace_hours=float(section.get("launch_grace_hours", DEFAULT_LAUNCH_GRACE_HOURS)),
        )


def fmt_ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(TS_FORMAT)


def parse_ts(value: Any) -> Optional[datetime]:
    """An ISO `...Z` timestamp as written by `fmt_ts` / the hub's own `created_at`; anything else -> None."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _last_known_league(status: Mapping[str, Any], game: str) -> Optional[str]:
    for section in ("prices", "meta"):
        league = ((status.get(section) or {}).get(game) or {}).get("league")
        if isinstance(league, str) and league:
            return league
    return None


def note_gap(status: dict[str, Any], game: str, now: datetime) -> dict[str, Any]:
    """Records (or refreshes) `game`'s gap in `status` and returns the entry. `since` and `last_league` survive
    from the first observation; only `last_checked_at` moves."""
    gaps = status.get("league_gap")
    if not isinstance(gaps, dict):
        gaps = status["league_gap"] = {}
    entry = gaps.get(game)
    if not isinstance(entry, dict) or parse_ts(entry.get("since")) is None:
        entry = {"since": fmt_ts(now), "last_league": _last_known_league(status, game)}
    entry["last_checked_at"] = fmt_ts(now)
    gaps[game] = entry
    return entry


def clear_gap(status: dict[str, Any], game: str) -> Optional[dict[str, Any]]:
    """Removes `game`'s gap; returns the removed entry (None when there was none). An empty `league_gap` mapping is
    dropped too, so a status with no gap looks exactly like one that never had the key."""
    gaps = status.get("league_gap")
    if not isinstance(gaps, dict) or game not in gaps:
        return None
    removed = gaps.pop(game)
    if not gaps:
        status.pop("league_gap", None)
    return removed if isinstance(removed, dict) else {}


def gap_age_days(entry: Any, now: datetime) -> Optional[float]:
    since = parse_ts((entry or {}).get("since")) if isinstance(entry, dict) else None
    if since is None:
        return None
    return (now - since).total_seconds() / 86400


def classify(
    status: Mapping[str, Any], games: "set[str] | frozenset[str]", max_days: float, now: datetime,
) -> tuple[frozenset[str], dict[str, float]]:
    """Splits `games` (the ones that hit a gap right now) into those whose recorded gap is still tolerated and
    those past `max_days` (game -> age in days). A game with no usable record is tolerated: the record is written
    in the same breath as the gap is seen, so its absence means "just now", never "forever"."""
    tolerated: set[str] = set()
    exceeded: dict[str, float] = {}
    gaps = status.get("league_gap") if isinstance(status.get("league_gap"), dict) else {}
    for game in games:
        age = gap_age_days(gaps.get(game), now)
        if age is not None and age > max_days:
            exceeded[game] = age
        else:
            tolerated.add(game)
    return frozenset(tolerated), exceeded


def in_launch_grace(first_snapshot_at: Any, now: datetime, grace_hours: float) -> bool:
    """True while `now` is within `grace_hours` of the league's first snapshot. An unknown first snapshot is NOT in
    grace (fail closed: the old strict behaviour)."""
    first = parse_ts(first_snapshot_at)
    if first is None:
        return False
    return (now - first).total_seconds() <= grace_hours * 3600
