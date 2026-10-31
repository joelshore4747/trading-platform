"""Credentials and connector/broker skeletons.

Two things are under test: that a secret cannot leak through a repr, and that
no skeleton can act. The second is a standing guard — if someone implements a
live broker, these tests fail and the change becomes impossible to land
quietly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from trading_platform.brokers.base import (
    AccountSnapshot,
    BinanceBroker,
    Broker,
    BrokerNotImplementedError,
    PaperBroker,
    available_brokers,
)
from trading_platform.core.credentials import (
    REDACTED,
    Credential,
    CredentialSet,
    MissingCredentialError,
)
from trading_platform.data.connectors import (
    BinanceConnector,
    ConnectorNotImplementedError,
    DeribitConnector,
    MarketDataConnector,
    available_connectors,
)

SECRET = "sk-not-a-real-key-9f3a2b7c1d4e"
START = datetime(2025, 1, 1, tzinfo=UTC)


# -- Secrets must not leak --------------------------------------------------


def test_repr_never_contains_the_secret(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The failure mode that actually happens: a secret in a log or traceback."""
    monkeypatch.setenv("TEST_API_KEY", SECRET)
    cred = Credential.from_env("TEST_API_KEY")

    assert cred.is_present
    assert SECRET not in repr(cred)
    assert SECRET not in str(cred)
    assert SECRET not in f"{cred}"
    assert SECRET not in "{}".format(cred)  # noqa: UP032 — deliberately exercising format
    assert REDACTED in repr(cred)


