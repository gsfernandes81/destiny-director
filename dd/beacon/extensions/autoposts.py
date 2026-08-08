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
import logging
from dataclasses import dataclass

import hikari as h
import lightbulb as lb
from sqlalchemy.ext.asyncio import AsyncSession

from ...common import settings
from ...common.auth import check_invoker_is_owner
from ...common.bot import CachedFetchBot
from ...common.schemas import MirroredChannel, db_session
from ...common.utils import discord_error_logger
from .. import utils
from . import mirror_tracing

logger = logging.getLogger(__name__)

loader = lb.Loader()

# Permissions that allow users to manage autoposts in a guild
end_user_allowed_perms = [
    h.Permissions.MANAGE_WEBHOOKS,
    h.Permissions.MANAGE_GUILD,
    h.Permissions.MANAGE_CHANNELS,
    h.Permissions.ADMINISTRATOR,
]

# Shared command group that every followable module attaches a subcommand to.
autopost_command_group = lb.Group(
    "autopost",
    "Autopost control (mention a role with ping_role when enabling to also ping it)",
)


def set_owners_footer(embed: h.Embed, bot_owners: list[h.User]) -> h.Embed:
    """Set an embed footer listing the bot owner(s) as points of contact.

    The footer text lists every owner's username; the icon uses the primary
    (first) owner's avatar since footers only support a single icon."""
    return embed.set_footer(
        ", ".join(f"@{owner.username}" for owner in bot_owners),
        icon=bot_owners[0].display_avatar_url,
    )


@dataclass(frozen=True, slots=True)
class AutopostPerm:
    """One permission the bot wants in an autopost target, and why."""

    permission: h.Permissions
    label: str
    required: bool  # required → gates enable; advisory → shown but never blocks
    why: str  # static fallback reason when the specific block-source is unknown


# Single source of truth for the perms an autopost target needs. View Channel and Send
# Messages (both delivery paths post as the bot) are hard requirements. Manage Webhooks
# is required *only where the webhook-follow delivery path can actually run* — see
# ``for_channel``, which drops it for legacy-only targets (threads, forums, …). Embed
# Links stays advisory (embeds still send without it, links just don't render).
_AUTOPOST_PERMS: list[AutopostPerm] = [
    AutopostPerm(
        h.Permissions.VIEW_CHANNEL,
        "View Channel",
        True,
        "I can't see the channel without it",
    ),
    AutopostPerm(
        h.Permissions.SEND_MESSAGES,
        "Send Messages",
        True,
        "I can't post autoposts without it",
    ),
    AutopostPerm(
        h.Permissions.EMBED_LINKS,
        "Embed Links",
        False,
        "embeds won't render without it",
    ),
    AutopostPerm(
        h.Permissions.MANAGE_WEBHOOKS,
        "Manage Webhooks",
        True,
        "the webhook-follow delivery path needs it",
    ),
]


# Discord's channel-follow creates a follower webhook in the target, which only
# standard text channels support; threads, forums, media, voice/stage and
# announce-as-target reject it (50024 → NEEDS_LEGACY). Decide proactively by type.
_WEBHOOK_FOLLOW_TARGET_TYPES = frozenset({h.ChannelType.GUILD_TEXT})


def _supports_webhook_follow(channel: h.PartialChannel) -> bool:
    """Whether Discord's webhook-follow works for this target's channel type."""
    return channel.type in _WEBHOOK_FOLLOW_TARGET_TYPES


