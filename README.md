# poe-data-hub

Central data hub for the PoE ecosystem: fetches prices and build-meta from
poe.ninja / PoE2 Scout and publishes normalized JSON to Firebase Storage, so
downstream consumers (Discord bot, PoE Checker) never call those APIs
directly.

Full contract: see `SPEC.md`.

## Status

All phases through 6 are live (see `SPEC.md`'s Status section):
`poe2/prices/latest.json`, `poe2/meta/latest.json`, `status.json` are
published to Firebase Storage on a schedule. PoE1 support is a later phase.

## Setup

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
set GOOGLE_APPLICATION_CREDENTIALS=path\to\service-account.json
```

## Run

```
python -m src.main --run-prices-once
python -m src.main --run-meta-once
python -m src.main --run-cleanup-once
python -m src.main --serve          # persistent scheduler (all three, on a schedule)
```

## Deploy

See `deploy/README.md` for the systemd service (`deploy/poe-data-hub.service`)
used on the production GCE VM.

## Tests

```
pytest tests/
```
