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

import enum
import inspect
import logging
import typing as t

import hikari as h
import lightbulb as lb
from toolbox.errors import CacheFailureError
from toolbox.members import calculate_permissions

from dd.hmessage import HMessage

logger = logging.getLogger(__name__)


def get_function_name() -> str:
    """Get the name of the function this was called from"""
    return inspect.stack()[1][3]


async def check_invoker_has_perms(
    ctx: lb.Context,
    permissions: h.Permissions | list[h.Permissions],
    all_required: bool = False,
):
    bot: h.RESTAware = ctx.client.app
    invoker = ctx.user
    if not isinstance(permissions, (list, tuple)):
        permissions_ = (permissions,)
    else:
        permissions_ = permissions

    if not ctx.guild_id:
        return False

    channel = await bot.rest.fetch_channel(ctx.channel_id)

    if isinstance(channel, h.GuildThreadChannel):
        channel = await bot.rest.fetch_channel(channel.parent_id)

    if not isinstance(channel, h.PermissibleGuildChannel):
        return False

    member = await bot.rest.fetch_member(ctx.guild_id, invoker.id)
    invoker_perms = calculate_permissions(member, channel)

    if all_required:
        return all(
            [permission == (permission & invoker_perms) for permission in permissions_]
        )
    else:
        return any(
            [permission == (permission & invoker_perms) for permission in permissions_]
        )


def explain_missing_permission(
    member: h.Member,
    channel: h.PermissibleGuildChannel,
    permission: h.Permissions,
) -> str | None:
    """Best-effort attribution of *why* the bot is missing ``permission`` here.

    Replicates ``toolbox.members.calculate_permissions``' apply-order and, for a
    permission that ends up **missing**, returns the *most-specific* cause (a channel
    override on the bot, on a role, or on @everyone, vs. never granted at the server
    level). Returns ``None`` when the permission is actually present, when the member
    is the guild owner / an administrator, or when the guild can't be resolved — the
    caller then falls back to a plain ✅/❌ without a source line. Pure over
    ``member`` + ``channel`` + their cached overwrites ⇒ unit-testable with stubs."""
    guild = member.get_guild()
    if guild is None:
        return None
    if guild.owner_id == member.id:
        return None

    guild_roles = guild.get_roles()
    member_roles = [r for r in guild_roles.values() if r.id in member.role_ids]

    perms = guild_roles[guild.id].permissions  # @everyone base
    for role in member_roles:
        perms |= role.permissions
    if perms & h.Permissions.ADMINISTRATOR:
        return None

    overwrites = channel.permission_overwrites
    everyone_ow = overwrites.get(channel.guild_id)
    if everyone_ow:
        perms &= ~everyone_ow.deny
        perms |= everyone_ow.allow

    role_allow = h.Permissions.NONE
    role_deny = h.Permissions.NONE
    denying_roles: list[h.Role] = []
    for role in member_roles:
        overwrite = overwrites.get(role.id)
        if overwrite:
            role_allow |= overwrite.allow
            role_deny |= overwrite.deny
            if permission & overwrite.deny:
                denying_roles.append(role)
    perms &= ~role_deny
    perms |= role_allow

    member_ow = overwrites.get(member.id)
    if member_ow:
        perms &= ~member_ow.deny
        perms |= member_ow.allow

    if permission & perms == permission:
        return None  # actually present — nothing to attribute

    # Attribute to the most-specific override that denies the bit.
    if member_ow and (permission & member_ow.deny):
        return "a channel permission override on me denies it"
    if denying_roles and not (permission & role_allow):
        names = ", ".join(f"@{role.name}" for role in denying_roles)
        noun = "role" if len(denying_roles) == 1 else "roles"
        return f"a channel override on the {names} {noun} denies it"
    if everyone_ow and (permission & everyone_ow.deny):
        return "the channel's @everyone override denies it"
    return (
        "none of my roles grant it here — grant my role this permission or add a "
        "channel override"
    )


