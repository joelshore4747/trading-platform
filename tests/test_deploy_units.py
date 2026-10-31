"""The systemd units, rendered and then read back.

These files were written, reviewed, documented in two places, and never once
executed. They shipped with four defects, and every one of them is the kind a
human reading the file does not see because the file looks right:

1. Two incompatible layouts. The collector and reconciler were system units at
   /opt with `User=trading`; the health check was a user unit using `%h`.
   docs/SETUP.md said to `sudo cp` all three into /etc/systemd/system, where
   `%h` is root's home, while check_health.py's own docstring said
   `systemctl --user`.
2. trading-reconcile.service had no EnvironmentFile, no Environment=DSN and no
   comment pointing at `systemctl edit`, while run_reconcile.py requires
   TRADING_PLATFORM_DSN. It would have failed on every hourly fire.
3. OnFailure pointed at status-email@%n.service, which this repository does not
   contain — so the health check would run, fail, and tell nobody, which is the
   state its own comment says it replaces.
4. The units read /etc/trading_platform/collector.toml; both documents told you
   to create ./collector.toml in the repository root.

Each test below fails on one of those. That is the point of the file: the units
are now generated, and a generator is only trustworthy if something reads its
output back.
"""

from __future__ import annotations

import configparser
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "deploy" / "install.sh"

#: Units that run continuously or on a timer, as opposed to the alert template
#: which is only ever started by another unit's OnFailure.
LONG_RUNNING = ("trading-collector.service", "trading-reconcile.service", "trading-health.service")

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or not INSTALL.exists(),
    reason="needs bash and deploy/install.sh",
)


