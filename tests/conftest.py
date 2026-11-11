"""Shared test setup.

Database tests need TEST_DATABASE_URL. Each test runs inside its own new
PostgreSQL schema, which is dropped afterwards, so tests cannot touch real tables.
"""
import os
import shutil
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from soccer_forecasting import PROJECT_ROOT
from soccer_forecasting.env import load_env
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