async def _resolve_bot_member_channel(
    ctx: lb.Context,
) -> tuple[h.Member | None, h.PermissibleGuildChannel | None]:
    """Resolve the bot's own member and the permission-bearing target channel.

    Cache-first with a REST fallback for the member (a freshly-joined guild may not
    be cached yet); a thread resolves to its parent, matching
    ``check_invoker_has_perms``. Returns ``(None, None)`` when there's no guild or the
    bot user is unknown, and ``(member, None)`` when the channel isn't a permissible
    guild channel or can't be fetched — e.g. the bot lacks View Channel (perms can't
    be computed for it)."""
    app = t.cast(h.GatewayBot, ctx.client.app)
    if not ctx.guild_id:
        return None, None

    me = app.get_me()
    if me is None:
        return None, None

    member = app.cache.get_member(ctx.guild_id, me.id) or await app.rest.fetch_member(
        ctx.guild_id, me.id
    )

    # A 403/404 here (the bot can't see the channel, or it's gone) must not crash the
    # command — the caller renders the diagnostics embed instead.
    try:
        channel = await app.rest.fetch_channel(ctx.channel_id)
        if isinstance(channel, h.GuildThreadChannel):
            channel = await app.rest.fetch_channel(channel.parent_id)
    except (h.ForbiddenError, h.NotFoundError):
        return member, None
    if not isinstance(channel, h.PermissibleGuildChannel):
        return member, None

    return member, channel


async def resolve_bot_perms(
    ctx: lb.Context,
) -> tuple[h.Member | None, h.PermissibleGuildChannel | None, h.Permissions | None]:
    """Resolve the bot member + target channel once and compute the bot's perms.

    Returns the ``(member, channel, perms)`` bundle so callers (the enable gate and
    the diagnostics builder) can reuse a single resolution for both
    ``calculate_permissions`` and ``explain_missing_permission``. ``perms`` is
    ``None`` when undeterminable (no guild, unknown bot user, non-permissible channel,
    or a ``CacheFailureError`` from toolbox)."""
    member, channel = await _resolve_bot_member_channel(ctx)
    if member is None or channel is None:
        return member, channel, None
    try:
        return member, channel, calculate_permissions(member, channel)
    except CacheFailureError:
        return member, channel, None


async def compute_bot_perms(ctx: lb.Context) -> h.Permissions | None:
    """The bot's effective permissions in ``ctx``'s channel, or ``None`` when it can't
    be determined. Mirrors ``check_invoker_has_perms`` but for the bot's own member."""
    _, _, perms = await resolve_bot_perms(ctx)
    return perms


class DestVerdict(enum.Enum):
    """Whether the bot can *confirm* it cannot send to a destination channel.

    Only ``CONFIRMED_*`` verdicts may count toward the cross-run mirror auto-disable;
    ``SENDABLE`` and ``UNKNOWN`` must never count — the cost of wrongly disabling a
    healthy destination (silent loss of service) far outweighs leaving a dead one
    enabled (wasted API calls), so every ambiguity biases toward *not* disabling."""

    SENDABLE = enum.auto()  # bot holds View Channel + Send Messages here
    CONFIRMED_UNSENDABLE = enum.auto()  # computed perms lack View or Send
    CONFIRMED_GONE = enum.auto()  # channel/guild gone, bot can't see it, or was kicked
    UNKNOWN = enum.auto()  # couldn't determine — do NOT count toward disable


# Minimal perms genuinely required to send, computed on a thread's parent. A normal
# channel needs View + Send Messages; a thread needs View + Send Messages In Threads
# (the base Send Messages perm does not authorise posting inside a thread). Being *less*
# eager to confirm "unsendable" is the safe direction given the cost asymmetry.
_REQUIRED_SEND_PERMS = h.Permissions.VIEW_CHANNEL | h.Permissions.SEND_MESSAGES
_REQUIRED_THREAD_SEND_PERMS = (
    h.Permissions.VIEW_CHANNEL | h.Permissions.SEND_MESSAGES_IN_THREADS
)


async def _fetch_channel_or_gone(
    app: h.GatewayBot, channel_id: int
) -> h.PartialChannel | DestVerdict:
    """Cache-first channel fetch. Returns the channel, or ``CONFIRMED_GONE`` on a
    403/404 (the channel is gone or the bot can't see it). REST is hit only on a cache
    miss."""
    channel = app.cache.get_guild_channel(channel_id)
    if channel is not None:
        return channel
    try:
        return await app.rest.fetch_channel(channel_id)
    except (h.ForbiddenError, h.NotFoundError):
        return DestVerdict.CONFIRMED_GONE


