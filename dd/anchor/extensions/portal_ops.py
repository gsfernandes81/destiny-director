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

"""Anchor-side Portal Ops daily autopost.

Fetches the Destiny 2 **Portal** featured ("Ops") rotation + each op's guaranteed
reward from ``GetProfile`` component 204 (CharacterActivities), dedups + buckets the
featured activities into Portal tabs, and renders + posts a daily summary. A featured
op is identified by its guaranteed-drop reward marker (``…_guaranteed`` uiStyle), not
Bungie's ``isFocusedActivity`` flag — the flag misses featured ops that carry the drop
without being flagged focused (e.g. the weekly Defiant Battleground, Sparrow Racing
League). The beacon bot mirrors the post and provides the user-facing ``/portal``
navigator (see ``dd/beacon/extensions/portal_ops.py``).

Pinnacle Ops (the weekly featured raid/dungeon/GM) is intentionally omitted for now:
it is NOT in component 204 — Bungie does not expose the weekly featured raid/dungeon
rotator (see the project memory note ``bungie-api-no-featured-raid-dungeon``) — so it
would need a hardcoded fixed-rotation table, deferred until that data is settled.

Name resolution uses live manifest-entity resolution (``/Destiny2/Manifest/{entity}/
{hash}/``) — a handful of hashes per post, far lighter than loading whole manifest
tables, which suits this low-cardinality feature.
"""

import datetime as dt
import logging
import re
import typing as t

import aiocron
import aiohttp
import hikari as h
import lightbulb as lb

from dd.hmessage import HMessage

from ...common import components, schemas, settings
from ...common.bot import CachedFetchBot
from ...common.utils import fetch_emoji_dict
from ..autopost import Feed, register_feed
from . import (
    bungie_api as api,
    xur,
)
from .bungie_api.constants import API_ROOT, DESTINY_ITEM_TYPE_ARMOR

logger = logging.getLogger(__name__)

loader = lb.Loader()

# ── Component 204 + manifest-entity endpoints ──────────────────────────────────
# A profile fetch scoped to CharacterActivities (204) only. The shared
# ``client.fetch_profile`` is pinned to components 100,200, so Portal Ops uses its
# own URL.
API_PROFILE_204 = API_ROOT + "/Destiny2/{membership_type}/Profile/{membership_id}/"
API_ENTITY = API_ROOT + "/Destiny2/Manifest/{entity}/{hash}/"

# The featured guaranteed weapon/armor drop is marked by a reward uiStyle ending in
# ``_guaranteed`` — ``daily_grind_guaranteed`` for daily Ops, plus weekly/seasonal
# variants for longer-rotation ops such as Battlegrounds. The generic ``extra_engram``
# bonus drop ("Ops Bonus Drop") lacks that suffix and is intentionally dropped (see
# plan Decision 4).
GUARANTEED_REWARD_UI_STYLE_SUFFIX = "_guaranteed"

# ``directActivityModeType`` values for the PvP tabs (DestinyActivityModeType enum).
MODE_CRUCIBLE = 5
MODE_IRON_BANNER = 19
MODE_GAMBIT = 63
MODE_TRIALS = 84
# Sparrow Racing League's own mode. Bungie also tags it AllPvP (5) in
# ``activityModeTypes``, so it's grouped under the Crucible tab.
MODE_RACING = 94
_PVP_TAB_BY_MODE = {
    MODE_GAMBIT: "Gambit",
    MODE_TRIALS: "Trials",
    MODE_IRON_BANNER: "Iron Banner",
    MODE_CRUCIBLE: "Crucible",
    MODE_RACING: "Crucible",
}

# PvE Ops tabs are identified by the activity *type* name (activityTypeHash). Verified
# live against the in-game Portal: Pinnacle Ops gathers the exotic/seasonal pinnacle
# formats; Seasonal Arena is an Arena Op; "Vanguard Op" covers both the Fireteam and
# Arena "Vanguard Alert" playlists and is split by fireteam size below.
_PINNACLE_OPS_TYPES = {"exotic mission", "crawl", "onslaught"}
_ARENA_OPS_TYPES = {"seasonal arena"}
# A "Vanguard Op" with a fireteam this large is the 6-player Arena variant, not the
# 3-player Fireteam one.
_ARENA_FIRETEAM_SIZE = 6

