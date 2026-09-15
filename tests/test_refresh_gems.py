"""Unit tests for tools/refresh_gems.py (build + stage
internal/gems_normalized_{game}.json -- see src/main.py's run_gems_once
for the second hop that republishes it to the public {game}/gems.json).

Fixtures used:
  - data/samples/repoe_poe2_skill_gems.json -- full real RePoE poe2 response
    (1.3MB, live-fetched 2026-09-15), naturally clears MIN_GEMS["poe2"].
  - data/samples/repoe_poe1_gems_sample.json -- a curated *subset* of the
    real poe1 RePoE response (the full file is ~41MB with per-level data,
    too large to commit -- see refresh_gems.py's module docstring for why
    that bulk is excluded from the published output too). Hand-picked real
    entries covering every case normalize_poe1 must handle: a normal active
    gem with a Royale duplicate (Fireball/FireballRoyale), a transfigured
    alt_x/alt_y pair plus its base (IceNova/IceNovaAltX/IceNovaAltY), a
    normal support gem (SupportAddedFireDamage), a DNT placeholder, and an
    internal-only no-name gem.
  - data/samples/ggg_trade2_items.json / poe1_trade_items.json -- full real
    trade(2)/data/items responses (live-fetched 2026-09-15).
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from tools import refresh_gems

SAMPLES = Path(__file__).resolve().parents[1] / "data" / "samples"
POE2_REPOE = json.loads((SAMPLES / "repoe_poe2_skill_gems.json").read_text(encoding="utf-8"))
POE1_REPOE_SAMPLE = json.loads((SAMPLES / "repoe_poe1_gems_sample.json").read_text(encoding="utf-8"))
POE2_TRADE_ITEMS = json.loads((SAMPLES / "ggg_trade2_items.json").read_text(encoding="utf-8"))
POE1_TRADE_ITEMS = json.loads((SAMPLES / "poe1_trade_items.json").read_text(encoding="utf-8"))


def _resp(data):
    mock_resp = MagicMock()
    mock_resp.json.return_value = data
    mock_resp.raise_for_status.return_value = None
    return mock_resp


# ---------------------------------------------------------------------------
# trade_gem_names
# ---------------------------------------------------------------------------


def test_trade_gem_names_real_poe2_sample():
    names = refresh_gems.trade_gem_names(POE2_TRADE_ITEMS)
    assert len(names) == 905
    assert "Ancestral Cry" in names


def test_trade_gem_names_real_poe1_sample():
    names = refresh_gems.trade_gem_names(POE1_TRADE_ITEMS)
    assert len(names) == 610  # 873 raw entries, deduped to unique names
    assert "Absolution" in names


def test_trade_gem_names_rejects_non_dict():
    with pytest.raises(ValueError, match="unexpected trade"):
        refresh_gems.trade_gem_names(["not", "a", "dict"])


def test_trade_gem_names_rejects_missing_gems_group():
    with pytest.raises(ValueError, match="no 'Gems'"):
        refresh_gems.trade_gem_names({"result": [{"label": "Currency", "entries": []}]})


# ---------------------------------------------------------------------------
# normalize_poe2 (real full sample)
# ---------------------------------------------------------------------------


def test_normalize_poe2_real_sample_clears_floor():
    gems = refresh_gems.normalize_poe2(POE2_REPOE, trade_names=None)
    assert len(gems) >= refresh_gems.MIN_GEMS["poe2"]


def test_normalize_poe2_drops_dnt_placeholders():
    gems = refresh_gems.normalize_poe2(POE2_REPOE, trade_names=None)
    assert not any(g["display_name"].startswith("[DNT]") for g in gems)


def test_normalize_poe2_never_marks_transfigured():
    gems = refresh_gems.normalize_poe2(POE2_REPOE, trade_names=None)
    assert all(g["transfigured_of"] is None for g in gems)


def test_normalize_poe2_flags_lineage_gems_matching_hub_price_category():
    # "Amanamu's Tithe" is one of the LineageGem-category names the hub's
    # scout_prices fetcher already publishes -- RePoE's is_lineage flag
    # should identify the same gem.
    gems = refresh_gems.normalize_poe2(POE2_REPOE, trade_names=None)
    by_name = {g["display_name"]: g for g in gems}
    assert by_name["Amanamu's Tithe"]["is_lineage"] is True
    assert by_name["Ancestral Cry"]["is_lineage"] is False


def test_normalize_poe2_verified_tradeable_uses_trade_names():
    trade_names = refresh_gems.trade_gem_names(POE2_TRADE_ITEMS)
    gems = refresh_gems.normalize_poe2(POE2_REPOE, trade_names=trade_names)
    by_name = {g["display_name"]: g for g in gems}
    assert by_name["Ancestral Cry"]["verified_tradeable"] is True


def test_normalize_poe2_verified_tradeable_null_when_trade_unavailable():
    gems = refresh_gems.normalize_poe2(POE2_REPOE, trade_names=None)
    assert all(g["verified_tradeable"] is None for g in gems)


# ---------------------------------------------------------------------------
# normalize_poe1 (curated real subset)
# ---------------------------------------------------------------------------


def test_normalize_poe1_filters_junk_and_keeps_real_gems():
    gems = refresh_gems.normalize_poe1(POE1_REPOE_SAMPLE, trade_names=None)
    names = {g["display_name"] for g in gems}
    # Royale duplicate, DNT placeholder, and the no-name internal-only gem
    # must all be filtered out.
    assert "[DNT] Call of the Wild" not in names
    assert not any("Royale" in n for n in names)
    assert len(gems) == 5  # Fireball, IceNova, IceNovaAltX, IceNovaAltY, SupportAddedFireDamage


def test_normalize_poe1_marks_transfigured_variants_with_their_base():
    gems = refresh_gems.normalize_poe1(POE1_REPOE_SAMPLE, trade_names=None)
    by_id = {g["id"]: g for g in gems}
    assert by_id["IceNova"]["transfigured_of"] is None
    assert by_id["IceNovaAltX"]["display_name"] == "Ice Nova of Frostbolts"
    assert by_id["IceNovaAltX"]["transfigured_of"] == "Ice Nova"
    assert by_id["IceNovaAltY"]["display_name"] == "Ice Nova of Deep Freeze"
    assert by_id["IceNovaAltY"]["transfigured_of"] == "Ice Nova"


def test_normalize_poe1_is_support_flag():
    gems = refresh_gems.normalize_poe1(POE1_REPOE_SAMPLE, trade_names=None)
    by_id = {g["id"]: g for g in gems}
    assert by_id["SupportAddedFireDamage"]["is_support"] is True
    assert by_id["Fireball"]["is_support"] is False


def test_normalize_poe1_never_sets_is_lineage():
    gems = refresh_gems.normalize_poe1(POE1_REPOE_SAMPLE, trade_names=None)
    assert all(g["is_lineage"] is False for g in gems)


def test_normalize_poe1_transfigured_gem_absent_from_trade_list_is_flagged_not_dropped():
    # Confirmed live 2026-09-15: GGG's trade/data/items Gems list has zero
    # transfigured names. verified_tradeable must be False, not a drop.
    trade_names = refresh_gems.trade_gem_names(POE1_TRADE_ITEMS)
    assert "Ice Nova of Frostbolts" not in trade_names
    gems = refresh_gems.normalize_poe1(POE1_REPOE_SAMPLE, trade_names=trade_names)
    by_id = {g["id"]: g for g in gems}
    assert by_id["IceNovaAltX"]["verified_tradeable"] is False
    assert by_id["Fireball"]["verified_tradeable"] is True  # base skills ARE in the trade list


# ---------------------------------------------------------------------------
# TARGETS / run() plumbing
# ---------------------------------------------------------------------------


def test_targets_cover_both_games_with_distinct_paths():
    games = {t.game for t in refresh_gems.TARGETS}
    assert games == {"poe1", "poe2"}
    paths = {t.blob_path for t in refresh_gems.TARGETS}
    assert paths == {"internal/gems_normalized_poe2.json", "internal/gems_normalized_poe1.json"}


def _url_dispatch_get(url_map):
    def _get(url, **kwargs):
        if url not in url_map:
            raise AssertionError(f"unexpected URL fetched: {url}")
        return _resp(url_map[url])

    return _get


def test_run_uploads_both_games_on_success():
    poe2_repoe_url = refresh_gems.REPOE_RAW_TMPL.format(repo="poe2", file="skill_gems.json")
    poe1_repoe_url = refresh_gems.REPOE_RAW_TMPL.format(repo="poe1", file="gems.json")
    url_map = {
        poe2_repoe_url: POE2_REPOE,
        refresh_gems.TRADE_ITEMS_URL["poe2"]: POE2_TRADE_ITEMS,
        # Reuse the real poe2 sample as poe1's RePoE payload here -- it's
        # shape-compatible with normalize_poe1's field access (base_item /
        # display_name), clears MIN_GEMS["poe1"] easily, and keeps this a
        # real-fixture-only test rather than fabricating a poe1 payload.
        poe1_repoe_url: POE2_REPOE,
        refresh_gems.TRADE_ITEMS_URL["poe1"]: POE1_TRADE_ITEMS,
    }
    with patch("requests.get", side_effect=_url_dispatch_get(url_map)), \
         patch("tools.refresh_gems.PricesPublisher") as MockPublisher:
        mock_publisher = MockPublisher.return_value
        code = refresh_gems.run()

    assert code == 0
    assert mock_publisher.publish.call_count == 2
    published_paths = {call.args[1] for call in mock_publisher.publish.call_args_list}
    assert published_paths == {"internal/gems_normalized_poe2.json", "internal/gems_normalized_poe1.json"}
    for call in mock_publisher.publish.call_args_list:
        envelope = call.args[0]
        assert envelope["schema_version"] == refresh_gems.GEMS_SCHEMA_VERSION
        assert len(envelope["gems"]) > 0


def test_run_does_not_upload_on_low_gem_count():
    with patch("requests.get", side_effect=[
        _resp({"SomeGem": {"base_item": {"display_name": "X"}}}),  # poe2 RePoE: 1 gem, below floor
        _resp(POE2_TRADE_ITEMS),
        _resp(POE2_REPOE),  # poe1 target succeeds
        _resp(POE1_TRADE_ITEMS),
    ]), patch("tools.refresh_gems.PricesPublisher") as MockPublisher:
        mock_publisher = MockPublisher.return_value
        code = refresh_gems.run()

    assert code == 1
    mock_publisher.publish.assert_called_once()
    assert mock_publisher.publish.call_args.args[1] == "internal/gems_normalized_poe1.json"


def test_run_one_game_repoe_fetch_failure_does_not_block_the_other():
    with patch("requests.get", side_effect=[
        requests.ConnectionError("boom"),  # poe2 RePoE fetch fails outright
        _resp(POE2_REPOE),  # poe1 RePoE fetch (reused, see above) succeeds
        _resp(POE1_TRADE_ITEMS),
    ]), patch("tools.refresh_gems.PricesPublisher") as MockPublisher:
        mock_publisher = MockPublisher.return_value
        code = refresh_gems.run()

    assert code == 1
    mock_publisher.publish.assert_called_once()
    assert mock_publisher.publish.call_args.args[1] == "internal/gems_normalized_poe1.json"


def test_run_trade_items_failure_is_non_fatal_and_nulls_verified_tradeable():
    with patch("requests.get", side_effect=[
        _resp(POE2_REPOE),
        requests.ConnectionError("boom"),  # poe2 trade cross-check fails
        _resp(POE2_REPOE),
        requests.ConnectionError("boom"),  # poe1 trade cross-check fails
    ]), patch("tools.refresh_gems.PricesPublisher") as MockPublisher:
        mock_publisher = MockPublisher.return_value
        code = refresh_gems.run()

    assert code == 0  # trade cross-check is a nice-to-have, not fatal
    assert mock_publisher.publish.call_count == 2
    for call in mock_publisher.publish.call_args_list:
        envelope = call.args[0]
        assert all(g["verified_tradeable"] is None for g in envelope["gems"])
