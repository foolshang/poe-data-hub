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
    scout_entries: list[dict[str, Any]],
    scout_ok: bool,
    scout_bucket_of: Callable[[str], str],
    previous: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Merge ninja+scout entries into the spec-8.1 shape, applying
    stale-while-error per source."""
    sources_status = {"ninja": "ok" if ninja_ok else "error", "scout": "ok" if scout_ok else "error"}
    currency: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []

    def route(entries: list[dict[str, Any]], bucket_of: Callable[[str], str]) -> None:
        for e in entries:
            (currency if bucket_of(e["category"]) == "currency" else items).append(e)

    if ninja_ok:
        route(ninja_entries, ninja_bucket_of)
    if scout_ok:
        route(scout_entries, scout_bucket_of)

    if previous:
        for source, ok in (("ninja", ninja_ok), ("scout", scout_ok)):
            if ok:
                continue
            for bucket_name, bucket_list in (("currency", currency), ("items", items)):
                for e in previous.get(bucket_name, []):
                    if e.get("source") == source:
                        stale_entry = dict(e)
                        stale_entry["stale"] = True
                        bucket_list.append(stale_entry)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "league": league,
        "sources": sources_status,
        "currency": currency,
        "items": items,
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

    def publish(self, data: dict[str, Any], blob_path: str, cache_control: str) -> None:
        payload = json.dumps(data, ensure_ascii=False, indent=2)

        bucket = self._client_lazy().bucket(self._bucket_name)
        blob = bucket.blob(blob_path)
        blob.cache_control = cache_control
        blob.upload_from_string(payload, content_type="application/json")

        self._verify_publish(bucket, blob_path, payload)
        self._save_local_copy(blob_path, payload)

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

    def _save_local_copy(self, blob_path: str, payload: str) -> None:
        name = Path(blob_path).name
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        (self._local_cache_dir / f"{name}.{stamp}").write_text(payload, encoding="utf-8")
        (self._local_cache_dir / name).write_text(payload, encoding="utf-8")

        stamped_copies = sorted(self._local_cache_dir.glob(f"{name}.*"), reverse=True)
        for old in stamped_copies[LOCAL_TIMESTAMPED_COPIES_TO_KEEP:]:
            old.unlink(missing_ok=True)
