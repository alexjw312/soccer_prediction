"""The public web pages, as four tabs plus a page per match.

- Fixtures (/): one match day at a time. Rows show the teams and the kickoff time or
  score; a match's forecast is on its own page.
- Table (/table): the league table, worked out from this season's results.
- Performance (/model-performance): how the saved forecasts have scored, and the
  one-time test before going live.
- How it works (/how-it-works): the pipeline, the model and the rules, in plain words.
- A match (/match/{id}): date, kickoff, stadium, managers, the forecast, each team's last
  five, the last five meetings, and what the model saw. Stadiums come from config/stadiums.csv.
- A team (/team/{id}): manager, stadium, next match, its part of the table, results and
  fixtures, what the model saw in its latest forecast, and the squad. Managers and squads
  are copied from football-data.org's teams file each morning (live_loading.update_club_people).

The pages only read saved rows (api.DASHBOARD_READS), so what a visitor sees is what
was stored before kickoff. Team names come from the database and are always escaped.
The font (Geist, SIL Open Font License) is served from this package, so the pages
make no requests to other sites.

Each club has a round badge in its colours with its three-letter code (no crests: they
are the clubs' trademarks). On a match page the home/draw/away bar uses the two clubs'
colours, checked for visibility and clashes by team_colours.py, with the draw in neutral
gray; the default blue and orange are the fallback. After full time the outcome that happened keeps its colour and the
other two fade, so the bar shows the forecast and the result together. Text never
takes a data colour; a colour always sits beside words that say the same thing.
Times are shown in the service's local time zone (UK time).
"""
import csv
import json
import math
from bisect import bisect_left
from datetime import date, datetime
from html import escape
from pathlib import Path
from urllib.parse import quote_plus

from .team_colours import Club, badge, match_colours

REPOSITORY = "https://github.com/alexjw312/soccer_prediction"
ATTRIBUTION = "Football data provided by the Football-Data.org API"
HISTORY_CREDIT = "Historical results: OpenFootball football.json (CC0)"

TABS = (("fixtures", "/", "Fixtures"), ("table", "/table", "Table"),
        ("performance", "/model-performance", "Performance"), ("how", "/how-it-works", "How it works"))

# (result code, css name, probability column)
OUTCOMES = (("H", "home", "p_home"), ("D", "draw", "p_draw"), ("A", "away", "p_away"))
ENDED = {"postponed": "Postponed", "abandoned": "Abandoned", "canceled": "Cancelled"}

DAY_SQL = """
    SELECT m.match_id, m.match_date, m.kickoff_utc, m.status, m.source_round, m.home_goals, m.away_goals,
           m.result, h.team_name AS home, a.team_name AS away, p.generated_at_utc
    FROM matches m
    JOIN teams h ON h.team_id = m.home_team_id
    JOIN teams a ON a.team_id = m.away_team_id
    LEFT JOIN LATERAL (SELECT generated_at_utc FROM predictions p
                       WHERE p.match_id = m.match_id AND p.forecast_for_date = m.match_date
                       ORDER BY p.generated_at_utc DESC LIMIT 1) p ON true
    WHERE m.season = %s AND m.match_date = %s
    ORDER BY m.kickoff_utc NULLS LAST, h.team_name
"""

TABLE_SQL = """
    WITH clubs AS (SELECT home_team_id AS team_id FROM matches WHERE season = %(season)s
                   UNION SELECT away_team_id FROM matches WHERE season = %(season)s),
    games AS (SELECT home_team_id AS team_id, home_goals AS gf, away_goals AS ga, match_date, kickoff_utc
              FROM matches WHERE season = %(season)s AND status = 'completed'
              UNION ALL
              SELECT away_team_id, away_goals, home_goals, match_date, kickoff_utc
              FROM matches WHERE season = %(season)s AND status = 'completed')
    SELECT t.team_id, t.team_name, count(g.gf) AS played,
           count(*) FILTER (WHERE g.gf > g.ga) AS won,
           count(*) FILTER (WHERE g.gf = g.ga) AS drawn,
           count(*) FILTER (WHERE g.gf < g.ga) AS lost,
           coalesce(sum(g.gf), 0) AS goals_for, coalesce(sum(g.ga), 0) AS goals_against,
           (array_agg(CASE WHEN g.gf > g.ga THEN 'W' WHEN g.gf = g.ga THEN 'D' ELSE 'L' END
                      ORDER BY g.match_date DESC, g.kickoff_utc DESC NULLS LAST)
                FILTER (WHERE g.gf IS NOT NULL))[1:5] AS form
    FROM clubs c
    JOIN teams t ON t.team_id = c.team_id
    LEFT JOIN games g ON g.team_id = c.team_id
    GROUP BY t.team_id, t.team_name
"""

RECENT_SQL = """
    SELECT m.match_id, m.match_date, m.home_team_id = %(team)s AS at_home,
           CASE WHEN m.home_team_id = %(team)s THEN a.team_name ELSE h.team_name END AS opponent,
           CASE WHEN m.home_team_id = %(team)s THEN m.home_goals ELSE m.away_goals END AS gf,
           CASE WHEN m.home_team_id = %(team)s THEN m.away_goals ELSE m.home_goals END AS ga
    FROM matches m
    JOIN teams h ON h.team_id = m.home_team_id
    JOIN teams a ON a.team_id = m.away_team_id
    WHERE m.status = 'completed' AND %(team)s IN (m.home_team_id, m.away_team_id) AND m.match_date < %(before)s
    ORDER BY m.match_date DESC, m.kickoff_utc DESC NULLS LAST
    LIMIT 5
"""

HEAD_TO_HEAD_SQL = """
    SELECT m.match_id, m.season, m.match_date, m.home_team_id, h.team_name AS home, a.team_name AS away,
           m.home_goals, m.away_goals, m.result
    FROM matches m
    JOIN teams h ON h.team_id = m.home_team_id
    JOIN teams a ON a.team_id = m.away_team_id
    WHERE m.status = 'completed' AND m.match_date < %(before)s
      AND ((m.home_team_id = %(one)s AND m.away_team_id = %(other)s)
        OR (m.home_team_id = %(other)s AND m.away_team_id = %(one)s))
    ORDER BY m.match_date DESC, m.kickoff_utc DESC NULLS LAST
    LIMIT 5
"""

TEAM_FIXTURES_SQL = """
    SELECT m.match_id, m.match_date, m.kickoff_utc, m.status, m.home_goals, m.away_goals, m.result,
           h.team_name AS home, a.team_name AS away, m.home_team_id = %(team)s AS at_home,
           CASE WHEN m.home_team_id = %(team)s THEN a.team_name ELSE h.team_name END AS opponent,
           CASE WHEN m.home_team_id = %(team)s THEN m.home_goals ELSE m.away_goals END AS gf,
           CASE WHEN m.home_team_id = %(team)s THEN m.away_goals ELSE m.home_goals END AS ga,
           p.generated_at_utc
    FROM matches m
    JOIN teams h ON h.team_id = m.home_team_id
    JOIN teams a ON a.team_id = m.away_team_id
    LEFT JOIN LATERAL (SELECT generated_at_utc FROM predictions p
                       WHERE p.match_id = m.match_id AND p.forecast_for_date = m.match_date
                       ORDER BY p.generated_at_utc DESC LIMIT 1) p ON true
    WHERE m.season = %(season)s AND %(team)s IN (m.home_team_id, m.away_team_id)
    ORDER BY m.match_date, m.kickoff_utc NULLS LAST
"""

LATEST_FORM_SQL = """
    SELECT p.match_id, p.forecast_for_date, p.feature_cutoff_date, p.features,
           m.home_team_id = %(team)s AS at_home, h.team_name AS home, a.team_name AS away
    FROM predictions p
    JOIN matches m ON m.match_id = p.match_id
    JOIN teams h ON h.team_id = m.home_team_id
    JOIN teams a ON a.team_id = m.away_team_id
    WHERE %(team)s IN (m.home_team_id, m.away_team_id)
    ORDER BY p.generated_at_utc DESC
    LIMIT 1
"""

SCORED_SQL = """
    SELECT r.match_id, r.match_date, h.team_name AS home, a.team_name AS away, m.home_goals, m.away_goals,
           r.result, r.p_home, r.p_draw, r.p_away, r.log_loss
    FROM prediction_results r
    JOIN matches m ON m.match_id = r.match_id
    JOIN teams h ON h.team_id = m.home_team_id
    JOIN teams a ON a.team_id = m.away_team_id
    WHERE r.match_status = 'completed' AND r.on_forecast_date
    ORDER BY m.kickoff_utc DESC NULLS LAST, r.match_date DESC, h.team_name
"""


# --- reading ------------------------------------------------------------------------

def match_days(conn, season: str) -> list[date]:
    return [r["match_date"] for r in conn.execute(
        "SELECT DISTINCT match_date FROM matches WHERE season = %s ORDER BY match_date", (season,)).fetchall()]


def pick_day(days: list[date], today: date) -> date:
    """Today if there are matches today, otherwise the next match day, otherwise the last one."""
    if not days or today in days:
        return today
    later = [d for d in days if d > today]
    return later[0] if later else days[-1]


def day_rows(conn, season: str, day: date) -> list[dict]:
    return conn.execute(DAY_SQL, (season, day)).fetchall()


def league_table(conn, season: str) -> list[dict]:
    rows = conn.execute(TABLE_SQL, {"season": season}).fetchall()
    for r in rows:
        r["points"] = 3 * r["won"] + r["drawn"]
        r["goal_difference"] = r["goals_for"] - r["goals_against"]
        r["form"] = list(reversed(r["form"] or []))  # oldest first, newest on the right
    rows.sort(key=lambda r: (-r["points"], -r["goal_difference"], -r["goals_for"], r["team_name"]))
    return rows


