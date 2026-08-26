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

# DB-backed dynamic user-command system, ported from the lightbulb v2 implementation
# (see git history of beacon/modules/user_commands.py and bot.UserCommandBot).
#
# The admin ``/command`` group (preview/add/delete/edit/rename) lives in the control
# guild and lets the bot owner manage rows in the ``common.schemas.UserCommand`` table.
# ``resync_user_commands`` then builds lightbulb v3 command/group objects from those
# rows and (re)registers them globally on the live client.

import asyncio
import collections
import json
import logging
import traceback as tb
import typing as t

import hikari as h
import lightbulb as lb
from sqlalchemy.ext.asyncio import AsyncSession

from ...common import cfg, settings
from ...common.auth import NotBotOwnerError, owner_only
from ...common.bot import CachedFetchBot
from ...common.components import rebuild_components
from ...common.discord_logging import log_command_failure
from ...common.schemas import UserCommand, db_session
from ...common.utils import (
    FriendlyValueError,
    follow_link_single_step,
    guild_scope,
    parse_message_link,
)

logger = logging.getLogger(__name__)

loader = lb.Loader()

# When False the "Embed" (response_type 3) choice is hidden from the admin commands,
# matching the v2 EMBEDS_FEATURE_FLAG behaviour. The response handler still supports
# rendering embeds for rows that already have response_type 3.
EMBEDS_FEATURE_FLAG = False

NOTE_ABOUT_SLOW_DISCORD_PROPAGATION = (
    "\nNote:\n"
    + "Discord propagates command changes slowly so it may take a few minutes for "
    + "changes to take effect."
)

# Registry of dynamic command/group objects currently registered on the client,
# keyed by their layer-name tuple. Used to unregister them on the next resync.
_registered_commands: dict[tuple[str, ...], t.Any] = {}


def _layers_repr(*layers: str) -> str:
    """Render the non-empty layer names as ``a -> b -> c`` (v2 formatting)."""
    return " -> ".join(layer for layer in layers if layer != "")


def _friendly_error_message(e: Exception) -> str:
    """Format the FriendlyValueError message block exactly as v2 did."""
    return (
        ("\nError message:\n" + "\n".join(e.args) + "\n")
        if isinstance(e, FriendlyValueError)
        else ""
    )


# Discord rejects message content longer than 2000 characters; error responses
# inline a full traceback, so they must be truncated to fit.
DISCORD_MESSAGE_LIMIT = 2000


def _truncate_middle(text: str, limit: int) -> str:
    """Shorten ``text`` to at most ``limit`` chars, keeping the head and tail and
    marking the elision. The head shows where an error began and the tail shows
    the actual exception, which is the most useful part of a traceback."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    marker = "\n…(truncated)…\n"
    if limit <= len(marker):
        return text[:limit]
    keep = limit - len(marker)
    head = keep // 2
    return text[:head] + marker + text[-(keep - head) :]


def _error_response(header: str, e: Exception, reserve: int = 0) -> str:
    """Assemble the full error response (header + friendly message + traceback).

    Matches the v2 layout: a header line, the FriendlyValueError message (if any),
    then the formatted traceback in a code block. The traceback is truncated so the
    whole response — minus ``reserve`` chars left free for content the caller
    appends — fits within Discord's message-length limit."""
    prefix = header + _friendly_error_message(e) + "\n Error trace:\n```"
    suffix = "\n```"
    trace = "\n".join(tb.format_exception(e))
    budget = DISCORD_MESSAGE_LIMIT - reserve - len(prefix) - len(suffix)
    return prefix + _truncate_middle(trace, budget) + suffix


