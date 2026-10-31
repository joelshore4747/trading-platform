"""The Polygon download script.

What is worth testing here is not "it fetches" — `live/polygon.py` is tested for
that, and was verified against a real response on 2026-09-08. It is the three
things this script exists to get right, each of which was measured against a
real free-tier key and each of which produces a misleading failure if wrong:

* it must not ask for data the tier refuses, because a 403 reads as a bad key;
* it must not exceed the rate limit, because a retry storm gets a key banned;
* it must not guess between `equity` and `etf`, because SPY is a fund and AAPL
  is not, and a universe built by scanning the cache would silently mix them.

Every test here is network-free: the transport is a stub, so a failure means the
script is wrong rather than that a venue was unreachable.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load() -> Any:
    spec = importlib.util.spec_from_file_location(
        "fetch_polygon_under_test", ROOT / "scripts" / "fetch_polygon.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fp = load()


def _payload(n: int, start_ms: int, step_ms: int = 86_400_000) -> bytes:
    """A Polygon aggregates response with `n` daily bars, shaped as the wire is."""
    return json.dumps(
        {
            "ticker": "AAPL",
            "status": "OK",
            "resultsCount": n,
            "results": [
                {
                    "t": start_ms + i * step_ms,
                    "o": 100.0 + i,
                    "h": 101.0 + i,
                    "l": 99.0 + i,
                    "c": 100.5 + i,
                    "v": 1000.0 + i,
                }
                for i in range(n)
            ],
        }
    ).encode()


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLYGON_API_KEY", "test-key-not-a-real-one")


# -- It must not guess between equity and etf ----------------------------------


def test_market_is_required_and_not_inferred(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        fp.main(["SPY", "--start", "2026-06-01"])
    assert "--market" in capsys.readouterr().err


def test_only_equity_and_etf_are_accepted(capsys: pytest.CaptureFixture[str]) -> None:
    """`spot` is a real key in cache.MARKETS, so this is a typo the cache would
    otherwise accept — filing US stocks beside BTCUSDT."""
    with pytest.raises(SystemExit):
        fp.main(["AAPL", "--market", "spot", "--start", "2026-06-01"])
    assert "--market" in capsys.readouterr().err


def test_the_two_markets_write_to_different_paths(tmp_path: Path) -> None:
    calls: list[str] = []

    def transport(url: str, headers: Mapping[str, str]) -> bytes:
        calls.append(url)
        return _payload(2, int(datetime(2026, 6, 1, tzinfo=UTC).timestamp() * 1000))

    for market, symbol in (("equity", "AAPL"), ("etf", "SPY")):
        fp.urllib_transport = lambda **_: transport  # type: ignore[assignment]
        fp.main(
            [
                symbol,
                "--market",
                market,
                "--interval",
                "1d",
                "--start",
                "2026-06-01",
                "--end",
                "2026-06-10",
                "--cache",
                str(tmp_path),
            ]
        )
    assert (tmp_path / "equity" / "AAPL" / "1d.parquet").exists()
    assert (tmp_path / "etf" / "SPY" / "1d.parquet").exists()


# -- It must not ask for data the tier refuses ---------------------------------


#: A floor written down here rather than read from the module under test. The
#: first version of the test below asserted against `fp.RECENCY_GUARD_MIN`
#: itself, so setting that constant to 0 left the assertion trivially true and
#: the mutation survived -- a test that cannot fail, of exactly the kind this
#: repository keeps finding. The number a human decided is the number to assert.
MINIMUM_ACCEPTABLE_LAG_MIN = 10


def test_end_defaults_to_before_the_recency_cutoff(capsys: pytest.CaptureFixture[str]) -> None:
    """Left at `now`, every request 403s and reads as a broken key."""
    fp.urllib_transport = lambda **_: lambda url, headers: _payload(1, 0)  # type: ignore[assignment]
    fp.main(["AAPL", "--market", "equity", "--start", "2026-06-01", "--dry-run"])
    out = capsys.readouterr()
    url = next(line for line in out.out.splitlines() if line.startswith("would GET"))
    # .../range/1/day/<start_ms>/<end_ms>?adjusted=false&...
    end_ms = int(url.split("?")[0].rsplit("/", 1)[1])
    end = datetime.fromtimestamp(end_ms / 1000, UTC)
    lag_minutes = (datetime.now(UTC) - end).total_seconds() / 60
    assert lag_minutes >= MINIMUM_ACCEPTABLE_LAG_MIN, (
        f"--end defaulted to {lag_minutes:.1f} min ago; the free tier 403s inside "
        f"{MINIMUM_ACCEPTABLE_LAG_MIN} min and that failure reads as a bad key"
    )
    assert "403" in out.err


def test_an_end_inside_the_cutoff_is_warned_about(capsys: pytest.CaptureFixture[str]) -> None:
    fp.urllib_transport = lambda **_: lambda url, headers: _payload(1, 0)  # type: ignore[assignment]
    recent = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    fp.main(["AAPL", "--market", "equity", "--start", "2026-06-01", "--end", recent, "--dry-run"])
    assert "403" in capsys.readouterr().err


def test_dry_run_touches_no_network(capsys: pytest.CaptureFixture[str]) -> None:
    def explode(url: str, headers: Mapping[str, str]) -> bytes:
        raise AssertionError("--dry-run made a request")

    fp.urllib_transport = lambda **_: explode  # type: ignore[assignment]
    assert fp.main(["AAPL", "--market", "equity", "--start", "2026-06-01", "--dry-run"]) == 0
    assert "no network touched" in capsys.readouterr().out


def test_end_before_start_is_refused() -> None:
    with pytest.raises(SystemExit, match="before"):
        fp.main(["AAPL", "--market", "equity", "--start", "2026-09-01", "--end", "2026-08-01"])


# -- It must not exceed the rate limit -----------------------------------------


def test_requests_are_paced_rather_than_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gap between consecutive requests is the free tier's whole budget.

    Asserted on the sleep the script asks for, not on wall-clock time, so this
    stays deterministic and fast.
    """
    slept: list[float] = []
    monkeypatch.setattr(fp.time, "sleep", lambda s: slept.append(s))
    now = [0.0]
    monkeypatch.setattr(fp.time, "monotonic", lambda: now[0])

    calls: list[float] = []
    fp._paced(calls)
    assert slept == [], "nothing to pace before the first request"

    calls.append(0.0)
    now[0] = 1.0  # only a second has passed
    fp._paced(calls)
    assert slept and slept[0] == pytest.approx(fp.MIN_REQUEST_INTERVAL_S - 1.0)

    slept.clear()
    now[0] = 1000.0  # plenty of time has passed
    fp._paced(calls)
    assert slept == [], "no sleep when the budget has already recovered"


