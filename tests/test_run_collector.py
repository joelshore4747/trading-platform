"""The collector entrypoint: what it refuses, and what `--check` promises.

The library is tested in `tests/test_collect.py`. This is about the script — the
thing systemd actually runs — and specifically about `--check`, whose whole
value is that it is trustworthy: it must catch a broken configuration, name what
is broken, and leave nothing behind.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_collector.py"


def load() -> Any:
    spec = importlib.util.spec_from_file_location("run_collector_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


collector = load()


def config(**over: Any) -> dict[str, Any]:
    source: dict[str, Any] = {
        "name": "alpaca-iex",
        "vendor": "alpaca",  # which client fetches
        "venue": "iex",  # which tape it describes
        "asset_class": "stocks",  # Alpaca's own path segment
        "symbols": ["AAPL"],
    }
    source.update(over)
    if source.get("venue") is None:
        source.pop("venue")
    return {"market": "equity", "interval": "1m", "source": [source]}


@pytest.fixture(autouse=True)
def _fake_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APCA_API_KEY_ID", "not-a-real-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "not-a-real-key")
    monkeypatch.setenv("POLYGON_API_KEY", "not-a-real-key")


def test_a_valid_config_validates_and_names_its_venue() -> None:
    checked = collector.validate(config())
    assert len(checked) == 1
    _spec, client = checked[0]
    assert client.venue == "alpaca-stocks"


def test_the_shipped_example_config_is_valid() -> None:
    """An example that does not parse is worse than no example: it is followed.

    Mutation: break a key name in `config/collector.example.toml` — red."""
    checked = collector.validate(collector.load_config(ROOT / "config" / "collector.example.toml"))
    assert {spec["name"] for spec, _ in checked} == {"alpaca-sip", "polygon"}


def test_a_missing_credential_names_the_variable_and_the_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 401 twenty minutes into a run is a worse way to learn this.

    Mutation: drop the `require_all()` call from `validate` — `--check` passes
    and the failure moves to the first poll, where systemd will restart into it
    forever."""
    monkeypatch.delenv("APCA_API_SECRET_KEY")
    with pytest.raises(SystemExit, match="APCA_API_SECRET_KEY"):
        collector.validate(config())


def test_a_source_with_no_symbols_or_no_name_is_refused() -> None:
    with pytest.raises(SystemExit, match="no symbols"):
        collector.validate(config(symbols=[]))
    with pytest.raises(SystemExit, match="needs a name"):
        collector.validate(config(name=""))


def test_zero_replicas_is_refused_rather_than_running_nothing() -> None:
    """`replicas = 0` would silently capture nothing from that source while the
    unit reported healthy. Mutation: drop the check — red."""
    with pytest.raises(SystemExit, match="replicas must be at least 1"):
        collector.validate(config(replicas=0))


def test_a_config_with_no_sources_is_refused(tmp_path: Path) -> None:
    """A collector with no sources runs forever and captures nothing, which
    looks exactly like a collector that is working."""
    path = tmp_path / "empty.toml"
    path.write_text('market = "stocks"\n')
    with pytest.raises(SystemExit, match="no \\[\\[source\\]\\]"):
        collector.load_config(path)
    with pytest.raises(SystemExit, match="no configuration at"):
        collector.load_config(tmp_path / "absent.toml")


