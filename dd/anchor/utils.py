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
import concurrent.futures
import contextlib
import datetime as dt
import functools
import logging
import typing as t

import aiofiles
import aiohttp
import attr
import hikari as h
import yarl

from dd.hmessage import HMessage

from ..common.bot import CachedFetchBot
from ..common.utils import ErrorClass, classify_error


class FeatureDisabledError(Exception):
    pass


@contextlib.contextmanager
def operation_timer(op_name, logger: logging.Logger | None = None):
    if logger is None:
        logger = logging.getLogger("main/" + __name__)
    start_time = dt.datetime.now()
    logger.info("Announce started")
    yield lambda t: (t - start_time).total_seconds()
    end_time = dt.datetime.now()
    time_delta = end_time - start_time
    minutes = time_delta.seconds // 60
    seconds = time_delta.seconds % 60
    logger.info(f"{op_name} finished in {minutes} minutes and {seconds} seconds")


def weekend_period(
    today: dt.datetime | None = None,
) -> tuple[dt.datetime, dt.datetime]:
    if today is None:
        today = dt.datetime.now()
    today = dt.datetime(today.year, today.month, today.day, tzinfo=dt.UTC)
    monday = today - dt.timedelta(days=today.weekday())
    # Weekend is friday 1700 UTC to Tuesday 1700 UTC
    friday = monday + dt.timedelta(days=4) + dt.timedelta(hours=17)
    tuesday = friday + dt.timedelta(days=4)
    return friday, tuesday


def week_period(today: dt.datetime | None = None) -> tuple[dt.datetime, dt.datetime]:
    if today is None:
        today = dt.datetime.now()
    today = dt.datetime(today.year, today.month, today.day, tzinfo=dt.UTC)
    monday = today - dt.timedelta(days=today.weekday())
    start = monday + dt.timedelta(days=1) + dt.timedelta(hours=17)
    end = start + dt.timedelta(days=7)
    return start, end


def day_period(today: dt.datetime | None = None) -> tuple[dt.datetime, dt.datetime]:
    if today is None:
        today = dt.datetime.now()
    today = dt.datetime(today.year, today.month, today.day, 17, tzinfo=dt.UTC)
    today_end = today + dt.timedelta(days=1)
    return today, today_end


@attr.s
class MessageFailureError(Exception):
    channel_id: int = attr.ib()
    message_kwargs: dict[str, t.Any] = attr.ib()
    source_exception_details: Exception = attr.ib()


async def find_duplicate_uncrossposted_message(
    message: HMessage,
    channel: h.TextableChannel,
    lookback_days: int = 2,
) -> h.Message | None:
    async for channel_message in channel.fetch_history(
        after=dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=lookback_days)
    ):
        channel_message_proto = HMessage.from_message(channel_message)

        if (
            h.MessageFlag.CROSSPOSTED not in channel_message.flags
            and channel_message_proto == message
        ):
            logging.error(
                "Found duplicate, uncrossposted lost sector message after reported "
                "failure to send. Returning message for crossposting if necessary..."
            )
            return channel_message
        else:
            return None


class CrosspostError(Exception):
    """A message could not be crossposted (non-news channel, or retries exhausted).

    Raised ONLY in the strict mode (``max_attempts`` set) used by the web publish path,
    so a failed publish surfaces an error to the form instead of hanging — and the
    caller never marks a post "published" when no crosspost actually happened. The
    fire-and-forget autopost callers keep the retry-forever behaviour (``max_attempts is
    None``) and this is never raised for them.
    """


