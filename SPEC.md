# SPEC: poe-data-hub v2

Source of truth for JSON contracts published to Firebase Storage. Consumers
(Discord bot, PoE Checker) must treat this file — not chat history — as the
canonical schema reference. Update it in the same change that changes a
publisher's output shape.

## 1. เป้าหมาย

สร้าง data hub ตัวเดียวที่:

1. ดึงข้อมูลราคา จาก poe.ninja และ PoE2 Scout → publish เป็น `prices/latest.json`
2. ดึงข้อมูล mod ของ item ที่ผู้เล่นใช้จริง จากหน้า builds ของ poe.ninja → นับความถี่
   mod แยกตาม slot / archetype พร้อมช่วง roll (min-max) ที่เจอจริง → publish เป็น
   `meta/latest.json`
3. เป็นตัวเดียวที่ยิง API ภายนอก — consumer ดึงจาก Firebase เท่านั้น

ห้ามยิง character API ของ GGG (`character-window/get-items`). GGG แตะแค่
`trade2/data/stats` ครั้งเดียวแล้ว cache. `pathofexile.com/api/leagues` ถูก
สำรวจไว้สำหรับ `startAt`/`endAt` แต่ไม่รองรับ PoE2 จริง (เพิกเฉยต่อ `realm` param
ทุกกรณี ตอบ PoE1 เสมอ) — วันเริ่ม/จบลีกจึงมาจาก Steam news API แทน
(`src/fetchers/league_dates.py`) ซึ่งไม่ใช่ GGG จึงไม่กระทบกติกานี้.
รองรับ PoE1 และ PoE2 (แยก path `poe1/...`, `poe2/...`). ราคา (prices) รองรับ
ทั้งสองเกมตั้งแต่ phase 7; meta (mod frequency) ยังเป็น PoE2-only.

## 8. JSON Schema ที่ publish (contract กับ consumer)

ห้ามเปลี่ยน breaking โดยไม่เพิ่ม `schema_version`. Additive optional fields
เพิ่มได้โดยไม่ต้อง bump (per `CLAUDE.md`).

### 8.1 `{game}/prices/latest.json` (`{game}` = `poe1` | `poe2`)

Same schema for both games. PoE1 has no Scout equivalent (`poe2scout.com` is
PoE2-only) — `sources` simply omits the `"scout"` key entirely for PoE1
rather than reporting a fake `"ok"`/`"error"` for a source that was never
queried.

```json
{
  "schema_version": 2,
  "generated_at": "2026-07-08T12:00:00Z",
  "league": "<detected league>",
  "sources": {"ninja": "ok", "scout": "ok"},
  "sample": {
    "per_category": {
      "ninja": {"Currency": {"ok": true, "count": 137}, "UniqueWeapon": {"ok": false, "count": 0}},
      "scout": {"Currency": {"ok": true, "count": 90}}
    }
  },
  "currency": [
    {
      "category": "Currency",
      "name": "Divine Orb",
      "base": null,
      "value": 42.5,
      "value_currency": "exalted",
      "divine_value": 1.0,
      "chaos_value": 361.2,
      "source": "ninja",
      "listing_count": 12345,
      "trade_id": "divine-orb",
      "icon_url": "https://web.poecdn.com/gen/image/...png"
    }
  ],
  "items": [
    {
      "category": "UniqueWeapon",
      "name": "...",
      "base": "...",
      "value": 55.0,
      "value_currency": "exalted",
      "divine_value": 0.3,
      "chaos_value": 4.7,
      "source": "scout",
      "listing_count": 75,
      "trade_id": "some-unique-basetype",
      "icon_url": "https://web.poecdn.com/gen/image/...png"
    }
  ]
}
```

Required fields on every `currency` / `items` entry (never omitted, per the
"no price field silently dropped" rule):

- `value` + `value_currency` — PoE2 uses `"exalted"` as the primary unit
  (falling back to whatever the source's own primary unit is if no exalted
  rate exists); PoE1 is always `"chaos"`, per binding condition 1
- `divine_value` — value expressed in divine orbs, regardless of primary unit
- `source` — `"ninja"` or `"scout"`

Additive fields, present whenever the source provides them (kept
permanently — approved 2026-07-08, consumers may rely on them):

- `chaos_value` — value in chaos orbs, when the source's `rates` table
  exposes a chaos conversion (ninja: always for PoE2 currently; scout: not
  always available)
