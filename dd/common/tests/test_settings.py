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

"""Tests for dd.common.settings — the DB-backed replacement for the old cfg.py env vars
(embed colors, default_url, followable channel ids, log/alerts channel, alert level,
disable_bad_channels). Uses the ambient SQLite test DB (dd/common/tests/conftest.py)."""

import hikari as h
import pytest
import pytest_asyncio
from sqlalchemy import delete

from dd.common import cfg, schemas, settings

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def _reset_settings_cache():
    """Start each test from an empty ``auto_post_settings`` table (session-scoped DB,
    shared with every other test module — see test_autopost_settings.py's identical
    fixture) and an unloaded settings cache, so tests can't leak into each other."""
    async with schemas.db_session() as session, session.begin():
        await session.execute(delete(schemas.AutoPostSettings))
    settings.invalidate()
    settings._cache.clear()
    yield
    settings.invalidate()
    settings._cache.clear()


# --- colors ----------------------------------------------------------------------


async def test_embed_default_color_defaults_to_brand_pink_when_unset():
    # The hardcoded default (see settings._DEFAULTS) is the Kyber brand pink, not black.
    assert await settings.get_embed_default_color() == h.Color(0xEC42A5)


async def test_embed_default_color_reflects_a_saved_row():
    await schemas.AutoPostSettings.set_value("embed_default_color", "#EC42A5")
    settings.invalidate()
    assert await settings.get_embed_default_color() == h.Color(0xEC42A5)


async def test_embed_default_color_sync_matches_async_after_preload():
    await schemas.AutoPostSettings.set_value("embed_default_color", "#123456")
    await settings.preload()
    assert settings.get_embed_default_color_sync() == h.Color(0x123456)


async def test_malformed_stored_color_falls_back_to_black():
    await schemas.AutoPostSettings.set_value("embed_default_color", "not-a-color")
    settings.invalidate()
    assert await settings.get_embed_default_color() == h.Color(0)


# --- simple url/level/bool/int settings ------------------------------------------


async def test_default_url_defaults_to_empty_string():
    assert await settings.get_default_url() == ""


async def test_default_url_reflects_a_saved_row():
    await schemas.AutoPostSettings.set_value("default_url", "https://example.com")
    settings.invalidate()
    assert await settings.get_default_url() == "https://example.com"


async def test_alert_min_level_defaults_to_error():
    assert await settings.get_alert_min_level() == "ERROR"


async def test_disable_bad_channels_defaults_to_false():
    assert await settings.get_disable_bad_channels() is False


async def test_disable_bad_channels_reflects_a_saved_row():
    await schemas.AutoPostSettings.set_enabled("disable_bad_channels", True)
    settings.invalidate()
    assert await settings.get_disable_bad_channels() is True


async def test_log_and_alerts_channel_id_default_to_zero():
    assert await settings.get_log_channel_id() == 0
    assert await settings.get_alerts_channel_id() == 0


async def test_alerts_channel_id_reflects_a_saved_row():
    await schemas.AutoPostSettings.set_value("alerts_channel_id", "123456")
    settings.invalidate()
    assert await settings.get_alerts_channel_id() == 123456


# --- followables -------------------------------------------------------------------


