"""Make and save live forecasts for one match day.

The policy (config/milestone0.json and config/live.json):

- forecasts for match day D are made on day D in London time, before kickoff;
- form counts only matches dated D minus 2 days or earlier, and only results
  that had been downloaded by the moment the forecast is made;
- the newest download must be recent, and no earlier fixture may still be
  missing its result, so stale data cannot pass for a fresh forecast;
- a saved forecast is never changed; running again the same day adds nothing.

A preview applies the same rules to any day but saves nothing.
"""
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import joblib
import numpy as np

from .features import FEATURE_VERSION, compute_form, cutoff_offset_days
from .live_loading import live_config, load_live
from .live_source import fetch_snapshot, season_start_year
from .metrics import check_probabilities
from .models import FEATURES
from .parsing import SourceDataError


class ForecastError(RuntimeError):
    """A rule for a trustworthy forecast was not met; nothing was saved."""


@dataclass
class Forecast:
    match_id: int
    kickoff_utc: datetime
    home: str
    away: str
    p_home: float
    p_draw: float
    p_away: float
    saved: bool = False


@dataclass
class ForecastDay:
    day: date
    model_run_id: int | None = None
    forecasts: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    inserted: int = 0
    already_saved: int = 0


def london_date(moment: datetime, zone: str) -> date:
    return moment.astimezone(ZoneInfo(zone)).date()


def latest_model_run(conn, root: Path) -> tuple[int, object]:
    row = conn.execute(
        """SELECT model_run_id, artifact_path, artifact_sha256, features, class_order FROM model_runs
           WHERE purpose = 'deployment' AND feature_version = %s
           ORDER BY created_at DESC, model_run_id DESC LIMIT 1""", (FEATURE_VERSION,)).fetchone()
    if row is None:
        raise ForecastError("no deployment model yet; run train-deployment first")
    run_id, path, sha, features, classes = row
    file = root / path
    if not file.exists():
        raise ForecastError(f"model file {path} is missing; run train-deployment to restore it")
    if hashlib.sha256(file.read_bytes()).hexdigest() != sha:
        raise ForecastError(f"model file {path} does not match its recorded hash")
    if list(features) != FEATURES or list(classes) != ["H", "D", "A"]:
        raise ForecastError("the model's features or class order differ from this code")
    return run_id, joblib.load(file)


def _latest_snapshot(conn, provider: str):
    return conn.execute(
        """SELECT source_commit, retrieved_at_utc FROM source_files WHERE provider = %s
           ORDER BY retrieved_at_utc DESC LIMIT 1""", (provider,)).fetchone()


def _feature_matrix(rows: list[dict], match_ids: list[int]) -> tuple[np.ndarray, list[dict]]:
    sides = {(r["match_id"], r["venue"]): r for r in rows}
    matrix, saved = [], []
    for match_id in match_ids:
        values = {}
        for side, venue in (("home", "H"), ("away", "A")):
            row = sides[(match_id, venue)]
            for name in FEATURES:
                if name.startswith(side + "_"):
                    values[name] = row[name[len(side) + 1:]]
        matrix.append([np.nan if values[n] is None else float(values[n]) for n in FEATURES])
        saved.append({**values, "feature_cutoff_date": sides[(match_id, "H")]["feature_cutoff_date"].isoformat()})
    return np.array(matrix, dtype=float), saved