def for_channel(channel: h.PartialChannel) -> list[AutopostPerm]:
    """The perms table for a specific target.

    Two per-target adjustments to the base table:

    * **Threads** deliver via ``channel.send()`` into the thread, which Discord gates on
      **Send Messages in Threads alone** (resolved against the parent) — the base Send
      Messages perm does not authorise posting inside a thread. So a thread *swaps* Send
      Messages for Send Messages in Threads rather than requiring both; requiring both
      false-blocks a locked parent that grants only Send-in-Threads. Matches
      ``utils._REQUIRED_THREAD_SEND_PERMS`` used by the reachability sweep.

    * **Manage Webhooks is dropped for legacy-only targets.** A target Discord can't
      follow into (``not _supports_webhook_follow`` — threads, forums, media, …) always
      delivers via the legacy bot-post path, which needs no webhook. Requiring Manage
      Webhooks there would false-block a working setup, so it's removed from the table
      for those targets (and from the disable path too — see ``disable_mirror``)."""
    supports_follow = _supports_webhook_follow(channel)
    is_thread = isinstance(channel, h.GuildThreadChannel)
    table: list[AutopostPerm] = []
    for perm in _AUTOPOST_PERMS:
        if perm.permission == h.Permissions.MANAGE_WEBHOOKS and not supports_follow:
            continue  # legacy-only target → the webhook path can never run here
        if is_thread and perm.permission == h.Permissions.SEND_MESSAGES:
            table.append(
                AutopostPerm(
                    h.Permissions.SEND_MESSAGES_IN_THREADS,
                    "Send Messages in Threads",
                    True,
                    "posting into a thread needs it",
                )
            )
            continue
        table.append(perm)
    return table


@dataclass(frozen=True, slots=True)
class PermStatus:
    """Per-permission result for the diagnostics embed.

    ``determinable`` is False when the bot couldn't work the permission out at all (e.g.
    it can't even see the channel) — rendered as ❓ rather than ✅/❌.
    """

    perm: AutopostPerm
    granted: bool
    block_source: str | None  # from explain_missing_permission; may be None
    determinable: bool = True


def build_perm_statuses(
    perms: h.Permissions | None,
    perm_channel: h.PermissibleGuildChannel | None,
    member: h.Member | None,
    target_channel: h.PartialChannel | None,
    view_channel_missing: bool = False,
) -> list[PermStatus]:
    """Turn resolved bot perms into a per-permission checklist.

    ``target_channel`` selects the perms table (thread → adds Send-in-Threads);
    ``perm_channel`` (thread → parent) + ``member`` drive the best-effort block-source
    for each missing **required** perm. When ``perms`` is ``None`` the perms couldn't be
    computed, so each entry is **undeterminable** (❓) — except that when
    ``view_channel_missing`` is set (the bot can't even see the channel), View Channel
    is a definite ❌ while the *other* perms stay undeterminable."""
    table = (
        for_channel(target_channel)
        if target_channel is not None
        else list(_AUTOPOST_PERMS)
    )
    statuses: list[PermStatus] = []
    for entry in table:
        if perms is not None:
            determinable = True
            granted = entry.permission & perms == entry.permission
        elif view_channel_missing and entry.permission == h.Permissions.VIEW_CHANNEL:
            determinable = True  # a 403 on fetch proves View Channel is missing
            granted = False
        else:
            determinable = False  # can't tell without seeing the channel
            granted = False

        block_source: str | None = None
        if (
            determinable
            and not granted
            and entry.required
            and perms is not None
            and perm_channel is not None
            and member is not None
        ):
            block_source = utils.explain_missing_permission(
                member, perm_channel, entry.permission
            )
        statuses.append(PermStatus(entry, granted, block_source, determinable))
    return statuses


def permission_error_embed(
    bot_owners: list[h.User],
    statuses: list[PermStatus],
    perms_known: bool,
) -> h.Embed:
    """The permission-diagnostics embed: a ✅/❌ checklist of the perms the bot needs
    here, a ``└`` block-source line under each missing required perm, and the bot
    owner(s) as points of contact. When ``perms_known`` is False the bot couldn't read
    its own perms, so a note is appended."""
    lines: list[str] = []
    for status in statuses:
        if not status.determinable:
            mark = "❓"
        elif status.granted:
            mark = "✅"
        else:
            mark = "❌"
        suffix = "" if status.perm.required else " (recommended)"
        lines.append(f"{mark} {status.perm.label}{suffix}")
        if status.determinable and not status.granted and status.perm.required:
            lines.append(f"    └ {status.block_source or status.perm.why}")

    description = (
        "I need these permissions in this channel to autopost here:\n\n"
        + "\n".join(lines)
    )
    if not perms_known:
        description += (
            "\n\nI couldn't read my own permissions here — am I fully in this server?"
        )

    return set_owners_footer(
        h.Embed(
            title="Permission Error",
            description=description,
            color=settings.get_embed_error_color_sync(),
        ),
        bot_owners,
    )


