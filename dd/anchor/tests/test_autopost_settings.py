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

# Autopost settings page: render reflects the AutoPostSettings rows, save persists via
# the model, unknown keys are ignored, and the homepage card is registered. Exercised
# with a fake request (no live server); auth is the web_auth middleware, covered in
# test_web_auth.py, so the handlers assume an already-authenticated request.

import asyncio
import html
import json
import re
import typing as t

import aiohttp.web
import hikari as h
import pytest
from sqlalchemy import delete

from dd.anchor import autopost, web
from dd.anchor.extensions import autopost_settings as aps
from dd.common import cfg, schemas, settings
from dd.hmessage import HMessage

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clean_settings() -> t.Iterator[None]:
    """Start each test from an empty auto_post_settings table (session-scoped DB).

    Sync fixture driving the async delete via ``asyncio.run`` — mirrors conftest's DB
    setup; the anchor test suite avoids async fixtures.
    """

    async def _clear() -> None:
        async with schemas.db_session() as session, session.begin():
            await session.execute(delete(schemas.AutoPostSettings))

    asyncio.run(_clear())
    # dd.common.settings caches rows across the whole process; without this a test can
    # read another test's (now-deleted) values until the TTL happens to lapse.
    settings.invalidate()
    settings._cache.clear()
    yield


async def _noop(**_kwargs: object) -> HMessage:
    """Stand-in constructor; the render path never calls it."""
    raise AssertionError("the settings page must not build a post to render a row")


class _FakeRequest:
    """Minimal aiohttp.web.Request stand-in exposing an awaitable ``.json()``."""

    def __init__(self, payload: object, *, raise_on_json: bool = False) -> None:
        self._payload = payload
        self._raise = raise_on_json

    async def json(self) -> object:
        if self._raise:
            raise ValueError("bad body")
        return self._payload


def _as_request(req: _FakeRequest) -> aiohttp.web.Request:
    return t.cast(aiohttp.web.Request, req)


# --- rendering --------------------------------------------------------------------


@pytest.mark.integration
async def test_render_reflects_db_state() -> None:
    await schemas.AutoPostSettings.set_enabled("lost_sector", True)
    await schemas.AutoPostSettings.set_enabled("xur", False)

    html_out = await aps._render_html()

    # An enabled row renders a checked box; a disabled row renders unchecked.
    assert 'data-slug="lost_sector" checked' in html_out
    assert 'data-slug="xur" checked' not in html_out
    assert 'data-slug="xur"' in html_out
    # Every known toggle appears with its label + description, and rows are switches.
    # Compare against the escaped copy — descriptions carry apostrophes/em-dashes.
    for setting in aps._SETTINGS:
        assert f'data-slug="{setting.slug}"' in html_out
        assert html.escape(setting.label) in html_out
        assert html.escape(setting.desc) in html_out
    assert 'class="switch"' in html_out
    # One .group box per top-level feed; sub-toggles share their parent's box.
    assert html_out.count('class="group"') == sum(1 for s in aps._SETTINGS if not s.sub)
    assert aps._TOGGLES_PLACEHOLDER not in html_out


@pytest.mark.integration
async def test_render_shows_a_header_for_each_general_settings_category() -> None:
    # Branding and Logging & Alerts are separate categories/boxes now, each with its own
    # header — not one undifferentiated "general settings" blob, and not reusing the
    # first row's own label as a fake group title.
    html_out = await aps._render_html()

    assert '<div class="groupheader">Branding</div>' in html_out
    assert '<div class="groupheader">Logging &amp; Alerts</div>' in html_out
    # A feed group gets no header — its toggle row already names it.
    assert html_out.count('class="groupheader"') == len(
        {s.category for s in aps._SETTINGS if s.category}
    )


