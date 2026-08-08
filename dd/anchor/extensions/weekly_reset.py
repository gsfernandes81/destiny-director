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

"""Weekly Reset Overview — anchor producer for the ``weekly_reset`` followable.

Unlike the other followables, ``weekly_reset`` historically had *no* anchor producer:
a human hand-authored the post and dropped it into the announce channel, and beacon
mirrored it. This extension automates that authoring:

1. At Tuesday reset a cron derives everything the Bungie API can give us, merges the
   carried-over curated bits, and persists a **draft** (the WeeklyResetContext) to the
   ``weekly_reset_draft`` :class:`~dd.common.schemas.RotationData` row.
2. The team fills in the weapons/rotators/prose the API can't supply through an
   owner-authenticated **web form** (``/weekly_reset create`` links to it; the routes
   live at the bottom of this module, and Discord-OAuth auth is enforced centrally by
   the ``web_auth.py`` middleware), backed by the same data/render/publish core below.
3. On publish the assembled post is crossposted to the ``weekly_reset`` followable
   channel (see :mod:`dd.common.settings`); beacon mirrors it as usual.

Everything the API can't supply (editorial prose, the featured raid/dungeon rotators,
Iron Banner/Trials schedule, Pantheon pair) carries over week-to-week in the
``weekly_reset_config`` :class:`~dd.common.schemas.RotationData` row.

This module is the UI-agnostic core: the context model, the persisted config, the
Bungie derivations, the Components V2 renderer, the manifest-backed option pools and the
publish path (:func:`publish_draft`). The post is created and published entirely from
the web form's Create/Publish buttons — there is no reset-day autopost cron. The Discord
input UI (a `/weekly_reset` command group + interactive editor) has been removed in
favour of the web form.
"""

import asyncio
import dataclasses
import datetime as dt
import json
import logging
import re
import typing as t
from pathlib import Path

import aiohttp.web
import aiosqlite
import hikari as h
import lightbulb as lb

from dd.hmessage import HMessage

from ...common import schemas, settings
from ...common.bot import CachedFetchBot
from ...common.components import (
    finalize_cv2_post,
    footer_button_specs,
)
from ...common.utils import fetch_emoji_dict

# ``utils`` is re-exported (not used directly here now): the publish path moved to
# ``hybrid_post_core``, but the tests patch ``wr.utils`` — the same module object the
# core uses — to steer that shared publish code.
from .. import (
    hybrid_post_core,
    utils as utils,
    web,
)
from ..hybrid_post_core import (
    DraftMeta,
    HybridPostSpec,
    WeaponRef,
    # Re-exported (used only via ``wr.<name>`` in the test suite, not in this module).
    _discord_error_note as _discord_error_note,
    build_cv2,
    compute_rotator,
    current_reset_ts,
    get_weapon_pool,
    next_reset_ts,
    resolve_weapon,
)
from . import (
    bungie_api as api,
    portal_ops,
)

logger = logging.getLogger(__name__)
loader = lb.Loader()

# ---------------------------------------------------------------------------
# Static chrome + curated defaults
# ---------------------------------------------------------------------------

#: RotationData slug for the in-progress draft (the WeeklyResetContext being edited).
DRAFT_SLUG = "weekly_reset_draft"
#: RotationData slug for the carried-over curated config (rotator order, schedules…).
CONFIG_SLUG = "weekly_reset_config"

#: Gap between an emoji token and its text. The hand-authored posts moved from a "┊"
#: bar to a plain two-space gap; the surrounding " {SEP} " template yields those two
#: spaces when this is empty.
SEP = ""
LEGACY_ACTIVITIES_URL = "https://kyberscorner.com/destiny2/legacy-activities/"
SIGN_OFF = "See you starside! \U0001f4ab"
#: Editorial suffix on the Zavala weapon line (tier text is not API-derivable).
ZAVALA_TIER_SUFFIX = "(T5 / rolls vary)"
TRIALS_IB_REMINDER = (
    "Reminder: Trials of Osiris is unavailable while Iron Banner is active."
)
#: The Fireteam/Quickplay Bonus Focus rotates daily; this static link is shown when no
#: specific weapon is set for the week.
QUICKPLAY_BONUS_DEFAULT = "[Changes Daily](https://www.light.gg)"
#: Emoji token prefixing the daily-rotating Quickplay Bonus Focus default line.
BONUS_DROP_EMOJI = "Bonus_Drop"
#: CONQUESTS (Seasonal Tab) difficulty tiers, in post order. The weekly tier->activity
#: assignment is Portal presentation data the Bungie API does not expose (activities
#: surface as untiered "…: Customize" entries), so this section is hand-curated — see
#: plans/weekly_reset_conquests.md.
CONQUEST_TIERS = ("Expert", "Master", "GM", "Ultimate")

# The seven Pantheon bosses (Pantheon 2.0 roster); the weekly Reprise/Encore pair is
# picked from here by the team — Bungie publishes no forward schedule.
PANTHEON_BOSSES = (
    "Argos",
    "Warpriest",
    "Gahlran",
    "Consecrated Mind",
    "Calus",
    "Morgeth",
    "Insurrection Prime",
)

# Featured raid/dungeon weekly rotators. NO Bungie endpoint exposes these, so they
# are a deterministic cycle over curated ordered lists anchored to a verified reset.
# The anchor + lists below are DEFAULTS; they live in the weekly_reset_config doc and
# are re-derived in tests from the sampled posts (see tests/test_weekly_reset.py).
#
# CONVENTION: the anchor (and the sampled reset timestamps in the tests) are the values
# shown on a post's "Resets:" line — i.e. the *next* Tuesday, when that week's content
# expires — NOT the week's start. `build_draft_context` therefore keys the rotator by
# `next_reset_ts(current_reset_ts())`; keying by `current_reset_ts()` (the week's start)
# instead is off by one week. See `next_reset_ts` and the `build_draft_context` note.
DEFAULT_ROTATOR_ANCHOR = 1782234000  # 2026-06-23 17:00 UTC "Resets:" boundary
DEFAULT_RAID_PAIRS: tuple[tuple[str, str], ...] = (
    ("King's Fall", "Garden of Salvation"),
    ("Root of Nightmares", "Deep Stone Crypt"),
    ("Crota's End", "Vault of Glass"),
    ("Last Wish", "Vow of the Disciple"),
)
DEFAULT_DUNGEON_PAIRS: tuple[tuple[str, str], ...] = (
    ("Spire of the Watcher", "Pit of Heresy"),
    ("Ghosts of the Deep", "Prophecy"),
    ("Warlord's Ruin", "Grasp of Avarice"),
)

# Curated Iron Banner week reset timestamps (unix, Tuesday 17:00 UTC). Trials is off
# on IB weeks. Team maintains this list ~once/episode; empty is fine (they toggle by
# hand).
DEFAULT_IB_WEEK_RESETS: tuple[int, ...] = ()

DEFAULT_CRUCIBLE_1V6 = "Sparrow Racing, Rumble"

# --- Bounded selector domains --------------------------------------------------------
# Small, stable fields are picked from Choice dropdowns instead of free-typed
# autocomplete, to cut the number of inputs. Each list is well under Discord's 25-choice
# limit. (Large domains — GM strikes ~46, weapons — stay on manifest autocomplete.)