class MirrorOutcome(enum.Enum):
    """Classification of a hikari error hit while (un)following a channel."""

    MISSING_PERMS = enum.auto()  # bot lacks channel perms → tell the user
    NEEDS_LEGACY = enum.auto()  # announce channel → use a legacy mirror instead
    CHANNEL_GONE = enum.auto()  # no such channel/webhook (e.g. forum) → ignore
    # proactive gate: bot can't post here → refuse enable (never a Discord code)
    BOT_MISSING_SEND = enum.auto()
    OTHER = enum.auto()  # unclassified → re-raise


# Discord JSON error codes — stable across wording/locale/version, unlike the message
# text the old substring matching relied on.
_OUTCOME_BY_CODE: dict[int, MirrorOutcome] = {
    50001: MirrorOutcome.MISSING_PERMS,  # Missing Access
    50013: MirrorOutcome.MISSING_PERMS,  # Missing Permissions
    50024: MirrorOutcome.NEEDS_LEGACY,  # Cannot execute action on this channel type
    10003: MirrorOutcome.CHANNEL_GONE,  # Unknown Channel
}


def classify_mirror_error(exc: BaseException) -> MirrorOutcome:
    """Classify a hikari error by its Discord error code into a MirrorOutcome.

    Code-based, so it no longer breaks when Discord changes the message wording,
    locale, or API version. A non-hikari error (no ``code``) classifies as OTHER."""
    return _OUTCOME_BY_CODE.get(getattr(exc, "code", None), MirrorOutcome.OTHER)


async def _fetch_target_and_view_state(
    bot: CachedFetchBot, channel_id: int
) -> tuple[h.PartialChannel | None, bool]:
    """Fetch the target channel and report whether *View Channel* is the blocker.

    A 403 means the bot can't see the channel (no View Channel) → ``(None, True)``; any
    other fetch failure is undeterminable → ``(None, False)``. Goes to REST so a 403 is
    surfaced reliably rather than masked by a stale cache entry."""
    try:
        return await bot.rest.fetch_channel(channel_id), False
    except h.ForbiddenError:
        return None, True
    except h.HikariError:
        return None, False


async def respond_missing_perms(ctx: lb.Context, bot: CachedFetchBot) -> None:
    """Reply with the permission-diagnostics embed: a ✅/❌/❓ checklist of the perms
    the bot needs here, what's blocking each missing required one, and the bot
    owner(s) as points of contact. Shared by all three 'bot lacks perms' paths
    (Preflight 1's fetch 403, the proactive gate, and the reactive MISSING_PERMS
    catch)."""
    member, perm_channel, perms = await utils.resolve_bot_perms(ctx)
    target_channel, view_channel_missing = await _fetch_target_and_view_state(
        bot, ctx.channel_id
    )
    statuses = build_perm_statuses(
        perms, perm_channel, member, target_channel, view_channel_missing
    )
    await ctx.respond(
        permission_error_embed(
            await bot.fetch_owners(), statuses, perms_known=perms is not None
        )
    )


async def enable_non_legacy_mirror(
    bot: CachedFetchBot, followable_channel: int, ctx: lb.Context, session: AsyncSession
):
    await bot.rest.follow_channel(followable_channel, ctx.channel_id)
    if ctx.guild_id is None:
        raise RuntimeError("This command can only be used in a server.")
    await MirroredChannel.add_mirror(
        followable_channel,
        ctx.channel_id,
        ctx.guild_id,
        False,
        session=session,
    )


