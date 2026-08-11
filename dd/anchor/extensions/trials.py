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

"""Trials of Osiris — anchor producer for the ``trials`` followable.

Like ``weekly_reset``, the Trials post historically had *no* anchor producer: a human
hand-authored it and beacon mirrored it. This extension gives it the same hybrid
pipeline, built on :mod:`dd.anchor.hybrid_post_core`:

1. At Friday reset a cron seeds an uncrossposted **draft** (the :class:`TrialsContext`)
   to the ``trials_draft`` :class:`~dd.common.schemas.RotationData` row.
2. The team fills it — the featured maps and the bonus focus pool — through the
   owner-authenticated **web form** (``/trials``; ``/trials create`` links to it). Auth
   is enforced centrally by the Discord-OAuth middleware in ``web_auth.py``.
3. On publish the assembled post is crossposted to the ``trials`` followable channel
   (see :mod:`dd.common.settings`);
   beacon mirrors it to followers as usual.

The Trials post is effectively **fully manual**: the Bungie API does not expose the
weekly featured maps or the Saint-14 "bonus focus pool" (only the full focus pool), so
there are no API-seeded fields. ``Live until`` is the one derived value — the next
Tuesday reset, when the Trials weekend ends. The focus pool is manifest-linked (light.gg
deep links + weapon-type emoji) via the shared weapon pool + resolver.
"""

import asyncio
import dataclasses
import logging
import re
import typing as t
from pathlib import Path

import aiohttp.web
import hikari as h
import lightbulb as lb

from dd.hmessage import HMessage

from ...common import (
    feeds as dd_feeds,
    rotation_schema,
    schemas,
    settings,
)
from ...common.bot import CachedFetchBot
from ...common.components import (
    finalize_cv2_post,
    footer_button_specs,
)
from ...common.utils import fetch_emoji_dict
from .. import hybrid_post_core, web
from ..hybrid_post_core import (
    DraftMeta,
    HybridPostSpec,
    WeaponRef,
    build_cv2,
    current_reset_ts,
    next_reset_ts,
    resolve_weapon,
)

logger = logging.getLogger(__name__)
loader = lb.Loader()

# ---------------------------------------------------------------------------
# Slugs + static chrome
# ---------------------------------------------------------------------------

#: RotationData slugs for the in-progress draft, carried-over config, and metadata.
DRAFT_SLUG = "trials_draft"
CONFIG_SLUG = "trials_config"
META_SLUG = "trials_meta"
#: The editor-managed loot pool + schedule (a rotation-editor type, Dares-style sets).
LOOT_SLUG = rotation_schema.TRIALS_LOOT_SLUG

#: The post's fixed title (a masked link with an italic "of"), verbatim from the
#: hand-authored posts.
TRIALS_TITLE = "[Trials *of* Osiris](https://kyber3000.com/Trialspost)"
#: The Rewards section — two static lines the team never varies.
TRIALS_REWARDS: tuple[str, ...] = (
    "All Trials weapons available",
    "Weapon Attunement available",
)
#: The static sign-off line (an ``### `` H3 header + the Kyber cheer emoji). Kept as
#: body text; the link footer is now the button row (see TRIALS_GUIDES).
TRIALS_FOOTER_LINE = "### Good luck in your games!  :gscheer:"

#: Post-specific footer guide button(s); Support is appended by
#: ``footer_button_specs``. Drives both the live post and the web-form preview.
TRIALS_GUIDES: tuple[tuple[str, str], ...] = (
    ("Trials Report", "https://kyber3000.com/Trialspost"),
)