# Crucible slots: the first mode of each is fixed; only the second (featured) mode is a
# weekly input. The full mode set (base modes + Labs variants) exceeds Discord's
# 25-choice limit, so the second mode uses autocomplete over CRUCIBLE_MODES rather than
# a Choice selector. Add new Labs modes here as Bungie ships them.
CRUCIBLE_3V3_FIRST = "Competitive"
CRUCIBLE_6V6_FIRST = "Control"
# On Iron Banner weeks the 6v6 Control playlist is replaced by Iron Banner, so the fixed
# first mode is swapped for this at render time (see the CRUCIBLE OPS block).
CRUCIBLE_6V6_FIRST_IB = "Iron Banner"
CRUCIBLE_MODES: tuple[str, ...] = (
    # Base modes
    "Clash",
    "Control",
    "Rift",
    "Zone Control",
    "Eruption",
    "Relic",
    "Collision",
    "Momentum Control",
    "Team Scorched",
    "Scorched",
    "Rumble",
    "Survival",
    "Elimination",
    "Countdown",
    "Breakthrough",
    "Lockdown",
    "Salvage",
    "Showdown",
    "Mayhem",
    "Supremacy",
    "Doubles",
    # Labs / rotator variants
    "Heavy Metal",
    "Heavy Metal Supremacy",
    "Hardware",
    "Hardware Mix",
    "Hardware Supremacy",
    "Checkmate Clash",
    "Checkmate Control",
    "Checkmate Countdown",
    "Checkmate Rumble",
    "Checkmate Survival",
    "Checkmate Mix",
    "Classic Mix",
    "Rush Remixed",
)  # > 25 -> autocomplete, not a Choice selector
# Raid / dungeon domains (from the manifest; a new one ships ~1-2x/year — add it here).
RAIDS: tuple[str, ...] = (
    "Crota's End",
    "Crown of Sorrow",
    "Deep Stone Crypt",
    "Garden of Salvation",
    "King's Fall",
    "Last Wish",
    "Leviathan",
    "Leviathan, Eater of Worlds",
    "Leviathan, Spire of Stars",
    "Root of Nightmares",
    "Salvation's Edge",
    "Scourge of the Past",
    "The Desert Perpetual",
    "Vault of Glass",
    "Vow of the Disciple",
)  # 15 < 25
DUNGEONS: tuple[str, ...] = (
    "Duality",
    "Equilibrium",
    "Ghosts of the Deep",
    "Grasp of Avarice",
    "Pit of Heresy",
    "Prophecy",
    "Spire of the Watcher",
    "Sundered Doctrine",
    "The Shattered Throne",
    "Vesper's Host",
    "Warlord's Ruin",
)  # 11 < 25


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
#
# ``WeaponRef`` (the weapon slot shared with the Trials producer) lives in
# ``hybrid_post_core`` and is imported at the top of this module.


@dataclasses.dataclass
class WeeklyResetContext:
    """Every fillable slot in the Weekly Reset Overview post.

    Round-trips through the ``weekly_reset_draft`` RotationData row so an edit session
    survives bot restarts and can be resumed by any owner.
    """

    reset_ts: int
    # GRANDMASTER + FIRETEAM & ARENA OPS (QUICKPLAY). The GM strike/challenge-reward are
    # Portal-derived; the Bonus Focus weapons and the Quickplay/Control challenge
    # rewards are set by hand (the API exposes only the daily reward or the full weekly
    # pool, not this week's featured one). NOTE: for backwards compatibility
    # the legacy attribute names are kept — ``gm_weapon``/``quickplay_weapon`` are now
    # the GM/Quickplay *Challenge Reward* slots, and ``control_weapon`` is the Crucible
    # *Control* challenge reward.
    gm_strike: str = ""
    gm_weapon: WeaponRef | None = None  # GM Challenge Reward
    gm_bonus_focus: WeaponRef | None = None
    quickplay_weapon: WeaponRef | None = None  # Quickplay Challenge Reward
    quickplay_bonus_focus: WeaponRef | None = None
    control_weapon: WeaponRef | None = None  # Crucible Control Challenge Reward
    # ZAVALA'S WEAPON — set by hand (the vendor API doesn't expose the weekly weapon).
    zavala_weapon: WeaponRef | None = None
    # FEATURED RAIDS & DUNGEONS (weekly rotators)
    rotator_raids: tuple[str, str] = ("", "")
    rotator_dungeons: tuple[str, str] = ("", "")
    # FEATURED PANTHEON
    pantheon_reprise: str = ""
    pantheon_encore: str = ""
    # CRUCIBLE OPS
    crucible_1v6: str = DEFAULT_CRUCIBLE_1V6
    crucible_3v3: str = ""
    crucible_6v6: str = ""
    # CONQUESTS (Seasonal Tab) — hand-curated per tier (not API-derivable). Keys are
    # CONQUEST_TIERS; values are activity-name lists.
    conquests: dict[str, list[str]] = dataclasses.field(default_factory=dict)
    # UPDATES & EVENTS / Trials
    iron_banner: bool = False
    trials_active: bool = True
    #: Optional Bungie patch-notes link, ``{"label": ..., "url": ...}``.
    update_link: dict[str, str] | None = None
    # Editorial
    image_url: str | None = None
    events_narrative: str = ""
    notes: list[str] = dataclasses.field(default_factory=list)
    extra_links: list[dict[str, str]] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict[str, t.Any]:
        return {
            "reset_ts": self.reset_ts,
            "gm_strike": self.gm_strike,
            "gm_weapon": self.gm_weapon.to_dict() if self.gm_weapon else None,
            "gm_bonus_focus": (
                self.gm_bonus_focus.to_dict() if self.gm_bonus_focus else None
            ),
            "quickplay_weapon": (
                self.quickplay_weapon.to_dict() if self.quickplay_weapon else None
            ),
            "quickplay_bonus_focus": (
                self.quickplay_bonus_focus.to_dict()
                if self.quickplay_bonus_focus
                else None
            ),
            "control_weapon": (
                self.control_weapon.to_dict() if self.control_weapon else None
            ),
            "zavala_weapon": self.zavala_weapon.to_dict()
            if self.zavala_weapon
            else None,
            "rotator_raids": list(self.rotator_raids),
            "rotator_dungeons": list(self.rotator_dungeons),
            "pantheon_reprise": self.pantheon_reprise,
            "pantheon_encore": self.pantheon_encore,
            "crucible_1v6": self.crucible_1v6,
            "crucible_3v3": self.crucible_3v3,
            "crucible_6v6": self.crucible_6v6,
            "conquests": {k: list(v) for k, v in self.conquests.items()},
            "iron_banner": self.iron_banner,
            "trials_active": self.trials_active,
            "update_link": dict(self.update_link) if self.update_link else None,
            "image_url": self.image_url,
            "events_narrative": self.events_narrative,
            "notes": list(self.notes),
            "extra_links": [dict(link) for link in self.extra_links],
        }

    @classmethod
    def from_dict(cls, d: t.Mapping[str, t.Any]) -> "WeeklyResetContext":
        def weapon(key: str) -> WeaponRef | None:
            raw = d.get(key)
            return WeaponRef.from_dict(raw) if raw else None

        def pair(key: str) -> tuple[str, str]:
            raw = list(d.get(key) or ["", ""])
            raw = (raw + ["", ""])[:2]
            return (raw[0], raw[1])

        return cls(
            reset_ts=int(d["reset_ts"]),
            gm_strike=d.get("gm_strike", ""),
            gm_weapon=weapon("gm_weapon"),
            gm_bonus_focus=weapon("gm_bonus_focus"),
            quickplay_weapon=weapon("quickplay_weapon"),
            quickplay_bonus_focus=weapon("quickplay_bonus_focus"),
            control_weapon=weapon("control_weapon"),
            zavala_weapon=weapon("zavala_weapon"),
            rotator_raids=pair("rotator_raids"),
            rotator_dungeons=pair("rotator_dungeons"),
            pantheon_reprise=d.get("pantheon_reprise", ""),
            pantheon_encore=d.get("pantheon_encore", ""),
            crucible_1v6=d.get("crucible_1v6", DEFAULT_CRUCIBLE_1V6),
            crucible_3v3=d.get("crucible_3v3", ""),
            crucible_6v6=d.get("crucible_6v6", ""),
            conquests={
                str(k): [str(x) for x in (v or [])]
                for k, v in (d.get("conquests") or {}).items()
            },
            iron_banner=bool(d.get("iron_banner", False)),
            trials_active=bool(d.get("trials_active", True)),
            update_link=dict(d["update_link"]) if d.get("update_link") else None,
            image_url=d.get("image_url"),
            events_narrative=d.get("events_narrative", ""),
            notes=list(d.get("notes") or []),
            extra_links=[dict(link) for link in d.get("extra_links") or []],
        )


