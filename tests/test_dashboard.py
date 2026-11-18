"""The dashboard's pages: the four tabs and a page per match.

Uses the generated season from live_helpers (round 11 kicks off at 14:00 UTC on
Saturday 31 October 2026), the real history loaded by the ready fixture, and the
forecasted fixture from conftest.
"""
import csv
import json
import re
from datetime import date, datetime, timedelta, timezone
from html import unescape

import pytest
from live_helpers import add_snapshot, club_people, round_kickoff
from test_api import DAY_11, MORNING_11, client, read_only_factory

from soccer_forecasting import PROJECT_ROOT
from soccer_forecasting.api import ATTRIBUTION
from soccer_forecasting.dashboard import (age_on, final_test_summary, league_table, ordinal, pick_day, stadiums,
                                          whole_percents)
from soccer_forecasting.live_loading import load_live
from soccer_forecasting.team_colours import load_clubs, match_colours

UTC = timezone.utc
AFTER_ROUND_11 = datetime(2026, 11, 1, 5, 0, tzinfo=UTC)
TABS = {"/": "Fixtures", "/table": "Table", "/model-performance": "Performance", "/how-it-works": "How it works"}


def text(html: str) -> str:
    """The page as plain words: tags removed, entities decoded, runs of white space made one space."""
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


def fixture_rows(html: str) -> list[str]:
    return re.findall(r'<a class="fx".*?</a>', html, flags=re.S)


def round_11(db) -> list[tuple]:
    return db.execute("""SELECT m.match_id, h.team_name, a.team_name, m.home_team_id, m.away_team_id
                         FROM matches m JOIN teams h ON h.team_id = m.home_team_id
                         JOIN teams a ON a.team_id = m.away_team_id
                         WHERE m.match_date = %s ORDER BY m.match_id""", (DAY_11,)).fetchall()


def finish_round_11(db, root):
    add_snapshot(root, AFTER_ROUND_11, finished_rounds=11)
    load_live(db, root)


def test_each_tab_has_its_own_page(forecasted):
    db, root = forecasted
    api = client(db, root, MORNING_11)
    for path, label in TABS.items():
        page = api.get(path)
        assert page.status_code == 200 and "text/html" in page.headers["content-type"], path
        assert f'aria-current="page">{label}</a>' in page.text and ATTRIBUTION in page.text, path
        assert page.text.count('aria-current="page"') == 1, path


def test_fixtures_list_the_day_and_keep_forecasts_for_the_match_page(forecasted):
    db, root = forecasted
    html = client(db, root, MORNING_11).get("/").text
    assert "Saturday 31 October" in html and 'aria-current="date"' in html
    assert "Forecasts saved at 06:00" in html and "Open a match to see its forecast" in html
    rows = fixture_rows(html)
    assert len(rows) == 10 and all(">15:00<" not in r and ">14:00<" in r for r in rows)  # UK time, GMT
    assert all("%" not in r and "Forecast saved." in r for r in rows)
    assert {int(m) for m in re.findall(r'href="/match/(\d+)"', html)} == {r[0] for r in round_11(db)}


def test_names_are_escaped_on_every_page(forecasted):
    db, root = forecasted
    finish_round_11(db, root)  # first, because loading checks the club names
    db.execute("UPDATE teams SET team_name = 'Arsenal <b>FC</b>' WHERE team_name = 'Arsenal'")
    arsenal = next(r[0] for r in round_11(db) if "Arsenal <b>FC</b>" in r[1:3])
    api = client(db, root, AFTER_ROUND_11)
    for path in (f"/?date={DAY_11}", f"/match/{arsenal}", "/table", "/model-performance"):
        html = api.get(path).text
        assert "Arsenal &lt;b&gt;FC&lt;/b&gt;" in html and "<b>FC</b>" not in html, path


def test_on_a_day_without_matches_the_next_match_day_is_shown(forecasted):
    db, root = forecasted
    html = client(db, root, MORNING_11 - timedelta(days=2)).get("/").text
    assert "Saturday 31 October" in html  # Thursday has no matches; the strip moves to Saturday


def test_a_future_day_says_when_its_forecasts_come(forecasted):
    db, root = forecasted
    html = client(db, root, MORNING_11).get("/", params={"date": "2026-11-07"}).text
    assert "Saturday 7 November" in html and "saved on the morning of match day" in html
    rows = fixture_rows(html)
    assert len(rows) == 10 and not any("Forecast saved." in r for r in rows)


def test_a_day_with_no_matches_says_so(forecasted):
    db, root = forecasted
    html = client(db, root, MORNING_11).get("/", params={"date": "2026-11-03"}).text
    assert "No Premier League matches on this day." in html and not fixture_rows(html)