# The Trials bonus-focus-pool rotation: a fixed loop of curated weapon sets. Trials
# cycles through these one per *active* weekend (Iron Banner "No Trials" weeks are
# skipped, which falls out naturally because the cursor only advances when a post is
# committed). The loop is now edited through the rotation editor (the ``trials_loot``
# type — a Dares-style set pool + looping schedule) and stored in its own RotationData
# row; this baked constant is the single source of truth for the editor's starting
# document AND this producer's fallback when that row is absent (re-exported from the
# schema layer so the two can't drift). Seeded from the "Trials Bonus Pools" tab of the
# rotation spreadsheet as a one-off — the bot never reads the sheet at runtime.
DEFAULT_LOOT_SETS = rotation_schema.TRIALS_DEFAULT_LOOT_SETS


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class TrialsContext:
    """Every fillable slot in the Trials of Osiris post.

    Round-trips through the ``trials_draft`` RotationData row so an edit session
    survives restarts and can be resumed by any owner.
    """

    #: The "Live until" boundary shown in the post (a Tuesday 17:00 UTC reset). A Trials
    #: weekend runs Fri→Tue, so a fresh draft defaults this to the *upcoming* reset (the
    #: next Tuesday), not the one that just passed. Purely the display boundary — the
    #: post's lifecycle period key is stamped separately (see ``_now_reset_ts``).
    reset_ts: int
    #: Featured map names, in post order (human-entered free text).
    featured_maps: list[str] = dataclasses.field(default_factory=list)
    #: This week's bonus focus-pool weapons (manifest-linked where resolvable).
    focus_pool: list[WeaponRef] = dataclasses.field(default_factory=list)
    image_url: str | None = None
    #: Optional ad-hoc info notes.
    notes: list[str] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict[str, t.Any]:
        return {
            "reset_ts": self.reset_ts,
            "featured_maps": list(self.featured_maps),
            "focus_pool": [w.to_dict() for w in self.focus_pool],
            "image_url": self.image_url,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, d: t.Mapping[str, t.Any]) -> "TrialsContext":
        return cls(
            reset_ts=int(d["reset_ts"]),
            featured_maps=[str(m) for m in d.get("featured_maps") or []],
            focus_pool=[WeaponRef.from_dict(w) for w in d.get("focus_pool") or []],
            image_url=d.get("image_url"),
            notes=[str(n) for n in d.get("notes") or []],
        )


def _default_loot_sets() -> list[list[str]]:
    return [list(s) for s in DEFAULT_LOOT_SETS]


@dataclasses.dataclass
class TrialsConfig:
    """Carried-over data so each Friday's fresh draft starts pre-filled, not blank.

    Also holds the bonus-focus-pool rotation *cursor*: ``last_loot_set_index`` is the
    memory of the last set used, so the next draft defaults to the following set in the
    loop. ``-1`` = "none used yet" — the first draft is set 0. The loop itself (the pool
    of sets + the schedule) lives in the editor-managed ``trials_loot`` RotationData
    row, not here — see :func:`load_loot_rotation`.
    """

    default_image_url: str | None = None
    last_featured_maps: list[str] = dataclasses.field(default_factory=list)
    last_loot_set_index: int = -1

    def to_dict(self) -> dict[str, t.Any]:
        return {
            "default_image_url": self.default_image_url,
            "last_featured_maps": list(self.last_featured_maps),
            "last_loot_set_index": self.last_loot_set_index,
        }

    @classmethod
    def from_dict(cls, d: t.Mapping[str, t.Any] | None) -> "TrialsConfig":
        if not d:
            return cls()
        return cls(
            default_image_url=d.get("default_image_url"),
            last_featured_maps=[str(m) for m in d.get("last_featured_maps") or []],
            last_loot_set_index=int(d.get("last_loot_set_index", -1)),
        )


# ---------------------------------------------------------------------------
# Loot rotation (editor-managed pool + schedule; producer-owned cursor)
# ---------------------------------------------------------------------------


def _strip_weapon_type(value: str) -> str:
    """Drop a trailing ``" (Type)"`` the rotation editor's item autocomplete appends.

    The editor's set UI stores weapon values as ``"The Immortal (Submachine Gun)"``
    (type disambiguation), but :func:`resolve_weapon` matches a bare manifest name or a
    numeric hash. Stripping the suffix lets doc-sourced names resolve to manifest-linked
    WeaponRefs; a bare name (e.g. the baked default) passes through unchanged.
    """
    return re.sub(r"\s*\([^()]*\)\s*$", "", value).strip()