@loader.error_handler
async def _on_command_error(
    exc: lb.exceptions.ExecutionPipelineFailedException,
) -> bool:
    """Error handler for the admin ``/command`` group.

    Renders the v2-style traceback embed (which also surfaces any
    ``FriendlyValueError`` message). Each ``/command`` subcommand's ``invoke``
    therefore no longer needs its own try/except. Returns ``False`` for commands
    outside this group, and for the owner-gate rejection (handled uniformly by the
    shared client-level ``owner_check_error_handler``), so those fall through."""
    ctx = exc.context
    # Scope by command identity, not name, so a dynamic user command that happens
    # to be named "command" can't be mistaken for the admin group.
    if not isinstance(
        ctx.command,
        (PreviewCommand, AddCommand, DeleteCommand, EditCommand, RenameCommand),
    ):
        return False

    cause = exc.causes[0] if exc.causes else exc
    # Owner rejections are rendered by the shared owner_check_error_handler.
    if isinstance(cause, NotBotOwnerError):
        return False

    name, _ = log_command_failure(exc, logger=logger)
    await ctx.respond(_error_response(f"An error occured running `/{name}`.\n", cause))
    return True


# --------------------------------------------------------------------------------------
# Building v3 command objects from UserCommand rows
# --------------------------------------------------------------------------------------


def _copy_response_kwargs(msg: h.Message) -> dict[str, t.Any]:
    """The ``ctx.respond`` kwargs that re-send ``msg`` verbatim (response type 2).

    Two things a fetched message needs before it can be sent back out:

    - **Components are models, not builders.** hikari's send path calls ``build()`` on
      every entry of ``components``, and a fetched message exposes
      :class:`hikari.PartialComponent` models, which have no such method. Passing them
      through raised ``AttributeError: 'ContainerComponent' object has no attribute
      'build'`` — so every copy command whose source carried *any* component (a
      Components V2 post, or a plain message with a link-button row) failed outright.
      :func:`rebuild_components` turns them back into sendable builders.
    - **A Components V2 source is components-only.** It carries no content and no
      embeds — Discord treats them as mutually exclusive — and must go out with the
      ``IS_COMPONENTS_V2`` flag, so it gets a payload of its own rather than the
      content/embeds/attachments one.
    """
    components = rebuild_components(msg.components) if msg.components else []
    if h.MessageFlag.IS_COMPONENTS_V2 in (msg.flags or h.MessageFlag.NONE):
        return {"components": components, "flags": h.MessageFlag.IS_COMPONENTS_V2}
    return {
        "content": msg.content,
        "embeds": msg.embeds,
        "components": components,
        "attachments": msg.attachments,
    }


def _user_command_response_func_builder(
    cmd: UserCommand,
) -> t.Callable[[t.Any, lb.Context], t.Awaitable[None]]:
    """Build the ``invoke`` coroutine implementing a row's response behaviour.

    The three response types mirror the v2 ``_user_command_response_func_builder``."""

    if cmd.response_type == 0:

        async def handler(self: t.Any, ctx: lb.Context) -> None:
            # Command groups have no response of their own
            pass

    elif cmd.response_type == 1:

        async def handler(self: t.Any, ctx: lb.Context) -> None:
            text = cmd.response_data.strip()
            # Follow redirects once if any, then substitute these urls back into
            # the text and respond with it. Resolve concurrently (gather keeps
            # input order, so the regex-callback substitution still lines up). A
            # regex-callback (rather than str.format) keeps any literal braces in
            # the user's text inert.
            links = cfg.url_regex.findall(text)
            followed = iter(
                await asyncio.gather(*(follow_link_single_step(link) for link in links))
            )
            substituted = cfg.url_regex.sub(lambda _m: next(followed), text)
            # Only attach the "See more" button when there is somewhere to send people:
            # default_url is a DB setting now, not the env var prod always populated, so
            # a fresh install (its _DEFAULTS entry is "") or a clear on the settings
            # page legitimately leaves it blank. hikari does no url validation — it
            # would put `"url": ""` in the payload — and Discord rejects that. That
            # fails the *entire* response rather than dropping the button, i.e. every
            # text user command at once. A response missing its button is far better.
            default_url = await settings.get_default_url()
            components: h.UndefinedOr[list[h.api.ComponentBuilder]] = h.UNDEFINED
            if default_url:
                components = [
                    h.impl.MessageActionRowBuilder().add_link_button(
                        default_url, label="See more on Kyber's Corner!"
                    )
                ]
            await ctx.respond(substituted, components=components)

    elif cmd.response_type == 2:

        async def handler(self: t.Any, ctx: lb.Context) -> None:
            bot = t.cast(CachedFetchBot, ctx.client.app)
            channel_id, message_id = parse_message_link(str(cmd.response_data))
            # Bypass the cache (use raw REST) so a copied message always reflects the
            # live source, as in v2.
            msg_to_respond_with = await bot.rest.fetch_message(channel_id, message_id)
            await ctx.respond(**_copy_response_kwargs(msg_to_respond_with))

    elif cmd.response_type == 3:
        embed_kwargs = json.loads(cmd.response_data)
        embed_kwargs["color"] = (
            embed_kwargs.get("color") or settings.get_embed_default_color_sync()
        )

        async def handler(self: t.Any, ctx: lb.Context) -> None:
            kwargs = dict(embed_kwargs)
            image = kwargs.pop("image", None)
            embed = h.Embed(**kwargs)
            if image:
                image = await follow_link_single_step(image)
                embed.set_image(image)
            await ctx.respond(embed)

    else:
        raise FriendlyValueError(f"Unknown response type {cmd.response_type}")

    return handler