def test_the_pacing_interval_respects_five_requests_a_minute() -> None:
    """Measured against a real free-tier key: 7 quick requests -> 429."""
    assert fp.MIN_REQUEST_INTERVAL_S >= 12.0, "5 requests/minute is one every 12s"


# -- Failures must say which failure they are ----------------------------------


@pytest.mark.parametrize(
    ("status", "must_say"),
    [(403, "not a bad key"), (429, "rate limit")],
)
def test_a_refusal_explains_itself(
    status: int, must_say: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """403 and 429 both look like "it is broken" and mean different things."""

    def refuse(url: str, headers: Mapping[str, str]) -> bytes:
        raise fp.TransportError(f"HTTP {status} from {url}", status=status)

    fp.urllib_transport = lambda **_: refuse  # type: ignore[assignment]
    assert (
        fp.main(["AAPL", "--market", "equity", "--start", "2026-06-01", "--end", "2026-06-10"]) == 1
    )
    assert must_say in capsys.readouterr().err


def test_an_empty_window_names_the_likely_reason(capsys: pytest.CaptureFixture[str]) -> None:
    fp.urllib_transport = lambda **_: (  # type: ignore[assignment]
        lambda url, headers: json.dumps({"ticker": "AAPL", "status": "OK", "results": []}).encode()
    )
    assert (
        fp.main(["AAPL", "--market", "equity", "--start", "2026-09-05", "--end", "2026-09-07"]) == 1
    )
    assert "holiday" in capsys.readouterr().err


# -- It must not accept a silently shortened window ----------------------------
#
# The sharpest failure this script can have, because it succeeds: HTTP 200, no
# warning, and years of requested history quietly missing. A walk-forward then
# scores folds over a dataset nobody named. T-594716a0.


def _payload_starting(days_late: int) -> bytes:
    """A response whose first bar trails the request by `days_late` days."""
    start = datetime(2021, 1, 1, tzinfo=UTC) + timedelta(days=days_late)
    return _payload(5, int(start.timestamp() * 1000))


def test_a_silently_truncated_window_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fp.urllib_transport = lambda **_: lambda url, headers: _payload_starting(1348)  # type: ignore[assignment]
    code = fp.main(
        [
            "AAPL",
            "--market",
            "equity",
            "--start",
            "2021-01-01",
            "--end",
            "2026-09-04",
            "--cache",
            str(tmp_path),
        ]
    )
    err = capsys.readouterr().err
    assert code == 2, "a truncated window must not exit 0"
    assert "REFUSED" in err
    # The exact figure is one more than the payload offset because bars are
    # close-stamped (t + timeframe). Asserting the magnitude rather than the
    # literal keeps this about truncation instead of about stamping.
    shortfall = int(re.search(r"(\d+) days short", err).group(1))  # type: ignore[union-attr]
    assert shortfall > 1000, f"expected years of shortfall, reported {shortfall} days"
    assert not list(tmp_path.rglob("*.parquet")), "nothing may be written on a refusal"


def test_allow_short_writes_it_anyway(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The escape hatch exists so the shorter window is a decision, not a default."""
    fp.urllib_transport = lambda **_: lambda url, headers: _payload_starting(1348)  # type: ignore[assignment]
    code = fp.main(
        [
            "AAPL",
            "--market",
            "equity",
            "--start",
            "2021-01-01",
            "--end",
            "2026-09-04",
            "--cache",
            str(tmp_path),
            "--allow-short",
        ]
    )
    assert code == 0
    assert "--allow-short given" in capsys.readouterr().err
    assert (tmp_path / "equity" / "AAPL" / "1d.parquet").exists()


def test_a_long_weekend_does_not_trip_the_guard(tmp_path: Path) -> None:
    """A first bar trailing by a few days is the market being shut, not truncation.

    The grace must sit above the longest ordinary closure and far below a plan
    limit, which costs years. A guard that fires on every Easter is a guard
    somebody passes --allow-short to by reflex, and then it protects nothing.
    """
    fp.urllib_transport = lambda **_: lambda url, headers: _payload_starting(4)  # type: ignore[assignment]
    code = fp.main(
        [
            "AAPL",
            "--market",
            "equity",
            "--start",
            "2021-01-01",
            "--end",
            "2026-09-04",
            "--cache",
            str(tmp_path),
        ]
    )
    assert code == 0
    assert (tmp_path / "equity" / "AAPL" / "1d.parquet").exists()


def test_the_grace_is_wider_than_any_real_market_closure() -> None:
    """Longest US market closure in living memory was 6 days, September 2001."""
    assert 6 < fp.TRUNCATION_GRACE_DAYS < 60
