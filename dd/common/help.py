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

"""Shared, miru-free ``/help`` renderer for both bots.

Introspects the lightbulb v3 command client's registered commands, groups them into
user-facing categories, filters out administrative commands for non-team users, and
renders the result as a paginated Components V2 message via
:mod:`dd.common.components`.
"""

import logging
import typing as t
from dataclasses import dataclass

import hikari as h
import lightbulb as lb

from . import cfg, components, settings
from .bot import CachedFetchBot

# Catch-all category title for loose/ungrouped commands. User-facing.
GENERAL_CATEGORY = "General Commands"

# Header shown at the top of the help message.
HELP_HEADING = "# Bot Help"

# Commands that are global (so not caught by the control-guild check) but should
# still only be visible to the bot team. Names are top-level command/group names.
ADMIN_ONLY_NAMES: frozenset[str] = frozenset({"testing"})

# lightbulb stores the global-command registration under this key in the client's
# ``_registered_commands`` guild sets.
_GLOBAL_COMMAND_KEY = 0

# Marker + invocation hint shown beside context-menu (right-click) commands so they are
# visually distinct from ``/slash`` commands in the listing. Kept as constants so the
# glyph/wording is easy to tweak in one place.
CONTEXT_MENU_MARKER = "🖱️"
_MESSAGE_HINT = "right-click a message ▸ Apps"
_USER_HINT = "right-click a user ▸ Apps"


@dataclass(frozen=True)
class CommandDetail:
    """Long-form, per-command help shown by ``/help command:<name>``.

    ``command`` is the lookup key: the command's registered name (for context-menu
    commands their display name, e.g. ``"Post as JSON"``), matched case-insensitively
    against both the typed argument and the bot's registered commands. ``steps`` render
    as a numbered walkthrough and ``notes`` as a bulleted list; both are optional.
    """

    command: str
    title: str
    summary: str
    steps: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


# A generic, public detail entry for ``/help`` itself, suitable for inclusion in either
# bot's detail set so regular users have at least one detailed page to discover.
HELP_SELF_DETAIL = CommandDetail(
    command="help",
    title="help (slash command)",
    summary=(
        "Lists the bot's commands by category. Pass a command name to read a detailed "
        "walkthrough for just that command."
    ),
    steps=(
        "Run `/help` with no options to see every command available to you, grouped "
        "by category.",
        "Run `/help command:<name>` and pick a suggestion to read its detailed page. "
        "Only commands you can actually use are suggested.",
    ),
    notes=(
        "Context-menu commands are shown with a 🖱️ marker and an invocation hint "
        "instead of a leading slash.",
        "Admin-only commands are hidden from non-team members in both the list and the "
        "suggestions.",
    ),
)


def _command_data(obj: t.Any) -> t.Any | None:
    """Return lightbulb's ``CommandData`` for a command class / group / subgroup.

    Read the name and description through ``_command_data`` rather than ``obj.name``
    / ``obj.description`` directly: a command that declares an option named ``name``
    or ``description`` shadows that attribute with lightbulb's option descriptor,
    which evaluates to the ``lightbulb.utils.EMPTY`` marker when accessed on the
    class (rendering as ``<lightbulb.Marker: 'EMPTY'>`` in the help text). Groups
    expose ``_command_data`` as a property and command classes as a class var, so
    this works uniformly for both.
    """
    return getattr(obj, "_command_data", None)


def _command_name(obj: t.Any) -> str:
    """Return the registered name of a command class / group / subgroup."""
    data = _command_data(obj)
    return str(getattr(data, "name", "") or "") if data is not None else ""


def _command_description(obj: t.Any) -> str:
    """Return the registered description of a command class / group / subgroup."""
    data = _command_data(obj)
    return str(getattr(data, "description", "") or "") if data is not None else ""


def _command_type(obj: t.Any) -> h.CommandType:
    """Return a command's type, defaulting to SLASH.

    Groups and subcommands have no meaningful context-menu type, so anything without a
    ``type`` (or with a falsy one) is treated as a slash command.
    """
    data = _command_data(obj)
    return getattr(data, "type", h.CommandType.SLASH) or h.CommandType.SLASH


def _is_group(obj: t.Any) -> bool:
    """Whether ``obj`` is a (sub)group, i.e. exposes ``subcommands``."""
    return hasattr(obj, "subcommands")


