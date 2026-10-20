# Normalizer golden files (drift guard)

Frozen inputs and results of the hub's `ModNormalizer` (`src/processing/normalizer.py`), so that the rules the PoE
Checker mirrors in `stat_match.ts` cannot change in one repo without the other noticing (SPEC.md 8.12).

```
tests/golden/{poe1,poe2}/
  table.json.gz      GGG trade-stats table entries, 3 kinds:  {"explicit": {"explicit.stat_123": "+# to maximum Life", ...},
                                                                "implicit": {...}, "fractured": {...}}
  aliases.json.gz    the PUBLIC alias contract file (SPEC.md 8.12) exactly as published at {game}/aliases/latest.json
  expected.json.gz   {"format": "normalizer-golden/1", "game", "inputs_sha256": {...}, "mapped": N, "unmapped": N,
                      "cases": [[kind, text, result], ...]}
```

`cases`: ~20,300 (PoE1) / ~14,800 (PoE2) DISTINCT real item mod texts (taken from characters on poe.ninja; game text only,
no player data). `kind` is `explicit`, `implicit` or `fractured` (the lists the hub reads). `text` is the mod line as the
hub sees it (markup like `[Keyword|shown]` already reduced to the shown text). `result` is what the hub's normalizer
returns for that text:

* `[]` -- **unmapped** (an expected, normal outcome; about 5% PoE1 / 1% PoE2 of the distinct texts),
* otherwise one `[stat_id, [value, ...]]` entry per matched line (a multi-line text matched line by line gives several
  entries, a text matched as a whole gives one), e.g. `["explicit.stat_3299347043", [88.0]]`. Values follow SPEC.md 8.12
  (stored value, sign as in the GGG table wording).

The inputs are **frozen**: they change only through `python -m tools.update_normalizer_golden --create ...`, never by
themselves, so `expected.json.gz` changes only when the normalizer's behaviour changes.

## Hub side (already in place)

`tests/test_normalizer_golden.py` replays every case and fails on ANY difference (a result changed, mapped became
unmapped or the reverse, a stat id or a value changed) and on an input file edited without regenerating the expectations.
An intended rule change therefore always shows up as a diff of `expected.json.gz` in the same commit.

## Checker side (spec for the scheduled test -- to be written in the Checker repo)

Goal: fail within a week when the hub's behaviour changed and `stat_match.ts` did not follow, without anybody having to
remember. The hub repo is public, so no publishing step is needed.

1. **Fetch** the three files of both games from the hub repo's default branch:
   `https://raw.githubusercontent.com/foolshang/poe-data-hub/master/tests/golden/{poe1,poe2}/{table,aliases,expected}.json.gz`
   (gunzip + `JSON.parse`; Node's `zlib.gunzipSync`).
   **If a file cannot be fetched (network, 404, rate limit), WARN and skip -- do not fail the build.**
2. **Replay**: build the Checker's matcher from `table.json.gz` (+ `aliases.json.gz` through the same alias reader it uses in
   production) and run it for every `[kind, text]` of `expected.json.gz`.
3. **Compare** with `result`: same number of entries; every `stat_id` equal; every value equal within 1e-9 (JSON floats).
   An empty `result` means the Checker must also return "no match" -- never a fuzzy guess.
4. **Fail** on any difference, printing the number of differences grouped as "mapped -> unmapped", "unmapped -> mapped",
   "different stat_id", "different values" and the first 20 examples.
5. **When to run**: on every Checker CI run that touches matching code, AND on a weekly schedule (GitHub's `schedule`
   triggers are late and sometimes skipped -- measured 2026-10 on this account: hourly workflows ran every 2-9 hours, a
   weekly one 5-7 hours late -- so the weekly run is a backstop, not a guarantee; the on-change run is the main guard).
6. **Vendoring is optional**: a vendored copy (with the `inputs_sha256` of the version it was taken from) makes the test
   offline, but then a stale copy defeats the purpose; if you vendor, the weekly job must compare the vendored
   `expected.json.gz` hash with the repo's and warn when they differ.

A failing Checker test means: the hub's normalizer behaviour changed (look at the hub commit that changed
`tests/golden/*/expected.json.gz`) or the port drifted. Port the rule change, or tell the hub owner if the hub change was
not intended.
