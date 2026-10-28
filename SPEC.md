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
3. ดึงข่าว (forum ของ GGG เอง — scrape listing + เนื้อ thread รายอัน, phase 8
   เดิมใช้ Steam News, เปลี่ยนมาเป็น forum ล้วน 2026-07-18) และ Deal of the Day
   (pathofexile.com) → publish เป็น `{game}/news/latest.json` / `mtx/latest.json`
   — state ว่า consumer โพสต์อะไรไปแล้วยังอยู่ฝั่ง consumer เสมอ
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
Actions, not the deploy VM -- GGG permanently blocks both `trade2/data/stats`
(PoE2, confirmed 2026-07-09) and `trade/data/stats` (PoE1, confirmed
2026-07-15 -- see CLAUDE.md's "deploy gotcha" note) from GCP IPs). Both
games fetched, validated, and published independently in one script run --
one game's failure can't corrupt or block the other's cache. Envelope shape:
`{"fetched_at": <unix epoch>, "data": <raw GGG trade(2)/data/stats response>}`.

**Operational note (learned the hard way, 2026-07-15)**: PoE1 meta's very
first run on the deploy VM failed outright -- `internal/stats_cache_poe1.json`
didn't exist yet (this blob path didn't exist before phase 10's code merged,
and the existing weekly cron had last run *before* that, so it never
populated it), so `ModNormalizer.load()` fell through Firebase (404) ->
direct GGG (403, blocked) -> disk cache (nothing there yet either) and
raised, failing that cycle's PoE1 meta run entirely (caught by
`run_meta_once`'s per-game try/except -- PoE2 was unaffected). Fixed by
manually triggering `refresh-stats.yml` (`gh workflow run`) once, then
re-running the meta job. Lesson generalized in CLAUDE.md: **any new
stats-cache Firebase path needs an actual manually-triggered
`refresh-stats.yml` run before its first meta job on the VM, not just
"routed through a safe path" reasoning** -- verify the blob exists.

### 8.4 `{game}/news/latest.json` (`{game}` = `poe1` | `poe2`, schema v2, 2026-07-18 redesign)

**GGG's own forum is now the sole source** (`src/fetchers/news.py`'s
`fetch_forum_news`) -- Steam News was dropped entirely. Reason: Steam is
not a reliable primary source. A real patch note, GGG thread 3985152
("0.5.4c Patch Notes", posted 2026-07-17), never made it to Steam's feed at
all -- confirmed live 2026-07-18, the day this redesign happened. The forum
listing pages (`GGG_LISTING_URLS`) were already being scraped here since
phase 8 to *upgrade* Steam links to real GGG threads; that scrape is now
the entry source itself. One entry per thread on the configured listing(s);
`content` is the thread's first post, scraped from the thread's own page.

```json
{
  "schema_version": 2,
  "generated_at": "2026-07-18T15:00:00Z",
  "sources": {"forum": "ok"},
  "entries": [
    {
      "id": "forum-3985152",
      "title": "0.5.4c Patch Notes",
      "content": "0.5.4c Patch Notes\nAdded support for the new Supporter Packs.\nThank you for your support!",
      "category": "patch",
      "link": "https://www.pathofexile.com/forum/view-thread/3985152",
      "link_th": "https://th.pathofexile.com/forum/view-thread/3986001",
      "published": "2026-07-16T18:30:00Z"
    }
  ]
}
```

- `id` — `"forum-{thread_id}"`, `thread_id` from the listing href
  (`/forum/view-thread/{id}`). **Breaking change from schema v1**, which
  used a bare Steam `gid` with no prefix -- see "required deploy order"
  below, this must not reach production ahead of every consumer's own
  migration.
- `category` — `"news"` | `"patch"` | `"mtx"`. Threads scraped from either
  game's dedicated Patch Notes forum (PoE2 `2212`, PoE1 `patch-notes`) are
  unconditionally `"patch"` (`FORCED_PATCH_LISTING_URLS`) regardless of
  title wording -- a bare "Hotfix 2" title wouldn't otherwise match the
  title heuristic. Every other thread (including `poe-press` and
  `dev-manifesto`, see "Listing coverage" below) falls back to the original
  title-keyword heuristic (`categorize()`, unchanged since phase 8):
  version-number-prefixed or contains "patch" -> patch; contains a sale
  keyword -> mtx; else news.
- `link` — **always `www.pathofexile.com`, never rewritten to `th.`**
  (unchanged from phase 8: that's a display concern for whoever renders the
  link). There is no more `link_is_ggg` field -- every entry is inherently
  a GGG link by construction now, so the field would always be `true` and
  was dropped as dead weight.
- `link_th` (**optional, additive, phase 11, matching logic unchanged**) --
  key is entirely absent when there's no confident match, never `null`.
  See below.
- `content` can legitimately be `""` for a **brand-new** thread whose
  content scrape failed (forum layout changed for that specific thread, or
  a transient fetch error) -- title/link/category are independent of
  content scraping and are still published; a warning is logged
  (`news(%s): content scrape failed for new thread ...`), this is not
  treated as an error. Already-known threads (thread `id` present in the
  previous publish) reuse their previously-scraped `content` unchanged --
  a thread's content is scraped **once**, not every cycle.
- If the listing scrape fails entirely (every configured listing URL
  errors, or the combined result is empty -- e.g. GGG's forum HTML
  structure changes): `sources.forum` is `"error"` and the previous
  publish's `entries` are reused verbatim (stale-while-error, spec 10.1b)
  instead of publishing an empty list.
- `content` is published in full (not truncated) -- any length limit for
  display (e.g. Discord's embed description cap) is a consumer concern.
- `published` is a real UTC timestamp, localized from the forum's own
  displayed post_date using the timezone name scraped from that same page
  (see "Timezone correctness" below) -- not a copy of Steam's timestamp,
  which no longer exists in this pipeline.

#### Listing coverage (`GGG_LISTING_URLS`, updated 2026-07-19)

- **PoE2**: `2211` (Announcements), `2212` (Patch Notes).
- **PoE1**: `news`, `patch-notes`, `poe-press`, `dev-manifesto`.

The PoE1 set started as just `news` at the 2026-07-18 redesign and was
found incomplete the very next day: GGG posts PoE1's 3.29.0 patch notes to
a dedicated `patch-notes` forum, separate from `news` -- a blind spot the
old Steam feed never had, since Steam merged everything into one stream
regardless of which forum category GGG posted to. Root-caused by an actual
missed real-world patch note, then closed by auditing **every** category
on both games' live forum index (not just the obviously news-shaped ones)
rather than fixing the one gap found and moving on:

- `poe-press` — genuinely first-party GGG content despite the "press" name
  (confirmed by reading actual thread content, not just the category
  title) -- real gap, real news missed (a league-launch announcement was
  sitting there uncovered).
- `dev-manifesto` — legitimate first-party content, very low frequency
  (gaps of many months between posts), added anyway since a quiet category
  costs nothing extra and the content that does appear is high-signal.
- Deliberately **not** added: `ps4-patch-notes` / `xbox-patch-notes`
  (console-specific mirrors of the same PC patch notes, redundant for this
  feed) and every trade/PvP/guild/bug-report/discussion forum on both
  games (confirmed non-news by title, not assumed).
- PoE2's forum-index cluster of numeric ids adjacent to `2211`/`2212`
  (`2213`-`2220`, `2304`, `2381`) was audited too and confirmed to be
  entirely non-news (Feedback, Bug Reports, Builds, Trading, Jobs) --
  `2211`/`2212` remain the only PoE2 news categories.

`poe-press`/`dev-manifesto` are **not** in `FORCED_PATCH_LISTING_URLS` --
their titles already read naturally via the default `categorize()`
heuristic (e.g. a league-launch press release correctly falls to `"news"`,
not `"patch"`).

Thai counterparts (`TH_GGG_LISTING_URLS`), confirmed live 2026-07-19:
`patch-notes` is the same URL slug on `th.` (like `news` already was,
genuinely translated -- confirmed via a real pair, EN thread 3985332 the
3.29.0 patch notes vs TH thread 3985358, posted the same second on both).
`dev-manifesto`'s Thai counterpart is **not** the same slug -- it's forum
id `1010` ("แถลงการณ์จากทีมพัฒนา"), found by browsing th.'s own forum index
and confirmed by comparing thread titles/dates against the English
versions (e.g. EN 3292308 "Character Balance in Path of Exile: Lake of
Kalandra" vs TH 3292313, 24 seconds apart). `poe-press` has **no** Thai
counterpart at all -- checked the full th. forum nav menu, no press-release
category exists there; `link_th` simply never resolves for those entries,
the same graceful "no match = no field" behavior as any other unmatched
entry, nothing special needed in code for this case.

#### Timezone correctness (`published`, 2026-07-18)

Every GGG forum page (listing or thread, `www.` or `th.`) embeds
`window.momentTimezone = '<IANA name>';` in a `<script>` block, and the
`post_date` text displayed on that page is rendered in *that* timezone --
**not UTC**. Confirmed live 2026-07-18 from the dev machine two independent
ways: (1) Steam's real UTC `date` for "0.5.4b Patch Notes" was ~6.5h behind
that same thread's own forum `post_date`; (2) the HTTP response `Date`
header (true UTC) compared against the most recent reply's displayed
`post_date` on an active thread showed a consistent ~5h+ gap. Both are
consistent with the scraped value at the time, `Asia/Bangkok` (UTC+7) --
almost certainly GeoIP-dependent (a deploy VM in a different region may see
a different value), so the hub never hardcodes it: `_localize_to_utc` reads
the tz name fresh from each scraped page (`_MOMENT_TZ_RE`) and converts the
naive `post_date` to a real UTC value via `zoneinfo.ZoneInfo`. Falls back to
treating the naive value as if it were already UTC -- logged, never silent
-- only if the tz name is missing or unrecognized, same "never drop the
entry, degrade precision instead" policy used for a content-scrape failure.
(`zoneinfo` needs the `tzdata` package for a reliable IANA database on
Windows dev machines; `requirements.txt` pins it explicitly.)

#### `link_th` (phase 11, matching logic unchanged by the 2026-07-18 redesign)

From the bot repo's 2026-07-15 survey (`Bot-Poe-Discord/poe-discord-bot`'s
`HISTORY.md`): `th.pathofexile.com` carries **genuinely separate,
independently-translated threads** for some but not all news -- it is not a
1:1 domain mirror of the English forum. Two things had to be confirmed live
before this could be implemented at all:

1. **Which Thai forum category actually has translations.** PoE1's Thai
   `forum/view-forum/news` is real and translated (same URL path as
   English, just the `th.` domain). PoE2 is *not* that simple: English
   categories `2211`/`2212` return English content verbatim on `th.` (Thai
   UI chrome only, no translation) -- the real Thai PoE2 categories are
   **different forum ids**, `2282` ("ประกาศ Early Access") and `2283`
   ("รายละเอียดแพทช์"), confirmed live 2026-07-15 by opening a sample thread
   and checking its `<title>` matched the Thai listing.
2. **How to match an English entry to its Thai counterpart.** Not every
   thread has a translation (survey found 3/30 missing for PoE1), so
   list-position pairing breaks the moment any one is skipped. Steam's own
   `published` timestamp was tried and rejected (back when Steam was still
   a source, phase 11) -- cross-checked live against a real thread and
   found a **~7000 second (nearly 2 hour) gap** between Steam's `date` and
   that same thread's own forum post_date (different GGG systems, not a
   clean timezone offset). What *does* line up tightly: **the English
   GGG-forum thread's own post_date against the Thai thread's own
   post_date** -- both are the same forum software, same display clock
   (see "Timezone correctness" above -- this still holds even though
   Steam is gone, since both sides come from the forum). Real examples
   confirmed live 2026-07-15 (PoE1, EN vs TH post_date): `Jul 7, 4:19:03 AM`
   vs `7 ก.ค. 04:19:11` (8s apart), `Jun 20, 4:00:00 AM` vs
   `20 มิ.ย. 04:00:00` (exact), `Jun 8, 7:07:18 AM` vs `8 มิ.ย. 07:08:13`
   (55s apart) -- every real pair found was under 60 seconds apart.

Matched by time-proximity, within `TH_MATCH_TOLERANCE_SECONDS = 300`
(`src/fetchers/news.py`; 300s is ~5x every real gap observed, still far
tighter than a "same day" window), attempted for **every** entry now (the
2026-07-18 redesign dropped the old "only if a confident title-match
already upgraded the Steam link" gate -- every entry already *is* a GGG
thread by construction, there's nothing left to gate on). **Strict per the
project owner's instruction**: if more than one Thai candidate falls within
that tolerance, the match is ambiguous and `link_th` is left out entirely
-- never a guess ("จับคู่ไม่มั่นใจ = ไม่ใส่ field เด็ดขาด" — an uncertain
match must never produce a field, a wrong Thai link is worse than no Thai
link). PoE2's Thai patch-notes category sometimes merges multiple English
hotfix threads into one combined Thai thread (survey finding) -- this can
validly result in two different English entries pointing at the same
`link_th`, which is correct (both hotfixes really are covered by that one
Thai thread), not a bug.

#### Required deploy order (binding, 2026-07-18)

The id-space break (bare Steam `gid` -> `"forum-{thread_id}"`) is
consumer-breaking: the Discord bot's production `seen.json` keys on the old
id scheme. If `{game}/news/latest.json` flips to schema v2 in production
before the bot has migrated, the bot's own dedup behaves unpredictably and
risks posting its entire visible backlog into the channel. Per the project
owner's explicit instruction, the rollout **must** happen in this order,
not "deploy whenever the hub code is ready":

1. Hub code for this redesign is merged/present on the VM, but the running
   process is **not** restarted yet -- production `{game}/news/latest.json`
   still publishes the old Steam-sourced v1 shape.
2. Bot repo is updated to read the new `forum-{thread_id}` id space, with
   backlog protection confirmed working (deploy-time: mark everything on
   the current listing as seen without posting; only post threads that
   arrive after that point).
3. Bot change deploys to production.
4. Only then does the hub process actually restart, switching
   `{game}/news/latest.json` to schema v2 in production.
5. Watch the bot's first cycle after step 4: it must stay quiet (everything
   currently listed gets silently marked seen) -- no backlog flood. This is
   the go/no-go check for the whole rollout.

All pre-rollout end-to-end testing of the new pipeline (including the
"0.5.4c Patch Notes" live-verification case) targets a local file or a
scratch Firebase Storage path (e.g. `poe2/news/test_v2.json`), never the
real `{game}/news/latest.json`, until step 4 above.

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

### 8.6 `{game}/gems.json` (`{game}` = `poe1` | `poe2`, skill-meta phase 1)

Skill/support gem name + metadata table, used to normalize a character's
main-skill name before counting mod/passive meta per skill (skill-centric
meta work, phase 2+). Two sources per game, combined by
`tools/refresh_gems.py`:

