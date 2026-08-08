import asyncio as aio
import base64
import hashlib
import logging
import typing as t
from enum import Enum

import aiohttp
import hikari as h
import regex as re

from . import cfg

if t.TYPE_CHECKING:
    from dd.hmessage.message import HMessage

re_user_side_emoji = re.compile(r"(<a?)?:(\w+)(~\d)*:(\d+>)?")

# Collapse digit runs (snowflakes, references, counts) so otherwise-identical
# messages share one error identity / reference code. Mirrors the same idea in
# ``discord_logging`` (which re-imports the helpers below).
_DIGIT_RUN = re.compile(r"\d+")


def _normalize(text: str) -> str:
    return _DIGIT_RUN.sub("#", text)


def identity_for_exc(exc: BaseException) -> str:
    """The stable identity of an exception (type + normalized message).

    Used by :func:`reference_code`, the mirror kernels, and the logging handler so
    the code shown to a user matches the code on the deduped alert. Lives here
    (rather than in ``discord_logging``) as a pure, Discord-free helper that the
    mirror subsystem can import without pulling in the logging handler.
    """
    return f"{type(exc).__module__}.{type(exc).__qualname__}: {_normalize(str(exc))}"


def reference_code(identity: str) -> str:
    """A short, stable, human-friendly code for an error identity.

    Base32 of a blake2s digest -> uppercase ``A-Z2-7``, 6 chars.
    """
    digest = hashlib.blake2s(identity.encode("utf-8"), digest_size=5).digest()
    return base64.b32encode(digest).decode("ascii").rstrip("=")[:6]


def format_duration(seconds: float) -> str:
    """Render an elapsed duration as ``"<x> seconds"`` or ``"<m> minutes <s> seconds"``.

    Replaces the duplicated inline formatting in the mirror progress functions.
    Seconds are rounded to the nearest whole second; under a minute reads as plain
    seconds, from a minute up it reads as whole minutes plus remaining seconds.
    """
    seconds = round(seconds)
    if seconds < 60:
        return f"{seconds} seconds"
    minutes = seconds // 60
    return f"{minutes} minutes {seconds % 60} seconds"


class ErrorClass(Enum):
    """Whether a failed Discord API call is worth retrying.

    ``PERMANENT`` failures (missing perms/access, unknown channel/message, malformed
    request, unauthorized) will not succeed on retry, so they are recorded and
    excluded from further scheduling immediately. ``TRANSIENT`` failures (rate
    limits, 5xx, timeouts, connection errors, and unknown exceptions) are retried
    with backoff.
    """

    PERMANENT = 1
    TRANSIENT = 2


# Discord JSON error codes (the ``.code`` on a hikari ``HTTPResponseError``) that are
# permanent even though their HTTP status might otherwise look retryable.
_PERMANENT_400_CODES = frozenset({50035, 50006})

# Connection-level exception types that mean "try again later" rather than a hard
# rejection. ``aiohttp.ClientConnectionError`` covers DNS/connect/reset failures.
_TRANSIENT_EXC_TYPES: tuple[type[BaseException], ...] = (
    TimeoutError,
    ConnectionError,
    aiohttp.ClientConnectionError,
)

_unknown_error_logger = logging.getLogger("dd.common.classify_error")