@dataclasses.dataclass
class WeeklyResetConfig:
    """Carried-over curated data the Bungie API cannot supply.

    Structural constants (rotator order/anchor, Pantheon pool, IB schedule) plus the
    last values the team entered, so next week's draft starts pre-filled, not blank.
    """

    rotator_anchor: int = DEFAULT_ROTATOR_ANCHOR
    raid_pairs: tuple[tuple[str, str], ...] = DEFAULT_RAID_PAIRS
    dungeon_pairs: tuple[tuple[str, str], ...] = DEFAULT_DUNGEON_PAIRS
    pantheon_pool: tuple[str, ...] = PANTHEON_BOSSES
    ib_week_resets: tuple[int, ...] = DEFAULT_IB_WEEK_RESETS
    crucible_1v6: str = DEFAULT_CRUCIBLE_1V6
    crucible_3v3: str = ""
    crucible_6v6: str = ""
    default_image_url: str | None = None
    event_image_map: dict[str, str] = dataclasses.field(default_factory=dict)
    # Last-entered editorial values, for pre-fill continuity.
    last_pantheon_reprise: str = ""
    last_pantheon_encore: str = ""

    def to_dict(self) -> dict[str, t.Any]:
        return {
            "rotator_anchor": self.rotator_anchor,
            "raid_pairs": [list(p) for p in self.raid_pairs],
            "dungeon_pairs": [list(p) for p in self.dungeon_pairs],
            "pantheon_pool": list(self.pantheon_pool),
            "ib_week_resets": list(self.ib_week_resets),
            "crucible_1v6": self.crucible_1v6,
            "crucible_3v3": self.crucible_3v3,
            "crucible_6v6": self.crucible_6v6,
            "default_image_url": self.default_image_url,
            "event_image_map": dict(self.event_image_map),
            "last_pantheon_reprise": self.last_pantheon_reprise,
            "last_pantheon_encore": self.last_pantheon_encore,
        }

    @classmethod
    def from_dict(cls, d: t.Mapping[str, t.Any] | None) -> "WeeklyResetConfig":
        if not d:
            return cls()

        def pairs(key: str, fallback: tuple[tuple[str, str], ...]):
            raw = d.get(key)
            if not raw:
                return fallback
            return tuple((str(p[0]), str(p[1])) for p in raw)

        return cls(
            rotator_anchor=int(d.get("rotator_anchor", DEFAULT_ROTATOR_ANCHOR)),
            raid_pairs=pairs("raid_pairs", DEFAULT_RAID_PAIRS),
            dungeon_pairs=pairs("dungeon_pairs", DEFAULT_DUNGEON_PAIRS),
            pantheon_pool=tuple(d.get("pantheon_pool") or PANTHEON_BOSSES),
            ib_week_resets=tuple(int(x) for x in d.get("ib_week_resets") or ()),
            crucible_1v6=d.get("crucible_1v6", DEFAULT_CRUCIBLE_1V6),
            crucible_3v3=d.get("crucible_3v3", ""),
            crucible_6v6=d.get("crucible_6v6", ""),
            default_image_url=d.get("default_image_url"),
            event_image_map=dict(d.get("event_image_map") or {}),
            last_pantheon_reprise=d.get("last_pantheon_reprise", ""),
            last_pantheon_encore=d.get("last_pantheon_encore", ""),
        )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


async def load_config() -> WeeklyResetConfig:
    return WeeklyResetConfig.from_dict(await schemas.RotationData.get_data(CONFIG_SLUG))


async def save_config(config: WeeklyResetConfig) -> None:
    await schemas.RotationData.set_data(CONFIG_SLUG, config.to_dict())


async def load_draft() -> WeeklyResetContext | None:
    data = await schemas.RotationData.get_data(DRAFT_SLUG)
    return WeeklyResetContext.from_dict(data) if data else None


async def save_draft(ctx: WeeklyResetContext) -> None:
    await schemas.RotationData.set_data(DRAFT_SLUG, ctx.to_dict())


# ---------------------------------------------------------------------------
# Reset-time + rotator computation (deterministic, no API)
# ---------------------------------------------------------------------------
#
# ``current_reset_ts`` / ``next_reset_ts`` / ``rotator_index`` / ``compute_rotator``
# (plus ``REFERENCE_RESET`` / ``WEEK``) are generic and live in ``hybrid_post_core``;
# ``current_reset_ts``, ``next_reset_ts`` and ``compute_rotator`` are imported above.


# ---------------------------------------------------------------------------
# Bungie derivations (all best-effort — any failure leaves the slot for the team)
# ---------------------------------------------------------------------------


class PortalDerivation(t.NamedTuple):
    gm_strike: str
    gm_weapon: WeaponRef | None


# Portal (component-204) derivation signature. Only the GM Nightfall is derived from the
# Portal: it's the one weekly-stable weapon the API exposes (the featured Nightfall is
# the same all week, so its guaranteed reward is too). The Quickplay/Control featured
# weapons are set by hand — the API only surfaces the daily reward or the full weekly
# pool, never the single featured weekly weapon (set via `/weekly_reset set_reward`).
_STRIKE_ACTIVITY_TYPE_HASH = 556925641  # DestinyActivityTypeDefinition "Strike"


async def derive_portal_fields() -> PortalDerivation:
    """GM Nightfall strike + reward weapon from the authed Portal (component 204).

    The GM Nightfall is the only Strike-type featured op carrying the weekly Nightfall
    *challenge* (ordinary playlist strikes have none), and its guaranteed reward is the
    weekly GM weapon. Both stay correctable via `/weekly_reset set_reward`; anything
    Bungie doesn't surface is left blank/None.
    """
    try:
        ops = await portal_ops.fetch_portal_ops()
    except Exception:
        logger.warning("weekly_reset: fetch_portal_ops failed", exc_info=True)
        return PortalDerivation("", None)

    for op in ops:
        is_gm = op.activity_type_hash == _STRIKE_ACTIVITY_TYPE_HASH
        if is_gm and op.challenge_count > 0:
            gm_weapon = (
                WeaponRef(name=op.reward_name, hash=op.reward_hash)
                if op.reward_hash
                else None
            )
            return PortalDerivation(op.activity_name, gm_weapon)
    return PortalDerivation("", None)


