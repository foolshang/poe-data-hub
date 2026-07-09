"""Normalizes item mod text into stat_id-keyed templates (spec section 7.1).

Loads the GGG trade2 `data/stats` table — the one and only GGG endpoint this
project touches (see CLAUDE.md) — and caches it to disk for up to 7 days.
Every raw mod line scraped from an item (via ninja_chars, later phase) is
matched against this table by replacing its numeric rolls with `#` and doing
an exact lookup against the known template text: GGG's own table already
represents placeholders as literal `#`, so no per-template regex is needed.
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

STATS_URL = "https://www.pathofexile.com/api/trade2/data/stats"
DEFAULT_CACHE_PATH = Path("data/cache/ggg_trade2_stats.json")
DEFAULT_UNMAPPED_LOG_PATH = Path("data/unmapped_mods.log")

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


class ModNormalizer:
    def __init__(
        self,
        http_client: HttpClient,
        cache_path: Path = DEFAULT_CACHE_PATH,
        cache_ttl_seconds: float = 7 * 24 * 60 * 60,
        unmapped_log_path: Path = DEFAULT_UNMAPPED_LOG_PATH,
    ):
        self._http = http_client
        self._cache_path = cache_path
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._ttl = cache_ttl_seconds
        self._unmapped_log_path = unmapped_log_path
        self._unmapped_log_path.parent.mkdir(parents=True, exist_ok=True)
        # mod_kind -> {template text ("+# to maximum Life"): stat_id}
        self._by_kind: dict[str, dict[str, str]] = {}
        self.processed_count = 0
        self.unmapped_count = 0

    def load(self, force_refresh: bool = False) -> None:
        raw = None if force_refresh else self._read_cache()
        if raw is None:
            try:
                raw = self._fetch_and_cache()
            except Exception as e:
                stale = self._read_cache(ignore_ttl=True)
                if stale is None:
                    raise
                log.warning(
                    "normalizer: trade2 stats refresh failed (%s) -- falling back to stale disk cache "
                    "(some environments, e.g. cloud-hosted VMs, get a persistent 403 from GGG's API even "
                    "though it works fine from a residential IP; see CLAUDE.md)",
                    e,
                )
                raw = stale
        self._build_index(raw)

    def _read_cache(self, ignore_ttl: bool = False) -> Optional[dict[str, Any]]:
        if not self._cache_path.exists():
            return None
        try:
            envelope = json.loads(self._cache_path.read_text(encoding="utf-8"))
            if not ignore_ttl and time.time() - envelope.get("fetched_at", 0) > self._ttl:
                return None
            return envelope["data"]
        except (OSError, ValueError, KeyError):
            log.warning("normalizer: could not read stats cache, will refetch", exc_info=True)
            return None

    def _fetch_and_cache(self) -> dict[str, Any]:
        data = self._http.get_json(STATS_URL)
        if not isinstance(data, dict) or not isinstance(data.get("result"), list) or not data["result"]:
            raise ValueError(f"trade2 stats validation failed: unexpected shape from {STATS_URL}")
        envelope = {"fetched_at": time.time(), "data": data}
        self._cache_path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
        return data

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