@pytest.mark.integration
async def test_render_category_rows_are_flat_peers_not_indented() -> None:
    # embed_default_color/alert_min_level are sub=False only because something has to
    # start the group box — under an explicit category header every setting in it is a
    # peer, so none should get the ".sub" indented/dimmer treatment that would visually
    # single one out as if it were that category's "parent" row.
    html_out = await aps._render_html()
    by_slug = {s.slug: s for s in aps._SETTINGS}

    def _row_start(slug: str) -> str:
        # The opening <div class="..."> is a short, fixed distance before the escaped
        # label text (only the label-block's own wrapper divs sit between them).
        label = html.escape(by_slug[slug].label)
        idx = html_out.index(label)
        return html_out[max(0, idx - 120) : idx]

    for slug in (
        "embed_default_color",
        "embed_error_color",
        "default_url",
        "alert_min_level",
        "disable_bad_channels",
        "log_channel_id",
    ):
        assert 'class="row sub' not in _row_start(slug), (
            f"{slug} row is unexpectedly indented"
        )

    # A genuine feed sub-setting is untouched — lost_sector_details really IS a
    # refinement of lost_sector, so it keeps the indented/dimmer styling.
    assert 'class="row sub' in _row_start("lost_sector_details")


@pytest.mark.integration
async def test_render_category_rows_dim_by_flagship_not_position() -> None:
    # A categorised group's rows are flat peers (see the test above), but they still
    # need the same two-tone rhythm a feed group's light-parent/dark-subs gives it.
    # Branding's flagship setting (what "Branding" IS) stays light; Logging & Alerts has
    # no single flagship, so every one of its settings — including the one that must
    # structurally start the group — is dark. See _Setting.dim's docstring.
    html_out = await aps._render_html()
    by_slug = {s.slug: s for s in aps._SETTINGS}

    def _row_start(slug: str) -> str:
        label = html.escape(by_slug[slug].label)
        idx = html_out.index(label)
        return html_out[max(0, idx - 120) : idx]

    assert 'class="row flat-alt' not in _row_start("embed_default_color")
    assert 'class="row flat-alt' in _row_start("embed_error_color")
    assert 'class="row flat-alt' in _row_start("default_url")

    assert 'class="row flat-alt' in _row_start("alert_min_level")
    assert 'class="row flat-alt' in _row_start("disable_bad_channels")
    assert 'class="row flat-alt' in _row_start("log_channel_id")
    assert 'class="row flat-alt' in _row_start("alerts_channel_id")

    # A feed group never gets .flat-alt — it uses .sub, not zebra-striping.
    assert 'class="row flat-alt' not in _row_start("lost_sector_details")


@pytest.mark.integration
async def test_render_missing_row_is_unchecked() -> None:
    # No rows seeded → every toggle renders unchecked (producers treat None as off).
    # Matched against the toggles specifically: the send modal's publish checkbox is
    # also `checked` by default, and it is not a setting.
    html_out = await aps._render_html()

    assert not re.search(r'data-slug="[^"]+" checked', html_out)


@pytest.mark.integration
async def test_handle_get_returns_html_response() -> None:
    resp = await aps._handle_get(_as_request(_FakeRequest(None)))

    assert resp.status == 200
    assert resp.content_type == "text/html"
    assert resp.text is not None
    assert 'data-slug="lost_sector"' in resp.text


# --- per-feed actions ---------------------------------------------------------------
#
# Preview and Send now replaced the `/<feed> show` and `send` commands. They render on
# the row itself rather than a per-feed page, and the rendered post shows in a modal —
# so the list stays a list of toggles.


@pytest.fixture
def _registered_feed(monkeypatch: pytest.MonkeyPatch) -> t.Iterator[None]:
    """Register one feed, so the row actions render.

    The real registry is filled by the producer modules at import time; a test that
    relied on some other test having imported them would pass or fail by ordering.
    """
    monkeypatch.setattr(
        autopost,
        "_feeds",
        {
            "lost_sector": autopost.Feed(
                name="lost_sector", channel_id=7, message_constructor_coro=_noop
            )
        },
    )
    yield


@pytest.mark.integration
async def test_feed_rows_carry_both_actions_with_hover_cards(
    _registered_feed: None,
) -> None:
    html_out = await aps._render_html()

    for action in ("preview", "send"):
        assert f'data-action="{action}" data-slug="lost_sector"' in html_out, (
            f"the {action} action is missing from the lost_sector row"
        )
    # Explanations are hover cards, not paragraphs — two labelled buttons do not need
    # two blocks of copy on a page that is otherwise a dense list.
    assert html_out.count("title=") >= 2


