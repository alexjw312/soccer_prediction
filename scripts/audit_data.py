#!/usr/bin/env python3
"""Download a pinned OpenFootball snapshot and audit its structure.
Standard library only. This script does not train a model or create a database.
Run from any directory: python3 scripts/audit_data.py --download
Run offline against the saved snapshot: python3 scripts/audit_data.py
"""
import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "milestone0.json"


def json_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def flatten_matches(payload):
    if isinstance(payload.get("matches"), list):
        return payload["matches"], "matches"
    if isinstance(payload.get("rounds"), list):
        rows = []
        for group in payload["rounds"]:
            for match in group.get("matches", []):
                row = dict(match)
                row.setdefault("round", group.get("name"))
                rows.append(row)
        return rows, "rounds[].matches"
    raise ValueError("Unsupported JSON layout: no matches or rounds array")


def valid_score(score):
    return (isinstance(score, list) and len(score) == 2
            and all(type(x) is int and x >= 0 for x in score))


def download(config):
    manifest_path = ROOT / "data" / "manifest.json"
    existing = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
    if existing and existing["source_commit"] != config["source_commit"]:
        raise ValueError("Snapshot commit differs. Use a new project copy for a new snapshot.")
    manifest = existing or {
        "provider": "openfootball/football.json",
        "source_commit": config["source_commit"],
        "source_commit_timestamp": config["source_commit_timestamp"],
        "audit_as_of": config["as_of_date"],
        "files": []
    }
    by_path = {row["source_path"]: row for row in manifest["files"]}
    paths = [f"{s}/en.1.json" for s in config["seasons"]] + ["LICENSE.md", "README.md"]
    for source_path in paths:
        prior = by_path.get(source_path)
        if prior:
            if prior["status"] == 200:
                local = ROOT / prior["local_path"]
                if not local.is_file() or sha256(local.read_bytes()) != prior["sha256"]:
                    raise ValueError(f"Existing snapshot failed hash verification: {source_path}")
            print(f"Cached {source_path}: {prior['status']}", flush=True)
            continue
        url = f"https://raw.githubusercontent.com/openfootball/football.json/{config['source_commit']}/{source_path}"
        rel = Path("data/raw/openfootball") / config["source_commit"] / source_path
        entry = {"source_path": source_path, "url": url,
                 "retrieved_at_utc": datetime.now(timezone.utc).isoformat()}
        try:
            request = Request(url, headers={"User-Agent": "SoccerForecasting-Milestone0/1.0"})
            with urlopen(request, timeout=25) as response:
                body = response.read()
                entry.update(status=response.status, bytes=len(body), sha256=sha256(body),
                             local_path=rel.as_posix(), content_type=response.headers.get("Content-Type"),
                             last_modified=response.headers.get("Last-Modified"))
            (ROOT / rel).parent.mkdir(parents=True, exist_ok=True)
            (ROOT / rel).write_bytes(body)
        except HTTPError as error:
            entry.update(status=error.code, error=str(error))
            if error.code not in (404, 410):
                manifest["files"].append(entry)
                json_write(manifest_path, manifest)
                raise
        except (URLError, TimeoutError) as error:
            # Do not manufacture missing-data results when transport failed.
            raise RuntimeError(f"Transport failed for {source_path}: {error}") from error
        manifest["files"].append(entry)
        by_path[source_path] = entry
        json_write(manifest_path, manifest)
        print(f"Fetched {source_path}: {entry['status']}, {entry.get('bytes', 0)} bytes", flush=True)
    return manifest