async def build_draft_context(
    config: WeeklyResetConfig | None = None,
) -> WeeklyResetContext:
    """Assemble a fresh draft: compute + best-effort API + carried-over config."""
    config = config or await load_config()
    reset_ts = current_reset_ts()
    # Weekly rotations (raids/dungeons, IB schedule) MUST be keyed by the boundary shown
    # on the "Resets:" line — the *next* Tuesday, when this week's content expires — as
    # that is the convention the anchor/sample data are calibrated to (see
    # DEFAULT_ROTATOR_ANCHOR). Keying by ``reset_ts`` (the week's *start*) instead
    # retrieves the *previous* week's rotation (off-by-one).
    rotation_ts = next_reset_ts(reset_ts)

    ctx = WeeklyResetContext(reset_ts=reset_ts)
    # Carried-over / deterministic fields.
    ctx.rotator_raids = compute_rotator(
        config.raid_pairs, config.rotator_anchor, rotation_ts
    )
    ctx.rotator_dungeons = compute_rotator(
        config.dungeon_pairs, config.rotator_anchor, rotation_ts
    )
    ctx.pantheon_reprise = config.last_pantheon_reprise
    ctx.pantheon_encore = config.last_pantheon_encore
    ctx.crucible_1v6 = config.crucible_1v6
    ctx.crucible_3v3 = config.crucible_3v3
    ctx.crucible_6v6 = config.crucible_6v6
    ctx.iron_banner = rotation_ts in config.ib_week_resets
    ctx.trials_active = not ctx.iron_banner
    ctx.image_url = config.default_image_url

    # Best-effort Portal (component-204) derivation (never fatal — team fills gaps).
    # Only the weekly GM strike + weapon come from the Portal. The Quickplay/Control and
    # Zavala weapons are manual (`/weekly_reset set_reward`): the API exposes only the
    # daily reward or the full weekly pool, not the single featured weekly weapon.
    derived = await derive_portal_fields()
    ctx.gm_strike = derived.gm_strike
    ctx.gm_weapon = derived.gm_weapon

    return ctx


# ---------------------------------------------------------------------------
# Components V2 renderer
# ---------------------------------------------------------------------------


def _weapon_emoji(weapon: WeaponRef) -> str:
    """The emoji token for a weapon line (its type emoji, or a generic fallback)."""
    return weapon.emoji_name or "weapon"


def _crucible_playlist(first: str, stored: str) -> str:
    """A Crucible playlist line's text: the fixed first mode, plus the featured second
    mode (the part after the first ``", "``) when one is set.

    ``first`` is always shown — the base playlist (Competitive/Control, or Iron Banner
    for the 6v6 slot on IB weeks) is a permanent Crucible offering, so the line never
    hides just because no featured mode was chosen. Idempotent over the stored ``"First,
    Second"`` value and back-compatible with a bare or empty stored value.
    """
    _, _, featured = stored.partition(", ")
    return f"{first}, {featured}" if featured else first


def build_body(ctx: WeeklyResetContext) -> str:
    """The full post markdown, with ``:emoji:`` tokens still un-substituted."""
    lines: list[str] = [
        "# Weekly Reset Overview",
        "",
        f"Resets: <t:{next_reset_ts(ctx.reset_ts)}:f>",
    ]

    # THIS WEEK — the Bungie patch link, the Trials-returns reminder, and any editorial
    # events. Trials is mutually exclusive with Iron Banner weeks.
    trials_line = ctx.trials_active and not ctx.iron_banner
    if ctx.update_link or ctx.iron_banner or ctx.events_narrative or trials_line:
        lines += ["### THIS WEEK", ""]
        if ctx.update_link:
            label = ctx.update_link.get("label") or "Update"
            url = ctx.update_link.get("url") or ""
            if url:
                lines.append(f":Bungie: {SEP} [{label}]({url})")
        if trials_line:
            lines.append(f":trials: {SEP} Trials returns on Friday at reset")
        if ctx.iron_banner:
            lines.append(f":IronBanner: {SEP} Iron Banner has returned!")
            lines += ["", TRIALS_IB_REMINDER]
        if ctx.events_narrative:
            lines += ["", ctx.events_narrative]

    # GRANDMASTER — the weekly GM Nightfall: a bold title line (strike name) followed by
    # its Bonus Focus and Challenge Reward weapons. The strike + challenge reward are
    # Portal-derived; the bonus-focus weapon is set by hand.
    if ctx.gm_strike or ctx.gm_weapon or ctx.gm_bonus_focus:
        lines += ["### GRANDMASTER", ""]
        if ctx.gm_strike:
            lines.append(f"**:gm_nightfall: {SEP} {ctx.gm_strike}**")
        if ctx.gm_bonus_focus:
            lines.append(
                f":{_weapon_emoji(ctx.gm_bonus_focus)}: {SEP} "
                f"Bonus Focus: {ctx.gm_bonus_focus.markdown()}"
            )
        if ctx.gm_weapon:
            lines.append(
                f":{_weapon_emoji(ctx.gm_weapon)}: {SEP} "
                f"Challenge Reward: {ctx.gm_weapon.markdown()}"
            )

    # FIRETEAM & ARENA OPS (QUICKPLAY) — the Bonus Focus rotates daily (a static link
    # unless a specific weapon is set) plus the weekly Challenge Reward weapon.
    if ctx.quickplay_weapon or ctx.quickplay_bonus_focus:
        lines += ["### FIRETEAM & ARENA OPS (QUICKPLAY)", ""]
        if ctx.quickplay_bonus_focus:
            lines.append(
                f":{_weapon_emoji(ctx.quickplay_bonus_focus)}: {SEP} "
                f"Bonus Focus: {ctx.quickplay_bonus_focus.markdown()}"
            )
        else:
            lines.append(
                f":{BONUS_DROP_EMOJI}: {SEP} Bonus Focus: {QUICKPLAY_BONUS_DEFAULT}"
            )
        if ctx.quickplay_weapon:
            lines.append(
                f":{_weapon_emoji(ctx.quickplay_weapon)}: {SEP} "
                f"Challenge Reward: {ctx.quickplay_weapon.markdown()}"
            )

    # CONQUESTS — one line per non-empty tier, in CONQUEST_TIERS order. Hand-curated;
    # the API can't supply the weekly tier->activity map (see the plan).
    if any(ctx.conquests.get(tier) for tier in CONQUEST_TIERS):
        lines += ["### CONQUESTS", ""]
        for tier in CONQUEST_TIERS:
            activities = [a for a in ctx.conquests.get(tier, []) if a]
            if activities:
                lines.append(f":Conquests: {SEP} {tier}: {', '.join(activities)}")

    # FEATURED RAIDS & DUNGEONS
    if any(ctx.rotator_raids) or any(ctx.rotator_dungeons):
        lines += ["### FEATURED RAIDS & DUNGEONS", ""]
        if any(ctx.rotator_raids):
            lines.append(
                f":raid: {SEP} {' + '.join(x for x in ctx.rotator_raids if x)}"
            )
        if any(ctx.rotator_dungeons):
            lines.append(
                f":dungeon: {SEP} {' + '.join(x for x in ctx.rotator_dungeons if x)}"
            )

    # Ad-hoc info notes (e.g. "Duality is available due to a bug").
    for note in ctx.notes:
        if note:
            lines += ["", f":info: {note}"]

    # FEATURED PANTHEON
    if ctx.pantheon_reprise or ctx.pantheon_encore:
        lines += ["### FEATURED PANTHEON", ""]
        if ctx.pantheon_reprise:
            lines.append(f":Pantheon: {SEP} Reprise: {ctx.pantheon_reprise}")
        if ctx.pantheon_encore:
            lines.append(f":Pantheon: {SEP} Encore: {ctx.pantheon_encore}")

    # ZAVALA'S WEAPON
    if ctx.zavala_weapon:
        emoji = ctx.zavala_weapon.emoji_name or "weapon"
        lines += [
            "### ZAVALA'S WEAPON",
            "",
            f":{emoji}: {SEP} {ctx.zavala_weapon.markdown()} {ZAVALA_TIER_SUFFIX}",
        ]

    # CRUCIBLE OPS — the base playlists always show (their fixed first mode is a
    # permanent Crucible offering), with the featured second mode appended when set.
    # 1v6 is optional free text. On Iron Banner weeks the 6v6 Control playlist becomes
    # Iron Banner and the (no-longer-featured) Control Challenge Reward is dropped.
    six_first = CRUCIBLE_6V6_FIRST_IB if ctx.iron_banner else CRUCIBLE_6V6_FIRST
    lines += ["### CRUCIBLE OPS", "", "**Playlists**"]
    if ctx.crucible_1v6:
        lines.append(f":crucible: {SEP} 1v6: {ctx.crucible_1v6}")
    lines.append(
        f":crucible: {SEP} 3v3: "
        f"{_crucible_playlist(CRUCIBLE_3V3_FIRST, ctx.crucible_3v3)}"
    )
    lines.append(
        f":crucible: {SEP} 6v6: {_crucible_playlist(six_first, ctx.crucible_6v6)}"
    )
    if ctx.control_weapon and not ctx.iron_banner:
        lines += ["", "**Control**"]
        lines.append(
            f":{_weapon_emoji(ctx.control_weapon)}: {SEP} "
            f"Challenge Reward: {ctx.control_weapon.markdown()}"
        )

    # MORE
    legacy = f"[**View Legacy Activities**]({LEGACY_ACTIVITIES_URL}) ↗"
    lines += ["### MORE", "", legacy]
    for link in ctx.extra_links:
        label, url = link.get("label"), link.get("url")
        if label and url:
            lines.append(f"[**{label}**]({url}) ↗")

    lines += ["", SIGN_OFF]
    return "\n".join(lines)