@pytest.mark.integration
async def test_sub_settings_and_url_rows_get_no_actions(
    _registered_feed: None,
) -> None:
    # `lost_sector_details` refines its parent and has no producer of its own, and the
    # eververse image URL is a value, not a feed — neither can be previewed or sent.
    html_out = await aps._render_html()

    assert 'data-slug="lost_sector_details"' in html_out  # the row still renders
    for slug in ("lost_sector_details", "xur_default_image", "eververse_image_url"):
        assert f'data-action="preview" data-slug="{slug}"' not in html_out


@pytest.mark.integration
async def test_page_hosts_the_preview_and_send_modals() -> None:
    # The post is drawn in a modal by the shared renderer, so the page must load it and
    # its stylesheet, and the publish choice belongs in the send confirmation rather
    # than sitting pre-set on the page.
    resp = await aps._handle_get(_as_request(_FakeRequest(None)))
    assert resp.text is not None
    body = resp.text

    assert '<dialog class="feedmodal" id="previewDialog">' in body
    assert '<dialog class="feedmodal" id="sendDialog">' in body
    assert 'id="sendPreview"' in body
    assert 'id="publish"' in body
    assert "/static/cv2_render.js" in body
    assert "/static/cv2_preview.css" in body
    # Both preview hosts must opt into the shared styling, or the renderer draws
    # correct DOM with none of its appearance.
    assert body.count('class="modalpreview cv2-preview"') == 2


# --- saving -----------------------------------------------------------------------


@pytest.mark.integration
async def test_handle_save_persists_toggles() -> None:
    req = _FakeRequest({"settings": {"lost_sector": True, "xur": False}})

    resp = await aps._handle_save(_as_request(req))

    assert resp.status == 200
    assert await schemas.AutoPostSettings.get_enabled("lost_sector") is True
    assert await schemas.AutoPostSettings.get_enabled("xur") is False


@pytest.mark.integration
async def test_handle_save_ignores_unknown_slugs() -> None:
    req = _FakeRequest({"settings": {"not_a_feed": True, "ada": True}})

    resp = await aps._handle_save(_as_request(req))

    assert resp.status == 200
    # The known slug is written; the unknown one never creates a row.
    assert await schemas.AutoPostSettings.get_enabled("ada") is True
    assert await schemas.AutoPostSettings.get_enabled("not_a_feed") is None


@pytest.mark.integration
async def test_handle_save_coerces_truthy_values() -> None:
    # The client sends booleans, but bool() must coerce anything the JSON carries.
    req = _FakeRequest({"settings": {"eververse": 1, "portal_ops": 0}})

    await aps._handle_save(_as_request(req))

    assert await schemas.AutoPostSettings.get_enabled("eververse") is True
    assert await schemas.AutoPostSettings.get_enabled("portal_ops") is False


# --- url setting (eververse default image) ----------------------------------------


@pytest.mark.integration
async def test_render_shows_url_field_with_value() -> None:
    await schemas.AutoPostSettings.set_eververse_image_url(
        "https://example.com/banner.png"
    )

    html_out = await aps._render_html()

    # The URL setting renders a text input (not a switch) carrying its saved value.
    assert 'class="urlfield" data-slug="eververse_image_url"' in html_out
    assert 'value="https://example.com/banner.png"' in html_out


@pytest.mark.integration
async def test_handle_save_persists_url_value() -> None:
    req = _FakeRequest(
        {"settings": {"eververse_image_url": "https://example.com/banner.png"}}
    )

    resp = await aps._handle_save(_as_request(req))

    assert resp.status == 200
    assert (
        await schemas.AutoPostSettings.get_eververse_image_url()
        == "https://example.com/banner.png"
    )