def test_check_opens_no_connection_and_writes_no_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--check` that leaves an `ingest_runs` row behind is not a check: running
    it in `ExecStartPre` on every restart would fill the table with runs that
    captured nothing.

    Mutation: have `main` call `build` before the `--check` branch, as it once
    did — `connect` is called and this goes red."""
    calls: list[str] = []
    monkeypatch.setattr(collector, "is_configured", lambda: True)
    monkeypatch.setattr(
        collector,
        "connect",
        lambda *a, **k: calls.append("connect") or (_ for _ in ()).throw(AssertionError),
    )
    monkeypatch.setattr(collector, "load_config", lambda path: config(symbols=["AAPL", "MSFT"]))
    assert collector.main(["--check"]) == 0
    assert calls == []


def test_without_a_dsn_the_collector_refuses_rather_than_capturing_to_nowhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(collector, "is_configured", lambda: False)
    monkeypatch.setattr(collector, "load_config", lambda path: config())
    with pytest.raises(SystemExit, match="TRADING_PLATFORM_DSN is unset"):
        collector.main([])


# -- what the adversarial review of 2026-09-04 found ---------------------------------------


def test_a_market_the_store_does_not_know_is_refused_by_check() -> None:
    """The P0. `market = "stocks"` shipped in the example config and is not one
    of the store's markets, so every flush raised, both sources dropped 100% of
    their bars, both ingest runs closed `ok` with `rows_written = 0`, and
    `--check` passed. The collector ran all day and wrote nothing.

    Mutation: delete the `market not in MARKETS` block from `validate()` — red.
    The message must also say where 'stocks' does belong, or the next person
    makes the same mistake."""
    with pytest.raises(SystemExit) as raised:
        collector.validate({**config(), "market": "stocks"})
    assert "not one of the store's markets" in str(raised.value)
    assert "asset_class" in str(raised.value)

    with pytest.raises(SystemExit, match="not one of the store's markets"):
        collector.validate({k: v for k, v in config().items() if k != "market"})


def test_an_interval_nothing_can_parse_is_refused_by_check() -> None:
    with pytest.raises(SystemExit, match="interval"):
        collector.validate({**config(), "interval": "7m"})


def test_a_source_with_no_venue_is_refused() -> None:
    """`venue` is the TAPE, not the vendor. Stamping it with the vendor made
    every Alpaca-vs-Polygon disagreement read as basis — every error a shrug,
    which is the exact failure the reconciler says it refuses.

    Mutation: default `venue` to the vendor in `validate()` — red, and the
    reconciler silently stops finding errors."""
    with pytest.raises(SystemExit, match="declares no venue"):
        collector.validate(config(venue=None))


def test_a_venue_that_differs_only_in_case_or_padding_is_refused() -> None:
    """`nasdaq`, `Nasdaq` and `nasdaq ` would be three different venues, so two
    sources on one tape would compare as cross-venue and their errors would be
    filed as basis.

    Mutation: drop the normalisation check — red."""
    for bad in ("Nasdaq", "nasdaq ", " nasdaq"):
        with pytest.raises(SystemExit, match="lower case"):
            collector.validate(config(venue=bad))


def test_the_vendor_is_what_selects_the_client() -> None:
    _spec, client = collector.validate(config(vendor="polygon", venue="sip"))[0]
    assert client.venue == "polygon"
    with pytest.raises(SystemExit, match="vendor must be one of"):
        collector.validate(config(vendor="not-a-vendor"))


# -- the tape is not a matter of opinion (T-c9da9b75) --------------------------------------


def test_the_free_iex_feed_may_not_call_itself_the_consolidated_tape() -> None:
    """Alpaca's free tier serves IEX alone. Labelling it `sip` makes every
    difference against a genuinely consolidated source a SAME-venue
    disagreement, which the reconciler reports as an error rate and the
    operations page alarms on — so the multi-source check measures the
    IEX-to-consolidated spread and calls it bad prints. The example config said
    so in a comment, and a comment does not run.

    Mutation: drop the `_refuse_a_mislabelled_tape` call from `validate`, or
    return early when `expected != venue` — this goes green and the whole
    multi-source validation quietly stops meaning anything."""
    with pytest.raises(collector.ConfigError, match="is the 'iex' tape"):
        collector.validate(config(feed="iex", venue="sip"))


def test_the_honest_labels_are_both_accepted() -> None:
    """The control. Without it the rule above could be "refuse everything",
    which would pass that test and break every real configuration.

    Mutation: refuse unconditionally — red here."""
    assert collector.validate(config(feed="iex", venue="iex"))
    assert collector.validate(config(feed="sip", venue="sip"))


def test_a_vendor_whose_tiers_are_not_known_is_left_alone() -> None:
    """This refuses only what it actually knows. Guessing a tape for a vendor
    nobody has checked would be the same mislabelling in the other direction.

    Mutation: make the lookup default to the feed name instead of None —
    Polygon's `feed` would then have to equal its venue and this goes red."""
    assert collector.validate(config(vendor="polygon", feed="delayed", venue="sip"))