async def enable_legacy_mirror(
    bot: CachedFetchBot,
    followable_channel: int,
    ctx: lb.Context,
    role: h.Role | None,
    session: AsyncSession,
):
    channel = await bot.fetch_channel(ctx.channel_id)
    if not isinstance(channel, h.TextableChannel):
        raise TypeError(f"Channel {ctx.channel_id} is not a textable channel")

    if ctx.guild_id is None:
        raise RuntimeError("This command can only be used in a server.")
    await MirroredChannel.add_mirror(
        followable_channel,
        ctx.channel_id,
        ctx.guild_id,
        True,
        role_mention_id=(role.id if role else 0),
        session=session,
    )


async def unfollow_channel(bot: CachedFetchBot, news_channel: int, target_channel: int):
    for hook in await bot.rest.fetch_channel_webhooks(target_channel):
        if (
            isinstance(hook, h.ChannelFollowerWebhook)
            and hook.source_channel
            and hook.source_channel.id == news_channel
        ):
            await bot.rest.delete_webhook(hook)


async def disable_mirror(
    ctx: lb.Context, followable_channel: int, bot: CachedFetchBot, session: AsyncSession
):
    # Check if this is a legacy mirror, and if so, remove it and return
    if int(ctx.channel_id) in (await MirroredChannel.fetch_dests(followable_channel)):
        await MirroredChannel.remove_mirror(
            followable_channel, ctx.channel_id, session=session
        )

    else:
        # If this is not a legacy mirror, then we need to delete the follow webhook.
        # Fetch and delete follow-based webhooks that name our channel as a source.
        #
        # Deleting the webhook needs Manage Webhooks. If the bot has since lost it, we
        # must STILL drop our record — never gate `disable`, so a broken autopost is
        # always removable. A missing-perms failure here is swallowed (logged); the
        # orphaned webhook can be cleaned up later once perms return (or by a guild
        # admin), but the mirror is gone from our side either way.
        try:
            await unfollow_channel(
                bot=bot, news_channel=followable_channel, target_channel=ctx.channel_id
            )
        except h.HikariError as e:
            if classify_mirror_error(e) is not MirrorOutcome.MISSING_PERMS:
                raise
            logger.warning(
                "Disabling mirror %s→%s: can't delete the follow webhook (missing "
                "Manage Webhooks); removing the record anyway, webhook left orphaned.",
                followable_channel,
                ctx.channel_id,
            )

        # Also remove the mirror
        await MirroredChannel.remove_mirror(
            followable_channel, ctx.channel_id, session=session
        )
        # Drop it from the in-memory crosspost trace cache so it doesn't leak or go
        # stale (the tracer would otherwise re-add a mirror just removed).
        mirror_tracing.forget_traced_mirror(followable_channel, int(ctx.channel_id))


def insufficient_permissions_embed(bot_owners: list[h.User]) -> h.Embed:
    return set_owners_footer(
        h.Embed(
            title="Insufficient permissions",
            description="You have insufficient permissions to use this command.\n"
            + "Any one of the following permissions is needed:\n```\n"
            + "- Manage Webhooks\n"
            + "- Manage Guild\n"
            + "- Manage Channel\n"
            + "- Administrator\n```\n"
            + "Make sure that you have this permission in this channel and not "
            + "just in this guild\n"
            + "Feel free to contact me on discord if you are having issues!\n",
            color=settings.get_embed_error_color_sync(),
        ),
        bot_owners,
    )


def autopost_error_embed(bot_owners: list[h.User], error_reference: str) -> h.Embed:
    return set_owners_footer(
        h.Embed(
            title="Pardon our dust!",
            description="An error occurred while trying to update autopost settings. "
            + "Please contact me **(username at the bottom of the embed)** with the "
            + f"error reference `{error_reference}` and we will fix this for you.",
            color=settings.get_embed_error_color_sync(),
        ),
        bot_owners,
    )


