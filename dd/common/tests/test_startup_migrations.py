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

"""``db_migrations.run_migrations``: the opt-out, the fatality, the config lookup.

``alembic.command.upgrade`` is stubbed throughout — no database is contacted. The
config path resolution *is* exercised for real against this repo's ``alembic.ini``
(see :func:`test_config_resolves_from_the_package_not_the_cwd`), which is the part
that differs between a source checkout and the runtime image.
"""

from pathlib import Path

import pytest
from alembic.config import Config

from dd.common import cfg, db_migrations


@pytest.fixture
def upgrade_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Config, str]]:
    """Record ``alembic upgrade`` invocations instead of running them."""
    calls: list[tuple[Config, str]] = []

    def fake_upgrade(config: Config, revision: str, **_kwargs: object) -> None:
        calls.append((config, revision))

    monkeypatch.setattr(db_migrations.command, "upgrade", fake_upgrade)
    return calls


@pytest.mark.asyncio
async def test_opt_out_skips_the_migration(
    monkeypatch: pytest.MonkeyPatch, upgrade_calls: list[tuple[Config, str]]
) -> None:
    monkeypatch.setattr(cfg, "run_migrations_on_startup", False)
    await db_migrations.run_migrations()
    assert upgrade_calls == []


@pytest.mark.asyncio
async def test_enabled_by_default_upgrades_to_head(
    monkeypatch: pytest.MonkeyPatch, upgrade_calls: list[tuple[Config, str]]
) -> None:
    monkeypatch.setattr(cfg, "run_migrations_on_startup", True)
    await db_migrations.run_migrations()
    assert [revision for _config, revision in upgrade_calls] == ["head"]


@pytest.mark.asyncio
async def test_failure_is_fatal(
    monkeypatch: pytest.MonkeyPatch, upgrade_calls: list[tuple[Config, str]]
) -> None:
    """The bot must not come up against an un-migrated database."""
    monkeypatch.setattr(cfg, "run_migrations_on_startup", True)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("migration exploded")

    monkeypatch.setattr(db_migrations.command, "upgrade", boom)
    with pytest.raises(RuntimeError, match="migration exploded"):
        await db_migrations.run_migrations()


@pytest.mark.asyncio
async def test_alembic_does_not_reconfigure_the_root_logger(
    monkeypatch: pytest.MonkeyPatch, upgrade_calls: list[tuple[Config, str]]
) -> None:
    """env.py's opt-out must be set, or alembic's fileConfig() would drop the root
    logger to WARNING and add a second stderr handler for the rest of the process."""
    monkeypatch.setattr(cfg, "run_migrations_on_startup", True)
    await db_migrations.run_migrations()
    config, _revision = upgrade_calls[0]
    assert config.attributes["configure_logging"] is False


def test_config_resolves_from_the_package_not_the_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The container's WORKDIR is /app but tests and local runs start anywhere, so the
    lookup must not depend on the working directory."""
    monkeypatch.chdir(tmp_path)
    ini = db_migrations._alembic_ini_path()
    assert ini.is_file()
    assert (ini.parent / "migrations").is_dir()


def test_missing_config_raises_with_the_paths_tried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(db_migrations, "_config_candidates", lambda: (tmp_path,))
    with pytest.raises(FileNotFoundError, match=str(tmp_path / "alembic.ini")):
        db_migrations._alembic_ini_path()


def test_ini_without_migrations_dir_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An ini with no script directory would otherwise fail deeper in alembic."""
    (tmp_path / "alembic.ini").write_text("[alembic]\n")
    monkeypatch.setattr(db_migrations, "_config_candidates", lambda: (tmp_path,))
    with pytest.raises(FileNotFoundError):
        db_migrations._alembic_ini_path()


def test_run_migrations_is_only_called_from_the_entry_points() -> None:
    """The suite must never migrate the configured database.

    It cannot today — the only call sites are the two ``__main__`` modules, which no
    test imports (importing one starts a gateway). This pins that down: a call added
    anywhere a fixture might reach would fail here.
    """
    package_root = Path(db_migrations.__file__).resolve().parent.parent
    needle = "await " + "run_migrations()"  # split so this file never matches itself
    callers = {
        path.relative_to(package_root).as_posix()
        for path in package_root.rglob("*.py")
        if "tests" not in path.parts and needle in path.read_text(encoding="utf-8")
    }
    assert callers == {"beacon/__main__.py", "anchor/__main__.py"}