def _build_slash_command(cmd: UserCommand) -> type[lb.SlashCommand]:
    """Create a dynamic ``lb.SlashCommand`` subclass for a depth-N command row."""
    handler = _user_command_response_func_builder(cmd)
    name = cmd.ln_names[-1]
    class_name = "UserCommand_" + "_".join(cmd.ln_names)
    # Build the class through lightbulb's command metaclass (type(SlashCommand)),
    # passing name/description as metaclass kwargs the way ``class X(SlashCommand,
    # name=...)`` would. The metaclass is typed as returning CommandBase, so cast
    # back to the concrete SlashCommand subclass we constructed.
    cls = type(lb.SlashCommand)(
        class_name,
        (lb.SlashCommand,),
        {"invoke": lb.invoke(handler)},
        name=name,
        description=cmd.description or name,
    )
    return t.cast(type[lb.SlashCommand], cls)


def _build_command_objects(
    command_groups: list[UserCommand], commands: list[UserCommand]
) -> dict[tuple[str, ...], lb.Group | type[lb.SlashCommand]]:
    """Build all top-level command/group objects from the supplied rows.

    Returns a mapping of layer-name tuple -> top-level object suitable for
    ``client.register``. Subcommands/subgroups are attached onto their parent
    groups rather than returned separately."""

    # Top-level objects to register, keyed by their l1 name.
    top_level: dict[str, lb.Group | type[lb.SlashCommand]] = {}
    # All groups/subgroups keyed by their full layer tuple, used as registration
    # parents for deeper layers.
    groups: dict[tuple[str, ...], lb.Group | lb.SubGroup] = {}

    # command_groups come ordered by l1, l2, l3 so parents precede children.
    for cmd in command_groups:
        if cmd.depth == 1:
            group = lb.Group(cmd.l1_name, cmd.description or cmd.l1_name)
            top_level[cmd.l1_name] = group
            groups[(cmd.l1_name,)] = group
        elif cmd.depth == 2:
            parent = groups.get((cmd.l1_name,))
            if not isinstance(parent, lb.Group):
                raise FriendlyValueError(
                    f"{cmd.l1_name} is not an existing command group"
                )
            subgroup = parent.subgroup(cmd.l2_name, cmd.description or cmd.l2_name)
            groups[(cmd.l1_name, cmd.l2_name)] = subgroup

    for cmd in commands:
        cls = _build_slash_command(cmd)
        if cmd.depth == 1:
            top_level[cmd.l1_name] = cls
        else:
            parent = groups.get(tuple(cmd.ln_names[:-1]))
            if parent is None:
                raise FriendlyValueError(
                    f"{_layers_repr(*cmd.ln_names[:-1])} is not an existing "
                    + "command group"
                )
            parent.register(cls)

    return {(name,): obj for name, obj in top_level.items()}


