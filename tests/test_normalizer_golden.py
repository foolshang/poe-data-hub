"""Drift guard, layer 1: the normalizer's result for ~35,000 real mod texts is frozen in tests/golden/ (see
tools/update_normalizer_golden.py). Any change of any result fails this test in the commit that causes it, so a rule
change can never reach the PoE Checker's port (stat_match.ts) unnoticed. If the change is intended: review the printed
differences, run `python -m tools.update_normalizer_golden`, commit the new expected.json.gz, and tell Winai so the
Checker follows (SPEC.md 8.12)."""
import pytest

from tools.update_normalizer_golden import GOLDEN, diff

MESSAGE = (
    "\nnormalizer results changed for the frozen golden texts.\n"
    "If this is intended: python -m tools.update_normalizer_golden , commit tests/golden/*/expected.json.gz, and TELL WININ "
    "so the Checker's stat_match.ts follows (SPEC.md 8.12). Differences:\n  "
)


@pytest.mark.parametrize("game", ["poe1", "poe2"])
def test_golden_results_are_unchanged(game):
    problems = diff(game, GOLDEN / game)
    assert not problems, MESSAGE + "\n  ".join(problems)


@pytest.mark.parametrize("game", ["poe1", "poe2"])
def test_golden_covers_every_kind_and_both_outcomes(game):
    from tools.update_normalizer_golden import _read_gz

    stored = _read_gz(GOLDEN / game / "expected.json.gz")
    kinds = {k for k, _t, _r in stored["cases"]}
    assert kinds == {"explicit", "implicit", "fractured"}
    assert stored["mapped"] > 10_000 and stored["unmapped"] > 100          # a golden with no unmapped cases would not guard "must stay unmapped"
    assert stored["mapped"] + stored["unmapped"] == len(stored["cases"])
