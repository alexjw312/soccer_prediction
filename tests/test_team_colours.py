"""Club colours: the config file, and the home/away colours chosen for every pairing."""
import csv
from itertools import permutations

import pytest

from soccer_forecasting import PROJECT_ROOT
from soccer_forecasting.team_colours import (CLUB_FLOORS, DRAW, DRAW_FLOORS, Club, badge, contrast, delta_e, distinct,
                                             load_clubs, match_colours, visible)

CLUBS = load_clubs(PROJECT_ROOT)


def test_every_club_the_database_knows_has_colours_and_a_unique_code():
    with (PROJECT_ROOT / "config" / "team_aliases.csv").open(newline="") as handle:
        names = {r["team_name"] for r in csv.DictReader(handle)}
    assert sorted(names - CLUBS.keys()) == []  # add a row to config/club_colours.csv for each new club
    codes = [c.code for c in CLUBS.values()]
    assert len(codes) == len(set(codes))


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_every_pairing_gets_visible_distinct_colours(mode):
    for home, away in permutations(list(CLUBS) + [None], 2):
        c = match_colours(CLUBS.get(home), CLUBS.get(away))[mode]
        for side in ("home", "away"):
            assert visible(c[side], mode), (home, away, side)
            assert distinct(c[side], DRAW[mode], DRAW_FLOORS), (home, away, side)
            assert contrast(c[side], c[f"{side}_ink"]) >= 4, (home, away, side)  # the printed percentage
        assert distinct(c["home"], c["away"], CLUB_FLOORS), (home, away)


def test_kit_colours_are_used_when_they_work_and_the_second_colour_settles_a_clash():
    light = match_colours(CLUBS["Liverpool"], CLUBS["Arsenal"])["light"]
    assert (light["home"], light["away"]) == (CLUBS["Liverpool"].primary, CLUBS["Arsenal"].secondary)  # navy
    city_fulham = match_colours(CLUBS["Manchester City"], CLUBS["Fulham"])
    assert city_fulham["light"]["home"] == CLUBS["Manchester City"].primary  # sky blue stays sky blue
    assert city_fulham["light"]["away"] == "#000000" and city_fulham["dark"]["away"] == "#FFFFFF"  # Fulham's two


@pytest.mark.parametrize("pair, normal, worst_red_green", [(("#2A78D6", "#EB6834"), 33.6, 24.7),
                                                             (("#EF0107", "#2A78D6"), 37.6, 28.1)])
def test_the_distance_measure_agrees_with_the_palette_checker(pair, normal, worst_red_green):
    # Reported by the dataviz skill's validate_palette.js for the same two colours.
    assert delta_e(*pair) == pytest.approx(normal, abs=0.05)
    assert min(delta_e(*pair, "protan"), delta_e(*pair, "deutan")) == pytest.approx(worst_red_green, abs=0.05)


def test_badges_use_the_secondary_colour_for_text_only_when_it_reads():
    assert badge(CLUBS["Manchester City"], "Manchester City") == ("MCI", "#6CABDD", "#1C2C5B")
    assert badge(CLUBS["Arsenal"], "Arsenal")[2] == "#FFFFFF"  # navy on red would not read
    assert badge(None, "Wolverhampton Wanderers") == ("WW", "#8F8D86", "#FFFFFF")
    assert badge(None, "Luton")[0] == "LUT"


def test_a_bad_colour_in_the_file_is_refused(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "club_colours.csv").write_text(
        "team_name,code,primary,secondary\nArsenal,ARS,red;}body{display:none,#FFFFFF\n")
    with pytest.raises(ValueError, match="bad code or colour"):
        load_clubs(tmp_path)


def test_a_club_without_colours_gets_the_default_pair():
    c = match_colours(None, None)
    assert (c["light"]["home"], c["light"]["away"]) == ("#2A78D6", "#EB6834")
    assert isinstance(Club("ABC", "#000000", "#FFFFFF"), Club)
