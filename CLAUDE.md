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
- GGG's API is touched in exactly two places, both cached/refreshed weekly
  via `refresh-stats.yml`: the trade(2) `data/stats` table, and (skill-meta
  phase 1) the trade(2) `data/items` "Gems" list, used only as a
  `verified_tradeable` cross-check for `{game}/gems.json` (see below).
  Everything else — prices, character detail, league list — comes from
  poe.ninja / PoE2 Scout. `pathofexile.com/api/leagues` was investigated for
  league start/end dates but does not support PoE2 (ignores the `realm`
  param entirely) — league start/end dates instead come from the Steam news
  API (`src/fetchers/league_dates.py`), which is not GGG and doesn't touch
  this rule. RePoE fork data (`raw.githubusercontent.com/repoe-fork/*`,
  used for `{game}/gems.json`'s primary source, and separately by
  PoE_Price-Trade_Checker's `build_mod_meta.py`) is community-maintained
  datamine, not a GGG endpoint — not subject to this rule or to the GCP-IP
  block below.
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

- **`char_items.main_skill` was never persisted before skill-meta phase 2
  (added 2026-09-15), even though `CharacterDetail.main_skill` was already
  being fetched and used every meta run** — `_run_meta_for_game` used it
  transiently to compute the coarse `archetype` bucket
  (`Archetyper.classify`), then discarded it; `items_json` only stores
  gear/jewel mods, not the character's skill. Found while designing the new
  `(game, slot, main_skill, level_bracket, stat_id)` counting dimension,
  which needs the *exact* skill name, not just its archetype bucket.
  **Consequence: the raw skill name for every `char_items` row inserted
  before this migration is permanently unrecoverable** — re-fetching a
  historical character from poe.ninja now would only return their *current*
  skill, not what they were running when that historical row was captured
  (players respec), so it would silently mislabel old data rather than
  legitimately backfill it. Decided (explicitly, with the project owner) to
  accept the gap rather than attempt that: the skill dimension starts
  accumulating from this deploy forward only, even though `char_items`
  itself is still fully populated back to the start of PoE1's current
  league (90-day retention). `--reprocess-char-items`
  (`src/main.py`'s `run_reprocess_char_items`) recomputes `skill_mod_counts`
  from stored `char_items` entirely offline (no poe.ninja calls) whenever
  gems.json normalization or `skill_meta.level_brackets` changes, but it can
  only ever act on rows that have `main_skill` populated -- rows from
  before this deploy are silently skipped by design (`LocalDb.
  iter_char_items_with_main_skill` filters `WHERE main_skill IS NOT NULL`),
  not an oversight.
- **`{game}/gems.json` (skill-meta phase 1, added 2026-09-15) needed a
  second `internal/`-staged two-hop publish, same shape as
  `internal/stats_cache*.json` above, for the same IAM reason** — its
  GGG-sourced input (`trade(2)/data/items`) is blocked from GCP IPs, so
  building it has to run in `refresh-stats.yml` via `tools/refresh_gems.py`,
  using the `stats-refresh` service account. That account is
  IAM-restricted to `internal/` only (see above) — it **cannot** write the
  real public `poe2/gems.json` / `poe1/gems.json` paths directly, so
  `refresh_gems.py` stages the finished result at
  `internal/gems_normalized_{game}.json` and `src/main.py`'s
  `run_gems_once` job (VM, full-access credentials, daily,
  `schedule.gems_cron_hour_thailand`) copies it to the public path. Caught
  during implementation, before ever deploying — would otherwise have been
  a silent, permanent, every-single-run publish failure only visible as
  "gems.json never updates" days later, not a loud error.
  Two other things confirmed live the same day, worth keeping in mind if
  this pipeline is touched again:
  - **`trade(2)/data/items`'s "Gems" category does not list transfigured
    gem names at all** (0/217 PoE1 transfigured display names from RePoE
    matched, e.g. no "Ice Nova of Frostbolts") — it's fine as a
    `verified_tradeable` flag, but must never be used to *filter* RePoE's
    gem list, or every transfigured gem would silently vanish.
  - PoE1's raw RePoE `gems.json` is ~**41MB** (full per-level progression
    data) — too large to commit as a test fixture and, more importantly,
    too large to ever hand to the VM (see the VM RAM budget note below):
    `refresh_gems.py` normalizes down to a lean per-gem record *on GitHub's
    runner* and stages only that; `run_gems_once` on the VM only ever does
    a small Firebase-to-Firebase copy, no parsing of the raw file at all.
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
- **The production deploy VM lives under a *different* GCP project than
  Firebase** (learned the hard way 2026-07-18, first time deploying from a
  fresh session): the hub publishes to Firebase Storage under
  `poe-hub-b4723` (`config/config.yaml`'s `firebase.project_id`), but the
  actual GCE instance (`poe-bot-vm`, zone `us-central1-a`, "alongside the
  existing Discord bot" per `deploy/README.md`) is provisioned under
  `poe-discord-bot-501417` instead — `gcloud compute instances list
  --project=poe-hub-b4723` returns nothing (Compute Engine API isn't even
  enabled there). Always target `poe-discord-bot-501417` for
  `gcloud compute ssh`/instance lookups, not the Firebase project. Also:
  the VM's actual `poe-data-hub.service` runs as `User=Winai`, not the
  dedicated system user `deploy/README.md` describes setting up — the real
  deployed setup diverged from that doc at some point; don't assume
  `sudo -u poe-data-hub` works.
- **VM RAM budget (measured 2026-09-10, steady state; the "no OOM risk" reading below was
  overtaken by the 2026-10-15 outage -- see that entry for the current cap and numbers)**: `poe-bot-vm` is an
  **e2-micro (~955MB RAM + 2GB swap)** running *both* the Discord bot and
  poe-data-hub. `free -m`: used ~605MB, available ~349MB, swap already
  ~180MB in use. poe-data-hub baseline RSS ~239MB, peaks to VmHWM ~322MB
  during the prices job's poe1 fetch (~80MB transient spike, the largest
  single-job cost currently). No OOM risk at current load, but **no
  headroom for another heavy long-lived process or a big one-off parse** —
  before adding any memory-heavy code path, check `free -m`
  /`systemctl status poe-data-hub` on the VM first, and prefer
  streaming/chunked processing or (as with `{game}/gems.json`'s ~41MB raw
  PoE1 RePoE source, above) doing the heavy parse somewhere else entirely
  (GitHub Actions) and only ever handing the VM an already-small result.
- **GGG forum's `window.momentTimezone` (used to correctly localize
  `post_date` to UTC in `src/fetchers/news.py`, see SPEC.md 8.4) drifts
  over time on the same source, not just by requester geography** (found
  2026-07-18 during the news-v2 deploy's live verification): the VM
  observed `Asia/Bangkok` during the actual first post-restart job run,
  then `America/Chicago` (stable across 4 repeated checks) about 10
  minutes later from the same VM. The "read the tz name fresh from every
  scraped page, never hardcode it" design handles this correctly per-page,
  but it does mean a `forum-*` news entry's `published` value can shift
  slightly between publish cycles even though the real-world post time
  never changed — expected behavior given the source, not a bug, but
  don't assume `published` is bitwise-stable across snapshots for the same
  id.
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
  - **"Freshness" and "success" are not the same as "the data is real"**
    (learned 2026-09-17, from the ninja_builds schema-break postmortem):
    both layers stayed green the entire ~43-day breakage, because both only
    checked "did the job run and publish something recently" — neither
    checked whether the published *content* was non-empty. The meta job
    "succeeded" and pinged every single cycle while `sample_total` was
    silently 0. Fixed by checking the actual population count directly on
    both layers (`src/main._all_games_got_real_characters` gates the Layer-1
    ping; `tools/heartbeat_check.check_meta_sample_total` checks Layer 2) —
    but the general lesson applies beyond meta: when adding a monitoring
    check for any future job, check a content signal (a count, a nonzero
    field) in addition to timestamp/success, not instead of it.
- **An error message's own wording is not a diagnosis — hit the real
  endpoint yourself before writing down a root cause** (learned 2026-09-16):
  `ninja_builds`'s meta job started failing 100% of the time (every slot,
  both games) starting around 2026-08-04 (`data/samples/last_good_builds_*.bin`'s
  mtimes are the tell), surfacing as `_validate_search_response`'s "missing
  expected valueList(s)" error. The 2026-09-15 phase-17 live-deploy note in
  SPEC.md read that message, saw both leagues were brand new, and wrote down
  "poe.ninja's build index hasn't caught up yet" as the cause — plausible,
  matched the error's own wording ("possibly an empty filter result"), and
  wrong. Actually curling the real endpoint the next day showed `total`
  was a real nonzero count (poe.ninja *does* have the data) while the
  valueList section was simply absent from the wire response — a genuine
  upstream protobuf schema change (confirmed by diffing the raw bytes
  against `last_good_builds_*.bin` field-by-field: old schema was
  field1=total/field5=repeated valueList/field6=repeated dictRef; current
  responses have no field 5 at all, and field 6 now holds something else
  entirely, 7 short entries instead of 9 longer dictRefs). A day of SPEC.md
  history had the wrong cause on record because nobody re-fetched the live
  URL to check. Rule going forward: when a fetcher's validation starts
  failing, curl/fetch the exact failing URL for real before writing any
  "probably X" explanation into SPEC.md or a commit message, even when a
  benign-sounding explanation is sitting right there in the error text.
- **poe.ninja's builds-search response has (at least) two incompatible
  "which passive is this" ID spaces — never resolve one dictionary against
  the other's indices** (learned 2026-09-17, skill-meta phase 3
  investigation): the search response's own "keypassives" column resolves
  through its own small ordinal dictionary (poe.ninja's internal
  enumeration, ~260-330 entries, index e.g. `59`) — completely different
  numbers from `passiveSelection` (character-detail endpoint) or Path of
  Building's own tree `nodes` list, both of which use the *real* GGG
  passive-tree node IDs (thousands to hundred-thousands range, e.g.
  `65091`). Proven with ground truth, not assumed: a character's real
  keystone "Dance with Death" resolves correctly as `keypassive_dict[59]`
  through the search endpoint's own data — but index `59` means nothing
  against `passiveSelection`'s ID space. If a future phase needs to resolve
  `passiveSelection`'s node IDs to names (skill-meta "path B", full tree
  including small nodes), it needs real external GGG tree data — poe.ninja
  never exposes a dictionary for that ID space anywhere in this schema.

