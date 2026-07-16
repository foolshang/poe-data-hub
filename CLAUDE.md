# poe-data-hub

Central data hub for the PoE ecosystem. Fetches prices (poe.ninja + PoE2 Scout)
and build meta (poe.ninja builds pages) and publishes normalized JSON to
Firebase Storage. Discord bot and PoE Checker read from Firebase only — they do
not hit poe.ninja/scout/GGG directly once this is live.

Full spec: see project owner for `SPEC: poe-data-hub v2`.

## Rules

- All outbound HTTP goes through `src/utils/http_client.py` — never call
  `requests` directly from a fetcher. This is what enforces rate limiting,
  retry/backoff, and the structured request log (`data/fetch.log`).
- GGG's API is touched in exactly one place: the trade2 `data/stats` table
  (cached, refreshed weekly). Everything else — prices, character detail,
  league list — comes from poe.ninja / PoE2 Scout. `pathofexile.com/api/leagues`
  was investigated for league start/end dates but does not support PoE2
  (ignores the `realm` param entirely) — league start/end dates instead come
  from the Steam news API (`src/fetchers/league_dates.py`), which is not GGG
  and doesn't touch this rule.
- Every published JSON file must match its schema in the spec exactly
  (`schema_version` field bumps on any breaking change). Additive optional
  fields are fine without a bump.
- No price field is ever silently dropped — every price entry carries
  `value` + `value_currency` + `divine_value`.
- Fetchers must validate responses before use (spec 10.1b) and never crash
  the job on a bad/empty response — log + stale-while-error instead.
- **GGG's `trade2/data/stats` (PoE2) and `trade/data/stats` (PoE1) both
  return a persistent 403 from cloud-provider IPs** (trade2 confirmed
  2026-07-09; trade confirmed 2026-07-15 the hard way — the first live
  PoE1 meta run on the deploy VM failed outright with this 403, see below).
  Works fine from a residential/dev IP, blocked identically regardless of
  User-Agent. **Never add a code path that calls either endpoint directly
  from the deploy VM** — it will fail every time, permanently, not
  intermittently. GitHub Actions runners were confirmed reachable instead
  (3/3 test runs, 3 different IPs, 200 OK — see git history for
  `.github/workflows/test-ggg-reachability.yml`, since deleted), so
  `.github/workflows/refresh-stats.yml` (weekly cron + manual trigger) runs
  `tools/refresh_stats.py` from there and publishes to Firebase Storage at
  `internal/stats_cache.json` (PoE2) / `internal/stats_cache_poe1.json`
  (PoE1). **This Firebase copy is now the primary source of the stats
  table for every environment** — `ModNormalizer.load()`'s order is
  Firebase -> direct GGG fetch -> stale on-disk cache (see
  `src/processing/normalizer.py` docstring). The GitHub Actions workflow
  uses its own narrowly-scoped service account
  (`stats-refresh@poe-hub-b4723.iam.gserviceaccount.com`, IAM-condition
  restricted to the `internal/` prefix only) — deliberately not the same
  key as the VM's `service-account.json`, so a compromised CI secret can't
  touch `poe2/prices` or `poe2/meta`.
  **Deploy gotcha (learned 2026-07-15, PoE1 meta's first-ever VM run):**
  adding a *new* game's stats-cache Firebase path only helps once
  `refresh-stats.yml` has actually run *after* that code merged — the
  existing weekly cron schedule doesn't retroactively backfill a path that
  didn't exist yet, and there's no on-disk cache either the very first time
  a new game's meta job runs on a given VM. Symptom: `ModNormalizer.load()`
  falls through Firebase (404, nothing there yet) -> direct GGG (403,
  blocked) -> disk cache (nothing there yet either) -> raises, and that
  game's whole meta run fails for that cycle (caught by `run_meta_once`'s
  per-game try/except, so it doesn't take the other game down, but that
  game publishes nothing that cycle). **Fix**: manually trigger
  `refresh-stats.yml` (`gh workflow run refresh-stats.yml`) right after
  deploying any change that adds/touches a stats-cache path, don't wait for
  the weekly cron, and don't assume "it's routed through a safe path" means
  "it's already populated" — verify the actual blob exists on Firebase
  before trusting a new game's meta job to succeed on the VM.

