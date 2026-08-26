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

"""Server-side Components V2 node rules — sanitising and validation (no Discord I/O).

A "node" is a raw Discord component-payload dict (the same JSON shape the REST API sends
and accepts). **Authoring happens on the web**: ``web_static/cv2_model.js`` mirrors
these rules client-side and edits the tree there; this module is what the server
independently trusts — :func:`validate` gates every publish and
:func:`sanitize_for_preview` guarantees a mid-construction tree can still be rendered.

This file used to be much larger. It carried a whole modal-driven editing layer — field
specs, mutators, an add-flow catalogue, tree operations — for the in-Discord builder
(``cv2_builder.py``), which the web builder replaced and which was deleted. None of that
had a caller afterwards, so it went too; what remains is only what the server itself
needs. Keep it that way: node construction and tree editing belong to the client now.

Nesting mirrors Discord's real rules, not an idealised tree:

- **Container** (``17``) is *top-level only* — it cannot be nested inside another
  container — and holds the other display components plus link-button action rows.
- **Section** (``9``) holds 1–3 Text Displays plus exactly one *accessory* (a Thumbnail
  or a link button).
- **Thumbnail** (``11``) is valid *only* as a section accessory, never a free-standing
  block.
- **File** (``13``) round-trips when editing an existing post but can't be authored (it
  needs a real uploaded attachment, not a URL).
- An **action row** (``1``) holds up to five buttons — note the plural; assuming one is
  a bug this module has already had.

So the tree is at most three deep: root → container → section. ``cv2_model.js``'s
``allowedIn``/``canDrop`` are the live copy of those rules for the editor; the checks
here are the ones that actually gate a send.
"""

import typing as t

import hikari as h

from ..common.components import CV2_TEXT_LIMIT, cv2_utf16_len
from ..common.utils import construct_emoji_substituter, re_user_side_emoji

# --- Discord component type ids ---------------------------------------------------

ACTION_ROW = 1
BUTTON = 2
SECTION = 9
TEXT_DISPLAY = 10
THUMBNAIL = 11
MEDIA_GALLERY = 12
FILE = 13
SEPARATOR = 14
CONTAINER = 17

_MAX_GALLERY_ITEMS = 10
_MAX_SECTION_TEXTS = 3
_MAX_TOP_LEVEL = 10
_MAX_ROW_BUTTONS = 5  # Discord's per-action-row cap
_MAX_BUTTON_LABEL = 80

Node = dict[str, t.Any]


# --- classification ---------------------------------------------------------------


def kind(node: Node) -> str:
    """Classify a node into a builder "kind" (``text``, ``container``, …).

    An action row is only ever a link-button row here, so it classifies as
    ``link_button`` (the same kind as a bare link button).
    """
    ty = node.get("type")
    return {
        CONTAINER: "container",
        TEXT_DISPLAY: "text",
        SECTION: "section",
        MEDIA_GALLERY: "media",
        SEPARATOR: "separator",
        FILE: "file",
        THUMBNAIL: "thumbnail",
        ACTION_ROW: "link_button",
        BUTTON: "link_button",
    }.get(ty, "unknown")  # type: ignore[arg-type]


# --- constructors -----------------------------------------------------------------


# --- field specs + mutators (pure) ------------------------------------------------


def _button_of(node: Node) -> Node:
    """The FIRST button inside a link-button node (unwrapping the action row).

    Only correct where exactly one button is possible — a section accessory, or a label
    preview. For validating or sanitizing a row, use :func:`_buttons_of`: a row loaded
    from an existing post can carry up to five.
    """
    if node.get("type") == ACTION_ROW:
        return node["components"][0]
    return node


def _buttons_of(node: Node) -> list[Node]:
    """Every button in a link-button node (a row may hold several; a bare
    button, one)."""
    if node.get("type") == ACTION_ROW:
        return node.get("components", [])
    return [node]


# --- add-flow catalogue -----------------------------------------------------------


# --- tree navigation + edits (pure) -----------------------------------------------


# --- labels + previews ------------------------------------------------------------