async def _respond_autopost_error(
    ctx: lb.Context, bot: CachedFetchBot, e: Exception
) -> None:
    error_reference = await discord_error_logger(e, operation="Autopost")
    await ctx.respond(autopost_error_embed(await bot.fetch_owners(), error_reference))


async def _drop_existing_follow(
    bot: CachedFetchBot, followable_channel: int, target_channel: int
) -> None:
    """Remove any existing follow webhook when switching to a legacy mirror.

    A forum channel has no webhooks to remove (Unknown Channel → CHANNEL_GONE) and is
    ignored; a permissions error propagates so the caller can report MISSING_PERMS."""
    try:
        await unfollow_channel(bot, followable_channel, target_channel)
    except h.NotFoundError as e:
        if classify_mirror_error(e) is not MirrorOutcome.CHANNEL_GONE:
            raise


async def _enable_autopost(
    bot: CachedFetchBot,
    followable_channel: int,
    ctx: lb.Context,
    ping_role: h.Role | None,
    session: AsyncSession,
    target_channel: h.PartialChannel,
) -> None:
    """Enable an autopost mirror for ``ctx``'s channel.

    New-style (follow-webhook) mirrors can't ping roles, so when a ping role is
    requested we go straight to a legacy mirror and drop any prior follow webhook.
    Non-text targets can't be followed either, so they go straight to legacy (no follow
    webhook to drop — they never had one). Otherwise we try a new-style mirror first and
    keep the reactive NEEDS_LEGACY fallback as a safety net."""
    if ping_role:
        # Drop any prior follow webhook FIRST, then record the legacy mirror. The drop
        # is perms-gated (Manage Webhooks); doing it before the DB write keeps enable
        # atomic — a failure surfaces the error *without* leaving a half-applied mirror
        # row committed. Only follow-capable targets could hold a follow webhook, so
        # legacy-only targets (threads, forums, …) skip the drop entirely (they never
        # had one, and no longer require Manage Webhooks — see ``for_channel``).
        if _supports_webhook_follow(target_channel):
            await _drop_existing_follow(bot, followable_channel, ctx.channel_id)
        await enable_legacy_mirror(bot, followable_channel, ctx, ping_role, session)
        return

    if not _supports_webhook_follow(target_channel):
        await enable_legacy_mirror(bot, followable_channel, ctx, ping_role, session)
        return

    try:
        await enable_non_legacy_mirror(bot, followable_channel, ctx, session)
    except h.BadRequestError as e:
        # An announce (news) channel can't be followed into (50024 → NEEDS_LEGACY) —
        # degrade to a legacy mirror. Manage Webhooks is a hard requirement gated by
        # Preflight 3, so a missing-webhooks 403 no longer reaches here; any other error
        # is real and must surface (a MISSING_PERMS 403 propagates to the reactive
        # handler, which reports it accurately).
        if classify_mirror_error(e) is not MirrorOutcome.NEEDS_LEGACY:
            raise
        await enable_legacy_mirror(bot, followable_channel, ctx, ping_role, session)


