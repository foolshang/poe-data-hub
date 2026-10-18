"""ModNormalizer + the RePoE-generated alias file: loading, last-resort ordering, deprecated preference."""
import json

import pytest

from src.processing.normalizer import ModNormalizer
from src.processing.rendering_aliases import build_aliases

ALIASES_URL = "https://example.test/internal/rendering_aliases_poe1.json"
STATS_URL = "https://example.test/internal/stats_cache_poe1.json"


def variant(string, lo=None, hi=None, handlers=None):
    return {"condition": [{"min": lo, "max": hi, "negated": None}], "index_handlers": [handlers or []],
            "string": string, "format": ["#"], "reminder_text": None, "is_markup": None}


def entry(ids, trade, *variants):
    return {"ids": ids, "trade_stats": [{"id": t, "text": "x", "type": t.split(".")[0], "option": None} for t in trade],
            "English": list(variants), "hidden": None}


class RoutedHttp:
    """get_json routed by URL; a missing route raises like a 404 does."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get_json(self, url):
        self.calls.append(url)
        if url not in self.routes:
            raise RuntimeError("404")
        return self.routes[url]


def make(tmp_path, table, entries, with_aliases=True, http=None, payload=None):
    stats = {"result": [{"id": k, "entries": [{"id": sid, "text": t} for sid, t in rows.items()]} for k, rows in table.items()]}
    if payload is None:
        payload = build_aliases("poe1", entries, table)
    routes = {STATS_URL: {"fetched_at": 1.0, "data": stats}}
    if with_aliases:
        routes[ALIASES_URL] = payload
    n = ModNormalizer(http_client=http or RoutedHttp(routes), game="poe1", cache_path=tmp_path / "stats.json",
                      unmapped_log_path=tmp_path / "u.log", firebase_stats_url=STATS_URL, firebase_aliases_url=ALIASES_URL)
    n.load()
    return n


SOCKETS = entry(["local_has_X_sockets"], ["explicit.stat_1"], variant("Has 1 Socket", 1, 1), variant("Has {0} Sockets", 2, None))


def test_a_wording_the_table_cannot_reach_is_mapped_through_the_published_aliases(tmp_path):
    n = make(tmp_path, {"explicit": {"explicit.stat_1": "Has 1 Socket"}}, [SOCKETS])
    result = n.normalize("Has 2 Sockets", "explicit")
    assert [m.stat_id for m in result] == ["explicit.stat_1"] and result[0].values == [2.0]
    assert result[0].template == "Has 1 Socket"
    assert n.aliases_source == "firebase" and n.alias_hits == 1


def test_without_the_aliases_file_the_normalizer_behaves_as_before(tmp_path):
    n = make(tmp_path, {"explicit": {"explicit.stat_1": "Has 1 Socket"}}, [SOCKETS], with_aliases=False)
    assert n.normalize("Has 2 Sockets", "explicit") == []
    assert n.aliases_source is None
    assert [m.stat_id for m in n.normalize("Has 1 Socket", "explicit")] == ["explicit.stat_1"]


def test_aliases_are_cached_to_disk_and_used_when_firebase_is_unreachable(tmp_path):
    table = {"explicit": {"explicit.stat_1": "Has 1 Socket"}}
    make(tmp_path, table, [SOCKETS])                           # first load writes the cache
    stats = {"result": [{"id": "explicit", "entries": [{"id": "explicit.stat_1", "text": "Has 1 Socket"}]}]}
    down = RoutedHttp({STATS_URL: {"fetched_at": 1.0, "data": stats}})   # aliases route missing = 404
    n = make(tmp_path, table, [SOCKETS], http=down)
    assert n.aliases_source == "disk_cache"
    assert [m.stat_id for m in n.normalize("Has 3 Sockets", "explicit")] == ["explicit.stat_1"]


def test_an_unreadable_aliases_payload_is_ignored(tmp_path):
    n = make(tmp_path, {"explicit": {"explicit.stat_1": "Has 1 Socket"}}, [SOCKETS], payload={"schema_version": 99, "aliases": []})
    assert n.aliases_source is None
    assert n.normalize("Has 2 Sockets", "explicit") == []


def test_an_alias_never_overrides_a_text_the_earlier_rules_already_map(tmp_path):
    other = entry(["other"], ["explicit.stat_2"], variant("Has {0} Sockets", 2, None))
    n = make(tmp_path, {"explicit": {"explicit.stat_1": "Has # Sockets", "explicit.stat_2": "Other"}}, [other])
    assert [m.stat_id for m in n.normalize("Has 2 Sockets", "explicit")] == ["explicit.stat_1"]   # exact match wins
    assert n.alias_hits == 0


def test_two_current_stats_rendering_the_same_unmapped_text_stay_unmapped(tmp_path):
    a = entry(["a"], ["explicit.stat_10"], variant("{0}% increased Thing", 1, None))
    b = entry(["b"], ["explicit.stat_11"], variant("{0}% increased Thing", 1, None))
    n = make(tmp_path, {"explicit": {"explicit.stat_10": "Alpha #", "explicit.stat_11": "Beta #"}}, [a, b])
    assert n.normalize("5% increased Thing", "explicit") == []
    assert n.alias_ambiguous == 1


def test_a_current_stat_is_preferred_over_a_deprecated_only_one_that_the_table_picked(tmp_path):
    old = entry(["old_do_not_use_spell_block"], ["explicit.stat_20"], variant("{0}% Chance to Block", None, None))
    cur = entry(["spell_block"], ["explicit.stat_21"], variant("{0}% Chance to Block", None, None))
    # the table lists the SAME text for both; the dict-based index keeps the later one = the deprecated one here
    n = make(tmp_path, {"explicit": {"explicit.stat_21": "#% Chance to Block", "explicit.stat_20": "#% Chance to Block"}}, [old, cur])
    result = n.normalize("30% Chance to Block", "explicit")
    assert [m.stat_id for m in result] == ["explicit.stat_21"]
    assert n.alias_preferred_current == 1


def test_a_deprecated_choice_is_kept_when_no_current_stat_renders_the_text(tmp_path):
    old = entry(["old_do_not_use_only"], ["explicit.stat_30"], variant("{0}% More Only", None, None))
    n = make(tmp_path, {"explicit": {"explicit.stat_30": "#% More Only"}}, [old])
    assert [m.stat_id for m in n.normalize("5% More Only", "explicit")] == ["explicit.stat_30"]
    assert n.alias_preferred_current == 0


def test_every_second_maps_to_one_and_the_default_branch_stays_unmapped(tmp_path):
    void = entry(["void_ms"], ["explicit.stat_40"], variant("Gain a Void Charge every second", 1000, 1000),
                 variant("Gain a Void Charge every {0} seconds", None, None, ["milliseconds_to_seconds"]))
    shrine = entry(["shrine"], ["explicit.stat_41"], variant("{0}% chance to Trigger Shrine", 1, 99),
                   variant("Trigger Level 20 Shrine", 2000, 2000), variant("Trigger Shrine", None, None))
    n = make(tmp_path, {"explicit": {"explicit.stat_40": "Gain a Void Charge every # seconds", "explicit.stat_41": "#% chance to Trigger Shrine"}}, [void, shrine])
    assert n.normalize("Gain a Void Charge every second", "explicit")[0].values == [1.0]
    assert n.normalize("Trigger Shrine", "explicit") == []
    assert n.normalize("Trigger Level 20 Shrine", "explicit")[0].values == [2000.0]


def test_duplicate_table_texts_are_reported_not_silent(caplog, tmp_path):
    import logging
    from src.processing.normalizer import ModNormalizer

    table = {"result": [{"id": "explicit", "entries": [
        {"id": "explicit.stat_1", "text": "Same text"},
        {"id": "explicit.stat_2", "text": "Same text"},
        {"id": "explicit.stat_3", "text": "Other text"},
    ]}]}
    n = ModNormalizer(http_client=None, game="poe1", cache_path=tmp_path / "s.json", unmapped_log_path=tmp_path / "u.log")
    with caplog.at_level(logging.WARNING):
        n._build_index(table)
    assert n.duplicate_texts == {"explicit": 1}
    assert any("shared by several stat ids" in r.message for r in caplog.records)
    assert n._by_kind["explicit"]["Same text"] == "explicit.stat_2"      # last wins, unchanged behaviour