def test_a_match_page_shows_when_and_where_and_the_forecast(forecasted):
    db, root = forecasted
    match_id, home, *_ = round_11(db)[0]
    probabilities = db.execute("SELECT p_home, p_draw, p_away FROM predictions WHERE match_id = %s",
                               (match_id,)).fetchone()
    html = client(db, root, MORNING_11).get(f"/match/{match_id}").text
    words = text(html)
    assert "Saturday 31 October 2026" in words and "14:00 UK time" in words
    ground, town = stadiums(root)[home]
    assert f"Stadium {ground} {town} Map" in words
    shown = [int(x) for x in re.findall(r'<span class="pick-value">(\d+)%</span>', html)]
    assert shown == whole_percents(list(probabilities))
    assert "before kickoff, and never changed" in words and "What happened" not in words


def test_a_match_page_shows_what_the_model_saw(forecasted):
    db, root = forecasted
    match_id, features = db.execute("SELECT match_id, features FROM predictions ORDER BY match_id LIMIT 1").fetchone()
    html = client(db, root, MORNING_11).get(f"/match/{match_id}").text
    assert "What the model saw" in html and f'{features["home_ppg_last5"]:.2f}' in html
    assert f'href="/matches/{match_id}/forecasts"' in html


def test_last_five_and_head_to_head_come_from_earlier_matches_only(forecasted):
    db, root = forecasted
    api = client(db, root, MORNING_11)
    for match_id, home, away, home_id, away_id in round_11(db):
        html = api.get(f"/match/{match_id}").text
        recent = re.findall(r'<ol class="recent">(.*?)</ol>', html, flags=re.S)
        assert [len(re.findall("<li>", r)) for r in recent] == [5, 5]  # ten rounds played before
        meetings = db.execute(
            """SELECT count(*) FROM matches WHERE status = 'completed' AND match_date < %s
               AND ((home_team_id, away_team_id) = (%s, %s) OR (home_team_id, away_team_id) = (%s, %s))""",
            (DAY_11, home_id, away_id, away_id, home_id)).fetchone()[0]
        shown = re.search(r'<ol class="h2h">(.*?)</ol>', html, flags=re.S)
        if meetings == 0:
            assert shown is None and "No Premier League meetings since 2020/21" in html
        else:
            dates = [datetime.strptime(d, "%a %d %b %Y").date()
                     for d in re.findall(r'<span class="date">(\w+ \d+ \w+ \d{4})</span>', shown.group(1))]
            assert len(dates) == min(5, meetings) and dates == sorted(dates, reverse=True) and dates[0] < DAY_11
            assert f"Last {min(5, meetings)} meeting" in html


def test_after_full_time_the_outcome_that_happened_keeps_its_colour(forecasted):
    db, root = forecasted
    finish_round_11(db, root)
    api = client(db, root, AFTER_ROUND_11)
    rows = fixture_rows(api.get("/", params={"date": DAY_11.isoformat()}).text)
    assert len(rows) == 10 and all("Full time" in r for r in rows)
    for match_id, result, loss in db.execute("""SELECT match_id, result, log_loss FROM prediction_results
                                                ORDER BY match_id""").fetchall():
        html = api.get(f"/match/{match_id}").text
        lit = re.findall(r'class="seg (home|draw|away)"', html)  # the one block without "faded"
        assert lit == [{"H": "home", "D": "draw", "A": "away"}[result]] and html.count(" faded") == 2
        assert html.count("What happened") == 1 and f"{loss:.3f} in log loss" in html
        earlier = "".join(re.findall(r'<ol class="(?:recent|h2h)">(.*?)</ol>', html, flags=re.S))
        assert "Sat 31 Oct" not in earlier  # the match itself is not part of its own history


def test_the_performance_tab_scores_every_played_forecast(forecasted):
    db, root = forecasted
    api = client(db, root, MORNING_11)
    assert "Nothing to score yet" in api.get("/model-performance").text
    finish_round_11(db, root)
    html = client(db, root, AFTER_ROUND_11).get("/model-performance").text
    assert "10 forecast matches played so far" in text(html)
    assert html.count('class="num hit"') == 10  # one bold cell per match: the chance given to what happened
    mean = db.execute("SELECT avg(log_loss) FROM prediction_results").fetchone()[0]
    assert f"<td class=\"num\">{mean:.3f}</td>" in html


