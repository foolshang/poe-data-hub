"""The alias-contract cases (tests/fixtures/alias_contract/, SPEC.md 8.12) replayed OFFLINE against the table entries and
public aliases each game's cases depend on. Two independent checks:
  * the hub's ModNormalizer (the whole rule chain) must give every case's expected result;
  * for cases the alias table produced, PublicAliasReader (reads only the readable public file) must agree.
Regenerate the fixture with tools/build_alias_contract_fixture.py when the cases or the contract change."""
import json
import logging
from pathlib import Path

import pytest

from src.processing.normalizer import ModNormalizer
from src.processing.rendering_aliases import PublicAliasReader, from_public_alias, valid_public

FIX = Path(__file__).parent / "fixtures" / "alias_contract"
CASES = json.loads((FIX / "cases.json").read_text(encoding="utf-8"))["cases"]


class _NoHttp:
    def get_json(self, url):
        raise RuntimeError("offline")


def _subset(game):
    return json.loads((FIX / f"subset_{game}.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def normalizers(tmp_path_factory):
    logging.disable(logging.CRITICAL)
    out = {}
    for game in ("poe1", "poe2"):
        sub = _subset(game)
        tmp = tmp_path_factory.mktemp(game)
        envelope = {"fetched_at": 4102444800, "data": {"result": [
            {"id": k, "entries": [{"id": i, "text": t} for i, t in e.items()]} for k, e in sub["table"].items()]}}
        (tmp / "stats.json").write_text(json.dumps(envelope), encoding="utf-8")
        pub = sub["public"]
        internal = {"schema_version": 1, "game": game, "generated_at": pub["generated_at"], "stats": {},
                    "deprecated": pub["deprecated_trade_ids"], "aliases": [from_public_alias(a) for a in pub["aliases"]]}
        (tmp / f"rendering_aliases_{game}.json").write_text(json.dumps(internal), encoding="utf-8")
        n = ModNormalizer(http_client=_NoHttp(), game=game, cache_path=tmp / "stats.json", unmapped_log_path=tmp / "u.log")
        n.load()
        out[game] = n
    yield out
    logging.disable(logging.NOTSET)


def test_the_fixture_is_the_size_and_shape_the_contract_promises():
    assert 30 <= len(CASES) <= 60
    cats = {c["category"] for c in CASES}
    for needed in ("local_suffix", "fixed_number", "plural", "negate", "no_number", "units", "cluster_notable", "must_not_map"):
        assert needed in cats, needed
    assert all(c["expect"]["status"] in ("mapped", "unmapped") for c in CASES)
    for game in ("poe1", "poe2"):
        assert valid_public(_subset(game)["public"])


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_hub_normalizer_gives_the_expected_result(normalizers, case):
    got = normalizers[case["game"]].normalize(case["text"], case["kind"])
    exp = case["expect"]
    if exp["status"] == "unmapped":
        assert got == [], f"{case['text']!r} must stay unmapped ({case['why']})"
    else:
        assert [(m.stat_id, m.values) for m in got] == [(exp["stat_id"], exp["values"])], case["why"]


@pytest.mark.parametrize("case", [c for c in CASES if c.get("served_by_alias_table")], ids=lambda c: c["id"])
def test_public_reader_alone_reproduces_the_alias_served_cases(case):
    reader = PublicAliasReader(_subset(case["game"])["public"])
    stat_id, values = reader.resolve(case["kind"], case["text"])
    assert stat_id == case["expect"]["stat_id"]
    assert [round(v, 6) for v in values] == [round(v, 6) for v in case["expect"]["values"]]


def test_stat_group_members_share_the_group_with_the_expected_stat():
    grouped = [c for c in CASES if "stat_group" in c]
    assert grouped, "at least the case-only 'supported' case"
    for c in grouped:
        assert c["expect"]["stat_id"] in c["stat_group"]["members"] and len(c["stat_group"]["members"]) >= 2
