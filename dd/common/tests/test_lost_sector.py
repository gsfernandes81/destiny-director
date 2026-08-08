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

"""The Lost Sector post body builder (shared by the live post + preview wall). Pure."""

import typing as t

import hikari as h
import pytest

from dd.common import lost_sector, settings


class _StubSector:
    """A stand-in Sector — build_body reads only .name and .shortlink_gfx here."""

    def __init__(self, name: str, shortlink_gfx: str) -> None:
        self.name = name
        self.shortlink_gfx = shortlink_gfx


def _sectors(*pairs: tuple[str, str]) -> list:
    """A stub sector list typed as a bare ``list`` so build_body accepts it."""
    return t.cast("list", [_StubSector(name, link) for name, link in pairs])


def test_build_body_without_details_lists_sectors_between_header_and_footer():
    sectors = _sectors(("Perdition", "https://kyber3000.com/ls/perdition"))
    body = lost_sector.build_body(sectors, details_enabled=False)
    # Header (title as a ## heading) and the raw :emoji: reward footer are present.
    assert "## [World Lost Sectors](https://kyber3000.com/LS)" in body
    assert ":enhancement_core: Enhancement Core" in body
    # Each sector is a :LS:-prefixed masked link with raw tokens (previewer renders it).
    assert ":LS: **[Perdition](https://kyber3000.com/ls/perdition)**" in body
    # details disabled -> no Champions/Shields block.
    assert "Champions:" not in body


def test_build_body_orders_header_sectors_footer():
    sectors = _sectors(("A", "https://x/a"), ("B", "https://x/b"))
    body = lost_sector.build_body(sectors, details_enabled=False)
    assert body.index("World Lost Sectors") < body.index("[A]") < body.index("[B]")
    assert body.index("[B]") < body.index("Enhancement Core")


# --- format_post: the image url is a DB setting and may be blank -------------------


async def _no_details() -> bool:
    return False


def _patch_post_deps(
    monkeypatch: pytest.MonkeyPatch, image_url: str, followed: list[str]
) -> None:
    """Pin format_post's async deps: image url, details toggle, and link follower."""

    async def _get_image_url() -> str:
        return image_url

    async def _follow(url: str, logger: t.Any = None) -> str:
        followed.append(url)
        return url + "?resolved"

    monkeypatch.setattr(settings, "get_lost_sector_image_url", _get_image_url)
    monkeypatch.setattr(
        lost_sector.schemas.AutoPostSettings,
        "get_lost_sector_details_enabled",
        staticmethod(_no_details),
    )
    monkeypatch.setattr(lost_sector, "follow_link_single_step", _follow)


@pytest.mark.asyncio
async def test_format_post_does_not_follow_a_blank_image_url(
    monkeypatch: pytest.MonkeyPatch,
):
    # A blank setting must not reach follow_link_single_step: aiohttp raises InvalidURL
    # inside the helper's own retry loop, where it is swallowed as a ClientError, so an
    # unguarded call burns an ERROR alert per attempt plus the retry sleeps on every
    # build — and the post ends up image-less regardless.
    followed: list[str] = []
    _patch_post_deps(monkeypatch, "", followed)

    hmsg = await lost_sector.format_post(
        sectors=_sectors(("Perdition", "https://x/p")), emoji_dict={}
    )

    assert followed == []
    container = t.cast(t.Any, hmsg.components[0])
    assert not any(
        isinstance(c, h.impl.MediaGalleryComponentBuilder) for c in container.components
    )


@pytest.mark.asyncio
async def test_format_post_follows_and_embeds_a_configured_image_url(
    monkeypatch: pytest.MonkeyPatch,
):
    followed: list[str] = []
    _patch_post_deps(monkeypatch, "https://kyberscorner.com/ls.gif", followed)

    hmsg = await lost_sector.format_post(
        sectors=_sectors(("Perdition", "https://x/p")), emoji_dict={}
    )

    assert followed == ["https://kyberscorner.com/ls.gif"]
    container = t.cast(t.Any, hmsg.components[0])
    gallery = [
        c
        for c in container.components
        if isinstance(c, h.impl.MediaGalleryComponentBuilder)
    ]
    assert len(gallery) == 1
    # The *followed* url is what gets embedded, not the configured one.
    payload, attachments = t.cast(t.Any, gallery[0]).build()
    assert "https://kyberscorner.com/ls.gif?resolved" in str(payload)
    assert list(attachments) == []  # URL-referenced, nothing uploaded