- **RePoE fork** (`raw.githubusercontent.com/repoe-fork/{poe1,poe2}` --
  the same community datamine `PoE_Price-Trade_Checker/tools/build_mod_meta.py`
  already pulls `base_items.json`/`mods.json` from; `skill_gems.json` for
  poe2, `gems.json` for poe1 are the two gem-specific files neither project
  had touched before this). Primary source: display name, tags,
  is_support/gem_type, and (poe1 only) the `discriminator` field
  (`alt_x`/`alt_y`) that identifies transfigured variants, e.g. `IceNovaAltX`
  -> "Ice Nova of Frostbolts". **PoE2's RePoE data has zero entries with a
  `discriminator` field** -- confirmed empirically 2026-09-15, not missing
  data: PoE2 has no transfigured-gem mechanic in the current game.
- **GGG's own `trade(2)/data/items` "Gems" category** -- name-only list
  (`{"result": [{"label": "Gems", "entries": [{"type": "<name>"}, ...]}]}`),
  used *only* as a `verified_tradeable` cross-check flag, never as a filter.
  Confirmed live 2026-09-15 that this list does **not** expose transfigured
  gem names at all (0/217 PoE1 transfigured display names from RePoE
  matched) -- treating absence here as "drop it" would silently delete
  every transfigured gem PoE1 has, so every RePoE-reported gem is kept
  regardless of this flag's value.

```json
{
  "schema_version": 1,
  "generated_at": "2026-09-15T03:00:00Z",
  "gems": [
    {
      "id": "IceNovaAltX",
      "display_name": "Ice Nova of Frostbolts",
      "is_support": false,
      "gem_type": null,
      "tags": ["cold", "spell", "area"],
      "transfigured_of": "Ice Nova",
      "is_lineage": false,
      "verified_tradeable": false
    }
  ]
}
```

- `id`: the RePoE dict key (stable identifier, not always equal to
  `display_name` -- e.g. transfigured variants).