async def format_weekly_reset(ctx: WeeklyResetContext, bot: CachedFetchBot) -> HMessage:
    """Render the context to a Components V2 :class:`HMessage`."""
    # No dedicated weekly-reset page — just the shared Support button.
    hmsg = build_cv2(build_body(ctx), ctx.image_url, buttons=footer_button_specs())
    # Resolve :emoji: then cap CV2 text (naive front-to-back truncate + CRITICAL alert).
    return await finalize_cv2_post(
        hmsg, await fetch_emoji_dict(bot), post_name="Weekly Reset"
    )


async def weekly_reset_message_constructor(bot: CachedFetchBot) -> HMessage:
    """Announcer hook: render the current draft (build a fresh one if none is saved)."""
    ctx = await load_draft()
    if ctx is None:
        ctx = await build_draft_context()
    return await format_weekly_reset(ctx, bot)


def validate_post(ctx: WeeklyResetContext) -> list[str]:
    """Problems that would make the post empty or break Components V2 limits."""
    problems: list[str] = []
    body = build_body(ctx)
    if len(body) > 3900:
        problems.append(
            f"Post is too long ({len(body)}/3900 chars) — trim some sections."
        )
    if not (ctx.quickplay_weapon or ctx.gm_strike or ctx.zavala_weapon):
        problems.append(
            "Post looks empty — fill in at least the Vanguard/Zavala section."
        )
    if ctx.image_url and not ctx.image_url.startswith(("http://", "https://")):
        problems.append("Image URL must start with http:// or https://.")
    if not settings.get_followable_channel_sync("weekly_reset"):
        problems.append(
            "No 'weekly_reset' channel configured (Autopost Settings) — nowhere to "
            "publish."
        )
    return problems


# ---------------------------------------------------------------------------
# Rich preview (web form)
# ---------------------------------------------------------------------------
#
# There is no preview renderer here any more. ``POST /weekly_reset/preview`` hands the
# page the post's own CV2 node tree (``hybrid_post_core.post_spec_nodes``) and the
# shared client renderer draws it — the same one the builder canvas and the mirror log
# use. The body writes ``<t:…:f>`` tokens and lets the renderer localise them, which is
# what Discord does with them in the posted message.


# ---------------------------------------------------------------------------
# Draft metadata (post message id, publish status, "needs attention" flags)
# ---------------------------------------------------------------------------
#
# ``DraftMeta`` (the post lifecycle record, incl. ``reset_ts`` + ``is_current``) is
# generic and lives in ``hybrid_post_core``; it is imported above. Only the
# followable-specific slug and its load/save helpers stay here.

META_SLUG = "weekly_reset_meta"


async def load_meta() -> DraftMeta:
    return DraftMeta.from_dict(await schemas.RotationData.get_data(META_SLUG))


async def save_meta(meta: DraftMeta) -> None:
    await schemas.RotationData.set_data(META_SLUG, meta.to_dict())


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


async def _render_for_spec(ctx: WeeklyResetContext, bot: CachedFetchBot) -> HMessage:
    """``HybridPostSpec.render`` hook, indirecting through the module global so a test
    that monkeypatches ``format_weekly_reset`` is honoured by the shared publish
    core."""
    return await format_weekly_reset(ctx, bot)


def _now_reset_ts() -> int:
    """``HybridPostSpec.current_reset_ts`` hook: the current reset-period boundary.

    Indirects through the module global so a test that monkeypatches
    ``weekly_reset.current_reset_ts`` steers the shared route code's notion of "now".
    """
    return current_reset_ts()


# ``_SPEC`` (the HybridPostSpec wiring this producer to the shared core) is constructed
# at the bottom of the module, once every hook it references is defined; the wrappers
# and route handlers below resolve it at call time.


async def post_or_edit_unpublished(
    bot: CachedFetchBot, ctx: WeeklyResetContext, meta: DraftMeta
) -> DraftMeta:
    """Create-or-update the *uncrossposted* in-channel post (delegates to the core)."""
    return await hybrid_post_core.post_or_edit_unpublished(_SPEC, bot, ctx, meta)


async def publish_draft(
    bot: CachedFetchBot, ctx: WeeklyResetContext, meta: DraftMeta
) -> tuple[DraftMeta, str]:
    """Publish (crosspost) the in-channel post (delegates to the core)."""
    return await hybrid_post_core.publish_draft(_SPEC, bot, ctx, meta)


# ---------------------------------------------------------------------------
# Single-writer lock
# ---------------------------------------------------------------------------

#: Serialises read-modify-write of the shared draft doc (single bot process).
_draft_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Manifest-backed option pools + apply mutators
# ---------------------------------------------------------------------------

# Reward slots as (label, attribute): the web form renders one weapon picker per entry
# and ``apply_reward_field`` writes the resolved WeaponRef back.
_REWARD_FIELDS: tuple[tuple[str, str], ...] = (
    ("GM Challenge Reward weapon", "gm_weapon"),
    ("GM Bonus Focus weapon", "gm_bonus_focus"),
    ("Quickplay Challenge Reward weapon", "quickplay_weapon"),
    ("Quickplay Bonus Focus weapon", "quickplay_bonus_focus"),
    ("Crucible Control Challenge Reward weapon", "control_weapon"),
    ("Zavala's Weapon", "zavala_weapon"),
)

# DestinyActivityModeType ids used to classify activities (raid/dungeon vs strike).
_MODE_RAID = 4
_MODE_DUNGEON = 82
# Variant/difficulty suffixes (after a ": ") stripped to reach the base activity name.
_VARIANT_SUFFIXES = frozenset(
    {
        "standard",
        "prestige",
        "normal",
        "master",
        "legend",
        "adept",
        "expert",
        "advanced",
        "hero",
        "grandmaster",
        "beginner",
        "challenge mode",
        "eternity",
        "explorer",
        "explorer (matchmade)",
        "ultimatum",
        "customize",
        "matchmade",
        "private",
        "epic",
        "contest",
    }
)
# Whole names that are only a difficulty tier — never a real activity.
_DIFFICULTY_ONLY = frozenset(
    {
        "adept",
        "advanced",
        "expert",
        "grandmaster",
        "hero",
        "legend",
        "master",
        "normal",
        "beginner",
    }
)
# Prefixes stripped from GM strike names (difficulty/quest wrappers).
_STRIKE_PREFIXES = (
    "Nightfall Grandmaster: ",
    "Grandmaster Nightfall: ",
    "Grandmaster: ",
    "Nightfall: ",
    "Legendary ",
    "Legend ",
    "QUEST: ",
    "Quest: ",
)
# Substrings marking non-strike playlist/event junk dropped from the GM strike pool.
# "battlegrounds"/"strikes" (plural) are playlists; singular "Battleground: X" stays.
_STRIKE_JUNK = (
    "guardian games",
    "contest of elders",
    "fireteam ops",
    "crucible",
    "armsweek",
    "playlist",
    "training",
    "rushdown",
    "blitz",
    "battlegrounds",
    "strikes",
)


