"""The public alias contract ({game}/aliases/latest.json, SPEC.md 8.12): stat groups, the readable transform, and the
staged->public copy job."""
from unittest.mock import patch

from src.processing.rendering_aliases import (
    PUBLIC_SCHEMA_VERSION,
    build_aliases,
    build_stat_groups,
    to_public,
    valid_public,
)

TABLE = {
    "explicit": {
        "explicit.stat_1": "Chance to Block is Lucky",
        "explicit.stat_2": "Has # Socket",
        "explicit.stat_3": "Has # Socket",                       # identical text -> group, canonical = last
        "explicit.indexable_support_7": "Socketed Gems are Supported by Level # Multistrike",
        "explicit.stat_8": "Socketed Gems are supported by Level # Multistrike",   # case only
        "explicit.stat_9": "Only one",
    },
    "implicit": {"implicit.stat_3": "Has # Socket"},             # same numeric id, other kind: never merged
}


def test_stat_groups_identical_and_case_only_and_never_across_kinds():
    groups = build_stat_groups(TABLE)
    by_members = {tuple(g["members"]): g for g in groups}
    ident = by_members[("explicit.stat_2", "explicit.stat_3")]
    assert ident["reason"] == "identical_text" and ident["canonical"] == "explicit.stat_3"   # last wins, like ModNormalizer
    case = by_members[("explicit.indexable_support_7", "explicit.stat_8")]
    assert case["reason"] == "case_only" and case["canonical"] is None
    assert len(groups) == 2                                                                   # singletons and cross-kind are not groups


def _payload():
    translations = [{
        "ids": ["base_stat"], "trade_stats": [{"id": "explicit.stat_2"}],
        "English": [{"string": "Has {0} Socket", "condition": [{"min": 1, "max": 1}], "index_handlers": [[]]},
                    {"string": "Has {0} Sockets", "condition": [{"min": 2}], "index_handlers": [[]]}],
    }]
    return build_aliases("poe1", translations, TABLE)


def test_public_document_is_readable_lossless_and_additive():
    payload = _payload()
    doc = to_public(payload, build_stat_groups(TABLE), "https://example/stat_translations.json")
    assert valid_public(doc) and doc["schema_version"] == PUBLIC_SCHEMA_VERSION == 1
    assert doc["game"] == "poe1" and doc["generated_at"] == payload["generated_at"]
    assert doc["deprecated_trade_ids"] == payload["deprecated"]
    assert doc["stat_aliases"] == {"explicit.stat_2": "explicit.stat_3"}                     # sibling -> canonical only
    assert "RePoE" in doc["source"]["note"] and doc["source"]["stat_translations"].startswith("https://")
    assert len(doc["aliases"]) == len(payload["aliases"]) >= 1
    for pub, internal in zip(doc["aliases"], payload["aliases"]):
        assert set(pub) == {"text", "trade_id", "kinds", "placeholder_stat_index", "negate", "divisor", "sign",
                            "conditions", "implied_values", "deprecated"}
        assert pub["text"] == internal["x"] and pub["trade_id"] == internal["n"] and pub["conditions"] == internal["c"]


def test_valid_public_rejects_wrong_shapes():
    good = to_public(_payload(), [], "u")
    assert valid_public(good)
    assert not valid_public({**good, "schema_version": 2})
    assert not valid_public({**good, "aliases": {}})
    assert not valid_public({k: v for k, v in good.items() if k != "stat_aliases"})
    assert not valid_public(None)


def _factory_with(tmp_path, staged: dict):
    from tests.test_main import _fake_prices_publisher_factory

    created: list = []
    factory = _fake_prices_publisher_factory(tmp_path, created)
    seed = factory(bucket_name="test-bucket")
    for path, doc in staged.items():
        seed.publish(doc, path, cache_control="no-cache")
    return factory, created