def _expand_loot_rotation(doc: t.Mapping[str, t.Any] | None) -> list[list[str]]:
    """Expand a ``trials_loot`` doc into the looping list of weapon-name lists.

    ``{sets: [{name, weapons}], schedule: [name, …]}`` → the ordered, looping list of
    weapon-name lists the cursor walks (a schedule entry naming no set is dropped — the
    editor's save gate blocks that, but be defensive). Falls back to the baked default
    loop when the doc is absent/empty, so the rotation works before anyone edits it.
    """
    if doc:
        by_name = {
            str(s.get("name", "")): [
                _strip_weapon_type(str(w)) for w in s.get("weapons") or []
            ]
            for s in doc.get("sets") or []
        }
        rotation = [by_name[n] for n in doc.get("schedule") or [] if n in by_name]
        if rotation:
            return rotation
    return _default_loot_sets()


async def load_loot_rotation() -> list[list[str]]:
    """The current loot loop, sourced from the editor-managed ``trials_loot`` doc."""
    return _expand_loot_rotation(await schemas.RotationData.get_data(LOOT_SLUG))


def _next_in_rotation(rotation: list[list[str]], last_index: int) -> list[str]:
    """The weapon-name list the next draft defaults to (set after ``last_index``)."""
    if not rotation:
        return []
    return rotation[(last_index + 1) % len(rotation)]


def _match_in_rotation(rotation: list[list[str]], names: t.Iterable[str]) -> int | None:
    """Index of the rotation entry equal to ``names`` (case/order-insensitive) or None.

    A committed post's focus pool is matched here to decide which set was "used".
    """
    wanted = {n.strip().lower() for n in names if n and n.strip()}
    if not wanted:
        return None
    for i, s in enumerate(rotation):
        if {n.strip().lower() for n in s} == wanted:
            return i
    return None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


async def load_config() -> TrialsConfig:
    return TrialsConfig.from_dict(await schemas.RotationData.get_data(CONFIG_SLUG))


async def save_config(config: TrialsConfig) -> None:
    await schemas.RotationData.set_data(CONFIG_SLUG, config.to_dict())


async def load_draft() -> TrialsContext | None:
    data = await schemas.RotationData.get_data(DRAFT_SLUG)
    return TrialsContext.from_dict(data) if data else None


async def save_draft(ctx: TrialsContext) -> None:
    await schemas.RotationData.set_data(DRAFT_SLUG, ctx.to_dict())


async def load_meta() -> DraftMeta:
    return DraftMeta.from_dict(await schemas.RotationData.get_data(META_SLUG))


async def save_meta(meta: DraftMeta) -> None:
    await schemas.RotationData.set_data(META_SLUG, meta.to_dict())


# ---------------------------------------------------------------------------
# Draft build (no API — fully manual, seeded from the carried-over config)
# ---------------------------------------------------------------------------


async def build_draft_context(config: TrialsConfig | None = None) -> TrialsContext:
    """A fresh draft: the upcoming reset, carried-over maps + the NEXT loot set.

    The bonus focus pool defaults to the set after the last-used one (the editor-managed
    rotation loop); each name resolves to a manifest-linked :class:`WeaponRef`
    (light.gg), degrading to a plain name if the manifest is offline. Still editable.
    """
    config = config or await load_config()
    rotation = await load_loot_rotation()
    items = await get_weapon_items()
    focus_pool = [
        w
        for name in _next_in_rotation(rotation, config.last_loot_set_index)
        if (w := resolve_weapon(name, items))
    ]
    return TrialsContext(
        # Live until the *upcoming* reset — the weekend runs Fri→Tue, so once this
        # Tuesday's reset has passed the live window ends at the next Tuesday.
        reset_ts=next_reset_ts(current_reset_ts()),
        featured_maps=list(config.last_featured_maps),
        focus_pool=focus_pool,
        image_url=config.default_image_url,
    )


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

#: Weapon-type emoji names present in the Kyber guild. :func:`build_body` prefixes each
#: focus-pool weapon with its type emoji when the guild has one, falling back to the
#: generic ``:weapon:`` otherwise (the guild has no ``bow`` emoji, for instance) —
#: mirroring xûr's ``emoji_include_list`` gate so a missing type never leaks a literal
#: ``:bow:`` into the post. Populated once at startup from the guild emoji dict (see
#: :func:`_on_started`); defaults to just ``{"weapon"}`` so a not-yet-warmed
#: process shows the generic icon.
_weapon_emoji_names: frozenset[str] = frozenset({"weapon"})


