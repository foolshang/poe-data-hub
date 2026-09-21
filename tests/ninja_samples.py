"""Fake HTTP client serving poe.ninja's REAL search/dictionary responses captured
2026-09-21 (data/samples/), shared by the skill-centric tests. Never hits the
network."""
from __future__ import annotations

from pathlib import Path

from src.utils import pbwire as pb

SAMPLES = Path(__file__).resolve().parents[1] / "data" / "samples"
STAMP = "2026-09-21"

# (game, query-param, value) -> captured filtered-search response
LIST_SAMPLES = {
    ("poe1", "skills", "Winter Orb"): "ninja_poe1_builds_search_skill_WinterOrb_2026-09-21.bin",
    # Captured from a real run's failed_responses: their dps-*/rate-* columns hold FEWER
    # than 100 entries (99 and 94) because characters without a value are omitted.
    ("poe1", "skills", "Flicker Strike"): "ninja_poe1_builds_search_skill_FlickerStrike_2026-09-21.bin",
    ("poe1", "skills", "Frenzy"): "ninja_poe1_builds_search_skill_Frenzy_2026-09-21.bin",
    ("poe2", "skills", "Whirling Slash"): "ninja_poe2_builds_search_skill_WhirlingSlash_2026-09-21.bin",
    ("poe2", "spiritgems", "Ghost Dance"): "ninja_poe2_builds_search_spirit_GhostDance_2026-09-21.bin",
}


class SampleHttp:
    def __init__(self, game: str):
        self.game = game
        self.calls: list[str] = []
        body = pb.first((SAMPLES / f"ninja_{game}_builds_search_noFilter_{STAMP}.bin").read_bytes(), 1)
        self._hash_to_name = {pb.as_str(pb.first(e, 2)): pb.as_str(pb.first(e, 1)) for e in pb.all_of(body, 6)}

    def get_bytes(self, url: str) -> bytes:
        self.calls.append(url)
        if "/builds/dictionary/" in url:
            name = self._hash_to_name[url.rsplit("/", 1)[1]]
            return (SAMPLES / f"ninja_{self.game}_dict_{name}_{STAMP}.bin").read_bytes()
        query = url.split("?", 1)[1]
        for (game, param, value), fname in LIST_SAMPLES.items():
            if game == self.game and f"{param}={value.replace(' ', '%20')}" in query:
                return (SAMPLES / fname).read_bytes()
        if "&" not in query:  # only `overview=` -> the unfiltered population response
            return (SAMPLES / f"ninja_{self.game}_builds_search_noFilter_{STAMP}.bin").read_bytes()
        raise AssertionError(f"no sample for {url}")
