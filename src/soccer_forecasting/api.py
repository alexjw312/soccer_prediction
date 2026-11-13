"""A read-only web API and dashboard over saved forecasts.

Everything here reads what predict and daily saved. Nothing recomputes a
forecast, so what you see is exactly what was recorded before kickoff. The
database session is set to read-only, so a bug here cannot change any data.

Run it with: python -m soccer_forecasting serve, then open http://127.0.0.1:8000
(the dashboard) or http://127.0.0.1:8000/docs (the API's own documentation).
"""
import math
import os
from contextlib import contextmanager
from datetime import date, datetime, timezone
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from psycopg.rows import dict_row

from . import PROJECT_ROOT
from .env import load_env
from .live_loading import live_config
from .loading import read_json

ATTRIBUTION = "Football data provided by the Football-Data.org API"
HISTORY_CREDIT = "Historical results: OpenFootball football.json (CC0)"

FORECAST_SQL = """
    SELECT r.prediction_id, r.match_id, h.team_name AS home, a.team_name AS away,
           r.kickoff_utc, r.forecast_for_date, r.p_home, r.p_draw, r.p_away,
           r.generated_at_utc, r.information_cutoff_utc, r.feature_cutoff_date,
           r.model_run_id, r.data_snapshot, r.match_status, r.match_date, r.on_forecast_date,
           r.result, m.home_goals, m.away_goals, r.log_loss
    FROM prediction_results r
    JOIN matches m ON m.match_id = r.match_id
    JOIN teams h ON h.team_id = m.home_team_id
    JOIN teams a ON a.team_id = m.away_team_id
"""


def read_only_connection(url: str):
    @contextmanager
    def connect():
        with psycopg.connect(url, autocommit=True, row_factory=dict_row) as conn:
            conn.execute("SET default_transaction_read_only = on")
            yield conn
    return connect


def _forecast(row: dict) -> dict:
    played = row["match_status"] == "completed"
    return {
        "match_id": row["match_id"], "home": row["home"], "away": row["away"],
        "kickoff_utc": row["kickoff_utc"], "forecast_for_date": row["forecast_for_date"],
        "probabilities": {"home": row["p_home"], "draw": row["p_draw"], "away": row["p_away"]},
        "generated_at_utc": row["generated_at_utc"],
        "information_cutoff_utc": row["information_cutoff_utc"],
        "feature_cutoff_date": row["feature_cutoff_date"],
        "model_run_id": row["model_run_id"], "data_snapshot": row["data_snapshot"],
        "match_status": row["match_status"], "on_forecast_date": row["on_forecast_date"],
        "result": ({"outcome": row["result"], "home_goals": row["home_goals"], "away_goals": row["away_goals"],
                    "log_loss": row["log_loss"]} if played else None),
    }


