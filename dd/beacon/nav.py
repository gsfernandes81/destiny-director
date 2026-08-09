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

# Define our custom navigator classes
import contextlib
import datetime as dt
import logging
import re
import typing as t
import uuid
from asyncio import Lock, Task, create_task, sleep
from random import randint
from typing import override

import hikari as h
import lightbulb as lb
from lightbulb import components as lbc

from dd.hmessage import HMessage

from ..common import (
    components as dd_components,
    feeds as dd_feeds,
    settings,
)
from ..common.bot import CachedFetchBot
from ..common.cfg import navigator_timeout, url_regex
from ..common.utils import accumulate, discord_error_logger, get_ordinal_suffix
from . import utils

logger = logging.getLogger(__name__)

# A stable sentinel compared by equality elsewhere (e.g. ada.py checks
# `page.embeds[0] == NO_DATA_HERE_EMBED`), so it is built once at import time rather
# than re-resolved per read — see settings.get_embed_default_color_sync's docstring.
NO_DATA_HERE_EMBED = h.Embed(
    title="No data here!", color=settings.get_embed_default_color_sync()
)

# Tolerance for binning Destiny reset-time messages into periods: a message
# posted up to this long before a reset still bins into the period it belongs to.
reset_time_tolerance = dt.timedelta(minutes=60)

# Unicode play (▶) / reverse (◀) triangles used as the default next/prev button
# emoji. Defined as module-level constants so they are not constructed in
# function-argument defaults (ruff B008).
NEXT_PAGE_EMOJI = chr(9654)
PREV_PAGE_EMOJI = chr(9664)


