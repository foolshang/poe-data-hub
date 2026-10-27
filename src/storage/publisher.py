"""Publishes JSON snapshots to Firebase Storage (spec sections 5, 8.1, 8.3).

Implements stale-while-error: if a source's fetch failed this round, its
entries from the last successfully published snapshot are reused and each
tagged `"stale": true`, instead of publishing a hole for that source.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from google.cloud import storage

log = logging.getLogger(__name__)

DEFAULT_LOCAL_CACHE_DIR = Path("data/cache")
LOCAL_TIMESTAMPED_COPIES_TO_KEEP = 3
SCHEMA_VERSION = 2


def build_prices_schema(
    league: str,
    ninja_entries: list[dict[str, Any]],
    ninja_ok: bool,
    ninja_bucket_of: Callable[[str], str],
    scout_entries: Optional[list[dict[str, Any]]] = None,
    scout_ok: Optional[bool] = None,
    scout_bucket_of: Optional[Callable[[str], str]] = None,
    previous: Optional[dict[str, Any]] = None,
    ninja_per_category: Optional[dict[str, dict[str, Any]]] = None,
    scout_per_category: Optional[dict[str, dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Merge ninja(+scout) entries into the spec-8.1 shape, applying
    stale-while-error per source. scout_* params are optional -- PoE1 has no
    Scout equivalent (poe2scout.com is PoE2-only), so `sources` simply omits
    the "scout" key entirely for games that never queried it, rather than
    reporting a fake "ok"/"error" for a source that was never asked.

    `{ninja,scout}_per_category` (additive field, no schema_version bump --
    see CLAUDE.md "additive optional fields are fine without a bump") is
    published as `sample.per_category.{ninja,scout}`: a whole-source "ok"
    status can hide a partial degradation (e.g. 12/23 poe.ninja categories
    silently failing while NinjaPricesFetcher.fetch_all's own per-category
    skip logic swallows the exceptions and still returns entries for the
    other 11) -- this surfaces that per-category detail so monitoring can
    catch it. Omitted per source when that source's fetch never ran
    (ninja_ok/scout_ok False before any per-category attempt) or wasn't
    used at all (poe1 + scout)."""
    uses_scout = scout_ok is not None
    sources_status: dict[str, str] = {"ninja": "ok" if ninja_ok else "error"}
    if uses_scout:
        sources_status["scout"] = "ok" if scout_ok else "error"

    currency: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []

    def route(entries: list[dict[str, Any]], bucket_of: Callable[[str], str]) -> None:
        for e in entries:
            (currency if bucket_of(e["category"]) == "currency" else items).append(e)

    if ninja_ok:
        route(ninja_entries, ninja_bucket_of)
    if uses_scout and scout_ok:
        route(scout_entries or [], scout_bucket_of)  # type: ignore[arg-type]

    if previous:
        sources_to_check = [("ninja", ninja_ok)]
        if uses_scout:
            sources_to_check.append(("scout", scout_ok))
        for source, ok in sources_to_check:
            if ok:
                continue
            for bucket_name, bucket_list in (("currency", currency), ("items", items)):
                for e in previous.get(bucket_name, []):
                    if e.get("source") == source:
                        stale_entry = dict(e)
                        stale_entry["stale"] = True
                        bucket_list.append(stale_entry)

    per_category: dict[str, dict[str, dict[str, Any]]] = {}
    if ninja_per_category is not None:
        per_category["ninja"] = ninja_per_category
    if uses_scout and scout_per_category is not None:
        per_category["scout"] = scout_per_category

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "league": league,
        "sources": sources_status,
        "sample": {"per_category": per_category},
        "currency": currency,
        "items": items,
    }


INDEX_SCHEMA_VERSION = 1