def create_app(root: Path = PROJECT_ROOT, connect=None, now=None) -> FastAPI:
    config = live_config(root)
    zone = ZoneInfo(config["local_timezone"])
    cohort = read_json(root / "config" / "milestone0.json")["initial_modeling_seasons"]
    now = now or (lambda: datetime.now(timezone.utc))
    if connect is None:
        load_env(root, ("DATABASE_URL",))
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError("set DATABASE_URL in the terminal or in .env")
        connect = read_only_connection(url)

    def database():
        try:
            with connect() as conn:
                yield conn
        except psycopg.OperationalError as error:
            raise HTTPException(status_code=503, detail=f"database unavailable: {error}") from None

    app = FastAPI(title="Premier League match forecasts",
                  description=f"Saved home/draw/away forecasts, made on match day before kickoff. {ATTRIBUTION}.")

    def health_info(conn) -> dict:
        latest = conn.execute("""SELECT source_commit, retrieved_at_utc FROM source_files WHERE provider = %s
                                 ORDER BY retrieved_at_utc DESC LIMIT 1""", (config["provider"],)).fetchone()
        model = conn.execute("""SELECT model_run_id, created_at FROM model_runs WHERE purpose = 'deployment'
                                ORDER BY created_at DESC, model_run_id DESC LIMIT 1""").fetchone()
        job = conn.execute("""SELECT job, status, finished_at FROM job_runs
                              ORDER BY finished_at DESC LIMIT 1""").fetchone()
        saved = conn.execute("SELECT count(*) AS n FROM predictions").fetchone()["n"]
        age = (now() - latest["retrieved_at_utc"]).total_seconds() / 3600 if latest else None
        status = ("no live data" if latest is None
                  else "stale" if age > config["max_snapshot_age_hours"] else "ok")
        return {"status": status, "database": "ok",
                "latest_snapshot": latest["source_commit"] if latest else None,
                "latest_snapshot_utc": latest["retrieved_at_utc"] if latest else None,
                "latest_snapshot_age_hours": round(age, 1) if age is not None else None,
                "max_snapshot_age_hours": config["max_snapshot_age_hours"],
                "deployment_model_run": model["model_run_id"] if model else None,
                "last_job": job, "saved_forecasts": saved, "attribution": ATTRIBUTION}

    @app.get("/health")
    def health(conn=Depends(database)):
        """Is the database reachable, and how fresh is the live data?"""
        return health_info(conn)

    @app.get("/forecasts")
    def forecasts_for_day(day: date | None = Query(None, alias="date", description="Match day, YYYY-MM-DD"),
                          conn=Depends(database)):
        """Saved forecasts for one match day (default: today in London)."""
        day = day or now().astimezone(zone).date()
        rows = conn.execute(FORECAST_SQL + " WHERE r.forecast_for_date = %s ORDER BY r.kickoff_utc, h.team_name",
                            (day,)).fetchall()
        body = {"date": day, "forecasts": [_forecast(r) for r in rows], "attribution": ATTRIBUTION}
        if not rows:
            body["message"] = "No saved forecasts for this day. Forecasts are made on match day, before kickoff."
        return body

    @app.get("/forecasts/upcoming")
    def upcoming_forecasts(conn=Depends(database)):
        """Saved forecasts for matches that have not kicked off yet."""
        rows = conn.execute(FORECAST_SQL + """ WHERE r.kickoff_utc > %s AND r.match_status = 'scheduled'
                                               AND r.on_forecast_date ORDER BY r.kickoff_utc, h.team_name""",
                            (now(),)).fetchall()
        return {"forecasts": [_forecast(r) for r in rows], "attribution": ATTRIBUTION}

    @app.get("/matches/{match_id}/forecasts")
    def match_forecasts(match_id: int, conn=Depends(database)):
        """Every forecast ever saved for one match, oldest first."""
        if conn.execute("SELECT 1 FROM matches WHERE match_id = %s", (match_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail=f"no match with id {match_id}")
        rows = conn.execute(FORECAST_SQL + " WHERE r.match_id = %s ORDER BY r.generated_at_utc",
                            (match_id,)).fetchall()
        return {"match_id": match_id, "forecasts": [_forecast(r) for r in rows], "attribution": ATTRIBUTION}

    def upcoming_fixtures_rows(conn, limit: int) -> list[dict]:
        return conn.execute(
            """SELECT m.match_id, m.kickoff_utc, m.match_date, h.team_name AS home, a.team_name AS away,
                      p.p_home, p.p_draw, p.p_away, p.generated_at_utc
               FROM matches m
               JOIN teams h ON h.team_id = m.home_team_id
               JOIN teams a ON a.team_id = m.away_team_id
               LEFT JOIN LATERAL (SELECT * FROM predictions p
                                  WHERE p.match_id = m.match_id AND p.forecast_for_date = m.match_date
                                  ORDER BY p.generated_at_utc DESC LIMIT 1) p ON true
               WHERE m.season = %s AND m.status = 'scheduled' AND m.kickoff_utc > %s
               ORDER BY m.kickoff_utc, h.team_name LIMIT %s""", (config["season"], now(), limit)).fetchall()

    @app.get("/fixtures/upcoming")
    def upcoming_fixtures(limit: int = Query(20, ge=1, le=380), conn=Depends(database)):
        """The next fixtures, with their forecast if one has been saved yet."""
        rows = upcoming_fixtures_rows(conn, limit)
        return {"fixtures": [{
            "match_id": r["match_id"], "home": r["home"], "away": r["away"], "kickoff_utc": r["kickoff_utc"],
            "forecast": ({"home": r["p_home"], "draw": r["p_draw"], "away": r["p_away"],
                          "generated_at_utc": r["generated_at_utc"]} if r["p_home"] is not None else None),
        } for r in rows], "note": "Forecasts are published on match day, before kickoff.",
            "attribution": ATTRIBUTION}

    def performance_info(conn) -> dict:
        settled = conn.execute(
            """SELECT result, log_loss, p_home, p_draw, p_away FROM prediction_results
               WHERE match_status = 'completed' AND on_forecast_date""").fetchall()
        if not settled:
            return {"matches": 0, "message": "No forecasted match has been played yet."}
        shares = conn.execute(
            """SELECT result, count(*)::float / sum(count(*)) OVER () AS share FROM matches
               WHERE season = ANY(%s) AND status = 'completed' GROUP BY result""", (cohort,)).fetchall()
        share = {r["result"]: r["share"] for r in shares}
        picked = {"H": "p_home", "D": "p_draw", "A": "p_away"}
        hits = sum(max(picked, key=lambda k: r[picked[k]]) == r["result"] for r in settled)
        return {
            "matches": len(settled),
            "log_loss": sum(r["log_loss"] for r in settled) / len(settled),
            "baseline_log_loss": sum(-math.log(share[r["result"]]) for r in settled) / len(settled),
            "uniform_log_loss": math.log(3),
            "accuracy": hits / len(settled),
            "note": "Lower log loss is better. The baseline gives every match the historical home/draw/away "
                    "shares. With few matches played, these numbers move a lot.",
        }

    @app.get("/performance")
    def performance(conn=Depends(database)):
        """How saved forecasts have done on matches played on their forecast day."""
        return performance_info(conn)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(conn=Depends(database)):
        info, perf = health_info(conn), performance_info(conn)
        fixtures = upcoming_fixtures_rows(conn, 20)
        recent = conn.execute(FORECAST_SQL + """ WHERE r.match_status = 'completed' AND r.on_forecast_date
                                                 ORDER BY r.kickoff_utc DESC LIMIT 20""").fetchall()
        return dashboard_html(info, perf, fixtures, recent, zone)

    return app


def _pct(value) -> str:
    return f"{value:.0%}"


def _when(moment, zone) -> str:
    return moment.astimezone(zone).strftime("%a %d %b, %H:%M")


def dashboard_html(info: dict, perf: dict, fixtures: list, recent: list, zone) -> str:
    e = escape
    fresh = (f"Latest download {e(_when(info['latest_snapshot_utc'], zone))} London time "
             f"({info['latest_snapshot_age_hours']} hours ago)" if info["latest_snapshot_utc"] else "No live data yet")
    status_class = "ok" if info["status"] == "ok" else "warn"

    fixture_rows = "".join(
        f"<tr><td>{e(_when(f['kickoff_utc'], zone))}</td><td>{e(f['home'])} v {e(f['away'])}</td>"
        + (f"<td class=num>{_pct(f['p_home'])}</td><td class=num>{_pct(f['p_draw'])}</td>"
           f"<td class=num>{_pct(f['p_away'])}</td>" if f["p_home"] is not None
           else "<td colspan=3 class=muted>published on match day</td>")
        + "</tr>" for f in fixtures) or "<tr><td colspan=5 class=muted>No upcoming fixtures loaded.</td></tr>"

    result_names = {"H": "home win", "D": "draw", "A": "away win"}
    recent_rows = "".join(
        f"<tr><td>{e(_when(r['kickoff_utc'], zone))}</td><td>{e(r['home'])} {r['home_goals']}–{r['away_goals']} "
        f"{e(r['away'])}</td><td class=num>{_pct(r['p_home'])}</td><td class=num>{_pct(r['p_draw'])}</td>"
        f"<td class=num>{_pct(r['p_away'])}</td><td>{result_names[r['result']]}</td>"
        f"<td class=num>{r['log_loss']:.3f}</td></tr>" for r in recent
    ) or "<tr><td colspan=7 class=muted>No forecasted match has been played yet.</td></tr>"

    if perf["matches"]:
        performance = (f"<p>{perf['matches']} forecasted matches played. Log loss {perf['log_loss']:.3f}, "
                       f"against {perf['baseline_log_loss']:.3f} for historical shares and "
                       f"{perf['uniform_log_loss']:.3f} for one third each (lower is better). "
                       f"Most likely outcome right: {_pct(perf['accuracy'])}.</p>"
                       f"<p class=muted>{e(perf['note'])}</p>")
    else:
        performance = f"<p class=muted>{e(perf['message'])}</p>"

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Premier League match forecasts</title>
<style>
 body {{ font: 15px/1.5 -apple-system, system-ui, sans-serif; max-width: 920px; margin: 2rem auto; padding: 0 1rem;
        color: #1d1d1f; background: #fafafa; }}
 h1 {{ font-size: 1.6rem; margin-bottom: .2rem; }} h2 {{ font-size: 1.15rem; margin-top: 2rem; }}
 table {{ width: 100%; border-collapse: collapse; background: #fff; }}
 th, td {{ text-align: left; padding: .45rem .6rem; border-bottom: 1px solid #e5e5e5; }}
 th {{ font-weight: 600; font-size: .85rem; color: #555; }} .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
 .muted {{ color: #777; }} .status {{ display: inline-block; padding: .1rem .5rem; border-radius: 1rem; font-size: .8rem; }}
 .ok {{ background: #e3f4e6; color: #1f6b2c; }} .warn {{ background: #fdf0d8; color: #8a5a00; }}
 footer {{ margin-top: 2.5rem; font-size: .8rem; color: #777; }}
</style></head><body>
<h1>Premier League match forecasts</h1>
<p class=muted>Home win, draw and away win probabilities, made on match day before kickoff and never changed afterwards.</p>
<p><span class="status {status_class}">data {e(info['status'])}</span> {fresh}. Model run {info['deployment_model_run'] or '–'}.</p>
<h2>Upcoming fixtures</h2>
<table><tr><th>Kickoff (London)</th><th>Match</th><th class=num>Home</th><th class=num>Draw</th><th class=num>Away</th></tr>
{fixture_rows}</table>
<h2>Recent forecasts and results</h2>
<table><tr><th>Kickoff (London)</th><th>Result</th><th class=num>Home</th><th class=num>Draw</th><th class=num>Away</th>
<th>Outcome</th><th class=num>Log loss</th></tr>
{recent_rows}</table>
<h2>Performance so far</h2>
{performance}
<footer>{e(ATTRIBUTION)}. {e(HISTORY_CREDIT)}. API documentation: <a href="/docs">/docs</a>.</footer>
</body></html>"""
