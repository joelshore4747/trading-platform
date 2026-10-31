"""A strategy version changes when its code or its parameters change, and not otherwise.

Mutations these catch: hashing the class body instead of the file (a helper
edit above the class leaves the version unchanged); dropping params from the
material (fast=10 and fast=12 share a version); a version that accepts a
strategy with no source on disk.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

from trading_platform.core.events import Bar
from trading_platform.strategy.base import Strategy, StrategyContext
from trading_platform.strategy.examples.sma_crossover import SMACrossover
from trading_platform.strategy.versioning import fingerprint


def test_same_code_and_params_give_the_same_version() -> None:
    a = fingerprint(SMACrossover("BTCUSDT", fast=10, slow=30))
    b = fingerprint(SMACrossover("BTCUSDT", fast=10, slow=30))
    assert a == b
    assert a.family == "SMACrossover"
    assert ("fast", "10") in a.params and ("slow", "30") in a.params
    assert a.label == f"SMACrossover@{a.digest}"


def test_a_parameter_change_is_a_new_version() -> None:
    a = fingerprint(SMACrossover("BTCUSDT", fast=10, slow=30))
    b = fingerprint(SMACrossover("BTCUSDT", fast=12, slow=30))
    assert a.source == b.source
    assert a.digest != b.digest


def test_a_source_change_is_a_new_version(tmp_path: Path) -> None:
    """Two copies of one module differing only in a helper above the class."""
    body = """
from trading_platform.strategy.base import Strategy

def helper():
    return {marker}

class Probe(Strategy):
    name = "probe"
    def __init__(self):
        self.window = 5
    def on_bar(self, bar, ctx):
        pass
"""
    versions = []
    for marker in ("1", "2"):
        module_dir = tmp_path / f"m{marker}"
        module_dir.mkdir()
        path = module_dir / "probe_mod.py"
        path.write_text(body.format(marker=marker))
        import importlib.util

        spec = importlib.util.spec_from_file_location(f"probe_mod_{marker}", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        versions.append(fingerprint(module.Probe()))
    assert versions[0].params == versions[1].params == (("window", "5"),)
    assert versions[0].source != versions[1].source
    assert versions[0].digest != versions[1].digest


def test_a_strategy_with_no_source_on_disk_cannot_be_versioned() -> None:
    namespace: dict[str, object] = {}
    exec(
        "from trading_platform.strategy.base import Strategy\n"
        "class Ghost(Strategy):\n"
        "    name = 'ghost'\n"
        "    def on_bar(self, bar, ctx): pass\n",
        namespace,
    )
    ghost = namespace["Ghost"]()  # type: ignore[operator]
    with pytest.raises(ValueError, match="no reproducible version"):
        fingerprint(ghost)


def test_private_state_and_the_name_are_not_parameters() -> None:
    class Counting(Strategy):
        name = "counting"

        def __init__(self) -> None:
            self.window = 5
            self._seen = 0
            self.allocation = Decimal("0.5")

        def on_bar(self, bar: Bar, ctx: StrategyContext) -> None:
            self._seen += 1

    v = fingerprint(Counting())
    assert v.params == (("allocation", "Decimal('0.5')"), ("window", "5"))
