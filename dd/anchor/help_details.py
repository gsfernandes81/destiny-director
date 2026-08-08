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

"""Per-command detailed ``/help`` pages for the anchor bot.

Each :class:`~dd.common.help.CommandDetail` is keyed by its command's registered name
and surfaced via ``/help command:<name>``. Anchor is owner-only, so these are visible to
the bot team only.
"""

from ..common.help import CommandDetail

POST_COMPONENTS_DETAIL = CommandDetail(
    command="components",
    title="Post a Components V2 message (/post components)",
    summary=(
        "Build a rich Components V2 message in Discord with an interactive button "
        "builder, then post it. Owner/team only."
    ),
    steps=(
        "Run `/post components` in the channel you want to post to (pass `channel:` to "
        "target another text/announcement channel instead).",
        'In the ephemeral builder, press "Add" and pick a block type — container, '
        "text, section, media gallery, separator or link button — filling its details "
        "in the modal that opens.",
        'Select a block to Edit, Delete or Move it; press "Open ▸" to go inside a '
        'container or section and "◂ Back" to come out again.',
        'Press "Post" to send the finished message to the chosen channel.',
    ),
    notes=(
        "The preview updates live as you edit; incomplete blocks (an empty container, "
        "a section without an accessory) show a placeholder until you fill them.",
        "Containers are top-level only; sections hold 1–3 text blocks plus one "
        "accessory (a thumbnail image or a link button).",
        "Only link buttons are supported — interactive buttons and select menus need "
        'per-button code, so they are not offered. Use "Edit post" to change a '
        "post later.",
        "The builder session lasts ~14 minutes before it times out; just re-run.",
    ),
)

CREATE_POST_DETAIL = CommandDetail(
    command="post",
    title="Create an embed (/post embed)",
    summary=(
        "Build an embed from scratch with the interactive builder and post it to the "
        "current channel. Owner/team only."
    ),
    steps=(
        "Run `/post embed` in the channel you want to post to.",
        "Fill in the embed via the ephemeral builder buttons (title, text, colour, "
        "author, image, thumbnail, footer).",
        'Press "Post" to send the embed to the current channel.',
    ),
    notes=(
        "If the bot cannot post in the channel you get a ForbiddenError message.",
        "Oversized embeds/attachments are rejected by Discord with a BadRequestError.",
    ),
)

EDIT_POST_DETAIL = CommandDetail(
    command="Edit post",
    title="Edit post (right-click message command)",
    summary=(
        "Edit a post the bot already made — either an embed or a Components V2 "
        "message — using the matching interactive builder. Owner/team only."
    ),
    steps=(
        "Right-click (long-press on mobile) a message this bot posted ▸ Apps ▸ "
        '"Edit post".',
        "The right builder opens for the post's format: the embed builder for embed "
        "posts, the Components V2 builder for CV2 posts, pre-loaded with its content.",
        'Press "Edit"/"Save" to apply your changes to the original message in place.',
    ),
    notes=(
        "Only works on messages posted by this bot.",
        "Embed posts must contain exactly one embed; CV2 posts open the block builder.",
        'Editing never changes a post\'s format — use "Convert to components" to turn '
        "an embed into a Components V2 message.",
    ),
)

COPY_POST_DETAIL = CommandDetail(
    command="Copy post",
    title="Copy post (right-click message command)",
    summary=(
        "Clone any embed or Components V2 message, tweak it in the matching builder, "
        "then post it fresh in the current channel. Owner/team only."
    ),
    steps=(
        'Right-click any embed or Components V2 message ▸ Apps ▸ "Copy post".',
        "Adjust the copied content in the ephemeral builder that matches its format.",
        'Press "Send" to post the result as a new message in the channel you ran the '
        "command in.",
    ),
    notes=(
        "The source must be a single-embed message or a Components V2 post.",
        "Unlike Edit, this posts a new message and leaves the original untouched.",
        "The copy keeps the source's format (embed stays embed, CV2 stays CV2).",
    ),
)

LS_UPDATE_DETAIL = CommandDetail(
    command="ls_update",
    title="Update lost sector post (right-click message command)",
    summary=(
        "Re-render an existing lost sector announcement with the current day's data, "
        "fixing a stale or wrong post. Owner/team only."
    ),
    steps=(
        'Right-click the lost sector announcement message ▸ Apps ▸ "ls_update".',
        "The bot rebuilds today's lost sector post and edits the message in place.",
    ),
    notes=(
        "Lost sector autoposts must be enabled first, or the command refuses to run.",
    ),
)

CONVERT_COMPONENTS_DETAIL = CommandDetail(
    command="Convert to components",
    title="Convert to components (right-click message command)",
    summary=(
        "Turn an embed post this bot made into a Components V2 message in place, after "
        "a preview. Owner/team only."
    ),
    steps=(
        "Right-click (long-press on mobile) an embed message this bot posted ▸ Apps ▸ "
        '"Convert to components".',
        "The bot shows a preview of how the converted Components V2 post will look.",
        'Press "Convert" to replace the original in place, or "Cancel" to leave it '
        "untouched.",
    ),
    notes=(
        "Only works on this bot's own messages, and only on embed posts that aren't "
        'already Components V2 (those get an error pointing you at "Edit post").',
        "The message must have an embed with content to convert.",
        "Conversion is one-way: once a message becomes Components V2 the flag "
        'can\'t be removed. Use "Edit post" afterwards to tweak the result.',
    ),
)

HELP_DETAILS: tuple[CommandDetail, ...] = (
    POST_COMPONENTS_DETAIL,
    CREATE_POST_DETAIL,
    EDIT_POST_DETAIL,
    COPY_POST_DETAIL,
    CONVERT_COMPONENTS_DETAIL,
    LS_UPDATE_DETAIL,
)