# --------------------------------------------------------------------------------------
# Runtime registration / resync
# --------------------------------------------------------------------------------------


def _code_defined_command_names(client: lb.Client) -> set[str]:
    """Top-level names of commands defined in code (i.e. not DB-backed).

    Everything registered on the client that is not one of the dynamic, DB-backed
    objects we track in ``_registered_commands`` is code-defined. Used both to stop
    ``/command`` from shadowing a built-in command (see ``_warn_if_code_defined``)
    and to detect clashes during resync.
    """
    dynamic = set(_registered_commands.values())
    names: set[str] = set()
    for cmd in client.registered_commands:
        if cmd in dynamic:
            continue
        data = lb.utils.get_command_data(cmd)
        if data.name:
            names.add(str(data.name))
    return names


async def _warn_if_code_defined(ctx: lb.Context, layer1: str) -> bool:
    """If ``layer1`` names a code-defined command, warn the user and return ``True``.

    ``/command`` only manages DB-backed commands. A built-in command defined in code
    must not be added over, edited or deleted through it: there is no DB row to act
    on, and creating one would only produce a clash that ``resync_user_commands``
    then refuses to register. Callers should return early when this returns ``True``.
    """
    if layer1 and layer1 in _code_defined_command_names(ctx.client):
        await ctx.respond(
            f"`{layer1}` is a built-in command defined in the bot's code and cannot "
            "be managed with `/command`. No changes were made."
        )
        return True
    return False


def _restore_invocation_mapping_defaults(client: lb.Client) -> None:
    """Work around a lightbulb 3.2.3 bug in ``Client.unregister``.

    ``client._command_invocation_mapping`` is created as a ``defaultdict`` of
    ``defaultdict``s so that ``sync_application_commands`` can blindly do
    ``mapping[snowflake][command_path].put(...)`` and have missing keys auto-create.
    When a :class:`lightbulb.Group` is unregistered, however, ``unregister`` rebuilds
    each per-snowflake entry with a plain ``dict`` comprehension, discarding the
    ``defaultdict`` factory. The next ``sync_application_commands`` then raises
    ``KeyError`` for any command path not already present — which is exactly what
    happens the first time a third command layer is added (the path
    ``(l1, l2, l3)`` is new, and the inner mapping is no longer a defaultdict).

    Re-wrap each inner mapping as a ``defaultdict`` using the outer mapping's own
    factory so the missing keys auto-create again. This is a safe no-op once the
    upstream bug is fixed.
    """
    mapping = client._command_invocation_mapping
    # The outer mapping is a defaultdict at runtime even though lightbulb annotates
    # it as a plain dict; bail out unchanged if that ever stops being true.
    if not isinstance(mapping, collections.defaultdict):
        return
    factory = mapping.default_factory
    if factory is None:
        return
    for snowflake, inner in list(mapping.items()):
        if not isinstance(inner, collections.defaultdict):
            restored = t.cast(
                "collections.defaultdict[tuple[str, ...], t.Any]", factory()
            )
            restored.update(inner)
            mapping[snowflake] = restored