# Tab display order for the rendered post: the four PvE Ops tabs first, then PvP.
TAB_ORDER = [
    "Solo Ops",
    "Fireteam Ops",
    "Pinnacle Ops",
    "Arena Ops",
    "Crucible",
    "Gambit",
    "Trials",
    "Iron Banner",
]

# Strips the trailing Portal variant suffix from an activity display name so the
# Matchmade/Customize/Normal/Master/… variants of one base activity collapse to a
# single name for dedup + display.
_VARIANT_SUFFIX_RE = re.compile(
    r":\s*(Matchmade|Customize|Normal|Master|Legend|Expert|Advanced)\s*$"
)


class PortalOp(t.NamedTuple):
    """One deduped featured Portal op + its guaranteed reward."""

    tab: str
    activity_name: str
    activity_type: str
    reward_name: str
    reward_hash: int
    reward_emoji: str
    tier: int | None
    # Discriminators used by consumers (e.g. the weekly-reset derivations) to classify
    # an op without re-resolving its manifest defs. Defaulted so older call sites and
    # tests keep working.
    reward_item_type: int | None = None  # DestinyItemType: 3=weapon, 2=armour
    activity_type_hash: int | None = None
    challenge_count: int = 0
    max_party: int | None = None
    mode_types: tuple[int, ...] = ()  # DestinyActivityModeType list


# ── Pure helpers (unit-tested in tests/test_portal_ops.py) ─────────────────────


def base_activity_name(name: str) -> str:
    """Drop the ``: Matchmade``/``: Customize``/… variant suffix from an activity
    name so its variants collapse to one base name."""
    return _VARIANT_SUFFIX_RE.sub("", name).strip()


def bucket_for(
    activity_type_name: str, mode_type: int | None, max_party: int | None
) -> str:
    """Map an activity to its Portal tab.

    PvP is keyed off ``directActivityModeType`` first (most reliable), then the
    activity-type name. PvE Ops tabs (Solo/Fireteam/Pinnacle/Arena) are identified by
    the activity *type* name — the in-game Portal groups PvE by activity type, not by
    fireteam size — with ``Vanguard Op`` split into Fireteam vs Arena by fireteam size.
    Unknown PvE types fall back to party size (solo when single-player, else Fireteam).
    """
    if mode_type in _PVP_TAB_BY_MODE:
        return _PVP_TAB_BY_MODE[mode_type]

    type_lower = (activity_type_name or "").lower()
    if "trials" in type_lower:
        return "Trials"
    if "gambit" in type_lower:
        return "Gambit"
    if "iron banner" in type_lower:
        return "Iron Banner"
    if "crucible" in type_lower:
        return "Crucible"

    # PvE Ops tabs, by activity type.
    if type_lower == "solo ops":
        return "Solo Ops"
    if type_lower in _PINNACLE_OPS_TYPES:
        return "Pinnacle Ops"
    if type_lower in _ARENA_OPS_TYPES:
        return "Arena Ops"
    if type_lower == "vanguard op":
        # Fireteam & Arena "Vanguard Alert" share this type; the 6-player one is Arena.
        if (max_party or 0) >= _ARENA_FIRETEAM_SIZE:
            return "Arena Ops"
        return "Fireteam Ops"

    # Unknown PvE type: best-effort by fireteam size.
    if max_party == 1:
        return "Solo Ops"
    return "Fireteam Ops"


def dedupe_ops(ops: t.Iterable[PortalOp]) -> list[PortalOp]:
    """Collapse ops that share a guaranteed reward + base activity name.

    The raw focused list carries Matchmade/Customize pairs and Normal/Master
    duplicates of the same base activity (identical reward), plus the same featured
    drop surfaced across multiple characters. Keying on (reward hash, activity name)
    keeps one representative each while preserving genuinely distinct ops (e.g. a
    Solo and a Fireteam "Quickplay" with different rewards).
    """
    seen: dict[tuple[int, str], PortalOp] = {}
    for op in ops:
        seen.setdefault((op.reward_hash, op.activity_name), op)
    return list(seen.values())


