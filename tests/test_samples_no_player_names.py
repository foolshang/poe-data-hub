"""data/samples/ is tracked in a public repo: no real player's account / character name may be committed.
Fix a failure with `python -m tools.scrub_samples` (see its docstring)."""
from pathlib import Path

from tools.scrub_samples import SAMPLES, is_pseudonym, player_strings, sample_files


def test_no_real_player_names_in_samples():
    leaks = {}
    for path in sample_files():
        if path.name.startswith("last_good_builds"):          # runtime-overwritten and gitignored, not a fixture
            continue
        real = {s for s in player_strings(path) if not is_pseudonym(s)}
        if real:
            leaks[str(path.relative_to(SAMPLES))] = len(real)
    assert not leaks, f"real player names in samples (run python -m tools.scrub_samples): {leaks}"


def test_the_scanner_is_not_vacuous():
    # a scanner that silently finds nothing would make the test above meaningless (it did once while writing it)
    counts = {p.name: len(player_strings(p)) for p in sample_files()}
    assert counts["ninja_poe1_builds_search_2026-09-16.bin"] > 100       # current protobuf schema
    assert counts["ninja_poe1_builds_search_ring.json"] > 100            # pre-2026-08-04 schema
    assert counts["ninja_poe1_char_detail.json"] == 2                    # character-detail JSON