def _emoji_name_for(emoji_name: str | None, available: t.Container[str]) -> str:
    """The best emoji name for a weapon type — delegates to the shared
    :func:`hybrid_post_core.weapon_emoji_name` (the same helper Iron Banner uses)."""
    return hybrid_post_core.weapon_emoji_name(emoji_name, available)


def _weapon_emoji(w: WeaponRef) -> str:
    """The guild emoji name to prefix ``w`` with (see :func:`_emoji_name_for`)."""
    return _emoji_name_for(w.emoji_name, _weapon_emoji_names)


async def _prewarm_weapon_emoji(bot: CachedFetchBot) -> None:
    """Populate :data:`_weapon_emoji_names` from the guild emoji dict (once at startup).

    Reuses the shared short-TTL preview-emoji cache. On failure the dict is empty and
    the module keeps its ``{"weapon"}`` default, so posts degrade to the generic icon.
    """
    global _weapon_emoji_names
    emoji = await hybrid_post_core.preview_emoji_dict(bot)
    if emoji:
        _weapon_emoji_names = frozenset(emoji) | {"weapon"}


def build_body(ctx: TrialsContext) -> str:
    """The full post markdown, with ``:emoji:`` tokens still un-substituted.

    Each focus-pool weapon is prefixed with its weapon-type emoji (``:scout_rifle:`` …),
    falling back to the generic ``:weapon:`` for a type the guild has no emoji for — see
    :data:`_weapon_emoji_names`.
    """
    lines: list[str] = [
        f"# {TRIALS_TITLE}",
        "",
        f"Live until <t:{ctx.reset_ts}:f>",
    ]

    maps = [m for m in ctx.featured_maps if m]
    if maps:
        lines += ["### Featured Maps", ""]
        lines += [f"- {m}" for m in maps]

    lines += ["### Rewards", "", *TRIALS_REWARDS]

    pool = [w for w in ctx.focus_pool if w and w.name]
    if pool:
        lines += ["", "**This Week's Bonus Focus Pool**"]
        # No "- " bullet: the leading weapon-type emoji is the marker.
        lines += [f":{_weapon_emoji(w)}: {w.markdown()}" for w in pool]

    for note in ctx.notes:
        if note:
            lines += ["", f":info: {note}"]

    lines.append(TRIALS_FOOTER_LINE)
    return "\n".join(lines)


async def format_trials(ctx: TrialsContext, bot: CachedFetchBot) -> HMessage:
    """Render the context to a Components V2 :class:`HMessage`."""
    hmsg = build_cv2(
        build_body(ctx),
        ctx.image_url,
        buttons=footer_button_specs(guides=TRIALS_GUIDES),
    )
    # Resolve :emoji: then cap CV2 text (naive front-to-back truncate + CRITICAL alert).
    return await finalize_cv2_post(
        hmsg,
        await fetch_emoji_dict(bot),
        post_name=dd_feeds.FEEDS["trials"].display_name,
    )


async def _render_for_spec(ctx: TrialsContext, bot: CachedFetchBot) -> HMessage:
    """``HybridPostSpec.render`` hook, indirecting through the module global so a test
    that monkeypatches ``format_trials`` is honoured by the shared publish core."""
    return await format_trials(ctx, bot)


def _now_reset_ts() -> int:
    """``HybridPostSpec.current_reset_ts`` hook: the current reset-period boundary.

    Trials reuses the weekly Tuesday ``current_reset_ts()`` as its period key even
    though a weekend runs Fri→Tue: a Friday post stamps the *preceding* Tuesday and
    stays "current" until the next Tuesday reset — exactly the live window. Indirects
    through the module global so a test monkeypatching ``current_reset_ts`` steers the
    shared route code.
    """
    return current_reset_ts()