async def resync_user_commands(
    client: lb.Client, session: AsyncSession | None = None, *, sync: bool = True
) -> None:
    """Rebuild every dynamic user command from the DB and re-register it globally.

    Mirrors the v2 ``UserCommandBot.sync_schema_to_bot_cmds`` +
    ``sync_bot_cmds_to_discord`` cycle: drop the previously-registered dynamic
    commands, rebuild fresh objects from the ``UserCommand`` rows and register
    them, then push everything to Discord.

    When called from within an open transaction (the admin ``/command``
    subcommands), pass that ``session`` so the rebuild reads the rows the caller
    just wrote/deleted but has not yet committed. Calling resync inside the
    transaction means a sync failure rolls the DB change back instead of leaving an
    orphaned row that blocks re-adding the same command."""

    # Unregister everything we registered last time round.
    for obj in _registered_commands.values():
        try:
            client.unregister(obj)
        except Exception:
            logger.exception("Failed to unregister a dynamic user command")
    _registered_commands.clear()

    # ``Client.unregister`` corrupts the command-invocation mapping when it drops a
    # group (see helper); repair it before re-registering and syncing below.
    _restore_invocation_mapping_defaults(client)

    # With our dynamic commands now unregistered, everything left on the client is
    # code-defined. Snapshot their names so a DB-backed command can never clobber a
    # code-defined command of the same name.
    code_defined_names = _code_defined_command_names(client)

    command_groups = await UserCommand.fetch_command_groups(session=session)
    commands = await UserCommand.fetch_commands(session=session)

    objects = _build_command_objects(command_groups, commands)

    for key, obj in objects.items():
        (name,) = key
        if name in code_defined_names:
            # A DB-backed command must never shadow a code-defined one. This can arise
            # when a command is added to code *after* a DB row of the same name existed
            # (e.g. the new world-activity commands vs. a pre-existing `/dares` row).
            # Self-heal: delete the offending row(s) — both the standalone-command and
            # group forms so a group's subcommands cascade — and alert once, rather than
            # skipping and re-alerting on every resync while a dead row lingers.
            try:
                await UserCommand.delete_command_group(
                    name, cascade=True, session=session
                )
                await UserCommand.delete_command(name, session=session)
            except Exception:
                logger.exception(
                    "Failed to delete DB-backed user command %r that clashes with a "
                    "code-defined command; skipping it so the code-defined one works",
                    name,
                )
            else:
                logger.critical(
                    "Deleted DB-backed user command %r: it clashed with a code-defined "
                    "command of the same name and would have shadowed it.",
                    name,
                )
            continue
        # In a test environment register to the test guild(s) so command changes
        # propagate instantly; in production register globally so the commands are
        # available in every guild (Discord propagates global commands slowly).
        if cfg.test_env:
            client.register(obj, guilds=guild_scope(*cfg.test_env))
        else:
            client.register(obj, global_=True)
        _registered_commands[key] = obj

    if sync:
        await client.sync_application_commands()


# --------------------------------------------------------------------------------------
# Admin /command group (owner only, control guild scoped)
# --------------------------------------------------------------------------------------


def _type_choices(
    *, type_needed: bool, command_groups_allowed: bool
) -> list[lb.Choice[int]]:
    """Build the ``type`` option choices, honouring EMBEDS_FEATURE_FLAG.

    ``type_needed`` drops the "No Change" choice (used outside of /edit)."""
    if EMBEDS_FEATURE_FLAG:
        choices = [
            lb.Choice("No Change", -1),
            lb.Choice("Text", 1),
            lb.Choice("Message Copy", 2),
            lb.Choice("Embed", 3),
        ]
    else:
        choices = [
            lb.Choice("No Change", -1),
            lb.Choice("Text", 1),
            lb.Choice("Message Copy", 2),
        ]

    if command_groups_allowed:
        choices.append(lb.Choice("Command Group", 0))
    if type_needed:
        choices = choices[1:]
    return choices