def classify_error(exc: BaseException) -> ErrorClass:
    """Classify a Discord API exception as ``PERMANENT`` or ``TRANSIENT``.

    Checked by ``isinstance`` first, then refined for the generic
    :class:`hikari.HTTPResponseError` by HTTP ``status``. Unknown exceptions are
    treated as ``TRANSIENT`` (so a stray bug retries rather than silently dropping a
    target) but logged once so they surface.
    """
    # Permanent client errors: missing perms/access (403), unknown channel/message/
    # guild (404), unauthorized (401). These never succeed on retry.
    if isinstance(exc, (h.ForbiddenError, h.NotFoundError, h.UnauthorizedError)):
        return ErrorClass.PERMANENT

    # Bad request (400): malformed/invalid; treated as permanent. The "already
    # crossposted" 400 is handled as success at the kernel call site, so it never
    # reaches here.
    if isinstance(exc, h.BadRequestError):
        return ErrorClass.PERMANENT

    # Rate limited (429): always retry after backoff.
    if isinstance(exc, h.RateLimitTooLongError):
        return ErrorClass.TRANSIENT

    # Other HTTP responses: 5xx (and any unexpected status) → transient.
    if isinstance(exc, h.HTTPResponseError):
        if exc.status >= 500:
            return ErrorClass.TRANSIENT
        # A 4xx we did not special-case above is unlikely to fix itself; treat the
        # specific permanent JSON codes as permanent, the rest as transient so we do
        # not silently give up on something recoverable.
        code = getattr(exc, "code", None)
        if code in _PERMANENT_400_CODES:
            return ErrorClass.PERMANENT
        return ErrorClass.TRANSIENT

    # Timeouts / connection resets / generic transport errors: retry.
    if isinstance(exc, _TRANSIENT_EXC_TYPES):
        return ErrorClass.TRANSIENT

    # Unknown: retry but surface it once so it gets noticed.
    _unknown_error_logger.warning(
        "Unclassified mirror kernel error %s treated as transient",
        identity_for_exc(exc),
        exc_info=exc,
    )
    return ErrorClass.TRANSIENT


# A Discord channel link embeds the guild id then the channel id; a message link adds
# the message id as a third segment. These let commands accept links/mentions/ids for
# channels in *other* servers — the slash-command channel option type can't, since its
# picker only lists channels in the guild the command was invoked from.
_re_channel_link = re.compile(
    r"(?:https?://)?(?:\w+\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)"
)
_re_channel_mention = re.compile(r"<#(\d+)>")
_re_message_link = re.compile(
    r"(?:https?://)?(?:\w+\.)?discord(?:app)?\.com/channels/\d+/(\d+)/(\d+)"
)


def parse_channel_ref(value: str) -> tuple[int, int | None]:
    """Parse a channel link, channel mention, or raw id.

    Returns ``(channel_id, guild_id)``; ``guild_id`` is only populated when a full
    channel link is supplied (a mention or bare id carries no guild). Lets commands
    target channels outside the current server, which the slash-command channel option
    type cannot.
    """
    value = value.strip()
    if match := _re_channel_link.search(value):
        guild_id, channel_id = int(match.group(1)), int(match.group(2))
        return channel_id, guild_id
    if match := _re_channel_mention.search(value):
        return int(match.group(1)), None
    try:
        return int(value), None
    except ValueError as e:
        raise ValueError(f"{value!r} is not a channel link, mention, or id") from e


def parse_message_link(value: str) -> tuple[int, int]:
    """Parse a Discord message link into ``(channel_id, message_id)``.

    Accepts ``.../channels/<guild_id>/<channel_id>/<message_id>`` (the guild segment
    is ignored — a message is uniquely addressed by its channel and id).
    """
    value = value.strip()
    if match := _re_message_link.search(value):
        return int(match.group(1)), int(match.group(2))
    raise ValueError(f"{value!r} is not a Discord message link")


# lightbulb registers global commands under guild key 0, so a guild id of 0 in a
# ``guilds=`` list silently turns a guild-scoped command into a global one.
GLOBAL_COMMAND_KEY = 0


def guild_scope(*guild_ids: int) -> list[int]:
    """Build a ``guilds=`` list safe to pass to lightbulb, dropping the 0 sentinel.

    A guild id of ``0`` is lightbulb's global-command key, so letting it through
    would register a guild-scoped command globally. Drop any such ids (warning when
    we do, since it usually means a guild-id env var is unset), and raise if nothing
    valid remains rather than registering globally by accident. Non-zero sentinels
    like ``-1`` are kept — they harmlessly scope to a nonexistent guild.
    """
    scoped = [gid for gid in guild_ids if gid != GLOBAL_COMMAND_KEY]
    if len(scoped) != len(guild_ids):
        logging.getLogger("main/" + __name__).warning(
            "Dropped guild id(s) equal to the global-command key (0) from a command "
            "registration scope; check that guild-id env vars are set."
        )
    if not scoped:
        raise ValueError(
            "Guild registration scope collapsed to empty after removing the "
            "global-command key (0); refusing to register globally by accident."
        )
    return list(dict.fromkeys(scoped))  # de-dupe, preserve order


