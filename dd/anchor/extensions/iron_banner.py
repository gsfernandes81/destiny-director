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

"""Iron Banner — anchor producer for the ``iron_banner`` followable.

Iron Banner runs one week roughly every 4 weeks (on the weeks Trials is *not* live). Per
Kyber's design the Discord post is deliberately simple — it highlights the dates, game
mode(s) and the current **Bonus Focus Pool** (with light.gg deep links + weapon-type
emoji) and then links out to the full guide with a button; the guide covers everything
else. So unlike Trials there is **no web form and no manual publish**: this is the fully
automatic :mod:`dd.anchor.autopost` producer pattern: a cron, the ``AutoPostSettings``
toggle, and the web feed page for a manual preview/send.

The schedule and pools are **date-anchored** and edited in the rotation editor (the
``iron_banner`` type — see :mod:`dd.common.rotation_schema`). A daily 17:00 UTC cron
posts the announcement **once** when an Iron Banner week opens (a small posted-guard
keeps a missed day self-healing without reposting), gated by the enable/disable toggle,
and crossposts it so beacon mirrors it to followers. The domain/layout lives in
:mod:`dd.common.iron_banner`; this module adds the manifest-backed weapon resolution,
the Components-V2 assembly (with the guide button) and the scheduling.
"""

import asyncio
import datetime as dt
import logging
import typing as t

import aiocron
import hikari as h
import lightbulb as lb

from dd.hmessage import HMessage

from ...common import (
    components,
    iron_banner as ib,
    schemas,
    settings,
)
from ...common.bot import CachedFetchBot
from ...common.utils import fetch_emoji_dict
from .. import hybrid_post_core
from ..autopost import Feed, discord_announcer, register_feed

logger = logging.getLogger(__name__)
loader = lb.Loader()

#: RotationData row holding the posted-guard (so the daily cron posts an event once).
META_SLUG = "iron_banner_meta"
#: The followable channel this post publishes to (None if not configured — the cron and
#: owner command are then skipped, mirroring the Trials gate).
_CHANNEL_ID: int | None = settings.get_followable_channel_sync("iron_banner")


async def format_post(bot: CachedFetchBot) -> HMessage:
    """Build the Components-V2 Iron Banner post for the live (or next) event.

    Renders the current-or-next event's simplified highlight post — dates, game modes
    and the bonus focus pool — plus a link **button** to the full guide. Raises when
    there is no current/next event in the schedule (so ``/iron_banner send``/``show``
    report it rather than posting nothing).
    """
    rotation = await ib.load_rotation()
    event = rotation.current_or_next()
    if event is None:
        raise RuntimeError(
            "No current or upcoming Iron Banner event in the schedule — "
            "add one in the rotation editor."
        )

    emoji_dict = t.cast(dict[str, h.Emoji], await fetch_emoji_dict(bot))
    available = set(emoji_dict) | {"weapon"}
    pool_lines = await hybrid_post_core.resolve_weapon_lines(
        event.pool_weapon_names, available
    )

    container = h.impl.ContainerComponentBuilder(
        accent_color=await settings.get_embed_default_color()
    )
    container.add_text_display(ib.build_body(event, pool_lines))
    # The standard footer button row (Iron Banner Guide + Support).
    # finalize_cv2_post only substitutes emoji + trims text, so the row is preserved.
    container.add_component(components.footer_buttons_row(guides=ib.GUIDES))

    return await components.finalize_cv2_post(
        HMessage(components=[container]), emoji_dict, post_name="Iron Banner"
    )


# ---------------------------------------------------------------------------
# Posted-guard (so the daily cron posts each event once, self-healing)
# ---------------------------------------------------------------------------


def _event_period(event: ib.Event) -> int:
    """The reset-period key for an event's posted-guard: its start normalised to the
    containing weekly reset boundary (Tuesday 17:00 UTC).

    Keying the guard on the *period* rather than the raw ``start_ts`` means correcting
    an event's start date within the same week (a common editor tweak) doesn't look like
    a new event and re-trigger the post. For a well-formed Tuesday-reset start this
    equals ``start_ts``; a stray non-Tuesday start still collapses to a stable key.
    """
    start = dt.datetime.fromtimestamp(event.start_ts, tz=dt.UTC)
    return hybrid_post_core.current_reset_ts(start)


async def _load_last_posted_reset() -> int:
    """The reset-period key of the last Iron Banner event we posted (0 = none)."""
    data = await schemas.RotationData.get_data(META_SLUG)
    return int((data or {}).get("last_posted_reset", 0) or 0)


async def _save_last_posted_reset(period: int) -> None:
    await schemas.RotationData.set_data(META_SLUG, {"last_posted_reset": int(period)})


# ---------------------------------------------------------------------------
# Schedule + startup
# ---------------------------------------------------------------------------


@loader.listener(h.StartedEvent)
async def _schedule_iron_banner(
    event: h.StartedEvent, bot: CachedFetchBot = lb.di.INJECTED
) -> None:
    if not _CHANNEL_ID:
        return

    # Prewarm the manifest weapon pool so the first post's weapon resolution is fast
    # (best-effort; a failure just means the first resolve pays the manifest cost).
    asyncio.create_task(hybrid_post_core.get_weapon_pool())

    # Daily 17:00 UTC — Iron Banner begins at the Tuesday reset, but a daily check with
    # a posted-guard self-heals a missed Tuesday without reposting on later days.
    # Enable/disable lives on AutoPostSettings (the /iron_banner auto cmd + web toggle).
    @aiocron.crontab("0 17 * * *", start=True)
    # Testing: post every minute -> @aiocron.crontab("* * * * *", start=True)
    async def autopost_iron_banner() -> None:
        if not await schemas.AutoPostSettings.get_iron_banner_enabled():
            return
        try:
            rotation = await ib.load_rotation()
        except Exception:
            # Malformed stored doc / DB outage with no cache — skip this run rather than
            # posting the baked default (see iron_banner.load_rotation).
            logger.exception("iron_banner: rotation unavailable; skipping this run")
            return
        current = rotation.active_event()
        if current is None:
            return  # not an Iron Banner week
        period = _event_period(current)
        if await _load_last_posted_reset() == period:
            return  # already posted this event's reset period
        await discord_announcer(
            bot,
            channel_id=await settings.get_followable_channel("iron_banner"),
            construct_message_coro=format_post,
            publish_message=True,
            cv2=True,
        )
        await _save_last_posted_reset(period)


# Contribute this feed's producer wiring to the web feed page (Preview / Send now).
# Registered unconditionally, unlike the command group above: with no configured
# followable the feed is *dormant*, and its page says so rather than 404-ing behind a
# link /autopost_settings shows either way. Preview still works — it needs no channel.
register_feed(
    Feed(
        name="iron_banner",
        channel_id=_CHANNEL_ID,
        message_constructor_coro=format_post,
        message_announcer_coro=discord_announcer,
    )
)
