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

"""Interactive embed builder, built on lightbulb v3 components (miru-free).

``build_embed_with_user`` shows an ephemeral message with a row of edit buttons; each
opens a modal for its field(s), mutates the in-progress embed and edits the message in
place. Done returns the finished embed. Used by the ``/post`` embed commands.
"""

import asyncio
import contextlib
import logging
import typing as t
import uuid

import hikari as h
import lightbulb as lb
from lightbulb import components as lbc

from dd.hmessage import HMessage

from ..common import settings
from ..common.utils import (
    construct_emoji_substituter,
    fetch_emoji_dict,
    follow_link_single_step,
    re_user_side_emoji,
    substitute_guild_emoji,
)

# (label, current value, required, multi-line) for one modal text input.
_FieldSpec = tuple[str, str, bool, bool]
_Mutate = t.Callable[[h.Embed, list[str]], t.Awaitable[h.Embed | None]]


async def substitute_user_side_emoji(
    bot_or_emoji_dict: h.GatewayBot | dict[str, h.Emoji], text: str
) -> str:
    """Substitutes user-side emoji with their respective mentions.

    Accepts a resolved emoji dict (no I/O) or a bot (fetches the Kyber emoji first).
    """
    emoji_dict = (
        await fetch_emoji_dict(bot_or_emoji_dict)
        if isinstance(bot_or_emoji_dict, h.GatewayBot)
        else bot_or_emoji_dict
    )
    return re_user_side_emoji.sub(construct_emoji_substituter(emoji_dict), text)


class _BuilderState:
    """Shared, mutable state for one embed-builder session."""

    def __init__(self, embed: h.Embed) -> None:
        self.embed = embed
        self.result: h.Embed | None = None  # set when the user presses Done


# --- per-field modal field specs + mutators (pure; unit-tested) -------------------


def _author_fields(embed: h.Embed) -> list[_FieldSpec]:
    author = embed.author
    name = (author.name or "") if author else ""
    icon = (author.icon.url if author and author.icon else "") or ""
    url = (author.url or "") if author else ""
    return [
        ("Author", name, False, False),
        ("Icon URL", icon, False, False),
        ("Author URL", url, False, False),
    ]


def _footer_fields(embed: h.Embed) -> list[_FieldSpec]:
    footer = embed.footer
    text = (footer.text or "") if footer else ""
    icon = (footer.icon.url if footer and footer.icon else "") or ""
    return [("Footer", text, False, False), ("Icon URL", icon, False, False)]


async def _mutate_title(embed: h.Embed, values: list[str]) -> h.Embed | None:
    embed.title = values[0] or None
    return embed


async def _mutate_color(embed: h.Embed, values: list[str]) -> h.Embed | None:
    if not values[0]:
        return None
    try:
        embed.color = h.Color.of(values[0])
    except ValueError as e:
        logging.error("Embed builder: invalid color %r", values[0])
        logging.exception(e)
        return None
    return embed


async def _mutate_author(embed: h.Embed, values: list[str]) -> h.Embed | None:
    embed.set_author(
        name=values[0] or None, icon=values[1] or None, url=values[2] or None
    )
    return embed


async def _mutate_footer(embed: h.Embed, values: list[str]) -> h.Embed | None:
    embed.set_footer(values[0] or None, icon=values[1] or None)
    return embed


async def _mutate_image(embed: h.Embed, values: list[str]) -> h.Embed | None:
    # Empty → clear; otherwise follow one redirect so the newest image is embedded.
    embed.set_image(await follow_link_single_step(values[0]) if values[0] else None)
    return embed


async def _mutate_thumbnail(embed: h.Embed, values: list[str]) -> h.Embed | None:
    embed.set_thumbnail(await follow_link_single_step(values[0]) if values[0] else None)
    return embed


# --- modal ------------------------------------------------------------------------


class _PropertiesModal(lbc.Modal):
    """A modal with 1..N text inputs that applies ``mutate`` to ``embed`` on submit."""

    def __init__(
        self, *, field_specs: list[_FieldSpec], embed: h.Embed, mutate: _Mutate
    ) -> None:
        self._embed = embed
        self._mutate = mutate
        self._fields: list[lbc.TextInput] = []
        for label, value, required, multi_line in field_specs:
            add = (
                self.add_paragraph_text_input
                if multi_line
                else self.add_short_text_input
            )
            self._fields.append(
                add(label, value=value or h.UNDEFINED, required=required)
            )

    async def on_submit(self, ctx: lbc.ModalContext) -> h.Embed | None:
        values = [ctx.value_for(field) or "" for field in self._fields]
        # Defer first so a slow mutation (image/thumbnail redirect follow) can't blow
        # the ~3s modal ack window; then edit the builder message in place.
        await ctx.interaction.create_initial_response(
            h.ResponseType.DEFERRED_MESSAGE_UPDATE
        )
        new = await self._mutate(self._embed, values)
        if new is not None:
            await ctx.interaction.edit_initial_response(embed=new)
        return new