async def crosspost_message_with_retries(
    bot: CachedFetchBot,
    channel: h.TextableChannel | int,
    message_id: int,
    *,
    max_attempts: int | None = None,
):
    """Crosspost ``message_id`` in ``channel``, retrying transient failures.

    ``max_attempts is None`` (default) is the legacy fire-and-forget mode used by the
    automatic producers: retry FOREVER with growing backoff, and silently skip a
    non-news channel. Passing an explicit ``max_attempts`` opts into STRICT mode (the
    web publish path): a non-news channel, a permanent error, or ``max_attempts``
    transient failures raise :class:`CrosspostError`/the underlying error instead of
    looping — so the form gets a real error rather than an indefinitely-hanging request.
    """
    strict = max_attempts is not None
    if isinstance(channel, int):
        resolved = bot.cache.get_guild_channel(channel) or await bot.rest.fetch_channel(
            channel
        )
    else:
        resolved = channel
    if not isinstance(resolved, h.GuildNewsChannel):
        if strict:
            raise CrosspostError(
                f"Channel {getattr(resolved, 'id', channel)} is not an "
                "announcement/news channel, so its posts can't be crossposted to "
                "followers. Convert it to an Announcement channel in Discord first."
            )
        logging.warning(
            "Attempted to crosspost a message in a non-news channel. Skipping..."
        )
        return
    # Strict (web) mode uses a short backoff so a transient blip doesn't hang the
    # request for minutes; fire-and-forget mode keeps the slower cadence it always had.
    crosspost_backoff = 2 if strict else 30
    attempts = 0
    while True:
        try:
            await bot.rest.crosspost_message(resolved.id, message_id)
        except Exception as e:
            if (
                isinstance(e, h.BadRequestError)
                and "This message has already been crossposted" in e.message
            ):
                # If the message has already been crossposted
                # then we can ignore the error
                break

            e.add_note("Failed to publish message with exception\n")
            logging.exception(e)
            attempts += 1
            # Strict mode gives up (re-raises) on a permanent error or once the attempt
            # budget is spent, so the publish route can report it. Legacy mode never
            # gives up.
            if strict and (
                classify_error(e) is ErrorClass.PERMANENT or attempts >= max_attempts
            ):
                raise
            await aio.sleep(crosspost_backoff)
            crosspost_backoff = crosspost_backoff * 2
        else:
            break


async def send_message(
    bot: CachedFetchBot,
    msg_proto: HMessage,
    channel_id: int,
    crosspost: bool = True,
    deduplicate: bool = False,
) -> h.Message:
    send_backoff = 10
    channel: h.TextableChannel | None = None
    msg: h.Message | None = None
    while True:
        try:
            channel = t.cast(
                h.TextableChannel,
                bot.cache.get_guild_channel(channel_id)
                or await bot.rest.fetch_channel(channel_id),
            )
            msg = await channel.send(**msg_proto.to_message_kwargs())
        except Exception as e:
            e.add_note("Failed to send lost sector with exception\n")
            logging.exception(e)

            if deduplicate and channel:
                # channel.send sometimes "fails" while still sending out
                # the correct message. In case this happens, check for
                # such a message, assign it to "msg" and continue forward
                # for it to be corssposted
                #
                # Wait before doing this to ensure that when
                # find_duplicate_uncrossposted_message is called and checks
                # the channel message history, the message will appear if
                # it was sent
                await aio.sleep(send_backoff / 2)
                msg = await find_duplicate_uncrossposted_message(msg_proto, channel)
                if msg:
                    break

            await aio.sleep(send_backoff)
            send_backoff = send_backoff * 2
        else:
            break

    if msg is None:
        raise RuntimeError("send_message exited its loop without sending a message")
    if not crosspost:
        return msg

    if channel is None:
        raise RuntimeError("send_message has no channel to crosspost from")
    await crosspost_message_with_retries(bot, channel, msg.id)

    return msg


# Cap image downloads so a slow/hung host can't block this coroutine forever.
_IMAGE_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=30)


async def download_linked_image(url: str) -> str | None:
    # Returns the name of the downloaded image
    # Throws an aiohttp.client_exceptions.InvalidURL on
    # an invalid url
    # ToDo: Implement a per URL lock on this function
    #       Also implement a naming scheme based on path
    #       And implement a name size limit as required
    async with aiohttp.ClientSession(timeout=_IMAGE_DOWNLOAD_TIMEOUT) as session:
        backoff_timer = 1
        try:
            while True:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        name = _get_uri_name(resp.url)
                        async with aiofiles.open(name, mode="wb") as f:
                            await f.write(await resp.read())
                        return name
                    else:
                        await aio.sleep(backoff_timer)
                        backoff_timer = backoff_timer + (1 / backoff_timer)
        except aiohttp.InvalidURL:
            return None


def _get_uri_name(url: str | yarl.URL) -> str:
    return yarl.URL(url).name


async def run_in_thread_pool(func, *args, **kwargs):
    # Apply arguments without executing with functools partial
    partial_func = functools.partial(func, *args, **kwargs)
    # Execute in thread pool
    future = aio.get_event_loop().run_in_executor(
        concurrent.futures.ThreadPoolExecutor(), partial_func
    )
    await future
    exception = future.exception()
    if exception is not None:
        raise exception


def endl(*args: list[str]) -> str:
    # Returns a string with each argument separated by a newline
    return "\n".join([str(arg) for arg in args])
