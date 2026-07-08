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
รองรับ PoE1 และ PoE2 (แยก path `poe1/...`, `poe2/...`) — เฟสแรกทำ PoE2 ก่อน.

## 8. JSON Schema ที่ publish (contract กับ consumer)

ห้ามเปลี่ยน breaking โดยไม่เพิ่ม `schema_version`. Additive optional fields
เพิ่มได้โดยไม่ต้อง bump (per `CLAUDE.md`).

### 8.1 `poe2/prices/latest.json`

```json
{
  "schema_version": 2,
  "generated_at": "2026-07-08T12:00:00Z",
  "league": "<detected league>",
  "sources": {"ninja": "ok", "scout": "ok"},
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
  (PoE1, later phase, uses `"chaos"`)
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
├── poe2/meta/latest.json
├── poe2/meta/2026-07-08.json
├── poe1/...                    (เฟสหลัง)
└── status.json
```

`Cache-Control`: `max-age=300` สำหรับ prices, `max-age=3600` สำหรับ meta.

## Binding conditions (from project owner, still in force)

1. ห้ามตัด price field ใดๆ ทิ้งโดยไม่เสนอก่อน — ทุก entry มี `value` +
   `value_currency` + `divine_value` ครบ, เก็บทุกหน่วยที่ต้นทางมีให้
   (ดู `chaos_value` ด้านบน). กติกานี้ใช้กับ meta schema ด้วยเมื่อ implement.
2. Bucket ต้อง publish จริงบน Firebase Storage และเปิดผ่าน public URL ได้ —
   ไม่ใช่แค่ไฟล์ local.
3. `league_override` ใน `config.yaml` default เป็นค่าว่างเสมอ — ระบบเลือก
   ลีกอัตโนมัติเป็นทางหลัก.

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
- [ ] Phase 7: PoE1 (later phase)