async def fetch_emoji_dict(bot: h.GatewayBot):
    guild = bot.cache.get_guild(
        cfg.kyber_discord_server_id
    ) or await bot.rest.fetch_guild(cfg.kyber_discord_server_id)
    return {emoji.name: emoji for emoji in await guild.fetch_emojis()}


def construct_emoji_substituter(
    emoji_dict: dict[str, h.Emoji],
) -> t.Callable[[t.Any], str]:
    """Constructs a substituter for user-side emoji to be used in re.sub"""

    def func(match: t.Any) -> str:
        # Leave already-qualified ``<:name:id>`` mentions untouched — group(4) is the
        # trailing ``id>``. Without this, an app-emoji mention spliced in earlier (e.g.
        # a lazily-uploaded item icon) whose name collides with a guild emoji would be
        # silently rewritten to the guild emoji's id.
        if match.group(4):
            return str(match.group(0))
        maybe_emoji_name = str(match.group(2))
        return str(
            emoji_dict.get(maybe_emoji_name)
            or emoji_dict.get(maybe_emoji_name.lower())
            or match.group(0)
        )

    return func


def substitute_guild_emoji(
    hmsg: "HMessage", emoji_dict: dict[str, h.Emoji]
) -> "HMessage":
    """Resolve ``:name:`` tokens to guild-emoji mentions across ``hmsg``'s surfaces.

    A thin adapter over :meth:`HMessage.map_text` with ``emoji_dict``; already-qualified
    ``<:name:id>`` mentions are left as-is (see :func:`construct_emoji_substituter`)."""
    substituter = construct_emoji_substituter(emoji_dict)
    return hmsg.map_text(lambda text: str(re_user_side_emoji.sub(substituter, text)))


class space:
    zero_width = "\u200b"
    hair = "\u200a"
    six_per_em = "\u2006"
    thin = "\u2009"
    punctuation = "\u2008"
    four_per_em = "\u2005"
    three_per_em = "\u2004"
    figure = "\u2007"
    en = "\u2002"
    em = "\u2003"