- **`ndic.decode`'s split-point search is ambiguous — never accept the first
  match** (found 2026-09-21, before it reached the DB): PoE2's class
  dictionary grew from 30 to 31 entries (`Druid`) and a second split point
  appeared that also passes the "lengths sum to the remaining bytes" check
  and also decodes as UTF-8, so the old first-match rule silently returned 31
  garbled strings (`''`, `'of KitavaSor'`, the length bytes as a name).
  `decode` now evaluates every split point, keeps the single one whose names
  are non-empty and control-character-free, and raises on anything still
  ambiguous. Separately, `ninja_builds._get_dictionary` validates the class
  dictionary against `KNOWN_CLASSES_BY_GAME` and raises
  `DictionaryValidationError` (re-raised by `fetch_all`, not swallowed as a
  per-slot skip) on any unknown name -- a genuinely new class means adding it
  to that set. Note `CharacterRef.char_class` (from this dictionary) was
  unused downstream at the time; `char_items.class` comes from the
  character-detail JSON, so the live DB was never affected.

- **Skill-centric collection (2026-09-21) replaced the per-slot skill
  dimension; a character's skill is the poe.ninja list it was pulled from,
  never a guess.** The per-slot "main skill" was the first gem of poe.ninja's
  skill list -- not a main skill (survey: median cell = 1-2 characters, PoE1
  98% the same characters every run, PoE2 characters carry ~6 "main skills").
  Now: `src/processing/skill_plan.py` (which lists), `skill_counter.py`
  (counting), `skill_collect.py` (checkpointed, resumable run),
  `char_cache.py` (SQLite `char_details` cache shared with the legacy job);
  full design and numbers in SPEC.md 8.10. Things that will bite:
  - **Never read `archetype_hint` as a skill.** It is the old first-gem guess,
    kept only so the legacy per-slot archetype bucketing (`mod_counts` ->
    `meta/latest.json`, still consumed) keeps working. That per-slot job is a
    temporary bridge: retire it and the archetype `meta/latest.json` once PoE
    Checker's mod badge reads per-skill data.
  - **`snapshots.collection_method`**: everything before 2026-09-21 is
    `legacy_slot_top100`, whose `main_skill` columns are unreliable. Archive /
    publish of per-skill data must read only `skill_top100_v1`.
  - **`skill_meta.collect.scheduled` was `false` until 2026-09-22**: a first
    run takes ~8 h (PoE1) / ~10 h (PoE2) at 1 request / 2 s and must never
    start by surprise from a deploy. `--run-meta-once --game <g> --only
    skill` always runs it regardless of the flag. Now `true`, live on its own
    daily cron (`skill_meta`, 05:00 ICT, `schedule.
    skill_meta_cron_hour_thailand`) -- deliberately a SEPARATE
    `scheduler.add_job` from the slot job's `meta` cron (02:00 ICT, now
    always called with `parts=("slot",)` explicitly), not bundled into one
    job, so the two never share a schedule/games-order/healthcheck. The
    skill job always runs `games=["poe1","poe2"]` (PoE1 then PoE2,
    sequential, never parallel -- both peaked >200MB RSS in the first live
    runs on this e2-micro), regardless of `game_versions`' own order (fixed
    2026-09-22: `run_meta_once`'s game iteration now follows an explicit
    `games` list's own order, not always `configured`'s; the "is this a full
    run over every configured game" check that gates both healthcheck pings
    is a set comparison, not list-order-sensitive, for the same reason). Has
    its own healthchecks.io check (`healthchecks.skill_meta_ping_url`,
    recommended Period 24h / Grace 4h), independent of `meta_ping_url` --
    fires only when both games' `characters_distinct > 0` this cycle
    (`_all_games_got_real_skill_characters`, mirrors the slot ping's own
    guard). `tools/heartbeat_check.py` independently checks
    `{game}/skills/index.json`'s freshness (< 30h) and `population_total`
    (> 0) for both games, Layer 2.
  - poe.ninja's index only holds characters of level >= 80 (checked
    2026-09-21) and lists come back level-descending, so ~all data lands in
    the `90-100` bracket. Re-check at each new league's launch whether lower
    levels get indexed.
  - Main and spirit (PoE2) lists overlap by name; `gems.json`'s `gem_type` does
    not identify spirit skills (24 of 450) -- the source of truth is which
    poe.ninja facet a skill came from.
  - **A per-skill list's `dps-<skill>.*`/`rate-*` etc. columns can hold fewer
    entries than there are rows** (99/98/94 of 100 seen live 2026-09-21,
    first PoE1 run) -- characters with no value for that skill are simply
    omitted from those columns, not padded. `_validate_search_response`'s
    per-skill-list call now checks row-alignment only for the columns the
    collector actually reads (`SKILL_LIST_ALIGNED_COLUMNS`), not every column
    in the response; the per-slot fetch keeps the strict all-columns check.
  - **`{game}/passives/latest.json` schema_version bumped 2 -> 3, full shape
    replacement** (2026-09-21): `main_skill`/`archetype`/`rank_in_bracket`
    gone, `skill_type` added, source table changed from `keypassive_counts`
    to `skill_passive_counts`. Any consumer still parsing the old shape needs
    updating -- see the "Deploy ordering for breaking id/schema changes"
    memory ([[feedback-deploy-id-space-ordering]]) if this ever needs
    sequencing with a bot/Checker deploy; this particular change was shipped
    directly on the project owner's explicit instruction, not previously
    coordinated with those repos.
  - **`{game}/skills/index.json` never drops a skill by population size** --
    only `skill_meta.publish.min_usage_pct` drops individual mod/passive rows
    inside a skill's own file. A thin skill (2-8 characters) still gets an
    index entry and a file with its real population shown, so a consumer can
    judge how much to trust its numbers (project owner's explicit ask).
  - `{game}/skills/dictionary.json` exists specifically so `stat_id ->
    template` isn't duplicated into 150+ per-skill files (~1.5MB each for
    PoE1 otherwise).
