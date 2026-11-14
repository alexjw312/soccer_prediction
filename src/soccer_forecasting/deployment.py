"""Train the model that makes live forecasts.

It uses the frozen choice and settings from model selection, refitted on every
historical season including the final test season. Because that uses the test
season for training, it is refused until the one-time final test has run.
This is a separate training run from the one the final test scored, and it
is recorded as such in model_runs.

The model file is also stored in the database (model_files), so a computer
that starts empty, such as GitHub's, can use the recorded model instead of
refitting it. Refitting on another computer can give a file that differs in
the last digits, which would no longer be the recorded model.
"""
import hashlib
import json
import shutil
import tempfile
from pathlib import Path

from .model_selection import FINAL_RESULT, FROZEN_SELECTION, ProtocolError, load_protocol
from .models import CLASSES, FEATURES, fit, load_dataset, save_artifact


def _settings_json(settings: dict) -> str:
    return json.dumps(settings, sort_keys=True)


def store_model_file(conn, run_id: int, data: bytes) -> None:
    """Keep the model file in the database. It refuses bytes that differ from the recorded hash."""
    conn.execute("INSERT INTO model_files (model_run_id, content) VALUES (%s, %s) ON CONFLICT (model_run_id) DO NOTHING",
                 (run_id, data))


def stored_model_file(conn, run_id: int) -> bytes | None:
    row = conn.execute("SELECT content FROM model_files WHERE model_run_id = %s", (run_id,)).fetchone()
    return None if row is None else bytes(row[0])


def train_deployment(conn, root: Path) -> dict:
    if not (root / FINAL_RESULT).exists():
        raise ProtocolError("run the final test first: training on every season uses the test season, "
                            "which would make a later final test meaningless")
    frozen = json.loads((root / FROZEN_SELECTION).read_text(encoding="utf-8"))
    split, protocol, protocol_sha = load_protocol(root)
    if protocol_sha != frozen["protocol_sha256"]:
        raise ProtocolError("the protocol changed after the model was selected")

    seasons = split["initial_modeling_seasons"]
    data = load_dataset(conn, split, frozen["feature_version"], seasons, allow_test=True)
    fingerprint = data.fingerprint()
    name, settings = frozen["selected_model"], frozen["settings"]

    existing = conn.execute(
        """SELECT model_run_id, artifact_path, artifact_sha256 FROM model_runs
           WHERE model_name = %s AND settings = %s::jsonb AND training_data_fingerprint = %s
             AND protocol_sha256 = %s""",
        (name, _settings_json(settings), fingerprint, protocol_sha)).fetchone()
    if existing:
        run_id, path, sha = existing
        file = root / path
        if file.exists() and hashlib.sha256(file.read_bytes()).hexdigest() == sha:
            store_model_file(conn, run_id, file.read_bytes())
            return {"model_run_id": run_id, "created": False, "model": name, "training_matches": len(data.y)}
        stored = stored_model_file(conn, run_id)
        if stored is not None:  # the database guarantees it matches the recorded hash
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(stored)
            return {"model_run_id": run_id, "created": False, "restored": True, "model": name,
                    "training_matches": len(data.y)}

    model = fit(name, settings, frozen["random_seed"], data)
    metadata = {
        "model": name, "settings": settings, "purpose": "live forecasts",
        "feature_version": frozen["feature_version"], "training_seasons": seasons,
        "training_matches": int(len(data.y)), "source_commit": split["source_commit"],
        "note": "Refitted on all historical seasons after the final test; not the evaluated artifact."}

    if existing:
        # Rebuild the recorded file only if refitting reproduces it exactly; otherwise leave it alone.
        target = root / existing[1]
        with tempfile.TemporaryDirectory() as scratch:
            record = save_artifact(model, Path(scratch), metadata)
            if record["model_sha256"] != existing[2]:
                raise RuntimeError(
                    f"refitting here gave a slightly different model file than recorded model run {existing[0]}, "
                    f"and the database holds no copy of that file. Run train-deployment on the computer that "
                    f"trained it: that stores a copy in the database.")
            target.parent.mkdir(parents=True, exist_ok=True)
            for file_name in ("model.joblib", "metadata.json"):
                shutil.copyfile(Path(scratch) / file_name, target.parent / file_name)
        store_model_file(conn, existing[0], target.read_bytes())
        return {"model_run_id": existing[0], "created": False, "restored": True, "model": name,
                "training_matches": len(data.y)}

    folder = root / "artifacts" / "deployment" / fingerprint[:12]
    record = save_artifact(model, folder, metadata)
    path = (folder / "model.joblib").relative_to(root).as_posix()
    model_bytes = (root / path).read_bytes()
    with conn.transaction():
        run_id = conn.execute(
            """INSERT INTO model_runs (model_name, purpose, feature_version, settings, training_seasons,
                                       training_matches, training_data_fingerprint, protocol_sha256, class_order,
                                       features, artifact_path, artifact_sha256, packages, source_commit)
               VALUES (%s, 'deployment', %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
               RETURNING model_run_id""",
            (name, frozen["feature_version"], _settings_json(settings), seasons, int(len(data.y)), fingerprint,
             protocol_sha, list(CLASSES), FEATURES, path, record["model_sha256"], json.dumps(record["packages"]),
             split["source_commit"])).fetchone()[0]
        store_model_file(conn, run_id, model_bytes)
    return {"model_run_id": run_id, "created": True, "model": name, "training_matches": len(data.y)}
