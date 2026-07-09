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
- **GGG's `trade2/data/stats` returns a persistent 403 from cloud-provider
  IPs** (confirmed 2026-07-09 from the production GCE VM, `35.188.24.245` —
  works fine from a residential/dev IP, blocked identically regardless of
  User-Agent). `ModNormalizer.load()` falls back to the on-disk cache past
  its TTL if a refresh fails, rather than crashing the whole meta job — this
  is load-bearing on the deploy VM, don't remove it. If GGG ever unblocks
  cloud IPs this becomes a no-op safety net, not a regression.

## Layout

- `src/fetchers/` — one module per external data source
- `src/processing/` — normalization/aggregation (mod meta pipeline, later phase)
- `src/storage/` — SQLite + Firebase publisher
- `src/utils/` — http_client, rate_limiter
- `config/config.yaml` — all tunables; no hardcoded league names anywhere