async def test_followable_channel_falls_back_to_cfg_seed(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(cfg, "followables", {"lost_sector": 42})
    assert await settings.get_followable_channel("lost_sector") == 42


async def test_followable_channel_db_row_overrides_cfg_seed(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(cfg, "followables", {"lost_sector": 42})
    await schemas.AutoPostSettings.set_value("lost_sector_channel", "99")
    settings.invalidate()
    assert await settings.get_followable_channel("lost_sector") == 99


async def test_followable_channel_unknown_feed_is_zero():
    assert await settings.get_followable_channel("not_a_real_feed") == 0


async def test_followable_channel_sync_matches_async_after_preload(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(cfg, "followables", {"xur": 7})
    await settings.preload()
    assert settings.get_followable_channel_sync("xur") == 7


async def test_get_followables_returns_every_slug(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cfg, "followables", {})
    followables = await settings.get_followables()
    assert set(followables) == set(settings.FOLLOWABLE_SLUGS)


# --- followable_name ---------------------------------------------------------------


async def test_followable_name_returns_configured_name(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cfg, "followables", {"lost_sector": 42})
    await settings.preload()
    assert settings.followable_name(id=42) == "lost_sector"


async def test_followable_name_falls_back_to_id(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cfg, "followables", {"lost_sector": 42})
    await settings.preload()
    assert settings.followable_name(id=99) == 99


# --- seed_followables_from_env (the FOLLOWABLES retirement bridge) -----------------


async def test_seed_writes_every_configured_followable(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cfg, "followables", {"lost_sector": 42, "xur": 99})

    written = await settings.seed_followables_from_env()

    assert written == {"lost_sector": 42, "xur": 99}
    assert await schemas.AutoPostSettings.get_value("lost_sector_channel") == "42"
    assert await schemas.AutoPostSettings.get_value("xur_channel") == "99"


async def test_seed_never_overwrites_an_existing_row(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cfg, "followables", {"lost_sector": 42})
    await schemas.AutoPostSettings.set_value("lost_sector_channel", "777")
    settings.invalidate()

    written = await settings.seed_followables_from_env()

    assert written == {}  # nothing written — a row already existed
    assert await schemas.AutoPostSettings.get_value("lost_sector_channel") == "777"


async def test_seed_skips_unset_and_unknown_feeds(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cfg, "followables", {"lost_sector": 0, "not_a_real_feed": 123})

    written = await settings.seed_followables_from_env()

    assert written == {}
    assert await schemas.AutoPostSettings.get_value("lost_sector_channel") is None


async def test_seed_is_idempotent_across_repeated_runs(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(cfg, "followables", {"lost_sector": 42})

    first = await settings.seed_followables_from_env()
    second = await settings.seed_followables_from_env()

    assert first == {"lost_sector": 42}
    assert second == {}
    assert await schemas.AutoPostSettings.get_value("lost_sector_channel") == "42"


async def test_seed_result_is_live_immediately(monkeypatch: pytest.MonkeyPatch):
    # The seed invalidates the cache on a write, so the just-seeded value is readable
    # in the same process without waiting out the TTL.
    monkeypatch.setattr(cfg, "followables", {"lost_sector": 42})

    await settings.seed_followables_from_env()

    assert await settings.get_followable_channel("lost_sector") == 42


# --- seed_settings_from_env (the retirement bridge for every OTHER env-controlled
# setting — colors, urls, alert level, disable_bad_channels, log/alerts channel) -------


async def test_seed_settings_writes_every_configured_var(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("EMBED_DEFAULT_COLOR", "0xEC42A5")
    monkeypatch.setenv("EMBED_ERROR_COLOR", "ED4245")
    monkeypatch.setenv("DEFAULT_URL", "https://example.com")
    monkeypatch.setenv("ALERT_MIN_LEVEL", "WARNING")
    monkeypatch.setenv("DISABLE_BAD_CHANNELS", "True")
    monkeypatch.setenv("LOG_CHANNEL_ID", "111")
    monkeypatch.setenv("ALERTS_CHANNEL_ID", "222")
    monkeypatch.setenv("LOST_SECTOR_GIF_URL", "https://example.com/ls.gif")
    monkeypatch.setenv("XUR_IMAGE_URL", "https://example.com/xur.png")

    written = await settings.seed_settings_from_env()

    assert written == {
        "default_url": "https://example.com",
        "alert_min_level": "WARNING",
        "lost_sector_image_url": "https://example.com/ls.gif",
        "xur_image_url": "https://example.com/xur.png",
        "embed_default_color": "#EC42A5",
        "embed_error_color": "#ED4245",
        "log_channel_id": "111",
        "alerts_channel_id": "222",
        "disable_bad_channels": "True",
    }
    assert await settings.get_embed_default_color() == h.Color(0xEC42A5)
    assert await settings.get_embed_error_color() == h.Color(0xED4245)
    assert await settings.get_default_url() == "https://example.com"
    assert await settings.get_alert_min_level() == "WARNING"
    assert await settings.get_disable_bad_channels() is True
    assert await settings.get_log_channel_id() == 111
    assert await settings.get_alerts_channel_id() == 222
    assert await settings.get_lost_sector_image_url() == "https://example.com/ls.gif"
    assert await settings.get_xur_image_url() == "https://example.com/xur.png"


async def test_seed_settings_never_overwrites_an_existing_row(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("DEFAULT_URL", "https://from-env.example.com")
    await schemas.AutoPostSettings.set_value("default_url", "https://from-db.example.com")
    settings.invalidate()

    written = await settings.seed_settings_from_env()

    assert "default_url" not in written
    assert await settings.get_default_url() == "https://from-db.example.com"


async def test_seed_settings_never_overwrites_an_existing_enabled_row(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("DISABLE_BAD_CHANNELS", "true")
    await schemas.AutoPostSettings.set_enabled("disable_bad_channels", False)
    settings.invalidate()

    written = await settings.seed_settings_from_env()

    assert "disable_bad_channels" not in written
    assert await settings.get_disable_bad_channels() is False


async def test_seed_settings_skips_unset_vars(monkeypatch: pytest.MonkeyPatch):
    for var in (
        "EMBED_DEFAULT_COLOR",
        "EMBED_ERROR_COLOR",
        "DEFAULT_URL",
        "ALERT_MIN_LEVEL",
        "DISABLE_BAD_CHANNELS",
        "LOG_CHANNEL_ID",
        "ALERTS_CHANNEL_ID",
        "LOST_SECTOR_GIF_URL",
        "XUR_IMAGE_URL",
    ):
        monkeypatch.delenv(var, raising=False)

    assert await settings.seed_settings_from_env() == {}


async def test_seed_settings_skips_unparseable_values(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EMBED_DEFAULT_COLOR", "not-a-hex-color")
    monkeypatch.setenv("LOG_CHANNEL_ID", "not-an-integer")

    written = await settings.seed_settings_from_env()

    assert "embed_default_color" not in written
    assert "log_channel_id" not in written
    assert await schemas.AutoPostSettings.get_value("embed_default_color") is None
    assert await schemas.AutoPostSettings.get_value("log_channel_id") is None


async def test_seed_settings_is_idempotent_across_repeated_runs(
    monkeypatch: pytest.MonkeyPatch,
):
    # Only ALERT_MIN_LEVEL is under test; clear the rest so ambient .env vars (this
    # suite runs with a real .env for cfg.py's import-time validation) can't leak in.
    for var in (
        "EMBED_DEFAULT_COLOR",
        "EMBED_ERROR_COLOR",
        "DEFAULT_URL",
        "DISABLE_BAD_CHANNELS",
        "LOG_CHANNEL_ID",
        "ALERTS_CHANNEL_ID",
        "LOST_SECTOR_GIF_URL",
        "XUR_IMAGE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ALERT_MIN_LEVEL", "CRITICAL")

    first = await settings.seed_settings_from_env()
    second = await settings.seed_settings_from_env()

    assert first == {"alert_min_level": "CRITICAL"}
    assert second == {}