def follow_control_command_maker(
    followable_channel: int,
    autoposts_name: str,
    autoposts_friendly_name: str,
    autoposts_desc: str,
):
    """Create a follow control command for a given followable channel

    Args:
        followable_channel (int): The channel ID of the followable channel
        autoposts_name (str): The name of the autoposts command
        autoposts_friendly_name (str): The friendly name to show users for
            the autoposts command. Must be singular and correctly capitalized
            ie first letter capitalized, rest lower case
        autoposts_desc (str): The description for the autoposts command
    """

    @autopost_command_group.register
    class FollowControl(
        lb.SlashCommand, name=autoposts_name, description=autoposts_desc
    ):
        # Note: Type int (not bool) is used so the choice names appear for the
        # user, matching the lightbulb v2 behaviour.
        option = lb.integer(
            "option",
            "Enabled or disabled",
            choices=[lb.Choice("Enable", 1), lb.Choice("Disable", 0)],
            default=1,
        )
        ping_role = lb.role(
            "ping_role",
            "An optional role to ping when autoposting",
            default=None,
        )

        @lb.invoke
        async def invoke(self, ctx: lb.Context, bot: CachedFetchBot = lb.di.INJECTED):
            await ctx.defer()

            enabling = bool(self.option)
            ping_role: h.Role | None = self.ping_role

            async with db_session() as session, session.begin():
                try:
                    # Preflight 1 — resolve the target channel once, up front. Use the
                    # REST (not the cache): a stale cache can mask a later permissions
                    # change and make the invoker-perms check pass spuriously. A 403
                    # means the bot can't see the channel (no View Channel). This single
                    # fetch is reused by Preflight 3 so the target isn't fetched twice.
                    (
                        target_channel,
                        view_channel_missing,
                    ) = await _fetch_target_and_view_state(bot, ctx.channel_id)
                    if view_channel_missing:
                        await respond_missing_perms(ctx, bot)
                        return

                    # Preflight 2 — the invoker must own the bot or hold a managing
                    # permission in this channel.
                    if not (
                        await check_invoker_is_owner(ctx)
                        or await utils.check_invoker_has_perms(
                            ctx, end_user_allowed_perms
                        )
                    ):
                        await ctx.respond(
                            insufficient_permissions_embed(await bot.fetch_owners())
                        )
                        return

                    # Apply the change. NEEDS_LEGACY (announce channel) and
                    # CHANNEL_GONE (forum webhooks) are resolved inside the helpers;
                    # only MISSING_PERMS and unclassified errors surface here.
                    if enabling:
                        # Preflight 3 — the bot must actually be able to post here (both
                        # delivery paths post as the bot). Reuse the Preflight-1 target
                        # fetch; only the bot's own perms still need resolving. Enable-
                        # only: never gate `disable`, so a broken autopost is always
                        # removable.
                        member, perm_channel, perms = await utils.resolve_bot_perms(ctx)
                        statuses = build_perm_statuses(
                            perms,
                            perm_channel,
                            member,
                            target_channel,
                            view_channel_missing,
                        )
                        perms_known = perms is not None
                        missing_required = any(
                            s.determinable and not s.granted and s.perm.required
                            for s in statuses
                        )
                        if not perms_known or missing_required:
                            # MirrorOutcome.BOT_MISSING_SEND — do NOT add the mirror.
                            await ctx.respond(
                                permission_error_embed(
                                    await bot.fetch_owners(), statuses, perms_known
                                )
                            )
                            return

                        if target_channel is None:
                            # Perms resolved, but the Preflight-1 target fetch failed
                            # transiently (a non-403 error → (None, False)). We can't
                            # enable without the channel object; report a retryable
                            # error instead of bare-returning after defer() and hanging
                            # the interaction on "thinking…" forever.
                            await _respond_autopost_error(
                                ctx,
                                bot,
                                RuntimeError(
                                    f"Could not fetch target channel {ctx.channel_id}"
                                ),
                            )
                            return
                        await _enable_autopost(
                            bot,
                            followable_channel,
                            ctx,
                            ping_role,
                            session,
                            target_channel,
                        )
                    else:
                        await disable_mirror(ctx, followable_channel, bot, session)
                except Exception as e:
                    if classify_mirror_error(e) is MirrorOutcome.MISSING_PERMS:
                        await respond_missing_perms(ctx, bot)
                        return
                    await _respond_autopost_error(ctx, bot, e)
                    raise
                else:
                    await ctx.respond(
                        h.Embed(
                            title=f"{autoposts_friendly_name} autoposts "
                            + ("enabled" if enabling else "disabled")
                            + "!",
                            color=await settings.get_embed_default_color(),
                        )
                    )

    return FollowControl


loader.command(autopost_command_group)
