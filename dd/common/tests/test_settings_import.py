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

"""Loading the legacy env vars into the DB-backed settings, at cutover.

This code runs once, against production, with the old database already backed up and
both bots down — which is exactly why it is tested. A wrong colour or a channel written
to the wrong slug would be discovered by a feed posting nowhere, hours later, with the
env vars it came from already deleted.
"""

import typing as t

import pytest

from dd.common import (
    feeds as dd_feeds,
    schemas,
    settings,
    settings_import,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

#: The shape prod's environment actually has, including the three dead FOLLOWABLES keys
#: (`prime`, `nwid`, `daily_reset`) that nothing has read since those feeds retired.
_PROD_ENV = {
    "ALERTS_CHANNEL_ID": "1000000000000000000",
    "ALERT_MIN_LEVEL": "WARNING",
    "EMBED_DEFAULT_COLOR": "0xEC42A5",
    "EMBED_ERROR_COLOR": "EF323F",
    "DEFAULT_URL": "https://kyberscorner.com/",
    "DISABLE_BAD_CHANNELS": "True",
    "LOST_SECTOR_GIF_URL": "https://example.com/ls.gif",
    "XUR_IMAGE_URL": "https://example.com/xur.png",
    "FOLLOWABLES": (
        '{"ada": 1, "twab": 2, "prime": 3, "nwid": 4, "lost_sector": 5,'
        ' "daily_reset": 6, "eververse": 7, "weekly_reset": 8, "trials": 9,'
        ' "xur": 10, "portal_ops": 0, "iron_banner": 11, "free_games": 12,'
        ' "weekly_nightfall": 13, "emblems_and_cosmetics": 14}'
    ),
}


@pytest.fixture
def _prod_env(monkeypatch: pytest.MonkeyPatch) -> t.Iterator[None]:
    for key, value in _PROD_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("LOG_CHANNEL_ID", raising=False)
    yield


async def _import(*, overwrite: bool = False) -> list[settings_import.Change]:
    rows = await schemas.AutoPostSettings.get_all_rows()
    changes = await settings_import.collect(rows, overwrite=overwrite)
    await settings_import.apply(changes)
    settings.reset_cache_for_tests()
    return changes


async def test_every_setting_reads_back_as_the_env_had_it(_prod_env: None) -> None:
    await _import()

    assert await settings.get_alerts_channel_id() == 1000000000000000000
    assert await settings.get_alert_min_level() == "WARNING"
    assert int(await settings.get_embed_default_color()) == 0xEC42A5
    assert int(await settings.get_embed_error_color()) == 0xEF323F
    assert await settings.get_default_url() == "https://kyberscorner.com/"
    assert await settings.get_disable_bad_channels() is True
    assert await settings.get_lost_sector_image_url() == "https://example.com/ls.gif"
    assert await settings.get_xur_image_url() == "https://example.com/xur.png"


async def test_every_catalog_feed_gets_its_channel(_prod_env: None) -> None:
    # Read back through get_followable_channel rather than by row name: that is the
    # function every producer and reader actually calls, so it proves the slug the
    # importer wrote is the slug the bots look under.
    await _import()

    assert await settings.get_followable_channel("xur") == 10
    assert await settings.get_followable_channel("weekly_nightfall") == 13
    assert await settings.get_followable_channel("emblems_and_cosmetics") == 14
    # 0 is a legitimate configured value — portal_ops shipped dormant.
    assert await settings.get_followable_channel("portal_ops") == 0

    # Every catalog feed ends up with a row — including the ones whose value is 0. A
    # missing row and a 0 row read the same through the getter but not on the page,
    # where one shows "none configured" and the other shows the channel you picked.
    rows = await schemas.AutoPostSettings.get_all_rows()
    assert {f.channel_key for f in dd_feeds.FOLLOWABLES} <= set(rows)


async def test_retired_followables_are_reported_not_written(_prod_env: None) -> None:
    # prime/nwid/daily_reset are feeds that no longer exist. Dropping them silently and
    # dropping a *renamed* feed's channel look identical in a log that says nothing, so
    # they are surfaced as skipped rows naming themselves.
    changes = await _import()

    retired = {c.slug: c for c in changes if c.skip == "not a feed in dd.common.feeds"}
    assert set(retired) == {"prime_channel", "nwid_channel", "daily_reset_channel"}
    rows = await schemas.AutoPostSettings.get_all_rows()
    assert not [name for name in rows if name.startswith(("prime", "nwid", "daily"))]


async def test_a_feed_missing_from_the_env_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The failure that would otherwise be found by a feed going quiet: the catalog grew
    # a followable the deployment's FOLLOWABLES never had.
    monkeypatch.setenv("FOLLOWABLES", '{"xur": 10}')
    rows = await schemas.AutoPostSettings.get_all_rows()
    changes = await settings_import.collect(rows, overwrite=False)

    missing = {c.slug for c in changes if c.skip == "absent from FOLLOWABLES"}
    assert "free_games_channel" in missing
    assert "xur_channel" not in missing


async def test_a_second_run_writes_nothing(_prod_env: None) -> None:
    # The cutover is not a single clean shot — a step gets re-run after a hiccup. A
    # re-run must be a no-op, including for a channel legitimately stored as 0.
    await _import()

    rows = await schemas.AutoPostSettings.get_all_rows()
    again = await settings_import.collect(rows, overwrite=False)

    assert [c.slug for c in again if c.writes] == []


async def test_a_value_edited_on_the_page_is_kept(_prod_env: None) -> None:
    # Once anchor is up, the settings page is the source of truth. Re-running the
    # importer must not quietly roll an operator's correction back to the env's value.
    await _import()
    await schemas.AutoPostSettings.set_value("xur_channel", "999")
    settings.reset_cache_for_tests()

    await _import()

    assert await settings.get_followable_channel("xur") == 999


async def test_overwrite_puts_the_env_value_back(_prod_env: None) -> None:
    await _import()
    await schemas.AutoPostSettings.set_value("xur_channel", "999")

    await _import(overwrite=True)

    assert await settings.get_followable_channel("xur") == 10


async def test_a_feed_toggle_survives_its_channel_being_written(
    _prod_env: None,
) -> None:
    # The toggles are already auto_post_settings rows and arrive with the data copy, on
    # the `enabled` column of the same table. Writing a channel must use a value-only
    # upsert, or the import would silently switch every feed off.
    await schemas.AutoPostSettings.set_enabled("lost_sector", True)
    await schemas.AutoPostSettings.set_enabled("xur", False)

    await _import()

    assert await schemas.AutoPostSettings.get_enabled("lost_sector") is True
    assert await schemas.AutoPostSettings.get_enabled("xur") is False
    assert await settings.get_followable_channel("lost_sector") == 5


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0xEC42A5", "#EC42A5"),
        ("EC42A5", "#EC42A5"),
        ("#EC42A5", "#EC42A5"),
        ("0", "#000000"),
        ("ec42a5", "#EC42A5"),
    ],
)
async def test_colour_spellings_normalise_to_the_pages_format(
    raw: str, expected: str
) -> None:
    # `async` with nothing awaited: the module carries a blanket asyncio mark (one mode
    # per file, as everywhere else here), and a sync test under it only earns a warning.
    assert settings_import._normalise_color(raw) == expected


