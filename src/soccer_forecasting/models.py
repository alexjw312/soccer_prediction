"""Datasets and model definitions for Milestone 3.

A dataset is read from the fixture_features view and joined to the result
only here. Labels use the fixed order 0 = H, 1 = D, 2 = A, and every fitted
model is checked to use the same order, so a probability can never end up
in the wrong column.
"""
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

import joblib
import numpy as np
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from .baseline import ProtectedSeasonError
from .metrics import CLASSES, check_probabilities

FEATURES = [f"{side}_{name}_last{n}"
            for side in ("home", "away") for n in (5, 10)
            for name in ("games", "ppg", "goals_for", "goals_against")]
LABEL_INDEX = {label: i for i, label in enumerate(CLASSES)}


@dataclass(frozen=True)
class Dataset:
    match_ids: np.ndarray
    seasons: np.ndarray
    X: np.ndarray
    y: np.ndarray

    def where(self, seasons) -> "Dataset":
        keep = np.isin(self.seasons, list(seasons))
        return Dataset(self.match_ids[keep], self.seasons[keep], self.X[keep], self.y[keep])

    def column(self, name: str) -> np.ndarray:
        return self.X[:, FEATURES.index(name)]

    def fingerprint(self) -> str:
        """A hash of exactly what a model sees, to detect data changing later."""
        digest = hashlib.sha256()
        for part in (self.match_ids.astype(np.int64), np.nan_to_num(self.X, nan=-999.0), self.y.astype(np.int64)):
            digest.update(np.ascontiguousarray(part).tobytes())
        return digest.hexdigest()


def load_dataset(conn, split_config: dict, feature_version: str, seasons: list[str],
                 *, allow_test: bool = False) -> Dataset:
    protected = set(seasons) & set(split_config["split"]["test"])
    if protected and not allow_test:
        raise ProtectedSeasonError(f"refusing to read the final test season: {sorted(protected)}")
    columns = ", ".join(f"f.{name}" for name in FEATURES)
    rows = conn.execute(
        f"""SELECT f.match_id, f.season, {columns}, m.result
            FROM fixture_features f JOIN matches m USING (match_id)
            WHERE f.feature_version = %s AND f.season = ANY(%s) AND m.status = 'completed'
            ORDER BY f.match_date, f.match_id""", (feature_version, seasons)).fetchall()
    found = {row[1] for row in rows}
    missing = [s for s in seasons if s not in found]
    if missing:
        raise RuntimeError(f"no {feature_version} features for {missing}; run build-features first")
    return Dataset(
        match_ids=np.array([row[0] for row in rows]),
        seasons=np.array([row[1] for row in rows], dtype=object),
        X=np.array([row[2:-1] for row in rows], dtype=float),  # None becomes NaN
        y=np.array([LABEL_INDEX[row[-1]] for row in rows]),
    )


class ClassFrequencyModel:
    """The baseline: the same H/D/A shares for every match, learned from training."""

    def fit(self, X, y):
        counts = np.bincount(y, minlength=3)
        if (counts == 0).any():
            raise ValueError("every outcome needs at least one training example")
        self.probabilities_ = counts / counts.sum()
        self.classes_ = np.arange(3)
        return self

    def predict_proba(self, X):
        return np.tile(self.probabilities_, (len(X), 1))


def _xgboost_classifier():
    try:
        from xgboost import XGBClassifier
    except Exception as error:  # XGBoost raises its own error type when OpenMP is missing
        raise RuntimeError("XGBoost could not be loaded. On macOS install OpenMP first: "
                           "brew install libomp") from error
    return XGBClassifier


def make_model(name: str, settings: dict, seed: int):
    if name == "class_frequency_baseline":
        return ClassFrequencyModel()
    if name == "logistic_regression":
        # Imputation and scaling sit inside the pipeline, so they are fitted on
        # the training rows only and then applied unchanged to later matches.
        return Pipeline([
            ("impute", SimpleImputer(strategy="mean")),
            ("scale", StandardScaler()),
            ("model", LogisticRegression(random_state=seed, **settings)),
        ])
    if name == "xgboost":
        return _xgboost_classifier()(objective="multi:softprob", eval_metric="mlogloss",
                                     random_state=seed, **settings)
    raise ValueError(f"unknown model {name!r}")


def fit(name: str, settings: dict, seed: int, train: Dataset):
    model = make_model(name, settings, seed).fit(train.X, train.y)
    if list(model.classes_) != [0, 1, 2]:
        raise ValueError(f"{name} learned classes {list(model.classes_)}, expected H, D, A")
    return model


def predict(model, data: Dataset) -> np.ndarray:
    P = np.asarray(model.predict_proba(data.X), dtype=float)
    check_probabilities(P)
    return P


def package_versions() -> dict:
    names = ["numpy", "scipy", "scikit-learn", "joblib"]
    out = {name: version(name) for name in names}
    for name in ("xgboost", "xgboost-cpu"):
        try:
            out[name] = version(name)
        except Exception:
            pass
    return out


def save_artifact(model, folder: Path, metadata: dict) -> dict:
    """Save a fitted model with everything needed to reuse or audit it."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "model.joblib"
    joblib.dump(model, path)
    record = {**metadata, "features": FEATURES, "class_order": list(CLASSES),
              "packages": package_versions(),
              "saved_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "model_file": path.name,
              "model_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    (folder / "metadata.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record