async def confirm_dest_unsendable(app: h.GatewayBot, channel_id: int) -> DestVerdict:
    """Classify whether the bot can send to ``channel_id`` — WITHOUT sending anything.

    Computes the bot's effective channel permissions from the gateway cache (roles +
    overwrites), the same way ``compute_bot_perms`` does for command channels, and maps
    the result to a :class:`DestVerdict`. Gates the cross-run mirror auto-disable: a
    genuinely dead destination (deleted / kicked / perms revoked) reads ``CONFIRMED_*``
    regardless of how often we post to it, so the decision stops being hostage to post
    cadence. Every ambiguity maps to ``UNKNOWN`` (which never counts)."""
    me = app.get_me()
    if me is None:
        return DestVerdict.UNKNOWN

    channel = await _fetch_channel_or_gone(app, channel_id)
    if isinstance(channel, DestVerdict):
        return channel
    # A thread carries no overwrites of its own — resolve to the parent for perms, but
    # remember it was a thread so we require the in-thread send perm below.
    is_thread = isinstance(channel, h.GuildThreadChannel)
    if isinstance(channel, h.GuildThreadChannel):
        if channel.parent_id is None:
            return DestVerdict.UNKNOWN
        channel = await _fetch_channel_or_gone(app, channel.parent_id)
        if isinstance(channel, DestVerdict):
            return channel
    if not isinstance(channel, h.PermissibleGuildChannel):
        return DestVerdict.UNKNOWN

    member = app.cache.get_member(channel.guild_id, me.id)
    if member is None:
        try:
            member = await app.rest.fetch_member(channel.guild_id, me.id)
        except h.NotFoundError:
            return DestVerdict.CONFIRMED_GONE  # bot is no longer in the guild
        except h.ForbiddenError:
            return DestVerdict.UNKNOWN

    try:
        perms = calculate_permissions(member, channel)
    except CacheFailureError:
        return DestVerdict.UNKNOWN

    required = _REQUIRED_THREAD_SEND_PERMS if is_thread else _REQUIRED_SEND_PERMS
    if perms & required == required:
        return DestVerdict.SENDABLE
    return DestVerdict.CONFIRMED_UNSENDABLE


def filter_discord_autoembeds(msg: h.Message | HMessage) -> list[h.Embed]:
    content = msg.content or ""
    filtered_embeds: list[h.Embed] = []

    if not content:
        # If there is no content
        # there will be no autoembeds
        return list(msg.embeds)

    for embed in msg.embeds or []:
        embed: h.Embed
        embed_url = embed.url or ""
        if embed_url not in content and (
            embed.title
            or embed.description
            or embed.fields
            or embed.footer
            or embed.author
        ):
            filtered_embeds.append(embed)
    return filtered_embeds


type self_ = t.Any


@t.overload
def ignore_own_user(
    func: t.Callable[
        [self_, h.MessageCreateEvent | h.MessageUpdateEvent], t.Awaitable[None]
    ],
) -> t.Callable[
    [self_, h.MessageCreateEvent | h.MessageUpdateEvent], t.Awaitable[None]
]: ...


@t.overload
def ignore_own_user(
    func: t.Callable[[h.MessageCreateEvent], t.Coroutine[t.Any, t.Any, None]],
) -> t.Callable[[h.MessageCreateEvent], t.Coroutine[t.Any, t.Any, None]]: ...


@t.overload
def ignore_own_user(
    func: t.Callable[[h.MessageUpdateEvent], t.Coroutine[t.Any, t.Any, None]],
) -> t.Callable[[h.MessageUpdateEvent], t.Coroutine[t.Any, t.Any, None]]: ...