def ops_by_tab(ops: t.Iterable[PortalOp]) -> dict[str, list[PortalOp]]:
    """Group ops into ordered tabs (``TAB_ORDER`` first, extras appended), each
    tab's ops sorted by activity name."""
    grouped: dict[str, list[PortalOp]] = {}
    for op in ops:
        grouped.setdefault(op.tab, []).append(op)

    ordered: dict[str, list[PortalOp]] = {}
    for tab in TAB_ORDER + [name for name in grouped if name not in TAB_ORDER]:
        if tab in grouped:
            ordered[tab] = sorted(grouped[tab], key=lambda o: o.activity_name)
    return ordered


# Weapon-type emoji that exist in the Kyber server (matched off itemTypeDisplayName).
_WEAPON_TYPE_EMOJI = frozenset(
    {
        "auto_rifle",
        "hand_cannon",
        "pulse_rifle",
        "scout_rifle",
        "sidearm",
        "submachine_gun",
        "shotgun",
        "sniper_rifle",
        "fusion_rifle",
        "linear_fusion_rifle",
        "grenade_launcher",
        "rocket_launcher",
        "machine_gun",
        "sword",
        "glaive",
        "combat_bow",
        "trace_rifle",
    }
)


def _reward_emoji(reward_def: dict[str, t.Any] | None) -> str:
    """Emoji for a reward item: its specific weapon-type emoji, :armor: for armor,
    else the generic :weapon:.

    Maps the item's ``itemTypeDisplayName`` (e.g. "Hand Cannon" -> :hand_cannon:) to a
    server emoji, falling back to :weapon: for weapon types without one."""
    if reward_def is None:
        return ":weapon:"
    if reward_def.get("itemType") == DESTINY_ITEM_TYPE_ARMOR:
        return ":armor:"
    slug = reward_def.get("itemTypeDisplayName", "").lower().replace(" ", "_")
    if slug in _WEAPON_TYPE_EMOJI:
        return f":{slug}:"
    if "bow" in slug:
        return ":combat_bow:"
    return ":weapon:"


# ── Live data path ─────────────────────────────────────────────────────────────


async def _resolve_entity(
    session: aiohttp.ClientSession,
    entity: str,
    hash_: int,
    cache: dict[tuple[str, int], dict[str, t.Any] | None],
) -> dict[str, t.Any] | None:
    """Resolve one manifest entity by hash (Option B), memoised in ``cache``."""
    key = (entity, int(hash_))
    if key in cache:
        return cache[key]
    url = API_ENTITY.format(entity=entity, hash=int(hash_))
    async with session.get(
        url, headers={"X-API-Key": schemas.BungieCredentials.api_key}
    ) as resp:
        cache[key] = (await resp.json()).get("Response")
    return cache[key]


def _entity_name(defn: dict[str, t.Any] | None) -> str:
    return (defn or {}).get("displayProperties", {}).get("name", "") or "Unknown"


def _is_guaranteed_reward_style(ui_style: str | None) -> bool:
    """Whether a reward ``uiStyle`` marks a featured guaranteed drop (vs the generic
    ``extra_engram`` bonus). Matches the ``…_guaranteed`` family across the daily,
    weekly and seasonal op rotations."""
    return bool(ui_style) and ui_style.endswith(GUARANTEED_REWARD_UI_STYLE_SUFFIX)


def _guaranteed_reward_hash(activities: t.Iterable[dict[str, t.Any]]) -> int | None:
    """First featured guaranteed reward hash across an activity's per-character copies,
    or ``None`` (i.e. the activity is not a featured op).

    ``visibleRewards`` is populated per character — a character that already claimed
    today's drop can surface the activity without the guaranteed-reward marker — so the
    reward is resolved across every copy rather than from a single one."""
    for activity in activities:
        for visible_reward in activity.get("visibleRewards", []):
            for reward_item in visible_reward.get("rewardItems", []):
                if _is_guaranteed_reward_style(reward_item.get("uiStyle")):
                    return reward_item["itemQuantity"]["itemHash"]
    return None