- **A read-only ad-hoc query against the live production SQLite file can
  crash a real job with "database is locked" -- `job_lock` does not protect
  against this** (learned 2026-09-22, first live incremental skill run):
  while PoE2's skill run was mid-`save_skill_list_members` (list 175/417), a
  `dbstat` diagnostic sweep run over SSH in a completely separate
  `sqlite3.connect()` (investigating the skill-centric tables' per-run disk
  growth) held a read lock over the then-1.5GB `char_items` table long enough
  that the job's write blew past Python sqlite3's *default* 5s busy timeout
  and raised `OperationalError: database is locked`, killing that attempt
  (cleanly -- `skill_run_lists`/`skill_run_members` checkpoints for the 175
  already-saved lists were intact, so the very next `--only skill` invocation
  resumed from list 176, no lost work). `job_lock` only serializes this
  project's *own* jobs against each other; it was never in the loop for a
  manual diagnostic script. Fixed at the connection level, not by avoiding
  concurrent reads: `LocalDb.__init__` now opens with `timeout=30` and sets
  `PRAGMA journal_mode=WAL` (persists in the db file itself, so this
  converted the production file to WAL permanently on first connect after
  deploy) + `PRAGMA busy_timeout=30000` -- WAL lets a writer's commit and a
  concurrent reader coexist instead of blocking each other, which is the real
  fix; the longer timeout is only a second line of defense for a reader that
  somehow isn't WAL-aware. **Still worth avoiding**, even with this fix: a
  long, unbounded full-table scan (`SELECT ... FROM dbstat`, or any query
  touching all of a multi-GB table like `char_items`) against the live
  production file while a job may be writing -- prefer `mode=ro` (already
  this project's convention for ad-hoc VM queries, see
  [[reference-vm-sqlite-readonly-access]]) and keep such queries narrow/
  indexed where the DB is large.