@pytest.mark.integration
async def test_handle_save_blank_url_clears_value() -> None:
    await schemas.AutoPostSettings.set_eververse_image_url("https://example.com/a.png")

    resp = await aps._handle_save(
        _as_request(_FakeRequest({"settings": {"eververse_image_url": "  "}}))
    )

    assert resp.status == 200
    # A blank field stores NULL → "no image".
    assert await schemas.AutoPostSettings.get_eververse_image_url() is None


@pytest.mark.integration
async def test_handle_save_rejects_non_http_url() -> None:
    resp = await aps._handle_save(
        _as_request(
            _FakeRequest({"settings": {"eververse_image_url": "ftp://x/y.png"}})
        )
    )

    assert resp.status == 400
    # The whole save aborts before any write — no row is created.
    assert await schemas.AutoPostSettings.get_eververse_image_url() is None


@pytest.mark.integration
async def test_handle_save_rejects_non_string_url() -> None:
    resp = await aps._handle_save(
        _as_request(_FakeRequest({"settings": {"eververse_image_url": 123}}))
    )

    assert resp.status == 400


async def test_handle_save_rejects_malformed_body() -> None:
    resp = await aps._handle_save(_as_request(_FakeRequest(None, raise_on_json=True)))

    assert resp.status == 400


async def test_handle_save_rejects_non_object_settings() -> None:
    resp = await aps._handle_save(_as_request(_FakeRequest({"settings": "nope"})))

    assert resp.status == 400


# --- color setting (embed_default_color) -------------------------------------------


@pytest.mark.integration
async def test_render_shows_color_field_with_value() -> None:
    await schemas.AutoPostSettings.set_value("embed_default_color", "#EC42A5")

    html_out = await aps._render_html()

    assert (
        'class="colorfield no-focus-ring" data-slug="embed_default_color"' in html_out
    )
    assert 'value="#EC42A5"' in html_out
    # The paired swatch carries the same value so it isn't drawn black on first load.
    assert 'data-for="embed_default_color" value="#EC42A5"' in html_out


@pytest.mark.integration
async def test_render_blank_color_swatch_falls_back_to_black() -> None:
    # No row saved yet: the text field stays blank, but the swatch (a native
    # input[type=color], which cannot be blank) shows black rather than nothing.
    html_out = await aps._render_html()

    assert 'data-for="embed_default_color" value="#000000"' in html_out
    assert re.search(
        r'class="colorfield no-focus-ring" data-slug="embed_default_color" value=""',
        html_out,
    )


@pytest.mark.integration
async def test_handle_save_persists_color_value() -> None:
    req = _FakeRequest({"settings": {"embed_error_color": "#FF0000"}})

    resp = await aps._handle_save(_as_request(req))

    assert resp.status == 200
    assert await schemas.AutoPostSettings.get_value("embed_error_color") == "#FF0000"
    # The save invalidates dd.common.settings' cache, so the new value is live at once.
    assert await settings.get_embed_error_color() == h.Color(0xFF0000)


@pytest.mark.integration
async def test_handle_save_rejects_malformed_color() -> None:
    resp = await aps._handle_save(
        _as_request(_FakeRequest({"settings": {"embed_default_color": "pink"}}))
    )

    assert resp.status == 400
    assert await schemas.AutoPostSettings.get_value("embed_default_color") is None


@pytest.mark.integration
async def test_handle_save_blank_color_clears_value() -> None:
    await schemas.AutoPostSettings.set_value("embed_default_color", "#EC42A5")

    resp = await aps._handle_save(
        _as_request(_FakeRequest({"settings": {"embed_default_color": "  "}}))
    )

    assert resp.status == 200
    assert await schemas.AutoPostSettings.get_value("embed_default_color") is None


# --- select setting (alert_min_level) -----------------------------------------------


@pytest.mark.integration
async def test_render_shows_select_field_with_current_option_selected() -> None:
    await schemas.AutoPostSettings.set_value("alert_min_level", "WARNING")

    html_out = await aps._render_html()

    assert 'class="selectfield" data-slug="alert_min_level"' in html_out
    assert '<option value="WARNING" selected>WARNING</option>' in html_out
    assert '<option value="ERROR">ERROR</option>' in html_out  # not selected


