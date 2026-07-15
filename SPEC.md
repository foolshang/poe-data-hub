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
3. ดึงข่าว (Steam News API + GGG forum link resolve) และ Deal of the Day
   (pathofexile.com) → publish เป็น `{game}/news/latest.json` / `mtx/latest.json`
   (phase 8) — state ว่า consumer โพสต์อะไรไปแล้วยังอยู่ฝั่ง consumer เสมอ
4. เป็นตัวเดียวที่ยิง API ภายนอก — consumer ดึงจาก Firebase เท่านั้น

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

Category taxonomy: PoE2 has 23 categories, PoE1 has 22 (phase 9 -- expanded
from an initial 6-category subset to match
`D:\Projects\PoE_Price-Trade_Checker`'s `profiles.py` `POE1_PROFILE` exactly,
since that project is another consumer relying on the same category names).
Every one of the 16 categories added in phase 9 was live-verified against
poe.ninja's *current* (non-legacy) exchange/stash endpoints before being
added — see `src/fetchers/ninja_prices.py`'s `POE1_CATEGORIES` docstring.

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

### 8.1b `{game}/prices/index.json` (phase 7, `hardcore` key added phase 9)

Tells a consumer exactly which league files are currently published for a
game, so it never has to guess/probe for event/hardcore files:

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
  ],
  "hardcore": {
    "league": "HC Runes of Aldur",
    "path": "poe2/prices/hardcore/latest.json",
    "generated_at": "2026-07-11T12:00:00Z"
  }
}
```

`events: []` when no event league is active for that game. `hardcore: null`
(not an empty object, and never omitted) when no HC variant was found this
cycle -- see 8.1c. Each entry's own `generated_at` can differ slightly from
`main.generated_at` and from the other entries — every league is fetched
and published as an independent step within the same prices job run, not
atomically together.

Event files for leagues that are no longer in poe.ninja's active-league list
are deleted by the daily cleanup job (spec 9) — membership-based (checked
against the *current* league list each cleanup run), not an age heuristic,
so a short-lived closed event doesn't linger and a long-running one is never
deleted just for being old.

### 8.1c `{game}/prices/hardcore/latest.json` (phase 9)

The Hardcore counterpart of the *main* challenge league only (never an HC
variant of an event league -- explicitly out of scope, see binding
condition 6), published with the **exact same 8.1 schema** as `latest.json`.

The HC league's name is **derived, never hardcoded**:
`src/fetchers/league_detector.py`'s `find_hardcore_variant(main_league,
all_league_names)` token-matches against poe.ninja's *unfiltered* league
list (i.e. before `EXCLUDE_LEAGUE_TOKENS` strips HC/Standard/SSF/Ruthless
out) -- a name is the HC variant of `main_league` iff removing a hardcore
token (`"hardcore"` or `"hc"`) from its tokens leaves exactly
`main_league`'s tokens. This correctly handles both games' different naming
conventions without hardcoding either: PoE2 prefixes `"HC "` (`"HC Runes of
Aldur"`), PoE1 prefixes `"Hardcore "` (`"Hardcore Mirage"`) -- confirmed live
2026-07-12, both currently resolve correctly. It also correctly rejects the
permanent `"Hardcore"` league itself (no tokens remain after stripping) and
an HC variant of a *different* league (e.g. `"Hardcore Ancestors"` when
main is `"Mirage"` -- remaining tokens don't match).

If no HC variant can be found this cycle (e.g. a brief league-transition
window), `find_hardcore_variant` returns `None` -- this is never an error:
no file is published, and if a stale one exists from a previous cycle it is
actively deleted (`PricesPublisher.delete_blobs_not_in`, same mechanism
event-league cleanup uses) so that `index.json`'s `hardcore: null` always
means "the file genuinely doesn't exist", never "we just didn't update it".

### 8.2 `{game}/meta/latest.json` (`{game}` = `poe1` | `poe2`, PoE1 added phase 10)

Live on Firebase since 2026-07-08 (PoE2), 2026-07-15 (PoE1). Same schema for
both games. Mods normalized to `stat_id` via the game's own trade-stats
templates (`trade2/data/stats` for PoE2, `trade/data/stats` for PoE1 --
8.3), `value_min` (first number)/`value_max` (last number) per mod, jewels
counted per-base-type per-gem (`jewel:emerald` etc, denominator = gem count
not character count), `count >= 5` noise floor before publish,
`stat_dictionary` embedded.

`days_since_league_start` is computed from `src/fetchers/league_dates.py`
(Steam news API, not GGG — see below), `null` only if no matching launch
announcement is found. `status.json`'s `meta.{game}.league_end_at` comes
from the same fetcher and is `null` for most of a league's life (GGG only
posts an "ends" announcement in the final stretch).