- **The 2026-10-15 outage was one job's RAM spike plus a network daemon that
  never recovered -- not a leak, and not "the VM died"** (learned 2026-10-18;
  ~72h of no publishing, every pipeline at once). Timeline (UTC): the weekly
  `archive` job fired at 07:50:11 -- exactly 7 days after the hub process
  started -- and `build_league_archive` pulled a whole league's stat tables
  into Python lists; the hub went to 711MB RSS + 1.9GB in swap (2.6GB) on a
  955MB VM, the box thrashed from ~07:54, `systemd-networkd` timed out on a
  DHCP renewal (`ens4: Failed`, 08:31), and the OOM killer finally took the hub
  at 09:27. The kernel and journald kept running; the VM simply had no network
  until a manual reset on 2026-10-18. The first OOM kill on 2026-10-08 07:48
  has the same 7-day signature, but the journal did not reach back far enough
  to prove it. Lessons:
  - **`IntervalTrigger(weeks=1)` was the wrong design for the archive job**:
    an interval's first fire is "process start + the interval", so every
    restart silently moved it and the crash landed at a time nobody chose. Use
    a fixed `CronTrigger` (now Sunday 13:00 ICT; 04:00 collides with cleanup).
  - **`build_league_archive` / `export_league_archive` load an entire league
    into RAM** (`iter_*_for_league` return `list[tuple]`, then nested dicts)
    and its "cheap, reads local SQLite only" docstring stopped being true once
    the DB reached ~5GB. It stays **disabled** (`archive.enabled: false`, both
    the weekly job and the league-change export in the meta jobs) until it is
    rewritten to aggregate in SQL and stream in chunks, and its RSS is measured
    against the real DB read-only before it is turned back on. While off:
    every skipped cycle logs a WARNING, `status.json` carries
    `archive.enabled` / `last_skipped_at` / `unarchived_league_changes`, and an
    `ARCHIVE GAP` warning repeats every cycle for each league that changed
    while it was off. **Deadline:** such a league's raw `skill_stat_counts`
    rows are deleted by retention after 45 days -- permanent loss.
    `--export-league-archive` is NOT guarded and will OOM the VM.
  - **Do not read a one-shot spike as a slow leak**: the first analysis here
    claimed ~350MB/day linear growth. `sar -r -f /var/log/sysstat/saDD` (10-min
    history, sysstat is installed) showed RAM flat at 375-530MB for the five
    days before, then a jump inside ~100 minutes. Check the history before
    saying "grows".
  - **`MemoryMax` is derived from the kernel's OOM table, never from systemd's
    "memory peak"**: for the killed hub the kernel table said 177,928 anon
    pages + 488,964 swap entries (the swap entries of all tasks summed to the
    whole swapfile), while systemd's own accounting line said "538.8M peak,
    273.7M swap peak" -- the two disagree and are unexplained. Cap =
    955MB - OS ~250 - bot peak 65 - report-service/caddy peaks 74 - margin 100
    = ~460MB (`poe-data-hub.service.d/10-memory.conf`: `MemoryMax=460M`,
    `MemorySwapMax=0`, `OOMScoreAdjust=300`; bot `-500`). Swap is off for the
    hub on purpose: it turned a clean OOM kill into an hour-long thrash. If a
    job needs more than the cap it cannot run on this box -- move it, don't
    stretch the cap.
  - **`deploy/hub-mem-log/`** (systemd timer, plain bash) writes one journal
    line per 10 minutes with the hub's RSS/peak/swap and its cgroup counters
    (`journalctl -t hub-mem`). Lines are logged at WARNING with an `ALERT`
    prefix when VmHWM reaches 400MB (a margin under 15% on the cap) or
    `memory.events` shows a cap hit / OOM kill: `journalctl -t hub-mem -p
    warning`. A danger sign is reported even if the cap never bit; the cap is
    never raised on the strength of one run.
  - **`deploy/net-watchdog/`** (systemd timer every 2 min, plain bash -- it must
    run when RAM is short) checks global IPv4 address + default route + TCP to
    the metadata server + DNS and escalates: `networkctl reconfigure <if>` on
    the 2nd consecutive failure, restart networkd/resolved on the 4th,
    guest-agent on the 8th, CRITICAL log after that; it never reboots the VM.
    `ens4` is managed by systemd-networkd (netplan). **Live-tested
    2026-10-18** by flushing ens4's address: recovered by the 2nd-check
    `reconfigure` in 221s with no service restarted. **The 4th-check restart
    path and the real `ens4: Failed` state have NOT been proven live** (only
    dry-run); do not claim them as tested.
  - **Every log under `data/` needs rotation.** `hub.log` rotated daily, but
    `fetch.log` (189MB) and `unmapped_mods*.log` (279MB + 807MB, 10M lines)
    were bare appends, and journald had grown to 2.7GB (one WARNING per
    unmapped-mod occurrence: ~260k lines in one hour). Now:
    `src/utils/log_rotation.py` (50MB, 2 backups) for `fetch.log` and the
    unmapped logs, the normalizer logs/writes each distinct unmapped mod once
    per run plus a one-line summary, and journald is capped (400M). Each
    distinct unmapped mod therefore shows up once per run in
    `data/unmapped_mods*.log`.
  - **`status.json running_jobs` entries were never cleared after a crash or
    reset** (`_mark_job_finished` does not run); `run_scheduler` now drops
    entries whose pid is dead at startup.