# Conquest activities are named "<Tier> Conquest: <Base>: Customize" in the manifest.
# <Base> may contain its own colon (e.g. "Operation: Seraph's Shield"), so capture it
# greedily between the fixed prefix and the ": Customize" suffix (don't split on ":").
_CONQUEST_NAME_RE = re.compile(r"^(\S+) Conquest: (.+): Customize$")
#: Manifest tier word -> the post's CONQUEST_TIERS label ("Grandmaster" -> "GM").
_CONQUEST_MANIFEST_TIER = {
    "Expert": "Expert",
    "Master": "Master",
    "Grandmaster": "GM",
    "Ultimate": "Ultimate",
}


def _parse_conquest_name(raw_name: str) -> tuple[str, str] | None:
    """Parse a manifest Conquest activity name into ``(post_tier, base_name)``.

    ``"Expert Conquest: Sunless Cell: Customize"`` -> ``("Expert", "Sunless Cell")``;
    ``"Grandmaster Conquest: Scarlet Keep: Customize"`` -> ``("GM", "Scarlet Keep")``.
    Returns ``None`` for any non-Conquest name (plain strikes, ``: Customize`` missions,
    etc.), which is how the pool excludes the non-Conquest variants.
    """
    match = _CONQUEST_NAME_RE.match(raw_name.strip())
    if not match:
        return None
    tier = _CONQUEST_MANIFEST_TIER.get(match.group(1))
    return (tier, match.group(2).strip()) if tier else None


@dataclasses.dataclass
class _Indexes:
    """Manifest-derived autocomplete data, built once and cached."""

    #: (name, hash, itemTypeDisplayName, itemType, rarity) per weapon/armour, deduped.
    items: list[tuple[str, int, str, int, str]]
    #: category ("raid"/"dungeon"/"strike"/"pantheon"/"crucible") -> sorted names.
    activities: dict[str, list[str]]
    #: Conquests pool: post tier ("Expert"/"Master"/"GM"/"Ultimate") -> sorted names.
    conquests: dict[str, list[str]]
    #: False when the manifest could not be read, so the activity/conquest pools are
    #: empty rather than genuinely so. Such a build is NOT cached — see get_indexes.
    complete: bool = True


_indexes: _Indexes | None = None
_indexes_lock = asyncio.Lock()


def _classify_activity(defn: dict[str, t.Any], type_name: str = "") -> str | None:
    """Classify a DestinyActivityDefinition, or None if it's none of our categories.

    ``type_name`` is the activity's resolved DestinyActivityTypeDefinition name — the
    authoritative signal. Pantheon is checked first (its encounters carry raid mode and
    would otherwise leak into raids); the GM strike pool is Strikes + Battlegrounds; the
    fireteam-size fallback only fires when there is neither a type nor a mode.
    """
    name = ((defn.get("displayProperties") or {}).get("name") or "").strip()
    low = name.lower()
    # Pantheon reprise/encore encounters — the featured boss names live in these.
    if low.startswith("featured reprise: ") or low.startswith("featured encore: "):
        return "pantheon"
    if "pantheon" in low:
        return None  # wings / customize variants — not a reprise/encore boss

    type_lower = type_name.lower()
    if type_lower == "raid":
        return "raid"
    if type_lower == "dungeon":
        return "dungeon"
    if type_lower == "strike" or "battleground" in low:
        return "strike"

    modes = set(defn.get("activityModeTypes") or [])
    direct = defn.get("directActivityModeType")
    if direct:
        modes.add(direct)
    if _MODE_RAID in modes:
        return "raid"
    if _MODE_DUNGEON in modes:
        return "dungeon"

    if not type_name and not modes:
        max_party = (defn.get("matchmaking") or {}).get("maxParty")
        if max_party == 6:
            return "raid"
        if max_party == 3:
            return "dungeon"
    return None


def _strip_variant(name: str) -> str:
    """Reduce a name to its base by dropping variant suffixes + a trailing '(...)'."""
    while ": " in name:
        base, _, suffix = name.rpartition(": ")
        suffix = suffix.strip().lower()
        if suffix in _VARIANT_SUFFIXES or re.fullmatch(r"level \d+", suffix):
            name = base.strip()
        else:
            break
    return re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()


def _clean_activity_name(name: str, category: str) -> str:
    """Normalise to the base name; "" to drop a variant/tier/junk entry."""
    name = name.strip()
    if not name:
        return ""
    if category == "pantheon":
        for prefix in ("Featured Reprise: ", "Featured Encore: "):
            if name.startswith(prefix):
                return name[len(prefix) :].split(":")[0].strip()
        return ""
    if category == "strike":
        for prefix in _STRIKE_PREFIXES:
            if name.startswith(prefix):
                name = name[len(prefix) :]
                break
    base = _strip_variant(name)
    if not base or base.lower() in _DIFFICULTY_ONLY:
        return ""
    if category == "strike" and any(junk in base.lower() for junk in _STRIKE_JUNK):
        return ""
    return base


async def _scan_activities() -> tuple[set[str], dict[str, set[str]], bool]:
    """The strike + conquest pools, from this extension's own manifest connection."""
    # Only GM strikes need manifest autocomplete now; raids/dungeons/pantheon/crucible
    # are bounded Choice selectors (see the *_CHOICES constants).
    strikes: set[str] = set()
    # Conquests pool, bucketed by post tier: only the manifest "<Tier> Conquest: <Base>:
    # Customize" activities, keyed by tier so the autocomplete matches the picked tier.
    conquest_by_tier: dict[str, set[str]] = {tier: set() for tier in CONQUEST_TIERS}
    try:
        path = await api._get_latest_manifest(schemas.BungieCredentials.api_key)
        async with aiosqlite.connect(path) as con:
            cur = await con.cursor()

            # Activity type names are the authoritative raid/dungeon/nightfall signal.
            await cur.execute("SELECT json FROM DestinyActivityTypeDefinition")
            activity_types: dict[int, str] = {}
            for (row,) in await cur.fetchall():
                defn = json.loads(row)
                activity_types[int(defn["hash"])] = (
                    defn.get("displayProperties") or {}
                ).get("name", "")

            await cur.execute("SELECT json FROM DestinyActivityDefinition")
            for (row,) in await cur.fetchall():
                defn = json.loads(row)
                raw_name = (defn.get("displayProperties") or {}).get("name", "")
                # Conquests: keep only the "<Tier> Conquest: <Base>: Customize" entries,
                # bucketed by tier — independent of the strike cleaning below.
                parsed = _parse_conquest_name(raw_name)
                if parsed:
                    conquest_by_tier[parsed[0]].add(parsed[1])
                type_name = activity_types.get(defn.get("activityTypeHash"), "")
                if _classify_activity(defn, type_name) == "strike":
                    cleaned = _clean_activity_name(raw_name, "strike")
                    if cleaned:
                        strikes.add(cleaned)
    except Exception:
        logger.warning("weekly_reset: manifest index build failed", exc_info=True)
        return strikes, conquest_by_tier, False
    return strikes, conquest_by_tier, True