def test_the_final_test_appears_only_with_a_real_report(forecasted):
    db, root = forecasted  # the fixture's report is a stand-in, so there is nothing to show
    assert final_test_summary(root) is None
    assert "Tested before going live" not in client(db, root, MORNING_11).get("/model-performance").text
    score = {"log_loss": 1.05, "accuracy": 0.46, "draws": {"observed": 0.27, "mean_forecast": 0.23}}
    (root / "reports" / "final_test.json").write_text(json.dumps({
        "selected_model": "logistic_regression", "test": {"seasons": ["2025-26"], "matches": 380},
        "scores": {"logistic_regression": score, "class_frequency_baseline": {**score, "log_loss": 1.09}},
        "reference_uniform_log_loss": 1.0986, "selected_vs_baseline": {"mean": -0.04, "interval": [-0.06, -0.01]}}))
    html = client(db, root, MORNING_11).get("/model-performance").text
    assert "Tested before going live" in html and "2025/26 season (380 matches)" in html
    assert "by 0.040 (95% interval 0.010 to 0.060)" in text(html)


def test_how_it_works_reads_the_model_and_rules_from_their_sources(forecasted):
    db, root = forecasted
    matches, features = db.execute("""SELECT training_matches, cardinality(features) FROM model_runs
                                      WHERE purpose = 'deployment'""").fetchone()
    words = text(client(db, root, MORNING_11).get("/how-it-works").text)
    assert f"trained on {matches:,} Premier League matches" in words and f"reads {features} numbers" in words
    assert "at least 2 days before match day" in words and "more than 30 hours old" in words


def test_an_unknown_match_page_is_a_404_page(forecasted):
    db, root = forecasted
    page = client(db, root, MORNING_11).get("/match/999999")
    assert page.status_code == 404 and "No match with this number" in page.text


def test_the_league_table_adds_up(forecasted):
    db, root = forecasted
    with read_only_factory(db)() as conn:
        table = league_table(conn, "2026-27")
    completed = db.execute("SELECT count(*) FROM matches WHERE season = '2026-27' AND status = 'completed'").fetchone()[0]
    assert len(table) == 20 and sum(r["played"] for r in table) == 2 * completed
    assert sum(r["goal_difference"] for r in table) == 0
    for r in table:
        assert r["points"] == 3 * r["won"] + r["drawn"] and r["won"] + r["drawn"] + r["lost"] == r["played"]
        assert len(r["form"]) == min(5, r["played"]) and set(r["form"]) <= {"W", "D", "L"}
    order = [(-r["points"], -r["goal_difference"], -r["goals_for"]) for r in table]
    assert order == sorted(order)


def test_fonts_come_from_this_site_only(forecasted):
    db, root = forecasted
    api = client(db, root, MORNING_11)
    font = api.get("/static/fonts/geist-sans-latin-400-normal.woff2")
    assert font.status_code == 200 and font.headers["content-type"] == "font/woff2"
    match_id = round_11(db)[0][0]
    for path in (*TABS, f"/match/{match_id}", f"/team/{round_11(db)[0][3]}"):
        html = api.get(path).text  # links out are fine; nothing may load from another site
        assert not re.search(r'(src|href)="https?://(?!github\.com/|www\.openstreetmap\.org/search)', html), path


# --- club badges and colours ---------------------------------------------------------

def test_fixture_rows_carry_both_clubs_badges(forecasted):
    db, root = forecasted
    clubs = load_clubs(root)
    rows = fixture_rows(client(db, root, MORNING_11).get("/").text)
    for row, (_, home, away, *_) in zip(rows, round_11(db)):
        codes = re.findall(r'<span class="badge"[^>]*aria-hidden="true">([A-Z]{3})</span>', row)
        assert codes == [clubs[home].code, clubs[away].code]


def test_the_match_page_draws_the_bar_in_the_two_clubs_colours(forecasted):
    db, root = forecasted
    match_id, home, away, *_ = round_11(db)[0]
    clubs = load_clubs(root)
    expected = match_colours(clubs[home], clubs[away])
    html = client(db, root, MORNING_11).get(f"/match/{match_id}").text
    style = re.search(r"<style>\.mc\{(.*?)\}@media \(prefers-color-scheme:dark\)\{\.mc\{(.*?)\}\}</style>", html)
    for mode, rules in (("light", style.group(1)), ("dark", style.group(2))):
        assert f'--home:{expected[mode]["home"]}' in rules and f'--away:{expected[mode]["away"]}' in rules
    assert '<div class="mc">' in html and html.count('class="badge lg"') == 2