class DateRangeDict(dict[dt.datetime, HMessage]):
    """Dict with keys that are contiguous date ranges up to limits

    The keys of the backing dict are the start of the date ranges.
    The keys received by __getitem__ are rounded down to the nearest date
    provided it is within DateRangeDict.period: dt.timedelta
    If the key provided is an int, then it is interpreted as n periods
    since the current datetime rounded down.

    period: dt.timedelta
        The period between each key

    limits: tuple[dt.datetime, dt.datetime]
        The upper and lower bounds of the dict"""

    def __init__(
        self,
        period: dt.timedelta,
        limits: tuple[dt.datetime, dt.datetime] | None = None,
    ):
        if not isinstance(period, dt.timedelta):
            raise TypeError("period must be of type datetime.timedelta")

        self.period = period

        if limits:
            if len(limits) != 2:
                raise ValueError("limits must be a tuple of length 2")

            if not all(isinstance(limit, dt.datetime) for limit in limits):
                raise TypeError("limits must be a tuple of datetime.datetime")

            if limits[0] > limits[1]:
                raise ValueError("limits[0] must be less than limits[1]")

            if limits[1] - limits[0] < period:
                raise ValueError("limits must be at least one period apart")

            if (limits[1] - limits[0]) % period != dt.timedelta(0):
                raise ValueError("limits must be an integer multiple of period apart")

            self.limits = limits

    def round_down(
        self,
        key: dt.datetime,
        tolerance: dt.timedelta = reset_time_tolerance,
    ) -> dt.datetime:
        """Round down key to nearest period with tolerance in the negative direction

        The tolerance parameter allows for rounding up by its value"""
        return (
            (key + tolerance - self.limits[0]) // self.period
        ) * self.period + self.limits[0]

    def index_to_date(
        self, index: int, tolerance: dt.timedelta = reset_time_tolerance
    ) -> dt.datetime:
        """Return the datetime of the period at <index>"""
        return (
            self.round_down(dt.datetime.now(tz=dt.UTC), tolerance=tolerance)
            + index * self.period
        )

    @override
    def __getitem__(self, key: dt.datetime | int) -> HMessage:
        if isinstance(key, int):
            key = self.index_to_date(key)
        if not isinstance(key, dt.datetime):
            raise TypeError("Key must be of type datetime.datetime or int")

        self._truncate_outside_limits()

        if not (self.limits[0] <= key <= self.limits[1]):
            raise IndexError(f"Key {key} is not in range {self.limits}")

        key = self.round_down(key)
        return super().__getitem__(key)

    @override
    def __contains__(self, __key: object) -> bool:
        if isinstance(__key, int):
            __key = self.index_to_date(__key)
        if not isinstance(__key, dt.datetime):
            raise TypeError("Key must be of type datetime.datetime or int")

        self._truncate_outside_limits()

        if not (self.limits[0] <= __key <= self.limits[1]):
            return False

        __key = self.round_down(__key)
        return super().__contains__(__key)

    @override
    def __setitem__(self, key: dt.datetime, value: HMessage) -> None:
        if not isinstance(key, dt.datetime):
            raise TypeError("Key must be of type datetime.datetime")

        if not (self.limits[0] <= key <= self.limits[1]):
            raise IndexError(f"Key {key} is not in range {self.limits}")

        self._truncate_outside_limits()
        key = self.round_down(key)
        super().__setitem__(key, value)

    def _truncate_outside_limits(self) -> None:
        """Remove all keys outside our limits"""
        for key in list(self.keys()):
            if not (self.limits[0] <= key <= self.limits[1]):
                self.pop(key)

    def purge_history(self) -> None:
        """Removes all keys in the past including now"""
        now = dt.datetime.now(tz=dt.UTC)
        for key in list(self.keys()):
            if key <= now:
                self.pop(key)

    @staticmethod
    def nearest_limit_from_period_and_ref(period: dt.timedelta, ref: dt.datetime):
        """Return the nearest lower limit to ref that is an int multiple of period"""
        if not isinstance(period, dt.timedelta):
            raise TypeError("period must be of type datetime.timedelta")

        if not isinstance(ref, dt.datetime):
            raise TypeError("ref must be of type datetime.datetime")

        now = dt.datetime.now(tz=dt.UTC)
        return ((now - ref) // period) * period + ref


# Custom ids for the navigator's prev/next buttons. The lbc.Menu routes presses by
# custom_id regardless of where the button is rendered (a top-level action row for embed
# pages, or a row appended to the CV2 components), so the same ids work for both modes.
_NAV_PREV_CUSTOM_ID = "dd_nav:prev"
_NAV_NEXT_CUSTOM_ID = "dd_nav:next"
_NAV_INDICATOR_CUSTOM_ID = "dd_nav:indicator"

# Cap the menu timeout below the interaction-token TTL (see components._MAX_TIMEOUT):
# the on-timeout "disable" edit reuses the interaction token, valid for ~15 minutes.
_MAX_NAV_TIMEOUT = 15 * 60 - 60


def build_nav_row(
    *,
    current_page: int,
    history_len: int,
    lookahead_len: int,
    date_label: str,
    all_disabled: bool = False,
    prev_id: str = _NAV_PREV_CUSTOM_ID,
    next_id: str = _NAV_NEXT_CUSTOM_ID,
) -> list[h.api.InteractiveButtonBuilder]:
    """Build the prev / date-indicator / next buttons for a navigator action row.

    ``prev_id`` / ``next_id`` must match the ids :class:`NavigatorView` registers on its
    menu — the caller passes per-instance ids so a press routes only to the owning menu;
    the indicator is a disabled button (never interactive) whose label is the date.
    """
    prev_disabled = all_disabled or current_page <= 1 - history_len
    next_disabled = all_disabled or current_page >= lookahead_len
    return [
        h.impl.InteractiveButtonBuilder(
            style=h.ButtonStyle.PRIMARY,
            custom_id=prev_id,
            emoji=PREV_PAGE_EMOJI,
            is_disabled=prev_disabled,
        ),
        h.impl.InteractiveButtonBuilder(
            style=h.ButtonStyle.SECONDARY,
            custom_id=_NAV_INDICATOR_CUSTOM_ID,
            label=date_label,
            is_disabled=True,
        ),
        h.impl.InteractiveButtonBuilder(
            style=h.ButtonStyle.PRIMARY,
            custom_id=next_id,
            emoji=NEXT_PAGE_EMOJI,
            is_disabled=next_disabled,
        ),
    ]


class NavigatorView:
    """A miru-free, date-indexed navigator over a :class:`NavPages`.

    Renders one :class:`HMessage` page at a time with prev / date-indicator / next
    buttons. Handles BOTH embed pages and Components V2 pages (e.g. the eververse
    autopost): a CV2 page's components are sent with the ``IS_COMPONENTS_V2`` flag and
    the nav row is appended as a top-level action row. Because a navigator edits one
    message across pages and Discord forbids toggling ``IS_COMPONENTS_V2`` on edit, a
    given navigator is single-mode — its NavPages' ``no_data_message`` matches (CV2 vs
    embed) so every page is the same type.

    Built on ``lightbulb.components.Menu`` (mirrors
    ``dd.common.components.Paginator``): the menu is a pure custom_id router, so the
    prev/next buttons route regardless of where they are rendered.
    """

    def __init__(
        self,
        *,
        pages: "NavPages",
        timeout: float | int | dt.timedelta | None = navigator_timeout,
        allow_start_on_blank_page: bool = False,
        display_date_offset: dt.timedelta = dt.timedelta(days=0),
    ) -> None:
        self._pages: NavPages = pages
        self._display_date_offset = display_date_offset

        if timeout is None:
            requested = int(navigator_timeout)
        elif isinstance(timeout, dt.timedelta):
            requested = int(timeout.total_seconds())
        else:
            requested = int(timeout)
        self._timeout = min(requested, _MAX_NAV_TIMEOUT)

        # Start on the most recent non-blank page (scan from 0 into history) unless the
        # caller allows starting on a blank page.
        if allow_start_on_blank_page:
            self._current_page = 0
        else:
            for page_no in range(0, -pages.history_len, -1):
                if page_no in pages:
                    self._current_page = page_no
                    break
            else:
                self._current_page = 0
        self._current_page = self._clamp(self._current_page)

        # Captured in ``send`` so the controls can be disabled on timeout.
        self._ctx: lb.Context | None = None
        self._message: h.Message | None = None

        # Per-instance button ids. lightbulb routes a component interaction to the first
        # attached menu whose custom_ids match, with NO message binding, so shared ids
        # would let a press on this navigator's message be handled by another live
        # navigator's menu — editing this message with that navigator's pages (e.g.
        # /lost sector showing /eververse content). Unique ids make the match resolve to
        # exactly this instance's menu.
        token = uuid.uuid4().hex
        self._prev_id = f"{_NAV_PREV_CUSTOM_ID}:{token}"
        self._next_id = f"{_NAV_NEXT_CUSTOM_ID}:{token}"

        self._menu = lbc.Menu()
        self._menu.add_interactive_button(
            h.ButtonStyle.PRIMARY,
            self._on_prev,
            custom_id=self._prev_id,
            emoji=PREV_PAGE_EMOJI,
        )
        self._menu.add_interactive_button(
            h.ButtonStyle.PRIMARY,
            self._on_next,
            custom_id=self._next_id,
            emoji=NEXT_PAGE_EMOJI,
        )

    # -- page bookkeeping --------------------------------------------------

    def _clamp(self, value: int) -> int:
        return max(
            -(self._pages.history_len - 1), min(value, self._pages.lookahead_len)
        )

    @property
    def current_page(self) -> int:
        return self._current_page

    @current_page.setter
    def current_page(self, value: int) -> None:
        self._current_page = self._clamp(value)

    @property
    def needs_pagination(self) -> bool:
        return (self._pages.history_len + self._pages.lookahead_len) > 1

    def _date_label(self) -> str:
        date = self._pages.index_to_date(self._current_page) + self._display_date_offset
        return f"{date.strftime('%B %-d')}{get_ordinal_suffix(date.day)}"

    def _nav_action_row(
        self, *, all_disabled: bool = False
    ) -> h.impl.MessageActionRowBuilder:
        row = h.impl.MessageActionRowBuilder()
        for button in build_nav_row(
            current_page=self._current_page,
            history_len=self._pages.history_len,
            lookahead_len=self._pages.lookahead_len,
            date_label=self._date_label(),
            all_disabled=all_disabled,
            prev_id=self._prev_id,
            next_id=self._next_id,
        ):
            row.add_component(button)
        return row

    # -- rendering ---------------------------------------------------------

    def _components(
        self, *, all_disabled: bool = False
    ) -> list[h.api.ComponentBuilder]:
        """The component list for the current page (nav row appended when paginated)."""
        page = self._pages[self._current_page]
        row = (
            self._nav_action_row(all_disabled=all_disabled)
            if self.needs_pagination
            else None
        )
        if page.components:
            # CV2 page: append the nav row as a top-level action row. Never mutate the
            # page's cached container builders (reused across renders); copy the list.
            comps: list[h.api.ComponentBuilder] = list(page.components)
            if row is not None:
                comps.append(row)
            return comps
        return [row] if row is not None else []

    def _render(self, *, all_disabled: bool = False) -> dict[str, t.Any]:
        page = self._pages[self._current_page]
        if page.components:
            return {
                "components": self._components(all_disabled=all_disabled),
                "flags": h.MessageFlag.IS_COMPONENTS_V2,
            }
        payload: dict[str, t.Any] = {
            "content": page.content,
            "embeds": page.embeds,
            "attachments": page.attachments,
        }
        comps = self._components(all_disabled=all_disabled)
        if comps:
            payload["components"] = comps
        # Clear stale attachments on edit: attachments=[] doesn't clear but
        # attachment=None does (preserves the previous navigator behaviour).
        if not payload.get("attachments"):
            payload.pop("attachments", None)
            payload = {"attachment": None, **payload}
        return payload

    # -- sending / editing -------------------------------------------------

    async def _respond_guarded(
        self,
        respond: t.Callable[[dict[str, t.Any]], t.Awaitable[t.Any]],
        *,
        operation: str,
    ) -> None:
        """Render the current page via ``respond``; on a client HTTP error fall back.

        A page can fail to render — Discord rejects an oversized CV2 page, or a media
        host 429s hikari's re-download — and that must never break the navigator. On
        such an error we log and re-``respond`` with a minimal same-mode placeholder so
        the initial open / prev / next stays usable. ``respond`` takes the payload.
        """
        try:
            await respond(self._render())
        except h.ClientHTTPResponseError as e:
            await discord_error_logger(e, operation=operation)
            # The fallback respond can also fail (a persistent 429, an expired
            # interaction token). Suppress it — the original error is already logged and
            # there is nothing further we can show — so it never escapes the button
            # handler / send.
            with contextlib.suppress(h.HikariError):
                await respond(self._fallback_render())

    async def send(self, ctx: lb.Context) -> None:
        """Send the current page from a ``lb.Context`` and attach the paginator."""
        await self._respond_guarded(
            lambda payload: ctx.respond(**payload), operation="Navigator page send"
        )
        if not self.needs_pagination:
            return
        self._ctx = ctx
        self._message = await ctx.interaction.fetch_initial_response()
        # ``attach`` blocks until the menu times out (a press never stops it); lightbulb
        # signals the timeout by raising. Swallow it, then disable the controls.
        with contextlib.suppress(TimeoutError):
            await self._menu.attach(ctx.client, timeout=self._timeout)
        await self._on_timeout()

    async def _edit(self, mctx: lbc.MenuContext) -> None:
        await self._respond_guarded(
            lambda payload: mctx.respond(edit=True, **payload),
            operation="Navigator page edit",
        )

    def _fallback_render(self) -> dict[str, t.Any]:
        """A minimal same-mode payload shown when a page fails to render.

        Same-mode is mandatory (Discord forbids toggling IS_COMPONENTS_V2 on an edit).
        A navigator is single-mode, so key off the navigator's ``cv2`` flag rather than
        the failed page. The nav row is kept so the user can page away.
        """
        row = self._nav_action_row() if self.needs_pagination else None
        if self._pages.cv2:
            comps: list[h.api.ComponentBuilder] = [
                dd_components.cv2_error("This page could not be displayed")
            ]
            if row is not None:
                comps.append(row)
            return {"components": comps, "flags": h.MessageFlag.IS_COMPONENTS_V2}
        payload: dict[str, t.Any] = {"embeds": [NO_DATA_HERE_EMBED]}
        if row is not None:
            payload["components"] = [row]
        return payload

    async def _on_prev(self, mctx: lbc.MenuContext) -> None:
        self.current_page -= 1
        await self._edit(mctx)

    async def _on_next(self, mctx: lbc.MenuContext) -> None:
        self.current_page += 1
        await self._edit(mctx)

    async def _on_timeout(self) -> None:
        if self._ctx is None or self._message is None:
            return
        # ``edit_message`` takes no flags; editing a CV2 message's components preserves
        # its flag. Skip cleanly if the token expired / the message is gone.
        with contextlib.suppress(h.UnauthorizedError, h.NotFoundError):
            await self._ctx.interaction.edit_message(
                self._message, components=self._components(all_disabled=True)
            )


class NavPages(DateRangeDict):
    """Class to maintain a dict of slash command responses over time.

    The key for the dict is the datetime after which the response was posted
    and the value is the HMessage instance for the response.
    Additionally the key also accepts an int and interprets it as n periods
    since the currrent datetime rounded down.

    __init__ registers tasks to update the dict regularly based on the
    lookahead_update_interval.

    Parameters
    channel: h.GuildNewsChannel
        The channel to fetch messages from
    period: dt.timedelta
        The period between each key
    reference_date: dt.datetime
        The date to use as the reference for the 0 key
    history_len: int
        The number of periods to keep in the past
    lookahead_len: int
        The number of periods to keep in the future
    lookahead_update_interval: int
        The number of seconds between each update of the lookahead
    suppress_content_autoembeds: bool
        Instructs the default preprocess_messages method to stop discord link auto
        embeds based on message content
    no_data_message: HMessage
        Message to use when no data is available
    """

    # Strong reference to the lookahead auto-update task, set in _setup_autoupdate
    # when lookahead_len > 0. Held only to keep the task from being garbage
    # collected, and cancelled by teardown() when the instance is replaced.
    _lookahead_task: "Task[None] | None" = None

    # Auto-update teardown handles: the registered history-updater listener and a
    # double-setup guard. Used by teardown() to release the per-instance listener +
    # lookahead task when NavPagesHolder rebuilds a feed against a new channel,
    # instead of leaking one of each per rebuild (memory-leak N4).
    _history_updater: "t.Callable[..., t.Coroutine[t.Any, t.Any, None]] | None" = None
    _autoupdate_set_up: bool = False

    def __init__(
        self,
        channel: h.GuildNewsChannel,
        period: dt.timedelta,
        reference_date: dt.datetime,
        history_len: int = 7,
        lookahead_len: int = 0,
        lookahead_update_interval: int = 1800,
        suppress_content_autoembeds: bool = True,
        no_data_message: HMessage | None = None,
        cv2: bool = False,
    ):
        super().__init__(period)
        self.history_len = history_len
        self.lookahead_len = lookahead_len
        self.channel = channel
        self.bot: CachedFetchBot = t.cast(CachedFetchBot, channel.app)
        self.lookahead_update_interval = lookahead_update_interval

        self._reference_date = reference_date
        self._suppress_content_autoembeds = suppress_content_autoembeds
        # When True this followable's posts are Components V2, so every page (including
        # the no-data page) must be CV2 — a navigator edits one message and Discord
        # forbids toggling IS_COMPONENTS_V2 on edit.
        self.cv2 = cv2
        if no_data_message is None:
            no_data_message = (
                HMessage(components=[dd_components.build_container(["No data here!"])])
                if cv2
                else HMessage(embeds=[NO_DATA_HERE_EMBED])
            )
        self.no_data_message = no_data_message

    @override
    def __getitem__(self, key: dt.datetime | int) -> HMessage:
        try:
            return super().__getitem__(key)
        except KeyError:
            return self.no_data_message

    @property
    def limits(self) -> tuple[dt.datetime, dt.datetime]:
        midpoint = self.nearest_limit_from_period_and_ref(
            period=self.period, ref=self._reference_date
        )
        limit_low = midpoint - self.period * (self.history_len - 1)
        limit_high = midpoint + self.period * self.lookahead_len
        return (limit_low, limit_high)

    def preprocess_messages(self, messages: list[h.Message]) -> HMessage:
        if not messages:
            return self.no_data_message
        msg: HMessage = accumulate([HMessage.from_message(msg) for msg in messages])

        # Components V2 navigator: convert any legacy embed pages to CV2 so every page
        # is single-mode (a navigator edits one message and Discord forbids toggling
        # IS_COMPONENTS_V2 on an edit). This supersedes the embed post-processing below,
        # which only applies to the classic embed navigators (cv2=False).
        if self.cv2:
            return self._finalize_cv2(msg)

        # Components V2 message: no embed/content post-processing applies.
        if msg.components:
            return msg

        if self._suppress_content_autoembeds:
            # Stop discord from making new auto embeds
            msg.content = (
                url_regex.sub(lambda x: f"<{x.group()}>", msg.content)
                .replace("<<", "<")
                .replace(">>", ">")
            )

        # Remove discord auto image embeds
        msg.embeds = utils.filter_discord_autoembeds(msg)
        # Remove embeds with no title or description
        msg.embeds = list(filter(lambda x: x.title or x.description, msg.embeds))

        return msg

    def _finalize_cv2(self, msg: HMessage) -> HMessage:
        """Coerce a page into Components V2 for a cv2 navigator.

        Legacy embed pages still in a followable's history are converted in-memory to
        CV2 so every page a cv2 navigator renders is CV2 — the navigator edits one
        message and Discord forbids toggling IS_COMPONENTS_V2 across an edit. Plain
        message content and attachments are dropped (the converter only reads embeds);
        acceptable for the transition window, which self-heals as history rolls over.
        """
        if not self.cv2:
            return msg

        # Keep any native CV2 containers (already-migrated posts); append a converted
        # container for any legacy embeds sharing the period (accumulate concatenates
        # both when a single period bin holds an embed post and a CV2 post). Images
        # convert to URL-referenced media galleries — Discord fetches them, the bot
        # never re-downloads/uploads them, so re-rendering is cheap.
        containers: list[h.api.ComponentBuilder] = list(msg.components)
        if msg.embeds:
            container = dd_components.embeds_to_container(
                msg.embeds, accent_color=settings.get_embed_default_color_sync()
            )
            if container.components:
                containers.append(container)

        if not containers:
            return self.no_data_message

        # Single enforcement point for the 4000-char CV2 cap. The page is assembled from
        # heterogeneous parts — native containers, converted embeds, and however many
        # messages ``accumulate`` merged into this bin — any of which can push the
        # message-wide text over the limit. Trim the whole page once so Discord never
        # rejects it for length, rather than each source guarding itself.
        return HMessage(components=dd_components.fit_cv2_components(containers))

    @classmethod
    async def from_channel(cls, bot: h.RESTAware, channel, **kwargs) -> t.Self:
        """
        Create a NavPages instance from a channel ID or channel object.

        Additional keyword arguments (kwargs) are passed directly to the class
        constructor. This allows customization of the instance at creation.

        Args:
            bot: The bot instance used to fetch the channel if needed.
            channel: The channel ID or channel object to create from.
            period (dt.timedelta): The period between each key.
            reference_date (dt.datetime): The date to use as the reference for
                the 0 key.
            history_len (int, optional): The number of periods to keep in the
                past. Default is 7.
            lookahead_len (int, optional): The number of periods to keep in the
                future. Default is 0.
            lookahead_update_interval (int, optional): The number of seconds
                between each update of the lookahead. Default is 1800.
            suppress_content_autoembeds (bool, optional): If True, instructs the
                default preprocess_messages method to stop Discord link auto
                embeds based on message content. Default is True.
            no_data_message (HMessage, optional): Message to use when no
                data is available. Default is HMessage(embeds=[NO_DATA_HERE_EMBED]).
            **kwargs: Additional keyword arguments for the class constructor.

        Returns:
            An instance of NavPages.
        """
        if isinstance(channel, (int, h.Snowflake)):
            channel = await t.cast(CachedFetchBot, bot).fetch_channel(int(channel))

        if not isinstance(channel, h.GuildNewsChannel):
            raise TypeError(
                f"Cannot create {cls.__name__} from {channel.__class__.__name__} "
                + "since it is not an Announce channel"
            )

        self: t.Self = cls(channel, **kwargs)

        await self._populate_history()
        await self._update_lookahead()
        self._setup_autoupdate()

        return self

    async def _populate_history(self):
        # Find start time
        after = self.limits[0]

        # Bin messages into periods
        binned_messages: dict[dt.datetime, list[h.Message]] = {}
        async for msg in self.channel.fetch_history(after=after - reset_time_tolerance):
            start_of_period = self.round_down(msg.timestamp)
            binned_messages.setdefault(start_of_period, []).append(msg)

        # Preprocess messages
        key = self.limits[0]
        now = dt.datetime.now(tz=dt.UTC)
        while key <= now:
            if binned_messages.get(key):
                self[key] = self.preprocess_messages(binned_messages[key])
            key += self.period

    @utils.ignore_own_user
    async def _update_history(self, event: h.MessageCreateEvent | h.MessageUpdateEvent):
        """Updates the history with any changes or new messages in self.channel"""

        if event.channel_id != self.channel.id:
            return

        logging.info(
            ("Update " if isinstance(event, h.MessageUpdateEvent) else "Create ")
            + f"event received in channel id {event.channel_id} "
            + f"for message id {event.message_id}"
        )

        retries = 12
        for retry_no in range(retries):
            try:
                if isinstance(event.message, h.Message):
                    msg = event.message
                elif isinstance(event.message, h.PartialMessage):
                    msg = await self.bot.fetch_message(
                        event.channel_id, event.message_id
                    )
                elif isinstance(event.message, h.Snowflakeish):
                    msg = await self.bot.fetch_message(event.channel_id, event.message)
                else:
                    raise ValueError(f"Unknown message type {event.message.__class__}")

                if not (
                    self.limits[0] <= self.round_down(msg.timestamp) <= self.limits[1]
                ):
                    logging.info(
                        f"Message {msg.id} not in limits {self.limits}. Ignoring"
                    )
                    return

                # Get all messages in this event's message's period
                from_ = self.round_down(msg.timestamp)
                until_ = from_ + self.period
                msgs_from_api = []
                async for msg_from_api in self.channel.fetch_history(after=from_):
                    if msg_from_api.timestamp > until_:
                        break
                    msgs_from_api.append(msg_from_api)

                self[from_] = self.preprocess_messages(msgs_from_api)

            except Exception as e:
                await discord_error_logger(e, operation="Nav backfill")
                await sleep(2**retry_no)
            else:
                break

    async def _update_lookahead(self):
        if self.lookahead_len <= 0:
            return

        self.update(
            await self.lookahead(
                self.index_to_date(1, tolerance=dt.timedelta(minutes=1))
            )
        )

    def _setup_autoupdate(self):
        if self._autoupdate_set_up:
            return
        self._autoupdate_set_up = True
        if self.history_len > 0:

            @self.bot.listen()
            async def history_updater(
                event: h.MessageCreateEvent
                | h.MessageUpdateEvent
                | h.MessageDeleteEvent,
            ):
                if isinstance(event, h.MessageDeleteEvent):
                    if event.channel_id == self.channel.id:
                        self.purge_history()
                        await self._populate_history()
                else:
                    await self._update_history(event)

            self._history_updater = history_updater

        if self.lookahead_len > 0:
            # Lightbulb v3 removed lightbulb.ext.tasks, and this updater is created
            # per-NavPages-instance (not at module load) so it cannot use the loader's
            # task registry. Self-schedule it with an asyncio loop instead.
            async def lookahead_update_task():
                while True:
                    await sleep(self.lookahead_update_interval)
                    try:
                        # Introduce a 5% jitter to the update interval
                        # to avoid ratelimit issues
                        await sleep(
                            randint(0, int(self.lookahead_update_interval / 20))
                        )
                        await self._update_lookahead()
                    except Exception as e:
                        await discord_error_logger(e, operation="Nav lookahead")

            # Keep a strong reference: the event loop only holds a weak ref to a
            # bare task, so without this the updater can be garbage-collected
            # mid-flight and silently stop.
            self._lookahead_task = create_task(lookahead_update_task())

    def teardown(self) -> None:
        """Release the auto-update listener + lookahead task.

        Unsubscribes the history-updater from all three message events and cancels
        the lookahead task. Called by :meth:`NavPagesHolder.reopen` once the
        replacement instance is live, so a feed whose channel is changed on the
        Autopost Settings page does not leak a listener + task per change
        (memory-leak N4). Idempotent: a second call is a no-op.
        """
        if self._history_updater is not None:
            for event_type in (
                h.MessageCreateEvent,
                h.MessageUpdateEvent,
                h.MessageDeleteEvent,
            ):
                self.bot.unsubscribe(event_type, self._history_updater)
            self._history_updater = None
        if self._lookahead_task is not None:
            self._lookahead_task.cancel()
            self._lookahead_task = None
        self._autoupdate_set_up = False

    async def lookahead(self, after: dt.datetime) -> dict[dt.datetime, HMessage]:
        """Return the predicted messages for the periods after <after>

        The dict must have <self.lookahead_len> entries, indexed by the start of the
        period and must contain the HMessage for that period."""
        return {}


# Regex matching the "**From**"/"**Till**" lines that the anchor bot adds to
# weekly-reset-style posts; these are stripped from the mirrored embed.
rgx_find_from_till_text = re.compile(r"\n\*\*(From|Till)\*\*[^\n]*")


class ResetPages(NavPages):
    """NavPages for posts that share the weekly-reset anchor formatting.

    Several posts (weekly-reset, portal-ops) use identical preprocessing, so
    they share this subclass: merge the message content and attachments into the
    embed and strip the redundant From/Till lines.
    """

    @override
    def preprocess_messages(self, messages: list[h.Message]) -> HMessage:
        # Components V2 navigator (portal_ops): the base already does cv2 conversion.
        # The embed merges below are only for the weekly-reset navigator (cv2=False),
        # which also uses this subclass.
        if self.cv2:
            return super().preprocess_messages(messages)

        if not messages:
            return self.no_data_message

        for message in messages:
            message.embeds = utils.filter_discord_autoembeds(message)
        msg_proto = accumulate([HMessage.from_message(message) for message in messages])

        # Components V2 message: the embed merges below don't apply.
        if msg_proto.components:
            return msg_proto

        msg_proto = msg_proto.merge_content_into_embed().merge_attachements_into_embed(
            default_url=settings.get_default_url_sync()
        )

        # Remove duplicate From/Till text from anchor embed
        for embed in msg_proto.embeds:
            embed.description = rgx_find_from_till_text.sub("", embed.description or "")

        return msg_proto


class NavPagesHolder:
    """Late-bound container for a :class:`NavPages` built on ``StartedEvent``.

    The pages object can only be built once the bot has started (it reads
    channel history), but command callbacks need it at invoke time. The holder
    lets a shared ``StartedEvent`` listener populate ``.pages`` while commands
    close over the holder and read ``.pages`` lazily.

    It is also what re-opens the feed when its channel is changed on the Autopost
    Settings page — see :meth:`reopen` and ``utils.reconcile_feed_channels``.
    """

    def __init__(
        self,
        feed: str,
        pages_cls: type[NavPages],
        from_channel_kwargs: dict[str, t.Any],
    ) -> None:
        self.pages: NavPages | None = None
        #: Why ``pages`` is None, phrased for a user (one of ``utils.FEED_*``). Set
        #: when the source channel turns out to be unusable at startup, so the command
        #: can say what is wrong instead of raising — see make_navigator_command.
        self.unavailable: str | None = None
        #: The channel ``pages`` was built from, 0 when there are none — so it doubles
        #: as what the reconciler compares against the configured id. A feed left
        #: unavailable reads as 0 and is therefore retried on each tick (quietly), and
        #: self-heals if the channel comes back; an *unconfigured* feed reads 0 against
        #: a configured 0 and so costs nothing.
        self.channel_id: int = 0
        self._feed = feed
        self._pages_cls = pages_cls
        self._kwargs = from_channel_kwargs
        self._bot: h.RESTAware | None = None
        #: The channel the last open was attempted against; -1 = never opened. Only
        #: used to decide whether to alert, so each distinct channel pages us once
        #: rather than once per retry.
        self._last_attempt: int = -1
        # Serialises opens. The StartedEvent build and a reconciler tick are separate
        # tasks, and building reads channel history — long enough for the two to
        # overlap on a slow start and leave two NavPages listening on one channel.
        self._lock = Lock()

    async def reopen(self, channel_id: int) -> None:
        """(Re)build the pages from whatever channel the feed is configured with now.

        Build-then-swap-then-tear-down: the old pages keep answering commands until the
        new ones are ready, so a rebuild never leaves a working feed briefly
        unavailable, and the old instance's history listener + lookahead task are
        released only once it has been replaced (a swap without the teardown would leak
        a listener per rebuild — memory-leak N4).

        ``channel_id`` is the configured id as the caller read it — the reconciler read
        it to decide there was anything to do here, so it is passed straight through
        rather than resolved a second time.
        """
        bot = self._bot
        if bot is None:
            return  # not started yet; the StartedEvent build will do the first open
        async with self._lock:
            # Page us on the first attempt against a given channel and stay quiet on
            # the retries after it — a permanently deleted channel would otherwise
            # alert once per reconciler tick, forever.
            alert = channel_id != self._last_attempt
            rebuild = self._last_attempt != -1
            self._last_attempt = channel_id

            # "Open" here is building the pages themselves: from_channel reads the
            # channel's history, so it is where an unreachable channel actually shows
            # up. It is never called at all when the feed is unconfigured.
            pages, unavailable = await utils.open_feed_source(
                self._feed,
                lambda cid: self._pages_cls.from_channel(bot, cid, **self._kwargs),
                channel_id=channel_id,
                alert_when_unreachable=alert,
            )
            old = self.pages
            self.pages = pages
            self.unavailable = unavailable
            # 0 for anything but a live set of pages, so a feed left unavailable is
            # retried on the next tick rather than mistaken for one that is up to date.
            self.channel_id = channel_id if pages is not None else 0
            if old is not None and old is not pages:
                old.teardown()
            if pages is not None and rebuild:
                logger.info(
                    "%s pages rebuilt against channel %s", self._feed, channel_id
                )

    async def _start(self, bot: h.RESTAware) -> None:
        """The first open, once the gateway is up and there is a bot to fetch with.

        Resolves the configured channel itself so ``_last_attempt`` records it: a boot
        open that fails is then a retry of the *same* channel on the next reconciler
        tick, and pages us once rather than twice.
        """
        self._bot = bot
        await self.reopen(await settings.get_followable_channel(self._feed))


def setup_nav_pages(
    loader: lb.Loader,
    *,
    feed: str,
    pages_cls: type[NavPages] = NavPages,
    **from_channel_kwargs: t.Any,
) -> NavPagesHolder:
    """Register a ``StartedEvent`` listener that builds the pages into a holder.

    ``pages_cls`` is built once the bot starts, from whatever channel ``feed`` is
    configured with *then* — resolved at startup rather than taken as an import-time
    id — and rebuilt whenever that channel changes afterwards, so picking or moving a
    feed's channel on the Autopost Settings page takes effect without a restart. Extra
    keyword arguments are forwarded to :meth:`NavPages.from_channel` (``period``,
    ``reference_date``, ``history_len``, ``lookahead_len``,
    ``suppress_content_autoembeds``, ``no_data_message`` ...).

    An unusable channel never stops the listener registering, and never raises out of
    it: ``utils.open_feed_source`` records why on the holder (so the command can answer
    for itself) and pages the bot owner(s) — a source channel is ours to fix, and a user
    hitting it can do nothing about it. What that page calls the feed comes from
    ``dd.common.feeds``, so ``feed`` is the only identity this takes.
    """
    holder = NavPagesHolder(feed, pages_cls, from_channel_kwargs)

    @loader.listener(h.StartedEvent)
    async def _on_start(event: h.StartedEvent) -> None:
        await holder._start(event.app)

    # A rebuild reads the whole channel history, so it is driven by a change in the
    # configured id rather than by the clock: a tick that finds the channel unmoved
    # does no work at all.
    utils.watch_feed_channel(feed, lambda: holder.channel_id, holder.reopen)

    return holder


def make_navigator_command(
    holder: NavPagesHolder,
    *,
    name: str,
    description: str,
    feed: str = "",
    allow_start_on_blank_page: bool = False,
    display_date_offset: dt.timedelta = dt.timedelta(days=0),
) -> type[lb.SlashCommand]:
    """Build a SlashCommand that shows ``holder.pages`` in a NavigatorView.

    The returned class is *not* registered; the caller registers it with
    ``loader.command(...)`` or ``group.register(...)`` as appropriate.

    ``feed`` is the catalog slug of the feed being paged. It names the feed in the
    "unavailable" answer this gives when the source channel turned out to be unusable
    at startup — and it is taken as a slug rather than a display name because a
    navigator's command name is routinely *not* the feed's (``/ls today``, ``/weekly
    reset``, ``/portal ops``), so falling back to ``name`` told a user their Lost Sector
    command was called "today". Left blank only where no feed backs the command, and
    then ``name`` is all there is to say.
    """
    display_name = dd_feeds.FEEDS[feed].display_name if feed else name

    class _NavCommand(lb.SlashCommand, name=name, description=description):
        @lb.invoke
        async def invoke(self, ctx: lb.Context):
            if holder.pages is None:
                # No pages: either the source channel is unusable (setup_nav_pages
                # recorded why and already paged us), or the bot hasn't finished
                # starting. Either way this is ours, not the user's — say so plainly
                # rather than raising an unhandled error at them.
                await ctx.respond(
                    await utils.feed_unavailable_embed(
                        display_name or name,
                        holder.unavailable or "the bot is still starting up",
                    )
                )
                return
            navigator = NavigatorView(
                pages=holder.pages,
                allow_start_on_blank_page=allow_start_on_blank_page,
                display_date_offset=display_date_offset,
            )
            await navigator.send(ctx)

    return _NavCommand