- `is_support`: `true`/`false` both games (derived from `gem_type ==
  "support"` on poe2, RePoE's own `is_support` on poe1).
- `gem_type`: poe2 only (`"active"` | `"support"` | `"spirit"`); always
  `null` for poe1 (RePoE's poe1 data has no equivalent field).
- `transfigured_of`: poe1 only -- the base skill's `display_name` when this
  entry is a transfigured variant (`discriminator` set), else `null`.
  Always `null` for poe2 (no transfigured mechanic yet).
- `is_lineage`: poe2 only -- RePoE's `is_lineage` flag, confirmed to match
  the same gems the hub's `scout_prices` fetcher already publishes under
  the `LineageGem`/`SkillGem` price category (spot-checked against
  "Amanamu's Tithe" 2026-09-15) -- lets a consumer join gem metadata here
  against price data already in `{game}/prices/latest.json` by name.
  Always `false` for poe1 (no Lineage-gem mechanic there).
- `verified_tradeable`: `true`/`false` when the trade-items cross-check
  ran this cycle, `null` when it failed/was unavailable (non-fatal, spec
  10.1b) -- **never used to drop a gem**, see above.
- Filtered out before publish: `[DNT]`/`[DNT-UNUSED]`-prefixed placeholder
  entries (both games), poe1's `*Royale` key duplicates (defunct PvP mode,
  confirmed zero equivalent in poe2), and any entry with no display name at
  all in either RePoE field (internal-only triggered/secondary gems never
  obtainable as a real item, e.g. poe1's `SupportCurseOnHitCurse`).
  Deliberately **excluded**: RePoE's per-gem `per_level`/`stat_conversions`/
  tooltip bulk (poe1's raw `gems.json` is ~41MB with it) -- out of scope for
  skill/gear/jewel meta normalization, and republishing that much data
  weekly would reproduce the exact Firebase egress cost problem prices
  already hit once (see `config.yaml`'s `schedule.prices_interval_minutes`
  comment).

**Two-hop publish** (same reason `internal/stats_cache.json` exists, 8.3
below): `trade(2)/data/items` is blocked from GCP IPs the same way
`trade(2)/data/stats` is (confirmed live 2026-09-15: 200 from a dev
machine, would 403 from the deploy VM), so `tools/refresh_gems.py` runs
from `refresh-stats.yml`'s GitHub Actions job, using the same
`internal/`-IAM-scoped `stats-refresh` service account (CLAUDE.md) -- which
therefore *cannot* write the public `{game}/gems.json` path directly. It
stages the finished, already-filtered, already-small result at
`internal/gems_normalized_{game}.json` instead (mirrors
`internal/stats_cache*.json`'s pattern exactly); `src/main.py`'s
`run_gems_once` job, running on the VM with full-access credentials, copies
that staged file to the public path on its own daily schedule
(`schedule.gems_cron_hour_thailand`, default 03:00 ICT). Staging the
*normalized* result rather than the raw RePoE response also means this
VM-side hop never has to download/parse poe1's ~41MB raw file -- all the
heavy lifting happens on GitHub's runner, not the RAM-constrained VM (see
CLAUDE.md's VM RAM budget note). `Cache-Control`: `no-cache` on the
internal staging copy, `max-age=3600` on the public file (same as meta).

Same stale-while-error posture as the rest of the pipeline, at the
whole-file level: if a game's RePoE fetch fails or its filtered gem count
drops below the sanity floor (`MIN_GEMS`, `tools/refresh_gems.py`), that
game's staging copy is left unchanged and `run_gems_once` skips
republishing rather than overwriting a good public copy with nothing. If
the staging copy has never been populated at all yet for a game (e.g.
before `refresh-stats.yml` has run once since this feature merged --
**same "Deploy gotcha" class of issue as 8.3**: manually trigger `gh
workflow run refresh-stats.yml` after deploying, don't wait for the weekly
cron), `run_gems_once` logs a warning and skips that game for the cycle.

### 8.7 ตำแหน่งไฟล์บน Firebase Storage

```
poe-data-hub/
├── poe2/prices/latest.json
├── poe2/prices/index.json
├── poe2/prices/events/{sanitized-league}.json   (0..n, only while an event is active)
├── poe2/prices/hardcore/latest.json             (only while an HC variant is found)
├── poe2/meta/latest.json
├── poe2/meta/2026-07-08.json
├── poe2/news/latest.json
├── poe2/gems.json                                (skill-meta phase 1, 8.6)
├── poe2/aliases/latest.json                       (rendering-alias contract, 8.12)
├── poe2/passives/latest.json                     (skill-meta phase 3 path A, 8.8)
├── poe1/prices/latest.json
├── poe1/prices/index.json
├── poe1/prices/events/{sanitized-league}.json   (0..n)
├── poe1/prices/hardcore/latest.json             (only while an HC variant is found)
├── poe1/meta/latest.json                        (phase 10)
├── poe1/meta/2026-07-15.json                    (phase 10)
├── poe1/news/latest.json
├── poe1/gems.json                                (skill-meta phase 1, 8.6)
├── poe1/aliases/latest.json                       (rendering-alias contract, 8.12)
├── poe1/passives/latest.json                     (skill-meta phase 3 path A, 8.8)
├── mtx/latest.json
├── internal/stats_cache.json                    (PoE2 trade2 stats, weekly refresh)
├── internal/stats_cache_poe1.json                (PoE1 trade stats, weekly refresh, phase 10)
├── internal/gems_normalized_poe2.json            (skill-meta phase 1, staging copy, weekly refresh)
├── internal/gems_normalized_poe1.json            (skill-meta phase 1, staging copy, weekly refresh)
├── archive/poe2-{league-slug}.json               (skill-meta phase 5, per-league, indented)
├── archive/poe1-{league-slug}.json               (skill-meta phase 5, per-league, indented)
└── status.json
```

`Cache-Control`: `max-age=300` สำหรับ prices/index/events, `max-age=3600`
สำหรับ meta, gems, passives, และ archive, `max-age=120` สำหรับ news/mtx,
`no-cache` สำหรับไฟล์ใต้ `internal/`.

#### Job cadence (phase 10: PoE1 meta added to the same nightly slot)

`run_meta_once` loops `game_versions` (poe2 then poe1) **sequentially in one
job invocation**, same `schedule.meta_cron_hour_thailand` cron slot as
before (default 02:00 ICT) -- not two separate schedules. One game's
failure never blocks or corrupts the other's publish (each game's whole
pipeline runs in its own try/except in `run_meta_once`); one Layer-1
healthchecks.io ping fires only after *both* games have been attempted
(task 7: a failure in either game must be visible, not maskable by the
other game happening to succeed).

Real measured wall-clock time and peak RSS, production VM. PoE2's number is
from the 2026-07-15 02:00 ICT (19:00 UTC) scheduled run. PoE1's is from a
manually-launched `--run-meta-once` at 2026-07-16 21:19 ICT (14:19 UTC),
run immediately after the post-incident service restart but ~16h after the
2026-07-15 22:28 UTC incident actually finished -- well outside the 1-2h
poe.ninja ban-window guidance, so this is the first clean, uncontended
measurement for both games (see the job-collision incident below for why
the first PoE1 attempt didn't produce a clean number). Job-lock held for
the whole invocation (`data/locks/meta.lock`), `running_jobs` empty
immediately after, zero `characters_failed` for PoE1, no 429s in
`data/hub.log` for the run -- nothing contended this measurement:

- **PoE2**: started 19:00:00 UTC, published 19:44:13 UTC -- **44m13s**,
  peak RSS **197.4 MB**, 1056 mods (3518 before the count>=5 filter).
- **PoE1**: started 14:59:36 UTC, published 15:43:17 UTC -- **43m41s**,
  peak RSS **118.5 MB**, 1404 mods (6182 before the count>=5 filter).
  Combined poe2+poe1 in one invocation: **84m18s** end to end (started
  14:18:59 UTC, both published by 15:43:17 UTC) -- comfortably inside a
  nightly maintenance window, as expected (8.10).

PoE2's clean number came in noticeably above the pre-phase-10 ~40min
estimate/baseline (197.4MB peak RSS is also higher than earlier informal
poe2-only runs) -- both are plausible normal day-to-day variance (sample
composition, character count) rather than a regression, but worth
rechecking against a few more days of real runs now both games are live.
PoE1 came in slightly faster than PoE2 despite publishing more mods (1404
vs 1056) and a much higher raw pre-filter count (6182 vs 3518) -- lower
peak RSS too (118.5MB vs 197.4MB) -- plausible given PoE1's simpler slot
set (12 slots fetched this run vs PoE2's 14, no focus/charm) rather than a
concern. The roughly-2x combined total (84m18s vs PoE2-alone's ~40min
baseline) matches the pre-phase-10 expectation.

**Open item, separate from timing**: this same PoE1 run's `unmapped_mod_pct`
was **10.27%**, over twice PoE2's own 2.2-2.3% and above the 5% alert
threshold (logged to `status.json.errors` as `meta(poe1)`). Consistent with
the known late-league/event data bias already flagged (8.2) rather than a
pipeline bug -- worth rechecking once the new PoE1 league starts and
Ancestors-specific gear drops out of the sample, not fixed here.

#### Meta job collision incident + fixes (2026-07-15)

While capturing the numbers above, a manually-launched `--run-meta-once`
retry collided with the already-running scheduled cron (see CLAUDE.md for
the full root-cause writeup -- both independently hammered poe.ninja and
triggered its server-side rate limiting for 2+ hours before being caught
and fixed by a service restart). Three permanent fixes landed as a result,
all covered by tests (`tests/test_job_lock.py`, `tests/test_rate_limiter.py`,
plus additions to `tests/test_http_client.py` and `tests/test_main.py`):

1. **`src/utils/job_lock.py`** -- a cross-process filesystem lock
   (`data/locks/{job_name}.lock`). `run_meta_once` acquires the `"meta"`
   lock before doing any work; since the scheduler and the manual CLI both
   call this same function, a second concurrent attempt from either source
   now skips cleanly and logs `meta job: skip -- previous run still active`
   instead of colliding. Stale locks (holder pid no longer alive) self-clear
   rather than jamming the job forever after a crash.
2. **`RateLimiter.note_retry_after(host, seconds)`** -- a 429's
   `Retry-After` header now pushes that host's next-allowed time out for
   *every* caller sharing the same `RateLimiter` instance, not just the one
   that received the 429.
3. **`src/main._build_http_client`** now returns one `RateLimiter` shared
   by every job in the process instead of a fresh instance per job call --
   without this, fix 2 has no one else to actually broadcast to.

Also added: `status.json`'s new `running_jobs.{job_name}` field (pid +
`started_at`), so a human can check what's currently running before
launching a manual CLI retry instead of guessing from `ps`/journalctl --
additive, no `schema_version` bump (this file has never had one; see 8.7).

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
  **2026-07-19: automated** -- see "Automatic league-start mode" below;
  this knob stays as the manual escape hatch.

No dry-run against a hypothetical "next league" was possible (it doesn't
exist yet), but every piece was independently live-tested against Mirage's
real data this phase: league detection, league dates (including the actual
end-of-league post), and the full builds→chars→normalize→aggregate→publish
pipeline end-to-end (see the Status entry below).

#### Automatic league-start mode (`schedule.league_start_mode`, 2026-07-19, per-game auto-detection 2026-07-20)

The manual knob above required someone to edit `config.yaml` and restart
the service right when a league launches, then remember to revert it a few
weeks later. `league_start_mode` automates both ends -- and as of
2026-07-20, can derive the date itself per game from GGG's own Steam News
announcements, so most leagues need zero manual config at all:

```yaml
schedule:
  league_start_mode:
    meta_runs_per_day: 4
    auto_end_date_offset_days: 21   # derived end_date = detected launch + this
    poe1:
      start_date: "2026-07-25"      # manual -- always wins over auto
      end_date: "2026-08-15"
    poe2: "auto"                    # or omit the poe2 key entirely -- same thing
```

**Per-game, manual always wins over auto.** Each game's value under
`league_start_mode` is either a manual `{start_date, end_date}` dict or the
string `"auto"` (equivalent to omitting the game's key entirely).
`_resolve_league_window` (`src/main.py`) resolves each one independently;
iterates `cfg["game_versions"]`, not a hardcoded poe1/poe2 pair, so a
future third game gets auto-detection for free with zero code changes.
league_start_mode is **active if any configured game's window contains
today** -- union, not per-game gating of the meta job itself, same
precedent the original manual knob established (one meta invocation
covers all games, so a league-start bump for either speeds up everyone's
data collection together).

**Auto-detection**: `LeagueDatesFetcher.find_upcoming_launch()`
(`src/fetchers/league_dates.py`) -- distinct from that class's existing
`fetch()`/`_find_start`, which only matches a "now available" post for an
*already-known* league name (i.e. only detects a launch retroactively, on
or after the day it happens). `find_upcoming_launch` instead searches
every official Steam post's title *and* body for a
`(?:launch(?:es|ing)?|releases?)\s+on\s+<Month Day>` phrase (case
insensitive) -- validated live 2026-07-19/20 against real historical posts
spanning multiple past league launches for both games:
- PoE1's real title: `"Path of Exile: Curse of the Allflame Releases on
  July 24 PDT"` -- date in the title.
- PoE2's real body: `"...Return of the Ancients officially launches on
  May 29 (PDT)..."` -- that post's own title, `"Return of the Ancients
  Launches Soon"`, has no date at all, confirming body search is required,
  not just title.
- Confirmed to correctly **exclude** real look-alike false positives that
  don't use the verb phrase: `"Play the Return of the Ancestors Event on
  June 25th PDT"`, `"Zizaran's Class Gauntlet Event Starts on April 8th"`,
  `"More ExileCon Tickets Available On May 17th PDT"`, `"Watch GGG Live on
  May 7th PDT"` -- none of these matched during validation, no denylist
  needed on top of the verb-phrase specificity.

Only keeps candidates where the parsed date is on/after the post's own
date (a genuinely forward-looking announcement, not a retrospective
mention), and picks the single most recent qualifying post -- self-corrects
if a later post restates/corrects the date, and naturally resolves to
whichever league most recently launched or is about to when the feed spans
multiple league cycles (confirmed against real multi-cycle sample data,
not just the latest one).

`status.json.league_start_mode_detail.{game}.source` reports which of five
states each game resolved to: `"manual"`, `"manual-malformed"` (bad manual
config, fell through to auto for that game, logged at ERROR),
`"auto"` (a current, still-relevant detection), `"auto-stale"` (Steam news
still has an old "launch on <date>" post, but its entire derived window --
including the offset buffer -- has already passed; functionally identical
to not-found, kept as a distinct label purely so a human reading
`status.json` doesn't mistake stale history for a fresh detection), or
`"auto-not-found"` (no qualifying post at all right now -- the normal state
for most of a league's life). **Checked 2026-07-19: PoE2 has not yet
announced its next league** (Steam feed had only patch notes/build
showcases), so `poe2` currently resolves to `"auto-not-found"` in
production -- this ships the detection capability itself, which will
resolve to `"auto"` automatically the moment GGG posts the announcement,
no deploy needed.

- **Asia/Bangkok, explicit on purpose** -- `start_date`/`end_date` are
  compared against `datetime.now(ZoneInfo("Asia/Bangkok")).date()`, never
  server-local or UTC. Learned from the `momentTimezone` drift lesson (8.4):
  never assume a bare date/time means what the reader's own locale assumes
  -- state the timezone explicitly in code, not just in a comment.
  `_compute_meta_trigger` (`src/main.py`) takes an optional `today`
  parameter purely for tests, defaulting to the real current date.
- **Checked hourly, independent of the meta job's own cadence** (new
  `league_start_mode_check` scheduler job, `IntervalTrigger(hours=1)`) --
  deliberately *not* "check only when the meta job is about to run", which
  would leave up to a full day of detection lag while still on the
  once-daily 2am cron leading up to a league (it wouldn't re-fire until the
  next day's slot). Also checked once directly at scheduler startup, so
  `status.json` reflects the correct state immediately on boot rather than
  waiting up to an hour. `LeagueDatesFetcher`'s existing 12h TTL cache
  (unchanged, reused as-is) keeps the hourly check from hitting Steam News
  more than twice a day per game regardless.
- **Live reschedule, no restart**: `scheduler.reschedule_job("meta", ...)`
  changes the running APScheduler job's trigger in place. This only affects
  the *next* fire-time computation -- it cannot touch an in-flight
  invocation, and `_run_meta_once_locked`'s existing job_lock (9's
  concurrency-safety fix) already prevents any overlap regardless of when a
  reschedule happens to land relative to a running cycle.
- **Falls back gracefully, never crashes the scheduler**: a malformed
  manual block (bad date string, `start_date` after `end_date`, missing
  key) logs an ERROR (loud, not silent -- a config typo should be noticed)
  and falls back to auto-detection for that specific game; a failed
  auto-detection attempt (network error, unexpected Steam response shape)
  logs a WARNING and resolves to `"auto-not-found"` for that game rather
  than raising. Either way the scheduler itself never crashes over this.
- **Visible, and a wrong auto-detection is bounded and noticeable by
  design**: every actual switch logs INFO with the old/new state, the new
  trigger, and the full per-game detail; top-level `status.json.league_start_mode`
  is `"active"`/`"inactive"` at all times (updated on every state change
  and once at startup), and `status.json.league_start_mode_detail` gives a
  human something concrete to check against if the mode is ever active
  when it shouldn't be. Per the project owner's explicit acceptance: if
  Steam-based date detection is ever wrong, the only real-world consequence
  is the meta job running more frequently than needed in the wrong window
  -- never wrong *data* -- and that's exactly the failure mode this
  visibility is meant to surface quickly. No `heartbeat.yml` change needed
  -- it's scheduler bookkeeping, not a data-fetching job, and the existing
  `META_MAX_AGE_HOURS = 26` tolerance (spec 9's Layer-2 check) already
  covers the meta job itself at any active frequency.
- **Day-1-of-league data gap**: confirmed by reading the code, not assumed
  -- a brand-new league can have zero characters indexed on poe.ninja yet
  for the first few hours, and this does *not* crash the meta job.
  `compute_archetype_distribution` has an explicit `if total == 0: return []`
  guard (`src/processing/aggregator.py`), and the rest of
  `_run_meta_for_game`'s aggregation/publish path tolerates an empty
  `by_slot` cleanly -- it publishes a valid but near-empty snapshot for that
  cycle rather than crashing or reusing a true stale-while-error path
  (nothing raised to trigger that). A pre-existing pipeline characteristic,
  not something this feature changes; the next scheduled run (every 6h
  during league-start mode, not 24h) retries automatically.

#### Job cadence (phase 8, updated 2026-07-19 for the GGG-forum redesign + listing-coverage fix)

News + mtx run as **one job** (`run_news_once`, `schedule.news_interval_minutes`,
default **5 minutes** -- approved cadence) since both are cheap, time-sensitive,
and share no fetch cost worth separating: per game, up to 4 English GGG
forum-listing scrapes (poe1: `news`/`patch-notes`/`poe-press`/`dev-manifesto`;
poe2: `2211`/`2212`) + up to 3 Thai forum-listing scrapes (`link_th`; poe1:
`news`/`patch-notes`/`1010`; poe2: `2282`/`2283` -- see "Listing coverage"
above), **plus one thread-detail-page fetch per genuinely new thread**
(2026-07-18: content is now scraped from each thread's own page rather than
coming from Steam). Steam's own API call is gone entirely. Steady-state
this is 0-2 new threads per game per 5-minute cycle in practice, so the
added cost is 0-4s at the `www.pathofexile.com` rate limit (2s/request) --
negligible. **One-time exception**: the very first run after a *new
listing URL* is added has no `previous_entries` for that category's ids
yet, so every thread currently on that listing's page 1 looks "new" and
gets its content scraped -- bounded, one-time only per category added, not
recurring. **Confirmed live**: the 2026-07-18 redesign's first-cycle burst
was 90 thread-detail fetches combined (60 poe2 across both listings + 30
poe1), larger than an earlier ~40 estimate -- real listing pages held more
threads per page than the spot-check that produced that number. At
~2s/thread this took a few minutes for that one job run (still well inside
the 5-minute cadence, just a longer single cycle), and the very next cycle
dropped to 0 new fetches as designed. The 2026-07-19 listing-coverage fix
(adding `patch-notes`/`poe-press`/`dev-manifesto` for poe1) triggers the
same one-time burst pattern again for poe1 specifically on its own next
deploy -- expected, not a regression. Plus one `pathofexile.com` homepage
scrape for deals (`src/fetchers/deals.py`).
Hosts already covered by existing `rate_limits` entries
(`www.pathofexile.com`, `th.pathofexile.com` -- `th.` is a distinct
rate-limit bucket from `www.`, added phase 11) -- negligible next to the
prices job's own budget (see the rate-limit subsection above), no separate
analysis needed.

**Prices cadence** (`schedule.prices_interval_minutes`): 20 min originally,
bumped to 180 (2026-08-02) then to **1440 = once/day** (2026-09-10), both
times to cut the asia-southeast1 inter-region egress from republishing the
~14MB `poe1/prices/latest.json` (and, at 1440, to drop the ~80MB
prices-poe1 memory spike on the 955MB e2-micro from 8x/day to once/day --
see the config comment). The
rate-limit headroom analysis below (written when the cycle was 20 min) only
gets more comfortable at a daily cadence. The scheduler passes
`next_run_time` on the prices job so a deploy/restart publishes immediately
rather than waiting a full day. Layer-2 `PRICES_MAX_AGE_MINUTES` in
`tools/heartbeat_check.py` and the healthchecks.io Period/Grace for
`prices_ping_url` must both be widened in lockstep with this value.

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

### 8.8 `data/archive/{game}-{league-slug}.json` (skill-meta phase 5)

One file per league, kept on the VM (git-ignored) and best-effort copied to
Firebase at `archive/{game}-{league-slug}.json` (indented, unlike the VM
copy -- see 8.7's Cache-Control note and `src/processing/archive_export.py`'s
module docstring for why). Never deleted, never overwritten by anything
except a fresh export of the *same* league -- reads `mod_counts`/
`skill_mod_counts`/`keypassive_counts` (all kept forever) rather than
`char_items` (90-day retention), so an already-ended league can still be
archived in full months later.

```json
{
  "schema_version": 1,
  "game": "poe2",
  "league": "Runes of Aldur",
  "league_slug": "runes-of-aldur",
  "exported_at": "2026-09-17T04:34:46Z",
  "age_buckets": ["1-21", "22-60", "61+", "unknown"],
  "mod_counts": {"<slot>": {"<archetype>": {"<age_bucket>": [{"stat_id": "...", "mod_kind": "...", "count": 0, "value_min": null, "value_max": null}]}}},
  "skill_mod_counts": {"<slot>": {"<main_skill>": {"<level_bracket>": {"<age_bucket>": [{"stat_id": "...", "mod_kind": "...", "count": 0, "population": 0, "usage_pct": 0.0, "value_min": null, "value_max": null}]}}}},
  "passives": {"<main_skill>": {"<level_bracket>": {"<age_bucket>": [{"keypassive": "...", "count": 0, "population": 0, "usage_pct": 0.0}]}}}
}
```

Only the *time* dimension collapses (into the four age buckets above, from
`days_since_league_start`); skill/slot/level_bracket/stat_id/keypassive stay
fully expanded on purpose -- answering a question next year that nobody
thought to ask today needs the real dimensions, not a pre-aggregated top-N.
`archetype` is intentionally **not** a top-level archive dimension for
`skill_mod_counts`/`passives` (it's a fallback-tier field on those tables,
not first-class -- see local_db.py); it still is for `mod_counts`, where
it's the primary dimension. `mod_counts` rows carry no `population`/
`usage_pct` at all (that table has no population column -- see
archive_export.py). Measured live 2026-09-17 (both games, ~13 real
productive snapshots combined): 1.5MB (poe2) / 2.8MB (poe1) after switching
the VM copy to compact JSON -- see Phase 20/21 below for the full sizing
investigation; not yet measured against a full-length league.

### 8.9 `{game}/passives/latest.json` (schema_version 3, replaced 2026-09-21 -- skill-meta phase 22)

**Notable/keystone passives only -- not the full passive tree.**
poe.ninja's builds-search "keypassives" column + dictionary (confirmed live
2026-09-17) is a ~260-330-entry enumeration covering only notable/keystone-
tier passives; the ~1000+ small stat nodes on the real tree are not in this
data at all. A full-tree "path B" would need `passiveSelection` (real GGG
tree node IDs, already captured verbatim into `char_items.
passive_selection_json` at near-zero cost, unresolved) plus real external
GGG tree data this project doesn't have yet -- see
`src/fetchers/ninja_builds.py`'s module docstring and CLAUDE.md for the
full investigation (including why `passiveSelection` and Path of Building's
own node list disagree for the same character, and why poe.ninja's
dictionary can't resolve `passiveSelection` at all -- different ID spaces).

**Full shape replacement (schema_version 2 -> 3)**, source table changed from
`keypassive_counts` (legacy per-slot, first-gem-guessed skill) to
`skill_passive_counts` (skill-centric collection, 8.10) -- `main_skill`/
`archetype`/`rank_in_bracket` are gone, `skill_type` is new:

```json
{
  "schema_version": 3,
  "generated_at": "2026-09-22T00:00:00Z",
  "game": "poe1",
  "league": "Allflame",
  "days_since_league_start": 58,
  "source": "keypassives (notable/keystone tier only, not the full passive tree); counted per skill-centric list (SPEC.md 8.10) -- a character only counts into the skill(s) it was pulled for, not every skill it happens to run",
  "passives": [
    {
      "skill_type": "main",
      "skill": "Righteous Fire",
      "level_bracket": "90-100",
      "keypassive": "Unwavering Stance",
      "count": 102,
      "population": 173,
      "usage_pct": 58.96
    }
  ]
}
```

Same counting rule as a regular slot's mods: counted once per character even
if (defensively) the same keypassive appears twice in that character's list.
Rows below `skill_meta.publish.min_usage_pct` (percentage of that skill's
population, not a population floor -- the old `min_population_publish` no
longer applies here) are dropped; a skill's passives can be entirely empty
without its `skills/index.json` entry or file disappearing (see 8.11).
`(skill_type, skill, level_bracket)` matches `skill_stat_counts`'s own
dimension (minus `slot`, since passives aren't slot-specific).

### 8.10 Skill-centric collection (2026-09-21; SQLite-side, publish step pending)

Replaces the *skill dimension* of the per-slot collection. Not a published
schema yet -- the publish step (per-skill mods, `passives/latest.json`, the
`skills[]` autocomplete list, archive) lands after the first live run; see
Phase 22 in Status.

**Why.** The 2026-09-21 distribution survey of the per-slot data found: the
median (slot x skill x bracket x archetype) cell held 1-2 characters (98% of
cells < 20), PoE1 returned the *same* ~1,334 characters every run (98% overlap
in 5 days), the "main skill" was the first gem of poe.ninja's skill list (in
real characters routinely a low-DPS utility skill; in PoE2 27% of the sample
had a spirit skill there), and PoE2 characters carry ~6 "main skills" each.

**Method.** poe.ninja's builds-search endpoint filters by skill and returns the
top 100 rows by level:

| query | meaning |
|---|---|
| `search?overview=<snapshot>` (no filter) | the whole-league population `total` **plus a facet table**: every skill with its character count (`skills`, PoE2 also `spiritgems`) and every class (`class`) |
| `&skills=<name>` | "Main Skills" list (both games) |
| `&spiritgems=<name>` | "Spirit Skills" list (PoE2 only) |

The facet table comes free with the unfiltered response, so planning costs one
request. Facets, lists and class names go through the same validated protobuf
path as the per-slot fetch (`NinjaBuildsFetcher.fetch_population_facets` /
`fetch_skill_list`; bad responses dump their bytes to `failed_responses/`).
`min-level` / `max-level` query params exist and work but are deliberately not
used (see Level brackets below).

**A skill's identity is the list a character was pulled from.** Nothing is
inferred from DPS or gem order. `CharacterDetail.archetype_hint` /
`CharacterRef.archetype_hint` (the old first-gem guess, renamed from
`main_skill`) is read only by the legacy archetype bucketing.

**Plan (`src/processing/skill_plan.py`), all thresholds in
`config.skill_meta.collect`:**
- Include a skill when `count / league population >= min_skill_pct` (0.1%).
  The base is poe.ninja's own population (~124k), not the sampled characters.
  (Using the sum of skill uses instead of the population would give 164 / 139
  / 88 skills instead of 171 / 271 / 146 -- the population base was chosen.)
- Main and spirit lists are separate lists under the same rules. They overlap
  (51 names >= 0.1% are in both PoE2 facets, e.g. Herald of Ice) and are never
  merged: dimension `skill_type` = `main` | `spirit`. `gems.json`'s `gem_type`
  is **not** used to decide this -- it flags only 24 of the 450 spirit entries.
- PoE1 Vaal skills ("Vaal Reap") are separate poe.ninja entries. With
  `fold_vaal` both the Vaal and base lists are fetched and their characters
  counted under the base name (`Reap`); a folded skill qualifies on the
  combined count; a member list under `min_member_list_count` is not requested.
- Ascendancy cut: characters of a class with `< min_ascendancy_pct` of the
  population (unascended base classes; PoE1 7, PoE2 8 classes) are dropped from
  the lists. The number of member rows / unique characters cut is logged every
  run and stored in the run stats. Real effect is tiny (1-4 rows) because
  lists are ordered by level.
- The plan (skill list + cut classes) is frozen in `skill_runs.plan_json` when a
  run starts; a resumed run continues that plan.

**Counting (`src/processing/skill_counter.py`).** A character is counted only
into the group(s) it was pulled from -- never into every skill it runs -- so
every skill has a comparable base (~100). Dimensions: `(game, skill_type, skill,
level_bracket, slot, stat_id)`; no archetype split. Everything is per
character, jewels included (deliberately unlike the legacy spec-6.4 per-gem
rule). `population` = characters counted in the (skill, bracket); `slot_holders`
= those wearing >= 1 item in the slot; `usage_pct = count / population`.
Keypassives are counted the same way from the search rows' own `keypassives`
column. SQLite keeps every stat with no count threshold; the publish-time
filter is `skill_meta.publish.min_usage_pct` (0.1%, percentage only -- the old
`count >= 5` / `population >= 20` rules do not apply). With ~100 characters per
skill any stat seen once is already >= 0.5%, so that filter removes nothing
today; it exists for when a skill's base grows.

**Level brackets** stay `28-65 / 65-75 / 75-90 / 90-100`. poe.ninja's index
currently holds only characters of **level >= 80** (its own level facet reports
80-100; `min-level=28..64` returns nothing) and top-100-by-level lists sit at
99-100, so in practice almost everything lands in `90-100` (99.7-100% of list
rows); `28-65` and `65-75` are empty and `75-90` holds only levels 80-89. This
is expected and accepted. **TODO at the next league launch: during its first
1-3 weeks, check whether poe.ninja indexes characters below level 80** (the 80
floor may be league-age dependent, not fixed). The bracket code already handles
low levels; nothing needs to change if it does.

**Data model (`src/storage/local_db.py`).** `char_details` (shared persistent
character cache), `skill_runs` / `skill_run_lists` / `skill_run_members`
(checkpoints), and results `skill_stat_counts`, `skill_passive_counts`,
`skill_populations` (kept forever, like `mod_counts`).

**Incremental + resumable.** A run is Phase A (fetch lists; a list is `done`
atomically with its members), B (fetch the distinct characters into
`char_details`, one commit per character), C (count per skill group, replacing
that group's rows, so it is idempotent). A kill / restart / deploy resumes the
unfinished run (< `resume_max_age_hours` old, same league) without refetching
finished lists or characters. A character is **reused, not refetched**, when the
stored copy is the same league, was fetched when its list row showed the same
level, and is younger than `reuse_max_age_days` (7; a level-capped character can
still swap gear). The row level is compared with the *list row* level stored at
fetch time, not the detail's own level, because the search index can lag the
detail endpoint. The index `version` in every character URL rolls over during a
long run, so the snapshot is re-resolved every `snapshot_refresh_seconds` and
once on a failed fetch. Every attempt logs new / reused / failed characters.
Guards: an empty plan, 0 characters loaded, or 0 characters counted refuses to
run; more than `max_failed_list_pct` failed lists or `max_failed_char_pct`
failed characters aborts with nothing counted and the run left resumable.

**Memory.** The population is never held in RAM: characters go to SQLite one at
a time and counting streams one skill group's ~100 members at a time.

**Disk retention (approved 2026-09-22, after the first live incremental run
measured ~46MB DB growth per run/game).** This dimension is ~10x denser per
snapshot than the legacy archetype one (158-416 skills vs ~8 archetypes), so
"kept forever" (the existing policy for `mod_counts`/`skill_mod_counts`/
`keypassive_counts`) would be ~30GB/year at 1 run/day/game on this VM's 29GB
disk. Two retention rules, `config.yaml`'s `retention.*`:
- **`skill_run_lists`/`skill_run_members`** (checkpoint-only data, needed
  solely to resume an unfinished run) are deleted immediately once a run's
  status becomes `'finished'` (`LocalDb.cleanup_finished_skill_run_checkpoints`,
  called right after `finish_skill_run` in `skill_collect.py`, and again as a
  safety net in the daily cleanup job) -- the single biggest avoidable share
  of this pipeline's growth, since a finished run is never resumed.
- **`skill_stat_counts`/`skill_passive_counts`/`skill_populations`** (the
  actual results) are pruned after `retention.skill_stat_counts_days` (45)
  by the daily cleanup job (`LocalDb.cleanup_old_skill_stat_counts`).
  This is only safe because `archive_export.py`'s `build_league_archive` now
  merges every fresh recomputation against the *previously written* archive
  file leaf-by-leaf instead of blindly overwriting: a leaf whose raw rows
  aged out is frozen at its last-known value, and any *partial* loss (an
  actual bug, not a clean age-out) raises `ArchiveRegressionError` and
  refuses to write rather than silently shrinking a permanent historical
  record -- see that module's own docstring for the full mechanism. This
  retention must never ship without that guard already in place, since a
  league's per-league archive is otherwise fully recomputed from live rows
  on every export.

**First live incremental run (2026-09-22, measured after the two full first
runs).** PoE1: 185 lists (fixed cost, no way to skip an unchanged list) + only
260 of 14,374 characters refetched (97.3% cache reuse) -> 28m39s total
(22.7min collect + 5.7min publishing 158 files), DB +46MB, peak RSS 235MB.
PoE2: 417 lists + 2,001 of 16,628 characters refetched (88.0% cache reuse,
lower than PoE1 -- consistent with PoE2's faster population churn already
seen in the 2026-09-21 distribution survey) -> ~42.9min real work (30.7min
collect + 12.1min publishing 417 files), DB +79.7MB, peak RSS 333MB. Combined
~71.3min/day for both games sequentially -- comfortably inside the approved
1-2h budget (`skill_meta.collect.scheduled: true` since 2026-09-22).

**First-run cost (measured 2026-09-21, 1 request / 2s).** PoE1: 171 lists +
13,780 distinct characters ~ 8.0 h; PoE2: 417 lists + 16,717 ~ 9.8 h. Observed
list stability between index versions: PoE2 lists kept a median 98/100 members
(96.5/100 with the same level), so steady-state runs should refetch only a few
percent plus the 7-day refresh (estimated ~40-60 min per game per run).

**CLI.** `python -m src.main --run-meta-once [--game poe1|poe2 ...]
[--only slot|skill]` (plus `--publish-skill-meta --game G [--game G2 ...]`,
8.11, to republish from the latest finished run without recollecting).

**Scheduling (live since 2026-09-22): two independent daily cron jobs, not
one.** The slot part (`meta`, `schedule.meta_cron_hour_thailand`, 02:00 ICT)
and the skill-centric part (`skill_meta`,
`schedule.skill_meta_cron_hour_thailand`, **05:00 ICT** -- after gems 03:00
and cleanup 04:00, ~21h of headroom before the next day's 02:00 slot run even
on a much slower cycle) each hold the same `job_lock("meta")`, so they still
never race each other or a manual `--run-meta-once`, but they run on their
own schedules and never bundle together. The skill job always runs
`games=["poe1","poe2"]` explicitly (PoE1 then PoE2, sequential, never
parallel -- both peaked over 200MB RSS in the first live runs on this
e2-micro) regardless of `game_versions`' own order. Registered only when
`skill_meta.collect.scheduled: true`; left off the scheduler's job list
entirely otherwise (not just a runtime no-op), so "is it scheduled at all" is
answerable from the job list alone. `--only skill` always runs regardless of
the flag (unchanged).

**Its own healthcheck, independent of `meta_ping_url`
(`healthchecks.skill_meta_ping_url`, Layer 1).** Fires only when both
configured games completed the SAME cycle with `characters_distinct > 0` --
same "never ping a partial/empty cycle" rule `meta_ping_url` already used
(`_all_games_got_real_skill_characters`, mirrors `_all_games_got_real_characters`).
Recommended healthchecks.io settings for this check: **Period 24h, Grace
4h** (covers the ~71min combined measured cost several times over, plus the
scheduler's own `misfire_grace_time=3600` if the process was down right at
the 05:00 ICT trigger). Layer 2 (`tools/heartbeat_check.py`) independently
checks `{game}/skills/index.json`'s own `generated_at` (< 30h old) and
`population_total` (> 0) for both games -- same freshness-plus-content
pairing the meta checks already use, and the same two-independent-layers
reasoning (CLAUDE.md) applies: a bug that leaves Layer 1 pinging "successfully"
while the published content is stale/empty is still caught from the outside.

**Legacy per-slot job -- a temporary bridge.** The per-slot job (top-100 per
equipment slot -> `mod_counts` -> `{game}/meta/latest.json`) still runs because
consumers read that file. It shares the persistent character cache with the
skill-centric job, no longer writes any skill/passive data, and buckets
archetypes with `archetype_hint` (never a "main skill"). **When PoE Checker
moves its mod badge to the per-skill data, retire the per-slot job and the
archetype `meta/latest.json` entirely.**

**Old data is marked, not deleted.** `snapshots.collection_method` records how
each snapshot was collected: every snapshot that existed before 2026-09-21 is
`legacy_slot_top100` (added by migration with that default), skill-centric
snapshots are `skill_top100_v1`. In `legacy_slot_top100` snapshots the
`main_skill` of `char_items`, `skill_mod_counts` and `keypassive_counts` is a
first-gem guess and **not reliable** -- do not build on it. Archive and publish
of per-skill data must read only `skill_top100_v1` snapshots.

**`days_since_league_start` was NULL in every snapshot** (Allflame, Forbidden
Rites): `LeagueDatesFetcher` only recognised a "... Now Available" post, and
neither league had one. It now falls back to a post that names the league and
states its launch ("Releases on July 24", "launching September 4th"), most
recent post wins, forward-looking dates only. Verified against the live Steam
feed: Allflame -> 2026-07-24, Forbidden Rites -> 2026-09-04.

### 8.11 `{game}/skills/*` (skill-meta phase 22, added 2026-09-21)

Published from the skill-centric collection (8.10) by
`src/main.py`'s `publish_skill_meta` (`src/processing/skill_publish.py`
builds the shapes). Never built from a `legacy_slot_top100` snapshot.

**`{game}/skills/index.json`** -- every skill that was collected (main and,
in PoE2, spirit), each with its own base size **even when thin** (2-8
characters is common for a niche/new skill) so a consumer can show how much
to trust a skill's numbers, per the project owner's explicit ask -- nothing
is dropped from this file by population size, only `skill_meta.publish.
min_usage_pct` drops individual mod/passive rows inside a skill's own file:

```json
{
  "schema_version": 1,
  "generated_at": "2026-09-22T00:00:00Z",
  "game": "poe1",
  "league": "Allflame",
  "days_since_league_start": 58,
  "population_total": 124408,
  "skills": [
    {
      "skill_type": "main",
      "skill": "Winter Orb",
      "slug": "winter-orb",
      "facet_count": 9328,
      "populations": {"90-100": 100},
      "path": "poe1/skills/main/winter-orb.json"
    }
  ]
}
```

`facet_count` is poe.ninja's own reported population for that skill (PoE1
Vaal variants folded into their base skill's count, 8.10); `populations` is
this run's actual per-bracket sample size (almost always just `90-100`
today, 8.10's level-80-floor note). Sorted by `skill_type` then `facet_count`
descending. `slug` is the path segment (`skill_slug`/`_unique_slugs` in
skill_publish.py): a plain lowercase-hyphenated form of the skill name, with
`-2`, `-3`, ... appended on the rare collision.

**`{game}/skills/{main|spirit}/{slug}.json`** -- one skill's full mod/passive
breakdown, **compact JSON** (no indentation -- 150+ of these exist per game,
see `PricesPublisher.publish(..., compact=True)`):

```json
{
  "schema_version": 1,
  "generated_at": "2026-09-22T00:00:00Z",
  "game": "poe1",
  "league": "Allflame",
  "days_since_league_start": 58,
  "skill_type": "main",
  "skill": "Righteous Fire",
  "slug": "righteous-fire",
  "facet_count": 7140,
  "populations": {"90-100": 173},
  "mods": {
    "90-100": {
      "gloves": [
        {"stat_id": "implicit.stat_2011785027", "mod_kind": "implicit", "count": 154,
         "population": 173, "slot_holders": 170, "usage_pct": 89.02, "value_min": 1.0, "value_max": 1.0}
      ]
    }
  },
  "passives": {
    "90-100": [
      {"keypassive": "Unwavering Stance", "count": 102, "population": 173, "usage_pct": 58.96}
    ]
  }
}
```

`population` on a mod/passive row equals that bracket's entry in
`populations` (repeated per row, same convention `skill_mod_counts` used);
`slot_holders` is how many of `population` wear >= 1 item in that slot (lets
a consumer compute "% of ring-wearers" instead of "% of the whole skill" if
it wants to). No count-based floor is applied (`skill_meta.publish.
min_usage_pct`, a percentage, is the only filter) -- SQLite's
`skill_stat_counts`/`skill_passive_counts` keep every row regardless.

**`{game}/skills/dictionary.json`** -- the same `stat_id -> template` lookup
as `meta/latest.json`'s own `stat_dictionary`, published once per game
instead of duplicated into every skill file (~1.5MB for PoE1 -- repeating it
150+ times was not an option):

```json
{"schema_version": 1, "generated_at": "...", "game": "poe1", "stat_dictionary": [{"stat_id": "...", "template": "...", "mod_kind": "..."}]}
```

**Cache-Control** (`skill_meta.publish.*_cache_max_age_seconds`, config.yaml):
index 1h, per-skill files 6h, dictionary 24h -- provisional until the
skill-centric job's real update cadence is decided (it is still manual,
`skill_meta.collect.scheduled=false`; see 8.10).

**Read skills through `index.json` only (2026-10-19)**: nothing deletes the file of a skill that dropped out of a
later snapshot (5 PoE1 / 9 PoE2 on 2026-10-19), so the folder can hold stale files that were once correct. Only the
paths listed in `{game}/skills/index.json` are current; never enumerate the folder.

**Retroactive publish without recollecting**: `python -m src.main
--publish-skill-meta --game poe1 [--game poe2]` reads whatever skill-centric
run most recently *finished* for that game and (re)publishes from it --
used to ship a publish-side fix, or to publish for the first time after a
collection run that predates this feature, without repeating an 8-10h
collection (same idea as `run_gems_once` republishing a staged copy).

### 8.12 `{game}/aliases/latest.json` (`{game}` = `poe1` | `poe2`, public contract, added 2026-10-19)

The table a consumer (the PoE Checker) uses to turn an item's mod text into a GGG stat id when the GGG trade
table's single example wording per stat does not match what the game prints: singular/plural, `reduced` instead
of `increased`, a wording without the number, a unit change. It is **generated from RePoE's stat translations**
(the game's own datamine, which lists every wording of a stat with the value condition that selects it and the
GGG trade ids), so which wording belongs to which stat is **read, never guessed**. `src/processing/
rendering_aliases.py` builds it; `src/main.py`'s `run_aliases_once` publishes it.

**URLs** (public, no auth):

- `https://storage.googleapis.com/poe-data-hub/poe1/aliases/latest.json`
- `https://storage.googleapis.com/poe-data-hub/poe2/aliases/latest.json`

**Publishing.** Two hops, same reason and shape as `gems.json` (8.6): `tools/refresh_rendering_aliases.py` runs in
GitHub Actions (`refresh-stats.yml`, weekly + manual) and stages `internal/aliases_public_{game}.json`; the VM's
`run_aliases_once` (daily at `schedule.gems_cron_hour_thailand` + 10 min, and once at start) shape-checks it
(`schema_version`, `game`, `aliases`/`stat_groups`/`stat_aliases` present) and copies it to the public path. A
missing or malformed staged file is skipped with a log line; the previously published file is left untouched
(stale-while-error). The Actions service account is IAM-limited to `internal/` and cannot write the public path.
**Cache-Control `public, max-age=86400`** (24h, the same as `{game}/skills/dictionary.json`): the table changes
only when RePoE does. Compact JSON.

**Versioning contract.** `schema_version` is `1`. Removing or renaming a field, changing what a field means, or
changing the structure bumps it; adding an optional field does not (a reader must ignore unknown fields). A reader
that sees a `schema_version` it does not know must treat the file as absent, not guess.

**Open enums (reader rule).** The values of `aliases[].kinds`, `stat_groups[].kind` and the stat-id prefixes in
`stat_aliases` are an OPEN set: the hub reads three kinds today, but the GGG table also has `crafted`, `enchant`,
`scourge`, `rune`, `desecrated`, ... and kinds will be added WITHOUT a `schema_version` bump. **A reader must skip any
value it does not know (never fail, never treat it as an error)**; adding a kind or an alias is additive, removing or
renaming one is a version bump.

**Permanent coverage limit of PoE2 (not a bug).** An alias exists only where RePoE links a wording to a GGG trade stat
id (`trade_stats` in its translations). For PoE1 that is 7,170 of 11,257 entries; **for PoE2 only 292 of 10,759**
(measured 2026-10-20 on `stat_descriptions.json`; four of the other PoE2 translation files were checked and add 31 more). PoE2 wordings of
the other entries (e.g. "Bow Attacks fire an additional Arrow": in RePoE, but with an empty `trade_stats`) therefore
have NO alias, and 99% of the PoE2 mod texts the hub still cannot map are of this kind. The hub does not fill the
gap by inference: two derivations were tested against RePoE's own ground truth and rejected (hashing the internal
stat id: 0 of 6,946 matches; reusing the PoE1 numeric id of the same internal stat: 22 of 138 conflicting). **A text
that is not in this file is "not known", not a malfunction -- do not guess it** (no fuzzy matching, no borrowing a
stat from another game). The gap closes only if RePoE adds the links.

**Data freshness caveat.** RePoE lags the game by days after a patch (measured 2026-10: PoE2 2-7 days, PoE1 ~0.4
day, from "Export" commits). Early in a league some wordings are therefore missing from this file. **A text the
file does not cover is "not known yet", not a malfunction**: it stays unmapped and appears once RePoE catches up
and `refresh-stats.yml` has run. `generated_at` is the build time, not the RePoE commit time.

```json
{
  "schema_version": 1,
  "generated_at": "2026-10-19T08:56:49Z",
  "game": "poe1",
  "source": {"stat_translations": "https://raw.githubusercontent.com/repoe-fork/poe1/master/data/stat_translations.json", "note": "..."},
  "deprecated_trade_ids": ["2881111359", "3607444087", "4070519133", "562371749"],
  "aliases": [ { "...": "one object per wording, see below" } ],
  "stat_groups": [ { "...": "see below" } ],
  "stat_aliases": { "explicit.stat_2739830820": "explicit.indexable_skill_16" }
}
```

(Measured on the LIVE files 2026-10-20, after the generator started covering `crafted`/`enchant`/`scourge`/`rune`/`desecrated`: PoE1 2,829 aliases / 319 stat groups (295 `identical_text`, 24 `case_only`) / 307 `stat_aliases` / 4 deprecated ids / 763KB; PoE2 132 aliases / 35 stat groups / 35 `stat_aliases` / 40KB; both compact JSON, `Cache-Control: public, max-age=86400`. Aliases per kind, PoE1: explicit 1,821, implicit 574, fractured 589, enchant 1,009, crafted 136, scourge 182; PoE2: explicit 121, implicit 22, fractured 46, desecrated 54, crafted 20, rune 19, enchant 13 (an alias is counted once per kind it applies to). The three original kinds resolve identically to before for all ~35,000 golden texts.)

**`aliases[]` fields** (one object per wording that the GGG table text does NOT already cover):

| field | type | meaning |
|---|---|---|
| `text` | string | the wording the game prints. `{0}`, `{1}` are value placeholders (RePoE style); markup such as `[Keyword\|shown]` is already reduced to the shown text. A text with no placeholder is matched by **string equality** |
| `trade_id` | string | the numeric GGG trade stat id. The stat id of kind `k` is `f"{k}.stat_{trade_id}"` (e.g. `explicit.stat_4077843608`) |
| `kinds` | string[] | the item mod kinds in which this id exists in the GGG table (from 2026-10-20: `explicit`, `implicit`, `fractured`, `crafted`, `enchant`, `scourge`, `rune`, `desecrated` -- in that order, the first three always first; **the set of kinds is OPEN**, see below). Only apply the alias to a mod whose kind is in this list |
| `placeholder_stat_index` | int[] | one entry per placeholder in order of appearance in `text`: which stat's value it shows (index into `conditions`/`negate`/`divisor`/`sign`). Empty for a text without placeholder |
| `negate` | bool[] | per stat: the DISPLAYED number is the negation of RePoE's internal value (`reduced` for `increased`, `slower` for `faster`) |
| `divisor` | int[] | per stat: `internal = displayed x divisor` (before negation). 1 normally; 100 = a percent shown as a fraction; 1000 = RePoE keeps milliseconds, the item shows seconds |
| `sign` | int[] | per stat: `stored = displayed x sign`. **This is the value to store/compare**: it follows the sign convention of the GGG table wording of that stat (so `29% reduced Duration` stores **-29** against the table's `#% increased Duration`) |
| `conditions` | [min,max][] | per stat: the range of the INTERNAL value for which RePoE prints this wording (`null` = unbounded). Singular `[1,1]` vs plural `[2,null]`, `[null,99]` vs `[100,null]` ... A candidate only applies when every stat's internal value is inside its range |
| `implied_values` | number[] \| null | for a `text` with no placeholder: the stored value(s) the wording proves. `[1.0]` for "Gain a Void Charge every second" (1 second, **not 1000**), `[]` when nothing is printed ("Chance to Block is Lucky"), `null` when the entry has placeholders |
| `deprecated` | bool | `trade_id` is deprecated-only: carried by old stats (`old_*`) and by no current stat. Prefer a current candidate over a deprecated one |

Only three shapes of "value implied by a wording without a number" are published (everything else stays out):
one exact value (`[v..v]`), the open-ended top of a pair (`N% chance to X` below 100, `X` at 100), and a
sign-only mirror pair (`Lucky` / `Unlucky`). An unconditional wording whose value is unknown (`[None..None]`,
e.g. "Trigger Level 1 Create Lesser Shrine ...") is **never** published with an invented value.

**How a consumer uses `aliases` (reference implementation: `PublicAliasReader` in `src/processing/
rendering_aliases.py`, tested against the cases below):**

1. For the item's mod kind `k` and text `t`: candidates = every alias with `k in kinds` where
   (a) `implied_values` is not null and `text == t` (value = `implied_values`), or
   (b) `text` with each `{n}` replaced by a signed decimal number matches `t` completely (full match); put the
   shown numbers into per-stat slots via `placeholder_stat_index`, check every stat's
   `internal = displayed x divisor x (-1 if negate)` against `conditions`, and take `stored = displayed x sign`.
2. Group candidates by `trade_id`. If any candidate is not `deprecated`, drop the deprecated ones.
3. Exactly one `trade_id` left: the result is `f"{k}.stat_{trade_id}"` with those stored values. Zero: no match.
   **More than one: ambiguous, treat as NO match** (do not pick one).

**This file is only the LAST rule of the chain.** The hub's `ModNormalizer` (and a consumer that wants the same
answers) tries, per mod text, in this order and stops at the first hit: (1) exact match of the table text with
numbers collapsed to `#` per kind (an explicit `+` on the item tries the template that spells the plus first,
otherwise the plain one first; a singularized retry; the `increased`->`reduced` / `more`->`less` retry with the
value negated); (2) the table's `(Local)` twin **only when no global stat with the same text exists** (an item
text cannot say which); (3) templates with a number baked into the text (`Has 1 Socket`,
`#% increased Damage per 100 Strength`): the fixed numbers must be equal, so `per 100` never matches `per 15`;
(4) a negative roll of a `+#` template (`-30% to Fire Resistance`); (5) singular/plural endings against the
table; (6) **this file**. The whole raw block is tried first, then each line separately; a text that matches
nothing is **unmapped**, which is an expected, normal result (guessing is exactly what this contract exists to
avoid: a fuzzy match was measured to pick the wrong stat 92% of the time where the hub had an answer). If the
chosen stat is deprecated-only and exactly one current stat renders the same text, the current one wins. A
consumer that implements only rule 6 will disagree with the hub on texts the earlier rules handle.

**Rules 1-5 are mirrored in the PoE Checker (`stat_match.ts`, ported from `normalizer.py`; verified 2026-10-19
against the real normalizer on ~120,000 mod texts with 0 differences).** The two implementations must not drift:
**any change to rules 1-5 in `src/processing/normalizer.py` (or to the order of the chain) must be reported to
Winai so the Checker follows in the same cycle**; changing the alias file's format is a `schema_version` bump
(above). Rule 6's data comes from this file, so alias content changes need no code change on the Checker side.

**Drift guard (2026-10-20, automatic).** `tests/golden/{poe1,poe2}/` freezes the table entries, the public alias file and
the normalizer's result for ~35,000 real mod texts; the hub's `tests/test_normalizer_golden.py` fails on ANY change of any
result, so a rules change cannot be committed without a visible diff of `expected.json.gz`
(`python -m tools.update_normalizer_golden` regenerates it from the frozen inputs). The Checker replays the same files in a
scheduled CI job (spec in `tests/golden/README.md`; a fetch failure there is a warning, not a failure).

**`stat_groups[]` and `stat_aliases`** -- stat ids the game cannot tell apart from the item text. The GGG table
gives the SAME text to several stat ids (PoE1: 260 groups) or texts differing only in letter case (24 groups: e.g.
`...supported by Level # ...` / `...Supported by Level # ...`). A consumer that matches by text lands on one member
while the hub's rows (e.g. `meta/latest.json`, `skills/`) may sit under another:

```json
{"kind": "explicit", "reason": "identical_text", "canonical": "explicit.indexable_skill_16",
 "members": ["explicit.indexable_skill_16", "explicit.stat_2739830820"], "text": "+# to Level of all Raise Zombie Gems"}
{"kind": "explicit", "reason": "case_only", "canonical": null,
 "members": ["explicit.indexable_support_106", "explicit.stat_2178803872", "explicit.stat_4197676934"],
 "text": "Socketed Gems are Supported by Level # Chance to Bleed"}
```

- `identical_text`: every member has exactly the same table text. `canonical` is the id the hub emits (last in table
  order, ModNormalizer's rule). `stat_aliases` maps every other member to it: **to look up a hub row for a stat id,
  first replace it by `stat_aliases.get(id, id)`.**
- `case_only`: the texts differ only in case. The hub emits the member whose casing matches the item's text, so rows
  may exist under **any** member and `canonical` is `null`: look all `members` up. These ids are not in `stat_aliases`.
- Groups never span kinds (`explicit.stat_1` and `implicit.stat_1` are different rows).
- The wording of the true duplicates (the same text under two ids that the game itself cannot distinguish, e.g.
  "Impale Enemies on Hit with Attacks" under trade ids 725880290 and 3739863694) is **not** resolved here: the alias
  rule leaves such a text unmapped on purpose.

**Test cases for a consumer's own implementation:** `tests/fixtures/alias_contract/` -- `cases.json` (49 cases,
mod text -> expected `stat_id` and values, or **expected unmapped** with a stability flag), `CASES.md` (the same as a
table), `sample_poe1.json` (ten real entries, one per kind of thing an alias can express, each explained) and
`subset_{game}.json` (the GGG table entries and public aliases the cases depend on; `tests/test_alias_contract_cases.py`
replays them offline through both the hub's chain and `PublicAliasReader`). Categories: exact, `(Local)` suffix,
fixed numbers, singular/plural, `increased`/`reduced`, explicit `+`, text without a number, units (`every second` =
1, not 1000), cluster-jewel notables, case-only stat groups, and must-NOT-map cases (`Mana Burn rate`, `Lesser
Shrine`, `Impale`, `Pierce`, ...). `may_change` marks an unmapped case the hub plans to map later (e.g. shield-local
`+5% Chance to Block`, which needs slot context); `by_design` ones stay unmapped. Regenerate with
`tools/build_alias_contract_fixture.py` from the real stats cache and the staged public files.

**`{game}/skills/` consumers read `index.json`, never the folder listing.** Skill files of skills that dropped out of
a later snapshot are not deleted (5 PoE1 / 9 PoE2 on 2026-10-19) and hold stale numbers; only the files that
`{game}/skills/index.json` lists are current.

### 8.13 League archive: streaming and accumulating (rewrite, 2026-10-28; local commits, not deployed)

Why: `build_league_archive` loaded a whole league into RAM (the 2026-10-15 outage) and the three skill tables are
deleted by retention 45 days after a snapshot, so the `skills` dimension can no longer be recomputed -- it has to be
**accumulated** one snapshot at a time. Code: `src/processing/archive_stream.py` (SQL aggregation, streaming JSON writer),
`archive_skills.py` (state + merge + render + restore), `archive_runner.py` (one league), `tools/prepare_archive_copy.py`.
The old `archive_export.py` is untouched and still what the (disabled) scheduled jobs call.

**Three files per (game, league)** in `data/archive/`:
- `{game}-{slug}.json` -- the file other apps already read: `mod_counts`, `skill_mod_counts`, `passives`, exactly the old
  shape without `skills` (pinned by a test against the old builder's output). Recomputed in full on every run from the
  legacy tables, which retention never touches. Published plain at `archive/{game}-{slug}.json`.
- `{game}-{slug}.skills.json` -- the `skills` dimension, rendered from the state; header carries `render_version` and
  `merged_snapshot_ids`, the end of the file `populations_without_rows` (additive). Published gzip-encoded at
  `archive/{game}-{slug}.skills.json`. Not rendered again when nothing was merged and the file on disk is complete, of the
  current `render_version` and lists exactly the merged ids.
- `{game}-{slug}.skills.state.sqlite3` (+ `.bak`, refreshed before every merge that changes it) -- the accumulation, the only
  copy of snapshots whose raw rows retention deleted. VM only, not published.

**Merge rules:** only `skill_runs.status = 'finished'`; a `running` older than `skill_meta.collect.resume_max_age_hours`
(48) is *treated as* abandoned in memory only (the DB is never written) and reported; sources = live DB then each
`--extra-source` copy, all `mode=ro` and with `snapshot_id` indexes; a snapshot comes from the first source where it is
finished and has rows; a snapshot in two sources must agree on `created_at`, `days` and row counts or the run raises
(`SnapshotConflictError`); finished-but-no-rows-anywhere is `unavailable` (a loss, logged ERROR). One state transaction per
snapshot adds its rows and records its id, so a crash never counts a snapshot twice or half. A state that exists but cannot be
read raises `ArchiveStateError` and is never recreated; a skills file without a state raises `LegacyArchiveError`.
`--rebuild-archive` renames the existing files (and `.bak`) to `*.legacy` (never deleted, never overwritten) and starts over.

**Measured (2026-10-28, real DB read-only, `tools/run_capped.sh` cap 150MB, `nice -n 19`, hub running):**

| | Allflame (PoE1) | Forbidden Rites (PoE2) |
|---|---|---|
| full build on the VM from nothing | 35 snapshots, 968 s, +11.2MB RSS | 33 snapshots, 1732 s, +10.2MB RSS |
| one new snapshot onto a transferred state | 37 s, +14.2MB | 47 s, +13.6MB |
| nothing new (file already current) | 18 s, +9.3MB | 34 s, +9.3MB |
| outputs: base / skills / state | 4.5 / 71.0 / 56.7MB | 3.3 / 122.1 / 99.5MB |
| gzip: skills / state | 6.8 / 8.3MB | 12.7 / 15.0MB |

Peak anon memory of the cgroup was 32-41MB in every run (cap 150MB). Before the "skip render / probe instead of count"
change the nothing-new pass took 217 s / 305 s (hub load differed, so only the order of magnitude is comparable).
Where the full build's time goes (Allflame, VM, hub idle): merge 770 s of 873 s, of which 474 s the process's own CPU
(Python upserts), the rest waiting for I/O; the `-wal` file of the live DB stayed 0 bytes during that run (it was not
measured during the 968 s run, which overlapped a meta job).

**State recovery (cheapest first):**
1. `.bak` beside the state: one merge behind. Protects against a bad merge or a corrupt file, not against losing the disk.
2. `restore_state_from_skills_file(skills.json, state.sqlite3)` (dev machine: it loads the JSON). Rebuilds every count, value
   range and population (including `populations_without_rows`); the per-snapshot metadata (`created_at`, row counts, source)
   is not in the file, so restored ids are trusted as merged and the cross-source conflict check cannot run for them.
   Verified on the real data of both leagues: restore, then an export against a DB that still holds every merged snapshot,
   merges nothing, reproduces the state tables row for row and the skills file byte for byte (apart from `exported_at`).
   A file rendered before `populations_without_rows` existed restores without those populations.
3. A full rebuild from the live DB and the insurance copies, for whatever snapshots are still available.

**First rebuild (plan, not yet done): build on the dev machine, ship the state, let the VM only accumulate.** Never a
full rebuild on the VM (968 s / 1732 s of CPU on a box that serves the Discord bot).
1. Dev, per game: `python -m tools.prepare_archive_copy <backup>.sqlite3.gz <copy>.sqlite3`; record the sha256 of the `.gz`
   and of the prepared copy.
2. Dev: `python -m src.main --export-league-archive <game>:<league> --archive-db <copy>.sqlite3 --archive-dir <dir> --rebuild-archive --no-publish`
   (the copy plays the live DB). Record, for the three outputs, the sha256, and for the state the
   `merged_snapshot_ids`; check that list equals the finished `skill_runs` snapshots of the copy.
3. Ship to a staging directory on the VM (`gcloud compute scp`), never straight into `data/archive/`. On the VM compare
   the sha256 of each file with step 2, and read `merged_snapshot_ids` from the shipped state (`SELECT snapshot_id FROM
   merged_snapshots`) and from the skills file's header: both must equal the recorded list.
4. Keep what is in `data/archive/` (the old 17 September files and any `.legacy`): move the staged files in only if no file of
   the same name exists, otherwise stop and look.
5. On the VM, one league at a time, under `tools/run_capped.sh` with the service untouched: `--export-league-archive
   <game>:<league>` (no `--rebuild-archive`). Expected: `merged` = only the snapshots taken after the copy, `already
   merged` = the copy's count, `unavailable` = `[]`. Anything else: stop, the `.bak` is the state as shipped.
6. Compare the new state's `merged_snapshot_ids` with the shipped list plus the newly merged ones; only then enable publishing.
Rehearsed on 2026-10-28 in a temporary folder on the VM with states built from the insurance copies: the sha256 of both
state files matched after transfer, and the live DB added exactly one snapshot per league (merged in 37 s / 47 s).

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
- [x] Phase 11: Thai forum link (`link_th`, 8.4), from the bot repo's
      2026-07-15 survey (its `HISTORY.md`). Two things confirmed live before
      writing any code (both diverged from the naive "just swap the
      domain" assumption): PoE2's Thai forum categories are different ids
      from English (`2282`/`2283`, not `2211`/`2212` -- the English ids
      return untranslated English content on `th.`), and Steam's
      `published` timestamp is unusable for EN<->TH matching (~7000s gap
      from a real thread's own forum post_date, not a clean timezone
      offset) -- what actually lines up tightly is the English GGG-forum
      thread's own post_date against the Thai thread's own post_date, every
      real pair found live within 60 seconds. `link_th` is therefore only
      attempted for entries with an already-confident `link_is_ggg: true`,
      matched by time-proximity with a strict single-candidate-within-300s
      rule (project owner's explicit instruction: ambiguous = no field,
      never a guess). Purely additive (`link_th` key absent, never `null`,
      when no confident match) -- no `schema_version` bump.
      228/228 tests passing.
- [x] Phase 12: news pipeline redesigned to source from GGG's own forum
      exclusively (8.4, `schema_version` 1 -> 2) -- Steam News dropped
      entirely from `src/fetchers/news.py` (moved off it: `STEAM_APPIDS`
      relocated to `src/fetchers/league_dates.py`, its only remaining
      consumer, an unrelated feature that's explicitly still allowed to use
      Steam per the binding condition in section 1). Trigger: GGG thread
      3985152 ("0.5.4c Patch Notes") never appeared on Steam's feed at all,
      confirmed live 2026-07-18 -- proof Steam isn't a reliable primary
      source. `id` changed from a bare Steam `gid` to `"forum-{thread_id}"`
      (breaking; see 8.4's "required deploy order" callout -- this must not
      reach production ahead of the bot's own `seen.json` migration).
      `content` now comes from each thread's own first post
      (`fetch_thread_content`), scraped only for threads not already present
      in the previous publish's entries (passed into `fetch_forum_news` as
      `previous_entries` -- `run_news_once` already had `fetch_previous`
      available, no new state file needed) -- keeps steady-state cost to
      0-2 thread-detail fetches/game/cycle. Found and handled two real HTML
      layout variants scraping live thread pages: the regular reply layout
      (`td.content-container div.content`) and GGG's own "news"-style posts
      (`<tr class="newsPost"><td colspan="2">`, no `content-container`
      class) -- `fetch_thread_content` scopes to "first `<tr>` in the
      post-list table, then whatever `.content` div is inside it" to cover
      both without hardcoding either. `category` now also forces `"patch"`
      for any thread sourced from PoE2's dedicated 2212 forum, on top of
      the unchanged title heuristic. `link_is_ggg` field dropped (always
      `true` now, dead weight). The whole Steam-title-matching machinery
      (`_resolve_ggg_link`/`_normalize_title`/fuzzy-token matching) deleted
      as dead code -- nothing left to cross-reference once the forum is the
      only source. `link_th` (phase 11) matching logic is unchanged, just
      simpler to invoke (every entry is already a GGG thread, no more
      "only if a confident title-match already upgraded the Steam link"
      gate) -- and slightly more accurate now that it's forum-post_date to
      forum-post_date on both sides.
      **Timezone bug found and fixed before it shipped**: every GGG forum
      page embeds `window.momentTimezone = '<IANA name>';`, and displayed
      `post_date` text is rendered in that timezone, not UTC (confirmed
      live two independent ways -- Steam's real UTC date for "0.5.4b Patch
      Notes" was ~6.5h behind that thread's own forum post_date; the HTTP
      `Date` header vs. an active thread's most recent reply showed a
      consistent ~5h+ gap -- both consistent with the scraped value,
      `Asia/Bangkok`, almost certainly GeoIP-dependent). `published` would
      have silently drifted by several hours if the naive post_date text
      had been stamped with a `Z` suffix directly. `_localize_to_utc` reads
      the tz name fresh from each scraped page rather than hardcoding it,
      using stdlib `zoneinfo` (`tzdata` added to `requirements.txt` for a
      reliable IANA database on Windows). **Rollout is sequenced, not just
      coded and shipped**: per the project owner's explicit instruction,
      the hub restart that actually flips production to schema v2 must wait
      until the bot repo has migrated its `seen.json` and confirmed backlog
      protection works -- see 8.4's "required deploy order" callout, which
      is the authoritative sequence for whoever executes this rollout.
      256/257 tests passing (the one failure, `test_rate_limiter.py`'s
      `test_wait_enforces_interval_between_calls`, is a pre-existing timing
      flake on this dev machine unrelated to this change -- passes in
      isolation, `src/utils/rate_limiter.py` untouched by this phase).
      **Deployed and verified live, 2026-07-18**, following the required
      deploy order above (bot migrated + confirmed first, then hub
      restarted): `poe1|poe2/news/latest.json` in production confirmed
      `schema_version: 2`, `sources.forum`, every id `forum-`-prefixed;
      thread 3985152 present with real content. First post-restart cycle's
      backlog burst was **90 thread-detail fetches** (60 poe2 + 30 poe1),
      larger than this phase's earlier ~40 estimate -- real listing pages
      held more threads than the spot-check that produced that number;
      corrected in the job-cadence note above. Confirmed via
      `data/fetch.log`: burst cycle took ~2min for poe2 (matches the
      2s/thread rate limit), and the very next cycle (5 min later) dropped
      to 0 new fetches, ~16s total -- steady state reached in one cycle, as
      designed. `hc-ping.com` pings confirmed `200 OK` every cycle for 8+
      hours of continuous post-deploy observation, zero news-related
      WARNING/ERROR in that window. Bot side confirmed via the real Discord
      channel (not just logs, which stayed silent throughout -- the bot
      apparently doesn't log routine news polls): 2 posts went out normally
      after the flip, `link_th` resolved for 1 of the 2, no backlog flood.
      **Second timezone finding, worth knowing for next time**: the VM's
      own observed `momentTimezone` was confirmed to *drift over time*, not
      just differ by geography from a dev machine -- `Asia/Bangkok` during
      the actual first post-restart job run, `America/Chicago` (stable
      across 4 repeated checks) about 10 minutes later from the same VM.
      `_localize_to_utc`'s "read fresh per page, never hardcode" design
      handled this correctly in both cases (the published value for
      3985152 mathematically matches the Bangkok conversion the job
      actually observed at run time), but the implication is that
      `published` for an *already-known* thread can shift slightly between
      publish cycles if the hub's observed timezone changes between one
      listing scrape and the next -- not a bug, just a real source-data
      characteristic to be aware of if a consumer ever diffs `published`
      across snapshots expecting it to be perfectly stable.
- [x] Phase 13: PoE1 listing-coverage gap found and closed (8.4's "Listing
      coverage" subsection), one day after phase 12 shipped -- 3.29.0's
      patch notes never reached the feed because GGG posts PoE1 patch notes
      to a dedicated `patch-notes` forum, separate from `news`, and PoE1
      only had `news` configured (PoE2 already covered both its
      announcements and patch-notes forums, `2211`+`2212` -- an asymmetry
      the Steam-era feed never exposed, since Steam merged every category
      into one stream). Root-caused from an actual missed real-world patch
      note, then closed by auditing every category on both games' live
      forum index rather than stopping at the one gap found -- also added
      `poe-press` (genuinely first-party GGG content despite the name; a
      live league-launch announcement was sitting there uncovered) and
      `dev-manifesto` (legitimate first-party, very low frequency, added
      since a quiet category costs nothing extra). Deliberately skipped
      console-specific patch-notes forums (redundant PC-content mirrors)
      and confirmed every other category on both games (trade/PvP/guild/
      bug-reports/discussion, and PoE2's `2213`-`2220`/`2304`/`2381`
      cluster) is genuinely non-news, not just assumed. PoE1's `patch-notes`
      forced-category treatment mirrors PoE2's `2212`; `poe-press`/
      `dev-manifesto` are not forced, their titles already read correctly
      via the default heuristic. Thai counterparts added where they exist:
      `patch-notes` is the same slug on `th.` (confirmed via a real pair,
      EN 3985332 vs TH 3985358, posted the same second); `dev-manifesto`'s
      Thai counterpart is a *different* forum id (`1010`,
      "แถลงการณ์จากทีมพัฒนา", found by browsing th.'s own nav and confirmed
      by real title/date matching, 24s apart); `poe-press` has no Thai
      counterpart at all (checked the full th. nav, doesn't exist) --
      `link_th` simply never resolves for those entries, no special-casing
      needed. 38/38 `test_news.py` tests passing (3 new cases covering all
      4 poe1 categories combined, forced-category behavior, and both real
      confirmed `link_th` pairs), 259/260 project-wide (the one failure is
      the same pre-existing `test_rate_limiter.py` timing flake noted in
      phase 12, still unrelated to this change).
- [x] Phase 14: automatic league-start mode (`schedule.league_start_mode`,
      §9's "Automatic league-start mode" subsection), the last piece needed
      before PoE1's 2026-07-25 league launch. Replaces the manual
      "edit config, restart the service, remember to revert later" workflow
      the existing `meta_runs_per_day_league_start` knob required (that
      knob stays as a manual escape hatch for an unplanned/delayed league).
      Global scope, not per-game, same precedent phase 10 already
      established for the manual knob (`_run_meta_once_locked` already
      covers both games in one invocation). `_compute_meta_trigger`
      (`src/main.py`) computes the correct trigger from today's
      Asia/Bangkok calendar date -- explicit timezone handling on purpose,
      the exact lesson phase 12's `momentTimezone` drift taught (never
      assume a bare date means what the reader's own locale assumes). A new
      hourly `league_start_mode_check` scheduler job live-reschedules the
      running meta job (`scheduler.reschedule_job`, no restart) on any
      state change, also run once at startup so `status.json` is correct
      immediately on boot rather than up to an hour stale. Confirmed by
      reading the code (not assumed) that `_run_meta_once_locked`'s
      existing job_lock (phase 9) fully covers the "meta run stuck across a
      mode-switch boundary" case -- rescheduling only changes a trigger's
      next-fire computation, it can't touch an in-flight run -- and that a
      zero-sample day-1-of-league morning already can't crash the job
      (`compute_archetype_distribution`'s existing `if total == 0: return []`
      guard). Malformed config (bad dates, `start_date` after `end_date`,
      missing keys) logs ERROR and falls back to the manual knob rather
      than crashing the scheduler. 13 new tests (`_compute_meta_trigger`
      boundary/fallback/malformed cases, `run_scheduler` wiring, the
      checker job's reschedule/no-reschedule/status-write behavior, and
      job-lock non-interference) plus one in `test_aggregator.py` for the
      zero-sample guard -- 272/273 project-wide (same pre-existing
      unrelated timing flake).
- [x] Phase 15: `league_start_mode` extended to per-game auto-detection
      (8.4's "Automatic league-start mode" subsection) -- one day after
      phase 14 shipped requiring manual dates, closing the gap for PoE2 and
      every future league on either game without further code changes.
      `LeagueDatesFetcher.find_upcoming_launch()` (`src/fetchers/league_dates.py`)
      is new: unlike that class's existing `_find_start` (only matches a
      "now available" post for an *already-known* league name, i.e. only
      detects retroactively, on or after launch day), it searches every
      official Steam post's title+body for a
      `launch(es/ing)?|release(s)? on <date>` phrase, needing no advance
      knowledge of the league's name. The regex and its "keep only
      forward-looking matches, most-recent-post wins" selection logic were
      validated against real historical posts spanning multiple past
      league launches for both games (not synthetic examples) -- found the
      real title-vs-body split (PoE1 states the date in the title, PoE2
      only in the body -- "Return of the Ancients Launches Soon" itself
      has no date at all) and four real look-alike false positives
      (event/gauntlet/ticket/livestream posts using similar "on `<date>`"
      phrasing) that the verb-phrase requirement correctly excludes without
      needing a denylist. `_resolve_league_window`/`_compute_meta_trigger`
      (`src/main.py`) now resolve a window **per game** (iterating
      `cfg["game_versions"]`, not hardcoded) instead of one global window;
      manual config always wins over auto for that game; active if **any**
      game's window contains today (union, same precedent phase 14's
      manual knob already established). Added a fifth resolution state,
      `"auto-stale"`, distinct from plain `"auto"` -- Steam news can still
      surface an old "launch on `<date>`" post whose entire derived window
      has already passed; functionally identical to not-found
      (`in_range` is always false) but labeled separately in
      `status.json.league_start_mode_detail` so a human doesn't mistake
      stale history for a fresh detection. Checked live 2026-07-19: PoE2
      has not yet announced its next league, so this ships the detection
      capability itself (resolves to `"auto-not-found"` in production
      today) rather than a manually-entered date -- it'll self-populate
      the moment GGG posts the announcement. 6 new tests in
      `test_league_dates.py` for `find_upcoming_launch`'s title/body
      matches, all four real false positives, retrospective-mention
      exclusion, and most-recent-wins selection; phase 14's `test_main.py`
      `league_start_mode` suite rewritten/extended for per-game resolution,
      manual-wins-over-auto, union-active semantics, the `auto-stale`
      distinction, and malformed-manual-falls-back-to-auto-for-that-game --
      281/282 project-wide (same pre-existing unrelated timing flake).
- [x] Phase 16 (skill-meta phase 1): `{game}/gems.json` (8.6) -- skill/support
      gem name+metadata table, combining RePoE fork data (primary,
      `raw.githubusercontent.com`, not GGG, not IP-blocked) with GGG's
      `trade(2)/data/items` "Gems" list (`verified_tradeable` cross-check
      only -- confirmed live it has zero transfigured names, so it's never a
      filter). Two-hop publish (`tools/refresh_gems.py` stages
      `internal/gems_normalized_{game}.json` via `refresh-stats.yml`'s
      `internal/`-IAM-scoped service account, `src/main.py`'s new
      `run_gems_once` VM job republishes to the public path daily) -- caught
      and fixed *before* first deploy that the scoped service account
      can't write outside `internal/` (see CLAUDE.md). Live-verified
      2026-09-15 after full deploy: `poe2/gems.json` 414KB/1120 gems (0
      transfigured -- correct, PoE2 has no transfigured mechanic yet, 80
      `is_lineage`), `poe1/gems.json` 343KB/915 gems (214 transfigured
      confirmed incl. "Ice Nova of Frostbolts", 0 `is_lineage` -- correct,
      no such mechanic in PoE1), both served 200 OK with correct
      `Cache-Control`. Neither monitoring layer flagged anything; VM RAM
      unaffected (86.5MB/peak 90MB right after restart, well inside budget
      -- see CLAUDE.md's VM RAM budget note). 20 new tests
      (`test_refresh_gems.py`) + 3 (`test_main.py`'s `run_gems_once` suite),
      325/325 passing pre-deploy.
- [x] Phase 17 (skill-meta phase 2): a second, orthogonal SQLite counting
      dimension -- `skill_mod_counts` (`game, slot, main_skill,
      level_bracket, archetype, stat_id`), covering gear and jewels,
      alongside (not replacing) `mod_counts`' archetype-only one. Kept
      forever, same policy as `mod_counts`. `archetype` is denormalized onto
      every row (not purely a function of `main_skill` -- `Archetyper`'s
      weapon-attack fallback depends on the character's actual weapon, so
      the same skill can land in different archetypes for different
      characters) so the 4-tier fallback (skill+bracket -> skill ->
      archetype -> all, `LocalDb.query_skill_mod_counts`) is a plain
      aggregation over this one table, no join back to `mod_counts` needed.
      Skill names normalized against `{game}/gems.json` (phase 16) via the
      new `src/processing/skill_normalizer.py` -- exact-string match only
      (confirmed live that poe.ninja's names match RePoE `display_name`
      byte-for-byte), transfigured variants kept distinct automatically (own
      `display_name`), unmapped -> `"unknown"` and logged, never dropped. A
      matched *support*-gem name is also treated as unmapped (a "main skill"
      can never legitimately be one -- signals the upstream socket-group
      pick was wrong for that character, not real data). Level brackets
      configurable (`skill_meta.level_brackets`, default 28-65/65-75/75-90/
      90-100, half-open except the last); a level below the first bracket is
      excluded from this dimension only (still counted normally in the
      existing archetype pipeline). Population = "everyone already in the
      per-slot poe.ninja samples matching this skill+bracket" (gear:
      per-character; jewels: per-gem-instance, spec 6.4) -- explicitly not a
      separately-fetched "true" population (poe.ninja's builds-search has no
      such query), per item 5/7 of the driving conversation.

      **Historical backfill gap, accepted, not fixed** (see CLAUDE.md):
      `char_items.main_skill` was never persisted before this phase even
      though `CharacterDetail.main_skill` was already fetched every run --
      only used transiently for the old archetype bucket, then discarded.
      Every `char_items` row from before this deploy has no recoverable
      skill name (re-fetching now would return characters' *current* skill,
      not what they ran historically -- respec makes that actively
      misleading, not just imprecise), so the skill dimension starts
      accumulating from this deploy forward only, decided explicitly rather
      than attempting an inaccurate re-fetch-based backfill. `char_items`
      itself is unaffected and still holds gear/level data back to the
      start of PoE1's current league (90-day retention) for the *existing*
      archetype-based `mod_counts` dimension.

      `--reprocess-char-items` (`src/main.py`'s `run_reprocess_char_items`)
      recomputes `skill_mod_counts` from already-stored `char_items` rows
      entirely offline (no poe.ninja calls, item 5) -- for when gems.json
      normalization or `level_brackets` changes and needs reapplying
      retroactively across the retention window; only acts on rows with
      `main_skill` populated (silently skips pre-migration rows by design,
      not an oversight). Idempotent per snapshot (deletes + replaces that
      snapshot's rows, never accumulates on repeat runs).

      Deliberately NOT done in this phase: no new public Firebase JSON is
      published yet (`min_population_publish` config exists and is already
      used by `query_skill_mod_counts`'s fallback-tier decision, but nothing
      calls that from a publish step) -- consolidating this into a
      consumer-facing view is skill-meta phase 4's job ("รวมเป็นภาพเดียวต่อ
      skill"), once passives (phase 3) exist too.

      37 new tests: 19 in `test_aggregator.py`, 8 in `test_skill_normalizer.py`
      (new file), 8 in `test_local_db.py`, 2 in `test_main.py`
      (`run_reprocess_char_items`'s recompute + idempotent-rerun behavior).

      **Live deploy verification, 2026-09-15**: pulled + restarted cleanly,
      `--run-meta-once` run manually right after. Both games returned **zero
      characters from poe.ninja's builds-search** this cycle -- coincidental
      timing, not caused by this change: both leagues are brand new
      (poe2 "Forbidden Rites", inside its already-detected
      `league_start_mode` window; poe1 "Allflame", which also means
      `config.yaml`'s stale `poe1_override: "Mirage"` no longer matches any
      active candidate -- a separate, pre-existing piece of league-rotation
      maintenance, unrelated to skill-meta and not fixed here), and
      poe.ninja's build index hadn't caught up to either yet (every
      `ninja_builds` slot request failed the same
      `missing expected valueList(s)` parse check for both games). Confirms
      the new code path runs cleanly end-to-end against live production
      with zero characters (no crash, `skill dimension -- 0 rows, top
      skills=[], 0 thin bracket(s), 0 unmapped` logged correctly, matching
      the same empty-population handling already covered by
      `test_compute_archetype_distribution_empty_refs_returns_empty_list`),
      `SkillNormalizer` loaded real `gems.json` for both games (poe2: 1055,
      poe1: 911 -- both slightly different from phase 16's day-one 1120/915
      since the weekly refresh had run again since; expected drift, not a
      bug), and the SQLite file grew by ~12KB (two new near-empty snapshot
      rows only) -- **but item 14's requested content** (top 20 skills per
      game, population per bracket, thin-bracket list, a meaningful
      duration/RSS delta against real per-character work) **could not be
      produced this cycle, because that data does not exist anywhere yet**
      -- not a gap in this implementation. Re-run `--run-meta-once` (or wait
      for the next scheduled 02:00 ICT cron) once poe.ninja's index has
      real characters for either league to get those numbers; correctness
      in the meantime rests on the 37 passing unit tests against real
      fixture data (RePoE `gems.json`, real GGG stat templates) rather than
      a live content sample.

      **Correction, 2026-09-16**: the diagnosis above ("poe.ninja's build
      index hasn't caught up yet") was wrong -- written from the error
      message's own wording without re-curling the live endpoint to check.
      The real cause, found the next day: poe.ninja replaced the entire
      builds-search wire format around 2026-08-04 (`total` in the response
      was a real nonzero population count the whole time; only the
      character-row data was structurally absent -- a schema change, not an
      empty result). See CLAUDE.md's "diagnose from the real endpoint, not
      the error message" entry and Phase 18 below for the actual fix.

- [x] Phase 18: `ninja_builds` full schema re-reverse-engineering
      (`src/fetchers/ninja_builds.py`, `src/utils/ndic.py` new) after
      poe.ninja's 2026-08-04 wire-format change went undiagnosed for ~43
      days (Phase 17's "index hasn't caught up" note, corrected above).
      Re-derived live via a real browser network capture cross-checked
      byte-for-byte against a from-scratch protobuf field dump: search
      response's valueList array moved from field 5 to field 12 (28 columns
      now, up from 10 -- rate/crit/hit-chance/projectiles/forks/chains/
      splits/pierces/aoe/duration added), each column now self-describes
      which dictionary resolves its indices (field 11) instead of the
      caller having to guess; the `/builds/dictionary/{hash}` endpoint
      stopped being protobuf entirely, replaced by a custom "NDIC"-magic
      binary format (length-prefixed string array after an unneeded
      hash-bucket header region) -- see `src/utils/ndic.py`'s module
      docstring for the full byte-level writeup. `items=` filter param
      confirmed still functional server-side (a false start during this
      same investigation briefly assumed otherwise from poe.ninja's own UI
      not sending it from its default view, and had to be corrected before
      shipping). Confirmed live: no bot/browser-fingerprint distinction on
      either endpoint -- `curl` and a real browser get byte-identical
      responses, so plain `requests`-based `HttpClient` calls work fine in
      production, no browser automation needed at runtime.

      Three hardening requirements from the project owner, all shipped:
      1. **No silent empty result**: `_validate_search_response` now treats
         "0 characters extracted but response `total` is nonzero" as a hard
         `ValueError`, not a quiet empty list -- this exact combination is
         precisely how the 43-day breakage presented. `ndic.decode` raises
         immediately (with the actual magic bytes/version found) on a
         magic-byte or version mismatch, never a silently-wrong dictionary.
      2. **`buildLeagues[].indexed` is logged, never a query gate**:
         checked whether it predicts character-data availability (it
         doesn't -- PoE2's real main league "Forbidden Rites" reports
         `indexed: false` yet returns a full 28-column response, identical
         in shape to `indexed: true` leagues) before deciding how to use
         it; logged purely for visibility since `buildLeagues` comes free
         with the `snapshotVersions` response `resolve_snapshot` already
         fetches (zero extra requests either way).
      3. **Systematic last-known-good raw storage**: `_save_last_good` now
         covers the search response (pre-existing), every dictionary name,
         and the index-state JSON -- not just the search body as before --
         so the *next* format change can be diffed from real bytes
         immediately instead of starting from scratch, the same way
         `last_good_builds_*.bin`'s existence made *this* diagnosis fast
         once someone thought to look.
      Etiquette (poe.ninja is a free community site): per-(snapshot,
      item_filter) results are now cached for the fetcher instance's
      lifetime (previously only dictionaries were); `User-Agent` already
      self-identifies with a contact email (`http_client.py`, unchanged);
      rate limiting unchanged (`rate_limits."poe.ninja": 2`, already shared
      across jobs via `job_lock`/the shared `RateLimiter`).

      Verified against real captured samples for both games
      (`data/samples/ninja_{poe1,poe2}_{builds_search,dict_class,dict_gem,
      index_state}_2026-09-16.{bin,json}`) -- PoE1's schema confirmed
      identical in structure to PoE2's (not re-assumed this time), just
      more dictionaries (15 vs 7 -- bandit/atlasskill/mastery/runegraft/
      tattoo/vestigialmod/pantheon/secondascendancy, PoE1-only systems).
      27 tests (`test_ninja_builds.py` rewritten, `test_ndic.py` new),
      357/357 project-wide passing.

- [x] Phase 19: close the exact monitoring gap Phase 18's incident exposed --
      both layers stayed green for the whole ~43-day breakage because
      neither checked *content*, only that the job ran and the timestamp
      was fresh. Layer 1 (`src/main._all_games_got_real_characters`, called
      from `_run_meta_once_locked` before `_ping_healthcheck`): the meta
      ping now only fires when every configured game's
      `summary.sample_total` is nonzero *and* present (a game missing from
      `results` entirely -- its `_run_meta_for_game` call raised -- also
      blocks the ping, same as a present-but-zero one). Layer 2
      (`tools/heartbeat_check.check_meta_sample_total`): reads
      `status.meta.{game}.meta_job_summary.sample_total` from the public
      `status.json` and fails if it's 0 or missing, independent of Layer 1
      and of `check_meta_freshness`. Investigated first whether
      `pathOfBuildingExport`/Path of Building export data from poe.ninja's
      character-detail endpoint could replace the character-list source
      entirely (project owner's ask, prompted by the Phase 18 incident) --
      concluded C (not worth it): that endpoint is the *same* one
      `ninja_chars.py` already calls today, its JSON already gives
      structured mod arrays PoB's flat text would have to be re-parsed to
      recover, and the wrapper is exactly as much poe.ninja's own surface
      as what already broke. Two things kept from that investigation for
      later phases: `passiveSelection` (already free in the existing
      character-detail response) as the passive-tree data source instead of
      PoB, pending a real check of why it disagreed with PoB's own node
      list for the same character (109 vs 155 nodes, not a subset) before
      trusting either; and storing `pathOfBuildingExport` verbatim
      (compressed, as received, ~12KB/character) in `char_items` at
      near-zero marginal cost, as a ready-made artifact for future
      consumers -- not yet implemented. 10 new tests (`test_main.py`,
      `test_heartbeat_check.py`), 367/367 project-wide passing.

- [x] Phase 20 (skill-meta phase 5): per-league archive export
      (`src/processing/archive_export.py`, new). One file per league --
      `data/archive/{game}-{league-slug}.json` -- not a rough summary: reads
      `mod_counts`/`skill_mod_counts` (kept forever, never cleaned up)
      rather than `char_items` (90-day retention), so a league that ended
      long ago can still be archived in full. Collapses only the *time*
      dimension, into three age buckets (`"1-21"`, `"22-60"`, `"61+"`, plus
      `"unknown"` for the rare snapshot where league-start detection failed
      -- never silently dropped) -- skill/slot/level_bracket/stat_id stay
      fully expanded, on purpose (spec item 20: answering next year's
      not-yet-thought-of question needs the real dimensions, not a
      pre-aggregated top-N). `mod_counts` (archetype dimension) and
      `skill_mod_counts` (skill+bracket dimension, `archetype` deliberately
      *not* kept as an archive dimension -- it's a fallback-tier field on
      that table, not first-class, see local_db.py) are archived
      separately, using the same population-dedup logic as
      `LocalDb.query_skill_mod_counts`'s `_aggregate_rows`. `mod_counts` has
      no `population` column at all (that denominator only ever lived in
      the ephemeral published meta.json), so its archived rows carry
      `count`/`value_min`/`value_max` only -- no fabricated `usage_pct`.
      `passives` is always `null` (Phase 21 fills it in; the field exists
      now so that phase doesn't need a schema_version bump).

      Three triggers, all shipped: (1) automatic on league change --
      `_archive_previous_league_if_changed`, called from
      `_run_meta_for_game` right before inserting the new snapshot, compares
      `LocalDb.most_recent_league(game)` against the just-detected league
      and archives the old one if they differ, before any new data can land
      on top of it; a failure here is logged loudly but never blocks the
      meta run's own insert. (2) automatic weekly --
      `run_archive_export_once`, a new `IntervalTrigger(weeks=1)` scheduler
      job, re-exports whichever league is currently active per game (bounds
      data loss from a VM dying mid-league to at most a week; idempotent
      and cheap, no poe.ninja calls, so no healthcheck ping needed). (3)
      manual -- `--export-league-archive game:league` (e.g. `poe1:Mirage`)
      backfills a specific league, including one that already ended,
      regardless of what's currently active.

      Kept on the VM (`data/archive/`, git-ignored, same as `data/*.sqlite3`)
      and best-effort copied to Firebase at `archive/{game}-{league-slug}.json`
      (spec item 22: "if it doesn't add burden" -- a publish failure is
      logged and swallowed, never fails the export itself, since the VM
      copy is the durable one). Not sent to GitHub yet (item 23's size
      numbers needed first to decide that; not yet measured against a real
      multi-week league). 17 new tests (`test_archive_export.py` new, plus
      `test_main.py` orchestration/scheduler-registration tests), 385/385
      project-wide passing.

- [x] Phase 21: archive size fix + skill-meta phase 3 path A (notable/
      keystone passives), both requested together after live-checking
      Phase 20's real production archive size.

      **Size investigation, 2026-09-17** -- the "3-5.5MB at 13 days -> could
      hit 25-40MB by league end" worry turned out to rest on a wrong
      premise: queried the real VM database and found poe2 "Forbidden
      Rites" had 49 snapshots but only **3** ever produced real character
      data (the schema was broken -- Phase 18 -- for this league's entire
      life until the fix landed), and poe1 "Allflame" had 157 snapshots but
      only **38** productive (35 from *before* the 2026-08-04 break under
      the old schema, 3 from after the fix) -- so the archive's size
      reflects a handful of real snapshots, not 13 (or 54, poe1's real
      elapsed days) days of steady accumulation, and growth from here is
      governed by the skill/stat state-space's cardinality, not wall-clock
      time -- confirmed already sub-linear (poe2's 3 snapshots collapsed
      22,433 raw skill_mod_counts rows to 8,006 distinct). Separately found
      a real, unrelated inefficiency: `json.dumps(indent=2)` was ~49% of
      the file, pure whitespace on a structure nested 4-5 dicts deep with
      thousands of leaf rows. Fixed in `archive_export.py`'s own VM-copy
      write only (`separators=(",", ":")`) -- deliberately did not touch
      `PricesPublisher.publish` (the Firebase-copy path, and every other
      published file project-wide), since that's a shared convention, not
      an archive-specific call. Re-exported both real leagues after
      deploying: poe2 2,952KB -> **1,494KB**, poe1 5,480KB -> **2,825KB**
      (~49% reduction each, confirmed live, zero data difference).

      **Passive-tree investigation, 2026-09-17** (real browser, real
      endpoints -- see CLAUDE.md for the full trail): resolved the Phase
      19 109-vs-155-node puzzle as far as possible without external tree
      data. The live character page shows three genuinely separate
      categories -- main tree, **Ascendancy** (its own 8-node sub-tree),
      and **Quest Reward Choices** (non-tree bonuses entirely) --
      confirmed via `passiveCounts: {"passives": 122, "ascendancy": 8,
      "anoints": 1, "bonusPassives": 0}` on the same character-detail
      response. Neither ninja number matches the other: `passiveSelection`
      (109) isn't even equal to `passiveCounts.passives` (122) in the same
      payload, and PoB's node count (155) exceeds the full official total
      (122+8+1=131) by 24 more -- likely cluster-jewel/timeless-jewel-
      generated or mastery-effect node IDs PoB's tree-state resolution
      pulls in, not confirmable further without real GGG tree data. Tested
      (not assumed) whether the builds-search "keypassive"/"skilltrait"
      dictionaries could resolve `passiveSelection`'s node IDs: **no** --
      proven with ground truth (the character's real keystone "Dance with
      Death" resolves correctly through the *search* endpoint's own
      "keypassives" column + its own small ordinal dictionary, index 59 of
      264, but that dictionary's indices are poe.ninja's own internal
      enumeration, a completely different ID space from `passiveSelection`/
      PoB's real GGG tree node IDs in the thousands-to-hundred-thousands
      range). Conclusion: two viable paths, not one --
      - **Path A** (chosen for this phase): the builds-search "keypassives"
        column, zero extra requests (already resolved onto `CharacterRef`
        in `ninja_builds.py`). Notable/keystone tier only, ~260-330 names.
      - **Path B** (deferred, real future work if small nodes are wanted):
        `passiveSelection`'s real GGG node IDs, needs external tree data
        (~1000+ node ID -> name pairs) this project doesn't have yet --
        comparable in scope to RePoE for skill gems.

      **Path A shipped**: `CharacterRef.keypassives` (new field, resolved
      in `ninja_builds.py` exactly like `main_skill` already was, from the
      same search response -- `_resolve_multi_dict_column` generalizes
      `_main_skill` to return every index, not just the first). New
      `aggregate_keypassives_by_skill` (aggregator.py) counts per
      `(main_skill, level_bracket, archetype, keypassive)` -- same shape as
      `skill_mod_counts` minus `slot` (passives aren't slot-specific) --
      using `get_detail` only for level/skill/archetype (already being
      fetched for the existing skill dimension; the keypassives themselves
      cost nothing extra). New `keypassive_counts` SQLite table (kept
      forever, same policy as `mod_counts`/`skill_mod_counts`). Published
      to the archive's now-real `passives` field (8.8) and a new
      `{game}/passives/latest.json` (8.9), both explicit in-file that this
      is notable/keystone-only, never the full tree, so this doesn't get
      mistaken for more than it is later.

      Also stored (per project owner's request, near-zero marginal cost):
      `passiveSelection` verbatim in a new `char_items.
      passive_selection_json` column (`CharacterDetail.passive_selection`,
      captured in `ninja_chars.py` alongside `main_skill`) -- unresolved,
      unused by anything yet, purely so a future Path B has real historical
      data instead of starting the clock over, same reasoning as why
      `main_skill` itself was added ahead of skill-meta phase 2.

      15 new tests across `test_ninja_builds.py` (incl. a ground-truth test
      pinning the "Dance with Death" index-59 resolution so this doesn't
      regress silently), `test_ninja_chars.py`, `test_local_db.py`,
      `test_aggregator.py`, `test_archive_export.py`. 400/400 project-wide
      passing.

- [x] Phase 22: skill-centric collection (top-100 per poe.ninja skill instead
      of top-100 per slot) -- **fully live**: collection, publish, archive
      re-export, retention, and its own scheduled cron + two-layer monitoring
      all implemented, tested, and deployed; two full first-runs and one
      incremental cycle per game completed live; `skill_meta.collect.scheduled`
      flipped to `true` 2026-09-22. Full design in 8.10/8.11.

      Done: facet/list fetchers (real-sample tests), skill plan (Vaal fold,
      spirit lists, ascendancy cut, all thresholds in config), counter,
      shared persistent character cache, checkpointed + resumable run,
      `collection_method` marking of old snapshots, `days_since_league_start`
      fix, legacy job kept as a bridge (shares the cache; no longer writes
      skill/passive data), CLI `--game` / `--only` / `--publish-skill-meta`.
      Removed from the live path: the first-gem `main_skill` guess (renamed
      `archetype_hint`, legacy archetype bucketing only), `aggregate_*_by_skill`,
      `compute_skill_bracket_population`, `aggregate_keypassives_by_skill`,
      and the legacy job's `passives/latest.json` publish, plus the legacy
      summary keys `skill_bracket_population`, `top_20_skills_by_population`,
      `thin_brackets_below_publish_threshold`, `skill_mod_counts_rows`,
      `keypassive_counts_rows`, `unmapped_skill_names`, `skill_gems_*`.

      Also fixed this phase: `ndic.decode` accepted the first of several
      valid split points and returned garbled names for PoE2's 31-entry class
      dictionary (`Druid`); class dictionaries are now validated against
      `KNOWN_CLASSES_BY_GAME` (CLAUDE.md). Removed the stale
      `league.poe1_override: "Mirage"` (that league had ended).

      **First live runs (2026-09-21/22, manual `--only skill`, one game at a
      time)**: PoE1 8h18m (158 main skills, 185 lists incl. Vaal, 14,374
      distinct characters, 16 failed = 0.11%, peak RSS 75.5MB); PoE2 9h46m
      (271 main + 145 spirit = 416 lists, 16,627 distinct characters, 104
      failed = 0.63%, peak RSS 81.4MB). Neither ever approached the VM's RAM
      guard (available never dropped below 278MB). PoE1's run hit a real bug
      the samples hadn't shown: `dps-<skill>.*`/`rate-*` columns in a
      per-skill list can hold *fewer* than 100 entries (characters with no
      value for that skill omitted -- 99/98/94 seen live), which the
      strict all-columns row-alignment check rejected; fixed by checking
      alignment only for the columns the collector actually reads
      (`SKILL_LIST_ALIGNED_COLUMNS`), fixed and redeployed mid-run, then
      resumed from checkpoint with zero lost work. The legacy per-slot job
      was also run manually once per game right after (8-9 min each, 83-86%
      cache reuse) to refresh `meta/latest.json` without waiting for the next
      scheduled cycle.

      Publish (8.11) implemented and run against both completed snapshots:
      `{game}/skills/index.json`, one `{game}/skills/{main|spirit}/{slug}.json`
      per skill (compact JSON), `{game}/skills/dictionary.json`, and
      `passives/latest.json` bumped to schema_version 3 (full shape
      replacement -- see 8.9; consumers reading the old
      `main_skill`/`archetype`/`rank_in_bracket` shape need updating).
      `build_league_archive` gained a `skills` key sourced only from
      `skill_top100_v1` snapshots (`mod_counts` still spans both collection
      methods -- it has no main-skill dimension to be unreliable).

      **Incremental cycle measured 2026-09-22** (see 8.10 for the numbers):
      PoE1 28m39s (97.3% cache reuse), PoE2 42.9min (88.0% reuse) -- combined
      ~71.3min/day, comfortably inside the approved 1-2h budget. Also found
      and fixed live during the incremental run: an ad-hoc `dbstat` diagnostic
      query held a read lock long enough to crash a concurrent write with
      "database is locked" (`LocalDb` now opens with `PRAGMA
      journal_mode=WAL` + a 30s busy timeout -- see CLAUDE.md; the run
      resumed from checkpoint with zero lost work).

      **Retention approved 2026-09-22** after the incremental run measured
      ~46-80MB DB growth per run/game: `skill_run_lists`/`skill_run_members`
      (checkpoint-only) deleted immediately once a run is `'finished'`;
      `skill_stat_counts`/`skill_passive_counts`/`skill_populations` pruned
      after 45 days (`retention.skill_stat_counts_days`). Safe only because
      `archive_export.py`'s `build_league_archive` now merges every fresh
      recomputation against the previously written archive leaf-by-leaf
      instead of overwriting -- a leaf whose raw rows aged out cleanly is
      frozen at its last value, a partial loss raises `ArchiveRegressionError`
      and refuses to write. `mod_counts`/`skill_mod_counts`/`keypassive_counts`
      (the legacy tables) are untouched, unchanged "kept forever" policy.

      **Scheduled and live** (see 8.10's Scheduling subsection): its own
      daily cron (`skill_meta`, 05:00 ICT, PoE1 then PoE2) separate from the
      slot job's `meta` cron (02:00 ICT, now `parts=("slot",)` explicitly so
      the two never bundle together), and its own healthchecks.io check
      (`skill_meta_ping_url`, recommended Period 24h / Grace 4h) plus Layer-2
      freshness+population checks on `{game}/skills/index.json` in
      `tools/heartbeat_check.py`.

      TODO: check at the next league's launch whether poe.ninja indexes
      characters below level 80 (8.10).
