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

import asyncio as aio
import logging
import typing as t

from dd.hmessage import HMessage

from ..common.bot import CachedFetchBot
from . import utils

logger = logging.getLogger(__name__)


class Feed(t.NamedTuple):
    """One autopost feed's producer wiring, as the web feed page needs it.

    ``name`` is the **followable name** (``lost_sector``, not the old ``ls`` command
    abbreviation) — the shared key that already joins autopost settings, the mirror log
    and stats (see ``plans/anchor_web_ia.md`` §1), so a feed page URL lines up with
    every other surface.

    ``channel_id`` is ``None`` for a *dormant* feed: one whose followable channel has
    not been set on the Autopost Settings page yet. Such a feed still
    registers and still previews — construction needs no channel — but cannot be sent.
    """

    name: str
    channel_id: int | None
    message_constructor_coro: t.Callable[..., t.Awaitable[HMessage]]
    message_announcer_coro: t.Callable[..., t.Awaitable[t.Any]] | None = None
    cv2: bool = True


# Feeds contributed by the producer modules at import time, mirroring how they
# contribute web routes and homepage cards (``web.register_routes`` /
# ``register_card``). Read at request time, so contribution order does not matter.
_feeds: dict[str, Feed] = {}


def register_feed(feed: Feed) -> None:
    """Contribute a feed's producer wiring for the web feed page.

    Call at import time from the producer module, next to its cron listener.
    Registration is unconditional — a dormant feed (no configured channel) registers
    too, so its page exists and explains itself rather than 404-ing behind a link the
    settings page shows regardless.

    **Dormancy is normalised here.** Producers pass
    ``settings.get_followable_channel_sync(name)``, and a followable that is
    present-but-unset — which is how portal_ops and iron_banner ship by default — yields
    the integer ``0``, not ``None``.
    The producers' own gates are falsy checks so they were right either way; the web
    handler's was ``is None``, so Send answered "started" and announced into channel 0,
    failing where only the log could see it. Collapsing the two spellings at the single
    place feeds are constructed is what makes ``channel_id: int | None`` mean what it
    says everywhere downstream.
    """
    if not feed.channel_id:
        feed = feed._replace(channel_id=None)
    _feeds[feed.name] = feed


def registered_feeds() -> dict[str, Feed]:
    """The contributed feeds, keyed by followable name (a copy)."""
    return dict(_feeds)


async def discord_announcer(
    bot: CachedFetchBot,
    channel_id: int,
    construct_message_coro: t.Callable[..., t.Awaitable[HMessage]],
    check_enabled: bool = False,
    enabled_check_coro: t.Callable[[], t.Awaitable[bool | None]] | None = None,
    publish_message: bool = True,
    cv2: bool = False,
):
    """Build a message and send (optionally crossposting) it to ``channel_id``.

    The shared announce path for the automatic followable producers (Lost Sector, Iron
    Banner, …) and the feed page's manual "Send now".
    ``check_enabled`` gates on ``enabled_check_coro`` (the producer's autopost getter);
    message construction retries with capped exponential backoff so a transient error
    (manifest/Discord blip) doesn't drop the post.
    """
    # ``cv2`` is accepted for signature parity with the other announcer
    # (``xur.api_to_discord_announcer``) so a Feed can hold either; this one creates a
    # fresh message rather than editing a placeholder, so the sent format is whatever
    # ``construct_message_coro`` returns — no flag-toggle constraint to honour here.
    hmessage: HMessage | None = None
    # ``retries`` lives outside the loop so the backoff actually grows on a sustained
    # failure (a reset-each-iteration counter stays pinned at 2s).
    retries = 0
    while True:
        try:
            if check_enabled and (
                enabled_check_coro is None or not await enabled_check_coro()
            ):
                return
            hmessage = await construct_message_coro(bot=bot)
        except Exception as e:
            logger.exception(e)
            retries += 1
            await aio.sleep(min(2**retries, 300))
        else:
            break

    logger.info("Announcing post to channel %s", channel_id)
    await utils.send_message(
        bot,
        hmessage,
        channel_id=channel_id,
        crosspost=publish_message,
        deduplicate=True,
    )
    logger.info("Announced post to channel %s", channel_id)