def _enabled_guilds(client: lb.Client, command: t.Any) -> set[int]:
    """Return the set of guild ids a top-level command/group is registered to.

    Reads the client's ``_registered_commands`` mapping populated by ``register``.
    The mapping value is a set of guild snowflakes (with ``0`` meaning global) or the
    literal string ``"defer"`` when guild registration is deferred. Returns an empty
    set when the command is not present / deferred.
    """
    registered = getattr(client, "_registered_commands", {})
    guilds = registered.get(command)
    if not isinstance(guilds, (set, frozenset, list, tuple)):
        return set()
    return {int(g) for g in guilds}


def _is_admin_command(client: lb.Client, command: t.Any) -> bool:
    """Whether a top-level command/group is administrative (team-only).

    A command is admin if it is registered to the control discord server, or if its
    name is in the explicit :data:`ADMIN_ONLY_NAMES` deny-set. Control-guild
    membership is checked specifically (not "is guild scoped") because in a test
    environment every command is guild-scoped.
    """
    if _command_name(command) in ADMIN_ONLY_NAMES:
        return True
    guilds = _enabled_guilds(client, command)
    return cfg.control_discord_server_id in guilds


def _format_command_line(
    qualified_name: str,
    description: str,
    *,
    command_type: h.CommandType = h.CommandType.SLASH,
) -> str:
    """Format a single command help line as markdown.

    The command name is highlighted with bold inline code. Slash commands render as
    ``/name``; context-menu (message/user) commands drop the slash and gain the
    :data:`CONTEXT_MENU_MARKER` plus an invocation hint, so they're visually distinct.
    """
    description = description.strip()
    if command_type is h.CommandType.MESSAGE:
        head = f"{CONTEXT_MENU_MARKER} **`{qualified_name}`** ({_MESSAGE_HINT})"
    elif command_type is h.CommandType.USER:
        head = f"{CONTEXT_MENU_MARKER} **`{qualified_name}`** ({_USER_HINT})"
    else:
        head = f"**`/{qualified_name}`**"
    return f"{head} - {description}" if description else head


def _collect_subcommand_lines(
    group_or_subgroup: t.Any, parents: list[str]
) -> list[str]:
    """Recursively collect help lines for a group's subcommands and subgroups."""
    lines: list[str] = []
    subcommands: dict[str, t.Any] = getattr(group_or_subgroup, "subcommands", {})
    for child in subcommands.values():
        name = _command_name(child)
        if not name:
            continue
        if _is_group(child):
            # A subgroup: recurse, prefixing its name.
            lines.extend(_collect_subcommand_lines(child, parents + [name]))
        else:
            qualified = " ".join(parents + [name])
            lines.append(_format_command_line(qualified, _command_description(child)))
    return lines


def group_commands(client: lb.Client, *, is_admin: bool) -> dict[str, list[str]]:
    """Group registered commands into user-facing categories of help lines.

    Each top-level :class:`lb.Group` becomes its own category (titled from the group
    name); loose/ungrouped commands fall under :data:`GENERAL_CATEGORY`. When
    ``is_admin`` is ``False`` administrative commands (whole groups and individual
    commands) are omitted. Categories with no visible commands are dropped.

    All visible commands are treated uniformly: nothing is special-cased or labelled
    by how it is backed.
    """
    categories: dict[str, list[str]] = {}

    for command in client.registered_commands:
        name = _command_name(command)
        if not name:
            continue

        if not is_admin and _is_admin_command(client, command):
            continue

        if _is_group(command):
            # A top-level group -> its own category named after the group.
            title = name.replace("_", " ").capitalize()
            lines = _collect_subcommand_lines(command, [name])
            if lines:
                categories.setdefault(title, []).extend(lines)
        else:
            # A loose command -> General Commands. Context-menu commands surface here
            # too (they have no subcommands); their type drives the rendered marker.
            line = _format_command_line(
                name,
                _command_description(command),
                command_type=_command_type(command),
            )
            categories.setdefault(GENERAL_CATEGORY, []).append(line)

    # Sort lines within each category for stable, readable output. Order General
    # Commands last so grouped categories appear first.
    ordered: dict[str, list[str]] = {}
    for title in sorted(categories, key=lambda t_: (t_ == GENERAL_CATEGORY, t_)):
        ordered[title] = sorted(categories[title])
    return ordered


def _category_section(title: str, lines: t.Sequence[str]) -> str:
    """Render a category as a markdown section (heading + command lines)."""
    return f"### {title}\n" + "\n".join(lines)