def audit_season(payload, season, as_of):
    matches, layout = flatten_matches(payload)
    teams, pairs, dates, keys, score_keys = Counter(), Counter(), [], Counter(), Counter()
    errors = []
    missing = Counter()
    valid_ft = 0
    future_scored = []
    future_unscored = 0
    past_unscored = 0
    valid_times = 0
    timezone_aware_times = 0
    result_counts = Counter()
    score_formats = Counter()
    completed_dates = []
    for index, row in enumerate(matches):
        prefix = {"row": index, "season": season}
        keys.update(row.keys())
        raw_score = row.get("score")
        if isinstance(raw_score, dict):
            score_formats["object"] += 1
            score_obj = raw_score
        elif valid_score(raw_score):
            # This snapshot also represents some full-time scores as a bare pair.
            score_formats["bare_full_time_pair"] += 1
            score_obj = {"ft": raw_score}
        elif raw_score is None:
            score_formats["absent_or_null"] += 1
            score_obj = {}
        else:
            score_formats["invalid"] += 1
            errors.append({**prefix, "issue": "unsupported_score_shape", "value": raw_score})
            score_obj = {}
        score_keys.update(score_obj.keys())
        for field in ("date", "time", "team1", "team2", "round"):
            if row.get(field) in (None, ""):
                missing[field] += 1
        try:
            day = date.fromisoformat(row["date"])
            dates.append(day)
            start_year = int(season[:4])
            if not date(start_year, 8, 1) <= day <= date(start_year + 1, 7, 31):
                errors.append({**prefix, "issue": "date_outside_expected_season_window", "date": day.isoformat()})
        except (KeyError, TypeError, ValueError):
            errors.append({**prefix, "issue": "invalid_date"})
            day = None
        home, away = row.get("team1"), row.get("team2")
        if not isinstance(home, str) or not home.strip() or not isinstance(away, str) or not away.strip():
            errors.append({**prefix, "issue": "invalid_team"})
        else:
            teams.update((home, away))
            pairs[(home, away)] += 1
            if home == away:
                errors.append({**prefix, "issue": "same_home_and_away"})
        raw_time = row.get("time")
        if raw_time:
            if re.fullmatch(r"\d{2}:\d{2}", str(raw_time)):
                hour, minute = map(int, raw_time.split(":"))
                if 0 <= hour <= 23 and 0 <= minute <= 59:
                    valid_times += 1
                else:
                    errors.append({**prefix, "issue": "invalid_time"})
            elif re.search(r"(Z|[+-]\d\d:\d\d|UTC)", str(raw_time)):
                timezone_aware_times += 1
            else:
                errors.append({**prefix, "issue": "unrecognized_time", "value": raw_time})
        ft = score_obj.get("ft")
        if valid_score(ft):
            valid_ft += 1
            if day and day > as_of:
                future_scored.append({**prefix, "date": day.isoformat(), "teams": [home, away]})
            elif day:
                completed_dates.append(day)
                result_counts["H" if ft[0] > ft[1] else "A" if ft[1] > ft[0] else "D"] += 1
        elif ft is not None and ft != []:
            errors.append({**prefix, "issue": "invalid_full_time_score", "value": ft})
        elif day and day > as_of:
            future_unscored += 1
        else:
            past_unscored += 1
        ht = score_obj.get("ht")
        if ht is not None and not valid_score(ht):
            errors.append({**prefix, "issue": "invalid_half_time_score", "value": ht})
        if valid_score(ht) and valid_score(ft) and any(a > b for a, b in zip(ht, ft)):
            errors.append({**prefix, "issue": "half_time_exceeds_full_time"})
    duplicate_pairs = [{"home": h, "away": a, "count": n} for (h, a), n in pairs.items() if n > 1]
    expected_pairs = {(h, a) for h in teams for a in teams if h != a}
    missing_pairs = expected_pairs.difference(pairs)
    complete = (len(matches) == 380 and len(teams) == 20 and valid_ft == 380
                and not errors and not duplicate_pairs and not missing_pairs and not future_scored)
    return {
        "season": season, "name": payload.get("name"), "layout": layout,
        "matches": len(matches), "valid_full_time_scores": valid_ft,
        "completed_on_or_before_as_of": sum(result_counts.values()),
        "teams": len(teams), "date_min": min(dates).isoformat() if dates else None,
        "date_max": max(dates).isoformat() if dates else None,
        "latest_completed_date": max(completed_dates).isoformat() if completed_dates else None,
        "missing": {k: missing[k] for k in ("date", "time", "team1", "team2", "round")},
        "time_without_timezone": valid_times, "time_with_explicit_timezone": timezone_aware_times,
        "match_keys": dict(sorted(keys.items())), "score_keys": dict(sorted(score_keys.items())),
        "raw_score_formats": dict(score_formats),
        "duplicate_ordered_team_pairs": duplicate_pairs,
        "missing_ordered_team_pairs_count": len(missing_pairs),
        "team_appearance_counts": dict(sorted(teams.items())),
        "unscored_on_or_before_as_of": past_unscored, "unscored_after_as_of": future_unscored,
        "future_scored_rows": future_scored, "errors": errors,
        "structurally_complete_380_match_season": complete,
        "label_counts_data_integrity_only": dict(result_counts),
        "example_record": matches[0] if matches else None
    }