- `listing_count` — number of live listings backing the price (`0` if the
  source didn't report one)
- `trade_id` — stable machine-readable slug for the item/currency
  (`detailsId` upstream), for linking to trade sites
- `icon_url` — CDN icon URL for UI display

If a source fails entirely: `sources.<name>` is set to `"error"` and stale
data from the previous publish is reused for that source's entries, with
`stale: true` set per-entry.

#### `sample.per_category` (phase 7.1, additive -- no `schema_version` bump)

`sources.<name>: "ok"` is whole-source: `NinjaPricesFetcher.fetch_all()`
already skips a category whose fetch/validate raised and just continues
with the rest (never lets one bad category fail the whole source), so
`sources.ninja: "ok"` can be true while, say, 12 of 23 categories silently
failed that run -- a real degradation invisible in `sources` alone.
`sample.per_category.{ninja,scout}` exposes the per-category detail behind
that aggregate: `{category_name: {"ok": bool, "count": int}}`. `"ok": false`
means that category's fetch/validate raised this run (entries reused from
the previous publish for that category are still merged into
`currency`/`items` via the existing stale-while-error path — this field is
diagnostic only, it doesn't change what gets published). `"ok": true` with
`"count": 0` means the category fetched fine and genuinely has zero lines.

Omitted per source when that source never ran at all (`ninja_ok`/`scout_ok`
`False` before any per-category attempt, or PoE1's absent `"scout"`). Scout
fetches every category in a single request (spec 3.2), so it has no
per-category skip step of its own — every category name present in
`sample.per_category.scout` is `"ok": true` by construction; a category
missing from the response just doesn't appear there at all (scout gives no
way to distinguish "doesn't exist for this league" from "failed").

`tools/heartbeat_check.py` (Layer 2) reads this and fails the run if more
than half of any single source's categories are `"ok": false` for either
game's `latest.json` — `check_per_category_health()`.

### 8.1a `{game}/prices/events/{league}.json` (phase 7)

`src/fetchers/league_detector.py`'s `LeagueDetector` already splits poe.ninja's
active-league list into one "main" league (the real challenge league) and
zero or more "events" (any other non-excluded — non-Standard/Hardcore/SSF/
Ruthless — league active at the same time, e.g. a time-limited crossover
event layered on top of the challenge league). Every currently-active event
league gets its own prices file, published with the **exact same 8.1 schema**
as `latest.json` — same required/additive fields, same stale-while-error
behavior, just for that league's own item pool instead of the main one.

`{league}` is `sanitize_league_filename(league_name)`
(`src/storage/publisher.py`): lowercased, non-alphanumeric runs collapsed to
a single `-`, leading/trailing `-` stripped — e.g. `"Return of the Ancients"`
→ `return-of-the-ancients`. This keeps the filename safe as a Firebase
Storage object-name segment regardless of what characters GGG/poe.ninja put
in a league's display name.

When no event league is active for a game (the common case), no files exist
under `{game}/prices/events/` at all — a consumer must not assume any
particular event file exists and should always go through `index.json`
(8.1b) to discover what's currently published, never guess or probe.

### 8.1b `{game}/prices/index.json` (phase 7)

Tells a consumer exactly which league files are currently published for a
game, so it never has to guess/probe for event files:

```json
{
  "schema_version": 1,
  "generated_at": "2026-07-11T12:00:00Z",
  "main": {
    "league": "Runes of Aldur",
    "path": "poe2/prices/latest.json",
    "generated_at": "2026-07-11T12:00:00Z"
  },
  "events": [
    {
      "league": "Return of the Ancients",
      "path": "poe2/prices/events/return-of-the-ancients.json",
      "generated_at": "2026-07-11T12:00:00Z"
    }
  ]
}
```

`events: []` when no event league is active for that game. Each entry's own
`generated_at` can differ slightly from `main.generated_at` and from the
other entries — every league is fetched and published as an independent
step within the same prices job run, not atomically together.

Event files for leagues that are no longer in poe.ninja's active-league list
are deleted by the daily cleanup job (spec 9) — membership-based (checked
against the *current* league list each cleanup run), not an age heuristic,
so a short-lived closed event doesn't linger and a long-running one is never
deleted just for being old.

### 8.2 `poe2/meta/latest.json`

Live on Firebase since 2026-07-08. Shape matches spec exactly: mods
normalized to `stat_id` via `trade2/data/stats` templates, `value_min` (first
number)/`value_max` (last number) per mod, jewels counted per-base-type
per-gem (`jewel:emerald` etc, denominator = gem count not character count),
`count >= 5` noise floor before publish, `stat_dictionary` embedded.

`days_since_league_start` is computed from `src/fetchers/league_dates.py`
(Steam news API, not GGG — see below), `null` only if no matching launch
announcement is found. `status.json`'s `league_end_at` comes from the same
fetcher and is `null` for most of a league's life (GGG only posts an "ends"
announcement in the final stretch).

