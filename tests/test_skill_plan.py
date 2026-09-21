"""Skill plan: which poe.ninja lists to collect, from the population facets."""
from __future__ import annotations

from src.fetchers.ninja_builds import PopulationFacets
from src.processing.skill_plan import SkillPlan, base_skill_name, build_plan
from tests.ninja_samples import SampleHttp

TOTAL = 100_000  # 0.1% = 100 characters


def _facets(skills, spirit=None, classes=None):
    return PopulationFacets(
        total=TOTAL, skills=skills, spirit_skills=spirit,
        classes=classes or {"Slayer": TOTAL - 5, "Marauder": 5},
    )


def test_threshold_is_a_percentage_of_the_whole_league_population():
    plan = build_plan("poe2", _facets({"Big": 5000, "AtLimit": 100, "JustUnder": 99}), 0.1, 0.1)
    assert {l.skill for l in plan.lists} == {"Big", "AtLimit"}  # 100/100000 = 0.1% is kept, 99 is not
    assert plan.group_counts[("main", "Big")] == 5000


def test_threshold_comes_from_config_not_hardcoded():
    facets = _facets({"A": 5000, "B": 600, "C": 150})
    assert {l.skill for l in build_plan("poe2", facets, 1.0, 0.1).lists} == {"A"}
    assert {l.skill for l in build_plan("poe2", facets, 0.5, 0.1).lists} == {"A", "B"}
    assert {l.skill for l in build_plan("poe2", facets, 0.1, 0.1).lists} == {"A", "B", "C"}


def test_main_and_spirit_are_separate_lists_with_the_same_rules():
    plan = build_plan("poe2", _facets({"Herald of Ice": 900}, spirit={"Herald of Ice": 800, "Ghost Dance": 50}), 0.1, 0.1)
    assert {(l.skill_type, l.skill) for l in plan.lists} == {("main", "Herald of Ice"), ("spirit", "Herald of Ice")}
    assert plan.skills("spirit") == ["Herald of Ice"]  # Ghost Dance (50) is under 0.1%


def test_poe1_vaal_variant_folds_into_the_base_skill_and_both_lists_are_fetched():
    plan = build_plan("poe1", _facets({"Reap": 700, "Vaal Reap": 300}), 0.1, 0.1, min_member_list_count=20)
    reap = [l for l in plan.lists if l.skill == "Reap"]
    assert {l.list_name for l in reap} == {"Reap", "Vaal Reap"}
    assert plan.group_counts[("main", "Reap")] == 1000
    assert "Vaal Reap" not in {l.skill for l in plan.lists}  # never its own skill


def test_vaal_only_skill_still_qualifies_under_the_base_name():
    plan = build_plan("poe1", _facets({"Vaal Smite": 400}), 0.1, 0.1)
    assert [(l.skill, l.list_name) for l in plan.lists] == [("Smite", "Vaal Smite")]


def test_folded_group_qualifies_on_the_combined_count():
    # each list alone is below 0.1% (100) but together they pass
    plan = build_plan("poe1", _facets({"Arc": 60, "Vaal Arc": 60}), 0.1, 0.1, min_member_list_count=1)
    assert {l.skill for l in plan.lists} == {"Arc"}


def test_tiny_member_list_is_not_requested_but_a_group_never_ends_up_with_no_list():
    plan = build_plan("poe1", _facets({"Reap": 700, "Vaal Reap": 3}), 0.1, 0.1, min_member_list_count=20)
    assert [l.list_name for l in plan.lists] == ["Reap"]
    plan = build_plan("poe1", _facets({"Reap": 60, "Vaal Reap": 50}), 0.1, 0.1, min_member_list_count=100)
    assert len(plan.lists) == 1 and plan.lists[0].list_name == "Reap"  # the largest one survives


def test_vaal_is_only_folded_on_poe1_and_only_when_enabled():
    assert base_skill_name("poe2", "Vaal Something") == "Vaal Something"
    assert base_skill_name("poe1", "Vaal Reap") == "Reap"
    assert base_skill_name("poe1", "Vaal Reap", fold_vaal=False) == "Vaal Reap"
    assert base_skill_name("poe1", "Vaal ") == "Vaal "


def test_ascendancy_cut_uses_class_share_of_the_population():
    plan = build_plan("poe2", _facets({"A": 5000}, classes={"Slayer": 99_890, "Invoker": 105, "Witch": 5}), 0.1, 0.1)
    assert plan.excluded_classes == frozenset({"Witch"})  # 0.005%; Invoker at 0.105% stays
    plan = build_plan("poe2", _facets({"A": 5000}, classes={"Slayer": 99_890, "Invoker": 105, "Witch": 5}), 0.1, 0.2)
    assert plan.excluded_classes == frozenset({"Witch", "Invoker"})  # configurable


def test_lists_are_ordered_biggest_skill_first_and_plan_round_trips_through_json():
    plan = build_plan("poe1", _facets({"Small": 300, "Big": 9000, "Mid": 800, "Vaal Mid": 100}), 0.1, 0.1)
    assert [l.skill for l in plan.lists][0] == "Big"
    assert SkillPlan.from_json(plan.to_json()) == plan


def test_real_poe1_sample_plan(tmp_path):
    from src.fetchers.ninja_builds import NinjaBuildsFetcher, SnapshotVersion

    fetcher = NinjaBuildsFetcher(
        SampleHttp("poe1"), game="poe1", failed_responses_dir=tmp_path / "f", last_good_sample_path=tmp_path / "x.bin"
    )
    facets = fetcher.fetch_population_facets(SnapshotVersion("l", "L", "v", "l"))
    plan = build_plan("poe1", facets, 0.1, 0.1, min_member_list_count=20)

    skills = set(plan.skills("main"))
    assert "Winter Orb" in skills and "Reap" in skills
    assert not any(s.startswith("Vaal ") for s in skills)  # all folded
    assert {l.list_name for l in plan.lists if l.skill == "Righteous Fire"} == {"Righteous Fire", "Vaal Righteous Fire"}
    assert plan.excluded_classes == {"Duelist", "Marauder", "Ranger", "Scion", "Shadow", "Templar", "Witch"}
    assert len(plan.lists) >= len(skills)


def test_real_poe2_sample_plan(tmp_path):
    from src.fetchers.ninja_builds import NinjaBuildsFetcher, SnapshotVersion

    fetcher = NinjaBuildsFetcher(
        SampleHttp("poe2"), game="poe2", failed_responses_dir=tmp_path / "f", last_good_sample_path=tmp_path / "x.bin"
    )
    facets = fetcher.fetch_population_facets(SnapshotVersion("l", "L", "v", "l"))
    plan = build_plan("poe2", facets, 0.1, 0.1)

    assert len(plan.skills("main")) == 271 and len(plan.skills("spirit")) == 146
    assert len(plan.lists) == 417  # no folding in PoE2: one list per skill
    assert "Druid" in plan.excluded_classes and "Invoker" not in plan.excluded_classes