def _paginate_sections(
    categories: dict[str, list[str]], *, title: str
) -> list[list[str]]:
    """Split categories into pages, each a list of markdown section strings.

    The first text display of every page is the help heading. Categories are kept
    whole where possible; a category whose lines exceed the per-page limits is split
    across pages (its title repeated) via the char/line chunker in
    :mod:`dd.common.components`.
    """
    pages: list[list[str]] = []
    current: list[str] = []
    current_len = len(title)

    def flush() -> None:
        nonlocal current, current_len
        if current:
            pages.append(current)
        current = []
        current_len = len(title)

    for cat_title, lines in categories.items():
        # Chunk this category's lines into page-sized blocks.
        for block in components.chunk_lines_to_sections(lines):
            section = _category_section(cat_title, block.split("\n"))
            if current and current_len + len(section) > components.MAX_PAGE_CHARS:
                flush()
            current.append(section)
            current_len += len(section)

    flush()
    return pages or [[]]


def _make_page_factory(
    sections: list[str], *, color: h.Color
) -> components.Cv2PageFactory:
    """Build a CV2 page factory rendering the heading + the given sections."""

    def factory() -> list[h.api.ComponentBuilder]:
        return [
            components.build_container([HELP_HEADING, *sections], accent_color=color)
        ]

    return factory


def render_detail_sections(detail: CommandDetail) -> list[str]:
    """Render a command's detailed help as a list of markdown sections.

    Pure (no Discord I/O) so it is unit-testable directly. Empty step/note blocks are
    omitted; the first section is the title heading plus the summary.
    """
    sections: list[str] = [f"## {detail.title}\n{detail.summary}".rstrip()]
    if detail.steps:
        numbered = "\n".join(f"{i}. {step}" for i, step in enumerate(detail.steps, 1))
        sections.append(f"**How to use it**\n{numbered}")
    if detail.notes:
        bulleted = "\n".join(f"- {note}" for note in detail.notes)
        sections.append(f"**Notes**\n{bulleted}")
    return sections


def _detail_page_factory(
    sections: list[str], *, color: h.Color
) -> components.Cv2PageFactory:
    """Build a CV2 page factory for a detail page (its title is its own heading)."""

    def factory() -> list[h.api.ComponentBuilder]:
        return [components.build_container(sections, accent_color=color)]

    return factory


async def paginate_detail(
    detail: CommandDetail, *, color: h.Color | None = None
) -> list[components.Cv2PageFactory]:
    """Split a command's detail into CV2 page factories (one page for short content)."""
    if color is None:
        color = await settings.get_embed_default_color()
    pages: list[list[str]] = []
    current: list[str] = []
    current_len = 0
    for section in render_detail_sections(detail):
        # An oversized single section is chunked by the shared line chunker.
        for block in components.chunk_lines_to_sections(section.split("\n")):
            if current and current_len + len(block) > components.MAX_PAGE_CHARS:
                pages.append(current)
                current, current_len = [], 0
            current.append(block)
            current_len += len(block)
    if current:
        pages.append(current)
    return [_detail_page_factory(page, color=color) for page in (pages or [[]])]


def _visible_details(
    client: lb.Client, by_key: dict[str, CommandDetail], *, is_admin: bool
) -> dict[str, CommandDetail]:
    """Detail entries whose target command is registered and visible to the invoker.

    Shared by the autocomplete provider and the detail lookup so "what is suggested" and
    "what can be opened" cannot drift apart.
    """
    visible: dict[str, CommandDetail] = {}
    for command in client.registered_commands:
        key = _command_name(command).casefold()
        if not key or key not in by_key:
            continue
        if not is_admin and _is_admin_command(client, command):
            continue
        visible[key] = by_key[key]
    return visible


# Discord allows at most 25 autocomplete choices.
_MAX_AUTOCOMPLETE_CHOICES = 25


def _detail_choices(visible: dict[str, CommandDetail], typed: str) -> dict[str, str]:
    """Build the ``{label: value}`` autocomplete choices from visible details.

    Matches the typed text against the command key or the title, caps at Discord's
    25-choice limit, and maps the friendly title (label) to the stable command key
    (value). Pure, so it is unit-testable without an interaction.
    """
    needle = typed.casefold()
    choices: dict[str, str] = {}
    for key, detail in visible.items():
        if needle and needle not in key and needle not in detail.title.casefold():
            continue
        choices[detail.title] = detail.command
        if len(choices) >= _MAX_AUTOCOMPLETE_CHOICES:
            break
    return choices


