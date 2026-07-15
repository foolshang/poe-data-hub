"""Normalizes item mod text into stat_id-keyed templates (spec section 7.1).

Loads GGG's `data/stats` table (trade2 for PoE2, trade for PoE1 -- phase 10,
see STATS_URL_BY_GAME) and caches it to disk for up to 7 days. Every raw mod
line scraped from an item (via ninja_chars) is matched against this table by
replacing its numeric rolls with `#` and doing an exact lookup against the
known template text: GGG's own table already represents placeholders as
literal `#`, so no per-template regex is needed. PoE1's `trade/data/stats`
has the identical `{"result": [{"id", "entries": [{"id", "text"}]}]}` shape
as trade2 (confirmed live 2026-07-14, data/samples/poe1_trade_stats.json --
15496 entries across 13 groups, including the "explicit"/"implicit"/
"fractured" groups this project actually uses), so no parsing changes were
needed beyond making the URL/cache path/Firebase path per-game.

Load order (added 2026-07-10, see CLAUDE.md): **Firebase
(`internal/stats_cache.json` for PoE2 / `internal/stats_cache_poe1.json` for
PoE1, both refreshed weekly by the same GitHub Actions workflow) -> direct
GGG fetch -> stale on-disk cache as a last resort**. Firebase is checked
first every call, not just when the local disk cache has expired -- GGG
permanently blocks requests to `trade2/data/stats` from GCP IPs (a 403,
confirmed live from the production VM), so the direct-GGG tier only ever
succeeds from non-cloud environments; Firebase is the primary source for
anything running on the deploy VM. `trade/data/stats` (PoE1) wasn't
independently reconfirmed blocked the same way, but is routed through the
same GitHub-Actions-refreshed Firebase path out of caution regardless (see
tools/refresh_stats.py). The disk-cache tier ignores its own TTL entirely
(any stale data beats crashing the whole meta job) since GGG rarely removes
a `stat_id`, only adds new ones -- staleness there mostly means "missing
recently-added stats", not "wrong mappings".
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from src.utils.http_client import HttpClient

log = logging.getLogger(__name__)

STATS_URL_BY_GAME = {
    "poe2": "https://www.pathofexile.com/api/trade2/data/stats",
    "poe1": "https://www.pathofexile.com/api/trade/data/stats",
}
DEFAULT_CACHE_PATH_BY_GAME = {
    "poe2": Path("data/cache/ggg_trade2_stats.json"),
    "poe1": Path("data/cache/ggg_trade_stats_poe1.json"),
}
DEFAULT_UNMAPPED_LOG_PATH_BY_GAME = {
    "poe2": Path("data/unmapped_mods.log"),
    "poe1": Path("data/unmapped_mods_poe1.log"),
}

# Only these mod kinds are tracked on items, per spec 7.1.
KNOWN_MOD_KINDS = {"explicit", "implicit", "fractured"}

# Most GGG templates have no literal sign ("# to maximum Life" covers both
# +87 and, on corrupted items, -20) so the sign is normally swallowed into
# the captured roll. But ~250 explicit templates bake a literal "+" into the
# text itself (e.g. "+#% Monster Elemental Resistances", always-positive map
# mods) — those never get a "-" counterpart. Try sign-swallowed first (the
# common case), then sign-kept-as-literal as a fallback for that alternative.
_NUMBER_WITH_SIGN_RE = re.compile(r"[+-]?\d+(?:\.\d+)?")
_NUMBER_NO_SIGN_RE = re.compile(r"\d+(?:\.\d+)?")

# Fallback-match helpers (see ModNormalizer._match_one): GGG's trade stats
# table normalizes away plurals and always spells negative-roll stats in
# their "positive" form ("increased X", "more X"), unlike actual item text.
# Only the trailing word is singularized ("Charm Slots" -> "Charm Slot") --
# matching mid-string "s" too would also mangle verbs like "Has".
_TRAILING_S_RE = re.compile(r"(\w)s\Z")
_NEGATION_WORD_PAIRS = [("reduced", "increased"), ("less", "more")]


@dataclass(frozen=True)
class NormalizedMod:
    stat_id: str
    mod_kind: str
    template: str
    values: list[float]


def _skeletonize(text: str, pattern: "re.Pattern[str]") -> tuple[str, list[float]]:
    """Replace every numeric roll in `text` with '#', collecting the rolls in order."""
    values: list[float] = []

    def repl(m: "re.Match[str]") -> str:
        values.append(float(m.group(0)))
        return "#"

    return pattern.sub(repl, text), values


def _validate_stats_shape(data: Any) -> None:
    if not isinstance(data, dict) or not isinstance(data.get("result"), list) or not data["result"]:
        raise ValueError("unexpected trade2 stats shape")


class ModNormalizer:
    def __init__(
        self,
        http_client: HttpClient,
        game: str = "poe2",
        cache_path: Optional[Path] = None,
        cache_ttl_seconds: float = 7 * 24 * 60 * 60,
        unmapped_log_path: Optional[Path] = None,
        firebase_stats_url: Optional[str] = None,
    ):
        if game not in STATS_URL_BY_GAME:
            raise ValueError(f"normalizer: unknown game {game!r}")
        self._game = game
        self._stats_url = STATS_URL_BY_GAME[game]
        self._http = http_client
        self._cache_path = cache_path or DEFAULT_CACHE_PATH_BY_GAME[game]
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._ttl = cache_ttl_seconds
        self._unmapped_log_path = unmapped_log_path or DEFAULT_UNMAPPED_LOG_PATH_BY_GAME[game]
        self._unmapped_log_path.parent.mkdir(parents=True, exist_ok=True)
        self._firebase_stats_url = firebase_stats_url
        # mod_kind -> {template text ("+# to maximum Life"): stat_id}
        self._by_kind: dict[str, dict[str, str]] = {}
        self.processed_count = 0
        self.unmapped_count = 0
        # Populated by load(): where the stats table came from and when that
        # copy was originally fetched (spec: exposed for status.json's
        # stats_cache_stale monitoring).
        self.stats_source: Optional[str] = None
        self.stats_fetched_at: Optional[float] = None

    def load(self, force_refresh: bool = False) -> None:
        raw: Optional[dict[str, Any]] = None
        fetched_at: Optional[float] = None
        source: Optional[str] = None

        if not force_refresh:
            firebase_result = self._try_firebase()
            if firebase_result is not None:
                raw, fetched_at = firebase_result
                source = "firebase"

        if raw is None:
            try:
                raw = self._fetch_from_ggg()
                fetched_at = time.time()
                source = "ggg_direct"
                self._write_disk_cache(raw, fetched_at)
            except Exception as e:
                cached = self._read_disk_cache()
                if cached is None:
                    raise
                raw, fetched_at = cached
                source = "disk_cache_stale"
                cache_age_days = round((time.time() - fetched_at) / 86400, 1)
                log.warning(
                    "normalizer(%s): stats unavailable from Firebase and GGG (%s) -- "
                    "falling back to disk cache (%s days old; some environments, e.g. cloud-hosted "
                    "VMs, get a persistent 403 from GGG's API even though it works fine from a "
                    "residential IP; see CLAUDE.md)",
                    self._game, e, cache_age_days,
                )

        self.stats_source = source
        self.stats_fetched_at = fetched_at
        self._build_index(raw)

    def _try_firebase(self) -> Optional[tuple[dict[str, Any], float]]:
        if not self._firebase_stats_url:
            return None
        try:
            envelope = self._http.get_json(self._firebase_stats_url)
            data = envelope["data"]
            fetched_at = float(envelope["fetched_at"])
            _validate_stats_shape(data)
        except Exception as e:
            log.warning("normalizer(%s): could not load stats from Firebase (%s), falling back", self._game, e)
            return None
        self._write_disk_cache(data, fetched_at)
        return data, fetched_at

    def _fetch_from_ggg(self) -> dict[str, Any]:
        data = self._http.get_json(self._stats_url)
        _validate_stats_shape(data)
        return data

    def _read_disk_cache(self) -> Optional[tuple[dict[str, Any], float]]:
        """Last-resort tier: whatever's on disk, however old -- ignores TTL
        entirely, since Firebase (refreshed weekly by CI) is the primary
        freshness guarantee now, not this local file."""
        if not self._cache_path.exists():
            return None
        try:
            envelope = json.loads(self._cache_path.read_text(encoding="utf-8"))
            return envelope["data"], float(envelope["fetched_at"])
        except (OSError, ValueError, KeyError):
            log.warning("normalizer: could not read disk stats cache", exc_info=True)
            return None

    def _write_disk_cache(self, data: dict[str, Any], fetched_at: float) -> None:
        envelope = {"fetched_at": fetched_at, "data": data}
        try:
            self._cache_path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
        except OSError:
            log.warning("normalizer: could not write disk stats cache", exc_info=True)

    def _build_index(self, data: dict[str, Any]) -> None:
        self._by_kind = {}
        for group in data.get("result", []):
            kind = group.get("id")
            if kind not in KNOWN_MOD_KINDS:
                continue
            bucket = self._by_kind.setdefault(kind, {})
            for entry in group.get("entries", []):
                stat_id, text = entry.get("id"), entry.get("text")
                if not stat_id or not text:
                    continue
                bucket[text] = stat_id

    def stat_dictionary(self) -> list[dict[str, str]]:
        """For embedding in meta/latest.json (spec 8.2) — consumers rely on this mapping."""
        return [
            {"stat_id": stat_id, "template": text, "mod_kind": kind}
            for kind, bucket in self._by_kind.items()
            for text, stat_id in bucket.items()
        ]

    def normalize(self, raw_text: str, mod_kind: str) -> list[NormalizedMod]:
        """Match one raw item mod entry (possibly multi-line/hybrid) to stat_id(s).

        Tries the longest possible match first — the whole raw block, which
        catches hybrid mods encoded as a single multi-stat entry in GGG's
        table — before falling back to matching each line independently.
        Unmatched text is logged and never raises.
        """
        raw_text = raw_text.strip("\n")
        if not raw_text:
            return []
        self.processed_count += 1

        whole = self._match_one(raw_text, mod_kind)
        if whole is not None:
            return [whole]

        lines = raw_text.split("\n")
        if len(lines) > 1:
            results = [self._match_one(line, mod_kind) for line in lines]
            if all(r is not None for r in results):
                return results  # type: ignore[return-value]

        self.unmapped_count += 1
        self._log_unmapped(raw_text, mod_kind)
        return []

    def _match_one(self, text: str, mod_kind: str) -> Optional[NormalizedMod]:
        bucket = self._by_kind.get(mod_kind, {})
        direct = self._try_skeletons(text, bucket, mod_kind)
        if direct is not None:
            return direct

        # Trade stats templates normalize plurals away ("Has # Charm Slot",
        # never "Slots") -- singularize simple trailing-s words and retry.
        singularized = _TRAILING_S_RE.sub(r"\1", text)
        if singularized != text:
            result = self._try_skeletons(singularized, bucket, mod_kind)
            if result is not None:
                return result

        # The game flips "increased X" to "reduced X" (and "more" to "less")
        # for readability when a roll is negative -- GGG's own stats table
        # only lists the "increased"/"more" form, so retry with the word
        # swapped back and the parsed value(s) negated.
        for negative_word, positive_word in _NEGATION_WORD_PAIRS:
            if negative_word in text.lower():
                swapped = re.sub(negative_word, positive_word, text, flags=re.IGNORECASE)
                result = self._try_skeletons(swapped, bucket, mod_kind)
                if result is not None:
                    negated_values = [-v for v in result.values]
                    return NormalizedMod(
                        stat_id=result.stat_id, mod_kind=mod_kind, template=result.template, values=negated_values
                    )
        return None

    @staticmethod
    def _try_skeletons(text: str, bucket: dict[str, str], mod_kind: str) -> Optional[NormalizedMod]:
        for pattern in (_NUMBER_WITH_SIGN_RE, _NUMBER_NO_SIGN_RE):
            skeleton, values = _skeletonize(text, pattern)
            stat_id = bucket.get(skeleton)
            if stat_id is not None:
                return NormalizedMod(stat_id=stat_id, mod_kind=mod_kind, template=skeleton, values=values)
        return None

    def _log_unmapped(self, raw_text: str, mod_kind: str) -> None:
        entry = {"ts": time.time(), "mod_kind": mod_kind, "text": raw_text}
        try:
            with self._unmapped_log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            log.warning("normalizer: could not write unmapped mod log entry", exc_info=True)
        log.warning("normalizer: unmapped mod (%s): %r", mod_kind, raw_text)
