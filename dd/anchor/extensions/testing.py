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

"""Owner-only test helpers for the anchor bot.

`/testing convert_sample` posts a bot-authored, multi-embed message crafted to exercise
every branch of ``dd.common.components.embeds_to_container`` — so you can then run the
"Convert to components" command on it and eyeball the conversion. It must be posted *by
this bot* because "Convert to components" only acts on the bot's own messages.

`/testing overflow_alert` deliberately builds an over-budget Components V2 post and
routes it through the real ``guard_cv2_post_sections`` path, so the CRITICAL
owner-pinging overflow alert fires on demand — a repeatable way to eyeball that the
alert renders as a clean notice (no traceback) and that the fixed header/footer survive
the body truncation.

Both are debugging aids, so the whole extension is gated on ``cfg.test_env`` — matching
beacon's ``testing`` extension, they never reach a production deployment.
"""

import datetime
import typing as t

import hikari as h
import lightbulb as lb

from ...common import cfg, components, settings, utils
from ...common.bot import CachedFetchBot

# This whole extension only loads in a test environment, matching beacon's `testing`.
loader = lb.Loader(should_load_hook=lambda: bool(cfg.test_env))

testing_group = lb.Group("testing", "Testing group")

# Reliable, real image URLs so Discord actually renders the thumbnails / images the
# converter turns into section accessories and media galleries.
_THUMB_1 = "https://picsum.photos/seed/dd-thumb-1/160"
_THUMB_2 = "https://picsum.photos/seed/dd-thumb-2/160"
_IMAGE = "https://picsum.photos/seed/dd-image/640/320"
_AUTHOR_ICON = "https://picsum.photos/seed/dd-author/64"
_FOOTER_ICON = "https://picsum.photos/seed/dd-footer/64"


def _sample_embeds() -> list[h.Embed]:
    """Three embeds that between them hit every ``embeds_to_container`` branch."""
    now = datetime.datetime.now(datetime.UTC)

    # 1) Kitchen sink: colour (first → container accent), author+link+icon, title+link,
    #    description, thumbnail (→ section accessory with the title/description), a mix
    #    of inline and block fields, a large image (→ gallery), footer + timestamp.
    kitchen_sink = h.Embed(
        title="Kitchen Sink Embed",
        url="https://example.com/title",
        description=(
            "Exercises author, linked title, description, thumbnail-as-section-"
            "accessory, fields, image and footer+timestamp."
        ),
        color=h.Color(0x5865F2),  # blurple — first colour wins the container accent
        timestamp=now,
    )
    kitchen_sink.set_author(
        name="Test Author", url="https://example.com/author", icon=_AUTHOR_ICON
    )
    kitchen_sink.set_thumbnail(_THUMB_1)
    kitchen_sink.add_field("Inline field A", "value a", inline=True)
    kitchen_sink.add_field("Inline field B", "value b", inline=True)
    kitchen_sink.add_field("Block field C", "inline layout is lost", inline=False)
    kitchen_sink.set_image(_IMAGE)
    kitchen_sink.set_footer("Footer text", icon=_FOOTER_ICON)

    # 2) No thumbnail: title/description render as plain text displays (the non-section
    #    branch); author without a link; title without a link; footer without a
    #    timestamp; a second colour that is ignored (the first embed's accent wins).
    no_thumb = h.Embed(
        title="Plain Title (no url)",
        description="No thumbnail, so title & description are plain text displays.",
        color=h.Color(0xED4245),  # ignored for the container accent
    )
    no_thumb.set_author(name="Author Without Link")
    no_thumb.add_field("A field", "field value")
    no_thumb.set_footer("Footer without a timestamp")

    # 3) Thumbnail only (→ standalone media gallery, since there is no text to anchor a
    #    section) plus a timestamp with no footer text (→ timestamp-only subtext line).
    thumb_only = h.Embed(timestamp=now)
    thumb_only.set_thumbnail(_THUMB_2)

    return [kitchen_sink, no_thumb, thumb_only]


@testing_group.register
class ConvertSampleEmbed(
    lb.SlashCommand,
    name="convert_sample",
    description="Post a bot embed that exercises every embeds_to_container branch",
):
    @lb.invoke
    async def invoke(self, ctx: lb.Context, bot: CachedFetchBot = lb.di.INJECTED):
        channel = t.cast(h.TextableChannel, await bot.fetch_channel(ctx.channel_id))
        await channel.send(embeds=_sample_embeds())
        await ctx.respond(
            "Posted a 3-embed sample covering every `embeds_to_container` branch. "
            'Right-click (long-press) it ▸ Apps ▸ "Convert to components" to check the '
            "conversion.\n\n"
            "Covered: accent (first colour wins), author ±link, linked/plain title, "
            "description, thumbnail→section accessory, thumbnail-only→gallery, fields, "
            "image gallery, footer+timestamp / footer-only / timestamp-only, and the "
            "divider between embeds. (The 'empty embed skipped' branch can't be "
            "produced by a real sent embed.)",
            ephemeral=True,
        )


@testing_group.register
class OverflowAlert(
    lb.SlashCommand,
    name="overflow_alert",
    description="Force a CV2 over-limit post to fire the CRITICAL overflow alert",
):
    @lb.invoke
    async def invoke(self, ctx: lb.Context, bot: CachedFetchBot = lb.di.INJECTED):
        # Mirror the Lost Sector autopost shape — a fixed header + rewards-style footer
        # that must survive, and a variable body — but blow the body well past the CV2
        # budget. guard_cv2_post_sections then truncates only the body, keeps the
        # header/footer, and fires the CRITICAL (owner-pinging) overflow alert: the
        # dd4313a path, on demand and without waiting for a real over-budget rotation.
        header = "# 🧪 CV2 overflow alert test\n\n"
        footer = "\n\n-# footer sentinel — this must survive the truncation"
        body = "Filler line sent to blow past the CV2 text budget. " * 130

        description = await components.guard_cv2_post_sections(
            header, body, footer, post_name="CV2 overflow test"
        )

        container = h.impl.ContainerComponentBuilder(
            accent_color=await settings.get_embed_default_color()
        )
        container.add_text_display(description)

        channel = t.cast(h.TextableChannel, await bot.fetch_channel(ctx.channel_id))
        await channel.send(components=[container], flags=h.MessageFlag.IS_COMPONENTS_V2)
        await ctx.respond(
            "Forced a CV2 overflow. In the owner **alerts channel** you should now see "
            "a clean 🚨 **CRITICAL** notice — *“CV2 overflow test autopost is N UTF-16 "
            "units (over the … budget) — truncated, content lost”* — with **no Python "
            "traceback** (the dd4313a fix). The message posted above keeps its header "
            "and footer sentinel with the body truncated (guard_cv2_post_sections).",
            ephemeral=True,
        )


loader.command(
    testing_group,
    guilds=utils.guild_scope(
        *cfg.test_env,
        cfg.control_discord_server_id,
        cfg.kyber_discord_server_id,
    ),
)
