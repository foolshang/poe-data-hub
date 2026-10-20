import json
from unittest.mock import MagicMock

import pytest

from tools import refresh_rendering_aliases as tool


def _translations(n=6000, with_alias=True):
    out = [{"ids": [f"filler_{i}"], "trade_stats": None, "English": []} for i in range(n)]
    if with_alias:
        out.append({"ids": ["local_has_X_sockets"], "trade_stats": [{"id": "explicit.stat_1", "text": "x", "type": "explicit", "option": None}],
                    "English": [{"condition": [{"min": 1, "max": 1, "negated": None}], "index_handlers": [[]], "string": "Has 1 Socket", "format": ["#"]},
                                {"condition": [{"min": 2, "max": None, "negated": None}], "index_handlers": [[]], "string": "Has {0} Sockets", "format": ["#"]}]})
    return out


def _stats_envelope(n=3500):
    entries = [{"id": f"explicit.stat_{100000 + i}", "text": f"Filler # {i}"} for i in range(n)]
    entries.append({"id": "explicit.stat_1", "text": "Has 1 Socket"})
    return {"fetched_at": 1.0, "data": {"result": [{"id": "explicit", "entries": entries}]}}


def _fetch(translations, envelope):
    return lambda url: translations if "repoe" in url or "raw.githubusercontent" in url else envelope


TARGET = tool.TARGETS[0]


def test_a_good_run_publishes_the_file_and_keeps_the_previous_one_as_last_good():
    publisher = MagicMock()
    previous = {"schema_version": 1, "aliases": [{}] * 1, "deprecated": []}
    publisher.fetch_previous.return_value = previous
    assert tool.refresh_one(publisher, TARGET, fetch=_fetch(_translations(), _stats_envelope())) is True
    calls = publisher.publish.call_args_list
    assert calls[0].args[1] == TARGET.last_good_path and calls[0].args[0] == previous
    assert calls[1].args[1] == TARGET.blob_path and calls[1].args[0]["aliases"]


def test_nothing_is_published_when_the_new_payload_shrank_too_much():
    publisher = MagicMock()
    publisher.fetch_previous.return_value = {"schema_version": 1, "aliases": [{}] * 100, "deprecated": []}
    assert tool.refresh_one(publisher, TARGET, fetch=_fetch(_translations(), _stats_envelope())) is False
    publisher.publish.assert_not_called()


def test_nothing_is_published_for_poe1_when_there_are_no_aliases():
    publisher = MagicMock()
    publisher.fetch_previous.return_value = None
    assert tool.refresh_one(publisher, TARGET, fetch=_fetch(_translations(with_alias=False), _stats_envelope())) is False
    publisher.publish.assert_not_called()


def test_poe2_may_legitimately_publish_an_empty_alias_file():
    publisher = MagicMock()
    publisher.fetch_previous.return_value = None
    poe2 = tool.TARGETS[1]
    assert tool.refresh_one(publisher, poe2, fetch=_fetch(_translations(with_alias=False), _stats_envelope())) is True
    assert publisher.publish.call_args.args[0]["aliases"] == []


@pytest.mark.parametrize("bad", [[], {"not": "a list"}, _translations(10)])
def test_a_broken_repoe_download_is_rejected(bad):
    publisher = MagicMock()
    assert tool.refresh_one(publisher, TARGET, fetch=_fetch(bad, _stats_envelope())) is False
    publisher.publish.assert_not_called()


def test_a_tiny_ggg_table_is_rejected():
    publisher = MagicMock()
    assert tool.refresh_one(publisher, TARGET, fetch=_fetch(_translations(), _stats_envelope(5))) is False
    publisher.publish.assert_not_called()


def test_a_failing_upload_reports_failure_without_raising():
    publisher = MagicMock()
    publisher.fetch_previous.return_value = None
    publisher.publish.side_effect = RuntimeError("readback verify failed")
    assert tool.refresh_one(publisher, TARGET, fetch=_fetch(_translations(), _stats_envelope())) is False


def _envelope_with_other_kinds():
    env = _stats_envelope()
    env["data"]["result"].append({"id": "rune", "entries": [{"id": "rune.stat_1", "text": "Has 1 Socket"}]})
    env["data"]["result"].append({"id": "crafted", "entries": [{"id": "crafted.stat_1", "text": "Has 1 Socket"}]})
    env["data"]["result"].append({"id": "pseudo", "entries": [{"id": "pseudo.stat_1", "text": "Has 1 Socket"}]})   # not an item mod kind
    return env


def test_the_public_file_covers_every_item_mod_kind_but_the_hub_private_file_stays_on_three():
    publisher = MagicMock()
    publisher.fetch_previous.return_value = None
    assert tool.refresh_one(publisher, TARGET, fetch=_fetch(_translations(), _envelope_with_other_kinds())) is True
    by_path = {c.args[1]: c.args[0] for c in publisher.publish.call_args_list}
    private = by_path[TARGET.blob_path]["aliases"]
    public = by_path[TARGET.public_staged_path]["aliases"]
    assert private and all(set(a["k"]) == {"explicit"} for a in private)                       # the running hub reads 3 kinds only
    assert public and all(a["kinds"] == ["explicit", "crafted", "rune"] for a in public)        # public: PUBLIC_KINDS order, no pseudo
    assert by_path[TARGET.public_staged_path]["stat_groups"] == [] or all(g["kind"] != "pseudo" for g in by_path[TARGET.public_staged_path]["stat_groups"])


def test_the_stats_table_is_downloaded_once_per_game():
    publisher = MagicMock()
    publisher.fetch_previous.return_value = None
    urls = []

    def fetch(url):
        urls.append(url)
        return _translations() if "raw.githubusercontent" in url else _stats_envelope()

    assert tool.refresh_one(publisher, TARGET, fetch=fetch) is True
    assert urls.count(TARGET.stats_url) == 1