- **No fallback for PoE2 prices (risk, 2026-10-18)**: `poe2scout.com/api/poe2/Leagues`
  now returns the site's HTML front page with HTTP 200 (confirmed with curl from
  a dev machine and from the VM), so `ScoutPricesFetcher` fails every cycle with
  `Expecting value: line 1 column 1` (first seen in the journal on 2026-10-08;
  it still worked on 2026-07-11). PoE2 prices are poe.ninja-only until a new
  Scout API path is found; if poe.ninja breaks, there is nothing to fall back
  to. Not investigated further yet.

- **Normalizer facts learned 2026-10-18/19 (replay on real characters + RePoE as
  ground truth)** -- see `project_normalizer_unmapped_gaps` memory for the
  step-by-step history:
  - **The GGG table has duplicate texts: 262 texts (PoE1) are carried by more
    than one stat id in the same kind, and `_build_index` keeps the LAST one
    silently** (`bucket[text] = id`). 4.03% of mapped occurrences go through
    such a text. RePoE says 3,430 occurrences are genuinely ambiguous (both
    stats render the same text), so neither choice is right for them. The
    Checker's `ModDatabase._typed_index` is also last-wins, so the two agree
    today -- that is luck of the same rule, not a shared code path.
  - **RePoE `format` says whether the game prints a leading `+`**
    (`additional_spell_block_%` uses `+#`, `base_spell_block_%` plain `#`), so
    the order in which mapping rules try "with sign" / "without sign" must be
    decided by the item text's explicit `+`, never guessed
    (`_EXPLICIT_PLUS_RE`; 22 texts / 286 occurrences changed).
  - **Deprecated `old_` / `_old_do_not_use_` stats**: 799 of the 801
    occurrences first reported were on trade ids shared with a current stat;
    only 2 sat on a deprecated-only id. An id is deprecated only if NO current
    entry carries it.
  - **Rendering aliases** (`src/processing/rendering_aliases.py`,
    `tools/refresh_rendering_aliases.py`, runs in `refresh-stats.yml`, staged
    at `internal/rendering_aliases_{game}.json` + `.last_good.json`): wordings
    that RePoE proves the game renders for exactly one GGG stat. Only three
    implied-value shapes are accepted (`[v..v]`, `[lo..]` with a sibling max
    of lo-1, sign-only mirrors); `[None..None]` and anything ambiguous stays
    UNMAPPED. Unit handlers matter (`every second` = 1, not 1000). The
    normalizer reads it last (Firebase -> disk cache -> none) and never fails
    without it. **Deploy order: push, then `gh workflow run refresh-stats.yml`
    and verify both `internal/rendering_aliases_*.json` blobs exist, THEN
    restart the VM** -- otherwise the first run silently has no aliases.
  - Alias gaps that are real, not bugs: `Impale Enemies on Hit with Attacks`
    (two trade ids, 725880290 and 3739863694, identical table text);
    `50% of Chaos Damage taken does not bypass Energy Shield` (ids 1119465199
    and 1865744989, identical text); `Projectiles Pierce an additional Target`
    (one trade id shared by four internal stats whose stored values differ).
  - **PoE2 has no real mod weights** (only 0/1) and no `essences.json` /
    `crafting_bench_options`. Weight-based features are PoE1-only; no schema
    may assume PoE2 weights. When RePoE `mods.json` is used, the `HiddenItem`
    class (1,307 tiers) must be filtered out. RePoE lag: PoE2 2-7 days, PoE1
    ~0.4 days (measure from "Export" commits, not "Version").

