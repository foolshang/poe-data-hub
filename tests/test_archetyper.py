from __future__ import annotations

from src.processing.archetyper import Archetyper


def test_minion_keyword_wins_over_everything():
    a = Archetyper()
    assert a.classify("Summon Skeletal Warrior", "Fire Staff") == "minion"


def test_totem_trap_mine_wins_over_element():
    a = Archetyper()
    assert a.classify("Fire Trap", "Wand") == "totem-trap-mine"
    assert a.classify("Lightning Totem", None) == "totem-trap-mine"


def test_element_keyword_classifies_spell():
    a = Archetyper()
    assert a.classify("Herald of Ash", None) == "spell-fire"
    assert a.classify("Frost Bolt", None) == "spell-cold"
    assert a.classify("Spark", None) == "spell-lightning"
    assert a.classify("Poison Nova", None) == "spell-chaos"


def test_weapon_fallback_for_non_elemental_skill():
    a = Archetyper()
    assert a.classify("Shield Wall", "Two Handed Mace") == "attack-melee"
    assert a.classify("Barrage", "Bow") == "attack-bow"
    assert a.classify("Fragmentation Rounds", "Crossbow") == "attack-crossbow"


def test_no_main_skill_is_unknown():
    a = Archetyper()
    assert a.classify(None, "Bow") == "unknown"
    assert a.classify("", "Bow") == "unknown"


def test_non_elemental_skill_with_no_weapon_is_unknown():
    a = Archetyper()
    assert a.classify("Shield Wall", None) == "unknown"