async def render_help(
    ctx: lb.Context,
    *,
    title: str = HELP_HEADING,
    color: h.Color | None = None,
    is_admin: bool = False,
) -> None:
    """Render and send the paginated ``/help`` message.

    Introspects ``ctx.client``'s registered commands, groups them into categories,
    filters administrative commands when ``not is_admin``, and sends a paginated
    Components V2 message (a single page when everything fits), attaching the
    paginator's controls.
    """
    if color is None:
        color = await settings.get_embed_default_color()
    categories = group_commands(ctx.client, is_admin=is_admin)

    if not categories:
        await ctx.respond(
            flags=h.MessageFlag.IS_COMPONENTS_V2,
            components=[
                components.build_container(
                    [title, "No commands available."], accent_color=color
                )
            ],
        )
        return

    section_pages = _paginate_sections(categories, title=title)
    pages: list[components.Page] = [
        _make_page_factory(sections, color=color) for sections in section_pages
    ]

    paginator = components.Paginator(pages)
    await paginator.send(ctx)


def _with_self_detail(
    details: t.Sequence[CommandDetail],
) -> dict[str, CommandDetail]:
    """Index a bot's details by lookup key, always including :data:`HELP_SELF_DETAIL`.

    Each bot passes only its own command details; the generic ``/help`` self-detail is
    added here so neither bot has to repeat it. De-duped by key, so a bot passing it
    explicitly is harmless.
    """
    return {d.command.casefold(): d for d in (HELP_SELF_DETAIL, *details)}


async def _invoker_is_admin(bot: CachedFetchBot, user_id: h.Snowflake) -> bool:
    """Whether ``user_id`` is a bot owner, degrading to ``False`` on REST failure.

    The owner-id list is cached (see :meth:`CachedFetchBot.fetch_owner_ids`), so this
    is normally instant. If the underlying ``fetch_application`` REST call genuinely
    fails (e.g. a Discord outage), fail open to the non-admin view rather than let
    ``/help`` error out — the worst case is admins transiently not seeing admin-only
    commands, never a command leak.
    """
    try:
        return user_id in await bot.fetch_owner_ids()
    except Exception:
        logging.exception(
            "Failed to fetch owner ids for /help admin check; defaulting to non-admin"
        )
        return False


def make_help_command(
    details: t.Sequence[CommandDetail] = (),
) -> type[lb.SlashCommand]:
    """Build a fresh ``/help`` command class for a bot's loader.

    ``details`` are this bot's per-command detailed pages, surfaced via the optional
    autocompleted ``command`` argument; the generic ``/help`` self-detail is included
    automatically (see :func:`_with_self_detail`), so callers pass only their own. With
    no argument ``/help`` lists commands as before. Only commands you can use are
    suggested and openable.
    """
    by_key: dict[str, CommandDetail] = _with_self_detail(details)

    async def autocomplete(ctx: lb.AutocompleteContext[str]) -> None:
        bot = t.cast(CachedFetchBot, ctx.client.app)
        is_admin = await _invoker_is_admin(bot, ctx.interaction.user.id)
        visible = _visible_details(ctx.client, by_key, is_admin=is_admin)
        await ctx.respond(_detail_choices(visible, str(ctx.focused.value or "")))

    class Help(
        lb.SlashCommand, name="help", description="Get help information for the bot"
    ):
        command = lb.string(
            "command",
            "Show a detailed walkthrough for a specific command",
            default="",
            autocomplete=autocomplete,
        )

        @lb.invoke
        async def invoke(
            self, ctx: lb.Context, bot: CachedFetchBot = lb.di.INJECTED
        ) -> None:
            is_admin = await _invoker_is_admin(bot, ctx.user.id)
            query = str(self.command).strip()
            if not query:
                await render_help(ctx, is_admin=is_admin)
                return

            detail = _visible_details(ctx.client, by_key, is_admin=is_admin).get(
                query.casefold()
            )
            if detail is None:
                await components.respond_cv2(
                    ctx,
                    components.cv2_error(f"No detailed help available for `{query}`."),
                    ephemeral=True,
                )
                return

            await components.Paginator(await paginate_detail(detail)).send(ctx)

    return Help
