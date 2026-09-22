"""Publish-time views over the skill-centric SQLite tables: index, per-skill
files, shared dictionary, passives -- src/processing/skill_publish.py."""
from __future__ import annotations

from src.processing.skill_publish import (
    PASSIVES_SCHEMA_VERSION,
    build_skill_dictionary,
    build_skill_groups,
    build_skill_passives_schema,
    build_skills_index,
    skill_slug,
)


def stat_row(skill_type="main", skill="Fireball", bracket="90-100", slot="ring", stat_id="s1",
             mod_kind="explicit", count=8, population=10, slot_holders=9, usage_pct=80.0, v_min=1.0, v_max=2.0):
    return (skill_type, skill, bracket, slot, stat_id, mod_kind, count, population, slot_holders, usage_pct, v_min, v_max)


def passive_row(skill_type="main", skill="Fireball", bracket="90-100", keypassive="Dance with Death",
                 count=8, population=10, usage_pct=80.0):
    return (skill_type, skill, bracket, keypassive, count, population, usage_pct)


def pop_row(skill_type="main", skill="Fireball", bracket="90-100", population=10, facet_count=500):
    return (skill_type, skill, bracket, population, facet_count)


def test_skill_slug_basic():
    assert skill_slug("Righteous Fire") == "righteous-fire"
    assert skill_slug("Cast on Critical") == "cast-on-critical"
    assert skill_slug("Ice Nova of Frostbolts") == "ice-nova-of-frostbolts"
    assert skill_slug("") == "skill"


def test_build_skill_groups_needs_no_stat_or_passive_rows_population_alone_defines_a_group():
    """A thin skill (2-8 characters) with every mod filtered out by usage_pct
    still gets a group -- so its population stays visible (F4's ask)."""
    groups = build_skill_groups([], [], [pop_row(population=3)], min_usage_pct=0.1)
    assert len(groups) == 1
    g = groups[0]
    assert g.populations == {"90-100": 3} and g.mods == {} and g.passives == {}


def test_min_usage_pct_drops_only_the_row_never_the_group():
    groups = build_skill_groups(
        [stat_row(usage_pct=0.05), stat_row(stat_id="s2", usage_pct=5.0)], [], [pop_row()], min_usage_pct=0.1,
    )
    g = groups[0]
    assert [r["stat_id"] for r in g.mods["90-100"]["ring"]] == ["s2"]
    assert g.populations == {"90-100": 10}  # group survives even if it had lost every mod


def test_mods_and_passives_sorted_by_count_desc():
    rows = [stat_row(stat_id="low", count=1), stat_row(stat_id="high", count=9)]
    prows = [passive_row(keypassive="low", count=1), passive_row(keypassive="high", count=9)]
    g = build_skill_groups(rows, prows, [pop_row()], min_usage_pct=0.1)[0]
    assert [r["stat_id"] for r in g.mods["90-100"]["ring"]] == ["high", "low"]
    assert [r["keypassive"] for r in g.passives["90-100"]] == ["high", "low"]


def test_main_and_spirit_are_independent_groups_for_the_same_skill_name():
    groups = build_skill_groups(
        [stat_row(skill_type="main", count=3), stat_row(skill_type="spirit", count=7)],
        [], [pop_row(skill_type="main"), pop_row(skill_type="spirit")], min_usage_pct=0.1,
    )
    by_type = {g.skill_type: g for g in groups}
    assert by_type["main"].mods["90-100"]["ring"][0]["count"] == 3
    assert by_type["spirit"].mods["90-100"]["ring"][0]["count"] == 7
    assert by_type["main"].slug == by_type["spirit"].slug == "fireball"  # same slug, different path (skill_type)


def test_groups_sorted_by_type_then_facet_count_desc():
    rows = [pop_row(skill="Small", facet_count=10), pop_row(skill="Big", facet_count=900)]
    groups = build_skill_groups([], [], rows, min_usage_pct=0.1)
    assert [g.skill for g in groups] == ["Big", "Small"]


def test_slug_collisions_are_disambiguated_deterministically():
    rows = [pop_row(skill="Foo!", facet_count=900), pop_row(skill="Foo?", facet_count=500)]
    groups = build_skill_groups([], [], rows, min_usage_pct=0.1)
    slugs = {g.skill: g.slug for g in groups}
    assert slugs["Foo!"] == "foo"       # bigger facet_count keeps the bare slug
    assert slugs["Foo?"] == "foo-2"
    assert len(set(slugs.values())) == 2


def test_skill_file_and_index_entry_shape():
    g = build_skill_groups([stat_row()], [passive_row()], [pop_row()], min_usage_pct=0.1)[0]
    file_data = g.to_file("poe1", "Allflame", 58)
    assert file_data["skill_type"] == "main" and file_data["skill"] == "Fireball" and file_data["slug"] == "fireball"
    assert file_data["facet_count"] == 500
    assert file_data["mods"]["90-100"]["ring"][0]["stat_id"] == "s1"
    assert file_data["passives"]["90-100"][0]["keypassive"] == "Dance with Death"
    assert file_data["days_since_league_start"] == 58 and file_data["league"] == "Allflame"

    entry = g.index_entry("poe1")
    assert entry == {
        "skill_type": "main", "skill": "Fireball", "slug": "fireball", "facet_count": 500,
        "populations": {"90-100": 10}, "path": "poe1/skills/main/fireball.json",
    }


def test_build_skills_index_shape_and_order():
    rows = [pop_row(skill="Small", facet_count=10), pop_row(skill="Big", facet_count=900)]
    groups = build_skill_groups([], [], rows, min_usage_pct=0.1)
    index = build_skills_index("poe1", "Allflame", 58, 124408, groups)
    assert index["schema_version"] == 1
    assert index["population_total"] == 124408
    assert index["days_since_league_start"] == 58
    assert [s["skill"] for s in index["skills"]] == ["Big", "Small"]


def test_build_skill_dictionary_shape():
    d = build_skill_dictionary("poe2", [{"stat_id": "s1", "template": "+# to Life", "mod_kind": "explicit"}])
    assert d["game"] == "poe2" and d["stat_dictionary"][0]["stat_id"] == "s1"


def test_build_skill_passives_schema_shape_filter_and_sort():
    groups = build_skill_groups(
        [], [passive_row(skill="A", usage_pct=0.05), passive_row(skill="B", keypassive="high", usage_pct=90.0),
             passive_row(skill="B", keypassive="low", usage_pct=10.0)],
        [pop_row(skill="A"), pop_row(skill="B")], min_usage_pct=0.1,
    )
    data = build_skill_passives_schema("poe1", "Allflame", 58, groups)
    assert data["schema_version"] == PASSIVES_SCHEMA_VERSION
    # A's only passive was below threshold -> no passive rows for A, but B's two are sorted by count desc
    kps = [(r["skill"], r["keypassive"]) for r in data["passives"]]
    assert kps == [("B", "high"), ("B", "low")]
    assert all("skill_type" in r and "level_bracket" in r for r in data["passives"])


def test_build_skill_passives_schema_empty_when_no_passives_survive():
    groups = build_skill_groups([], [passive_row(usage_pct=0.01)], [pop_row()], min_usage_pct=0.1)
    data = build_skill_passives_schema("poe1", "Allflame", 58, groups)
    assert data["passives"] == []
