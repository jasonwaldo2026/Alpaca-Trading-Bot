"""
Credential resolution.

The Streamlit apps read `st.secrets`, which is not a plain mapping: with no
secrets.toml on disk, even `.get()` raises. Running locally from a `.env`
with no secrets file is the documented setup, so that path must fall back
to the environment rather than crash the app at import.
"""


from core.client import Credentials


class _NoSecretsFile:
    """Mimics st.secrets when no secrets.toml exists anywhere."""

    def get(self, key, default=None):
        raise FileNotFoundError("No secrets found.")


class _Secrets(dict):
    pass


def test_missing_secrets_file_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "env-key")
    monkeypatch.setenv("ALPACA_API_SECRET", "env-secret")
    creds = Credentials.from_streamlit(_NoSecretsFile())
    assert creds.api_key == "env-key"
    assert creds.api_secret == "env-secret"
    assert creds.is_complete()


def test_missing_secrets_file_and_no_env_is_incomplete_not_an_error(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    creds = Credentials.from_streamlit(_NoSecretsFile())
    assert not creds.is_complete()


def test_secrets_win_over_env(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "env-key")
    monkeypatch.setenv("ALPACA_API_SECRET", "env-secret")
    creds = Credentials.from_streamlit(
        _Secrets(ALPACA_API_KEY="secret-key", ALPACA_API_SECRET="secret-secret")
    )
    assert creds.api_key == "secret-key"
    assert creds.api_secret == "secret-secret"


def test_empty_secret_falls_back_to_env(monkeypatch):
    """A blank value in secrets.toml should not mask a configured .env."""
    monkeypatch.setenv("ALPACA_API_KEY", "env-key")
    monkeypatch.setenv("ALPACA_API_SECRET", "env-secret")
    creds = Credentials.from_streamlit(_Secrets(ALPACA_API_KEY=""))
    assert creds.api_key == "env-key"


def test_from_streamlit_is_paper_by_default():
    assert Credentials.from_streamlit(_Secrets()).paper is True


def test_from_env_is_paper_by_default(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_API_SECRET", "s")
    assert Credentials.from_env().paper is True


# ── Positions ────────────────────────────────────────────────────────────────

class _Position:
    def __init__(self, symbol, asset_class):
        self.symbol = symbol
        self.asset_class = asset_class
        self.qty = "1"


class _Enum:
    def __init__(self, value):
        self.value = value


class _FakeTrading:
    def __init__(self, positions):
        self._positions = positions

    def get_all_positions(self):
        return self._positions


def test_positions_are_keyed_by_watchlist_symbol():
    from core.client import AlpacaClient
    client = AlpacaClient.__new__(AlpacaClient)
    client.trading = _FakeTrading([
        _Position("BTCUSD", _Enum("crypto")),
        _Position("AAPL", _Enum("us_equity")),
    ])
    assert set(client.get_positions()) == {"BTC/USD", "AAPL"}
    assert client.get_position_qty("BTC/USD") == 1.0
    assert client.get_position_qty("BTCUSD") == 0.0