def historical_shares(conn, cohort: list[str]) -> dict:
    """Home/draw/away shares over the historical seasons: the baseline forecast."""
    rows = conn.execute(
        """SELECT result, count(*)::float / sum(count(*)) OVER () AS share FROM matches
           WHERE season = ANY(%s) AND status = 'completed' GROUP BY result""", (cohort,)).fetchall()
    return {r["result"]: r["share"] for r in rows}


def scored_forecasts(conn) -> list[dict]:
    return conn.execute(SCORED_SQL).fetchall()


def deployed_model(conn) -> dict | None:
    return conn.execute(
        """SELECT model_run_id, model_name, settings, training_seasons, training_matches,
                  cardinality(features) AS feature_count
           FROM model_runs WHERE purpose = 'deployment'
           ORDER BY created_at DESC, model_run_id DESC LIMIT 1""").fetchone()


def first_season(conn) -> str | None:
    return conn.execute("SELECT min(season) AS season FROM matches").fetchone()["season"]


def match_detail(conn, match_id: int) -> dict | None:
    match = conn.execute(
        """SELECT m.match_id, m.season, m.match_date, m.kickoff_utc, m.status, m.source_round,
                  m.home_goals, m.away_goals, m.result, m.home_team_id, m.away_team_id,
                  h.team_name AS home, a.team_name AS away, h.coach_name AS home_coach, a.coach_name AS away_coach
           FROM matches m JOIN teams h ON h.team_id = m.home_team_id JOIN teams a ON a.team_id = m.away_team_id
           WHERE m.match_id = %s""", (match_id,)).fetchone()
    if match is None:
        return None
    forecasts = conn.execute(
        """SELECT p.prediction_id, p.forecast_for_date, p.generated_at_utc, p.feature_cutoff_date, p.kickoff_utc,
                  p.data_snapshot, p.p_home, p.p_draw, p.p_away, p.features, p.model_run_id,
                  r.model_name, r.training_seasons, r.training_matches
           FROM predictions p JOIN model_runs r ON r.model_run_id = p.model_run_id
           WHERE p.match_id = %s ORDER BY p.generated_at_utc""", (match_id,)).fetchall()
    on_date = [f for f in forecasts if f["forecast_for_date"] == match["match_date"]]
    match["forecast"] = on_date[-1] if on_date else None
    match["other_forecasts"] = [f for f in forecasts if f is not match["forecast"]]
    match["recent"] = {side: conn.execute(RECENT_SQL, {"team": match[f"{side}_team_id"],
                                                       "before": match["match_date"]}).fetchall()
                       for side in ("home", "away")}
    match["head_to_head"] = conn.execute(HEAD_TO_HEAD_SQL, {
        "one": match["home_team_id"], "other": match["away_team_id"], "before": match["match_date"]}).fetchall()
    match["history_from"] = first_season(conn)
    return match


def team_detail(conn, team_id: int, season: str) -> dict | None:
    team = conn.execute(
        """SELECT team_id, team_name, coach_name, coach_nationality, squad, squad_snapshot
           FROM teams WHERE team_id = %s""", (team_id,)).fetchone()
    if team is None:
        return None
    team["fixtures"] = conn.execute(TEAM_FIXTURES_SQL, {"team": team_id, "season": season}).fetchall()
    team["form"] = conn.execute(LATEST_FORM_SQL, {"team": team_id}).fetchone()
    return team


def stadiums(root: Path) -> dict[str, tuple[str, str]]:
    """Each club's home ground and town, from config/stadiums.csv (a checked list, see that file's test).

    football-data.org's team data also names stadiums, but several were years out of date
    (Everton at Goodison Park, Brentford at Griffin Park), so a short checked list is used.
    """
    with (root / "config" / "stadiums.csv").open(newline="", encoding="utf-8") as handle:
        return {r["team_name"]: (r["stadium"], r["town"]) for r in csv.DictReader(handle)}


def final_test_summary(root: Path) -> dict | None:
    """The one-time final test (reports/final_test.json), or None before it has been run."""
    path = root / "reports" / "final_test.json"
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        return {"matches": report["test"]["matches"], "seasons": report["test"]["seasons"],
                "model": report["scores"][report["selected_model"]],
                "baseline": report["scores"]["class_frequency_baseline"],
                "uniform_log_loss": report["reference_uniform_log_loss"],
                "difference": report["selected_vs_baseline"]}
    except (OSError, ValueError, KeyError, TypeError):
        return None


# --- small formatting helpers ---------------------------------------------------------

def whole_percents(probabilities: list[float]) -> list[int]:
    """Rounded percentages that still add up to 100 (largest remainders get the spare points)."""
    raw = [p * 100 for p in probabilities]
    floors = [math.floor(x) for x in raw]
    spare = 100 - sum(floors)
    for i in sorted(range(len(raw)), key=lambda i: raw[i] - floors[i], reverse=True)[:max(spare, 0)]:
        floors[i] += 1
    return floors


def long_day(day: date, year: bool = False) -> str:
    return f"{day:%A} {day.day} {day:%B}" + (f" {day.year}" if year else "")


def short_day(day: date, year: bool = False) -> str:
    return f"{day:%a} {day.day} {day:%b}" + (f" {day.year}" if year else "")


def clock(moment: datetime | None, zone) -> str:
    return moment.astimezone(zone).strftime("%H:%M") if moment else "–"


def season_label(season: str) -> str:
    return season.replace("-", "/")


def log_loss(row: dict) -> float | None:
    column = {code: col for code, _, col in OUTCOMES}.get(row.get("result"))
    return -math.log(row[column]) if column and row.get(column) else None


def outcome_name(code: str, home: str, away: str) -> str:
    return {"H": f"{home} win", "D": "Draw", "A": f"{away} win"}[code]


def ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def age_on(born: str | None, day: date) -> int | None:
    try:
        birth = date.fromisoformat(born)
    except (TypeError, ValueError):
        return None
    return day.year - birth.year - ((day.month, day.day) < (birth.month, birth.day))


def club_badge(name: str, clubs: dict[str, Club], size: str = "") -> str:
    """A round badge in the club's colours with its three-letter code; the name beside it says the same."""
    code, background, text = badge(clubs.get(name), name)
    return (f'<span class="badge{" " + size if size else ""}" style="--b:{background};--t:{text}" '
            f'aria-hidden="true">{escape(code)}</span>')


def team_link(team_id: int, name: str) -> str:
    return f'<a class="team-link" href="/team/{team_id}">{escape(name)}</a>'


def signed(n: int) -> str:
    """Goal difference as football tables print it: +3, 0, -2."""
    return f"{n:+d}" if n else "0"


def result_letter(gf: int, ga: int) -> str:
    return "W" if gf > ga else "D" if gf == ga else "L"


def forecast_words(row: dict, home: str, away: str) -> str:
    shares = whole_percents([row[col] for _, _, col in OUTCOMES])
    return f"{home} win {shares[0]}%, draw {shares[1]}%, {away} win {shares[2]}%."


# --- pieces ---------------------------------------------------------------------------

def split_bar(row: dict, *, result: str | None = None, min_label: float = 0.08) -> str:
    """The home/draw/away bar. Blocks too narrow for their percentage print none."""
    shares = whole_percents([row[col] for _, _, col in OUTCOMES])
    blocks = []
    for (code, name, col), share in zip(OUTCOMES, shares):
        faded = " faded" if result and code != result else ""
        label = f"{share}%" if row[col] >= min_label else ""
        blocks.append(f'<span class="seg {name}{faded}" style="flex-grow:{row[col]:.4f}">{label}</span>')
    return f'<div class="bar" aria-hidden="true">{"".join(blocks)}</div>'


def chip(letter: str) -> str:
    names = {"W": "won", "D": "drew", "L": "lost"}
    return f'<span class="chip {letter.lower()}" title="{names[letter]}">{letter}</span>'


def fixture_row(r: dict, zone, clubs: dict[str, Club]) -> str:
    e = escape
    home, away = e(r["home"]), e(r["away"])
    if r["status"] == "completed":
        middle = (f'<span class="score">{r["home_goals"]}<span class="dash">–</span>{r["away_goals"]}</span>'
                  f'<span class="state">Full time</span>')
        words = f"{home} {r['home_goals']}, {away} {r['away_goals']}, full time."
    elif r["status"] in ENDED:
        middle = f'<span class="state ended">{ENDED[r["status"]]}</span>'
        words = f"{home} v {away}, {ENDED[r['status']].lower()}."
    else:
        middle = f'<span class="time">{clock(r["kickoff_utc"], zone)}</span>'
        words = f"{home} v {away}, kickoff {clock(r['kickoff_utc'], zone)}."
    if r["generated_at_utc"] is not None:
        words += " Forecast saved."
    return (f'<li><a class="fx" href="/match/{r["match_id"]}" aria-label="{words}">'
            f'<span class="side home"><span class="nm">{home}</span>{club_badge(r["home"], clubs)}</span>'
            f'<span class="mid">{middle}</span>'
            f'<span class="side away">{club_badge(r["away"], clubs)}<span class="nm">{away}</span></span></a></li>')


def day_note(rows: list[dict], day: date, today: date, now: datetime, zone) -> str:
    if not rows:
        return "No Premier League matches on this day."
    saved = [r["generated_at_utc"] for r in rows if r["generated_at_utc"] is not None]
    if saved:
        first = min(saved).astimezone(zone)
        note = (f"Forecasts saved at {first:%H:%M}"
                f"{'' if first.date() == day else ' on ' + short_day(first.date())}, before the first kickoff. "
                "Open a match to see its forecast.")
    elif day > today:
        note = "Forecasts are saved on the morning of match day, before the first kickoff."
    elif day == today:
        note = "Today's forecasts are saved before the first kickoff."
    else:
        note = "No forecasts were saved for this day."
    if any(r["status"] == "scheduled" and r["kickoff_utc"] and r["kickoff_utc"] <= now for r in rows):
        note += " Results arrive with the next morning's download."
    return note


