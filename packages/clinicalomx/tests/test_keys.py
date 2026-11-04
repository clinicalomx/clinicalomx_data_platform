"""Tests for element keys in a merged object: keys from different TMAs never collide."""
from __future__ import annotations

from clinicalomx.extract.keys import key_for, path_from, sanitise

SLIDE = "Arutha YTMA 471-4_Scan1"
TMA0 = f"{SLIDE}:TMA0"
TMA1 = f"{SLIDE}:TMA1"


# The route.
def test_the_key_carries_the_route_from_the_anchor():
    assert key_for(SLIDE, f"{TMA0}:R0C0", "Core", "bgnorm") == "TMA0_Core_R0C0_bgnorm"


def test_one_rule_gives_a_shorter_key_when_the_route_is_shorter():
    assert key_for(TMA0, f"{TMA0}:R0C0", "Core", "bgnorm") == "Core_R0C0_bgnorm"


def test_two_tmas_with_the_same_core_id_do_not_collide():
    a = key_for(SLIDE, f"{TMA0}:R0C0", "Core", "raw")
    b = key_for(SLIDE, f"{TMA1}:R0C0", "Core", "raw")
    assert a != b
    assert (a, b) == ("TMA0_Core_R0C0_raw", "TMA1_Core_R0C0_raw")


def test_the_subtype_is_in_the_key():
    assert key_for(SLIDE, TMA0, "TMA", "raw") == "TMA_TMA0_raw"
    assert key_for(SLIDE, f"{TMA0}:R0C0", "Core", "raw") == "TMA0_Core_R0C0_raw"


def test_a_deeper_spine_keeps_every_segment():
    name = f"{SLIDE}:TissueSection0:TileGrid1:Tile_004"
    assert key_for(SLIDE, name, "Tile", "raw") == "TissueSection0_TileGrid1_Tile_Tile_004_raw"


# What it refuses to let happen.
def test_a_collision_after_sanitising_gets_a_suffix_rather_than_overwriting():
    taken = set()
    a = key_for(SLIDE, f"{SLIDE}:R 0", "Core", "raw", taken=taken)
    b = key_for(SLIDE, f"{SLIDE}:R/0", "Core", "raw", taken=taken)
    assert a == "Core_R_0_raw"
    assert b == "Core_R_0_raw-2", "the second element silently replaced the first"


def test_keys_survive_a_filesystem_and_a_url():
    assert sanitise("R0/C0 x") == "R0_C0_x"
    assert sanitise("///") == "x", "an all-unsafe name must still produce something"


def test_a_descendant_whose_name_is_not_a_prefix_still_gets_a_route():
    assert path_from(SLIDE, "OtherSlide:TMA9:R0C0") == ("OtherSlide", "TMA9")
    assert key_for(SLIDE, "OtherSlide:TMA9:R0C0", "Core", "raw") \
        == "OtherSlide_TMA9_Core_R0C0_raw"


def test_the_anchor_itself_has_no_route():
    assert path_from(SLIDE, TMA0) == ()
