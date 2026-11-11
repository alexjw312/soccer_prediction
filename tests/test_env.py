"""Reading settings from .env."""
from soccer_forecasting.env import load_env


def test_reads_only_the_names_asked_for(tmp_path, monkeypatch):
    for name in ("DATABASE_URL", "FOOTBALL_DATA_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text("# settings\nDATABASE_URL=postgresql://x\nFOOTBALL_DATA_API_KEY=secret\n")
    load_env(tmp_path, ("DATABASE_URL",))
    import os
    assert os.environ["DATABASE_URL"] == "postgresql://x"
    assert "FOOTBALL_DATA_API_KEY" not in os.environ


def test_a_value_set_in_the_terminal_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://terminal")
    (tmp_path / ".env").write_text("DATABASE_URL=postgresql://file\n")
    load_env(tmp_path, ("DATABASE_URL",))
    import os
    assert os.environ["DATABASE_URL"] == "postgresql://terminal"


def test_missing_file_is_fine(tmp_path):
    load_env(tmp_path, ("DATABASE_URL",))