def get_ordinal_suffix(day: int) -> str:
    return (
        {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
        if day not in (11, 12, 13)
        else "th"
    )


async def update_status(bot: h.GatewayBot, guild_count: int, test_env: bool):
    await bot.update_presence(
        activity=h.Activity(
            name=f"{guild_count} servers : )" if not test_env else "DEBUG MODE",
            type=h.ActivityType.LISTENING,
        )
    )


# Cap link-following HTTP requests so a hung redirect host can't block a
# coroutine indefinitely (aiohttp's implicit default is a 5-minute total).
_LINK_FOLLOW_TIMEOUT = aiohttp.ClientTimeout(total=10)
# Retry only *transient* failures (5xx / network / timeout); a 4xx or a
# redirect-less 2xx/3xx is permanent, so we return the original url at once
# rather than sleeping through a retry storm.
_LINK_FOLLOW_RETRIES = 2
_LINK_FOLLOW_RETRY_DELAY = 1


async def follow_link_single_step(
    url: str, logger: logging.Logger | None = None
) -> str:
    """Resolve a single redirect hop, falling back to ``url`` itself.

    Returns the ``Location`` header of a one-step redirect, or ``url`` unchanged
    when there is no redirect to follow. Never raises: transient failures (5xx,
    connection/timeout errors) are retried a bounded number of times and then
    give up to ``url`` so a dead or hung link can't block the caller."""
    if logger is None:
        logger = logging.getLogger("main/" + __name__)
    async with aiohttp.ClientSession(timeout=_LINK_FOLLOW_TIMEOUT) as session:
        for attempt in range(_LINK_FOLLOW_RETRIES + 1):
            try:
                async with session.get(url, allow_redirects=False) as resp:
                    location = resp.headers.get("Location")
                    if location:
                        return location
                    if resp.status < 500:
                        # 2xx/3xx-without-Location/4xx: nothing to follow, and a
                        # 4xx won't heal on retry — return the url as-is.
                        return url
                    logger.error(
                        f"Server error following url {url} (status {resp.status})"
                    )
            except (aiohttp.ClientError, TimeoutError) as e:
                logger.error(f"Network error following url {url}: {e!r}")
            if attempt < _LINK_FOLLOW_RETRIES:
                await aio.sleep(_LINK_FOLLOW_RETRY_DELAY)
        return url


class FriendlyValueError(ValueError):
    pass


def check_number_of_layers(
    ln_names: t.Sequence[t.Any] | int, min_layers: int = 1, max_layers: int = 3
):
    """Raises FriendlyValueError on too many layers of commands

    This is a simple helper function to check if ln_names is between min_layers and
    max_layers. If it is not, a FriendlyValueError is raised."""

    # ``ln_names`` is either an already-computed length (int) or a sequence of layer
    # names (list/tuple) to count. Narrow on int so the len() branch covers both
    # lists and tuples (callers pass ``*ln_names`` tuples).
    ln_name_length = ln_names if isinstance(ln_names, int) else len(ln_names)

    if ln_name_length > max_layers:
        raise FriendlyValueError(
            "Discord does not support slash "
            + f"commands with more than {max_layers} layers"
        )
    elif ln_name_length < min_layers:
        raise ValueError(f"Too few ln_names provided, need at least {min_layers}")


def ensure_session(sessionmaker):
    """Decorator for functions that optionally want an sqlalchemy async session

    Provides an async session via the `session` parameter if one is not already
    provided via the same.

    Caution: Always put below `@classmethod` and `@staticmethod`"""

    def ensured_session(
        f: t.Callable[..., t.Awaitable[t.Any]],
    ) -> t.Callable[..., t.Awaitable[t.Any]]:
        async def wrapper(*args: t.Any, **kwargs: t.Any) -> t.Any:
            session = kwargs.pop("session", None)
            if session is None:
                async with sessionmaker() as session, session.begin():
                    return await f(*args, **kwargs, session=session)
            else:
                return await f(*args, **kwargs, session=session)

        return wrapper

    return ensured_session


def accumulate[T](iterable: t.Sequence[T], /, empty_value: T | None = None) -> T:
    if not iterable:
        if empty_value is None:
            raise ValueError("accumulate() arg is an empty sequence")
        return empty_value
    final = iterable[0]
    for arg in iterable[1:]:
        final = final + arg  # ty: ignore[unsupported-operator]
    return final


async def discord_error_logger(
    e: Exception,
    error_reference: str | int | None = None,
    *,
    operation: str | None = None,
    level: int = logging.ERROR,
) -> str:
    """Surface an exception to the Discord alerts channel and the console.

    Routes through ``logging`` so the installed ``DiscordLogHandler`` renders a
    rich, deduplicated Components V2 alert (with traceback + severity) — it no
    longer sends to the channel directly. Returns the reference code shown to
    the user, which is deterministic per error identity so it matches the code
    on the resulting alert.

    ``operation`` is a short human label for what was being attempted (e.g.
    ``"Mirror update"``); when given it is surfaced in the alert header so the
    failure reads at a glance.

    ``level`` escalates the alert: pass ``logging.CRITICAL`` to raise a 🚨
    owner-pinging alert for a single occurrence (no storm needed). An escalated
    alert is treated as a *proactive notice* (e.g. an autopost was truncated to fit
    the cap) rather than a crash — its message is rendered as clean alert text with
    **no traceback**, so it reads as an alert and not an error report. A default
    (ERROR) call keeps the exception's traceback; storm-escalation to CRITICAL
    happens in the handler and preserves that real traceback.
    """
    code = (
        str(error_reference) if error_reference else reference_code(identity_for_exc(e))
    )
    logger = logging.getLogger("dd.error")
    if level > logging.ERROR:
        # Escalated proactive alert: log the message with no exc_info (so no traceback
        # block) and stamp the reference so the header code matches the returned one.
        logger.log(
            level, "%s", str(e), extra={"dd_operation": operation, "dd_reference": code}
        )
    else:
        logger.log(
            level,
            "Error reference: %s",
            code,
            exc_info=e,
            extra={"dd_operation": operation} if operation else None,
        )
    return code