def test_the_team_page_carries_the_clubs_colour(with_people):
    db, root = with_people
    clubs = load_clubs(root)
    html = client(db, root, MORNING_11).get(f"/team/{team_id(db, 'Arsenal')}").text
    assert f"--team:{match_colours(clubs['Arsenal'], None)['light']['home']}" in html
    assert '<span class="badge xl"' in html and ">ARS</span>" in html


# --- team pages ------------------------------------------------------------------------

@pytest.fixture
def with_people(forecasted):
    """A later download that also carries the teams file: invented managers and four-player squads."""
    db, root = forecasted
    add_snapshot(root, datetime(2026, 10, 30, 6, 0, tzinfo=UTC), finished_rounds=10, teams=club_people())
    load_live(db, root)
    return db, root


def team_id(db, name):
    return db.execute("SELECT team_id FROM teams WHERE team_name = %s", (name,)).fetchone()[0]


def test_the_match_page_names_both_managers_and_links_both_teams(with_people):
    db, root = with_people
    match_id, home, away, home_id, away_id = round_11(db)[0]
    html = client(db, root, MORNING_11).get(f"/match/{match_id}").text
    coaches = dict(db.execute("SELECT team_id, coach_name FROM teams WHERE team_id IN (%s, %s)",
                              (home_id, away_id)).fetchall())
    assert f"Manager {coaches[home_id]}" in text(html) and f"Manager {coaches[away_id]}" in text(html)
    assert f'href="/team/{home_id}"' in html and f'href="/team/{away_id}"' in html


def test_a_team_page_shows_its_manager_stadium_and_squad(with_people):
    db, root = with_people
    html = client(db, root, MORNING_11).get(f"/team/{team_id(db, 'Arsenal')}").text
    words = text(html)
    assert "<h1" in html and "Arsenal</h1>" in html and "Manager Coach 57 England" in words
    assert "Home stadium Emirates Stadium London Map" in words
    for group, player, age in (("Goalkeepers", "Player 57-0", 31), ("Defenders", "Player 57-1", 30),
                               ("Midfielders", "Player 57-2", 29), ("Forwards", "Player 57-3", 28)):
        assert f"{group} {player} {age} England" in words
    assert "as listed by football-data.org in the download of Fri 30 Oct 2026" in words


def test_a_team_page_lists_results_and_fixtures_and_the_next_match(with_people):
    db, root = with_people
    arsenal = team_id(db, "Arsenal")
    html = client(db, root, MORNING_11).get(f"/team/{arsenal}").text
    assert len(re.findall(r'<a class="tf"', html)) == 38  # the whole season: 10 played, 28 to come
    assert "Show 5 more" in html and "Show 23 more" in html
    results, coming = re.search(r'Results</h3>(.*?)Coming up</h3>(.*?)</section>', html, flags=re.S).groups()
    for part in (results, coming):  # oldest first, folded ones included, so the season reads top to bottom
        ids = [int(m) for m in re.findall(r'<a class="tf" href="/match/(\d+)"', part)]
        dates = [db.execute("SELECT match_date FROM matches WHERE match_id = %s", (i,)).fetchone()[0] for i in ids]
        assert dates == sorted(dates)
    assert results.index("<details") < results.index('<ol class="team-fixtures">')  # earlier results fold above
    assert coming.index('<ol class="team-fixtures">') < coming.index("<details")  # later fixtures fold below
    newest = re.findall(r'<a class="tf" href="/match/(\d+)"', results.split("</details>")[1])
    assert len(newest) == 5 and db.execute(
        """SELECT max(match_date) FROM matches WHERE status = 'completed' AND %s IN (home_team_id, away_team_id)""",
        (arsenal,)).fetchone()[0] == db.execute("SELECT match_date FROM matches WHERE match_id = %s",
                                                (int(newest[-1]),)).fetchone()[0]
    next_match = re.search(r'id="next-title".*?</section>', html, flags=re.S).group(0)
    match_id = next(r[0] for r in round_11(db) if arsenal in r[3:])
    assert f'href="/match/{match_id}"' in next_match and "Saturday 31 October. Forecast saved" in text(next_match)
    after = client(db, root, round_kickoff(10) + timedelta(hours=1)).get(f"/team/{arsenal}").text
    assert f'href="/match/{match_id}"' not in re.search(r'id="next-title".*?</section>', after, flags=re.S).group(0)


