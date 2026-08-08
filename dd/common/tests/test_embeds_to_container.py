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

# Tests for dd.common.components.embeds_to_container — the embed -> Components V2
# mapping used by anchor's "Convert to components" context-menu command. Pure builders,
# no Discord I/O: assert the produced container children reflect each embed part.

import hikari as h

from dd.common import components, settings


def _text(child: h.api.ComponentBuilder) -> str:
    assert isinstance(child, h.impl.TextDisplayComponentBuilder)
    return child.content


def test_title_and_description_become_text_displays() -> None:
    embed = h.Embed(
        title="My Title",
        description="Desc",
        url="https://example.com",
        color=h.Color(0x112233),
    )

    container = components.embeds_to_container(embed)

    assert container.accent_color == h.Color(0x112233)
    assert [_text(c) for c in container.components] == [
        "## [My Title](https://example.com)",
        "Desc",
    ]


def test_title_without_url_is_a_plain_heading() -> None:
    container = components.embeds_to_container(h.Embed(title="Bare"))
    assert [_text(c) for c in container.components] == ["## Bare"]


def test_thumbnail_wraps_text_in_a_section() -> None:
    embed = h.Embed(title="T", description="D")
    embed.set_thumbnail("https://img/thumb.png")

    (section,) = components.embeds_to_container(embed).components
    assert isinstance(section, h.impl.SectionComponentBuilder)
    assert isinstance(section.accessory, h.impl.ThumbnailComponentBuilder)
    assert section.accessory.media == "https://img/thumb.png"
    assert [tc.content for tc in section.components] == ["## T", "D"]


def test_thumbnail_without_text_becomes_a_media_gallery() -> None:
    embed = h.Embed()
    embed.set_thumbnail("https://img/thumb.png")

    (gallery,) = components.embeds_to_container(embed).components
    assert isinstance(gallery, h.impl.MediaGalleryComponentBuilder)
    assert [item.media for item in gallery.items] == ["https://img/thumb.png"]


def test_image_becomes_full_width_media_gallery() -> None:
    embed = h.Embed(description="body")
    embed.set_image("https://img/big.png")

    children = components.embeds_to_container(embed).components
    gallery = children[-1]
    assert isinstance(gallery, h.impl.MediaGalleryComponentBuilder)
    assert [item.media for item in gallery.items] == ["https://img/big.png"]


def test_fields_and_footer_and_author() -> None:
    embed = h.Embed(description="body")
    embed.set_author(name="Auth", url="https://a.example")
    embed.add_field("F1", "V1", inline=True)
    embed.set_footer(text="the footer")

    contents = [
        _text(c)
        for c in components.embeds_to_container(embed).components
        if isinstance(c, h.impl.TextDisplayComponentBuilder)
    ]
    # Author -> leading subtext, field -> bold-name/value, footer -> trailing subtext.
    assert contents[0] == "-# [Auth](https://a.example)"
    assert "**F1**\nV1" in contents
    assert contents[-1] == "-# the footer"

    # A field is preceded by a non-divider separator.
    kinds = [type(c).__name__ for c in components.embeds_to_container(embed).components]
    assert "SeparatorComponentBuilder" in kinds


def test_multiple_embeds_merge_with_a_divider() -> None:
    a = h.Embed(description="first")
    b = h.Embed(description="second")

    children = components.embeds_to_container([a, b]).components
    separators = [
        c for c in children if isinstance(c, h.impl.SeparatorComponentBuilder)
    ]
    assert any(sep.divider for sep in separators)
    texts = [
        c.content for c in children if isinstance(c, h.impl.TextDisplayComponentBuilder)
    ]
    assert texts == ["first", "second"]


def test_first_embed_color_wins_else_default() -> None:
    colored = h.Embed(description="x", color=h.Color(0x445566))
    plain = h.Embed(description="y")
    assert components.embeds_to_container([plain, colored]).accent_color == h.Color(
        0x445566
    )

    # No colour anywhere -> the shared embed default.
    assert (
        components.embeds_to_container(h.Embed(description="z")).accent_color
        == settings.get_embed_default_color_sync()
    )


def test_empty_embed_yields_empty_container() -> None:
    assert components.embeds_to_container(h.Embed()).components == []


# --- URL-referenced media (no upload / no round-trip) ----------------------------


def _uploads(container: h.impl.ContainerComponentBuilder) -> list:
    """Resources hikari would upload for this container (empty => URL-referenced)."""
    _payload, attachments = container.build()
    return list(attachments)


def test_image_media_is_url_referenced_not_uploaded() -> None:
    embed = h.Embed(description="body")
    embed.set_image("https://kyberscorner.com/lost-sector.gif")

    container = components.embeds_to_container(embed)

    # The image is kept as a media gallery referencing the URL...
    gallery = container.components[-1]
    assert isinstance(gallery, h.impl.MediaGalleryComponentBuilder)
    assert [item.media for item in gallery.items] == [
        "https://kyberscorner.com/lost-sector.gif"
    ]
    # ...but nothing is uploaded — Discord fetches the URL (no 413 / round-trip).
    assert _uploads(container) == []


def test_thumbnail_media_is_url_referenced_not_uploaded() -> None:
    embed = h.Embed(title="T", description="D")
    embed.set_thumbnail("https://kyberscorner.com/thumb.png")

    container = components.embeds_to_container(embed)
    (section,) = container.components
    assert isinstance(section, h.impl.SectionComponentBuilder)
    assert isinstance(section.accessory, h.impl.ThumbnailComponentBuilder)
    assert section.accessory.media == "https://kyberscorner.com/thumb.png"
    assert _uploads(container) == []


def test_image_only_embed_renders_the_image_referenced() -> None:
    # An image-only embed is no longer dropped — the image is referenced by URL.
    embed = h.Embed()
    embed.set_image("https://kyberscorner.com/only.gif")

    container = components.embeds_to_container(embed)
    assert isinstance(container.components[-1], h.impl.MediaGalleryComponentBuilder)
    assert _uploads(container) == []


def test_url_media_gallery_helper_uploads_nothing() -> None:
    gallery = components.url_media_gallery("https://kyberscorner.com/x.gif")
    _payload, attachments = gallery.build()
    assert list(attachments) == []
    assert [item.media for item in gallery.items] == ["https://kyberscorner.com/x.gif"]


def test_hikari_url_media_build_shape_is_pinned() -> None:
    # Tripwire: url_media_gallery/url_thumbnail rely on hikari's builders emitting the
    # URL in the payload while also returning one resource they would upload — which
    # our _Url* subclasses drop so Discord fetches the URL instead. The bot runs under
    # ``python -OO`` (asserts stripped), so this lives as a test: if a hikari upgrade
    # changes the (payload, resources) shape, CI fails loudly rather than the autoposts
    # silently 413ing on a re-uploaded ~15 MB gif.
    url = "https://kyberscorner.com/x.gif"
    for builder in (
        h.impl.MediaGalleryItemBuilder(media=url),
        h.impl.ThumbnailComponentBuilder(media=url),
    ):
        payload, resources = builder.build()
        assert payload["media"]["url"] == url
        assert len(list(resources)) == 1  # the resource _url_only strips