@pytest.mark.parametrize("raw", ["", "not-a-colour", "#GGGGGG", "0x1000000"])
async def test_a_colour_that_would_render_black_raises(raw: str) -> None:
    # _parse_color falls back to black on anything malformed. Writing that value and
    # discovering it as a black embed weeks later is the failure this avoids: the
    # import stops instead, while the env var it came from still exists.
    with pytest.raises(settings_import.SettingsImportError):
        settings_import._normalise_color(raw)


async def test_a_malformed_followables_blob_stops_the_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOLLOWABLES", "{not json")
    rows = await schemas.AutoPostSettings.get_all_rows()

    with pytest.raises(settings_import.SettingsImportError):
        await settings_import.collect(rows, overwrite=False)


async def test_nothing_in_the_environment_yields_no_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The likely operator error: running it without `railway run`, so none of the old
    # vars are present. It must report that rather than "0 settings to write, done".
    for key in (*_PROD_ENV, "LOG_CHANNEL_ID"):
        monkeypatch.delenv(key, raising=False)
    rows = await schemas.AutoPostSettings.get_all_rows()

    changes = await settings_import.collect(rows, overwrite=False)

    assert [c for c in changes if c.writes] == []


async def test_the_report_names_the_dropped_log_channel(
    _prod_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # LOG_CHANNEL_ID has no destination — Discord log forwarding was removed. Saying so
    # is the difference between "migrated" and "we forgot one".
    monkeypatch.setenv("LOG_CHANNEL_ID", "123")
    rows = await schemas.AutoPostSettings.get_all_rows()
    changes = await settings_import.collect(rows, overwrite=False)

    report = settings_import.format_report(changes, execute=False)

    assert "LOG_CHANNEL_ID" in report
    assert "no longer has a setting" in report