- **This repo is public: secrets and third-party data must never be committed** (audit 2026-10-19).
  healthchecks.io ping URLs are secrets (anyone holding one can turn a check green while the system is
  dead) and are read from env vars named in `config.healthchecks.*_ping_url_env`
  (`src/utils/config.py`), supplied on the VM by `EnvironmentFile=-/etc/poe-data-hub/healthchecks.env`
  (mode 600, outside the repo). **The old UUIDs are still in git history (3 commits of config.yaml)** and must
  be treated as leaked: the owner is re-creating all four checks on healthchecks.io (verify that the VM env file holds the NEW URLs before relying on pings). The User-Agent carries no personal
  email (`build_user_agent`: `HUB_CONTACT` env, default the project URL). `data/samples/` is tracked on
  purpose (fixtures), so it is NOT gitignored; instead `tests/test_samples_no_player_names.py` fails when a
  sample holds a real account/character name -- run `python -m tools.scrub_samples` (same-byte-length
  pseudonyms, structural protobuf rewrite) before committing any new poe.ninja capture. **Real player names
  from before the scrub remain in git history** (64 commits; rewriting was judged not worth it), and public
  GGG forum HTML samples contain forum usernames. The git author address and the GCP project / VM names are
  knowingly public.

- **Skill-run duration is dominated by the poe.ninja character-cache hit rate; never quote one
  number as "the" duration** (2026-10-19): the 2026-09-22 measurement of ~71 min for both games
  was a mostly-cached run. The 2026-10-18 22:00 UTC run took 88 min for PoE1 (2,269 characters
  fetched, 12,391 reused) but **5.95 h for PoE2** (10,035 of 16,820 fetched new, 6,777 reused;
  ETAs printed at the start read 4.4-4.7 h and were low too). At 1 request / 2 s the time is
  roughly `new characters x 2 s`; estimate from the progress line's fetched/reused split, not from
  an earlier run.
- **A skill publish was a memory spike, not a leak -- fixed 2026-10-19** (measured with
  `tools/measure_skill_publish.py`, read-only DB, discarding publisher, under a transient cap):
  `publish_skill_meta` did `fetchall()` on every `skill_stat_counts` row of the snapshot (PoE1
  216k rows, PoE2 340k) and then built a dict per row, both alive until the function returned:
  +128MB + 60MB for PoE1 (~190MB), hub RSS peak 332MB (PoE1) / 439MB (PoE2) of the 460MB cap,
  cgroup `max` events 3,941, no OOM. It was NOT "432 files at once" (serializing a file costs
  ~nothing). Now the groups come from the small populations/passives rows and each skill's mods
  are streamed one skill at a time (`iter_skill_mods`, a lazy cursor ordered by `id`, which is
  contiguous per skill because `replace_skill_counts_for_snapshot` inserts a skill in one
  executemany; non-contiguity raises and falls back to the buffered path). Measured after: PoE1
  +12MB, PoE2 +19MB over the process baseline, 7-9 s for the whole read+build. Output is
  byte-identical: `tests/test_skill_publish_streaming.py` compares against a verbatim copy of the
  old code (a mutation check proves it can fail), and all 597 files of the live PoE1/PoE2
  publish matched the files the old code had published (ignoring `generated_at`).
  **Don't "fix" this with one query per skill**: the table has 13.7M rows over 51 snapshots and
  the only usable index is `(snapshot_id)`, so 432 per-skill queries would each rescan the
  snapshot's 340k rows (~30 min instead of 4.5 s); a new index on a 13.7M-row table is not free
  either. The same fetchall-the-whole-snapshot shape still exists in `archive_export.py` (disabled)
  and must use this streaming approach when rewritten. `{game}/skills/` also keeps stale files of
  skills that dropped out of a later snapshot (5 PoE1 / 9 PoE2 on 2026-10-19; nothing deletes
  them and they are not in `index.json`). Open follow-ups, not done: RSS stays high after a big
  allocation because glibc does not return freed arenas (not a leak; try `malloc_trim` only if the
  baseline still sticks after this fix); prices' memory was NOT re-measured, the older numbers are
  not a confirmation.