def test_the_table_excerpt_shows_two_clubs_either_side_and_fades_the_cut(with_people):
    db, root = with_people
    with read_only_factory(db)() as conn:
        table = league_table(conn, "2026-27")
    api = client(db, root, MORNING_11)
    for position in (1, 2, 3, 10, 18, 19, 20):
        html = api.get(f"/team/{table[position - 1]['team_id']}").text
        excerpt = re.search(r'<div class="excerpt([^"]*)">(.*?)</div>', html, flags=re.S)
        rows = excerpt.group(2)
        shown = [int(n) for n in re.findall(r'<tr(?: class="current")?><td class="num pos">(\d+)</td>', rows)]
        ghosts = [int(n) for n in re.findall(r'<tr class="ghost" aria-hidden="true"><td class="num pos">(\d+)', rows)]
        start = max(1, min(position - 2, 16))
        assert shown == list(range(start, start + 5)), position
        assert re.findall(r'<tr class="current"><td class="num pos">(\d+)', rows) == [str(position)]
        assert ghosts == ([start - 1] if start > 1 else []) + ([start + 5] if start + 5 <= 20 else []), position
        assert ("cut-top" in excerpt.group(1)) == (start > 1) and ("cut-bottom" in excerpt.group(1)) == (start < 16)


def test_a_team_page_shows_the_form_from_its_latest_forecast(with_people):
    db, root = with_people
    match_id, home, away, home_id, away_id = round_11(db)[0]
    features = db.execute("SELECT features FROM predictions WHERE match_id = %s", (match_id,)).fetchone()[0]
    for side, team in (("home", home_id), ("away", away_id)):
        words = text(client(db, root, MORNING_11).get(f"/team/{team}").text)
        assert (f'Points per game {features[f"{side}_ppg_last5"]:.2f} {features[f"{side}_ppg_last10"]:.2f}'
                in words), side
        assert f"From the forecast for {home} v {away} on Saturday 31 October" in words


def test_a_team_page_before_the_teams_file_says_what_is_missing(forecasted):
    db, root = forecasted
    words = text(client(db, root, MORNING_11).get(f"/team/{team_id(db, 'Arsenal')}").text)
    assert "Manager Not listed yet" in words and "The squad appears after the next morning's download" in words


def test_team_page_names_are_escaped(with_people):
    db, root = with_people
    db.execute("""UPDATE teams SET coach_name = '<b>Boss</b>',
                  squad = '[{"name": "<i>Kid</i>", "position": "Midfield", "date_of_birth": null, "nationality": null}]'
                  WHERE team_name = 'Arsenal'""")
    html = client(db, root, MORNING_11).get(f"/team/{team_id(db, 'Arsenal')}").text
    assert "&lt;b&gt;Boss&lt;/b&gt;" in html and "&lt;i&gt;Kid&lt;/i&gt;" in html
    assert "<b>Boss</b>" not in html and "<i>Kid</i>" not in html


def test_an_unknown_team_page_is_a_404_page(forecasted):
    db, root = forecasted
    page = client(db, root, MORNING_11).get("/team/999999")
    assert page.status_code == 404 and "No team with this number" in page.text


# --- no database needed -----------------------------------------------------------------

def test_every_live_club_has_a_checked_stadium():
    with (PROJECT_ROOT / "config" / "team_aliases.csv").open(newline="") as handle:
        live = {r["team_name"] for r in csv.DictReader(handle) if r["provider"] == "football-data.org"}
    grounds = stadiums(PROJECT_ROOT)
    assert sorted(live - grounds.keys()) == []  # add a row to config/stadiums.csv with each new club
    assert all(ground.strip() and town.strip() for ground, town in grounds.values())


@pytest.mark.parametrize("probabilities", [(1 / 3, 1 / 3, 1 / 3), (0.585, 0.205, 0.21), (0.995, 0.004, 0.001)])
def test_percentages_always_add_up_to_100(probabilities):
    shares = whole_percents(list(probabilities))
    assert sum(shares) == 100 and all(abs(s - p * 100) < 1 for s, p in zip(shares, probabilities))


def test_ordinals_and_ages():
    assert [ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 20, 21, 22, 23)] == [
        "1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "20th", "21st", "22nd", "23rd"]
    assert age_on("2000-11-21", date(2026, 11, 20)) == 25 and age_on("2000-11-21", date(2026, 11, 21)) == 26
    assert age_on(None, date(2026, 11, 21)) is None and age_on("unknown", date(2026, 11, 21)) is None


def test_the_default_day_is_today_then_the_next_match_day_then_the_last():
    days = [date(2026, 11, 21), date(2026, 11, 22), date(2026, 11, 28)]
    assert pick_day(days, date(2026, 11, 22)) == date(2026, 11, 22)
    assert pick_day(days, date(2026, 11, 24)) == date(2026, 11, 28)
    assert pick_day(days, date(2026, 12, 30)) == date(2026, 11, 28)
    assert pick_day([], date(2026, 12, 30)) == date(2026, 12, 30)