def render(layout: str) -> tuple[dict[str, configparser.ConfigParser], dict[str, str]]:
    """Run the installer's dry run and read back what it staged.

    Dry run on purpose: it writes only into its own mktemp directory, needs no
    sudo, and enables nothing. --prefix is this checkout for both layouts so the
    system layout can be rendered on a machine with no /opt.
    """
    proc = subprocess.run(
        # --python is the interpreter running this test, not <prefix>/.venv.
        # The mutation eval copies the tree WITHOUT its venv, and pinning the
        # interpreter to the prefix made this test fail on the eval's control
        # run — which aborts the whole eval, so a guard added here would have
        # silently switched off the harness that measures every other guard.
        [
            "bash",
            str(INSTALL),
            "--layout",
            layout,
            "--prefix",
            str(ROOT),
            "--python",
            sys.executable,
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert proc.returncode == 0, f"dry run failed:\n{proc.stdout}\n{proc.stderr}"

    match = re.search(r"rendered .* into (/\S+)", proc.stdout)
    assert match, f"no stage directory in output:\n{proc.stdout}"
    stage = Path(match.group(1))

    settings: dict[str, str] = {}
    for key in ("collector config", "environment file", "unit directory", "checkout"):
        found = re.search(rf"^\s*{re.escape(key)}\s+(\S+)", proc.stdout, re.MULTILINE)
        assert found, f"{key!r} not reported in the plan"
        settings[key] = found.group(1)

    units: dict[str, configparser.ConfigParser] = {}
    for path in sorted(stage.glob("*")):
        # strict=False: systemd permits a key to repeat (ExecStartPre,
        # Environment), and configparser refuses that by default.
        # interpolation=None: systemd's specifiers (%i, %n, %h) are exactly the
        # syntax configparser reserves for its own substitution, and %h is what
        # one of these tests is here to find.
        parser = configparser.ConfigParser(strict=False, allow_no_value=True, interpolation=None)
        parser.optionxform = str  # type: ignore[method-assign,assignment]
        parser.read_string(path.read_text())
        units[path.name] = parser
    assert units, "the installer staged nothing"
    return units, settings


@pytest.fixture(scope="module")
def user() -> tuple[dict[str, configparser.ConfigParser], dict[str, str]]:
    return render("user")


@pytest.fixture(scope="module")
def system() -> tuple[dict[str, configparser.ConfigParser], dict[str, str]]:
    return render("system")


def exec_lines(unit: configparser.ConfigParser) -> list[str]:
    out = []
    for key in ("ExecStart", "ExecStartPre"):
        if unit.has_option("Service", key):
            out.append(unit.get("Service", key))
    return out


# -- Defect 1: two layouts that could not both be right -------------------------


@pytest.mark.parametrize("layout", ["user", "system"])
def test_no_unit_relies_on_a_systemd_specifier_for_its_location(layout: str) -> None:
    """%h is the whole of defect 1.

    In a --user unit it is your home; in a system unit under
    /etc/systemd/system it is root's. One file cannot be correct in both
    places, and the installer resolves the path instead of deferring it.
    """
    units, _ = render(layout)
    for name, unit in units.items():
        for section in unit.sections():
            for key, value in unit.items(section):
                assert "%h" not in value, f"{name}: {key}={value} defers its path to systemd"


def test_the_system_layout_runs_as_a_service_account_and_the_user_layout_does_not(
    user: tuple[dict[str, configparser.ConfigParser], dict[str, str]],
    system: tuple[dict[str, configparser.ConfigParser], dict[str, str]],
) -> None:
    system_units, _ = system
    user_units, _ = user
    for name in LONG_RUNNING:
        assert system_units[name].has_option("Service", "User"), f"{name} has no User= under system"
        assert not user_units[name].has_option("Service", "User"), (
            f"{name} sets User= under the user layout, where systemd --user refuses it"
        )


def test_protect_home_is_off_exactly_where_the_checkout_lives_in_home(
    user: tuple[dict[str, configparser.ConfigParser], dict[str, str]],
    system: tuple[dict[str, configparser.ConfigParser], dict[str, str]],
) -> None:
    """ProtectHome=true under the user layout would hide the checkout, the
    config and the environment file — every path the unit needs."""
    system_units, _ = system
    user_units, _ = user
    assert system_units["trading-collector.service"].get("Service", "ProtectHome") == "true"
    assert not user_units["trading-collector.service"].has_option("Service", "ProtectHome")


# -- Defect 2: a unit that needed the DSN and had no way to get it --------------


@pytest.mark.parametrize("layout", ["user", "system"])
def test_every_unit_that_touches_the_store_can_reach_a_dsn(layout: str) -> None:
    """Derived from the scripts, not from a list kept by hand.

    A hand-maintained list is the defect T-5d21db5f already records elsewhere in
    this repository: a new store-backed script added to the units and not to the
    list is exactly the case the test would need to catch.
    """
    units, _ = render(layout)
    for name in LONG_RUNNING:
        unit = units[name]
        scripts = [
            ROOT / token
            for line in exec_lines(unit)
            for token in line.split()
            if token.endswith(".py")
        ]
        assert scripts, f"{name} runs no script"
        needs_store = any("trading_platform.store" in s.read_text() for s in scripts if s.exists())
        if needs_store:
            assert unit.has_option("Service", "EnvironmentFile") or any(
                "TRADING_PLATFORM_DSN" in v for _, v in unit.items("Service")
            ), f"{name} runs a store-backed script with no EnvironmentFile and no DSN"


# -- Defect 3: OnFailure pointing at a unit nobody ships ------------------------


@pytest.mark.parametrize("layout", ["user", "system"])
def test_every_on_failure_target_is_a_unit_this_repository_ships(layout: str) -> None:
    units, _ = render(layout)
    shipped = {p.name.removesuffix(".in") for p in (ROOT / "deploy").glob("*.service*")}
    targets = [
        unit.get("Unit", "OnFailure")
        for unit in units.values()
        if unit.has_option("Unit", "OnFailure")
    ]
    assert targets, "nothing declares OnFailure, so a failure tells nobody"
    for target in targets:
        # trading-alert@%n.service -> trading-alert@.service, the template.
        template = re.sub(r"@[^.]*\.service$", "@.service", target)
        assert template in shipped, (
            f"OnFailure={target} resolves to {template}, which deploy/ does not contain"
        )


def test_the_alert_target_actually_exists_and_runs_something_executable() -> None:
    alert = ROOT / "deploy" / "alert.sh"
    assert alert.exists(), "deploy/alert.sh is missing, so OnFailure fires into nothing"
    import os

    assert os.access(alert, os.X_OK), "deploy/alert.sh is not executable"


# -- Defect 4: the units read one config path, the docs created another ---------


@pytest.mark.parametrize("layout", ["user", "system"])
def test_the_collector_reads_the_config_the_installer_creates(layout: str) -> None:
    units, settings = render(layout)
    configured = settings["collector config"]
    for line in exec_lines(units["trading-collector.service"]):
        if "--config" in line:
            tokens = line.split()
            value = tokens[tokens.index("--config") + 1]
            assert value == configured, (
                f"unit reads {value}, installer creates {configured} — "
                "the mismatch defect 4 shipped with"
            )


# -- The generator's own output ------------------------------------------------


@pytest.mark.parametrize("layout", ["user", "system"])
def test_nothing_rendered_still_contains_a_placeholder(layout: str) -> None:
    units, _ = render(layout)
    for name, unit in units.items():
        for section in unit.sections():
            for key, value in unit.items(section):
                assert not re.search(r"@[A-Z_]+@", value), f"{name}: {key}={value} unresolved"


@pytest.mark.parametrize("layout", ["user", "system"])
def test_every_executable_a_unit_names_exists(layout: str) -> None:
    units, settings = render(layout)
    prefix = Path(settings["checkout"])
    for name, unit in units.items():
        for line in exec_lines(unit):
            binary = Path(line.split()[0])
            assert binary.is_absolute(), f"{name}: {binary} is relative; systemd requires absolute"
            assert binary.exists(), f"{name}: {binary} does not exist"
            for token in line.split():
                if token.endswith(".py"):
                    assert (prefix / token).exists(), f"{name}: {token} not under {prefix}"