Known limitations, all logged rather than silently papered over:

- Sample size per slot is capped at poe.ninja's default ~100-row page; no
  working pagination/offset param was found on the builds-search endpoint,
  so `meta.sample_per_slot` in `config.yaml` has no effect yet (documented
  in `src/fetchers/ninja_builds.py`).
- `weapon` and `offhand` slots are unions of several base-type sub-fetches
  (Bow, Wand, Sceptre, ... / Buckler, Shield) merged and deduped client-side,
  since poe.ninja's `items` filter has no generic "any weapon" bucket and
  repeated `items=` query params are AND, not OR (verified live).
- `ModNormalizer` has two text-fallback rules beyond direct template
  matching, both discovered from real unmapped-log data: (1) trade2's table
  normalizes plurals away ("Has # Charm Slot", never "Slots") -- the
  trailing word is singularized and retried; (2) the game displays a
  negative "increased X" roll as "reduced X" (and "more X" as "less X"),
  but the table only lists the positive form -- retried with the word
  swapped back and the value negated. Together these fixed 55% of a sampled
  unmapped-log corpus (1211/2208). Remaining unmapped mods are dominated by
  unique-item-only flavor text and per-player-level scaling implicits that
  genuinely aren't in the trade2 stats table.
- Unmapped mod rate measured live: 0.88% and 2.76% on early test runs;
  5.57% on a full run before the plural/negation fallbacks existed (still
  under the DoD ceiling isn't guaranteed run-to-run without them — this is
  why `status.json` logs an error when `unmapped_mod_pct >= 5`).

### 8.3 ตำแหน่งไฟล์บน Firebase Storage

```
poe-data-hub/
├── poe2/prices/latest.json
├── poe2/prices/index.json
├── poe2/prices/events/{sanitized-league}.json   (0..n, only while an event is active)
├── poe2/meta/latest.json
├── poe2/meta/2026-07-08.json
├── poe1/prices/latest.json
├── poe1/prices/index.json
├── poe1/prices/events/{sanitized-league}.json   (0..n)
└── status.json
```

`Cache-Control`: `max-age=300` สำหรับ prices/index/events, `max-age=3600`
สำหรับ meta.

#### Rate-limit impact of phase 7 (PoE1 + event leagues)

`poe.ninja` is one shared rate-limit bucket (`config.yaml`
`rate_limits."poe.ninja": 2` — 1 request per 2s, enforced by
`src/utils/rate_limiter.py`), and every league fetched (main + each event, for
both games) costs one `NinjaPricesFetcher.fetch_all()` pass — 23 requests for
PoE2's category list, 6 for PoE1's (`src/fetchers/ninja_prices.py`). Scout
(`poe2scout.com`) is a separate host/bucket and PoE2-only — 2 requests per
PoE2 league fetched.

