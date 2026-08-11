# Copyright © 2019-present gsfernandes81

# This file is part of "dd" henceforth referred to as "destiny-director".

# destiny-director is free software: you can redistribute it and/or modify it under the
# terms of the GNU Affero General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later version.

# "destiny-director" is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
# PARTICULAR PURPOSE. See the GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License along with
# destiny-director. If not, see <https://www.gnu.org/licenses/>.

"""Smoke tests: every bot extension imports and loads cleanly.

A restructure (e.g. the v2→v3 migration) most often breaks by leaving a dangling
import or a bad command registration in one extension. ``load_extensions_strict`` only
*logs* such a failure at CRITICAL and keeps the rest of the bot running, so a broken
extension vanishes silently at startup with nothing to catch it.

These tests make that loud and automated:

- import every extension module and assert it exposes an ``lb.Loader`` (catches dangling
  imports), and
- run each package through the real ``load_extensions_strict`` against a never-started
  bot, asserting it logs no CRITICAL and raises nothing (catches registration errors
  such as duplicate command names).

No DB or network — they run in the default suite, pre-commit, and CI.
"""

import base64
import importlib
import logging
import pkgutil
from types import ModuleType

import hikari as h
import lightbulb as lb
import pytest

import dd.anchor.extensions
import dd.beacon.extensions
from dd.common import cfg
from dd.common.extension_loader import load_extensions_strict

_PACKAGES: list[ModuleType] = [dd.beacon.extensions, dd.anchor.extensions]

# hikari extracts the bot id from the token's first segment (base64 of a snowflake) at
# GatewayBot construction, so a dummy token must be shaped like that. The bot is never
# started, so the value is otherwise irrelevant.
_FAKE_TOKEN = base64.b64encode(b"123456789012345678").decode() + ".aaaaaa.bbbbbbbbbbbb"


def _extension_module_names(package: ModuleType) -> list[str]:
    """Immediate children of ``package`` (skipping ``_``-prefixed), like the loader."""
    return [
        f"{package.__name__}.{name}"
        for _finder, name, _is_pkg in pkgutil.iter_modules(package.__path__)
        if not name.startswith("_")
    ]


@pytest.mark.parametrize(
    "module_name",
    [name for package in _PACKAGES for name in _extension_module_names(package)],
)
def test_extension_imports_and_exposes_loader(module_name: str) -> None:
    module = importlib.import_module(module_name)
    loader = getattr(module, "loader", None)
    assert loader is not None, f"{module_name} exposes no `loader`"
    assert isinstance(loader, lb.Loader)


@pytest.mark.asyncio
@pytest.mark.parametrize("package", _PACKAGES, ids=lambda p: p.__name__)
async def test_extension_package_loads_cleanly(
    package: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Force the test-env-gated extensions (e.g. beacon's `testing`) to load too, so the
    # smoke test covers them as well.
    monkeypatch.setattr(cfg, "test_env", (123,))

    # A bot that is never started — load_extensions only registers the command tree
    # locally, so no gateway connection or Discord call happens here.
    bot = h.GatewayBot(token=_FAKE_TOKEN)  # never started; no gateway/Discord call
    client = lb.client_from_app(bot)

    with caplog.at_level(logging.CRITICAL, logger="dd.common.extension_loader"):
        await load_extensions_strict(client, package)

    critical = [
        record for record in caplog.records if record.levelno >= logging.CRITICAL
    ]
    assert not critical, "extension(s) failed to load: " + "; ".join(
        record.getMessage() for record in critical
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("package", _PACKAGES, ids=lambda p: p.__name__)
async def test_testing_extension_does_not_load_outside_a_test_env(
    package: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both bots' `testing` extensions are debug aids — gated on ``cfg.test_env``.

    Without ``TEST_ENV`` set they must register nothing at all, so `/testing` never
    reaches a production deployment (the control/kyber guild scopes notwithstanding).
    """
    monkeypatch.setattr(cfg, "test_env", ())

    bot = h.GatewayBot(token=_FAKE_TOKEN)  # never started; no gateway/Discord call
    client = lb.client_from_app(bot)

    await load_extensions_strict(client, package)

    registered = {
        getattr(command, "name", None) for command in client._registered_commands
    }
    assert "testing" not in registered
