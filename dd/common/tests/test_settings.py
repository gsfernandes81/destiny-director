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

import asyncio

import hikari as h
import pytest
import pytest_asyncio
from sqlalchemy import delete

from dd.common import schemas, settings

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def _reset_settings_cache():
    """Start each test from an empty ``auto_post_settings`` table (session-scoped DB,
    shared with every other test module — see test_autopost_settings.py's identical
    fixture) and an unloaded settings cache, so tests can't leak into each other."""
    async with schemas.db_session() as session, session.begin():
        await session.execute(delete(schemas.AutoPostSettings))
    settings.reset_cache_for_tests()
    yield
    settings.reset_cache_for_tests()


# --- cache refresh single-flight --------------------------------------------------


async def test_an_empty_but_freshly_loaded_cache_still_counts_as_fresh(
    monkeypatch: pytest.MonkeyPatch,
):
    # A fresh install with zero configured settings rows legitimately produces an
    # EMPTY _cache after a real preload() — that must still count as "fresh" (nothing
    # to refetch until the TTL lapses), not as "never loaded" every single call, which
    # would defeat the TTL cache's entire purpose on exactly that install.
    assert settings._cache == {}
    calls = 0
    real_get_all_rows = schemas.AutoPostSettings.get_all_rows

    async def _counting_get_all_rows(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        return await real_get_all_rows(*args, **kwargs)

    monkeypatch.setattr(
        schemas.AutoPostSettings, "get_all_rows", _counting_get_all_rows
    )

    await settings.get_default_url()
    await settings.get_default_url()
    await settings.get_default_url()

    assert settings._cache == {}
    assert calls == 1


async def test_concurrent_stale_reads_share_one_refresh(
    monkeypatch: pytest.MonkeyPatch,
):
    # Force the cache stale, then let several callers observe that at once — without
    # the single-flight lock, each would independently call preload() before any of
    # them finished updating _loaded_at.
    settings._loaded_at = 0.0
    calls = 0
    real_get_all_rows = schemas.AutoPostSettings.get_all_rows

    async def _counting_get_all_rows(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)  # yield, so concurrent callers actually overlap
        return await real_get_all_rows(*args, **kwargs)

    monkeypatch.setattr(
        schemas.AutoPostSettings, "get_all_rows", _counting_get_all_rows
    )

    await asyncio.gather(*(settings._ensure_fresh() for _ in range(5)))

    assert calls == 1


async def test_reset_cache_for_tests_drops_rows_not_just_the_freshness_stamp():
    # The whole reason this helper exists rather than a stale-mark: every test module's
    # reset fixture wipes the settings rows, but the *_sync getters never check
    # freshness, so anything left in _cache would still be served to the next test for
    # a full TTL window. Clearing both is what makes the reset actually hermetic.
    await schemas.AutoPostSettings.set_value("xur_channel", "7")
    await settings.preload()
    assert settings.get_followable_channel_sync("xur") == 7

    settings.reset_cache_for_tests()

    assert settings._cache == {}
    assert settings._loaded_at == 0.0
    assert settings.get_followable_channel_sync("xur") == 0


# --- colors ----------------------------------------------------------------------


async def test_embed_default_color_defaults_to_brand_pink_when_unset():
    # The hardcoded default (see settings._DEFAULTS) is the Kyber brand pink, not black.
    assert await settings.get_embed_default_color() == h.Color(0xEC42A5)


async def test_embed_default_color_reflects_a_saved_row():
    await schemas.AutoPostSettings.set_value("embed_default_color", "#EC42A5")
    settings.reset_cache_for_tests()
    assert await settings.get_embed_default_color() == h.Color(0xEC42A5)


async def test_embed_default_color_sync_matches_async_after_preload():
    await schemas.AutoPostSettings.set_value("embed_default_color", "#123456")
    await settings.preload()
    assert settings.get_embed_default_color_sync() == h.Color(0x123456)


async def test_default_for_exposes_the_same_default_the_getters_apply():
    # The accessor the settings page renders unset fields from — it must agree with what
    # a getter resolves to for the same slug, or the page shows one colour while the
    # bots draw another.
    assert settings.default_for("embed_default_color") == "#EC42A5"
    assert await settings.get_embed_default_color() == h.Color(0xEC42A5)


async def test_default_for_ignores_a_saved_row():
    # Strictly the built-in default, never the live value: a caller that wants what is
    # actually in effect uses a getter.
    await schemas.AutoPostSettings.set_value("embed_default_color", "#123456")
    settings.reset_cache_for_tests()

    assert settings.default_for("embed_default_color") == "#EC42A5"


async def test_default_for_unknown_slug_is_none():
    # Followable channels (and anything else with no _DEFAULTS entry) have no default to
    # render — the page has to treat that as "nothing to show", not as an empty string.
    assert settings.default_for("lost_sector_channel") is None
    assert settings.default_for("not_a_setting") is None


async def test_image_url_settings_have_no_default_and_resolve_to_empty():
    """The three image URLs are default-free, and all three agree on that.

    They used to be asymmetric: lost_sector and xur carried a ``(None, "")`` entry in
    _DEFAULTS while eververse had none, so ``default_for`` answered "" for two of them
    and None for the third — a difference with no consequence, since every getter
    coerces with ``or ""``. Removing the two entries rather than adding a third keeps
    _DEFAULTS holding only settings that genuinely have a default, and makes the
    page's "is there a default to show?" question answer honestly for all three.
    """
    for slug in ("lost_sector_image_url", "xur_image_url", "eververse_image_url"):
        assert settings.default_for(slug) is None, slug

    assert await settings.get_lost_sector_image_url() == ""
    assert await settings.get_xur_image_url() == ""


async def test_malformed_stored_color_falls_back_to_black():
    await schemas.AutoPostSettings.set_value("embed_default_color", "not-a-color")
    settings.reset_cache_for_tests()
    assert await settings.get_embed_default_color() == h.Color(0)


# --- simple url/level/bool/int settings ------------------------------------------


async def test_default_url_defaults_to_empty_string():
    assert await settings.get_default_url() == ""


async def test_default_url_reflects_a_saved_row():
    await schemas.AutoPostSettings.set_value("default_url", "https://example.com")
    settings.reset_cache_for_tests()
    assert await settings.get_default_url() == "https://example.com"


async def test_alert_min_level_defaults_to_error():
    assert await settings.get_alert_min_level() == "ERROR"


async def test_disable_bad_channels_defaults_to_false():
    assert await settings.get_disable_bad_channels() is False


async def test_disable_bad_channels_reflects_a_saved_row():
    await schemas.AutoPostSettings.set_enabled("disable_bad_channels", True)
    settings.reset_cache_for_tests()
    assert await settings.get_disable_bad_channels() is True


async def test_log_and_alerts_channel_id_default_to_zero():
    assert await settings.get_log_channel_id() == 0
    assert await settings.get_alerts_channel_id() == 0


async def test_alerts_channel_id_reflects_a_saved_row():
    await schemas.AutoPostSettings.set_value("alerts_channel_id", "123456")
    settings.reset_cache_for_tests()
    assert await settings.get_alerts_channel_id() == 123456


# --- followables -------------------------------------------------------------------


async def test_followable_channel_is_zero_until_a_row_is_saved():
    # No env fallback exists any more: a followable with no DB row is dormant, full
    # stop. The settings page is the only writer (see the module docstring).
    assert await settings.get_followable_channel("lost_sector") == 0


async def test_followable_channel_reflects_a_saved_row():
    await schemas.AutoPostSettings.set_value("lost_sector_channel", "99")
    settings.reset_cache_for_tests()
    assert await settings.get_followable_channel("lost_sector") == 99


async def test_followable_channel_unknown_feed_is_zero():
    assert await settings.get_followable_channel("not_a_real_feed") == 0


async def test_followable_channel_sync_matches_async_after_preload():
    await schemas.AutoPostSettings.set_value("xur_channel", "7")
    await settings.preload()
    assert settings.get_followable_channel_sync("xur") == 7


async def test_followable_channel_sync_is_zero_before_preload():
    # The import-time readers (beacon's extensions) see 0 rather than a stale env
    # value when preload() hasn't run — dormant, and alerted by
    # resolve_followable_channel.
    await schemas.AutoPostSettings.set_value("xur_channel", "7")
    settings.reset_cache_for_tests()
    assert settings.get_followable_channel_sync("xur") == 0


async def test_get_followables_returns_every_slug():
    followables = await settings.get_followables()
    assert set(followables) == set(settings.FOLLOWABLE_SLUGS)
    assert set(followables.values()) == {0}


# --- followable_name ---------------------------------------------------------------


async def test_followable_name_returns_configured_name():
    await schemas.AutoPostSettings.set_value("lost_sector_channel", "42")
    await settings.preload()
    assert settings.followable_name(id=42) == "lost_sector"


async def test_followable_name_falls_back_to_id():
    await schemas.AutoPostSettings.set_value("lost_sector_channel", "42")
    await settings.preload()
    assert settings.followable_name(id=99) == 99