- **The meta job must never run twice concurrently — enforced by a
  filesystem lock, not just discipline** (learned the hard way 2026-07-15):
  a manually launched `--run-meta-once` retry collided with the already-
  running scheduled 2am-Thailand meta cron (running as a thread inside the
  long-lived `--serve` daemon) — both hammered poe.ninja independently, each
  with its own isolated `RateLimiter` instance unaware of the other, and
  neither backed off from the other's `429`s. poe.ninja's server-side
  throttling escalated and both runs stalled for 2+ hours before this was
  even noticed (root-caused by checking `data/fetch.log` for `poe.ninja`
  entries specifically — the shared log file is also written by the
  always-running prices/news jobs, so filtering by URL pattern, not just
  tailing the file, was required to see what was actually stuck). Fixed
  three ways, all in the same commit:
  1. `src/utils/job_lock.py` — a cross-process lock (`data/locks/meta.lock`,
     pid + start time, atomic `O_CREAT|O_EXCL`, stale dead-pid locks
     self-clear). `run_meta_once` acquires it before doing any work; since
     both the scheduler and the manual CLI call that same function, a
     second concurrent attempt from *either* source now skips cleanly
     (`meta job: skip -- previous run still active`) instead of colliding.
     **On Windows dev machines, `_pid_alive` must never use
     `os.kill(pid, 0)`** — Windows implements any non-CTRL_EVENT signal via
     `TerminateProcess()`, so a "liveness check" would actually kill the
     target process; it uses `OpenProcess`/`CloseHandle` via `ctypes`
     instead. POSIX (the only prod target) keeps `os.kill(pid, 0)`.
  2. `RateLimiter.note_retry_after(host, seconds)` — when an HTTP 429
     carries a `Retry-After` header, that pause is now broadcast to every
     other caller sharing the *same* `RateLimiter` instance, so concurrent
     jobs back off together instead of independently retrying into a
     longer ban. Only takes effect because of fix 3.
  3. `src/main._build_http_client` now returns one `RateLimiter` shared by
     every job in the process (`_get_shared_rate_limiter`), not a fresh
     instance per job call — a prerequisite for fix 2 to actually reach
     concurrently-running jobs. Tests reset this module-level singleton
     between tests via an autouse fixture in `tests/conftest.py`.
  Also: `status.json` gained `running_jobs.{job_name}` (pid + started_at,
  set by `_mark_job_running`/`_mark_job_finished` around the meta job) so a
  human can check what's currently running before launching a manual CLI
  retry, instead of SSHing in to guess from `ps`/journalctl output — which
  is what led to the collision in the first place.
- **Monitoring is two independent layers on purpose — never collapse them
  into one** (added 2026-07-11, after a ~23h silent outage where the VM
  service was stopped for manual testing and nobody noticed until asked):
  - **Layer 1** (`src/main._ping_healthcheck`, config `healthchecks.*_ping_url`):
    the hub pings healthchecks.io itself, but only *after* a job's Firebase
    publish + readback-verify has already succeeded — not just "the job
    ran". Watches from the inside; catches everything (process dead, VM
    dead, network down, job hung) *except* "the hub is alive and pinging
    but somehow publishing stale/wrong data".
  - **Layer 2** (`tools/heartbeat_check.py`, `.github/workflows/heartbeat.yml`,
    hourly): a completely different provider (GitHub Actions, not
    healthchecks.io) reads the actual public `prices/latest.json` and
    `status.json` from the outside and checks their timestamps are recent.
    Catches "Layer 1 says fine but the data is actually stale" and serves
    as a backup if healthchecks.io itself has an outage.
  - Ping/check failures in either layer must never fail the underlying job
    (Layer 1) or need secrets (Layer 2 reads public URLs only).

## Layout

- `src/fetchers/` — one module per external data source
- `src/processing/` — normalization/aggregation (mod meta pipeline, later phase)
- `src/storage/` — SQLite + Firebase publisher
- `src/utils/` — http_client, rate_limiter
- `config/config.yaml` — all tunables; no hardcoded league names anywhere