- **Baseline (no events active, 1 league per game — today's real state):**
  23 + 6 = 29 poe.ninja requests + 2 scout requests = 31 requests, ≈62s
  sequential wall time. ~5% of the 1200s (20-minute) cycle interval.
- **Worst-realistic (2 simultaneous event leagues per game, on top of each
  game's main league — 3 leagues/game, more overlap than has ever been
  observed live in either game):** poe2 = 3×(23+2) = 75, poe1 = 3×6 = 18 →
  93 requests, ≈186s (~3.1 min) sequential wall time. Still only ~15.5% of
  the 20-minute cycle.

Conclusion: the 20-minute prices cycle interval remains comfortably
sufficient after phase 7 — even the deliberately-padded worst-case estimate
leaves >80% of the cycle unused. `LeagueDetector`'s own league-list request
is cached 12h (`league.cache_ttl_seconds`) and amortizes to effectively zero
marginal requests per cycle.

## Binding conditions (from project owner, still in force)

1. ห้ามตัด price field ใดๆ ทิ้งโดยไม่เสนอก่อน — ทุก entry มี `value` +
   `value_currency` + `divine_value` ครบ, เก็บทุกหน่วยที่ต้นทางมีให้
   (ดู `chaos_value` ด้านบน). กติกานี้ใช้กับ meta schema ด้วยเมื่อ implement.
2. Bucket ต้อง publish จริงบน Firebase Storage และเปิดผ่าน public URL ได้ —
   ไม่ใช่แค่ไฟล์ local.
3. `league.poe2_override` / `league.poe1_override` ใน `config.yaml` default
   เป็นค่าว่างเสมอ — ระบบเลือกลีกอัตโนมัติเป็นทางหลัก. ข้อยกเว้นเดียวที่ตั้งค่า
   จริงตอนนี้คือ `poe1_override: "Mirage"` เพราะ PoE1 กำลังอยู่ในสถานการณ์ที่
   กติกานี้เขียนไว้เป็นทางออกโดยเฉพาะ: มี event league ("Ancestors") ซ้อนกับ
   challenge league จริง ("Mirage") พร้อมกัน และ API ไม่มี field บอกว่าอันไหน
   เป็นตัวไหน — auto-guess (candidate แรกตามลำดับตัวอักษร) จะเลือกผิดถ้าไม่ pin
   ไว้ (ดู `config/config.yaml` comment และ `src/fetchers/league_detector.py`).

## Status

- [x] Phase 1: skeleton, http_client, rate_limiter
- [x] Phase 2: league_detector, ninja_prices, scout_prices → `prices/latest.json`
      live on Firebase, reviewed and approved 2026-07-08
- [x] Phase 3: normalizer (trade2 stats + mod-text templating)
- [x] Phase 4: ninja_builds + ninja_chars (dedupe cache) + archetyper --
      builds-search API is an undocumented custom binary (protobuf wire
      format, no `.proto`), reverse-engineered live 2026-07-08; see
      `src/fetchers/ninja_builds.py` module docstring and `src/utils/pbwire.py`
- [x] Phase 5: aggregator + SQLite + `meta/latest.json` publish -- live on
      Firebase 2026-07-08
- [x] Phase 6: systemd service (`deploy/poe-data-hub.service`, `--serve`
      CLI mode running 3 independent APScheduler jobs) + `status.json`
      (`last_meta`, `league_end_at`, `meta_job_summary`) + daily cleanup job
      (char_items 90d, Firebase meta files 30d, failed_responses 30d) +
      league start/end dates via Steam news (`src/fetchers/league_dates.py`,
      not GGG). Final verified run 2026-07-09: 1066 mods, 1197 unique
      characters, 0 failed, 2.35% unmapped (down from an initial 5.57% after
      fixing two `ModNormalizer` fallback gaps found from real unmapped-log
      data -- plural normalization and reduced/increased word-flip
      handling). `PricesPublisher.publish()` now reads every upload straight
      back from the GCS API (not the CDN-cached public URL) and raises
      loudly on any mismatch, so a partial/failed publish can never look
      like success (added 2026-07-09 after a public-URL cache staleness
      scare turned out to be `max-age=3600` doing its job, not a real
      failure -- verified via `gcloud storage cat`). 65/65 tests passing.
- [x] Phase 7: PoE1 prices (`poe1/prices/latest.json`, same 8.1 schema,
      `value_currency` always `"chaos"`, no Scout source) + event-league
      prices for both games (`{game}/prices/events/{league}.json`, 8.1a) +
      `{game}/prices/index.json` (8.1b) so a consumer never has to guess
      which leagues are currently published. `run_prices_once` now loops
      over `config.yaml`'s `game_versions` list and, per game, publishes the
      main league then every active event league then the index; daily
      cleanup (spec 9) now also deletes event files for leagues no longer in
      poe.ninja's active-league list (`PricesPublisher.delete_blobs_not_in`).
      `status.json`'s prices fields moved from flat top-level
      (`last_prices`/`league`/`sources`) to nested `status.prices.{game}`
      -- confirmed safe since `tools/heartbeat_check.py` only ever read
      `status.last_meta` and each file's own `generated_at`, never the old
      flat prices keys. `league.league_override` split into
      `league.poe2_override` / `league.poe1_override` (binding condition 3);
      PoE1 needed its override set immediately (see binding condition 3) --
      poe.ninja has no field distinguishing an overlapping event league from
      the real challenge league, and alphabetical-first-candidate guessing
      picks wrong when one is active. Rate-limit impact computed and
      confirmed comfortably within the 20-minute cycle even in a
      deliberately-padded worst-case (see 8.1's rate-limit subsection).
      121/121 tests passing.
- [x] Phase 7.1: `sample.per_category` observability (additive, no
      `schema_version` bump) — `NinjaPricesFetcher.fetch_all()` and
      `ScoutPricesFetcher.fetch_all()` now return `(entries, per_category)`
      instead of `(entries, counts)`, `per_category` being
      `{category: {"ok": bool, "count": int}}`; published on every prices
      file (`latest.json` and every `events/*.json`, both games) as
      `sample.per_category.{ninja,scout}` (8.1). Closes the last gap found
      in the pre-migration bot-repo field survey: `sources.ninja: "ok"`
      alone can't tell a consumer that, say, 12/23 categories are actually
      failing this run. `tools/heartbeat_check.py` now also fails when more
      than half of any single source's categories are `"ok": false`
      (`check_per_category_health`). 132/132 tests passing.