async def _build_indexes() -> _Indexes:
    # The two halves share nothing but the manifest, so they run together rather than in
    # sequence — the page now waits for this build, so it waits for their max, not their
    # sum. Running them concurrently also means their two manifest resolves coalesce
    # onto one (`manifest._inflight`); in sequence they were two Bungie round-trips,
    # each opening a fresh session, on the cold path this build exists to keep short.
    #
    # The weapon pool is generic and identical across producers, so it comes from the
    # process-wide, cached get_weapon_pool() (shared with trials) rather than being
    # re-scanned here; a manifest failure there yields [] so the strike/conquest index
    # still builds.
    items, (strikes, conquest_by_tier, complete) = await asyncio.gather(
        get_weapon_pool(), _scan_activities()
    )

    result = _Indexes(
        items=items,
        activities={"strike": sorted(strikes)},
        conquests={tier: sorted(names) for tier, names in conquest_by_tier.items()},
        # The item pool comes from get_weapon_pool, which returns [] uncached on its own
        # failure; an empty pool means that failed too, and is equally not cacheable.
        complete=complete and bool(items),
    )
    logger.info(
        "weekly_reset indexes: %d items; strikes=%d; conquests=%d",
        len(result.items),
        len(result.activities["strike"]),
        sum(len(names) for names in result.conquests.values()),
    )
    return result


async def get_indexes() -> _Indexes:
    """Build (once) and cache the manifest-backed autocomplete indexes.

    A build that could not read the manifest is returned but **not cached**, mirroring
    :func:`hybrid_post_core.get_weapon_pool`. Caching one used to be permanent: a single
    transient manifest failure at startup left the form with empty strike/conquest
    pickers for the life of the process, with nothing to indicate why.
    """
    global _indexes
    if _indexes is not None:
        return _indexes
    async with _indexes_lock:
        if _indexes is None:
            built = await _build_indexes()
            if not built.complete:
                return built
            _indexes = built
        return _indexes


async def resolve_reward_value(value: str) -> WeaponRef | None:
    """A hash (picked from autocomplete) -> full WeaponRef; else a plain typed name."""
    return resolve_weapon(value, (await get_indexes()).items)


def apply_gm_strike(ctx: WeeklyResetContext, value: str) -> None:
    ctx.gm_strike = value


def apply_crucible(ctx: WeeklyResetContext, three: str, six: str) -> None:
    """First mode of each slot is fixed; only the featured (second) mode is chosen."""
    if three:
        ctx.crucible_3v3 = f"{CRUCIBLE_3V3_FIRST}, {three}"
    if six:
        ctx.crucible_6v6 = f"{CRUCIBLE_6V6_FIRST}, {six}"


def apply_conquests(ctx: WeeklyResetContext, tier: str, value: str) -> None:
    """Replace one Conquests tier's activity list from a comma-separated string.

    An empty ``value`` clears that tier.
    """
    activities = [part.strip() for part in value.split(",") if part.strip()]
    if activities:
        ctx.conquests[tier] = activities
    else:
        ctx.conquests.pop(tier, None)


def apply_update(ctx: WeeklyResetContext, label: str, url: str) -> None:
    """Set (or clear, when ``url`` is blank) the UPDATES & EVENTS Bungie patch link."""
    label, url = label.strip(), url.strip()
    ctx.update_link = {"label": label or "Update", "url": url} if url else None


def apply_pantheon(ctx: WeeklyResetContext, reprise: str, encore: str) -> None:
    if reprise:
        ctx.pantheon_reprise = reprise
    if encore:
        ctx.pantheon_encore = encore


def apply_raids(ctx: WeeklyResetContext, feat1: str, feat2: str) -> None:
    if feat1:
        ctx.rotator_raids = (feat1, ctx.rotator_raids[1])
    if feat2:
        ctx.rotator_raids = (ctx.rotator_raids[0], feat2)


def apply_dungeons(ctx: WeeklyResetContext, feat1: str, feat2: str) -> None:
    if feat1:
        ctx.rotator_dungeons = (feat1, ctx.rotator_dungeons[1])
    if feat2:
        ctx.rotator_dungeons = (ctx.rotator_dungeons[0], feat2)


def apply_reward_field(
    ctx: WeeklyResetContext, field: str, weapon: WeaponRef | None
) -> None:
    if field in {
        "gm_weapon",
        "gm_bonus_focus",
        "quickplay_weapon",
        "quickplay_bonus_focus",
        "control_weapon",
        "zavala_weapon",
    }:
        setattr(ctx, field, weapon)


def _parse_links(raw: str) -> list[dict[str, str]]:
    """Parse the Notes & Links textarea ('Label | https://url' per line).

    Kept as the web /save helper (Step 5); only http(s) URLs are accepted.
    """
    links: list[dict[str, str]] = []
    for line in raw.splitlines():
        if "|" not in line:
            continue
        label, url = (part.strip() for part in line.split("|", 1))
        if label and url.startswith(("http://", "https://")):
            links.append({"label": label, "url": url})
    return links


async def mutate_draft(
    invoker_id: int,
    fn: t.Callable[[WeeklyResetContext], None],
) -> None:
    """Load-modify-save the persisted draft under the lock; the web /save primitive.

    Nothing calls this after the Discord input UI was removed; the web form's /save
    route (Step 5) reuses it.
    """
    # Auto-fill from the API the first time a field is set, so reset time, seasonal
    # raid/dungeon and the computed rotators are pre-populated instead of blank. Built
    # outside the lock (it does network I/O), then committed only if still absent.
    if await load_draft() is None:
        seeded = await build_draft_context()
        async with _draft_lock:
            if await load_draft() is None:
                await save_draft(seeded)
    async with _draft_lock:
        draft = await load_draft() or WeeklyResetContext(reset_ts=current_reset_ts())
        fn(draft)
        meta = await load_meta()
        meta.status = "draft"
        meta.last_edited_by = invoker_id
        meta.last_edited_ts = int(dt.datetime.now(tz=dt.UTC).timestamp())
        await save_draft(draft)
        await save_meta(meta)


# ---------------------------------------------------------------------------
# Owner-authenticated web form — routes
# ---------------------------------------------------------------------------
#
# The Discord input UI is gone; input now flows through this form. Auth is enforced
# centrally by the Discord-OAuth middleware in ``web_auth.py`` (which also covers the
# cross-origin defence), so this module carries no auth code. All security-relevant
# transforms (weapon resolution, the Iron-Banner⇒Trials-off rule, link validation) still
# run server-side in :func:`_context_from_payload`; the client payload is never trusted.

_FORM_HTML_PATH = (
    Path(__file__).resolve().parent.parent / "web_static" / "weekly_reset_form.html"
)


def _pair(raw: t.Any) -> tuple[str, str]:
    """Coerce an arbitrary client value into a 2-tuple of trimmed strings."""
    values = [str(x).strip() for x in (raw or [])]
    values = (values + ["", ""])[:2]
    return (values[0], values[1])


async def _build_options() -> dict[str, t.Any]:
    """Option pools shipped in the page bootstrap and filtered client-side.

    **Waits for the manifest rather than rendering without it.** A bounded wait was
    tried, and traded the wrong thing away: it turned a slow page into a page with empty
    weapon / GM strike / Conquest pickers and a banner nobody has to read, on the one
    form where those pools *are* the content. The reason the wait was there is now
    handled upstream — the manifest is prewarmed at ``StartedEvent``
    (``bungie_api.prewarm_manifest``) so it is on disk before anyone opens this, and
    concurrent resolves are coalesced onto one, so two pages opened together do not
    queue behind two downloads.

    ``ready`` survives for the case no amount of waiting fixes: the manifest could not
    be read at all, so the pools are genuinely empty and the page says so.
    """
    indexes = await get_indexes()
    return {
        # False when the manifest could not be read at all.
        "ready": indexes.complete,
        "items": [
            {"name": name, "hash": hash_, "type": type_name, "rarity": rarity}
            for (name, hash_, type_name, _item_type, rarity) in indexes.items
        ],
        "conquests": {tier: list(names) for tier, names in indexes.conquests.items()},
        "strikes": list(indexes.activities.get("strike", [])),
        "raids": list(RAIDS),
        "dungeons": list(DUNGEONS),
        "pantheon": list(PANTHEON_BOSSES),
        "crucible_modes": list(CRUCIBLE_MODES),
        "crucible_3v3_first": CRUCIBLE_3V3_FIRST,
        "crucible_6v6_first": CRUCIBLE_6V6_FIRST,
    }


