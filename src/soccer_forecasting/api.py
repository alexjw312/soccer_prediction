"""A read-only web API and dashboard over saved forecasts.

Everything here reads what predict and daily saved. Nothing recomputes a
forecast, so what you see is exactly what was recorded before kickoff. The
database session is set to read-only, so a bug here cannot change any data.

Run it with: python -m soccer_forecasting serve, then open http://127.0.0.1:8000
(the dashboard) or http://127.0.0.1:8000/docs (the API's own documentation). The
dashboard's pages (/, /table, /model-performance, /how-it-works, /match/{id} and
/team/{id}) are built in dashboard.py; the JSON routes are here.

In public (Render, see render.yaml) it runs as uvicorn soccer_forecasting.api:create_app
--factory, with a login that may only read DASHBOARD_READS (see dashboard_login.py).
/healthz answers without touching the database, so the host's health checks never
keep the database awake. Error messages to visitors never include database details.
"""
import logging
import math
import os
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from . import PROJECT_ROOT, dashboard, team_colours
from .dashboard import ATTRIBUTION
from .env import load_env
from .features import cutoff_offset_days
from .live_loading import live_config
from .loading import read_json

LOG = logging.getLogger(__name__)

# Every table and view the queries below read. The public dashboard's login may read
# these and nothing else; a test runs every page under that login to keep this complete.
DASHBOARD_READS = ("job_runs", "matches", "model_runs", "prediction_results", "predictions",
                   "source_files", "teams")

STATIC = Path(__file__).parent / "static"

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
        try:
            conninfo_to_dict(url)
        except psycopg.ProgrammingError:  # the text could hold the password, so it is not repeated
            raise RuntimeError("DATABASE_URL is not a valid database address (the value is not shown)") from None
        connect = read_only_connection(url)

    def database():
        try:
            with connect() as conn:
                yield conn
        except psycopg.OperationalError as error:
            LOG.warning("database unavailable: %s", error)  # in the host's logs only, never to visitors
            raise HTTPException(status_code=503, detail="database unavailable, try again shortly") from None

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

    @app.get("/healthz")
    def alive():
        """The web process is up. Does not touch the database (for the host's health checks)."""
        return {"status": "ok"}

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
        share = dashboard.historical_shares(conn, cohort)
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

    final_test = dashboard.final_test_summary(root)
    cutoff_days = cutoff_offset_days(root)
    grounds = dashboard.stadiums(root)
    clubs = team_colours.load_clubs(root)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def fixtures(day: date | None = Query(None, alias="date"), conn=Depends(database)):
        moment = now()
        today = moment.astimezone(zone).date()
        days = dashboard.match_days(conn, config["season"])
        day = day or dashboard.pick_day(days, today)
        return dashboard.fixtures_page(rows=dashboard.day_rows(conn, config["season"], day), days=days, day=day,
                                       today=today, now=moment, zone=zone, season=config["season"],
                                       info=health_info(conn), clubs=clubs)

    @app.get("/table", response_class=HTMLResponse, include_in_schema=False)
    def table(conn=Depends(database)):
        return dashboard.table_page(table=dashboard.league_table(conn, config["season"]), season=config["season"],
                                    info=health_info(conn), zone=zone, clubs=clubs)

    @app.get("/model-performance", response_class=HTMLResponse, include_in_schema=False)
    def performance_tab(conn=Depends(database)):
        return dashboard.performance_page(perf=performance_info(conn), scored=dashboard.scored_forecasts(conn),
                                          final=final_test, info=health_info(conn), zone=zone)

    @app.get("/how-it-works", response_class=HTMLResponse, include_in_schema=False)
    def how_it_works(conn=Depends(database)):
        return dashboard.how_page(model=dashboard.deployed_model(conn), cutoff_days=cutoff_days,
                                  max_age_hours=config["max_snapshot_age_hours"], info=health_info(conn), zone=zone)

    @app.get("/team/{team_id}", response_class=HTMLResponse, include_in_schema=False)
    def team_page(team_id: int, conn=Depends(database)):
        team = dashboard.team_detail(conn, team_id, config["season"])
        if team is None:
            return HTMLResponse(dashboard.team_not_found_page(health_info(conn), zone), status_code=404)
        return dashboard.team_page(team=team, table=dashboard.league_table(conn, config["season"]),
                                   stadium=grounds.get(team["team_name"]), season=config["season"], now=now(),
                                   zone=zone, info=health_info(conn), clubs=clubs)

    @app.get("/match/{match_id}", response_class=HTMLResponse, include_in_schema=False)
    def match_page(match_id: int, conn=Depends(database)):
        match = dashboard.match_detail(conn, match_id)
        if match is None:
            return HTMLResponse(dashboard.not_found_page(health_info(conn), zone), status_code=404)
        return dashboard.match_page(match=match, stadium=grounds.get(match["home"]), zone=zone,
                                    info=health_info(conn), clubs=clubs)

    return app