- **Normalizer A-D + rendering aliases went live 2026-10-19 09:01 UTC** (commit `00e8b62`, deploy order:
  push -> `gh workflow run refresh-stats.yml` -> verify `internal/rendering_aliases_*.json` ->
  restart -> recompute slot one game at a time via `tools/run_capped.sh`). Production
  `unmapped_mod_pct` on the SAME characters (1,333 / 1,214 before, 1,332 / 1,215 after): PoE1
  9.75% -> 0.69%, PoE2 1.92% -> 0.77%. The replay-scaled targets (0.78 / 1.05) were beaten: the
  replay corpus under-predicts the gain, so do not treat replay percentages as production
  forecasts. No stat's count dropped except through intended id changes (the explicit-plus rule
  moves e.g. weapon "+#% Chance to Block Spell Damage" from stat 561307714 to 19803471 with the
  same count). Rank effect: PoE1 body top 10 gained two "(Local)" ES rows at ranks 5-6; PoE2 top
  10s unchanged; PoE1 `jewel:cluster` rows 75 -> 167 (distinct stats 28 -> 69, published
  count>=5 filter applies). Local/global (global stat with a "(Local)" twin), replay,
  occurrence-weighted: PoE1 7.49% -> 6.47%, PoE2 6.83% -> 6.73%. Test lines tagged `hub-mem`
  at 2026-10-19 08:36-08:37 UTC in the journal are from testing the new ALERT logic, not real
  events. **Consumers of `{game}/skills/` must read `index.json` and never list the folder**:
  old skill files of skills that left a snapshot stay in Firebase (not deleted) and hold stale data.

- **Which "percent" is this? The metrics used in the normalizer work are NOT interchangeable**
  (2026-10-19: two readings were nearly mixed up). Always say which one a number is:
  1. **Production `unmapped_mod_pct`** (`status.json` -> `meta/{game}/meta_job_summary`, the slot
     job): unmapped / `mods_processed`, counted over the (character, slot) pairs that poe.ninja
     LISTED for the slot lists (PoE1 `sample_total` ~2,180: top 100 per slot, weapon ~1,080).
     Use for: deploy verification, the monitoring history (`meta_unmapped_history`).
  2. **Replay `unmapped_pct`** (`logs/.../replay/result_*.json` from the scratchpad `replay.py`):
     every slot of every stored character (PoE1 19,468 slot-chars from 1,330 characters), so
     jewels/offhands/flasks weigh ~9x more than in production. Use for: A/B of two normalizer
     versions on one fixed corpus (regressions, newly mapped texts), direction only. **Never
     quote it as a production forecast.**
  3. **Replay re-weighted to production's `sample.per_slot`** (rows x production slot sizes):
     predicted PoE1 after-deploy 0.68% (real 0.69%), PoE2 0.94% (real 0.77%) -- but it did NOT
     reproduce the "before" values (PoE1 16.7% vs real 9.75%, PoE2 2.24% vs 1.92%), so it is
     good for the direction and roughly for the size, not a promise. The plain replay predicted
     1.18 / 1.69 and the ratio-scaled targets were 0.78 / 1.05. When setting a target, first
     check the method reproduces the known BEFORE value; if it cannot, say the target is rough.
  4. **Local/global share** (7.49% PoE1 / 6.83% PoE2 before, 6.47% / 6.73% after): replay,
     weighted by occurrences of a mapped text. The same quantity on the published meta is 16.5%
     / 10.9% because `count` there is "characters holding the stat". Different weights, different
     question; do not compare across them.
  5. **Cluster-jewel stats**: published `meta/latest.json`, archetype `all`, count>=5 filter:
     rows 75 -> 167, distinct stats 28 -> 69. The replay-without-filter figure (88 -> 383) is
     not the same thing.
  6. **Checker-vs-hub gap** (14.2% -> 0.58% "label lost", 2.36% -> 2.93% "different id"):
     replay corpus, occurrence-weighted, so inherits all of 2's caveats.

- **`{game}/aliases/latest.json` is a PUBLIC contract (2026-10-19, SPEC.md 8.12) -- treat its shape like
  `meta/latest.json`.** Built in Actions (`tools/refresh_rendering_aliases.py` stages
  `internal/aliases_public_{game}.json`), copied by the VM's `run_aliases_once` (24h cache, like
  `dictionary.json`). It has readable field names on purpose; the hub-private
  `internal/rendering_aliases_{game}.json` keeps its compact names that the RUNNING hub reads, so never
  rename those without a coordinated deploy (an unreadable alias is skipped silently and the index would
  be empty -- the same silent-failure shape as the 2026-10-17 meta break). Any contract change needs a
  `schema_version` bump and `python -m tools.build_alias_contract_fixture` rerun (the 49 cases in
  `tests/fixtures/alias_contract/` are verified against the real table, the hub chain AND the independent
  `PublicAliasReader`). The file is only the LAST rule of the normalizer chain; a consumer implementing
  only it disagrees with the hub. **`PricesPublisher.fetch_previous` falls back to a local copy named by
  BASENAME, so every `*/latest.json` shares one fallback file** -- ask the bucket
  (`blob.exists()`), not `fetch_previous`, when a test or job must know whether a path exists.
  **Known gaps (not built):** no Layer-2 heartbeat check of the public alias file (freshness, non-empty);
  nothing yet forces the PoE Checker's port of normalizer rules 1-5 (`stat_match.ts`) to follow a change to
  `normalizer.py` -- today that depends on a person remembering to tell Winai (SPEC.md 8.12).

