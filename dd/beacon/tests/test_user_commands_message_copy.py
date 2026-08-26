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

# The response_type 2 ("copy this message link") user command, e.g. ``/loot tdp``.
#
# A fetched message exposes component *models*, and hikari's send path calls ``build()``
# on every entry of ``components`` — which models do not have. Passing them straight
# through therefore raised ``AttributeError: 'ContainerComponent' object has no
# attribute 'build'`` the moment the source message gained any component at all, which
# is exactly what converting a loot-table embed into a Components V2 post does. These
# pin the rebuild, and the components-only payload a CV2 source needs.

import hikari as h

from dd.beacon.extensions import user_commands as uc


class _FakeMessage:
    def __init__(self, **kw) -> None:
        self.content = kw.get("content", "")
        self.embeds = kw.get("embeds", [])
        self.components = kw.get("components", [])
        self.attachments = kw.get("attachments", [])
        self.flags = kw.get("flags", h.MessageFlag.NONE)


def _container() -> h.ContainerComponent:
    return h.ContainerComponent(
        type=h.ComponentType.CONTAINER,
        id=1,
        accent_color=None,
        is_spoiler=False,
        components=[
            h.TextDisplayComponent(
                type=h.ComponentType.TEXT_DISPLAY, id=2, content="The Desert Perpetual"
            )
        ],
    )


def test_cv2_source_is_copied_as_components_only():
    msg = _FakeMessage(components=[_container()], flags=h.MessageFlag.IS_COMPONENTS_V2)
    kwargs = uc._copy_response_kwargs(msg)

    assert kwargs["flags"] == h.MessageFlag.IS_COMPONENTS_V2
    # Content/embeds/attachments are mutually exclusive with CV2 components.
    assert set(kwargs) == {"components", "flags"}
    # Rebuilt into something hikari can actually serialize.
    payload, _ = kwargs["components"][0].build()
    assert payload["components"][0]["content"] == "The Desert Perpetual"


def test_plain_source_keeps_content_and_embeds():
    embed = h.Embed(title="Loot")
    msg = _FakeMessage(content="hi", embeds=[embed])
    kwargs = uc._copy_response_kwargs(msg)

    assert kwargs["content"] == "hi"
    assert kwargs["embeds"] == [embed]
    assert kwargs["components"] == []
    assert "flags" not in kwargs


def test_a_plain_source_with_a_link_button_row_is_rebuilt_too():
    # Not only CV2: any component at all used to hit the same AttributeError.
    row = h.ActionRowComponent(
        type=h.ComponentType.ACTION_ROW,
        id=1,
        components=[
            h.ButtonComponent(
                type=h.ComponentType.BUTTON,
                id=2,
                style=h.ButtonStyle.LINK,
                label="Kyber's Corner",
                emoji=None,
                custom_id=None,
                url="https://kyber3000.com",
                is_disabled=False,
            )
        ],
    )
    kwargs = uc._copy_response_kwargs(_FakeMessage(content="hi", components=[row]))

    payload, _ = kwargs["components"][0].build()
    assert payload["components"][0]["url"] == "https://kyber3000.com"