def build_index_schema(
    main_league: str,
    main_path: str,
    main_generated_at: str,
    events: list[dict[str, str]],
    hardcore: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Build `{game}/prices/index.json` (phase 7 addition, `hardcore` key
    added phase 9): tells consumers which leagues currently have a published
    prices file, without needing to guess or hit the (possibly stale/absent)
    event/hardcore files directly. `events` is a list of {"league", "path",
    "generated_at"} dicts, one per currently-active event league -- empty
    when there's no overlapping event, which is the common case. `hardcore`
    is that same shape (single dict, not a list -- only the main challenge
    league's HC counterpart, never an HC variant of an event league) or
    `None` when no HC variant was found this cycle (e.g. a league-transition
    window) -- never an error, per binding condition 5."""
    return {
        "schema_version": INDEX_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "main": {"league": main_league, "path": main_path, "generated_at": main_generated_at},
        "events": events,
        "hardcore": hardcore,
    }


def sanitize_league_filename(league_name: str) -> str:
    """"Return of the Ancients" -> "return-of-the-ancients" -- safe for use
    as a Firebase Storage object name segment."""
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", league_name.lower()).strip("-")
    return slug or "league"


NEWS_SCHEMA_VERSION = 2


def build_news_schema(
    entries: list[dict[str, Any]],
    ok: bool,
    previous: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build `{game}/news/latest.json` (schema v2, 2026-07-18: GGG-forum-
    sourced, replaces the v1 Steam-sourced shape -- src/fetchers/news.py's
    `fetch_forum_news`). One entry per forum thread on the configured
    listing(s), `id` = `"forum-{thread_id}"`. `entries` is *not* fully
    stateless like v1 was: `fetch_forum_news` is handed the previous
    publish's entries so it can skip re-scraping content for threads it
    already knows about, but the id list itself is always rebuilt fresh
    from the current listing every run -- which entries a *consumer* has
    already posted is still their own state, not published here (CLAUDE.md).

    Stale-while-error: if this run's fetch failed entirely (`ok=False`),
    reuse the previous publish's `entries` verbatim rather than publish an
    empty list -- same policy as prices (spec 10.1b: never crash/blank out
    on a bad response, log + reuse stale data instead)."""
    if not ok and previous:
        entries = previous.get("entries", [])
    return {
        "schema_version": NEWS_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": {"forum": "ok" if ok else "error"},
        "entries": entries,
    }


MTX_SCHEMA_VERSION = 1


def build_mtx_schema(
    deals: list[dict[str, Any]],
    ok: bool,
    previous: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build `mtx/latest.json` (phase 8): PoE1's Deal of the Day specials
    (src/fetchers/deals.py) -- not split per-game like prices/news, since
    PoE2 has no equivalent scrapable source at all (see deals.py docstring).

    Adds `first_seen_at` per deal by merging against `previous`'s deals
    (matched by `item_name`, the only stable key GGG's page actually gives --
    there's no numeric id like Steam's `gid`): carried over unchanged if the
    deal was already present last publish, set to this run's `generated_at`
    if it's new. This is deliberately a different concept from a consumer's
    own "have I posted this" dedup state (e.g. the bot repo's
    seen_deals.json) -- first_seen_at is just "when did the hub first see
    this on the page", published as data, not a posting-state mechanism.

    Stale-while-error: reuses the previous publish's `deals` verbatim
    (first_seen_at included, unmodified) when `ok=False`."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not ok and previous:
        return {
            "schema_version": MTX_SCHEMA_VERSION,
            "generated_at": now,
            "sources": {"ggg_shop": "error"},
            "deals": previous.get("deals", []),
        }

    previous_by_name = {d["item_name"]: d for d in (previous or {}).get("deals", [])}
    merged = []
    for d in deals:
        prev = previous_by_name.get(d["item_name"])
        first_seen_at = prev["first_seen_at"] if prev else now
        merged.append({**d, "first_seen_at": first_seen_at})

    return {
        "schema_version": MTX_SCHEMA_VERSION,
        "generated_at": now,
        "sources": {"ggg_shop": "ok" if ok else "error"},
        "deals": merged,
    }


def build_meta_schema(
    league: str,
    days_since_league_start: Optional[int],
    per_slot_sample: dict[str, int],
    unique_characters_fetched: int,
    fetch_failed: int,
    archetype_distribution: list[dict[str, Any]],
    stat_dictionary: list[dict[str, str]],
    mods: list[dict[str, Any]],
    min_count: int = 5,
) -> dict[str, Any]:
    """Build the spec-8.2 meta JSON shape. Mods with count < min_count are
    dropped to cut noise (spec: "publish เฉพาะ mod ที่ count >= 5")."""
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "league": league,
        "days_since_league_start": days_since_league_start,
        "sample": {
            "per_slot": per_slot_sample,
            "unique_characters_fetched": unique_characters_fetched,
            "fetch_failed": fetch_failed,
        },
        "archetype_distribution": archetype_distribution,
        "stat_dictionary": stat_dictionary,
        "mods": [m for m in mods if m["count"] >= min_count],
    }


# `build_passives_schema` (the `{game}/passives/latest.json` builder) moved to
# src/processing/skill_publish.py 2026-09-21 -- it now reads the skill-centric
# tables (skill_passive_counts), not keypassive_counts, and filters by
# usage_pct (skill_meta.publish.min_usage_pct), not population. See SPEC.md 8.9.


class PricesPublisher:
    def __init__(
        self,
        bucket_name: str,
        project_id: Optional[str] = None,
        local_cache_dir: Path = DEFAULT_LOCAL_CACHE_DIR,
    ):
        self._bucket_name = bucket_name
        self._project_id = project_id
        self._local_cache_dir = local_cache_dir
        self._local_cache_dir.mkdir(parents=True, exist_ok=True)
        self._client: Optional[storage.Client] = None

    def _client_lazy(self) -> storage.Client:
        if self._client is None:
            self._client = storage.Client(project=self._project_id)
        return self._client

    def fetch_previous(self, blob_path: str) -> Optional[dict[str, Any]]:
        """Best-effort read of the currently-published snapshot, used for
        stale-while-error merging. Falls back to the local cache copy if
        Firebase is unreachable or the blob doesn't exist yet."""
        try:
            bucket = self._client_lazy().bucket(self._bucket_name)
            blob = bucket.blob(blob_path)
            if blob.exists():
                return json.loads(blob.download_as_text())
        except Exception as e:
            log.warning("publisher: could not fetch previous snapshot from Firebase (%s) — trying local cache", e)

        local_copy = self._local_cache_dir / Path(blob_path).name
        if local_copy.exists():
            try:
                return json.loads(local_copy.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        return None

    def publish(self, data: dict[str, Any], blob_path: str, cache_control: str, compact: bool = False) -> None:
        """`compact=True` skips indentation (no data difference, ~50% smaller
        for deeply-nested payloads -- see `archive_export.py`'s own measured
        note). Used for `{game}/skills/*` files, where per-skill file count
        (150+) makes the indentation overhead add up; every other published
        file keeps the project's existing indented convention."""
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")) if compact else json.dumps(data, ensure_ascii=False, indent=2)

        bucket = self._client_lazy().bucket(self._bucket_name)
        blob = bucket.blob(blob_path)
        blob.cache_control = cache_control
        blob.upload_from_string(payload, content_type="application/json")

        self._verify_publish(bucket, blob_path, payload)
        self._save_local_copy(blob_path, payload)

    def publish_file(self, local_path: Path, blob_path: str, cache_control: str, gzip_upload: bool = False) -> None:
        """Uploads a file from disk without loading it into memory (`publish` needs the whole payload as a str --
        a 125MB archive would not fit the VM's cap). `gzip_upload=True` sends a gzip of the file with
        `Content-Encoding: gzip` (GCS serves it decompressed to clients that do not ask for gzip). Verified by
        comparing the size and MD5 GCS reports for the stored object with those of the bytes sent, so nothing is
        read back. No local timestamped copy is kept: the file is already on disk."""
        import base64
        import gzip
        import hashlib
        import shutil

        src = Path(local_path)
        upload = src
        tmp: Optional[Path] = None
        if gzip_upload:
            tmp = src.with_name(src.name + ".upload.gz")
            with open(src, "rb") as fin, gzip.open(tmp, "wb", compresslevel=6) as fout:
                shutil.copyfileobj(fin, fout, 1 << 20)
            upload = tmp
        try:
            md5 = hashlib.md5()
            with open(upload, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    md5.update(chunk)
            expected_md5 = base64.b64encode(md5.digest()).decode("ascii")
            expected_size = upload.stat().st_size

            bucket = self._client_lazy().bucket(self._bucket_name)
            blob = bucket.blob(blob_path)
            blob.cache_control = cache_control
            if gzip_upload:
                blob.content_encoding = "gzip"
            blob.upload_from_filename(str(upload), content_type="application/json")
            blob.reload()
            if blob.md5_hash != expected_md5 or int(blob.size) != expected_size:
                raise RuntimeError(
                    f"publisher: post-publish verification MISMATCH for {blob_path} -- sent {expected_size} bytes "
                    f"md5 {expected_md5}, stored {blob.size} bytes md5 {blob.md5_hash}"
                )
            log.info("publisher: uploaded %s (%d bytes%s)", blob_path, expected_size, ", gzip" if gzip_upload else "")
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)

    def _verify_publish(self, bucket: "storage.Bucket", blob_path: str, expected_payload: str) -> None:
        """Read the just-uploaded blob straight back from the GCS API (not
        the public HTTPS URL, which is CDN-cached) and confirm it matches
        what we intended to publish. Raises loudly on any mismatch/failure
        instead of letting a bad or partial upload go unnoticed."""
        try:
            readback = bucket.blob(blob_path).download_as_text()
        except Exception as e:
            raise RuntimeError(f"publisher: post-publish verification could not read back {blob_path}: {e}") from e
        if readback != expected_payload:
            raise RuntimeError(
                f"publisher: post-publish verification MISMATCH for {blob_path} -- "
                f"uploaded content does not match what was read back"
            )

    def delete_old_blobs(self, prefix: str, older_than_days: int, keep_names: frozenset[str] = frozenset()) -> int:
        """Delete blobs under `prefix` whose GCS `updated` timestamp is older
        than `older_than_days` (spec 9's daily cleanup job). Blobs whose
        filename is in `keep_names` (e.g. "latest.json") are never deleted.
        Best-effort: logs and returns the count actually deleted, never raises."""
        cutoff = datetime.now(timezone.utc).timestamp() - older_than_days * 86400
        deleted = 0
        try:
            bucket = self._client_lazy().bucket(self._bucket_name)
            for blob in bucket.list_blobs(prefix=prefix):
                if Path(blob.name).name in keep_names:
                    continue
                if blob.updated is not None and blob.updated.timestamp() < cutoff:
                    blob.delete()
                    deleted += 1
        except Exception as e:
            log.warning("publisher: delete_old_blobs(%s) failed partway through: %s", prefix, e)
        return deleted

    def delete_blobs_not_in(self, prefix: str, keep_names: frozenset[str]) -> int:
        """Delete every blob under `prefix` whose filename is NOT in
        `keep_names` (phase 7 cleanup: event-league price files for leagues
        that have since closed -- unlike delete_old_blobs, this is exact
        membership against the *currently* active league set, not an age
        heuristic, so a closed short-lived event doesn't linger and a
        long-running one is never deleted just for being old).
        Best-effort: logs and returns the count actually deleted, never raises."""
        deleted = 0
        try:
            bucket = self._client_lazy().bucket(self._bucket_name)
            for blob in bucket.list_blobs(prefix=prefix):
                if Path(blob.name).name not in keep_names:
                    blob.delete()
                    deleted += 1
        except Exception as e:
            log.warning("publisher: delete_blobs_not_in(%s) failed partway through: %s", prefix, e)
        return deleted

    def _save_local_copy(self, blob_path: str, payload: str) -> None:
        name = Path(blob_path).name
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        (self._local_cache_dir / f"{name}.{stamp}").write_text(payload, encoding="utf-8")
        (self._local_cache_dir / name).write_text(payload, encoding="utf-8")

        stamped_copies = sorted(self._local_cache_dir.glob(f"{name}.*"), reverse=True)
        for old in stamped_copies[LOCAL_TIMESTAMPED_COPIES_TO_KEEP:]:
            old.unlink(missing_ok=True)
