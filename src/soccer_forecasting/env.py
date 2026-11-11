"""Read settings such as DATABASE_URL from the project's .env file.

Only the names asked for are read, and a value already set in the terminal
always wins. The API key is deliberately not loaded here: only the download
code reads it, so it can never leak into anything else.
"""
import os
from pathlib import Path


def load_env(root: Path, names: tuple[str, ...]) -> None:
    path = root / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.strip().partition("=")
        name = name.strip()
        if sep and name in names and not os.environ.get(name):
            os.environ[name] = value.strip().strip('"').strip("'")