def validate_post(ctx: TrialsContext) -> list[str]:
    """Problems that would make the post empty or break Components V2 limits."""
    problems: list[str] = []
    body = build_body(ctx)
    if len(body) > 3900:
        problems.append(
            f"Post is too long ({len(body)}/3900 chars) — trim some sections."
        )
    if not (any(m for m in ctx.featured_maps) or any(w for w in ctx.focus_pool)):
        problems.append(
            "Post looks empty — add at least a featured map or a focus-pool weapon."
        )
    if ctx.image_url and not ctx.image_url.startswith(("http://", "https://")):
        problems.append("Image URL must start with http:// or https://.")
    if not settings.get_followable_channel_sync("trials"):
        problems.append(
            "Trials of Osiris has no channel to post to yet — pick one on the Feeds "
            "page."
        )
    return problems


# ---------------------------------------------------------------------------
# Manifest weapon pool (for the focus-pool picker + resolver)
# ---------------------------------------------------------------------------


async def get_weapon_items() -> list[tuple[str, int, str, int, str]]:
    """The manifest weapon pool the focus picker searches.

    A thin delegate to the process-wide :func:`hybrid_post_core.get_weapon_pool`, which
    builds (once) and caches the pool shared with every other producer, so the item scan
    isn't repeated per extension.
    """
    return await hybrid_post_core.get_weapon_pool()


# ---------------------------------------------------------------------------
# Web form — server-side context + bootstrap
# ---------------------------------------------------------------------------

_FORM_HTML_PATH = (
    Path(__file__).resolve().parent.parent / "web_static" / "trials_form.html"
)

#: Serialises read-modify-write of the shared draft doc (single bot process).
_draft_lock = asyncio.Lock()


async def _context_from_payload(payload: t.Mapping[str, t.Any]) -> TrialsContext:
    """Build a :class:`TrialsContext` from the form JSON, entirely server-side.

    The client is never trusted for security-relevant transforms: each focus-pool value
    is re-resolved server-side (a manifest hash or typed name -> WeaponRef) against the
    weapon pool, and the maps/notes are split per-line and trimmed.
    """
    ctx = TrialsContext(
        reset_ts=int(payload.get("reset_ts") or next_reset_ts(current_reset_ts()))
    )
    ctx.featured_maps = [
        line.strip()
        for line in str(payload.get("maps_text", "")).splitlines()
        if line.strip()
    ]
    items = await get_weapon_items()
    ctx.focus_pool = [
        w
        for value in payload.get("focus_pool") or []
        if (w := resolve_weapon(str(value), items))
    ]
    ctx.image_url = str(payload.get("image_url", "")).strip() or None
    ctx.notes = [
        line.strip()
        for line in str(payload.get("notes_text", "")).splitlines()
        if line.strip()
    ]
    return ctx


async def _form_loot_sets() -> tuple[list[dict[str, t.Any]], str | None]:
    """The named loot sets (resolved to manifest weapons) + this week's set name.

    Powers the form's set-card picker: sourced from the editor-managed
    ``trials_loot`` doc (falling back to the baked default doc), each set's weapon names
    are stripped of the editor's ``" (Type)"`` suffix and resolved to manifest-linked
    weapon refs so the client can hydrate them straight into the focus-pool picker.
    ``current`` is the set the cursor points at for this weekend — the one a fresh
    draft's focus pool defaults to — mirroring :func:`_expand_loot_rotation`'s schedule
    filtering so the "(this week)" hint matches the set the producer would pick.
    """
    doc = (
        await schemas.RotationData.get_data(LOOT_SLUG)
        or rotation_schema.trials_loot_default_doc()
    )
    items = await get_weapon_items()
    sets = [
        {
            "name": str(s.get("name", "")),
            "weapons": [
                w.to_dict()
                for name in s.get("weapons") or []
                if (w := resolve_weapon(_strip_weapon_type(str(name)), items))
            ],
        }
        for s in doc.get("sets") or []
    ]
    names = {s["name"] for s in sets}
    schedule = [str(n) for n in doc.get("schedule") or [] if str(n) in names]
    current = None
    if schedule:
        nxt = ((await load_config()).last_loot_set_index + 1) % len(schedule)
        current = schedule[nxt]
    return sets, current