async def layer_autocomplete(ctx: lb.AutocompleteContext[str]) -> None:
    """Autocomplete a ``layerN`` option from the DB given the other layers.

    lightbulb v3 autocomplete providers must *respond* via ``ctx.respond`` (the
    return value is discarded), and ``respond`` accepts plain strings/tuples rather
    than ``lb.Choice`` objects — unlike the v2 callback this was ported from, which
    returned a list of ``lb.Choice``."""

    def option_value(name: str) -> str:
        option = ctx.get_option(name)
        return str(option.value) if option is not None and option.value else ""

    focused = ctx.focused
    name = focused.name
    value = str(focused.value or "")

    # Determine which layer is being completed from the option name suffix.
    if not name.startswith("layer"):
        await ctx.respond([])
        return
    try:
        depth = int(name[len("layer") : len("layer") + 1])
    except ValueError:
        await ctx.respond([])
        return

    # Read the sibling layer values. The "new" rename options share the layer1/2/3
    # values as their context, matching the v2 postfix handling.
    l1_name = option_value("layer1")
    l2_name = option_value("layer2")
    l3_name = option_value("layer3")

    cmds = await UserCommand._autocomplete(l1_name, l2_name, l3_name)
    # Discord allows at most 25 autocomplete choices and rejects duplicate names, so
    # de-duplicate the matching layer names (name == value) while preserving order
    # and cap the response. Each choice is a plain string, which ctx.respond accepts.
    seen: set[str] = set()
    choices: list[str] = []
    for cmd in cmds:
        if cmd.depth != depth:
            continue
        layer_name = cmd.ln_names[depth - 1]
        if layer_name.startswith(value) and layer_name not in seen:
            seen.add(layer_name)
            choices.append(layer_name)
        if len(choices) >= 25:
            break
    await ctx.respond(choices)


command_group = lb.Group("command", "Custom command control")


@command_group.register
class PreviewCommand(
    lb.SlashCommand,
    name="preview",
    description="Preview a command response prior to adding it",
    hooks=[owner_only],
):
    type = lb.integer(
        "type",
        "Type of response to show the user",
        choices=_type_choices(type_needed=True, command_groups_allowed=False),
    )
    response = lb.string("response", "Respond to the user with this data", default="")

    @lb.invoke
    async def invoke(self, ctx: lb.Context, bot: CachedFetchBot = lb.di.INJECTED):
        await ctx.defer()
        response = self.response
        type_ = int(self.type)

        try:
            cmd = UserCommand(
                "temp1",
                "temp2",
                "temp3",
                description="temp cmd preview",
                response_type=type_,
                response_data=response,
            )
            handler = _user_command_response_func_builder(cmd)
            await handler(self, ctx)
        except Exception as e:
            logger.exception(e)
            # Reserve room for the raw-response block so the whole message stays
            # within Discord's length limit even with a long traceback.
            raw_block = "\nRaw response data:\n```\n" + _truncate_middle(response, 500)
            raw_block += "\n```"
            await ctx.respond(
                _error_response(
                    "An error occured while previewing the commmand.\n",
                    e,
                    reserve=len(raw_block),
                )
                + raw_block
            )
        else:
            await ctx.respond(
                "Preview generated.\nIf no response is visible then please contact"
                + f" <@{(await bot.fetch_owner_ids())[-1]}>.\n\n"
                + "Raw response data:\n```\n"
                + response
                + "\n```"
            )


@command_group.register
class AddCommand(
    lb.SlashCommand, name="add", description="Add a command", hooks=[owner_only]
):
    # Discord requires all required options before optional ones. type, description
    # and layer1 are required (so they lead); the layer1/2/3 options are kept
    # together after type & description, with the optional layer2/layer3 (used only
    # for nesting) and response following.
    type = lb.integer(
        "type",
        "Type of response to show the user",
        choices=_type_choices(type_needed=True, command_groups_allowed=True),
    )
    description = lb.string("description", "Description of the command")
    layer1 = lb.string("layer1", "1st layer commands and groups")
    layer2 = lb.string("layer2", "2nd layer commands and groups", default="")
    layer3 = lb.string("layer3", "3rd layer commands and groups", default="")
    response = lb.string("response", "Respond to the user with this data", default="")

    @lb.invoke
    async def invoke(self, ctx: lb.Context):
        await ctx.defer()
        layer1, layer2, layer3 = self.layer1, self.layer2, self.layer3
        type_ = int(self.type)

        if await _warn_if_code_defined(ctx, layer1):
            return

        async with db_session() as session, session.begin():
            await UserCommand.add_command(
                layer1,
                layer2,
                layer3,
                description=self.description,
                response_type=type_,
                response_data=self.response,
                session=session,
            )
            # Resync inside the transaction so a sync failure rolls the row back
            # rather than leaving an orphan that blocks re-adding the command.
            await resync_user_commands(ctx.client, session=session)
        await ctx.respond(
            "Successfully added the `"
            + _layers_repr(layer1, layer2, layer3)
            + "` command.\n"
            + NOTE_ABOUT_SLOW_DISCORD_PROPAGATION
        )


