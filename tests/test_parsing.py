"""Parser rules on small hand-written examples. No database needed."""
from datetime import date

import pytest

from soccer_forecasting.parsing import SourceDataError, full_time_score, parse_season


def row(**overrides):
    base = {"round": "Matchday 1", "date": "2021-08-13", "time": "20:00",
            "team1": "Home FC", "team2": "Away FC", "score": {"ht": [1, 0], "ft": [2, 1]}}
    base.update(overrides)
    return base


def test_reads_nested_score():
    assert full_time_score({"ht": [1, 0], "ft": [2, 1]}) == (2, 1)


def test_reads_nested_score_without_half_time():
    assert full_time_score({"ft": [0, 0]}) == (0, 0)


def test_reads_bare_pair():
    # Every bare pair in the cohort is in the final test season, so this format
    # would otherwise be exercised for the first time on the test data.
    assert full_time_score([3, 1]) == (3, 1)


def test_bare_zero_zero_is_a_real_score():
    assert full_time_score([0, 0]) == (0, 0)


@pytest.mark.parametrize("bad", [
    None, [], [1], [1, 2, 3], ["1", "2"], [True, 1], [-1, 0], [1.0, 2],
    {}, {"ht": [1, 0]}, {"ft": None}, {"ft": [1]}, "2-1",
])
def test_refuses_unusable_scores(bad):
    with pytest.raises(SourceDataError):
        full_time_score(bad)


def test_parse_season_builds_records_in_source_order():
    parsed = parse_season({"matches": [row(), row(team1="B FC", score=[0, 0])]}, "2021-22")
    assert [m.source_row_index for m in parsed] == [0, 1]
    first = parsed[0]
    assert (first.season, first.match_date, first.home_name, first.away_name) == (
        "2021-22", date(2021, 8, 13), "Home FC", "Away FC")
    assert (first.home_goals, first.away_goals) == (2, 1)
    assert (parsed[1].home_goals, parsed[1].away_goals) == (0, 0)


def test_missing_score_stops_instead_of_becoming_a_draw():
    match = row()
    del match["score"]
    with pytest.raises(SourceDataError, match="row 0"):
        parse_season({"matches": [match]}, "2021-22")


@pytest.mark.parametrize("field", ["date", "team1", "team2"])
def test_missing_required_field_stops(field):
    match = row()
    del match[field]
    with pytest.raises(SourceDataError):
        parse_season({"matches": [match]}, "2021-22")


def test_invalid_date_stops():
    with pytest.raises(SourceDataError):
        parse_season({"matches": [row(date="13/08/2021")]}, "2021-22")


def test_unknown_file_layout_stops():
    with pytest.raises(SourceDataError):
        parse_season({"rounds": []}, "2021-22")
