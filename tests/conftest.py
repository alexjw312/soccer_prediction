"""Shared test setup.

Database tests need TEST_DATABASE_URL. Each test runs inside its own new
PostgreSQL schema, which is dropped afterwards, so tests cannot touch real tables.
"""
import json
import os
import shutil
from datetime import datetime, timezone
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from soccer_forecasting import PROJECT_ROOT
from soccer_forecasting.env import load_env
from soccer_forecasting.features import build_features
from soccer_forecasting.live_loading import load_live
from soccer_forecasting.loading import load_history
from soccer_forecasting.model_selection import file_sha256
from soccer_forecasting.migrations import apply_migrations


@pytest.fixture
def db():
    load_env(PROJECT_ROOT, ("TEST_DATABASE_URL",))
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("set TEST_DATABASE_URL to run database tests")
    name = sql.Identifier(f"test_{uuid4().hex}")
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(name))
        try:
            conn.execute(sql.SQL("SET search_path TO {}").format(name))
            apply_migrations(conn, PROJECT_ROOT)
            yield conn
        finally:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(name))


@pytest.fixture
def project_copy(tmp_path):
    """A disposable copy of the project's inputs that a test is free to damage.

    Real live downloads in data/live are left out, so tests behave the same
    whether or not fetch-live has been run on this machine.
    """
    for folder in ("config", "data", "sql"):
        ignore = shutil.ignore_patterns("live") if folder == "data" else None
        shutil.copytree(PROJECT_ROOT / folder, tmp_path / folder, ignore=ignore)
    return tmp_path


@pytest.fixture
def ready(db, project_copy):
    """History loaded, features built, a frozen selection and final-test stand-in in place, and a
    generated live season loaded from one download (rounds 1-10 finished, downloaded 30 Oct 05:00 UTC)."""
    from live_helpers import add_snapshot
    root = project_copy
    load_history(db, root)
    build_features(db, root)
    reports = root / "reports"
    reports.mkdir()
    (reports / "frozen_selection.json").write_text(json.dumps({
        "selected_model": "logistic_regression", "settings": {"C": 0.03, "max_iter": 5000},
        "feature_version": "form_v1", "random_seed": 20261110,
        "protocol_sha256": file_sha256(root / "config" / "milestone3.json")}))
    (reports / "final_test.json").write_text('{"note": "stand-in for the one-time final test"}')
    add_snapshot(root, datetime(2026, 10, 30, 5, 0, tzinfo=timezone.utc), finished_rounds=10)
    load_live(db, root)
    return db, root


@pytest.fixture
def trained(ready):
    from soccer_forecasting.deployment import train_deployment
    db, root = ready
    train_deployment(db, root)
    return db, root