def test_secret_does_not_leak_through_a_containing_object(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TEST_API_KEY", SECRET)
    monkeypatch.setenv("TEST_API_SECRET", SECRET)
    creds = CredentialSet.from_env("venue", "TEST_API_KEY", "TEST_API_SECRET")

    assert SECRET not in repr(creds)
    assert SECRET not in repr(list(creds.credentials))
    assert SECRET not in repr({"creds": creds})


def test_value_is_reachable_only_through_named_accessors(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`require` and `reveal` are deliberately conspicuous, so reads are greppable."""
    monkeypatch.setenv("TEST_API_KEY", SECRET)
    cred = Credential.from_env("TEST_API_KEY")
    assert cred.require() == SECRET
    assert cred.reveal() == SECRET


# -- Absence fails loudly ---------------------------------------------------


def test_missing_credential_names_the_variable(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("TEST_API_KEY", raising=False)
    cred = Credential.from_env("TEST_API_KEY")

    assert not cred.is_present
    assert cred.reveal() is None
    with pytest.raises(MissingCredentialError, match="TEST_API_KEY"):
        cred.require()


def test_blank_is_treated_as_absent(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """.env.example ships every variable empty. A half-configured environment
    must fail like an unconfigured one, not send an empty key."""
    monkeypatch.setenv("TEST_API_KEY", "")
    assert not Credential.from_env("TEST_API_KEY").is_present


def test_credential_set_reports_what_is_missing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("A_KEY", SECRET)
    monkeypatch.delenv("A_SECRET", raising=False)
    creds = CredentialSet.from_env("venue", "A_KEY", "A_SECRET")

    assert not creds.is_complete
    assert creds.missing == ("A_SECRET",)
    with pytest.raises(MissingCredentialError, match="A_SECRET"):
        creds.require_all()


def test_empty_credential_set_is_vacuously_complete() -> None:
    """A public endpoint needs nothing, and that must not read as an error."""
    public = CredentialSet(venue="binance")
    assert public.is_complete
    assert public.missing == ()
    assert public.require_all() == {}
    assert "public" in repr(public)


# -- Connectors cannot act --------------------------------------------------


@pytest.mark.parametrize("connector", [BinanceConnector(), DeribitConnector()])
def test_every_connector_method_refuses(connector) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ConnectorNotImplementedError, match="skeleton"):
        connector.symbols()
    with pytest.raises(ConnectorNotImplementedError, match="skeleton"):
        connector.fetch_bars("BTCUSDT", "1d", START)
    with pytest.raises(ConnectorNotImplementedError, match="skeleton"):
        connector.fetch_funding("BTCUSDT", START)


def test_connectors_satisfy_the_protocol() -> None:
    for connector in available_connectors().values():
        assert isinstance(connector, MarketDataConnector)


def test_binance_connector_declares_no_credentials() -> None:
    """Klines are public. Declaring keys here would grant account access this
    project has no use for — the empty set is the security decision."""
    connector = BinanceConnector()
    assert connector.credentials.required_variables == ()
    assert connector.credentials.is_complete


def test_deribit_connector_declares_read_only_pair() -> None:
    assert DeribitConnector().credentials.required_variables == (
        "DERIBIT_CLIENT_ID",
        "DERIBIT_CLIENT_SECRET",
    )


def test_connector_error_points_at_the_working_path() -> None:
    with pytest.raises(ConnectorNotImplementedError, match="fetch_binance"):
        BinanceConnector().fetch_bars("BTCUSDT", "1d", START)


# -- Brokers cannot act -----------------------------------------------------


@pytest.mark.parametrize("broker", [PaperBroker(), BinanceBroker()])
def test_every_broker_method_refuses(broker) -> None:  # type: ignore[no-untyped-def]
    """The standing guard: implementing a live broker breaks this test, so the
    change cannot land quietly."""
    from decimal import Decimal

    from trading_platform.core.events import Order
    from trading_platform.core.types import Side

    order = Order(order_id="o1", symbol="BTCUSDT", side=Side.BUY, quantity=Decimal("1"))

    with pytest.raises(BrokerNotImplementedError, match="skeleton"):
        broker.submit(order)
    with pytest.raises(BrokerNotImplementedError, match="skeleton"):
        broker.cancel("o1")
    with pytest.raises(BrokerNotImplementedError, match="skeleton"):
        broker.fills_since(None)
    with pytest.raises(BrokerNotImplementedError, match="skeleton"):
        broker.account()


def test_brokers_satisfy_the_protocol() -> None:
    for broker in available_brokers().values():
        assert isinstance(broker, Broker)


def test_only_the_live_broker_is_flagged_live() -> None:
    """`is_live` is the flag anything dangerous should gate on."""
    assert PaperBroker().is_live is False
    assert BinanceBroker().is_live is True


def test_live_broker_error_says_it_is_unplanned() -> None:
    from decimal import Decimal

    from trading_platform.core.events import Order
    from trading_platform.core.types import Side

    order = Order(order_id="o", symbol="BTCUSDT", side=Side.BUY, quantity=Decimal("1"))
    with pytest.raises(BrokerNotImplementedError, match="Phase 6"):
        BinanceBroker().submit(order)


def test_account_snapshot_is_the_reconciliation_surface() -> None:
    from decimal import Decimal

    snap = AccountSnapshot(
        cash=Decimal("1000"), equity=Decimal("1200"), positions={"BTCUSDT": Decimal("2")}
    )
    assert snap.equity - snap.cash == Decimal("200")


# -- Neither layer may acquire I/O ------------------------------------------


@pytest.mark.parametrize(
    "module_path",
    [
        "trading_platform.data.connectors",
        "trading_platform.data.alpaca",
        "trading_platform.brokers.base",
        "trading_platform.core.credentials",
    ],
)
def test_no_network_or_filesystem_access_in_these_modules(module_path: str) -> None:
    """A diff that turns a `raise` into a network call must be hard to miss.
    This test makes it impossible to land silently."""
    import importlib

    module = importlib.import_module(module_path)
    assert module.__file__ is not None
    source = Path(module.__file__).read_text()

    for forbidden in (
        "import requests",
        "import httpx",
        "import socket",
        "urllib.request",
        "open(",
        "Path(",
    ):
        assert forbidden not in source, f"{module_path} must not contain {forbidden!r}"


def test_credentials_read_only_the_environment() -> None:
    """No file reading, no defaults, no config lookup — a secret that can be
    committed eventually is."""
    import trading_platform.core.credentials as mod

    assert mod.__file__ is not None
    source = Path(mod.__file__).read_text()
    assert "os.environ.get" in source
    assert "dotenv" not in source
    assert "json.load" not in source