def _collect_activities_by_hash(
    character_activities: dict[str, t.Any],
) -> dict[int, list[dict[str, t.Any]]]:
    """Group every available activity by hash, keeping all per-character copies.

    The featured-op filter is the guaranteed-reward marker (see
    ``_guaranteed_reward_hash``), applied downstream — *not* Bungie's
    ``isFocusedActivity`` flag, which misses featured ops that carry the guaranteed drop
    without being flagged focused (e.g. the weekly Defiant Battleground and Sparrow
    Racing League). Keeping every character's copy lets the reward be resolved across
    copies, since ``visibleRewards`` is populated per character."""
    activities: dict[int, list[dict[str, t.Any]]] = {}
    for character_data in character_activities.values():
        for activity in character_data.get("availableActivities", []):
            activities.setdefault(activity["activityHash"], []).append(activity)
    return activities


def _build_portal_op(
    activity: dict[str, t.Any],
    activity_def: dict[str, t.Any] | None,
    type_def: dict[str, t.Any] | None,
    reward_def: dict[str, t.Any] | None,
    reward_hash: int,
) -> PortalOp:
    """Assemble a ``PortalOp`` from an activity's resolved manifest defs (pure; no I/O).

    Kept out of ``fetch_portal_ops`` so the manifest-key mapping — especially the
    discriminator fields consumers classify ops by — is unit-testable without HTTP.
    """
    matchmaking = (activity_def or {}).get("matchmaking", {})
    mode_type = (activity_def or {}).get("directActivityModeType")
    type_name = _entity_name(type_def)
    return PortalOp(
        tab=bucket_for(type_name, mode_type, matchmaking.get("maxParty")),
        activity_name=base_activity_name(_entity_name(activity_def)),
        activity_type=type_name,
        reward_name=_entity_name(reward_def),
        reward_hash=reward_hash,
        reward_emoji=_reward_emoji(reward_def),
        tier=activity.get("difficultyTier"),
        reward_item_type=(reward_def or {}).get("itemType"),
        activity_type_hash=(activity_def or {}).get("activityTypeHash"),
        challenge_count=len((activity_def or {}).get("challenges") or []),
        max_party=matchmaking.get("maxParty"),
        mode_types=tuple((activity_def or {}).get("activityModeTypes") or ()),
    )


async def fetch_portal_ops() -> list[PortalOp]:
    """Fetch + dedup + bucket the current featured Portal ops with their rewards.

    Refreshes the Bungie token, resolves the primary membership + one profile (all
    characters), reads component 204, then resolves activity/type/reward names live.
    """
    access_token = await api.refresh_api_tokens(api.get_webserver_runner())

    async with aiohttp.ClientSession() as session:
        memberships = await api.client.fetch_memberships(session, access_token)
        membership = api.DestinyMembership.from_api_response(memberships)

        url = API_PROFILE_204.format(
            membership_type=membership.membership_type,
            membership_id=membership.membership_id,
        )
        async with session.get(
            url,
            params={"components": "204"},
            headers={
                "X-API-Key": schemas.BungieCredentials.api_key,
                "Authorization": f"Bearer {access_token}",
            },
        ) as resp:
            profile = (await resp.json())["Response"]

        character_activities = profile.get("characterActivities", {}).get("data", {})
        activities = _collect_activities_by_hash(character_activities)

        cache: dict[tuple[str, int], dict[str, t.Any] | None] = {}
        ops: list[PortalOp] = []
        for activity_hash, activity_copies in activities.items():
            reward_hash = _guaranteed_reward_hash(activity_copies)
            if reward_hash is None:
                # No featured guaranteed drop → not a Portal op.
                continue

            activity = activity_copies[0]
            activity_def = await _resolve_entity(
                session, "DestinyActivityDefinition", activity_hash, cache
            )
            type_hash = (activity_def or {}).get("activityTypeHash")
            type_def = (
                await _resolve_entity(
                    session, "DestinyActivityTypeDefinition", type_hash, cache
                )
                if type_hash
                else None
            )
            reward_def = await _resolve_entity(
                session, "DestinyInventoryItemDefinition", reward_hash, cache
            )

            ops.append(
                _build_portal_op(
                    activity, activity_def, type_def, reward_def, reward_hash
                )
            )

    return dedupe_ops(ops)


