"""Apply the numbered SQL files in sql/ to a database, each exactly once.

A table called schema_migrations records which files have been applied, so
running init-db again only applies new files. That is how a database built
for an earlier milestone gets upgraded without reloading its data.
"""
from pathlib import Path

# Every object 001_schema.sql creates. A database that has all of them but no
# schema_migrations table was built before migrations existed (Milestone 1).
FIRST_SCHEMA_OBJECTS = ("teams", "team_aliases", "source_files", "matches", "team_matches")


class MigrationError(RuntimeError):
    pass


def migration_files(root: Path) -> list[Path]:
    return sorted((root / "sql").glob("[0-9][0-9][0-9]_*.sql"))


def apply_migrations(conn, root: Path) -> dict:
    """Apply pending migrations in order. Returns what was adopted and applied."""
    files = migration_files(root)
    adopted = []
    with conn.transaction():
        conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                            version    text        PRIMARY KEY,
                            applied_at timestamptz NOT NULL DEFAULT now())""")
        applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        if not applied:
            present = [name for name in FIRST_SCHEMA_OBJECTS
                       if conn.execute("SELECT to_regclass(%s)", (name,)).fetchone()[0] is not None]
            if len(present) == len(FIRST_SCHEMA_OBJECTS):
                first = files[0].stem
                conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (first,))
                applied.add(first)
                adopted.append(first)
            elif present:
                raise MigrationError(f"database has only part of the first schema: {', '.join(present)}")

    newly_applied = []
    for path in files:
        if path.stem in applied:
            continue
        with conn.transaction():
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.stem,))
        newly_applied.append(path.stem)
    return {"adopted": adopted, "applied": newly_applied}
