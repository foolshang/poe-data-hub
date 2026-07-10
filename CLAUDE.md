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
  User-Agent). **Never add a code path that calls `trade2/data/stats`
  directly from the deploy VM** — it will fail every time, permanently, not
  intermittently. GitHub Actions runners were confirmed reachable instead
  (3/3 test runs, 3 different IPs, 200 OK — see git history for
  `.github/workflows/test-ggg-reachability.yml`, since deleted), so
  `.github/workflows/refresh-stats.yml` (weekly cron + manual trigger) runs
  `tools/refresh_stats.py` from there and publishes to Firebase Storage at
  `internal/stats_cache.json`. **This Firebase copy is now the primary
  source of the stats table for every environment** —
  `ModNormalizer.load()`'s order is Firebase -> direct GGG fetch -> stale
  on-disk cache (see `src/processing/normalizer.py` docstring). The
  GitHub Actions workflow uses its own narrowly-scoped service account
  (`stats-refresh@poe-hub-b4723.iam.gserviceaccount.com`, IAM-condition
  restricted to the `internal/` prefix only) — deliberately not the same
  key as the VM's `service-account.json`, so a compromised CI secret can't
  touch `poe2/prices` or `poe2/meta`.

## Layout

- `src/fetchers/` — one module per external data source
- `src/processing/` — normalization/aggregation (mod meta pipeline, later phase)
- `src/storage/` — SQLite + Firebase publisher
- `src/utils/` — http_client, rate_limiter
- `config/config.yaml` — all tunables; no hardcoded league names anywhere