def ignore_own_user(
    func: t.Callable[..., t.Awaitable[None]],
) -> t.Callable[..., t.Awaitable[None]]:
    # Use *args so this works both as a plain event listener (event,) and as a
    # method decorator (self, event,) — inspect.ismethod is always False at class
    # definition time, so the distinction cannot be made at decoration time.
    async def _wrapped(*args: t.Any) -> None:
        event = args[-1]
        if not isinstance(event.app, h.GatewayBot):
            return
        own_user = event.app.get_me()
        if own_user and event.author_id == own_user.id:
            # Never respond to self or mirror self
            return
        await func(*args)

    return _wrapped


# --- a feed whose source channel is unusable -----------------------------------------
#
# Beacon's commands are the *reader* side of a followable: they page through, or repeat,
# whatever anchor (or a human) posted in the feed's Kyber announce channel. When that
# channel is unset, deleted, or unreadable, the command has nothing to show — but it is
# registered regardless, because a command that silently stops existing is indistin-
# guishable to a user from the bot being broken, and Discord keeps serving the stale
# command list anyway. So: register always, answer clearly, and page us — the source
# channel is on OUR side, so a user seeing this can do nothing about it and we can.
#
# Note this has no bearing on mirroring. The mirror system runs off MirroredChannel
# rows keyed by source channel id and is always on: anything posted in a watched
# announce channel is relayed, whatever these settings say. Only the commands degrade.

FEED_UNCONFIGURED = "no channel has been set for it yet"
FEED_UNREACHABLE = "its channel has been deleted, or the bot lost access to it"


async def feed_unavailable_embed(display_name: str, reason: str) -> h.Embed:
    """The user-facing answer for a command whose source channel is unusable."""
    from dd.common import settings

    return h.Embed(
        title=f"{display_name} isn't available right now",
        description=(
            f"This command reads from the {display_name} channel, and {reason}. "
            "The bot owners have been alerted — nothing to do on your end."
        ),
        color=await settings.get_embed_error_color(),
    )


async def open_feed_source[T](
    feed: str,
    open_source: t.Callable[[int], t.Awaitable[T]],
    *,
    alert_when_unset: bool = True,
) -> tuple[T | None, str | None]:
    """Open ``feed``'s source channel for a reader, or report why it can't be.

    ``open_source`` is handed the configured channel id and does whatever "open"
    means for that reader — fetch the channel, build a :class:`NavPages` over its
    history, ... — so every reader shares one definition of the two ways a source
    can be unusable: never configured, or configured and since deleted / no longer
    visible to the bot (a 404/403 out of ``open_source``).

    Returns ``(opened, None)`` on success and ``(None, reason)`` otherwise, where
    ``reason`` is a ``FEED_*`` string the caller records for its command to answer
    with. Nothing raises: the callers are ``StartedEvent``/message listeners, where an
    exception is reported nowhere and simply loses the feed silently. Instead this
    logs CRITICAL, which forwards to the alerts channel AND pings the bot owner(s) —
    same rationale as ``autoposts.resolve_followable_channel``, whose docstring has
    the long version.

    ``alert_when_unset`` is False for a reader that re-opens its source on later
    events too: the unset state was already paged for once at import by
    ``resolve_followable_channel``, and re-paging for a state nobody has changed
    since adds nothing.
    """
    from dd.common import (
        feeds as dd_feeds,
        settings,
    )

    channel_id = await settings.get_followable_channel(feed)
    if not channel_id:
        if alert_when_unset:
            logger.critical(
                "%s has no channel set — its commands will answer 'unavailable' until "
                "one is picked on the Autopost Settings page.",
                dd_feeds.display_name(feed),
            )
        return None, FEED_UNCONFIGURED

    try:
        return await open_source(channel_id), None
    except (h.NotFoundError, h.ForbiddenError):
        # Configured, but deleted or the bot lost access since — this used to surface
        # as an unhandled exception in a listener (easy to miss; nothing else reports
        # it). A channel vanishing out from under a configured feed is exactly the
        # kind of silent regression nobody watches for on their own, hence CRITICAL.
        logger.critical(
            "%s channel %s is configured but no longer reachable (deleted, or the bot "
            "lost access) — its commands will answer 'unavailable' until it's fixed "
            "on the Autopost Settings page.",
            dd_feeds.display_name(feed),
            channel_id,
        )
        return None, FEED_UNREACHABLE