# --- preview sanitisation ---------------------------------------------------------

# A section accessory the preview can fall back on if none is set yet: a tiny 1px
# transparent placeholder would need a URL, so instead an incomplete section is rendered
# as a plain placeholder text (see ``_sanitize_node``).


def _placeholder(message: str) -> Node:
    return {"type": TEXT_DISPLAY, "content": f"-# ⚠️ {message}"}


def _accessory_ok(accessory: Node) -> bool:
    k = kind(accessory)
    if k == "thumbnail":
        return bool(accessory.get("media", {}).get("url"))
    if k == "link_button":
        button = _button_of(accessory)
        return bool(button.get("label") and button.get("url"))
    return False


def sanitize_for_preview(nodes: list[Node]) -> list[Node]:
    """Return a deep copy of ``nodes`` that is always valid to send to Discord.

    Mid-construction states (an empty container, a section without an accessory, an
    empty text block, …) would make Discord reject the live-preview edit. Each such node
    is downgraded to a placeholder text so the preview never breaks, while the real
    (possibly incomplete) nodes stay in the builder's state.
    """
    return [_sanitize_node(node) for node in nodes]


def _sanitize_node(node: Node) -> Node:
    k = kind(node)
    if k == "container":
        children = [_sanitize_node(child) for child in node.get("components", [])]
        if not children:
            children = [_placeholder("empty container — open it to add blocks")]
        return {**node, "components": children}
    if k == "section":
        texts = node.get("components", [])
        accessory = node.get("accessory")
        if not texts or not accessory or not _accessory_ok(accessory):
            return _placeholder("section — add 1–3 text blocks and an accessory")
        good_texts = [t for t in texts if (t.get("content") or "").strip()]
        if not good_texts:
            return _placeholder("section — add some text")
        return {**node, "components": good_texts, "accessory": accessory}
    if k == "text":
        if not (node.get("content") or "").strip():
            return _placeholder("empty text block")
        return node
    if k == "media":
        items = [i for i in node.get("items", []) if i.get("media", {}).get("url")]
        if not items:
            return _placeholder("empty media gallery")
        return {**node, "items": items}
    if k == "link_button":
        buttons = _buttons_of(node)
        if not buttons or not all(b.get("label") and b.get("url") for b in buttons):
            return _placeholder("incomplete link button")
        return node
    return node


# --- emoji substitution -----------------------------------------------------------


def substitute_emoji(nodes: list[Node], emoji_dict: dict[str, h.Emoji]) -> list[Node]:
    """Return a copy of ``nodes`` with ``:name:`` shortcodes resolved to mentions.

    Discord renders a custom emoji only from a full ``<:name:id>`` mention; a bare
    ``:armor:`` posts as those seven literal characters. Every *code*-driven CV2 post
    resolves them on the way out (``components.finalize_cv2_post``), but the web
    builder sent the author's node tree verbatim — so a post whose canvas and
    confirmation dialog both showed real emoji (the client renderer resolves shortcodes
    against the same guild map) arrived in the channel as raw text.

    Only a text display's ``content`` is rewritten. A button *label* is deliberately
    left alone: Discord renders no markdown there, and a button's emoji is a field of
    its own (``buttonEmojiFor`` in ``cv2_model.js`` fills it), so substituting would put
    visible ``<:name:id>`` text on the button — which is what the renderer models by
    drawing labels as plain text.

    Mentions already in the tree — a draft seeded from a live post carries them — are
    left untouched by :func:`construct_emoji_substituter`, so this is idempotent.
    """
    substituter = construct_emoji_substituter(emoji_dict)

    def walk(node: Node) -> Node:
        out = dict(node)
        if out.get("type") == TEXT_DISPLAY and isinstance(out.get("content"), str):
            out["content"] = re_user_side_emoji.sub(substituter, out["content"])
        children = out.get("components")
        if isinstance(children, list):
            out["components"] = [
                walk(child) if isinstance(child, dict) else child for child in children
            ]
        accessory = out.get("accessory")
        if isinstance(accessory, dict):
            out["accessory"] = walk(accessory)
        return out

    return [walk(node) if isinstance(node, dict) else node for node in nodes]