@pytest.mark.integration
async def test_render_select_defaults_to_settings_default_when_unset() -> None:
    # A bare <select> always shows its first <option> as "selected" even when none is
    # marked so — if no row is saved and nothing here corrects for that, the page would
    # show "DEBUG" (alphabetically first) selected while the bot actually applies
    # dd.common.settings' real default (ERROR). Caught by driving this page in a real
    # browser: the DOM's default selection doesn't reduce to a string match in html_out.
    html_out = await aps._render_html()

    assert '<option value="ERROR" selected>ERROR</option>' in html_out
    assert await settings.get_alert_min_level() == "ERROR"  # the two must agree


@pytest.mark.integration
async def test_handle_save_persists_select_value() -> None:
    resp = await aps._handle_save(
        _as_request(_FakeRequest({"settings": {"alert_min_level": "CRITICAL"}}))
    )

    assert resp.status == 200
    assert await schemas.AutoPostSettings.get_value("alert_min_level") == "CRITICAL"


@pytest.mark.integration
async def test_handle_save_rejects_unknown_select_option() -> None:
    resp = await aps._handle_save(
        _as_request(_FakeRequest({"settings": {"alert_min_level": "VERBOSE"}}))
    )

    assert resp.status == 400
    assert await schemas.AutoPostSettings.get_value("alert_min_level") is None


# --- channel setting (followable + log/alerts channels) -----------------------------


@pytest.mark.integration
async def test_render_shows_channel_field_with_current_option() -> None:
    await schemas.AutoPostSettings.set_value("lost_sector_channel", "123456789")

    html_out = await aps._render_html()

    assert 'class="channelfield" data-slug="lost_sector_channel"' in html_out
    assert 'data-scope="kyber"' in html_out
    assert '<option value="123456789" selected>123456789</option>' in html_out


@pytest.mark.integration
async def test_render_log_and_alerts_channel_scope_kyber_and_control() -> None:
    html_out = await aps._render_html()

    for slug in ("log_channel_id", "alerts_channel_id"):
        assert f'data-slug="{slug}" data-scope="kyber_control"' in html_out


@pytest.mark.integration
async def test_render_followable_channels_are_announce_only_log_alerts_are_not() -> (
    None
):
    # A followable channel is FOLLOWED by other servers (MirroredChannel), which Discord
    # only allows from an announcement channel — a plain text channel can't be followed
    # at all. log_channel_id/alerts_channel_id are never followed (the bot just sends
    # there), so any postable channel is fine for those.
    html_out = await aps._render_html()

    assert 'data-slug="lost_sector_channel"' in html_out
    assert (
        'data-scope="kyber" data-announce-only="true"'
        in html_out.split('data-slug="lost_sector_channel"')[1][:100]
    )
    for slug in ("log_channel_id", "alerts_channel_id"):
        assert (
            'data-scope="kyber_control" data-announce-only="false"'
            in html_out.split(f'data-slug="{slug}"')[1][:100]
        )


@pytest.mark.integration
async def test_handle_save_persists_channel_value() -> None:
    resp = await aps._handle_save(
        _as_request(_FakeRequest({"settings": {"xur_channel": "999"}}))
    )

    assert resp.status == 200
    assert await schemas.AutoPostSettings.get_value("xur_channel") == "999"
    assert await settings.get_followable_channel("xur") == 999


@pytest.mark.integration
async def test_handle_save_blank_channel_stores_zero_not_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Unlike a url/color row, a cleared channel must store "0", not NULL — NULL would
    # fall through to the FOLLOWABLES env-var seed (see
    # settings.get_followable_channel), so an operator explicitly clearing a channel
    # would not see the feed go dormant.
    monkeypatch.setattr(cfg, "followables", {"xur": 42})
    await schemas.AutoPostSettings.set_value("xur_channel", "999")

    resp = await aps._handle_save(
        _as_request(_FakeRequest({"settings": {"xur_channel": ""}}))
    )

    assert resp.status == 200
    assert await schemas.AutoPostSettings.get_value("xur_channel") == "0"
    assert await settings.get_followable_channel("xur") == 0