def forecast_day(conn, root: Path, day: date, now: datetime, *, save: bool) -> ForecastDay:
    if now.tzinfo is None:
        raise ValueError("now must carry a time zone")
    config = live_config(root)
    zone, provider, season = config["local_timezone"], config["provider"], config["season"]
    offset = cutoff_offset_days(root)
    cutoff = day + timedelta(days=offset)
    result = ForecastDay(day=day)

    if save and london_date(now, zone) != day:
        raise ForecastError(f"forecasts for {day} are made on that day in London; it is "
                            f"{london_date(now, zone)} there now (use --preview to look without saving)")

    fixtures = conn.execute(
        """SELECT m.match_id, m.kickoff_utc, h.team_name, a.team_name
           FROM matches m JOIN teams h ON h.team_id = m.home_team_id JOIN teams a ON a.team_id = m.away_team_id
           WHERE m.season = %s AND m.status = 'scheduled' AND m.match_date = %s AND m.kickoff_utc > %s
           ORDER BY m.kickoff_utc, h.team_name""", (season, day, now)).fetchall()
    if not fixtures:
        return result  # nothing left to forecast today, so freshness does not matter

    problems = []
    latest = _latest_snapshot(conn, provider)
    if latest is None:
        problems.append("no live data loaded; run fetch-live and load-live")
    else:
        name, retrieved = latest
        age = now - retrieved
        if age < timedelta(0):
            problems.append(f"the newest download ({name}) is from after this forecast time")
        elif age > timedelta(hours=config["max_snapshot_age_hours"]):
            problems.append(f"the newest download ({name}) is {age.total_seconds() / 3600:.0f} hours old; "
                            f"run fetch-live and load-live")
    missing = conn.execute(
        """SELECT h.team_name || ' v ' || a.team_name || ' (' || m.match_date || ')'
           FROM matches m JOIN teams h ON h.team_id = m.home_team_id JOIN teams a ON a.team_id = m.away_team_id
           WHERE m.season = %s AND m.status = 'scheduled' AND m.match_date <= %s
           ORDER BY m.match_date""", (season, cutoff)).fetchall()
    if missing:
        problems.append(f"{len(missing)} fixtures dated on or before {cutoff} still have no result, "
                        f"e.g. {missing[0][0]}")
    if problems and save:
        raise ForecastError("; ".join(problems))
    result.warnings = problems

    run_id, model = latest_model_run(conn, root)
    result.model_run_id = run_id
    ids = [f[0] for f in fixtures]
    rows = compute_form(conn, root, offset_days=offset, seasons=[], match_ids=ids, as_of=now)
    X, feature_values = _feature_matrix(rows, ids)
    P = np.asarray(model.predict_proba(X), dtype=float)
    check_probabilities(P)

    snapshot = latest[0] if latest else None
    for (match_id, kickoff, home, away), probs, values in zip(fixtures, P, feature_values):
        forecast = Forecast(match_id, kickoff, home, away, *map(float, probs))
        if save:
            inserted = conn.execute(
                """INSERT INTO predictions (match_id, model_run_id, feature_version, forecast_for_date,
                                            feature_cutoff_date, information_cutoff_utc, generated_at_utc,
                                            kickoff_utc, data_snapshot, p_home, p_draw, p_away, features)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                   ON CONFLICT (match_id, model_run_id, forecast_for_date) DO NOTHING""",
                (match_id, run_id, FEATURE_VERSION, day, cutoff, now, now, kickoff, snapshot,
                 forecast.p_home, forecast.p_draw, forecast.p_away, json.dumps(values))).rowcount
            forecast.saved = bool(inserted)
            result.inserted += inserted
            result.already_saved += 1 - inserted
        result.forecasts.append(forecast)
    return result


def record_job(conn, job: str, started: datetime, status: str, details: dict) -> None:
    conn.execute("""INSERT INTO job_runs (job, started_at, finished_at, status, details)
                    VALUES (%s, %s, %s, %s, %s::jsonb)""",
                 (job, started, datetime.now(timezone.utc), status, json.dumps(details, default=str)))


def run_daily(conn, root: Path, now: datetime | None = None, fetch=fetch_snapshot) -> ForecastDay:
    """Download, load, then forecast today's matches. Every run is logged in job_runs."""
    started = datetime.now(timezone.utc)
    now = now or started
    zone = live_config(root)["local_timezone"]
    today = london_date(now, zone)
    try:
        snapshot = fetch(root, season_start_year(today))
        loaded = load_live(conn, root)
        result = forecast_day(conn, root, today, datetime.now(timezone.utc) if now is started else now, save=True)
    except Exception as error:
        record_job(conn, "daily", started, "failed", {"day": today, "error": f"{type(error).__name__}: {error}"})
        raise
    record_job(conn, "daily", started, "succeeded", {
        "day": today, "snapshot": snapshot["snapshot"], "snapshots_loaded": sum(s.loaded for s in loaded),
        "fixtures": len(result.forecasts), "inserted": result.inserted, "model_run_id": result.model_run_id})
    return result