# --- validation -------------------------------------------------------------------


def _total_text_len(nodes: list[Node]) -> int:
    """Total displayable text across the tree, in the UTF-16 units Discord counts."""
    total = 0
    for node in nodes:
        k = kind(node)
        if k == "text":
            total += cv2_utf16_len(str(node.get("content") or ""))
        children = node.get("components")
        if isinstance(children, list):
            total += _total_text_len(children)
    return total


def validate(nodes: list[Node]) -> list[str]:
    """Return human-readable problems that would make the message invalid to send."""
    problems: list[str] = []
    if not nodes:
        problems.append("The message is empty — add at least one block.")
    if len(nodes) > _MAX_TOP_LEVEL:
        problems.append(
            f"Too many top-level blocks ({len(nodes)}); Discord allows "
            f"{_MAX_TOP_LEVEL}. Group some inside a container."
        )
    # Discord caps a CV2 message's total text at 4000 UTF-16 units. Without this the
    # only symptom is Discord rejecting the send, long after the text was written —
    # ``dd.common.components`` has enforced it for autoposts all along, but the builder
    # never consulted it.
    text_len = _total_text_len(nodes)
    if text_len > CV2_TEXT_LIMIT:
        problems.append(
            f"Too much text ({text_len} of {CV2_TEXT_LIMIT} characters). "
            f"Shorten it by about {text_len - CV2_TEXT_LIMIT} characters."
        )
    for node in nodes:
        _validate_node(node, problems)
    return problems


def _validate_node(node: Node, problems: list[str]) -> None:
    k = kind(node)
    if k == "container":
        children = node.get("components", [])
        if not children:
            problems.append("A container is empty — add a block inside or delete it.")
        for child in children:
            _validate_node(child, problems)
    elif k == "section":
        texts = node.get("components", [])
        if not (1 <= len(texts) <= _MAX_SECTION_TEXTS):
            problems.append(
                f"A section must have 1–{_MAX_SECTION_TEXTS} text blocks "
                f"(it has {len(texts)})."
            )
        if not node.get("accessory"):
            problems.append("A section is missing its accessory (thumbnail or button).")
    elif k == "text":
        if not (node.get("content") or "").strip():
            problems.append("A text block is empty.")
    elif k == "media":
        items = node.get("items") or []
        if not items:
            problems.append("A media gallery has no images.")
        elif len(items) > _MAX_GALLERY_ITEMS:
            problems.append(
                f"A media gallery has {len(items)} images; Discord allows "
                f"{_MAX_GALLERY_ITEMS}."
            )
    elif k == "link_button":
        buttons = _buttons_of(node)
        if not buttons:
            problems.append("A button row has no buttons.")
        if len(buttons) > _MAX_ROW_BUTTONS:
            problems.append(
                f"A button row has {len(buttons)} buttons; Discord allows "
                f"{_MAX_ROW_BUTTONS}. Split them across two rows."
            )
        for index, button in enumerate(buttons, start=1):
            where = f"Button {index}" if len(buttons) > 1 else "A link button"
            label = str(button.get("label") or "")
            url = str(button.get("url") or "")
            if not (label and url):
                problems.append(
                    f"{where} needs both a label and a URL."
                    if len(buttons) > 1
                    else "A link button needs both a label and a URL."
                )
                continue
            # A URL without a scheme is the common typo ("kyber3000.com"). The preview
            # silently drops such a button (the renderer only emits http(s) hrefs), so
            # without this the first sign of trouble is Discord refusing the send.
            if not url.startswith(("http://", "https://")):
                problems.append(
                    f"{where}'s URL must start with http:// or https:// (got "
                    f"{url[:40]!r})."
                )
            if len(label) > _MAX_BUTTON_LABEL:
                problems.append(
                    f"{where}'s label is {len(label)} characters; Discord allows "
                    f"{_MAX_BUTTON_LABEL}."
                )