async def _card_emoji_urls(
    loot_sets: list[dict[str, t.Any]], draft: TrialsContext
) -> dict[str, str]:
    """Guild emoji URLs (by emoji name) for the weapon-type icons on the set cards.

    Only the emoji names actually referenced by the sets + the current draft, plus the
    generic ``weapon`` fallback, so the payload stays tiny. Empty if the guild dict is
    not available yet — the cards then render names without icons.
    """
    emoji = await hybrid_post_core.preview_emoji_dict(web.get_bot())
    if not emoji:
        return {}
    names: set[str] = {
        str(w["emoji_name"])
        for s in loot_sets
        for w in s["weapons"]
        if w.get("emoji_name")
    }
    names |= {w.emoji_name for w in draft.focus_pool if w.emoji_name}
    # Key each weapon's own slug to the URL of the emoji it should show (type icon, a
    # bow-style alias, or the generic weapon), so the client looks up by emoji_name and
    # gets the same fallback chain the post uses. Always include the "weapon" fallback.
    urls = {"weapon": str(emoji["weapon"].url)} if "weapon" in emoji else {}
    for name in names:
        resolved = emoji.get(_emoji_name_for(name, emoji))
        if resolved:
            urls[name] = str(resolved.url)
    return urls


async def _build_bootstrap(draft: TrialsContext, meta: DraftMeta) -> dict[str, t.Any]:
    """The page bootstrap JSON: the draft, loot sets, toggles and lifecycle flags."""
    config = await load_config()
    loot_sets, current_loot_set = await _form_loot_sets()
    return {
        "draft": draft.to_dict(),
        # The editor-managed loot sets (resolved weapons) + which one is this weekend's,
        # for the form's set-card picker. The pool is edited in the rotation editor
        # (linked from the form); the form only picks the set for this weekend.
        "loot_sets": loot_sets,
        "current_loot_set": current_loot_set,
        # emoji name -> guild emoji URL, for the weapon-type icons on the cards.
        "emoji_urls": await _card_emoji_urls(loot_sets, draft),
        "default_image_url": config.default_image_url or "",
        "accent_color": str(await settings.get_embed_default_color()),
        # Whether a post already exists *for the current period* (Trials may skip a
        # week; False is a normal "no Trials post yet" state). Drives which action
        # buttons show: Create-* when there's none, Edit/Delete when there is.
        "post_this_period": meta.is_current(current_reset_ts()),
        "crossposted": meta.crossposted,
    }


def _record_loot_set_used(
    config: TrialsConfig, ctx: TrialsContext, rotation: list[list[str]]
) -> None:
    """Advance the rotation cursor to the set this published post used.

    Matches the post's focus pool against the loop's sets: a match (the usual case — the
    default IS a set, and a manual pick of another pool matches it) jumps the cursor to
    that set; a non-empty pool that matches nothing (a custom/edited pool) advances by
    one so the loop still progresses; an empty pool is a no-op. Called once per period
    (on the crosspost transition), so ``+ 1`` is from the previous period's set.
    """
    names = [w.name for w in ctx.focus_pool if w and w.name]
    if not names:
        return
    matched = _match_in_rotation(rotation, names)
    if matched is not None:
        config.last_loot_set_index = matched
    elif rotation:
        config.last_loot_set_index = (config.last_loot_set_index + 1) % len(rotation)


async def _advance_loot_cursor(ctx: TrialsContext) -> None:
    """``HybridPostSpec.on_published`` hook: record the published set as last-used.

    Fires once, only when a post goes live (uncrossposted -> crossposted) — so Iron
    Banner "No Trials" weekends (a cron draft that's deleted, never published) never
    advance the rotation, keeping it in sync with active weekends.
    """
    config = await load_config()
    rotation = await load_loot_rotation()
    _record_loot_set_used(config, ctx, rotation)
    await save_config(config)