- **`unmapped_mod_pct` (9.75% -> 0.69% PoE1, 1.92% -> 0.77% PoE2) covers only the mod lists the hub READS --
  never quote it as coverage of the mods on an item** (found 2026-10-20). `ninja_chars` reads only
  `implicitMods` / `explicitMods` / `fracturedMods` of poe.ninja's itemData, and `KNOWN_MOD_KINDS` has the same
  three kinds. The item data also carries `runeMods`, `desecratedMods`, `craftedMods`, `enchantMods`,
  `utilityMods`, `bondedMods`, `mutatedMods`, `vestigialMods`, none of which is collected. Sample of 40
  top-ring characters per game (live, 2026-10-20; biased toward strong characters): **PoE2 40.3% of 5,578 mod
  lines are never read** (rune 16.9, desecrated 9.6, crafted 6.6, enchant 4.3, utility 2.0, bonded 0.6,
  mutated 0.4), **PoE1 19.9% of 5,678** (enchant 11.9, crafted 3.7, utility 3.4, mutated 0.8). Some of those
  lines are not statistical mods at all (`Allocates X`, flask utility lines). The same trap as replay vs
  production and 7.5% vs 16.5%: a percentage is only meaningful together with its denominator.

- **Disk: the database does NOT grow linearly forever -- it saturates, and VACUUM needs as much free space as the
  database is big** (measured 2026-10-20; the old "~107MB/day, ~100 days of disk left" mixed a transient with a
  steady state). Disk 30.1GB, 15.6GB used, **14.4GB free**; the DB file is 5.43GB, the rest of the used space
  (~10.2GB: OS, logs, `data/cache` 0.94GB, rotated logs) is roughly constant. Per table (rows from `COUNT(*)`,
  bytes/row estimated from sampled column lengths + index entries, calibrated: the model sums to 5.25GB against the
  real 5.43GB, so treat each figure as +-10%): `skill_stat_counts` 14.4M rows x ~190B = 2.7GB, retention 45 days,
  556k rows/day (~106MB/day -- THIS is the growth), oldest snapshot 2026-09-21, so it keeps growing until ~2026-11-05
  and then stops; `char_items` 207k rows x ~8.9KB = 1.85GB, retention 90 days, 2.5k rows/day, already at its plateau
  (oldest snapshot 2026-07-22); `char_details` 0.44GB (30 days, at its plateau); the legacy `mod_counts` etc. ~0.2GB
  growing ~1MB/day. **Plateau without any change: DB ~7.5GB around 2026-11-05, then ~1MB/day**: 12.4GB free.
  Scenarios (estimates, same +-10%): enabling only PoE2 `rune` mods -> ~8.1GB, 11.7GB free, VACUUM margin ~3.6GB;
  enabling all four kinds (rune, desecrated, crafted, enchant; PoE1 crafted, enchant) -> ~9.9GB, ~10.0GB free, VACUUM
  (needs ~9.9GB) has NO margin -- would need a bigger disk (online persistent-disk resize) or shorter retention first.
  A full `dbstat` sweep of this DB on the e2-micro did not finish in 520 s (aborted, harmless on WAL but do not repeat);
  use `COUNT(*)` per table and sampled `LENGTH()` instead.

- **GGG answered 403 to ONE GitHub runner on 2026-10-19 10:47 UTC and it was not reproduced** (investigated
  2026-10-20; conclusion: transient, cause unknown). `refresh-stats.yml` runs: 08:21 and 08:56 succeeded, 10:47 failed on
  both `trade/data/stats` and `trade2/data/stats` (the stale Firebase cache stayed; the gems and alias steps still ran).
  A temporary probe (one honest request per endpoint, same library and User-Agent as the real refresh, runner IP logged)
  was run 3 times by hand on 2026-10-20 -- 04:09, 07:27, 08:37 UTC, from three different Microsoft IPs (San Jose,
  Phoenix, Des Moines) -- and got 200/200 every time. Ruled out: the User-Agent change (the same new UA succeeded at 08:56
  and failed at 10:47); our own request volume (4 requests in the 2.5 h before the failure). NOT ruled out: a short block
  or rate limit of that runner's shared IP. Caveat: every success was a Cloudflare `cf-cache-status: HIT` in ~0.1 s, i.e.
  possibly served from the edge cache without touching GGG's origin, so the probes show the edge answers, not that GGG
  accepts us. No workaround exists or is to be built (no User-Agent tricks, proxies, IP switching): if it recurs, accept the
  stale cache and report. Since commit `2140206` a failed fetch logs the status, `server`, `cf-ray`, `cf-mitigated`,
  `retry-after`, content-type and the first 300 characters of the body, so the next occurrence is diagnosable from the log
  alone. The earlier claim "GitHub Actions runners were confirmed reachable (3/3)" holds only as "usually reachable".
  GitHub's `schedule:` trigger on a newly added workflow did not fire for 4+ hours: do not rely on it for anything urgent.

## Layout

- `src/fetchers/` — one module per external data source
- `src/processing/` — normalization/aggregation (mod meta pipeline, later phase)
- `src/storage/` — SQLite + Firebase publisher
- `src/utils/` — http_client, rate_limiter
- `config/config.yaml` — all tunables; no hardcoded league names anywhere