**PoE1 data collected around 2026-07 is heavily league/event-biased and
should be read with that in mind**: it was gathered in the last days of the
Mirage challenge league, overlapping the closing days of the "Return of the
Ancestors" event -- late-league gear/mod distributions skew toward
end-game/damage-focused builds (per the general "meta shifts with league
age" pattern spec 6.5 already documents), and a portion of the sampled
population was specifically playing the event rather than a fresh-start
Mirage character. **The real value of PoE1 meta starts from the first day
of the *next* PoE1 league** (see 8.3 and "Job cadence" below for how the hub
is already set up to capture that automatically). This is flagged here, not
hidden, per CLAUDE.md's "never silently drop/misrepresent" spirit even
though it's not a price field.

PoE1's builds/character-detail endpoints are **byte-for-byte the same
protobuf/JSON format** as PoE2's, just under `/poe1/` instead of `/poe2/`
(confirmed live 2026-07-14 -- see `src/fetchers/ninja_builds.py` and
`ninja_chars.py` module docstrings for the one real structural difference,
`snapshotVersions[]` entries carrying a `type` field PoE2's lack). PoE1 has
no `focus`/`charm` slots (confirmed absent from its item facet entirely) and
uses the classic jewel system -- Cobalt/Crimson/Viridian/Prismatic (colors)
+ Cluster (all sizes collapsed to one `jewel:cluster` bucket) + named Abyss
jewel variants (`jewel:searchingeye`, `jewel:murderouseye`,
`jewel:ghastlyeye`, `jewel:hypnoticeye` -- PoE1 abyss jewels don't share one
generic base type, unlike Cluster) + `jewel:timeless`, all via the same
`_slugify_jewel_base()` PoE2 uses (extended phase 10 to strip a trailing
"Jewel" word and a cluster-size prefix, which PoE2's simpler bare names like
"Diamond" never had to begin with).

Known limitations, all logged rather than silently papered over:

- Sample size per slot is capped at poe.ninja's default ~100-row page; no
  working pagination/offset param was found on the builds-search endpoint,
  so `meta.sample_per_slot` in `config.yaml` has no effect yet (documented
  in `src/fetchers/ninja_builds.py`). Applies to both games.
- `weapon` and `offhand` slots are unions of several base-type sub-fetches
  merged and deduped client-side, since poe.ninja's `items` filter has no
  generic "any weapon" bucket and repeated `items=` query params are AND,
  not OR (verified live). PoE2: Bow, Crossbow, Mace, Quarterstaff, Sceptre,
  Spear, Staff, Wand / Buckler, Shield. PoE1: Bow, Claw, Dagger, Axe, Mace,
  Sword, Staff, Wand / Shield (no Crossbow/Quarterstaff/Sceptre/Spear --
  PoE2-only weapon types; no Buckler).
- `ModNormalizer` has two text-fallback rules beyond direct template
  matching, both discovered from real unmapped-log data: (1) the trade
  stats table normalizes plurals away ("Has # Charm Slot", never "Slots")
  -- the trailing word is singularized and retried; (2) the game displays a
  negative "increased X" roll as "reduced X" (and "more X" as "less X"),
  but the table only lists the positive form -- retried with the word
  swapped back and the value negated. Together these fixed 55% of a sampled
  PoE2 unmapped-log corpus (1211/2208). Remaining unmapped mods are
  dominated by unique-item-only flavor text and per-player-level scaling
  implicits that genuinely aren't in either game's trade stats table.
- Unmapped mod rate measured live (PoE2): 0.88% and 2.76% on early test
  runs; 5.57% on a full run before the plural/negation fallbacks existed
  (still under the DoD ceiling isn't guaranteed run-to-run without them —
  this is why `status.json` logs an error when `unmapped_mod_pct >= 5`, now
  tracked independently per game in `meta_unmapped_history.{game}`).
- Archetype classification rules (`config/archetype_rules.yaml`) are
  **shared between both games on purpose**, not split -- keyword-substring
  matching against element names ("Fire", "Cold", ...) turned out to
  already generalize well: live-tested phase 10 against a real 30-character
  PoE1 sample (weapon slot, so every character had real weapon data for the
  fallback path) and got **0% unknown**. The one real gap PoE1's actual
  skill names exposed was "Animate Guardian"/"Animate Weapon" (real PoE1
  minion skills) not matching any minion keyword -- fixed by adding
  `"Animate"` to `minion_keywords` (harmless for PoE2, which has no
  `Animate*` skill gems).

### 8.3 Internal GGG mod-stats cache (`internal/stats_cache.json`, `internal/stats_cache_poe1.json`)

Not a public consumer-facing schema (lives under `internal/`, referenced
internally by `ModNormalizer` -- see 8.2), but documented here since it's
part of the meta pipeline's data flow. Refreshed weekly by
`tools/refresh_stats.py` via `.github/workflows/refresh-stats.yml` (GitHub
Actions, not the deploy VM -- GGG permanently blocks `trade2/data/stats`
requests from GCP IPs, confirmed live 2026-07-09; `trade/data/stats`
(PoE1) wasn't independently reconfirmed blocked the same way but is routed
through the same safe path out of caution, phase 10). Both games fetched,
validated, and published independently in one script run -- one game's
failure can't corrupt or block the other's cache. Envelope shape:
`{"fetched_at": <unix epoch>, "data": <raw GGG trade(2)/data/stats response>}`.

### 8.4 `{game}/news/latest.json` (`{game}` = `poe1` | `poe2`, phase 8)

Steam news for one game, GGG-link-resolved (`src/fetchers/news.py`, ported
from `Bot-Poe-Discord/poe-discord-bot/feeds.py`). Stateless: the hub always
publishes the latest ~15 entries every run (matches Steam's own `count=15`),
regardless of what any consumer has already posted -- which entries have
been *posted* is consumer-side state (e.g. the bot repo's `seen.json`),
never published here (binding condition 4).

```json
{
  "schema_version": 1,
  "generated_at": "2026-07-12T15:00:00Z",
  "sources": {"steam": "ok"},
  "entries": [
    {
      "id": "1836506165584601",
      "title": "Path of Exile 2 Build Showcase: Slowmaxing Chronomancer",
      "content": "...(full BBCode/HTML-cleaned plain text, not truncated)",
      "category": "news",
      "link": "https://www.pathofexile.com/forum/view-thread/3982050",
      "link_is_ggg": true,
      "published": "2026-07-12T10:30:00Z"
    }
  ]
}
```

- `category` — `"news"` | `"patch"` | `"mtx"`, same heuristic as the bot
  repo's original (`categorize()`): version-number-prefixed or contains
  "patch" -> patch; contains a sale keyword -> mtx; else news.
- `link` — **always `www.pathofexile.com`, never rewritten to `th.`**
  (approved decision: that's a display concern for whoever renders the
  link, not the hub's job).
- `link_is_ggg` — `true` when title-matching against GGG's own forum
  listing found a confident match and `link` was upgraded from the raw
  Steam URL to the real forum thread; `false` means `link` is still the
  original Steam link (either no confident match, or the GGG listing
  scrape itself failed -- both degrade gracefully, never drop the entry).
- If the Steam fetch fails entirely: `sources.steam` is `"error"` and the
  previous publish's `entries` are reused verbatim (stale-while-error, spec
  10.1b) instead of publishing an empty list.
- `content` is published in full (not truncated) -- any length limit for
  display (e.g. Discord's embed description cap) is a consumer concern.

### 8.5 `mtx/latest.json` (phase 8)

PoE1's Deal of the Day shop specials (`src/fetchers/deals.py`, ported from
`Bot-Poe-Discord/poe-discord-bot/deals.py`). **Not split per-game** like
prices/news -- PoE2 (pathofexile2.com) was surveyed and has no scrapable
Deal of the Day at all (client-rendered SPA, no server-side data or JSON
API), so there's no poe2 side to split.

```json
{
  "schema_version": 1,
  "generated_at": "2026-07-12T15:00:00Z",
  "sources": {"ggg_shop": "ok"},
  "deals": [
    {
      "item_name": "Premium Quad Stash Tab",
      "link": "https://www.pathofexile.com/shop/item/PremiumQuadTab",
      "discount_percent": 20,
      "points_price": 120,
      "ends_in": "Ends in 1 day",
      "first_seen_at": "2026-07-11T09:05:00Z"
    }
  ]
}
```

- `item_name` is the only stable key GGG's page gives (no numeric id like
  Steam's `gid`) -- used as the merge key for `first_seen_at` and would be
  the natural dedup key for any consumer, same as the bot repo's
  `seen_deals.json` already does today.
- `ends_in` is raw scraped display text (e.g. `"Ends in 5 hours"`), **not**
  a machine-parseable timestamp -- known limitation carried over unchanged
  from the bot repo's original scrape, GGG's page doesn't expose a real one.
- `first_seen_at` (added phase 8, per project owner's explicit request) --
  when the hub *first* saw this exact `item_name` on the page, carried
  forward unchanged across runs while the deal keeps appearing, set to this
  run's `generated_at` the run it first appears. Computed by
  `build_mtx_schema()` merging against the previous publish -- deliberately
  a different concept from a consumer's own "have I posted this" dedup
  state; this is published data, not a posting-state mechanism.
- If the scrape fails entirely: `sources.ggg_shop` is `"error"` and the
  previous publish's `deals` (including their original `first_seen_at`) are
  reused verbatim.

### 8.6 ตำแหน่งไฟล์บน Firebase Storage

```
poe-data-hub/
├── poe2/prices/latest.json
├── poe2/prices/index.json
├── poe2/prices/events/{sanitized-league}.json   (0..n, only while an event is active)
├── poe2/prices/hardcore/latest.json             (only while an HC variant is found)
├── poe2/meta/latest.json
├── poe2/meta/2026-07-08.json
├── poe2/news/latest.json
├── poe1/prices/latest.json
├── poe1/prices/index.json
├── poe1/prices/events/{sanitized-league}.json   (0..n)
├── poe1/prices/hardcore/latest.json             (only while an HC variant is found)
├── poe1/meta/latest.json                        (phase 10)
├── poe1/meta/2026-07-15.json                    (phase 10)
├── poe1/news/latest.json
├── mtx/latest.json
├── internal/stats_cache.json                    (PoE2 trade2 stats, weekly refresh)
├── internal/stats_cache_poe1.json                (PoE1 trade stats, weekly refresh, phase 10)
└── status.json
```

`Cache-Control`: `max-age=300` สำหรับ prices/index/events, `max-age=3600`
สำหรับ meta, `max-age=120` สำหรับ news/mtx.

#### Job cadence (phase 10: PoE1 meta added to the same nightly slot)

`run_meta_once` loops `game_versions` (poe2 then poe1) **sequentially in one
job invocation**, same `schedule.meta_cron_hour_thailand` cron slot as
before (default 02:00 ICT) -- not two separate schedules. One game's
failure never blocks or corrupts the other's publish (each game's whole
pipeline runs in its own try/except in `run_meta_once`); one Layer-1
healthchecks.io ping fires only after *both* games have been attempted
(task 7: a failure in either game must be visible, not maskable by the
other game happening to succeed).

Real measured combined wall-clock time and peak RSS (both games, full run,
production VM): **[TODO: filled in after the live deploy-verification run
-- see the Status entry below for the actual numbers once measured]**.
PoE2 alone was already measured at ~40 minutes; PoE1's own category/slot
counts are comparable (22-23 poe.ninja builds-search filter requests per
game, see the rate-limit subsection below), so a roughly-doubled combined
total was expected going in, but VM wall-clock is dominated by unique
*character* count after dedup (one `NinjaCharsFetcher` request per unique
character, all sharing the same `poe.ninja` 2s rate-limit bucket the prices
job also uses), which isn't knowable without an actual measured run --
hence deferring to a real number rather than assuming the estimate. Either
way this runs once/day in an off-peak cron slot with no other job
contending for the same time window, so even a materially-larger-than-2x
total remains comfortably inside a nightly maintenance window.

#### Readiness for PoE1's next league launch (phase 10)

The whole point of adding PoE1 meta now, before Mirage/Ancestors ends, is
to capture data starting the *first day* of the next PoE1 league without
needing another deploy. Confirmed ready, no further code changes needed:

- **`LeagueDetector(game="poe1")`** already auto-detects the new league from
  poe.ninja's league list with zero code changes -- it's the same generic
  token-matching logic PoE2 already uses (phase 2), and `poe1_override` in
  `config.yaml` only needs updating if a *second* non-excluded PoE1 league
  is active simultaneously (an overlapping event, same ambiguity class as
  today's Mirage/Ancestors) and auto-guess picks wrong -- not required for
  every league transition.
- **`LeagueDatesFetcher(game="poe1")`** (phase 10) already resolves
  start/end dates from PoE1's own Steam news feed (separate appid, 238960)
  with the same "now available" / "league ends" pattern matching PoE2 uses
  -- including the present-tense "ends on" phrasing fix this phase found
  (see 8.2), which was specifically discovered by testing against Mirage's
  *own* real end-of-league Steam post, not a hypothetical.
- **`schedule.meta_runs_per_day_league_start`** (existing config knob, spec
  9) already applies to the *whole* meta job now that both games run in one
  invocation -- bumping it to 2-4 runs/day during the first few days of a
  new league (either game) speeds up both games' initial data collection
  together, not just one. No new per-game config was needed for this.

No dry-run against a hypothetical "next league" was possible (it doesn't
exist yet), but every piece was independently live-tested against Mirage's
real data this phase: league detection, league dates (including the actual
end-of-league post), and the full builds→chars→normalize→aggregate→publish
pipeline end-to-end (see the Status entry below).

#### Job cadence (phase 8)

News + mtx run as **one job** (`run_news_once`, `schedule.news_interval_minutes`,
default **5 minutes** -- approved cadence) since both are cheap, time-sensitive,
and share no fetch cost worth separating: per game, one Steam News API call
+ up to 2 GGG forum-listing scrapes (`src/fetchers/news.py`); plus one
`pathofexile.com` homepage scrape for deals (`src/fetchers/deals.py`).
Total ~7-9 requests per run across 2 hosts already covered by existing
`rate_limits` entries (`api.steampowered.com`, `www.pathofexile.com`) --
negligible next to the prices job's own budget (see the rate-limit
subsection above), no separate analysis needed.

One Layer-1 healthchecks.io ping per job run (`healthchecks.news_ping_url`),
same semantics as prices/meta. Layer 2 (`tools/heartbeat_check.py`) checks
`{game}/news/latest.json`'s `generated_at` for both games (15-minute
threshold, ~3x the 5-minute interval) -- does not check `mtx/latest.json`
separately, since both files publish in the same job run and news freshness
is already a reliable proxy for "did the job run".

#### Rate-limit impact of phases 7 and 9 (PoE1 + event leagues + hardcore)

`poe.ninja` is one shared rate-limit bucket (`config.yaml`
`rate_limits."poe.ninja": 2` — 1 request per 2s, enforced by
`src/utils/rate_limiter.py`), and every league fetched (main + each event +
hardcore, for both games) costs one `NinjaPricesFetcher.fetch_all()` pass —
23 requests for PoE2's category list, **22** for PoE1's (phase 9, up from an
initial 6 -- `src/fetchers/ninja_prices.py`). Scout (`poe2scout.com`) is a
separate host/bucket and PoE2-only — 2 requests per PoE2 league fetched
(main, each event, and hardcore all count).

- **Baseline (today's actual live state, confirmed 2026-07-12): poe2 has no
  event but does have a hardcore variant; poe1 has one event ("Ancestors")
  and a hardcore variant ("Hardcore Mirage") — 5 leagues fetched this
  cycle total (poe2 main, poe2 hardcore, poe1 main, poe1 event, poe1
  hardcore):**
  poe.ninja: 23 + 23 + 22 + 22 + 22 = 112, scout: 2 + 2 = 4 → **116
  requests**, ≈232s (~3.9 min) sequential wall time. ~19% of the 1200s
  (20-minute) cycle interval.
- **Worst-realistic (2 simultaneous event leagues per game on top of each
  game's main + hardcore — 4 leagues/game, more overlap than has ever been
  observed live in either game):**
  poe2 = 4×(23+2) = 100, poe1 = 4×22 = 88 → **188 requests**, ≈376s (~6.3
  min) sequential wall time. Still only ~31% of the 20-minute cycle.

Conclusion: the 20-minute prices cycle interval remains comfortably
sufficient after phase 9 — even the deliberately-padded worst-case estimate
leaves >65% of the cycle unused, and today's actual live state (not just a
hypothetical) already exercises 5 simultaneous league fetches at ~19%
utilization. `LeagueDetector`'s own league-list request is cached 12h
(`league.cache_ttl_seconds`) and amortizes to effectively zero marginal
requests per cycle (the hardcore lookup reuses the same cached fetch, no
extra request).

**Pre-existing characteristic worth flagging now that meta job runtime
roughly doubles (phase 10), not introduced by this phase**: `run_scheduler`
uses APScheduler's `BlockingScheduler` with its default executor
(`ThreadPoolExecutor(max_workers=10)`), and `max_instances=1` is set
*per job id* (prices, meta, news, cleanup each independently), not across
jobs -- so a meta run overlapping a prices cycle is possible and each job
builds its own fresh `HttpClient`/`RateLimiter` via `_build_http_client()`,
meaning the two run with **independent** rate-limit state against the same
`poe.ninja` host during any overlap window, not a shared budget. This
already existed pre-phase-10 (PoE2-only meta ran ~40min, up to two 20-min
prices cycles could fall inside that window); phase 10's ~2x meta runtime
roughly doubles the overlap window too. Not fixed here (would need a
shared rate limiter across concurrently-running jobs, a real architecture
change, and is out of this phase's scope) -- noted so it's a known,
documented characteristic rather than a silent gap, and worth its own
follow-up if poe.ninja 429s are ever observed during that window.

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
4. State ว่า consumer โพสต์/แสดงผลอะไรไปแล้วบ้าง (เช่น bot repo's `seen.json`,
   `seen_deals.json`) เป็นของฝั่ง consumer เสมอ — hub ไม่เก็บ ไม่ publish
   state แบบนี้เด็ดขาด (เห็นได้จาก phase 8: `news/latest.json` เป็น
   stateless snapshot ทุก run, `mtx/latest.json`'s `first_seen_at` คือ
   "hub เห็นครั้งแรกเมื่อไหร่" ไม่ใช่ "ใครโพสต์ไปแล้วหรือยัง" คนละแนวคิดกัน).
5. Standard / Hardcore Standard (permanent, non-challenge leagues) จะไม่ถูก
   ดึงราคาเลย ทั้ง main/event/hardcore — **ตัดสินใจไว้ตั้งใจ** (phase 9),
   เหตุผล: ไม่มีผู้ใช้จริง (ตัวบอทเองก็ไม่เคยดึงราคา Standard) ไม่ใช่ข้อจำกัด
   ทางเทคนิค — `EXCLUDE_LEAGUE_TOKENS` ใน `league_detector.py` กรองออกตั้งแต่
   ชั้น candidate list, ถ้าอนาคตมีความต้องการจริงค่อยพิจารณาใหม่.
6. Hardcore variant ดึงเฉพาะของ **main league เท่านั้น** (phase 9) — ไม่ทำ
   HC ของ event league (เช่นไม่มี "Hardcore Return of the Ancients")
   ขอบเขตที่ตั้งใจตัดไว้ ไม่ใช่ gap ที่ลืมทำ.

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
- [x] Phase 8: news + mtx, moved off the bot repo (`src/fetchers/news.py`,
      `src/fetchers/deals.py`, ported from `Bot-Poe-Discord/poe-discord-bot`'s
      `feeds.py`/`deals.py`) -- `{game}/news/latest.json` (8.4),
      `mtx/latest.json` (8.5, PoE1-only, `first_seen_at` per deal).
      `run_news_once` runs as one job every 5 minutes
      (`schedule.news_interval_minutes`), its own healthchecks.io ping
      (`healthchecks.news_ping_url`). `HttpClient.get_text()` added for HTML
      scraping (GGG forum listings, pathofexile.com deals sidebar) alongside
      the existing `get_json`/`get_bytes`. `link` is always
      `www.pathofexile.com`, never rewritten to `th.` (approved: that's a
      display concern for the consumer). GGG-link title-resolution logic
      (`_build_ggg_link_index`/`_resolve_ggg_link`) moved to the hub
      unchanged. Posting-dedup state (`seen.json`/`seen_deals.json`) stays
      entirely in the bot repo (binding condition 4) -- both new files are
      stateless snapshots republished fresh every run.
      `tools/heartbeat_check.py` now also checks `{game}/news/latest.json`
      freshness for both games. Live-verified end-to-end from the dev
      machine before deploy: 100% GGG-link resolution rate on both games'
      real current news, 3/3 real Deal of the Day entries scraped correctly.
      163/163 tests passing.
- [x] Phase 9: two gaps found from surveying the Checker repo, closed before
      its own migration to the hub --
      1. PoE1 category taxonomy expanded 6 → 22 to match
         `PoE_Price-Trade_Checker`'s `profiles.py` `POE1_PROFILE` exactly
         (8.1) -- every new category (Oil, Incubator, Scarab, Fossil,
         Resonator, Essence, DeliriumOrb, Artifact, Beast, UniqueFlask,
         UniqueJewel, SkillGem, ClusterJewel, Map, UniqueMap, BaseType)
         live-verified against poe.ninja's current exchange/stash endpoints
         before adding (Checker's own profile still references the old,
         now-404ing `currencyoverview`/`itemoverview` URLs -- only the
         `?type=` values were reusable, not the endpoint prefix).
      2. Hardcore variant of the main challenge league, both games
         (8.1c) -- `{game}/prices/hardcore/latest.json`, derived via new
         `find_hardcore_variant()` token-matching (never hardcoded),
         `{game}/prices/index.json` gained a `hardcore` key (8.1b, null
         when no HC variant found -- file is actively deleted in that case,
         never left stale). HC of event leagues and Standard/HC Standard
         both explicitly out of scope (binding conditions 5, 6).
      Rate-limit recomputed with today's actual live state (5 simultaneous
      league fetches: both games' main + hardcore, plus poe1's one active
      event) -- 116 requests, ~19% of the 20-minute cycle; worst-realistic
      padded estimate still only ~31%. `tools/heartbeat_check.py` now also
      checks hardcore freshness via `index.json`'s `hardcore` key (never
      flags a null value). 183/183 tests passing.
- [x] Phase 10: PoE1 meta pipeline (`poe1/meta/latest.json`, 8.2), reusing
      the whole poe2 pipeline (sample→dedupe→normalize→archetype→aggregate→
      publish) unchanged at every layer except the fetcher URLs -- PoE1's
      builds-search/character-detail endpoints turned out to be
      byte-for-byte the same protobuf/JSON format as PoE2's (confirmed live
      2026-07-14), which was the one open question this phase had to answer
      before committing to an approach. `ModNormalizer` now game-aware
      (`trade/data/stats` for PoE1 vs `trade2` for PoE2, 8.3); PoE1's stats
      cache refreshed by the same `refresh-stats.yml` GitHub Actions
      workflow, published to its own `internal/stats_cache_poe1.json`.
      `LeagueDatesFetcher` made game-aware too and, while verifying it
      against Mirage's real Steam posts, found and fixed a real bug: PoE1's
      end-of-league post phrasing is present-tense ("ends on", not PoE2's
      "will end on") and the original PoE2-only regex would have silently
      failed to parse it -- caught by testing against real data, not
      hypothetically. `_slugify_jewel_base()` extended to handle PoE1's
      jewel-base naming (trailing "Jewel" word, cluster-size prefixes,
      named Abyss variants -- see 8.2). Archetype rules stayed a single
      shared file by design after live-testing showed 0% unknown against a
      real 30-character PoE1 sample; only gap found was "Animate" as a
      minion keyword. `LocalDb.snapshots` gained a `game` column via an
      explicit migration (backfills existing rows as `'poe2'`).
      `_update_meta_status`/`heartbeat_check.py` restructured to nested
      per-game shape (`status.meta.{game}`), same precedent as phase 7's
      prices restructuring. Confirmed league-detection/league-dates need no
      further changes to auto-capture PoE1's next league launch from day
      one. PoE1 data collected now is explicitly flagged as late-league/
      event-biased (8.2) -- real value starts next PoE1 league.
      215/215 tests passing.