def test_run_aliases_once_copies_staged_to_public_with_the_24h_cache(tmp_path):
    from src.main import run_aliases_once

    doc = to_public(_payload(), build_stat_groups(TABLE), "u")
    factory, created = _factory_with(tmp_path, {"internal/aliases_public_poe1.json": doc})
    cfg = {"firebase": {"bucket": "test-bucket"}, "game_versions": ["poe1", "poe2"]}
    calls = []

    def spying_factory(**kw):
        pub = factory(**kw)
        orig = pub.publish

        def spy(data, blob_path, cache_control, compact=False):
            calls.append((blob_path, cache_control))
            return orig(data, blob_path, cache_control=cache_control, compact=compact)
        pub.publish = spy
        return pub

    with patch("src.main.PricesPublisher", new=spying_factory):
        result = run_aliases_once(cfg)
    assert calls == [("poe1/aliases/latest.json", "public, max-age=86400")]
    assert result["games"]["poe1"]["ok"] is True
    assert result["games"]["poe2"] == {"ok": False, "reason": "no staged data"}            # not staged yet: skipped, not failed hard
    assert result["ok"] is False
    assert created[-1].fetch_previous("poe1/aliases/latest.json") == doc
    # (fetch_previous falls back to a local copy named by basename -- "latest.json" for every game -- so ask the bucket)
    assert not created[-1]._client_lazy().bucket("test-bucket").blob("poe2/aliases/latest.json").exists()


def test_run_aliases_once_refuses_a_staged_file_that_breaks_the_contract_and_keeps_the_old_public_one(tmp_path):
    from src.main import run_aliases_once

    old = to_public(_payload(), [], "u")
    bad = {**old, "schema_version": 99}
    factory, created = _factory_with(tmp_path, {"internal/aliases_public_poe1.json": bad, "poe1/aliases/latest.json": old})
    with patch("src.main.PricesPublisher", new=factory):
        result = run_aliases_once({"firebase": {"bucket": "test-bucket"}, "game_versions": ["poe1"]})
    assert result["games"]["poe1"] == {"ok": False, "reason": "invalid staged file"}
    assert created[-1].fetch_previous("poe1/aliases/latest.json") == old


def test_run_aliases_once_refuses_a_staged_file_for_the_wrong_game(tmp_path):
    from src.main import run_aliases_once

    doc = to_public(_payload(), [], "u")                                                     # game == "poe1"
    factory, created = _factory_with(tmp_path, {"internal/aliases_public_poe2.json": doc})
    with patch("src.main.PricesPublisher", new=factory):
        result = run_aliases_once({"firebase": {"bucket": "test-bucket"}, "game_versions": ["poe2"]})
    assert result["games"]["poe2"]["ok"] is False
    assert not created[-1]._client_lazy().bucket("test-bucket").blob("poe2/aliases/latest.json").exists()


def test_build_aliases_kinds_parameter_extends_only_the_public_build():
    from src.processing.rendering_aliases import KINDS, PUBLIC_KINDS

    table = {"explicit": {"explicit.stat_2": "Has # Socket"}, "rune": {"rune.stat_2": "Has # Socket"}, "enchant": {"enchant.stat_9": "Other"}}
    translations = [{
        "ids": ["base_stat"], "trade_stats": [{"id": "explicit.stat_2"}],
        "English": [{"string": "Has {0} Socket", "condition": [{"min": 1, "max": 1}], "index_handlers": [[]]},
                    {"string": "Has {0} Sockets", "condition": [{"min": 2}], "index_handlers": [[]]}],
    }, {
        "ids": ["rune_only_stat"], "trade_stats": [{"id": "rune.stat_7"}],
        "English": [{"string": "Rune thing {0}", "condition": [{}], "index_handlers": [[]]}],
    }]
    table["rune"]["rune.stat_7"] = "Rune thing #"
    hub = build_aliases("poe2", translations, table)                      # default: the 3 hub kinds
    public = build_aliases("poe2", translations, table, PUBLIC_KINDS)
    assert all(set(a["k"]) <= set(KINDS) for a in hub["aliases"])
    assert any("rune" in a["k"] for a in public["aliases"])
    assert not any(a["n"] == "7" for a in hub["aliases"])                  # the rune-only stat is invisible to the hub build
    assert KINDS == PUBLIC_KINDS[:3]                                       # the original kinds stay first and in order


def test_stat_groups_follow_the_kinds_passed_in():
    table = {"explicit": {"explicit.stat_1": "X", "explicit.stat_2": "X"}, "rune": {"rune.stat_1": "Y", "rune.stat_2": "Y"}}
    assert [g["kind"] for g in build_stat_groups(table)] == ["explicit"]
    assert sorted(g["kind"] for g in build_stat_groups(table, ("explicit", "rune"))) == ["explicit", "rune"]
