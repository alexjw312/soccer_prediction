"""Turn one raw OpenFootball season file into validated match records.

This module has no database code, so its rules can be tested on small examples.
Anything it does not recognize raises an error. It never guesses a score,
because a guessed 0-0 would become a false training label.
"""
from dataclasses import dataclass
from datetime import date


class SourceDataError(ValueError):
    """The source data broke a rule, so nothing from this load should be saved."""


@dataclass(frozen=True)
class ParsedMatch:
    season: str
    source_row_index: int
    match_date: date
    source_time: str | None
    source_round: str | None
    home_name: str
    away_name: str
    home_goals: int
    away_goals: int


def _is_score_pair(value) -> bool:
    # bool is a subclass of int in Python, so compare exact types.
    return (isinstance(value, list) and len(value) == 2
            and all(type(goals) is int and goals >= 0 for goals in value))


def full_time_score(raw_score) -> tuple[int, int]:
    """Read a full-time score from either format the source uses.

    Most rows use {"ft": [2, 1], "ht": [1, 0]}. Some use a bare [2, 1].
    """
    pair = raw_score.get("ft") if isinstance(raw_score, dict) else raw_score
    if not _is_score_pair(pair):
        raise SourceDataError(f"no usable full-time score in {raw_score!r}")
    return pair[0], pair[1]


def parse_season(payload: dict, season: str) -> list[ParsedMatch]:
    """Validate every row of a season file, or raise on the first bad one."""
    rows = payload.get("matches")
    if not isinstance(rows, list):
        raise SourceDataError(f"{season}: expected a top-level 'matches' list")

    parsed = []
    for index, row in enumerate(rows):
        try:
            home_goals, away_goals = full_time_score(row.get("score"))
            parsed.append(ParsedMatch(
                season=season,
                source_row_index=index,
                match_date=date.fromisoformat(row["date"]),
                source_time=row.get("time"),
                source_round=row.get("round"),
                home_name=row["team1"],
                away_name=row["team2"],
                home_goals=home_goals,
                away_goals=away_goals,
            ))
        except (KeyError, TypeError, ValueError) as error:
            raise SourceDataError(f"{season} row {index}: {error}") from error
    return parsed