@command_group.register
class DeleteCommand(
    lb.SlashCommand, name="delete", description="Delete a command", hooks=[owner_only]
):
    layer1 = lb.string(
        "layer1",
        "1st layer commands and groups",
        autocomplete=layer_autocomplete,
    )
    layer2 = lb.string(
        "layer2",
        "2nd layer commands and groups",
        autocomplete=layer_autocomplete,
        default="",
    )
    layer3 = lb.string(
        "layer3",
        "3rd layer commands and groups",
        autocomplete=layer_autocomplete,
        default="",
    )
    delete_whole_group = lb.boolean(
        "delete_whole_group",
        "USE WITH CAUTION, DELETES ALL SUBCOMMANDS",
        default=False,
    )

    @lb.invoke
    async def invoke(self, ctx: lb.Context):
        await ctx.defer()
        layer1, layer2, layer3 = self.layer1, self.layer2, self.layer3

        if await _warn_if_code_defined(ctx, layer1):
            return

        if self.delete_whole_group:
            deleted_commands = await UserCommand.delete_command_group(
                layer1, layer2, cascade=True
            )
        else:
            # Try deleting a command first; fall back to deleting a group when
            # no command matched (groups have response_type 0).
            deleted_command = await UserCommand.delete_command(layer1, layer2, layer3)
            deleted_commands = (
                [deleted_command]
                if deleted_command
                else (await UserCommand.delete_command_group(layer1, layer2, layer3))
            )
        await resync_user_commands(ctx.client)
        if deleted_commands:
            await ctx.respond(
                "Deleted the following command(s):\n```"
                + "\n".join(str(cmd) for cmd in deleted_commands)
                + "\n```"
                + NOTE_ABOUT_SLOW_DISCORD_PROPAGATION
            )
        else:
            layers = _layers_repr(layer1, layer2, layer3)
            await ctx.respond(f"`{layers}` command or group not found")


@command_group.register
class EditCommand(
    lb.SlashCommand, name="edit", description="Edit a command", hooks=[owner_only]
):
    layer1 = lb.string(
        "layer1",
        "1st layer commands and groups",
        autocomplete=layer_autocomplete,
    )
    layer2 = lb.string(
        "layer2",
        "2nd layer commands and groups",
        autocomplete=layer_autocomplete,
        default="",
    )
    layer3 = lb.string(
        "layer3",
        "3rd layer commands and groups",
        autocomplete=layer_autocomplete,
        default="",
    )
    type = lb.integer(
        "type",
        "Type of response to show the user",
        choices=_type_choices(type_needed=False, command_groups_allowed=False),
        default=-1,
    )
    description = lb.string("description", "Description of the command", default="")
    response = lb.string("response", "Respond to the user with this data", default="")

    @lb.invoke
    async def invoke(self, ctx: lb.Context):
        await ctx.defer()
        layer1, layer2, layer3 = self.layer1, self.layer2, self.layer3
        type_ = int(self.type)
        description = self.description
        response = self.response

        if await _warn_if_code_defined(ctx, layer1):
            return

        async with db_session() as session, session.begin():
            # Delete subject command from db
            deleted_command = await UserCommand.delete_command(
                layer1, layer2, layer3, session=session
            )
            if deleted_command is None:
                raise FriendlyValueError(
                    f"No command `{_layers_repr(layer1, layer2, layer3)}` "
                    "found to edit."
                )

            # Update command parameters if specified
            ln_names = deleted_command.ln_names
            description = description or deleted_command.description
            type_ = type_ if type_ != -1 else deleted_command.response_type
            response = response or deleted_command.response_data

            # Add command back with new parameters
            await UserCommand.add_command(
                *ln_names,
                description=description,
                response_type=type_,
                response_data=response,
                session=session,
            )
            # Resync inside the transaction so a sync failure rolls back the
            # delete+re-add rather than leaving the DB in an edited-but-unsynced state.
            await resync_user_commands(ctx.client, session=session)
        await ctx.respond(
            "Successfully edited the `"
            + _layers_repr(layer1, layer2, layer3)
            + "` command.\n"
            + NOTE_ABOUT_SLOW_DISCORD_PROPAGATION
        )


