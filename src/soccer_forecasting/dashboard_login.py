"""A read-only database login for the public dashboard.

The dashboard only reads saved forecasts, so its login may only read the tables
and views it uses (DASHBOARD_READS in api.py). It cannot write anything, and it
cannot read anything else, such as the stored football-data.org downloads. If
its password ever leaked, it could only be used to read what the website already
shows.

On Neon, roles created with SQL get only the privileges granted to them, unlike
roles created in the Neon Console, which can read and write everything. So this
module creates the login with SQL, run by the database's owner. Running it again
gives the login a new password, which makes the old one useless.
"""
import secrets
from urllib.parse import quote, urlencode

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from .api import DASHBOARD_READS

ROLE = "dashboard_reader"


class LoginError(RuntimeError):
    """The login could not be set up safely; nothing was changed."""


def reader_address(owner_url: str, role: str, password: str) -> str:
    """The owner's database address with the reader's name and password instead."""
    params = conninfo_to_dict(owner_url)
    host, port, dbname = params.pop("host", None), params.pop("port", None), params.pop("dbname", "")
    params.pop("user", None)
    params.pop("password", None)
    if not host:
        raise LoginError("the database address has no host name")
    address = f"postgresql://{quote(role, safe='')}:{quote(password, safe='')}@{host}"
    address += f":{port}" if port else ""
    address += f"/{quote(dbname, safe='')}"
    return address + (f"?{urlencode(params)}" if params else "")


def create_dashboard_login(conn, owner_url: str, role: str = ROLE) -> dict:
    """Create the read-only login, or give it a new password, and prove it cannot write.

    Returns the reader's address (with its new password) and what was checked.
    """
    password = secrets.token_urlsafe(32)  # about 190 bits; Neon asks for at least 60
    name = sql.Identifier(role)
    with conn.transaction():
        found = conn.execute(
            """SELECT rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls,
                      EXISTS (SELECT 1 FROM pg_auth_members WHERE member = r.oid)
               FROM pg_roles r WHERE rolname = %s""", (role,)).fetchone()
        if found and (found[0] or found[1]):
            raise LoginError(f"a role named {role} already exists with more powers than reading, "
                             f"for example one created in the Neon Console; remove it or pick another name")
        verb = "ALTER" if found else "CREATE"
        conn.execute(sql.SQL(verb + " ROLE {} WITH LOGIN PASSWORD {}").format(name, sql.Literal(password)))
        conn.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(name))
        conn.execute(sql.SQL("ALTER ROLE {} SET statement_timeout = '15s'").format(name))

        database, schema = conn.execute("SELECT current_database(), current_schema()").fetchone()
        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(database), name))
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema), name))
        # Start from nothing, so running again also removes anything granted earlier.
        conn.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM {}").format(sql.Identifier(schema), name))
        for relation in DASHBOARD_READS:
            conn.execute(sql.SQL("GRANT SELECT ON {} TO {}").format(sql.Identifier(schema, relation), name))

    address = reader_address(owner_url, role, password)
    return {"role": role, "created": not found, "address": address, "reads": list(DASHBOARD_READS),
            "write_refused": _write_is_refused(address)}


def _write_is_refused(address: str) -> bool:
    """Log in as the reader, switch the read-only setting off as an attacker would, and try to delete."""
    with psycopg.connect(address, autocommit=True) as reader:
        reader.execute("SELECT count(*) FROM predictions").fetchone()
        reader.execute("SET default_transaction_read_only = off")
        try:
            reader.execute("DELETE FROM predictions WHERE false")
        except psycopg.errors.InsufficientPrivilege:
            return True
    raise LoginError("the new login was able to write; check the database's default privileges")
