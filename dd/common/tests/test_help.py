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

# Pure-logic unit tests for dd.common.help — no Discord I/O. Exercises the type-marker
# rendering, detail-page rendering/pagination, and the visibility/autocomplete helpers
# via small fakes for the lightbulb client and command objects.

import typing as t

import hikari as h
import lightbulb as lb
import pytest

from dd.common import (
    cfg,
    help as help_mod,
)
from dd.common.help import CommandDetail


class _Data:
    def __init__(
        self,
        name: str,
        description: str = "",
        type: h.CommandType = h.CommandType.SLASH,
    ) -> None:
        self.name = name
        self.description = description
        self.type = type


class _Cmd:
    """Fake loose command exposing only what dd.common.help reads."""

    def __init__(
        self,
        name: str,
        description: str = "",
        type: h.CommandType = h.CommandType.SLASH,
    ) -> None:
        self._command_data = _Data(name, description, type)


class _Client:
    """Fake lightbulb client: maps each command to its registered guild-id set."""

    def __init__(self, registered: dict[_Cmd, set[int]]) -> None:
        self._registered_commands = registered

    @property
    def registered_commands(self) -> list[_Cmd]:
        return list(self._registered_commands)


def _client(registered: dict[_Cmd, set[int]]) -> lb.Client:
    """Fake client cast to ``lb.Client`` at the call boundary (it exposes exactly the
    attributes dd.common.help reads)."""
    return t.cast(lb.Client, _Client(registered))


# A guild set that marks a command administrative (control-guild scoped).
_ADMIN_GUILDS = {cfg.control_discord_server_id}
_GLOBAL = {0}


# -- type markers ------------------------------------------------------------


def test_command_type_defaults_to_slash() -> None:
    assert help_mod._command_type(_Cmd("x", type=h.CommandType.MESSAGE)) is (
        h.CommandType.MESSAGE
    )
    assert help_mod._command_type(object()) is h.CommandType.SLASH


def test_format_command_line_slash() -> None:
    assert help_mod._format_command_line("ping", "desc") == "**`/ping`** - desc"
    assert help_mod._format_command_line("ping", "") == "**`/ping`**"


def test_format_command_line_message_command_has_marker_not_slash() -> None:
    line = help_mod._format_command_line(
        "Post as JSON", "Post a message", command_type=h.CommandType.MESSAGE
    )
    assert line.startswith(help_mod.CONTEXT_MENU_MARKER)
    assert "**`Post as JSON`**" in line  # name highlighted, no leading slash
    assert "/Post as JSON" not in line
    assert "right-click" in line
    assert line.endswith("- Post a message")


def test_format_command_line_user_command_has_marker() -> None:
    line = help_mod._format_command_line(
        "Inspect", "d", command_type=h.CommandType.USER
    )
    assert line.startswith(help_mod.CONTEXT_MENU_MARKER)
    assert "**`Inspect`**" in line
    assert "/Inspect" not in line


# -- listing -----------------------------------------------------------------


def test_group_commands_marks_message_commands() -> None:
    slash = _Cmd("ping", "Ping", h.CommandType.SLASH)
    msg = _Cmd("Post as JSON", "Post a CV2 message", h.CommandType.MESSAGE)
    client = _client({slash: _GLOBAL, msg: _GLOBAL})

    general = help_mod.group_commands(client, is_admin=False)[help_mod.GENERAL_CATEGORY]

    assert any(line.startswith("**`/ping`**") for line in general)
    msg_line = next(line for line in general if "Post as JSON" in line)
    assert msg_line.startswith(help_mod.CONTEXT_MENU_MARKER)
    assert "/Post as JSON" not in msg_line


def test_group_commands_hides_admin_from_non_admin() -> None:
    public = _Cmd("ping", "p")
    admin = _Cmd("secret", "s")
    client = _client({public: _GLOBAL, admin: _ADMIN_GUILDS})

    non_admin = help_mod.group_commands(client, is_admin=False).get(
        help_mod.GENERAL_CATEGORY, []
    )
    assert any("ping" in line for line in non_admin)
    assert not any("secret" in line for line in non_admin)

    as_admin = help_mod.group_commands(client, is_admin=True)[help_mod.GENERAL_CATEGORY]
    assert any("secret" in line for line in as_admin)


# -- detail rendering --------------------------------------------------------


def test_render_detail_sections_full() -> None:
    detail = CommandDetail(
        command="x",
        title="X title",
        summary="Does things.",
        steps=("first", "second"),
        notes=("note a", "note b"),
    )
    sections = help_mod.render_detail_sections(detail)

    assert sections[0] == "## X title\nDoes things."
    assert "1. first" in sections[1] and "2. second" in sections[1]
    assert "- note a" in sections[2] and "- note b" in sections[2]


def test_render_detail_sections_omits_empty_blocks() -> None:
    detail = CommandDetail(command="x", title="T", summary="S")
    assert help_mod.render_detail_sections(detail) == ["## T\nS"]


@pytest.mark.asyncio
async def test_paginate_detail_single_page() -> None:
    detail = CommandDetail("x", "T", "S", steps=("a",), notes=("b",))
    pages = await help_mod.paginate_detail(detail)

    assert len(pages) == 1
    rendered = pages[0]()
    assert isinstance(rendered, list) and len(rendered) == 1
    assert isinstance(rendered[0], h.impl.ContainerComponentBuilder)


@pytest.mark.asyncio
async def test_paginate_detail_splits_long_content() -> None:
    big_steps = tuple(f"step {i} " + "x" * 200 for i in range(60))
    detail = CommandDetail("x", "T", "S", steps=big_steps)
    assert len(await help_mod.paginate_detail(detail)) >= 2


# -- visibility + autocomplete choices ---------------------------------------


def test_visible_details_filters_by_registration_and_admin() -> None:
    pub = CommandDetail(command="ping", title="Ping", summary="s")
    adm = CommandDetail(command="secret", title="Secret", summary="s")
    unregistered = CommandDetail(command="ghost", title="Ghost", summary="s")
    by_key = {"ping": pub, "secret": adm, "ghost": unregistered}
    client = _client({_Cmd("ping"): _GLOBAL, _Cmd("secret"): _ADMIN_GUILDS})

    assert set(help_mod._visible_details(client, by_key, is_admin=False)) == {"ping"}
    assert set(help_mod._visible_details(client, by_key, is_admin=True)) == {
        "ping",
        "secret",
    }


def test_detail_choices_maps_title_to_key_and_filters() -> None:
    visible = {
        "ping": CommandDetail("ping", "Ping command", "s"),
        "post as json": CommandDetail("Post as JSON", "Post as JSON", "s"),
    }

    assert help_mod._detail_choices(visible, "") == {
        "Ping command": "ping",
        "Post as JSON": "Post as JSON",
    }
    assert help_mod._detail_choices(visible, "post") == {"Post as JSON": "Post as JSON"}


def test_detail_choices_caps_at_25() -> None:
    visible = {
        f"c{i:02d}": CommandDetail(f"c{i:02d}", f"Title {i}", "s") for i in range(40)
    }
    assert len(help_mod._detail_choices(visible, "")) == 25


def test_with_self_detail_always_includes_help() -> None:
    # The generic /help self-detail is auto-included so neither bot has to list it.
    assert help_mod._with_self_detail(())["help"] is help_mod.HELP_SELF_DETAIL
    indexed = help_mod._with_self_detail((CommandDetail("foo", "Foo", "summary"),))
    assert set(indexed) == {"help", "foo"}