@pytest.mark.integration
async def test_handle_save_rejects_non_numeric_channel() -> None:
    resp = await aps._handle_save(
        _as_request(_FakeRequest({"settings": {"xur_channel": "not-a-snowflake"}}))
    )

    assert resp.status == 400
    assert await schemas.AutoPostSettings.get_value("xur_channel") is None


# --- /autopost_settings/channels ----------------------------------------------------


class _FakeChannel:
    def __init__(self, channel_id: int, name: str, kind: h.ChannelType) -> None:
        self.id = channel_id
        self.name = name
        self.type = kind


class _FakeRest:
    def __init__(self, by_guild: dict[int, list[_FakeChannel]]) -> None:
        self._by_guild = by_guild

    async def fetch_guild_channels(self, guild_id: int) -> list[_FakeChannel]:
        return self._by_guild.get(guild_id, [])


class _FakeBot:
    def __init__(self, by_guild: dict[int, list[_FakeChannel]]) -> None:
        self.rest = _FakeRest(by_guild)


@pytest.fixture
def _guild_ids(monkeypatch: pytest.MonkeyPatch) -> tuple[int, int]:
    kyber, control = 111, 222
    monkeypatch.setattr(cfg, "kyber_discord_server_id", kyber)
    monkeypatch.setattr(cfg, "control_discord_server_id", control)
    return kyber, control


async def test_handle_channels_lists_postable_channels_from_both_guilds(
    monkeypatch: pytest.MonkeyPatch, _guild_ids: tuple[int, int]
) -> None:
    kyber, control = _guild_ids
    monkeypatch.setattr(
        aps,
        "_bot",
        _FakeBot(
            {
                kyber: [
                    _FakeChannel(1, "lost-sector", h.ChannelType.GUILD_TEXT),
                    _FakeChannel(2, "announcements", h.ChannelType.GUILD_NEWS),
                    _FakeChannel(3, "voice-chat", h.ChannelType.GUILD_VOICE),
                ],
                control: [_FakeChannel(4, "mod-log", h.ChannelType.GUILD_TEXT)],
            }
        ),
    )

    resp = await aps._handle_channels(_as_request(_FakeRequest(None)))
    payload = t.cast(dict, json.loads(resp.text or ""))

    ids = {c["id"] for c in payload["channels"]}
    assert ids == {"1", "2", "4"}  # the voice channel is filtered out
    assert payload["kyberGuildId"] == str(kyber)
    assert payload["controlGuildId"] == str(control)
    by_id = {c["id"]: c for c in payload["channels"]}
    assert by_id["1"]["name"] == "#lost-sector"
    assert by_id["1"]["guildId"] == str(kyber)
    assert by_id["4"]["guildId"] == str(control)
    # A plain text channel is tagged non-announce; a NEWS channel is announce=True — the
    # client filters followable pickers to announce-only (Discord's "Follow Channel"
    # requires it), so this flag must be correct, not just present.
    assert by_id["1"]["announce"] is False
    assert by_id["2"]["announce"] is True


async def test_handle_channels_survives_an_unreachable_guild(
    monkeypatch: pytest.MonkeyPatch, _guild_ids: tuple[int, int]
) -> None:
    kyber, _control = _guild_ids

    class _RaisingRest:
        async def fetch_guild_channels(self, _guild_id: int) -> list[_FakeChannel]:
            raise RuntimeError("not in guild")

    class _PartialBot:
        rest = _RaisingRest()

    monkeypatch.setattr(aps, "_bot", _PartialBot())

    resp = await aps._handle_channels(_as_request(_FakeRequest(None)))
    payload = t.cast(dict, json.loads(resp.text or ""))

    assert payload["channels"] == []


async def test_handle_channels_before_bot_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(aps, "_bot", None)

    resp = await aps._handle_channels(_as_request(_FakeRequest(None)))

    assert resp.status == 503


# --- homepage card ----------------------------------------------------------------


async def test_card_is_registered() -> None:
    titles = [card.title for card in web.registered_cards()]
    assert "Autopost Settings" in titles
    card = next(c for c in web.registered_cards() if c.title == "Autopost Settings")
    assert card.href == "/autopost_settings"