@command_group.register
class RenameCommand(
    lb.SlashCommand,
    name="rename",
    description="Rename a command or command group",
    hooks=[owner_only],
):
    layer1 = lb.string(
        "layer1",
        "1st layer commands and groups",
        autocomplete=layer_autocomplete,
    )
    layer2 = lb.string(
        "layer2",
        "2nd layer commands and groups",
        autocomplete=layer_autocomplete,
        default="",
    )
    layer3 = lb.string(
        "layer3",
        "3rd layer commands and groups",
        autocomplete=layer_autocomplete,
        default="",
    )
    layer1new = lb.string(
        "layer1new",
        "1st layer commands and groups",
        autocomplete=layer_autocomplete,
        default="",
    )
    layer2new = lb.string(
        "layer2new",
        "2nd layer commands and groups",
        autocomplete=layer_autocomplete,
        default="",
    )
    layer3new = lb.string(
        "layer3new",
        "3rd layer commands and groups",
        autocomplete=layer_autocomplete,
        default="",
    )

    @lb.invoke
    async def invoke(self, ctx: lb.Context):
        await ctx.defer()
        layer1, layer2, layer3 = self.layer1, self.layer2, self.layer3
        layer1new, layer2new, layer3new = self.layer1new, self.layer2new, self.layer3new

        # Block renaming a code-defined command (source) or renaming a DB command
        # onto a code-defined top-level name (destination).
        if await _warn_if_code_defined(ctx, layer1):
            return
        if await _warn_if_code_defined(ctx, layer1new):
            return

        async with db_session() as session, session.begin():
            # Delete subject command or group from db
            deleted_commands: list[UserCommand] = []

            # If layer3 is not specified then try and rename any groups
            # as well since there might be one specified
            deleted_commands.extend(
                await UserCommand.delete_command_group(
                    layer1, layer2, cascade=True, session=session
                )
                if not layer3
                else []
            )

            deleted_commands.append(
                await UserCommand.delete_command(
                    layer1, layer2, layer3, session=session
                )
            )

            added_commands: list[UserCommand] = []
            for deleted_command in deleted_commands:
                if not deleted_command:
                    continue
                # Add commands back with new parameters
                added_commands.append(
                    await UserCommand.add_command(
                        layer1new or deleted_command.l1_name,
                        layer2new or deleted_command.l2_name,
                        layer3new or deleted_command.l3_name,
                        description=deleted_command.description,
                        response_type=deleted_command.response_type,
                        response_data=deleted_command.response_data,
                        session=session,
                    )
                )

            # Resync inside the transaction so a sync failure rolls the rename back
            # rather than leaving the renamed rows committed but unsynced.
            await resync_user_commands(ctx.client, session=session)
        await ctx.respond(
            "Renamed:\n"
            + "\n".join(
                [
                    f"`{deleted_command}`  **to**  `{added_command}`"
                    for deleted_command, added_command in zip(
                        deleted_commands, added_commands, strict=False
                    )
                ]
            )
            + NOTE_ABOUT_SLOW_DISCORD_PROPAGATION
        )


# Control-guild only, plus the test guild(s) when running in a test environment.
# control_discord_server_id is always present and ``guild_scope`` strips any 0, so the
# list never collapses to a global (guild-0) registration.
loader.command(
    command_group,
    guilds=guild_scope(*cfg.test_env, cfg.control_discord_server_id),
)