async def _context_from_payload(payload: t.Mapping[str, t.Any]) -> WeeklyResetContext:
    """Build a :class:`WeeklyResetContext` from the form JSON, entirely server-side.

    The client is never trusted for security-relevant transforms: each weapon slot is
    resolved from its submitted value (a manifest hash or a typed name) via
    :func:`resolve_reward_value`; the Iron-Banner⇒Trials-off rule is enforced; the
    featured Crucible modes are prefixed with their fixed first mode; notes are split
    per-line and links validated to http(s) via :func:`_parse_links`.
    """
    ctx = WeeklyResetContext(
        reset_ts=int(payload.get("reset_ts") or current_reset_ts())
    )

    ctx.gm_strike = str(payload.get("gm_strike", "")).strip()
    ctx.gm_weapon = await resolve_reward_value(str(payload.get("gm_weapon", "")))
    ctx.gm_bonus_focus = await resolve_reward_value(
        str(payload.get("gm_bonus_focus", ""))
    )
    ctx.quickplay_weapon = await resolve_reward_value(
        str(payload.get("quickplay_weapon", ""))
    )
    ctx.quickplay_bonus_focus = await resolve_reward_value(
        str(payload.get("quickplay_bonus_focus", ""))
    )
    ctx.control_weapon = await resolve_reward_value(
        str(payload.get("control_weapon", ""))
    )
    ctx.zavala_weapon = await resolve_reward_value(
        str(payload.get("zavala_weapon", ""))
    )

    ctx.rotator_raids = _pair(payload.get("rotator_raids"))
    ctx.rotator_dungeons = _pair(payload.get("rotator_dungeons"))
    ctx.pantheon_reprise = str(payload.get("pantheon_reprise", "")).strip()
    ctx.pantheon_encore = str(payload.get("pantheon_encore", "")).strip()

    # Crucible: the first mode of each slot is fixed; only the featured (second) mode is
    # a weekly input, prefixed with its fixed first mode (mirrors apply_crucible).
    ctx.crucible_1v6 = str(payload.get("crucible_1v6", "")).strip()
    three = str(payload.get("crucible_3v3", "")).strip()
    six = str(payload.get("crucible_6v6", "")).strip()
    ctx.crucible_3v3 = f"{CRUCIBLE_3V3_FIRST}, {three}" if three else ""
    ctx.crucible_6v6 = f"{CRUCIBLE_6V6_FIRST}, {six}" if six else ""

    raw_conquests = payload.get("conquests") or {}
    ctx.conquests = {
        tier: activities
        for tier in CONQUEST_TIERS
        if (
            activities := [
                str(a).strip() for a in raw_conquests.get(tier, []) if str(a).strip()
            ]
        )
    }

    # Iron Banner and Trials are mutually exclusive — IB forces Trials off, server-side.
    ctx.iron_banner = bool(payload.get("iron_banner", False))
    ctx.trials_active = (
        False if ctx.iron_banner else bool(payload.get("trials_active", True))
    )

    update_url = str(payload.get("update_url", "")).strip()
    update_label = str(payload.get("update_label", "")).strip()
    ctx.update_link = (
        {"label": update_label or "Update", "url": update_url} if update_url else None
    )

    ctx.image_url = str(payload.get("image_url", "")).strip() or None
    ctx.events_narrative = str(payload.get("events_narrative", "")).strip()
    ctx.notes = [
        line.strip()
        for line in str(payload.get("notes_text", "")).splitlines()
        if line.strip()
    ]
    ctx.extra_links = _parse_links(str(payload.get("links_text", "")))
    return ctx


async def _build_bootstrap(
    draft: WeeklyResetContext, meta: DraftMeta
) -> dict[str, t.Any]:
    """The page bootstrap JSON: the draft, option pools, toggles and lifecycle flags."""
    config = await load_config()
    return {
        "draft": draft.to_dict(),
        "options": await _build_options(),
        "conquest_tiers": list(CONQUEST_TIERS),
        "reward_fields": [list(field) for field in _REWARD_FIELDS],
        # The saved default image (if any), so the form can pre-check "use as default"
        # when this week's image already is the default.
        "default_image_url": config.default_image_url or "",
        # The CV2 container's accent colour, mirrored as the preview's left bar.
        "accent_color": str(await settings.get_embed_default_color()),
        # Whether a post already exists *for the current reset week* (drives which
        # action buttons show: Create-* when there's none, Edit/Delete when there is),
        # and whether that post has been crossposted (hides the Edit-and-publish button
        # once published, and drives the stronger delete-confirm wording).
        "post_this_period": meta.is_current(current_reset_ts()),
        "crossposted": meta.crossposted,
    }


async def _persist_default_image(
    payload: t.Mapping[str, t.Any], ctx: WeeklyResetContext
) -> None:
    """Persist this week's image as the carried-over default when the box is ticked.

    ``build_draft_context`` seeds next week's ``ctx.image_url`` from it; an empty image
    URL with the box ticked clears the default. A no-op when the box is unticked.
    """
    if payload.get("set_default_image"):
        config = await load_config()
        config.default_image_url = ctx.image_url
        await save_config(config)


# The routes are auth-free thin wrappers over the shared hybrid_post_core handlers,
# passing this producer's ``_SPEC`` and the live bot from ``web.get_bot()`` (read at
# call time, so it tracks the stash). ``get_bot()`` rather than ``require_bot()``
# because hybrid_post_core already answers a ``None`` bot with the shared 503 — every
# producer's routes give the same answer without each one raising it. Auth is enforced
# by the web_auth middleware.
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
    followable_key="weekly_reset",
    post_noun="weekly-reset post",
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
    persist_default_image=_persist_default_image,
    form_html_path=_FORM_HTML_PATH,
    draft_lock=_draft_lock,
)


def register_weekly_reset_routes(app: aiohttp.web.Application) -> None:
    """Add the weekly-reset web form routes to the shared persistent app."""
    app.router.add_get("/weekly_reset", _handle_form_get)
    app.router.add_post("/weekly_reset/create", _handle_create)
    app.router.add_post("/weekly_reset/edit", _handle_edit)
    app.router.add_post("/weekly_reset/preview", _handle_preview)
    app.router.add_post("/weekly_reset/delete", _handle_delete)


web.register_routes(register_weekly_reset_routes)
web.register_card(
    web.Card(
        "Weekly Reset",
        "Compose & publish the weekly-reset post",
        "/weekly_reset",
    )
)


@loader.listener(h.StartedEvent)
async def _on_started(event: h.StartedEvent) -> None:
    if not await settings.get_followable_channel("weekly_reset"):
        return

    # Prewarm the manifest-backed option-pool indexes so the first form load is fast.
    asyncio.create_task(get_indexes())


# The web form's routes are always registered (above) and the form is reached from the
# control-panel card grid, which replaced the former `/weekly_reset create` command.
# There is no reset-day cron: the post is created and published entirely from the web
# form's Create/Publish buttons.