def day_strip(days: list[date], selected: date, today: date, size: int = 7) -> str:
    if not days:
        return ""
    i = bisect_left(days, selected)
    start = max(0, min(i - size // 2, len(days) - size))
    shown = days[start:start + size]
    before = [d for d in days if d < selected]
    after = [d for d in days if d > selected]

    def arrow(target: date | None, symbol: str, label: str) -> str:
        if target is None:
            return f'<span class="step off" aria-hidden="true">{symbol}</span>'
        return f'<a class="step" href="/?date={target.isoformat()}" aria-label="{label}">{symbol}</a>'

    pills = "".join(
        f'<a class="day" href="/?date={d.isoformat()}"{" aria-current=\"date\"" if d == selected else ""}>'
        f'<span class="dow">{"Today" if d == today else f"{d:%a}"}</span><span class="dom">{d.day} {d:%b}</span></a>'
        for d in shown)
    return (f'<nav class="strip" aria-label="Match days">'
            f'{arrow(before[-1] if before else None, "‹", "Previous match day")}'
            f'<div class="days">{pills}</div>'
            f'{arrow(after[0] if after else None, "›", "Next match day")}</nav>')


def table_html(table: list[dict], clubs: dict[str, Club], highlight: dict | None = None) -> str:
    highlight = highlight or {}
    rows = "".join(
        f'<tr class="{highlight.get(r["team_id"], "")}"><td class="num pos">{i}</td>'
        f'<td class="club"><span class="club-in">{club_badge(r["team_name"], clubs, "sm")}'
        f'{team_link(r["team_id"], r["team_name"])}</span></td>'
        f'<td class="num">{r["played"]}</td><td class="num wide">{r["won"]}</td><td class="num wide">{r["drawn"]}</td><td class="num wide">{r["lost"]}</td>'
        f'<td class="num wide">{r["goals_for"]}</td><td class="num wide">{r["goals_against"]}</td>'
        f'<td class="num">{signed(r["goal_difference"])}</td><td class="num pts">{r["points"]}</td>'
        f'<td class="last"><span class="form">{"".join(chip(x) for x in r["form"])}</span></td></tr>'
        for i, r in enumerate(table, start=1))
    return (f'<table class="league"><caption class="sr">League table</caption>'
            f'<thead><tr><th class="num" scope="col"><span class="sr">Position</span></th>'
            f'<th scope="col">Club</th><th class="num" scope="col" title="Played">P</th>'
            f'<th class="num wide" scope="col" title="Won">W</th><th class="num wide" scope="col" title="Drawn">D</th>'
            f'<th class="num wide" scope="col" title="Lost">L</th>'
            f'<th class="num wide" scope="col" title="Goals scored">GF</th>'
            f'<th class="num wide" scope="col" title="Goals conceded">GA</th>'
            f'<th class="num" scope="col" title="Goal difference">GD</th><th class="num" scope="col">Pts</th>'
            f'<th scope="col">Last five</th></tr></thead><tbody>{rows}</tbody></table>')


def scores_table(rows: list[tuple[str, float, float | None]]) -> str:
    body = "".join(
        f'<tr><th scope="row">{name}</th><td class="num">{loss:.3f}</td>'
        f'<td class="num">{"–" if right is None else f"{right:.0%}"}</td></tr>' for name, loss, right in rows)
    return (f'<table class="scores"><thead><tr><th scope="col"><span class="sr">Forecast</span></th>'
            f'<th class="num" scope="col">Log loss</th><th class="num" scope="col">Most likely outcome right</th>'
            f'</tr></thead><tbody>{body}</tbody></table>')


def status_line(info: dict, zone) -> str:
    if info["latest_snapshot_utc"] is None:
        return '<p class="status warn"><span class="icon" aria-hidden="true">!</span>No live data yet</p>'
    moment = info["latest_snapshot_utc"].astimezone(zone)
    if info["status"] == "stale":
        return (f'<p class="status warn"><span class="icon" aria-hidden="true">!</span>'
                f'Data is {info["latest_snapshot_age_hours"]:.0f} hours old. New forecasts wait for a fresh download.</p>')
    return f'<p class="status">Data updated {short_day(moment.date())}, {moment:%H:%M}</p>'


# --- tab pages ------------------------------------------------------------------------

def fixtures_page(*, rows: list[dict], days: list[date], day: date, today: date, now: datetime, zone,
                  season: str, info: dict, clubs: dict[str, Club]) -> str:
    rounds = sorted({r["source_round"] for r in rows if r["source_round"]})
    count = f'{len(rows)} {"match" if len(rows) == 1 else "matches"}' if rows else ""
    meta = ", ".join(x for x in (", ".join(rounds), count) if x)
    body = f"""
    {day_strip(days, day, today)}
    <section class="sheet" aria-labelledby="day-title">
      <header class="sheet-head">
        <h1 id="day-title">{long_day(day)}</h1>
        <p class="meta">{escape(meta)}{". " if meta else ""}Premier League {season_label(season)}. Times are UK time.</p>
        <p class="note">{day_note(rows, day, today, now, zone)}</p>
      </header>
      {f'<ol class="fixtures">{"".join(fixture_row(r, zone, clubs) for r in rows)}</ol>' if rows else ""}
    </section>"""
    return page("Premier League forecasts", body, info, zone, active="fixtures")


def table_page(*, table: list[dict], season: str, info: dict, zone, clubs: dict[str, Club]) -> str:
    body = f"""
    <section class="sheet" aria-labelledby="table-title">
      <header class="sheet-head"><h1 id="table-title">Table</h1>
        <p class="meta">Premier League {season_label(season)}, from the results downloaded so far.</p></header>
      {table_html(table, clubs)}
      <p class="fine pad">Ordered by points, then goal difference, then goals scored. Last five: newest on the right.</p>
    </section>"""
    return page(f"Table, Premier League {season_label(season)}", body, info, zone, active="table")


def performance_page(*, perf: dict, scored: list[dict], final: dict | None, info: dict, zone) -> str:
    if perf.get("matches"):
        live = (f'<p class="note">{perf["matches"]} forecast {"match" if perf["matches"] == 1 else "matches"} '
                f'played so far. With few matches, these numbers move a lot.</p>'
                + scores_table([("These forecasts", perf["log_loss"], perf["accuracy"]),
                                ("Historical shares", perf["baseline_log_loss"], None),
                                ("One third each", perf["uniform_log_loss"], None)]))
    else:
        live = ('<p class="note">Nothing to score yet. Each forecast is scored once its match has been played, '
                'with the result from the next morning\'s download.</p>')

    if scored:
        items = []
        for r in scored:
            shares = whole_percents([r[col] for _, _, col in OUTCOMES])
            cells = "".join(f'<td class="num{" hit" if r["result"] == code else ""}">{share}%</td>'
                            for (code, _, _), share in zip(OUTCOMES, shares))
            items.append(
                f'<tr><td class="when">{short_day(r["match_date"])}</td>'
                f'<td><a href="/match/{r["match_id"]}">{escape(r["home"])} {r["home_goals"]}–{r["away_goals"]} '
                f'{escape(r["away"])}</a></td>{cells}<td class="num">{r["log_loss"]:.3f}</td></tr>')
        record = (f'<div class="scroll"><table class="record"><thead><tr><th scope="col">Date</th>'
                  f'<th scope="col">Match</th><th class="num" scope="col">Home</th><th class="num" scope="col">Draw</th>'
                  f'<th class="num" scope="col">Away</th><th class="num" scope="col">Log loss</th></tr></thead>'
                  f'<tbody>{"".join(items)}</tbody></table></div>'
                  f'<p class="fine pad">The forecast\'s chance for what happened is in bold.</p>')
    else:
        record = '<p class="note pad">Played matches with a saved forecast will be listed here.</p>'

    tested = ""
    if final:
        model, base, diff = final["model"], final["baseline"], final["difference"]
        seasons = ", ".join(season_label(s) for s in final["seasons"])
        draws = model.get("draws", {})
        draw_line = (f' Draws were the weak spot: {draws["observed"]:.0%} of matches ended level, the model gave '
                     f'draws {draws["mean_forecast"]:.0%} on average and never made one its most likely outcome.'
                     if {"observed", "mean_forecast"} <= draws.keys() else "")
        tested = f"""
    <section class="sheet" aria-labelledby="tested"><h2 id="tested">Tested before going live</h2>
      <p class="note pad">Before launch, the model was scored once on the {seasons} season ({final["matches"]} matches),
      which played no part in choosing it.</p>
      {scores_table([("The model", model["log_loss"], model["accuracy"]),
                     ("Historical shares", base["log_loss"], base["accuracy"]),
                     ("One third each", final["uniform_log_loss"], None)])}
      <p class="fine pad">Better than historical shares by {-diff["mean"]:.3f} (95% interval {-diff["interval"][1]:.3f}
      to {-diff["interval"][0]:.3f}).{draw_line}
      <a href="{REPOSITORY}/blob/main/docs/EVALUATION.md">Full evaluation</a></p>
    </section>"""

    body = f"""
    <section class="sheet" aria-labelledby="perf-title">
      <header class="sheet-head"><h1 id="perf-title">How the forecasts are doing</h1>
        <p class="meta">Every forecast is scored with log loss: the penalty for the chance it gave to what actually
        happened. Giving it 100% scores 0; one third each always scores {math.log(3):.3f}. Lower is better.</p></header>
      <div class="pad">{live}</div>
    </section>
    <section class="sheet" aria-labelledby="record-title"><h2 id="record-title">Every scored forecast</h2>
      {record}</section>
    {tested}"""
    return page("Forecast performance", body, info, zone, active="performance")


LINKS = "".join(f'<a href="{href}">{label}</a>' for href, label in (
    (REPOSITORY, "Source code"), (f"{REPOSITORY}/blob/main/docs/ARCHITECTURE.md", "Architecture"),
    (f"{REPOSITORY}/blob/main/docs/EVALUATION.md", "Evaluation"), ("/docs", "API")))


def how_page(*, model: dict | None, cutoff_days: int, max_age_hours: int, info: dict, zone) -> str:
    if model:
        seasons = sorted(model["training_seasons"])
        model_text = (f'The forecasts come from a {escape(model["model_name"].replace("_", " "))} trained on '
                      f'{model["training_matches"]:,} Premier League matches from {season_label(seasons[0])} to '
                      f'{season_label(seasons[-1])}. For each match it reads {model["feature_count"]} numbers: each '
                      f'team\'s games played, points per game, goals scored and goals conceded over its last 5 and '
                      f'last 10 league games.')
    else:
        model_text = "No model has been trained for live use yet."
    body = f"""
    <section class="sheet prose" aria-labelledby="how-title">
      <header class="sheet-head"><h1 id="how-title">How it works</h1>
        <p class="meta">Each Premier League match gets three chances, home win, draw and away win, adding up to 100%.
        They are saved on match day before kickoff, never changed, and scored once the result is in.</p></header>
      <div class="pad">
      <h2>Every day</h2>
      <ol class="steps">
        <li>Each morning a scheduled job downloads the season's fixtures and results from football-data.org,
          checks them and stores them.</li>
        <li>On match day it works out each team's recent form and saves a forecast for every match that has not
          kicked off.</li>
        <li>The next morning's download brings the results, and each forecast is scored on the Performance tab.</li>
      </ol>
      <h2>The model</h2>
      <p>{model_text} Only matches played at least {abs(cutoff_days)} days before match day count, so a result
      that is published late can never slip into a forecast.</p>
      <p>It was chosen by comparing models on past seasons with a rule written down before any were scored, then
      tested once on a season it had never seen. The Performance tab has the numbers.</p>
      <h2>Rules that keep it honest</h2>
      <ul class="rules">
        <li>A forecast is saved only before kickoff; the database refuses anything later.</li>
        <li>Saved forecasts are never edited. If a match is moved, its old forecast is kept for the record but
          not scored.</li>
        <li>If the newest download is more than {max_age_hours} hours old, or an earlier result is still
          missing, no forecast is saved rather than a quietly wrong one.</li>
        <li>Every download is kept, and a finished result that later changes stops the job for a person to
          check.</li>
      </ul>
      <h2>What it does not know</h2>
      <p>Only results go in: no line-ups, injuries, transfers, managers, odds or expected goals. It also gives
      draws less than they deserve: about one match in four ends level, but a draw is almost never the single
      most likely outcome for this kind of model.</p>
      <h2>Data</h2>
      <p>{escape(ATTRIBUTION)}: fixtures and results for the current season.
      {escape(HISTORY_CREDIT)}: the past seasons used for training and for head-to-head records.
      Stadium names and club colours come from short lists checked by hand.</p>
      <p class="links-row">{LINKS}</p>
      </div>
    </section>"""
    return page("How it works", body, info, zone, active="how")


# --- the match page -------------------------------------------------------------------

def _forecast_section(match: dict, zone) -> str:
    e = escape
    forecast, played = match["forecast"], match["status"] == "completed"
    if forecast is None:
        if match["status"] == "scheduled":
            text = (f'The forecast for this match is saved on the morning of {long_day(match["match_date"])}, '
                    f'before kickoff. What the model saw, the form numbers it used, appears with it.')
        else:
            text = "No forecast was saved for this match."
        return (f'<section class="sheet" aria-labelledby="forecast-title"><h2 id="forecast-title">Forecast</h2>'
                f'<p class="note pad">{text}</p></section>')
    shares = whole_percents([forecast[col] for _, _, col in OUTCOMES])
    cells = []
    for (code, name, _), share in zip(OUTCOMES, shares):
        happened = played and match["result"] == code
        cells.append(f'<div class="pick{" happened" if happened else ""}{" dim" if played and not happened else ""}">'
                     f'<span class="pick-name"><span class="sw {name}" aria-hidden="true"></span>'
                     f'{e(outcome_name(code, match["home"], match["away"]))}</span>'
                     f'<span class="pick-value">{share}%</span>'
                     f'{"<span class=tag>What happened</span>" if happened else ""}</div>')
    words = forecast_words(forecast, e(match["home"]), e(match["away"]))
    saved = forecast["generated_at_utc"].astimezone(zone)
    lead = forecast["kickoff_utc"] - forecast["generated_at_utc"]
    hours, minutes = divmod(int(lead.total_seconds() // 60), 60)
    score = ""
    if played:
        loss = log_loss({**forecast, "result": match["result"]})
        score = (f' Its score: {loss:.3f} in log loss (lower is better; one third each would score '
                 f'{math.log(3):.3f}).')
    return f"""
    <section class="sheet" aria-labelledby="forecast-title"><h2 id="forecast-title">Forecast</h2>
      <div class="pad">
        <div class="picks" role="group" aria-label="{words}">{"".join(cells)}</div>
        {split_bar(forecast, result=match["result"] if played else None)}
        <p class="fine">Saved {short_day(saved.date())} at {saved:%H:%M} UK time, {hours} h {minutes:02d} min before
        kickoff, and never changed.{score}</p>
      </div>
    </section>"""


def _recent_list(recent: list[dict], clubs: dict[str, Club]) -> str:
    if not recent:
        return '<p class="fine">No league games in this project\'s data.</p>'
    items = []
    for g in recent:
        r = result_letter(g["gf"], g["ga"])
        venue = "H" if g["at_home"] else "A"
        items.append(f'<li>{chip(r)}<span class="opp">{club_badge(g["opponent"], clubs, "sm")}'
                     f'<span class="nm">{escape(g["opponent"])}</span></span>'
                     f'<span class="venue" title="{"home" if g["at_home"] else "away"}">{venue}</span>'
                     f'<span class="num sc">{g["gf"]}–{g["ga"]}</span>'
                     f'<span class="date">{short_day(g["match_date"])}</span></li>')
    return f'<ol class="recent">{"".join(items)}</ol>'


def _head_to_head(match: dict, clubs: dict[str, Club]) -> str:
    meetings, home_id = match["head_to_head"], match["home_team_id"]
    since = f" since {season_label(match['history_from'])}" if match["history_from"] else ""
    if not meetings:
        return (f'<p class="note pad">No Premier League meetings{since} in this project\'s data.</p>')
    wins = {"home": 0, "away": 0, "draw": 0}
    rows = []
    for m in meetings:
        if m["result"] == "D":
            wins["draw"] += 1
        elif (m["result"] == "H") == (m["home_team_id"] == home_id):
            wins["home"] += 1
        else:
            wins["away"] += 1
        winner = {"H": "home", "A": "away"}.get(m["result"], "")
        rows.append(f'<li><span class="date">{short_day(m["match_date"], year=True)}</span>'
                    f'<span class="h2h-home{" won" if winner == "home" else ""}"><span class="nm">{escape(m["home"])}</span>'
                    f'{club_badge(m["home"], clubs, "sm")}</span>'
                    f'<span class="num sc">{m["home_goals"]}–{m["away_goals"]}</span>'
                    f'<span class="h2h-away{" won" if winner == "away" else ""}">{club_badge(m["away"], clubs, "sm")}'
                    f'<span class="nm">{escape(m["away"])}</span></span></li>')
    n = len(meetings)
    summary = (f'{escape(match["home"])} {wins["home"]} {"win" if wins["home"] == 1 else "wins"}, '
               f'{wins["draw"]} {"draw" if wins["draw"] == 1 else "draws"}, '
               f'{escape(match["away"])} {wins["away"]} {"win" if wins["away"] == 1 else "wins"}')
    return (f'<p class="note pad">Last {n} {"meeting" if n == 1 else "meetings"}: {summary}.</p>'
            f'<ol class="h2h">{"".join(rows)}</ol>'
            f'<p class="fine pad">Premier League meetings{since}, the seasons this project holds.</p>')


def _features_section(match: dict) -> str:
    forecast = match["forecast"]
    if forecast is None:
        return ""
    values = forecast["features"] or {}
    if isinstance(values, str):
        values = json.loads(values)
    measures = [("Points per game", "ppg", 3.0), ("Scored per game", "goals_for", None),
                ("Conceded per game", "goals_against", None)]

    def cell(v, side, scale):
        if v is None:
            return f'<td class="num {side}">–</td>'
        width = max(0.0, min(1.0, v / scale)) * 100
        return (f'<td class="num {side}"><span class="val">{v:.2f}</span><span class="track" aria-hidden="true">'
                f'<span class="fill {side}" style="width:{width:.1f}%"></span></span></td>')

    rows = []
    for window in (5, 10):
        rows.append(f'<tr class="group"><th colspan="3" scope="colgroup">Last {window} league games</th></tr>')
        for label, key, top in measures:
            h, a = values.get(f"home_{key}_last{window}"), values.get(f"away_{key}_last{window}")
            scale = top or max(3.0, h or 0, a or 0)
            rows.append(f'<tr>{cell(h, "home", scale)}<th scope="row">{label}</th>{cell(a, "away", scale)}</tr>')
        games = [values.get(f"{side}_games_last{window}") for side in ("home", "away")]
        rows.append(f'<tr><td class="num home"><span class="val">{"–" if games[0] is None else games[0]}</span></td>'
                    f'<th scope="row">Games counted</th>'
                    f'<td class="num away"><span class="val">{"–" if games[1] is None else games[1]}</span></td></tr>')
    cutoff = values.get("feature_cutoff_date") or forecast["feature_cutoff_date"].isoformat()
    model = (f'{escape(forecast["model_name"].replace("_", " "))} (model run {forecast["model_run_id"]}), trained on '
             f'{forecast["training_matches"]:,} matches from {season_label(min(forecast["training_seasons"]))} to '
             f'{season_label(max(forecast["training_seasons"]))}')
    return f"""
    <section class="sheet" aria-labelledby="form-title"><h2 id="form-title">What the model saw</h2>
      <table class="compare"><caption class="sr">Form before the match</caption><thead><tr>
        <th class="num home" scope="col">{escape(match["home"])}</th><th scope="col"><span class="sr">Measure</span></th>
        <th class="num away" scope="col">{escape(match["away"])}</th></tr></thead>
        <tbody>{"".join(rows)}</tbody></table>
      <dl class="facts pad">
        <dt>Games up to</dt><dd>{long_day(date.fromisoformat(cutoff), year=True)}</dd>
        <dt>Data download</dt><dd>{escape(forecast["data_snapshot"])}</dd>
        <dt>Model</dt><dd>{model}</dd>
        <dt>As data</dt><dd><a href="/matches/{match["match_id"]}/forecasts">/matches/{match["match_id"]}/forecasts</a></dd>
      </dl>
      {_other_forecasts(match)}
    </section>"""


def _other_forecasts(match: dict) -> str:
    others = match["other_forecasts"]
    if not others:
        return ""
    items = "".join(
        f'<li>Made for {long_day(f["forecast_for_date"])}, before the match was moved: '
        f'{forecast_words(f, escape(match["home"]), escape(match["away"]))} Kept for the record, not scored.</li>'
        for f in others)
    return f'<ul class="fine others">{items}</ul>'


def _side(match: dict, side: str, clubs: dict[str, Club]) -> str:
    coach = match[f"{side}_coach"]
    manager = (f'<span class="coach"><span class="label">Manager</span> {escape(coach)}</span>' if coach else "")
    return (f'<div class="side-block {side}">{club_badge(match[side], clubs, "lg")}'
            f'<a class="name" href="/team/{match[f"{side}_team_id"]}">{escape(match[side])}</a>{manager}</div>')


def colour_style(scope: str, colours: dict[str, dict[str, str]], names: tuple[str, ...]) -> str:
    """A small style block that sets a page's club colours for light and dark mode.

    The values are #RRGGBB strings from team_colours (the config file is checked by a test)."""
    def rules(mode: str) -> str:
        return ";".join(f"--{n.replace('_', '-')}:{colours[mode][n]}" for n in names)
    return (f'<style>.{scope}{{{rules("light")}}}'
            f'@media (prefers-color-scheme:dark){{.{scope}{{{rules("dark")}}}}}</style>')


def match_page(*, match: dict, stadium: tuple[str, str] | None, zone, info: dict, clubs: dict[str, Club]) -> str:
    e = escape
    home, away = e(match["home"]), e(match["away"])
    played = match["status"] == "completed"
    day = match["match_date"]
    if played:
        centre = (f'<span class="score big">{match["home_goals"]}<span class="dash">–</span>{match["away_goals"]}</span>'
                  f'<span class="state">Full time</span>')
    elif match["status"] in ENDED:
        centre = f'<span class="state ended">{ENDED[match["status"]]}</span>'
    else:
        centre = f'<span class="time big">{clock(match["kickoff_utc"], zone)}</span>'
    round_text = f', {e(match["source_round"].lower())}' if match["source_round"] else ""
    if stadium:
        ground, town = stadium
        where = (f'{e(ground)}<span class="addr">{e(town)} <a class="map" '
                 f'href="https://www.openstreetmap.org/search?query={quote_plus(f"{ground}, {town}")}">Map</a></span>')
    else:
        where = '<span class="muted">Not listed</span>'
    colours = match_colours(clubs.get(match["home"]), clubs.get(match["away"]))
    body = f"""
    {colour_style("mc", colours, ("home", "away", "home_ink", "away_ink"))}
    <div class="mc">
    <a class="back" href="/?date={day.isoformat()}"><span aria-hidden="true">‹</span> {long_day(day)}</a>
    <section class="sheet match-head" aria-labelledby="match-title">
      <p class="meta center">Premier League {season_label(match["season"])}{round_text}</p>
      <h1 id="match-title" class="sr">{home} v {away}</h1>
      <div class="teams">
        {_side(match, "home", clubs)}<span class="mid">{centre}</span>{_side(match, "away", clubs)}
      </div>
      <dl class="info">
        <div><dt>Date</dt><dd>{long_day(day, year=True)}</dd></div>
        <div><dt>Kickoff</dt><dd>{clock(match["kickoff_utc"], zone)} UK time</dd></div>
        <div><dt>Stadium</dt><dd>{where}</dd></div>
      </dl>
    </section>
    {_forecast_section(match, zone)}
    <section class="sheet" aria-labelledby="recent-title"><h2 id="recent-title">Last five league games</h2>
      <div class="two pad">
        <div><h3>{team_link(match["home_team_id"], match["home"])}</h3>{_recent_list(match["recent"]["home"], clubs)}</div>
        <div><h3>{team_link(match["away_team_id"], match["away"])}</h3>{_recent_list(match["recent"]["away"], clubs)}</div>
      </div>
    </section>
    <section class="sheet" aria-labelledby="h2h-title"><h2 id="h2h-title">Head to head</h2>
      {_head_to_head(match, clubs)}</section>
    {_features_section(match)}
    </div>"""
    return page(f"{match['home']} v {match['away']}", body, info, zone, active="fixtures")


# --- the team page --------------------------------------------------------------------

POSITIONS = (("Goalkeeper", "Goalkeepers"), ("Defence", "Defenders"), ("Midfield", "Midfielders"),
             ("Offence", "Forwards"))


def table_excerpt(table: list[dict], team_id: int, clubs: dict[str, Club], around: int = 2) -> str:
    """The team's row with two clubs above and below; a faded row hints at more on each cut side."""
    index = next((k for k, r in enumerate(table) if r["team_id"] == team_id), None)
    if index is None:
        return '<p class="note pad">Not in this season\'s table.</p>'
    size = 2 * around + 1
    start = max(0, min(index - around, len(table) - size))
    end = min(len(table), start + size)

    def row(k: int, *, ghost: bool = False) -> str:
        r = table[k]
        current = ' class="current"' if k == index else ""
        hidden = ' class="ghost" aria-hidden="true"' if ghost else current
        name = escape(r["team_name"]) if ghost or k == index else team_link(r["team_id"], r["team_name"])
        return (f'<tr{hidden}><td class="num pos">{k + 1}</td>'
                f'<td class="club"><span class="club-in">{club_badge(r["team_name"], clubs, "sm")}{name}</span></td>'
                f'<td class="num">{r["played"]}</td><td class="num">{signed(r["goal_difference"])}</td>'
                f'<td class="num pts">{r["points"]}</td>'
                f'<td class="last"><span class="form">{"".join(chip(x) for x in r["form"])}</span></td></tr>')

    rows = ([row(start - 1, ghost=True)] if start > 0 else []) + [row(k) for k in range(start, end)] \
        + ([row(end, ghost=True)] if end < len(table) else [])
    cut = (" cut-top" if start > 0 else "") + (" cut-bottom" if end < len(table) else "")
    return (f'<div class="excerpt{cut}"><table class="league"><caption class="sr">Part of the league table</caption>'
            f'<thead><tr><th class="num" scope="col"><span class="sr">Position</span></th><th scope="col">Club</th>'
            f'<th class="num" scope="col" title="Played">P</th><th class="num" scope="col" title="Goal difference">GD</th>'
            f'<th class="num" scope="col">Pts</th><th scope="col">Last five</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def _team_fixture(r: dict, zone, clubs: dict[str, Club], *, played: bool) -> str:
    venue = "H" if r["at_home"] else "A"
    if played:
        letter = result_letter(r["gf"], r["ga"])
        end = f'<span class="res">{chip(letter)}<span class="num sc">{r["gf"]}–{r["ga"]}</span></span>'
        words = f'{"won" if letter == "W" else "drew" if letter == "D" else "lost"} {r["gf"]}–{r["ga"]}'
    elif r["status"] in ENDED:
        end = f'<span class="res muted">{ENDED[r["status"]]}</span>'
        words = ENDED[r["status"]].lower()
    else:
        end = f'<span class="res time">{clock(r["kickoff_utc"], zone)}</span>'
        words = f'kickoff {clock(r["kickoff_utc"], zone)}'
    label = f'{short_day(r["match_date"])}, {"home" if r["at_home"] else "away"} against {escape(r["opponent"])}, {words}.'
    return (f'<li><a class="tf" href="/match/{r["match_id"]}" aria-label="{label}">'
            f'<span class="date">{short_day(r["match_date"])}</span><span class="venue">{venue}</span>'
            f'<span class="opp">{club_badge(r["opponent"], clubs, "sm")}<span class="nm">{escape(r["opponent"])}</span>'
            f'</span>{end}</a></li>')


def _fixture_list(rows: list[dict], zone, clubs: dict[str, Club], *, played: bool, shown: int = 5) -> str:
    """Matches oldest first, so the season reads top to bottom with today between results and fixtures.
    Results show their newest five with the earlier ones folded above; fixtures show their next five
    with the later ones folded below."""
    if not rows:
        return f'<p class="note pad">{"No results yet." if played else "No more fixtures this season."}</p>'
    items = [_team_fixture(r, zone, clubs, played=played) for r in rows]
    folded, visible = (items[:-shown], items[-shown:]) if played else (items[shown:], items[:shown])
    listed = f'<ol class="team-fixtures">{"".join(visible)}</ol>'
    if not folded:
        return listed
    more = (f'<details class="more{" earlier" if played else ""}"><summary><span class="when-closed">Show '
            f'{len(folded)} more</span><span class="when-open">Hide {len(folded)}</span></summary>'
            f'<ol class="team-fixtures">{"".join(folded)}</ol></details>')
    return more + listed if played else listed + more


def _team_form(team: dict) -> str:
    latest = team["form"]
    if latest is None:
        return '<p class="note pad">Shown once a forecast has been saved for one of this team\'s matches.</p>'
    values = latest["features"] or {}
    if isinstance(values, str):
        values = json.loads(values)
    side = "home" if latest["at_home"] else "away"
    measures = (("Points per game", "ppg"), ("Scored per game", "goals_for"), ("Conceded per game", "goals_against"),
                ("Games counted", "games"))

    def value(key: str, window: int) -> str:
        v = values.get(f"{side}_{key}_last{window}")
        return "–" if v is None else (str(v) if key == "games" else f"{v:.2f}")
    rows = "".join(f'<tr><th scope="row">{label}</th><td class="num">{value(key, 5)}</td>'
                   f'<td class="num">{value(key, 10)}</td></tr>' for label, key in measures)
    match = f'{escape(latest["home"])} v {escape(latest["away"])}'
    return (f'<table class="form-table"><thead><tr><th scope="col"><span class="sr">Measure</span></th>'
            f'<th class="num" scope="col">Last 5 games</th><th class="num" scope="col">Last 10 games</th></tr></thead>'
            f'<tbody>{rows}</tbody></table>'
            f'<p class="fine pad">From the forecast for <a href="/match/{latest["match_id"]}">{match}</a> on '
            f'{long_day(latest["forecast_for_date"])}: league games up to '
            f'{long_day(latest["feature_cutoff_date"], year=True)}.</p>')


def _squad(team: dict, today: date) -> str:
    squad = team["squad"]
    if isinstance(squad, str):
        squad = json.loads(squad)
    if not squad:
        return '<p class="note pad">The squad appears after the next morning\'s download.</p>'
    groups = []
    known = {code for code, _ in POSITIONS}
    for code, title in (*POSITIONS, (None, "Other")):
        players = sorted((p for p in squad if (p.get("position") == code if code else p.get("position") not in known)),
                         key=lambda p: p["name"])
        if not players:
            continue
        groups.append(f'<tr class="group"><th colspan="3" scope="colgroup">{title}</th></tr>' + "".join(
            f'<tr><th scope="row">{escape(p["name"])}</th>'
            f'<td class="num">{age if (age := age_on(p.get("date_of_birth"), today)) is not None else "–"}</td>'
            f'<td>{escape(p.get("nationality") or "–")}</td></tr>' for p in players))
    return (f'<table class="squad"><thead><tr><th scope="col">Player</th><th class="num" scope="col">Age</th>'
            f'<th scope="col">Nationality</th></tr></thead><tbody>{"".join(groups)}</tbody></table>')


def team_page(*, team: dict, table: list[dict], stadium: tuple[str, str] | None, season: str, now: datetime,
              zone, info: dict, clubs: dict[str, Club]) -> str:
    e = escape
    today = now.astimezone(zone).date()
    fixtures = team["fixtures"]
    played = sorted((r for r in fixtures if r["status"] == "completed"),
                    key=lambda r: (r["match_date"], r["kickoff_utc"] or now))
    upcoming = [r for r in fixtures if r["status"] != "completed"]
    following = next((r for r in upcoming if r["status"] == "scheduled"
                      and (r["kickoff_utc"] or now) > now), None)

    position = next((k + 1 for k, r in enumerate(table) if r["team_id"] == team["team_id"]), None)
    standing = (f'{ordinal(position)}, {table[position - 1]["points"]} points' if position else "Not in this season")
    manager = (f'{e(team["coach_name"])}' + (f'<span class="addr">{e(team["coach_nationality"])}</span>'
                                             if team["coach_nationality"] else "")
               if team["coach_name"] else '<span class="muted">Not listed yet</span>')
    if stadium:
        ground, town = stadium
        where = (f'{e(ground)}<span class="addr">{e(town)} <a class="map" '
                 f'href="https://www.openstreetmap.org/search?query={quote_plus(f"{ground}, {town}")}">Map</a></span>')
    else:
        where = '<span class="muted">Not listed</span>'

    if following:
        note = ("Forecast saved. Open the match to see it." if following["generated_at_utc"] is not None
                else "The forecast is saved on the morning of match day.")
        next_match = (f'<p class="note pad">{long_day(following["match_date"])}. {note}</p>'
                      f'<ol class="fixtures">{fixture_row(following, zone, clubs)}</ol>')
    else:
        next_match = '<p class="note pad">No more matches scheduled this season.</p>'

    source = ""
    if team["squad_snapshot"]:
        try:
            seen = datetime.strptime(team["squad_snapshot"], "%Y%m%dT%H%M%SZ").date()
            source = f" in the download of {short_day(seen, year=True)}"
        except ValueError:
            source = ""
    accent = {mode: {"team": c["home"]} for mode, c in match_colours(clubs.get(team["team_name"]), None).items()}
    body = f"""
    {colour_style("tc", accent, ("team",))}
    <div class="tc">
    <section class="sheet match-head team-head" aria-labelledby="team-title">
      <div class="team-mark">{club_badge(team["team_name"], clubs, "xl")}</div>
      <h1 id="team-title" class="team-title">{e(team["team_name"])}</h1>
      <p class="meta center">Premier League {season_label(season)}</p>
      <dl class="info">
        <div><dt>Manager</dt><dd>{manager}</dd></div>
        <div><dt>Home stadium</dt><dd>{where}</dd></div>
        <div><dt>League position</dt><dd>{standing}</dd></div>
      </dl>
    </section>
    <section class="sheet" aria-labelledby="next-title"><h2 id="next-title">Next match</h2>{next_match}</section>
    <section class="sheet" aria-labelledby="position-title"><h2 id="position-title">In the table</h2>
      {table_excerpt(table, team["team_id"], clubs)}
      <p class="fine pad"><a href="/table">Full table</a></p></section>
    <section class="sheet" aria-labelledby="fixtures-title"><h2 id="fixtures-title">Results and fixtures</h2>
      <h3 class="sub pad">Results</h3>{_fixture_list(played, zone, clubs, played=True)}
      <h3 class="sub pad">Coming up</h3>{_fixture_list(upcoming, zone, clubs, played=False)}
    </section>
    <section class="sheet" aria-labelledby="form-title"><h2 id="form-title">What the model saw</h2>
      {_team_form(team)}</section>
    <section class="sheet" aria-labelledby="squad-title"><h2 id="squad-title">Squad</h2>
      {_squad(team, today)}
      <p class="fine pad">Manager and squad as listed by football-data.org{source}.</p></section>
    </div>"""
    return page(team["team_name"], body, info, zone, active="")


def team_not_found_page(info: dict, zone) -> str:
    body = ('<section class="sheet"><header class="sheet-head"><h1>No team with this number</h1>'
            '<p class="note"><a href="/table">Back to the table</a></p></header></section>')
    return page("Team not found", body, info, zone, active="")


def not_found_page(info: dict, zone) -> str:
    body = ('<section class="sheet"><header class="sheet-head"><h1>No match with this number</h1>'
            '<p class="note"><a href="/">Back to the fixtures</a></p></header></section>')
    return page("Match not found", body, info, zone, active="fixtures")


# --- the page shell ---------------------------------------------------------------------

FAVICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
           "%3Crect x='1' y='9' width='15' height='14' rx='3' fill='%232A78D6'/%3E"
           "%3Crect x='18' y='9' width='5' height='14' rx='2' fill='%238F8D86'/%3E"
           "%3Crect x='25' y='9' width='6' height='14' rx='2' fill='%23EB6834'/%3E%3C/svg%3E")


def page(title: str, body: str, info: dict, zone, *, active: str) -> str:
    tabs = "".join(f'<a href="{href}"{" aria-current=\"page\"" if key == active else ""}>{label}</a>'
                   for key, href, label in TABS)
    return f"""<!DOCTYPE html>
<html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="description" content="Home, draw and away chances for every Premier League match, saved on match day before kickoff and never changed.">
<title>{escape(title)}</title>
<link rel="icon" href="{FAVICON}">
<link rel="preload" href="/static/fonts/geist-sans-latin-400-normal.woff2" as="font" type="font/woff2" crossorigin>
<style>{CSS}</style></head>
<body>
<a class="skip" href="#main">Skip to content</a>
<header class="top">
  <div class="wrap bar-row">
    <a class="brand" href="/">Premier League forecasts</a>
    {status_line(info, zone)}
  </div>
  <nav class="wrap tabs" aria-label="Sections">{tabs}</nav>
</header>
<main id="main" class="wrap">
{body}
</main>
<footer class="wrap foot">
  <p>Forecasts are saved on match day before kickoff and never changed. {escape(ATTRIBUTION)}.
  {escape(HISTORY_CREDIT)}.</p>
  <p><a href="{REPOSITORY}">Source code</a><a href="/docs">API</a></p>
</footer>
</body></html>"""


CSS = """
@font-face{font-family:"Geist";font-weight:400;font-display:swap;src:url(/static/fonts/geist-sans-latin-400-normal.woff2) format("woff2")}
@font-face{font-family:"Geist";font-weight:500;font-display:swap;src:url(/static/fonts/geist-sans-latin-500-normal.woff2) format("woff2")}
@font-face{font-family:"Geist";font-weight:600;font-display:swap;src:url(/static/fonts/geist-sans-latin-600-normal.woff2) format("woff2")}
:root{
  --page:#F1F3F5;--surface:#FFFFFF;--ink:#14202E;--ink-2:#4A5565;--ink-3:#5F6A79;--line:#E1E5EA;
  --home:#2A78D6;--draw:#8F8D86;--away:#EB6834;--focus:#1D5FB8;--hover:#F6F7F9;--warn:#9A5B00;
  --font:"Geist",system-ui,-apple-system,"Segoe UI",sans-serif;
}
@media (prefers-color-scheme:dark){:root{
  --page:#0E141B;--surface:#161E27;--ink:#E8EDF2;--ink-2:#A9B4C0;--ink-3:#8693A2;--line:#26313D;
  --home:#3987E5;--draw:#8A8983;--away:#D95926;--focus:#7FB0F0;--hover:#1B2530;--warn:#F0B35A;
}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--page);color:var(--ink);font:400 15px/1.55 var(--font);font-feature-settings:"tnum"}
a{color:inherit;text-underline-offset:3px;text-decoration-thickness:1px}
a:focus-visible{outline:2px solid var(--focus);outline-offset:2px;border-radius:4px}
h1,h2,h3{font-weight:600;line-height:1.25;margin:0;letter-spacing:-.01em}
p{margin:0}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.skip{position:absolute;left:-999px;top:8px;background:var(--surface);padding:8px 12px}
.skip:focus{left:8px;z-index:2}
.num{text-align:right}
.muted{color:var(--ink-3)}
.wrap{max-width:880px;margin:0 auto;padding-left:20px;padding-right:20px}

.top{background:var(--surface);border-bottom:1px solid var(--line);margin-bottom:20px}
.bar-row{display:flex;justify-content:space-between;align-items:baseline;gap:16px;padding-top:16px;padding-bottom:6px}
.brand{font-weight:600;font-size:18px;text-decoration:none;letter-spacing:-.01em}
.status{color:var(--ink-3);font-size:13px}
.status.warn{color:var(--ink)}
.status .icon{display:inline-grid;place-items:center;width:16px;height:16px;border-radius:50%;background:var(--warn);color:var(--surface);font-size:11px;font-weight:600;margin-right:6px;vertical-align:-2px}
.tabs{display:flex;gap:26px;overflow-x:auto;scrollbar-width:none}
.tabs::-webkit-scrollbar{display:none}
.tabs a{padding:10px 0 11px;text-decoration:none;color:var(--ink-2);font-weight:500;white-space:nowrap;border-bottom:2px solid transparent}
.tabs a:hover{color:var(--ink)}
.tabs a[aria-current]{color:var(--ink);border-bottom-color:var(--ink)}

.strip{display:flex;align-items:stretch;gap:4px;margin-bottom:12px}
.days{display:flex;gap:4px;flex:1;min-width:0}
.day,.step{display:flex;flex-direction:column;align-items:center;justify-content:center;text-decoration:none;border-radius:8px;color:var(--ink-2)}
.day{flex:1 1 0;min-width:0;padding:7px 2px 8px}
.day:hover,.step:hover{background:var(--surface);color:var(--ink)}
.day .dow{font-size:13px;font-weight:500;line-height:1.3}
.day .dom{font-size:12px;line-height:1.3}
.day[aria-current]{background:var(--ink);color:var(--page)}
.step{width:32px;font-size:20px}
.step.off{opacity:.3}

.sheet{background:var(--surface);border:1px solid var(--line);border-radius:10px;margin-bottom:16px;overflow:hidden}
.sheet>h2{font-size:16px;padding:16px 20px 10px}
.sheet-head{padding:20px 20px 16px}
.sheet-head h1{font-size:24px}
.pad{padding-left:20px;padding-right:20px}
.sheet>.pad:last-child,.sheet>p.pad:last-child,.sheet>div.pad:last-child{padding-bottom:18px}
.meta{color:var(--ink-2);margin-top:4px;max-width:70ch}
.meta.center{text-align:center;margin:0 auto}
.note{color:var(--ink-2);margin-top:10px;max-width:72ch}
.sheet>.note.pad{margin-top:0}
.fine{color:var(--ink-3);font-size:13px;margin-top:10px}

.fixtures{list-style:none;margin:0;padding:0}
.fx{display:grid;grid-template-columns:minmax(0,1fr) 96px minmax(0,1fr);align-items:center;column-gap:12px;
  padding:14px 20px;border-top:1px solid var(--line);text-decoration:none;color:var(--ink)}
.fx:hover{background:var(--hover)}
.fx:focus-visible{outline-offset:-2px;border-radius:0}
.side{font-weight:500}
.side.home{text-align:right}
.mid{display:flex;flex-direction:column;align-items:center;gap:1px}
.time{font-weight:500;color:var(--ink-2)}
.score{font-weight:600;font-size:17px}
.score .dash{padding:0 6px;color:var(--ink-3);font-weight:400}
.state{font-size:11.5px;color:var(--ink-3)}
.state.ended{font-size:13px}

.league{width:100%;border-collapse:collapse;font-size:14px}
.league th{font-size:12px;font-weight:500;color:var(--ink-3);padding:0 6px 8px;text-align:left}
.league th.num{text-align:right}
.league td{padding:8px 6px;border-top:1px solid var(--line)}
.league td:first-child,.league th:first-child{padding-left:20px}
.league td:last-child,.league th:last-child{padding-right:20px}
.league .pos{color:var(--ink-3);width:44px}
.league .club{font-weight:500}
.league .pts{font-weight:600}
.last{width:120px}
.form{display:inline-flex;gap:3px}
.chip{display:inline-grid;place-items:center;width:18px;height:18px;border-radius:4px;font-size:11px;font-weight:600;line-height:1}
.chip{color:#fff;font-weight:700}
.chip.w{background:#439664}.chip.d{background:#8E9498}.chip.l{background:#CC453E}

table{border-collapse:collapse}
.scores{width:calc(100% - 40px);max-width:600px;margin:12px 20px 0;font-size:14px}
.scores th,.scores td{padding:8px 0;border-top:1px solid var(--line);font-weight:400;text-align:left}
.scores td.num,.scores th.num{text-align:right}
.scores thead th{border-top:0;font-size:12px;color:var(--ink-3);font-weight:500;padding-top:0}
.scores tbody th{color:var(--ink-2)}
.scores tbody tr:first-child th,.scores tbody tr:first-child td{color:var(--ink);font-weight:500}
.pad .scores{width:100%;max-width:600px;margin:12px 0 0}
.scroll{overflow-x:auto}
.record{width:100%;font-size:14px}
.record th{font-size:12px;font-weight:500;color:var(--ink-3);text-align:left;padding:0 8px 8px}
.record th.num{text-align:right}
.record td{padding:9px 8px;border-top:1px solid var(--line);white-space:nowrap}
.record td:first-child,.record th:first-child{padding-left:20px}
.record td:last-child,.record th:last-child{padding-right:20px}
.record td.when{color:var(--ink-3)}
.record td.num{color:var(--ink-3)}
.record td.hit{color:var(--ink);font-weight:600}
.record a{text-decoration:none}
.record a:hover{text-decoration:underline}

.prose h2{font-size:17px;margin:22px 0 8px}
.prose .pad>h2:first-child{margin-top:4px}
.prose p{color:var(--ink-2);max-width:68ch;margin-bottom:10px}
.steps,.rules{margin:0 0 10px;padding-left:22px;color:var(--ink-2);max-width:68ch}
.steps li,.rules li{margin-bottom:8px;padding-left:4px}
.steps li::marker{color:var(--ink-3);font-weight:500}
.links-row a{margin-right:18px}
.prose .pad{padding-bottom:20px}

.back{display:inline-flex;gap:6px;margin:0 0 12px;text-decoration:none;color:var(--ink-2);font-weight:500}
.back:hover{color:var(--ink)}
.match-head{padding:18px 20px 0}
.teams{display:grid;grid-template-columns:minmax(0,1fr) auto minmax(0,1fr);align-items:center;gap:20px;margin:16px 0 20px}
.side-block{display:flex;flex-direction:column;align-items:flex-start;gap:4px;min-width:0}
.side-block .badge{margin-bottom:4px}
.side-block.home{align-items:flex-end;text-align:right}
.teams .name{font-size:21px;font-weight:600;letter-spacing:-.01em;text-decoration:none}
.teams .name:hover,.team-link:hover{text-decoration:underline}
.coach{font-size:13px;color:var(--ink-2)}
.coach .label{color:var(--ink-3)}
.team-link{text-decoration:none}
.teams .mid{min-width:100px}
.big.time{font-size:28px;font-weight:600;color:var(--ink)}
.big.score{font-size:34px}
.info{display:grid;grid-template-columns:1.4fr 1fr 1.4fr;margin:0 -20px;border-top:1px solid var(--line)}
.info>div{padding:12px 20px 14px;border-left:1px solid var(--line)}
.info>div:first-child{border-left:0}
.info dt{font-size:12px;color:var(--ink-3);font-weight:500}
.info dd{margin:2px 0 0;font-weight:500}
.info .addr{display:block;font-weight:400;font-size:13px;color:var(--ink-3)}
.info .map{font-weight:400;font-size:13px;color:var(--ink-2)}

.picks{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-bottom:12px}
.pick{display:flex;flex-direction:column;gap:2px}
.pick-name{display:flex;align-items:center;gap:7px;font-size:13px;color:var(--ink-2);min-width:0}
.pick-value{font-size:30px;font-weight:600;letter-spacing:-.02em;line-height:1.15}
.pick.dim .pick-value{color:var(--ink-3)}
.tag{align-self:flex-start;font-size:12px;font-weight:600;border:1px solid var(--ink);border-radius:4px;padding:1px 6px}
.sw{display:inline-block;width:10px;height:10px;border-radius:3px;flex:none}
.sw.home{background:var(--home)}.sw.draw{background:var(--draw)}.sw.away{background:var(--away)}

.bar{display:flex;gap:2px;height:28px}
.seg{flex-basis:0;min-width:0;border-radius:4px;display:flex;align-items:center;padding:0 8px;color:#fff;
  font-size:13px;font-weight:600;white-space:nowrap}
.seg.home{background:var(--home);color:var(--home-ink,#fff)}.seg.draw{background:var(--draw)}
.seg.away{background:var(--away);color:var(--away-ink,#fff)}
.seg.home.faded,.seg.draw.faded,.seg.away.faded{color:var(--ink-2)}
.seg.home.faded{background:color-mix(in oklab,var(--home) 20%,var(--surface))}
.seg.draw.faded{background:color-mix(in oklab,var(--draw) 20%,var(--surface))}
.seg.away.faded{background:color-mix(in oklab,var(--away) 20%,var(--surface))}

.two{display:grid;grid-template-columns:1fr 1fr;gap:28px;padding-bottom:18px}
.two h3{font-size:14px;margin-bottom:6px}
.recent,.h2h{list-style:none;margin:0;padding:0;font-size:14px}
.recent li{display:grid;grid-template-columns:22px minmax(0,1fr) 18px 40px 80px;gap:8px;align-items:center;padding:7px 0;border-top:1px solid var(--line)}
.recent .opp{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.recent .venue{color:var(--ink-3);font-size:12px;text-align:center}
.recent .date,.h2h .date{color:var(--ink-3);font-size:13px}
.recent .date{text-align:right;white-space:nowrap}
.sc{font-weight:500}
.h2h{margin-top:10px}
.h2h li{display:grid;grid-template-columns:110px minmax(0,1fr) 52px minmax(0,1fr);gap:10px;align-items:center;padding:9px 20px;border-top:1px solid var(--line)}
.h2h .h2h-home{text-align:right}
.h2h .sc{text-align:center}
.h2h .won{font-weight:600}
.h2h-home,.h2h-away{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

.compare{width:calc(100% - 40px);margin:0 20px;font-size:14px}
.compare th,.compare td{padding:8px 12px;border-top:1px solid var(--line)}
.compare thead th{border-top:0;font-weight:600;color:var(--ink)}
.compare tbody th{font-weight:400;color:var(--ink-2);text-align:center;width:36%}
.compare tr.group th{font-weight:600;color:var(--ink);padding-top:16px;font-size:13px}
.compare td{width:32%}
.compare td.home,.compare thead th.home{text-align:right}
.compare td.away,.compare thead th.away{text-align:left}
.compare .val{display:inline-block;min-width:36px}
.compare td.home{direction:rtl}.compare td.home .val{direction:ltr}
.track{display:inline-block;width:calc(100% - 52px);max-width:120px;height:6px;border-radius:3px;background:var(--hover);vertical-align:middle;margin:0 10px}
.fill{display:block;height:100%;border-radius:3px}
.fill.home{background:var(--home)}.fill.away{background:var(--away)}
.facts{display:grid;grid-template-columns:max-content 1fr;gap:6px 20px;margin:14px 0 18px;font-size:14px}
.facts dt{color:var(--ink-3)}.facts dd{margin:0;overflow-wrap:anywhere}
.others{padding:0 20px 16px 38px}

.team-title{text-align:center;font-size:26px;margin-top:8px}
.team-head{border-top:4px solid var(--team,var(--line))}
.team-mark{display:flex;justify-content:center;margin-top:6px}
.badge{--size:22px;display:inline-grid;place-items:center;flex:none;width:var(--size);height:var(--size);border-radius:50%;
  background:var(--b);color:var(--t);font-size:calc(var(--size) * .36);font-weight:600;letter-spacing:.02em;line-height:1;
  box-shadow:0 0 0 1px rgba(127,127,127,.45);vertical-align:middle;font-feature-settings:normal}
.badge.sm{--size:16px;margin-right:8px;font-size:0;box-shadow:inset 0 0 0 3px var(--t),0 0 0 1px rgba(127,127,127,.45)}
.badge.lg{--size:40px}
.badge.xl{--size:56px}
.side{display:flex;align-items:center;gap:10px;min-width:0}
.side.home{justify-content:flex-end}
.side .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.club-in,.tf .opp,.recent .opp{display:flex;align-items:center;min-width:0}
.club-in a,.nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.h2h-home,.h2h-away{display:flex;align-items:center;gap:0}
.h2h-home{justify-content:flex-end}
.h2h-home .badge.sm{margin:0 0 0 8px}
.sub{font-size:14px;margin:6px 0 8px}
.team-fixtures{list-style:none;margin:0;padding:0;font-size:14px}
.tf{display:grid;grid-template-columns:100px 22px minmax(0,1fr) auto;gap:10px;align-items:center;padding:9px 20px;
  border-top:1px solid var(--line);text-decoration:none;color:var(--ink)}
.tf:hover{background:var(--hover)}
.tf .date{color:var(--ink-3);font-size:13px;white-space:nowrap}
.tf .venue{color:var(--ink-3);font-size:12px;text-align:center}
.tf .opp{font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tf .res{display:inline-flex;align-items:center;gap:8px;justify-content:flex-end;min-width:72px}
.tf .res.time{color:var(--ink-2);font-weight:500}
.more{border-top:1px solid var(--line)}
.more summary{padding:10px 20px;color:var(--ink-2);font-weight:500;cursor:pointer;font-size:14px}
.more summary:hover{color:var(--ink)}
.more[open] summary{border-bottom:1px solid var(--line)}
.more .when-open,.more[open] .when-closed{display:none}
.more[open] .when-open{display:inline}
.more .team-fixtures li:first-child .tf{border-top:0}
.sheet>.team-fixtures:last-child,.sheet>.more:last-child{margin-bottom:6px}
.excerpt .league thead th{height:30px;padding-top:0;padding-bottom:0;vertical-align:middle}
.excerpt .league td{height:38px;padding-top:0;padding-bottom:0}
.excerpt tr.current td{background:var(--hover);font-weight:600}
.excerpt tr.current td:first-child{box-shadow:inset 3px 0 var(--team,var(--ink))}
.excerpt.cut-top{-webkit-mask-image:linear-gradient(#000 0 30px,rgba(0,0,0,.08) 30px,#000 72px);
  mask-image:linear-gradient(#000 0 30px,rgba(0,0,0,.08) 30px,#000 72px)}
.excerpt.cut-bottom{-webkit-mask-image:linear-gradient(to top,rgba(0,0,0,.08) 0,#000 42px);
  mask-image:linear-gradient(to top,rgba(0,0,0,.08) 0,#000 42px)}
.excerpt.cut-top.cut-bottom{-webkit-mask-image:linear-gradient(#000 0 30px,rgba(0,0,0,.08) 30px,#000 72px,#000 calc(100% - 42px),rgba(0,0,0,.08) 100%);
  mask-image:linear-gradient(#000 0 30px,rgba(0,0,0,.08) 30px,#000 72px,#000 calc(100% - 42px),rgba(0,0,0,.08) 100%)}
.form-table,.squad{width:calc(100% - 40px);margin:0 20px;font-size:14px;border-collapse:collapse}
.form-table th,.form-table td,.squad th,.squad td{padding:8px 0;border-top:1px solid var(--line);text-align:left;font-weight:400}
.form-table .num,.squad .num{text-align:right}
.form-table thead th,.squad thead th{border-top:0;font-size:12px;color:var(--ink-3);font-weight:500;padding-top:0}
.form-table tbody th{color:var(--ink-2)}
.form-table td{width:24%}
.squad tbody th{font-weight:500;color:var(--ink)}
.squad .num{width:64px;padding-right:24px}
.squad td:last-child{color:var(--ink-2);width:40%}
.squad tr.group th{font-size:13px;font-weight:600;color:var(--ink);padding-top:16px}
.sheet>.squad{margin-bottom:4px}
.foot{color:var(--ink-3);font-size:13px;padding-top:12px;padding-bottom:40px}
.foot p{max-width:80ch;margin-bottom:6px}
.foot a{margin-right:16px}

@media (max-width:640px){
  .wrap{padding-left:12px;padding-right:12px}
  .bar-row{flex-direction:column;gap:2px;padding-top:12px}
  .tabs{gap:20px}
  .sheet-head{padding:16px}
  .sheet-head h1{font-size:21px}
  .pad{padding-left:16px;padding-right:16px}
  .sheet>h2{padding:14px 16px 8px}
  .fx{grid-template-columns:minmax(0,1fr) 72px minmax(0,1fr);padding:12px 12px;column-gap:8px}
  .side{line-height:1.3;gap:7px}
  .side .nm{white-space:normal}
  .step{width:22px}
  .day .dow{font-size:12px}.day .dom{font-size:11px}
  .league .wide{display:none}
  .league td:first-child,.league th:first-child{padding-left:12px}
  .league td:last-child,.league th:last-child{padding-right:12px}
  .league .pos{width:30px}
  .last{width:auto}
  .record td:first-child,.record th:first-child{padding-left:16px}
  .scores{width:calc(100% - 32px);margin:12px 16px 0}
  .match-head{padding:14px 16px 0}
  .teams{gap:10px}
  .teams .name{font-size:17px}
  .teams .mid{min-width:70px}
  .big.time{font-size:22px}.big.score{font-size:28px}
  .info{grid-template-columns:1fr;margin:0 -16px}
  .info>div{display:flex;justify-content:space-between;align-items:baseline;gap:16px;padding:10px 16px;
    border-left:0;border-top:1px solid var(--line)}
  .info>div:first-child{border-top:0}
  .info dd{margin:0;text-align:right}
  .picks{gap:8px}
  .pick-name{font-size:12px;align-items:flex-start}
  .pick-value{font-size:24px}
  .two{grid-template-columns:1fr;gap:18px}
  .h2h li{grid-template-columns:minmax(0,1fr) 44px minmax(0,1fr);padding:9px 16px;row-gap:2px}
  .h2h .date{grid-column:1/-1;text-align:center}
  .compare{width:calc(100% - 24px);margin:0 12px;font-size:13px}
  .compare th,.compare td{padding:8px 6px}
  .compare tbody th{white-space:nowrap}
  .track{display:none}
  .others{padding:0 16px 16px 32px}
  .team-title{font-size:22px}
  .tf{grid-template-columns:76px 18px minmax(0,1fr) auto;padding:9px 16px;gap:8px}
  .more summary{padding:10px 16px}
  .form-table,.squad{width:calc(100% - 32px);margin:0 16px}
  .side-block.home{align-items:flex-end}
  .coach{font-size:12px}
  .coach .label{display:block}
}
@media (forced-colors:active){.seg,.sw,.fill,.chip{forced-color-adjust:none}}
"""