def run_audit(config, manifest):
    as_of = date.fromisoformat(config["as_of_date"])
    by_path = {r["source_path"]: r for r in manifest["files"]}
    seasons = []
    for season in config["seasons"]:
        entry = by_path.get(f"{season}/en.1.json")
        if not entry or entry["status"] != 200:
            seasons.append({"season": season, "download_status": entry["status"] if entry else "not_downloaded"})
            continue
        path = ROOT / entry["local_path"]
        data = path.read_bytes()
        if sha256(data) != entry["sha256"]:
            raise ValueError(f"Hash mismatch: {path}")
        try:
            summary = audit_season(json.loads(data), season, as_of)
            summary["download_status"] = 200
            # The final test season's outcome totals stay out of saved output.
            if season in config.get("split", {}).get("test", []):
                summary["label_counts_data_integrity_only"] = "withheld: final test season"
        except (json.JSONDecodeError, ValueError) as error:
            summary = {"season": season, "download_status": 200, "parse_error": str(error)}
        seasons.append(summary)
    complete = [r for r in seasons if r.get("structurally_complete_380_match_season")]
    all_names = sorted({t for s in seasons for t in s.get("team_appearance_counts", {})})
    result = {
        "audit_as_of": config["as_of_date"], "source_commit": config["source_commit"],
        "source_commit_timestamp": config["source_commit_timestamp"],
        "complete_seasons": [r["season"] for r in complete],
        "complete_season_match_count": sum(r["matches"] for r in complete),
        "source_team_names": all_names, "seasons": seasons,
        "initial_modeling_seasons": config.get("initial_modeling_seasons", []),
        "scope": "Structural and availability audit only; no models fitted.",
        "limitations": [
            "A structurally valid result is not independently verified against an official match record.",
            "The snapshot does not establish original publication time or historical fixture revisions.",
            "Missing scores do not establish match status.",
            "Times without timezone metadata are not normalized UTC timestamps.",
            "Source name strings are not stable provider numeric IDs."
        ]
    }
    json_write(ROOT / "audit" / "data_audit.json", result)
    lines = [
        "OpenFootball snapshot — automated audit", "",
        f"As of {config['as_of_date']}; commit {config['source_commit']}.", "",
        "| Season | Rows | FT scores | Teams | Missing times | Complete season |",
        "| --- | ---: | ---: | ---: | ---: | --- |"
    ]
    for row in seasons:
        if "matches" not in row:
            lines.append(f"| {row['season']} | — | — | — | — | HTTP {row['download_status']} |")
        else:
            lines.append(f"| {row['season']} | {row['matches']} | {row['valid_full_time_scores']} | {row['teams']} | {row['missing']['time']} | {'Yes' if row['structurally_complete_380_match_season'] else 'No'} |")
    lines += ["", f"Structurally complete seasons contain {result['complete_season_match_count']} matches.",
              "", "These are data integrity counts, not prediction results. See data_audit.json for detailed checks.",
              "", "Raw files must match the saved manifest hashes before the audit runs.", ""]
    (ROOT / "audit" / "coverage.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({
        "complete_seasons": result["complete_seasons"],
        "complete_season_match_count": result["complete_season_match_count"],
        "source_team_name_count": len(all_names),
        "seasons": [{k: r.get(k) for k in ("season", "download_status", "matches", "valid_full_time_scores",
            "date_max", "latest_completed_date", "missing", "structurally_complete_380_match_season")} for r in seasons]
    }, indent=2))
    fatal = [r for r in seasons if r["season"] in config.get("initial_modeling_seasons", config["seasons"])
             and not r.get("structurally_complete_380_match_season")]
    return 1 if fatal else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true", help="Fetch pinned snapshot files, reusing verified existing bytes.")
    args = parser.parse_args()
    config = json.loads(CONFIG.read_text())
    manifest = download(config) if args.download else json.loads((ROOT / "data/manifest.json").read_text())
    if manifest["source_commit"] != config["source_commit"]:
        raise ValueError("Config and manifest commits do not match")
    return run_audit(config, manifest)


if __name__ == "__main__":
    sys.exit(main())