# ── Rendering ──────────────────────────────────────────────────────────────────

# Portal Ops has no dedicated page, so its footer button row carries no "guide" button
# (just the shared Support button) — see format below.


async def portal_ops_message_constructor(bot: CachedFetchBot) -> HMessage:
    ops = await fetch_portal_ops()
    emoji_dict = await fetch_emoji_dict(bot)

    description = "# :Daily_Portal_Focus: [Portal Ops](https://kyber3000.com)\n\n"
    description += f":time: Featured ops reset <t:{_next_daily_reset_unix()}:R>\n\n"

    grouped = ops_by_tab(ops)
    if not grouped:
        description += "No featured ops are currently available.\n\n"

    for tab in TAB_ORDER + [name for name in grouped if name not in TAB_ORDER]:
        tab_ops = grouped.get(tab)
        if not tab_ops:
            continue
        description += f"**{tab}**\n"
        for op in tab_ops:
            description += (
                f"• **{op.activity_name}** — {op.reward_emoji} "
                f"[{op.reward_name}](https://light.gg/db/items/{op.reward_hash})\n"
            )
        description += "\n"

    # Components V2: the whole post is one text display (no image/fields), matching
    # the Eververse/Ada layout — raw :emoji: tokens, resolved on the message below.
    container = h.impl.ContainerComponentBuilder(
        accent_color=await settings.get_embed_default_color()
    )
    container.add_text_display(description)
    # No post-specific guide (Portal Ops has no dedicated page) — just the shared
    # Support button.
    container.add_component(components.footer_buttons_row())

    # Resolve :emoji: then cap CV2 text (naive front-to-back truncate + CRITICAL alert
    # on overflow).
    return await components.finalize_cv2_post(
        HMessage(components=[container]), emoji_dict, post_name="Portal Ops"
    )


def _next_daily_reset_unix() -> int:
    """Unix timestamp of the next daily reset (17:00 UTC)."""
    now = dt.datetime.now(tz=dt.UTC)
    reset = now.replace(hour=17, minute=0, second=0, microsecond=0)
    if reset <= now:
        reset += dt.timedelta(days=1)
    return int(reset.timestamp())


# ── Autopost wiring (guarded: dormant until the followable channel is configured) ─

_PORTAL_OPS_CHANNEL = settings.get_followable_channel_sync("portal_ops")


if not _PORTAL_OPS_CHANNEL:
    # The followable channel id is not configured in this environment's FOLLOWABLES
    # (absent, or the 0 placeholder). Load cleanly and stay dormant rather than
    # KeyError-ing at import — the autopost cron is simply not scheduled until a real
    # channel id is set. The feed page still lists it, marked dormant.
    logger.info(
        "Portal Ops autopost is dormant: no 'portal_ops' entry in FOLLOWABLES. "
        "Add the followable channel id to enable it."
    )
else:

    @loader.listener(h.StartedEvent)
    async def on_start_schedule_autoposts(
        event: h.StartedEvent, bot: CachedFetchBot = lb.di.INJECTED
    ):
        # One daily post at daily reset (17:00 UTC) showing the current featured
        # state; weekly-rotating tabs just show whatever is currently featured.
        @aiocron.crontab("0 17 * * *", start=True)
        # Use below crontab for testing to post every minute
        # @aiocron.crontab("* * * * *", start=True)
        async def autopost_portal_ops():
            await xur.api_to_discord_announcer(
                bot,
                channel_id=await settings.get_followable_channel("portal_ops"),
                check_enabled=True,
                enabled_check_coro=schemas.AutoPostSettings.get_portal_ops_enabled,
                construct_message_coro=portal_ops_message_constructor,
                cv2=True,
            )


# Contribute this feed's producer wiring to the web feed page (Preview / Send now).
# Registered unconditionally, outside the dormancy gate above — see the same note in
# iron_banner.py: a dormant feed still gets a page that explains itself, and previews.
register_feed(
    Feed(
        name="portal_ops",
        channel_id=_PORTAL_OPS_CHANNEL,
        message_constructor_coro=portal_ops_message_constructor,
        message_announcer_coro=xur.api_to_discord_announcer,
    )
)