async def _open_edit_modal(
    mctx: lbc.MenuContext,
    state: _BuilderState,
    title: str,
    field_specs: list[_FieldSpec],
    mutate: _Mutate,
) -> None:
    modal = _PropertiesModal(field_specs=field_specs, embed=state.embed, mutate=mutate)
    custom_id = f"embed_edit:{uuid.uuid4()}"
    # ``respond_with_modal`` must be the first response on the button's context.
    await mctx.respond_with_modal(title, custom_id, components=modal)
    with contextlib.suppress(asyncio.TimeoutError):
        # Dismissing the modal times out here → leave the field untouched.
        new = await modal.attach(mctx.client, custom_id, timeout=300)
        if new is not None:
            state.embed = new


# --- public entry point -----------------------------------------------------------


async def build_embed_with_user(
    ctx: lb.Context,
    done_button_text: str = "Done",
    existing_embed: h.Embed | None = None,
) -> h.Embed | None:
    """Build an embed interactively; returns the embed on Done, else ``None``."""
    embed = existing_embed or h.Embed(
        title="Embed Builder",
        description="Use the buttons below to build your embed!\n",
        color=await settings.get_embed_default_color(),
    )
    state = _BuilderState(embed)

    # Pre-resolve the emoji dict once so the description mutator does no network I/O on
    # the modal ack path. A failure here just disables emoji substitution.
    bot = t.cast(h.GatewayBot, ctx.client.app)
    try:
        emoji_dict = await fetch_emoji_dict(bot)
    except Exception as e:
        logging.warning("Embed builder: could not pre-resolve emoji dict: %r", e)
        emoji_dict = {}

    async def _mutate_description(embed_: h.Embed, values: list[str]) -> h.Embed | None:
        embed_.description = values[0]
        # Resolve :emoji: across every field of the embed (title/description/fields/
        # author/footer), mutating ``embed_`` in place.
        substitute_guild_emoji(HMessage(embeds=[embed_]), emoji_dict)
        return embed_

    menu = lbc.Menu()

    def _register(
        custom_id: str,
        label: str,
        fields_fn: t.Callable[[h.Embed], list[_FieldSpec]],
        mutate: _Mutate,
    ) -> None:
        async def _on_press(mctx: lbc.MenuContext) -> None:
            await _open_edit_modal(mctx, state, label, fields_fn(state.embed), mutate)

        menu.add_interactive_button(
            h.ButtonStyle.SECONDARY, _on_press, custom_id=custom_id, label=label
        )

    _register(
        "embed:title",
        "Edit Title",
        lambda e: [("Title", e.title or "", False, False)],
        _mutate_title,
    )
    _register(
        "embed:text",
        "Edit Text",
        lambda e: [("Body", e.description or "", False, True)],
        _mutate_description,
    )
    _register(
        "embed:color",
        "Edit Color",
        lambda e: [
            (
                "Color",
                str(e.color or settings.get_embed_default_color_sync()),
                False,
                False,
            )
        ],
        _mutate_color,
    )
    _register("embed:author", "Edit Author", _author_fields, _mutate_author)
    _register(
        "embed:image",
        "Edit Image",
        lambda e: [("Image URL", e.image.url if e.image else "", False, False)],
        _mutate_image,
    )
    _register(
        "embed:thumbnail",
        "Edit Thumbnail",
        lambda e: [
            ("Thumbnail URL", e.thumbnail.url if e.thumbnail else "", False, False)
        ],
        _mutate_thumbnail,
    )
    _register("embed:footer", "Edit Footer", _footer_fields, _mutate_footer)

    async def _on_done(mctx: lbc.MenuContext) -> None:
        state.result = state.embed
        await mctx.respond(edit=True, components=menu.disable_all_components())
        mctx.stop_interacting()

    menu.add_interactive_button(
        h.ButtonStyle.SUCCESS, _on_done, custom_id="embed:done", label=done_button_text
    )

    await ctx.respond(embed=embed, components=menu, flags=h.MessageFlag.EPHEMERAL)
    # ``attach`` blocks until Done (which stops it) or the timeout (which raises).
    with contextlib.suppress(TimeoutError):
        await menu.attach(ctx.client, timeout=840)
    if state.result is None:
        # Timed out without Done — disable the controls (best-effort).
        with contextlib.suppress(h.NotFoundError, h.UnauthorizedError):
            await ctx.interaction.edit_initial_response(
                components=menu.disable_all_components()
            )
    return state.result