async def _persist_carryover(
    payload: t.Mapping[str, t.Any], ctx: TrialsContext
) -> None:
    """Persist carried-over config on every committed post (Create/Edit).

    Stores this week's maps as the carry-over and the image as the default when the
    form's "use as default" box is ticked (an empty URL with the box ticked clears the
    default). The loot-set rotation cursor is advanced separately, only on publish, by
    :func:`_advance_loot_cursor` (the ``on_published`` hook).
    """
    config = await load_config()
    config.last_featured_maps = list(ctx.featured_maps)
    if payload.get("set_default_image"):
        config.default_image_url = ctx.image_url
    await save_config(config)


# ---------------------------------------------------------------------------
# Web routes — thin wrappers over the shared hybrid_post_core handlers
# ---------------------------------------------------------------------------
# Auth is enforced centrally by the web_auth middleware; these pass this producer's
# ``_SPEC`` and the live bot from ``web.get_bot()`` (read at call time) into the shared
# handlers, which answer a ``None`` bot with the shared 503 themselves.


async def _handle_form_get(request: aiohttp.web.Request) -> aiohttp.web.Response:
    return await hybrid_post_core.form_get(_SPEC, request, web.get_bot())


async def _handle_create(request: aiohttp.web.Request) -> aiohttp.web.Response:
    return await hybrid_post_core.post_action(
        _SPEC, request, web.get_bot(), create=True
    )


async def _handle_edit(request: aiohttp.web.Request) -> aiohttp.web.Response:
    return await hybrid_post_core.post_action(
        _SPEC, request, web.get_bot(), create=False
    )


async def _handle_preview(request: aiohttp.web.Request) -> aiohttp.web.Response:
    return await hybrid_post_core.preview(_SPEC, request, web.get_bot())


async def _handle_delete(request: aiohttp.web.Request) -> aiohttp.web.Response:
    return await hybrid_post_core.delete(_SPEC, request, web.get_bot())


#: Wires this producer to the shared hybrid_post_core (built after every hook exists).
_SPEC = HybridPostSpec(
    followable_key="trials",
    post_noun="Trials post",
    form_path="/trials",
    current_reset_ts=_now_reset_ts,
    render=_render_for_spec,
    validate=validate_post,
    build_body=build_body,
    load_draft=load_draft,
    save_draft=save_draft,
    build_context=build_draft_context,
    context_from_payload=_context_from_payload,
    load_meta=load_meta,
    save_meta=save_meta,
    build_bootstrap=_build_bootstrap,
    persist_default_image=_persist_carryover,
    form_html_path=_FORM_HTML_PATH,
    draft_lock=_draft_lock,
    on_published=_advance_loot_cursor,
    footer_guides=TRIALS_GUIDES,
)


def register_trials_routes(app: aiohttp.web.Application) -> None:
    """Add the Trials web-form routes to the shared persistent app."""
    app.router.add_get("/trials", _handle_form_get)
    app.router.add_post("/trials/create", _handle_create)
    app.router.add_post("/trials/edit", _handle_edit)
    app.router.add_post("/trials/preview", _handle_preview)
    app.router.add_post("/trials/delete", _handle_delete)


web.register_routes(register_trials_routes)
# Also tells the feeds page that this feed is one a human writes — see
# hybrid_post_core.register_spec.
hybrid_post_core.register_spec(_SPEC)
web.register_card(
    web.Card(
        "Trials of Osiris",
        "Write this weekend's Trials post and publish it.",
        "/trials",
        web.CardGroup.SEND,
        20,
        featured=True,
    )
)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


@loader.listener(h.StartedEvent)
async def _on_started(
    event: h.StartedEvent, bot: CachedFetchBot = lb.di.INJECTED
) -> None:
    if not await settings.get_followable_channel("trials"):
        return

    # Prewarm the manifest weapon pool so the first form load is fast.
    asyncio.create_task(get_weapon_items())
    # Learn which weapon-type emoji the guild has, so build_body can prefix each focus
    # weapon with its type icon (and fall back to :weapon: for a missing type).
    asyncio.create_task(_prewarm_weapon_emoji(bot))


# The web form's routes are always registered (above) and the form is reached from the
# control-panel card grid, which replaced the former `/trials create` command. There is
# no reset-weekend cron: the post is created and published entirely from the web form's
# Create/Publish buttons.
