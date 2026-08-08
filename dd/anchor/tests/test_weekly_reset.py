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

"""Unit tests for the ``weekly_reset`` extension's pure logic (no Discord I/O).

Here we exercise the reset-time maths, the deterministic rotator cycle (anchored to the
three real posts this was built from), the post-body renderer, (de)serialisation,
validation, the manifest option pools + apply mutators + reward resolver, and the
Components V2 builder.
"""

import datetime as dt
import json
import types
import typing as t

import aiohttp.web
import hikari as h
import pytest

from dd.anchor import web
from dd.anchor.extensions import weekly_reset as wr

# The three real "Weekly Reset Overview" posts this feature was reverse-engineered from.
# These are each post's "Resets:"-line value — the *next* Tuesday (when that week's
# content expires), NOT the week's start. That is why the rotator tests feed them
# straight into compute_rotator: production feeds the same boundary via
# next_reset_ts(current_reset_ts()). See DEFAULT_ROTATOR_ANCHOR in weekly_reset.py.
SAMPLE_RESETS = (1782234000, 1782838800, 1783443600)
SAMPLE_RAIDS = (
    ("King's Fall", "Garden of Salvation"),
    ("Root of Nightmares", "Deep Stone Crypt"),
    ("Crota's End", "Vault of Glass"),
)
SAMPLE_DUNGEONS = (
    ("Spire of the Watcher", "Pit of Heresy"),
    ("Ghosts of the Deep", "Prophecy"),
    ("Warlord's Ruin", "Grasp of Avarice"),
)


def _full_ctx() -> wr.WeeklyResetContext:
    ctx = wr.WeeklyResetContext(reset_ts=1783443600)
    ctx.gm_strike = "The Sunless Cell"
    ctx.gm_weapon = wr.WeaponRef("Null Composure", 222)  # GM Challenge Reward
    ctx.gm_bonus_focus = wr.WeaponRef("Ouster Engine", 555, "sword")
    ctx.quickplay_weapon = wr.WeaponRef("Service Revolver", 111)  # QP Challenge Reward
    # quickplay_bonus_focus left unset -> the "Changes Daily" default line.
    ctx.control_weapon = wr.WeaponRef("The Helmsman", 333)  # Control Challenge Reward
    ctx.rotator_raids = ("Crota's End", "Vault of Glass")
    ctx.rotator_dungeons = ("Warlord's Ruin", "Grasp of Avarice")
    ctx.pantheon_reprise = "Argos"
    ctx.pantheon_encore = "Insurrection Prime"
    ctx.zavala_weapon = wr.WeaponRef("Horror's Least", 444, "pulse_rifle")
    ctx.crucible_3v3 = "Competitive, Clash"
    ctx.crucible_6v6 = "Control, Eruption"
    ctx.conquests = {
        "Expert": ["Sunless Cell"],
        "GM": ["Arms Dealer", "Scarlet Keep"],
    }
    ctx.update_link = {"label": "Update 9.7.0.3", "url": "https://example.com/notes"}
    ctx.notes = ["Duality is available due to a bug."]
    return ctx


# --- reset-time maths -------------------------------------------------------------


@pytest.mark.parametrize("reset_ts", SAMPLE_RESETS)
def test_current_reset_ts_matches_samples(reset_ts: int) -> None:
    at_reset = dt.datetime.fromtimestamp(reset_ts, tz=dt.UTC)
    assert wr.current_reset_ts(at_reset) == reset_ts


def test_current_reset_ts_floors_midweek() -> None:
    # Thursday of the first sampled week floors back to that week's Tuesday reset.
    midweek = dt.datetime(2026, 6, 25, 20, 0, tzinfo=dt.UTC)
    assert wr.current_reset_ts(midweek) == 1782234000


@pytest.mark.parametrize("reset_ts", SAMPLE_RESETS)
def test_next_reset_ts_is_the_following_tuesday(reset_ts: int) -> None:
    # The next boundary is exactly one week on, and still lands on a reset instant.
    nxt = wr.next_reset_ts(reset_ts)
    assert nxt == reset_ts + 7 * 86400
    assert wr.current_reset_ts(dt.datetime.fromtimestamp(nxt, tz=dt.UTC)) == nxt


# --- deterministic rotator cycle --------------------------------------------------


@pytest.mark.parametrize(
    ("reset_ts", "raids", "dungeons"),
    list(zip(SAMPLE_RESETS, SAMPLE_RAIDS, SAMPLE_DUNGEONS, strict=True)),
)
def test_rotators_reproduce_sampled_weeks(
    reset_ts: int, raids: tuple[str, str], dungeons: tuple[str, str]
) -> None:
    anchor = wr.DEFAULT_ROTATOR_ANCHOR
    assert wr.compute_rotator(wr.DEFAULT_RAID_PAIRS, anchor, reset_ts) == raids
    assert wr.compute_rotator(wr.DEFAULT_DUNGEON_PAIRS, anchor, reset_ts) == dungeons


def test_rotation_keyed_by_next_reset_boundary() -> None:
    # Live truth for the week starting 2026-06-30 (reset_ts=1782838800): featured
    # raids = Vault of Glass + Crota's End, dungeons = Warlord's Ruin + Grasp of
    # Avarice. The rotation must be keyed by the *next* reset (the "Resets:" line, the
    # rotators' calibration convention) — keying by reset_ts yields last week's set.
    reset_ts = 1782838800
    rot = wr.next_reset_ts(reset_ts)
    assert set(
        wr.compute_rotator(wr.DEFAULT_RAID_PAIRS, wr.DEFAULT_ROTATOR_ANCHOR, rot)
    ) == {"Vault of Glass", "Crota's End"}
    assert set(
        wr.compute_rotator(wr.DEFAULT_DUNGEON_PAIRS, wr.DEFAULT_ROTATOR_ANCHOR, rot)
    ) == {"Warlord's Ruin", "Grasp of Avarice"}
    # And the (buggy) reset_ts keying would have returned a *different* set.
    assert wr.compute_rotator(
        wr.DEFAULT_RAID_PAIRS, wr.DEFAULT_ROTATOR_ANCHOR, rot
    ) != wr.compute_rotator(wr.DEFAULT_RAID_PAIRS, wr.DEFAULT_ROTATOR_ANCHOR, reset_ts)


def test_rotator_cycle_wraps() -> None:
    # One full cycle later lands back on the anchor week's raids.
    one_cycle = 1782234000 + len(wr.DEFAULT_RAID_PAIRS) * 7 * 86400
    assert (
        wr.compute_rotator(wr.DEFAULT_RAID_PAIRS, wr.DEFAULT_ROTATOR_ANCHOR, one_cycle)
        == wr.DEFAULT_RAID_PAIRS[0]
    )


# --- post-body renderer -----------------------------------------------------------


def test_build_body_has_all_sections_and_deeplink() -> None:
    body = wr.build_body(_full_ctx())
    for marker in (
        "# Weekly Reset Overview",
        # Resets line shows the *next* Tuesday (reset_ts + 1 week), not reset_ts.
        "Resets: <t:1784048400:f>",
        "### THIS WEEK",
        "[Update 9.7.0.3](https://example.com/notes)",
        "Trials returns on Friday at reset",  # relocated from the old bottom block
        # GRANDMASTER: bold-titled strike, Bonus Focus + Challenge Reward weapons.
        "### GRANDMASTER",
        "The Sunless Cell**",
        "Bonus Focus: [Ouster Engine]",
        "Challenge Reward: [Null Composure]",
        # FIRETEAM: unset bonus focus falls back to the daily-rotating default link.
        "### FIRETEAM & ARENA OPS (QUICKPLAY)",
        "Bonus Focus: [Changes Daily](https://www.light.gg)",
        "Challenge Reward: [Service Revolver]",
        "### CONQUESTS",
        "Expert: Sunless Cell",
        "GM: Arms Dealer, Scarlet Keep",
        "### FEATURED RAIDS & DUNGEONS",
        "Crota's End + Vault of Glass",
        "### FEATURED PANTHEON",
        "Reprise: Argos",
        "### ZAVALA'S WEAPON",
        "(T5 / rolls vary)",
        # CRUCIBLE OPS: Playlists sub-block + Control Challenge Reward sub-block.
        "### CRUCIBLE OPS",
        "**Playlists**",
        "**Control**",
        "Challenge Reward: [The Helmsman]",
        ":info: Duality is available due to a bug.",
        "### MORE",
        "See you starside! \U0001f4ab",
    ):
        assert marker in body, marker
    # The old embed-era headers/format are gone.
    assert "**VANGUARD ALERTS**" not in body
    assert "**UPDATES & EVENTS**" not in body
    assert "(Seasonal Tab)" not in body
    assert "***See you starside!***" not in body
    assert "┊" not in body
    # light.gg deep link uses the item hash (fixes the old bare placeholder).
    assert "https://light.gg/db/items/444" in body


def test_iron_banner_week_hides_trials_line_shows_reminder() -> None:
    ctx = _full_ctx()
    ctx.iron_banner = True
    ctx.trials_active = False
    body = wr.build_body(ctx)
    assert "Iron Banner has returned" in body
    assert "Trials returns on Friday at reset" not in body  # Trials gated off on IB
    assert wr.TRIALS_IB_REMINDER in body


def test_iron_banner_week_drops_control_and_swaps_6v6_first_mode() -> None:
    # IB replaces the 6v6 Control playlist, so the fixed first mode reads "Iron Banner"
    # (featured second mode kept) and the Control Challenge Reward is dropped entirely.
    ctx = _full_ctx()  # control_weapon set; crucible_6v6 = "Control, Eruption"
    non_ib = wr.build_body(ctx)
    assert "6v6: Control, Eruption" in non_ib
    assert "**Control**" in non_ib and "The Helmsman" in non_ib

    ctx.iron_banner = True
    ctx.trials_active = False
    ib = wr.build_body(ctx)
    assert "6v6: Iron Banner, Eruption" in ib
    assert "6v6: Control" not in ib
    assert "**Control**" not in ib and "The Helmsman" not in ib
    # The Playlists sub-block still renders via the other crucible slots.
    assert "### CRUCIBLE OPS" in ib and "**Playlists**" in ib


def test_crucible_base_playlists_show_without_a_featured_mode() -> None:
    # No featured second mode and no 1v6 line: the base 3v3/6v6 playlists still render
    # (Competitive/Control are permanent offerings, never hidden), no trailing comma.
    ctx = wr.WeeklyResetContext(reset_ts=1)
    ctx.crucible_1v6 = ""
    body = wr.build_body(ctx)
    assert "### CRUCIBLE OPS" in body and "**Playlists**" in body
    assert "3v3: Competitive" in body and "3v3: Competitive," not in body
    assert "6v6: Control" in body and "6v6: Control," not in body
    assert "1v6:" not in body  # optional free text, cleared here

    # Iron Banner still swaps the 6v6 first mode even with no featured second mode.
    ctx.iron_banner = True
    ib = wr.build_body(ctx)
    assert "6v6: Iron Banner" in ib and "6v6: Iron Banner," not in ib


def test_trials_line_shows_on_non_ib_week() -> None:
    ctx = _full_ctx()
    ctx.iron_banner = False
    ctx.trials_active = True
    assert "Trials returns on Friday at reset" in wr.build_body(ctx)


def test_hand_typed_weapon_has_no_link() -> None:
    ctx = wr.WeeklyResetContext(reset_ts=1)
    ctx.zavala_weapon = wr.WeaponRef("Typed Name")  # no hash
    assert ctx.zavala_weapon.markdown() == "Typed Name"
    assert "](http" not in ctx.zavala_weapon.markdown()


# --- (de)serialisation ------------------------------------------------------------


def test_context_round_trip() -> None:
    ctx = _full_ctx()
    restored = wr.WeeklyResetContext.from_dict(ctx.to_dict())
    assert restored.to_dict() == ctx.to_dict()
    assert restored.rotator_raids == ("Crota's End", "Vault of Glass")
    assert restored.zavala_weapon is not None and restored.zavala_weapon.hash == 444
    assert restored.conquests == {
        "Expert": ["Sunless Cell"],
        "GM": ["Arms Dealer", "Scarlet Keep"],
    }
    assert restored.update_link == {
        "label": "Update 9.7.0.3",
        "url": "https://example.com/notes",
    }


def test_parse_conquest_name_selects_and_cleans() -> None:
    # Real manifest names -> (post tier, clean base). Grandmaster maps to the "GM" tier,
    # and a base name containing its own colon is preserved.
    assert wr._parse_conquest_name("Expert Conquest: Sunless Cell: Customize") == (
        "Expert",
        "Sunless Cell",
    )
    assert wr._parse_conquest_name("Grandmaster Conquest: Scarlet Keep: Customize") == (
        "GM",
        "Scarlet Keep",
    )
    assert wr._parse_conquest_name(
        "Ultimate Conquest: Operation: Seraph's Shield: Customize"
    ) == ("Ultimate", "Operation: Seraph's Shield")
    # Non-Conquest variants are excluded (return None) — this is bug (2)'s fix.
    for non_conquest in (
        "The Sunless Cell: Customize",
        "The Sunless Cell",
        "Nightfall: Advanced",
        "Defiant Battleground: EDZ",
    ):
        assert wr._parse_conquest_name(non_conquest) is None, non_conquest


def test_apply_conquests_sets_and_clears() -> None:
    ctx = wr.WeeklyResetContext(reset_ts=1)
    wr.apply_conquests(ctx, "GM", "Arms Dealer, Scarlet Keep , ")
    assert ctx.conquests["GM"] == ["Arms Dealer", "Scarlet Keep"]
    # Renders in CONQUEST_TIERS order under the section header.
    assert "GM: Arms Dealer, Scarlet Keep" in wr.build_body(ctx)
    wr.apply_conquests(ctx, "GM", "   ")  # blank clears the tier
    assert "GM" not in ctx.conquests
    assert "**CONQUESTS (Seasonal Tab)**" not in wr.build_body(ctx)


def test_apply_update_sets_and_clears() -> None:
    ctx = wr.WeeklyResetContext(reset_ts=1)
    wr.apply_update(ctx, "Update 9.7.0.3", "https://example.com/x")
    assert ctx.update_link == {
        "label": "Update 9.7.0.3",
        "url": "https://example.com/x",
    }
    assert "[Update 9.7.0.3](https://example.com/x)" in wr.build_body(ctx)
    wr.apply_update(ctx, "", "")  # blank url clears the link
    assert ctx.update_link is None


def test_config_round_trip_and_defaults() -> None:
    restored = wr.WeeklyResetConfig.from_dict(wr.WeeklyResetConfig().to_dict())
    assert restored.raid_pairs == wr.DEFAULT_RAID_PAIRS
    assert restored.pantheon_pool == wr.PANTHEON_BOSSES
    assert (
        wr.WeeklyResetConfig.from_dict(None).rotator_anchor == wr.DEFAULT_ROTATOR_ANCHOR
    )


# --- validation + reconciliation --------------------------------------------------


def test_validate_flags_empty_post() -> None:
    problems = wr.validate_post(wr.WeeklyResetContext(reset_ts=1))
    assert any("empty" in p for p in problems)


def test_validate_flags_bad_image_url() -> None:
    ctx = _full_ctx()
    ctx.image_url = "not-a-url"
    assert any("Image URL" in p for p in wr.validate_post(ctx))


# --- CV2 builder ------------------------------------------------------------------


def test_build_cv2_is_components_v2() -> None:
    ctx = _full_ctx()
    ctx.image_url = "https://example.com/art.jpg"
    hmessage = wr.build_cv2(wr.build_body(ctx), ctx.image_url)
    kwargs = hmessage.to_message_kwargs()
    assert kwargs["flags"] == h.MessageFlag.IS_COMPONENTS_V2
    assert kwargs["components"] and "content" not in kwargs


# --- manifest option pools + apply mutators + reward resolver ---------------------


@pytest.fixture
def stub_indexes():
    saved = wr._indexes
    wr._indexes = wr._Indexes(
        items=[
            ("Null Composure", 222, "Fusion Rifle", 3, "Legendary"),
            ("Cloudstrike", 333, "Sniper Rifle", 3, "Exotic"),
            ("Chill Inhibitor", 444, "Grenade Launcher", 3, "Exotic"),
        ],
        activities={
            "raid": ["Crota's End", "Vault of Glass"],
            "dungeon": ["Duality"],
            "strike": ["The Sunless Cell"],
            "pantheon": ["Argos", "Calus"],
            "crucible": ["Control"],
        },
        conquests={
            "Expert": ["Sunless Cell"],
            "Master": ["Conductor's Keep"],
            "GM": ["Arms Dealer", "Scarlet Keep"],
            "Ultimate": ["Lightblade"],
        },
    )
    yield
    wr._indexes = saved


def test_option_pool_domains_have_no_duplicates() -> None:
    # These option pools feed the web form's selectors; each must be non-empty, have no
    # blank entries, and carry no duplicates.
    for domain in (wr.RAIDS, wr.DUNGEONS, wr.PANTHEON_BOSSES, wr.CRUCIBLE_MODES):
        assert domain and all(domain)
        assert len(set(domain)) == len(domain), domain
    assert "Heavy Metal Supremacy" in wr.CRUCIBLE_MODES


@pytest.mark.parametrize(
    ("defn", "type_name", "expected"),
    [
        # Pantheon reprise/encore encounters -> pantheon (before the raid-mode check)
        (
            {
                "displayProperties": {"name": "Featured Reprise: Calus: The Pantheon"},
                "activityModeTypes": [4],
            },
            "Raid",
            "pantheon",
        ),
        # other Pantheon-named activities are excluded from every pool
        (
            {
                "displayProperties": {"name": "The Pantheon: Atraks Sovereign"},
                "activityModeTypes": [4],
            },
            "Raid",
            None,
        ),
        # authoritative type name
        ({}, "Raid", "raid"),
        ({}, "Dungeon", "dungeon"),
        ({}, "Strike", "strike"),
        # battlegrounds feed the GM strike pool by name
        (
            {"displayProperties": {"name": "Defiant Battleground: EDZ"}},
            "Nightfall",
            "strike",
        ),
        # mode fallback when no type
        ({"activityModeTypes": [4]}, "", "raid"),
        ({"directActivityModeType": 82}, "", "dungeon"),
        # fireteam-size fallback only when there is no type AND no mode
        ({"matchmaking": {"maxParty": 6}}, "", "raid"),
        ({"matchmaking": {"maxParty": 3}}, "", "dungeon"),
        # a typed 3-player strike is a strike, never a dungeon
        ({"matchmaking": {"maxParty": 3}}, "Strike", "strike"),
        ({"matchmaking": {"maxParty": 4}}, "", None),
        ({}, "", None),
    ],
)
def test_classify_activity(defn: dict, type_name: str, expected: str | None) -> None:
    assert wr._classify_activity(defn, type_name) == expected


def test_strip_variant() -> None:
    assert wr._strip_variant("Ghosts of the Deep: Standard") == "Ghosts of the Deep"
    assert wr._strip_variant("Vault of Glass: Challenge Mode") == "Vault of Glass"
    assert wr._strip_variant("Last Wish: Level 58") == "Last Wish"
    assert (
        wr._strip_variant("The Desert Perpetual (Epic): Standard")
        == "The Desert Perpetual"
    )
    # a meaningful ": X" (battleground location) is preserved
    assert wr._strip_variant("Defiant Battleground: EDZ") == "Defiant Battleground: EDZ"


def test_clean_activity_name() -> None:
    assert wr._clean_activity_name("Crota's End: Standard", "raid") == "Crota's End"
    assert wr._clean_activity_name("Grandmaster", "raid") == ""  # difficulty-only
    # strike playlist/event junk dropped
    assert (
        wr._clean_activity_name("Guardian Games: Competitive Nightfall", "strike") == ""
    )
    assert wr._clean_activity_name("The Sunless Cell", "strike") == "The Sunless Cell"
    # pantheon boss extracted from the Featured Reprise/Encore encounter names
    assert (
        wr._clean_activity_name("Featured Reprise: Calus: The Pantheon", "pantheon")
        == "Calus"
    )
    assert (
        wr._clean_activity_name(
            "Featured Encore: Warpriest: Atraks Sovereign", "pantheon"
        )
        == "Warpriest"
    )
    assert wr._clean_activity_name("The Pantheon: Atraks Sovereign", "pantheon") == ""


def test_apply_crucible_fixes_first_mode() -> None:
    ctx = wr.WeeklyResetContext(reset_ts=1)
    wr.apply_crucible(ctx, "Clash", "Rift")
    assert ctx.crucible_3v3 == "Competitive, Clash"
    assert ctx.crucible_6v6 == "Control, Rift"
    # only-one-provided leaves the other untouched
    wr.apply_crucible(ctx, "Eruption", "")
    assert ctx.crucible_3v3 == "Competitive, Eruption"
    assert ctx.crucible_6v6 == "Control, Rift"


def test_apply_pantheon_raids_dungeons() -> None:
    ctx = wr.WeeklyResetContext(reset_ts=1)
    wr.apply_pantheon(ctx, "Argos", "Calus")
    assert (ctx.pantheon_reprise, ctx.pantheon_encore) == ("Argos", "Calus")
    wr.apply_raids(ctx, "Vault of Glass", "Crota's End")
    assert ctx.rotator_raids == ("Vault of Glass", "Crota's End")
    wr.apply_dungeons(ctx, "Duality", "Prophecy")
    assert ctx.rotator_dungeons == ("Duality", "Prophecy")
    wr.apply_gm_strike(ctx, "The Sunless Cell")
    assert ctx.gm_strike == "The Sunless Cell"


def test_apply_reward_field_sets_and_clears() -> None:
    ctx = wr.WeeklyResetContext(reset_ts=1)
    weapon = wr.WeaponRef("Null Composure", 222, "fusion_rifle")
    for _label, key in wr._REWARD_FIELDS:
        wr.apply_reward_field(ctx, key, weapon)
        assert getattr(ctx, key) is weapon
    wr.apply_reward_field(ctx, "zavala_weapon", None)
    assert ctx.zavala_weapon is None


@pytest.mark.asyncio
async def test_resolve_reward_by_hash(stub_indexes) -> None:
    assert await wr.resolve_reward_value("222") == wr.WeaponRef(
        "Null Composure", 222, "fusion_rifle"
    )


@pytest.mark.asyncio
async def test_resolve_reward_by_name(stub_indexes) -> None:
    weapon = await wr.resolve_reward_value("cloudstrike")
    assert weapon is not None
    assert weapon.hash == 333 and weapon.emoji_name == "sniper_rifle"


@pytest.mark.asyncio
async def test_resolve_reward_free_text_and_blank(stub_indexes) -> None:
    typed = await wr.resolve_reward_value("Some Custom Roll")
    assert typed is not None
    assert typed == wr.WeaponRef(name="Some Custom Roll")
    assert typed.hash is None
    assert await wr.resolve_reward_value("   ") is None


# --- Portal (component-204) derivation -------------------------------------------


def _portal_op(
    name,
    *,
    item_type=None,
    type_hash=None,
    challenges=0,
    max_party=None,
    modes=(),
    reward="",
    reward_hash=0,
):
    from dd.anchor.extensions import portal_ops as po

    return po.PortalOp(
        tab="",
        activity_name=name,
        activity_type="",
        reward_name=reward,
        reward_hash=reward_hash,
        reward_emoji="",
        tier=None,
        reward_item_type=item_type,
        activity_type_hash=type_hash,
        challenge_count=challenges,
        max_party=max_party,
        mode_types=modes,
    )


# A live-shaped DEV Portal feed. Only the GM Nightfall is derived; the daily Quickplay
# and Control weapon ops are here to prove they're ignored (they live in Portal Ops).
_LIVE_PORTAL_OPS = [
    _portal_op(
        "Quickplay",
        item_type=3,
        max_party=6,
        modes=(3, 18, 7),
        reward="Tempered Dynamo",
        reward_hash=3,
    ),  # daily weapon — ignored
    _portal_op(
        "The Sunless Cell",
        item_type=3,
        type_hash=wr._STRIKE_ACTIVITY_TYPE_HASH,
        challenges=1,
        max_party=3,
        modes=(3, 18, 7),
        reward="Lotus-Eater",
        reward_hash=4,
    ),  # GM Nightfall ✓ (has a challenge)
    _portal_op(
        "The Insight Terminus",
        item_type=3,
        type_hash=wr._STRIKE_ACTIVITY_TYPE_HASH,
        challenges=0,
        max_party=3,
        modes=(3, 18, 7),
        reward="Cynosure",
        reward_hash=5,
    ),  # plain strike — no
    _portal_op(
        "Eruption",
        item_type=3,
        max_party=6,
        modes=(88, 5),
        reward="The Helmsman",
        reward_hash=8,
    ),  # daily PvP weapon — ignored
]


@pytest.mark.asyncio
async def test_derive_portal_fields_matches_live_week(monkeypatch) -> None:
    from dd.anchor.extensions import portal_ops as po

    async def fake_fetch():
        return list(_LIVE_PORTAL_OPS)

    monkeypatch.setattr(po, "fetch_portal_ops", fake_fetch)
    result = await wr.derive_portal_fields()
    # Only the GM Nightfall is derived — the strike-type op with a weekly challenge, not
    # the plain strike; the daily Quickplay/Control weapon ops in the feed are ignored.
    assert result.gm_strike == "The Sunless Cell"
    # GM reward weapon is that same op's guaranteed reward.
    assert result.gm_weapon == wr.WeaponRef("Lotus-Eater", 4)


@pytest.mark.asyncio
async def test_derive_portal_fields_survives_fetch_failure(monkeypatch) -> None:
    from dd.anchor.extensions import portal_ops as po

    async def boom():
        raise RuntimeError("portal down")

    monkeypatch.setattr(po, "fetch_portal_ops", boom)
    assert await wr.derive_portal_fields() == wr.PortalDerivation("", None)


# --- web form: payload -> context mapping -----------------------------------------


@pytest.mark.asyncio
async def test_context_from_payload_resolves_and_enforces_rules(stub_indexes) -> None:
    payload = {
        "reset_ts": 1783443600,
        "gm_strike": "The Sunless Cell",
        "gm_weapon": "222",  # manifest hash -> full WeaponRef (server-resolved)
        "quickplay_weapon": "Custom Roll",  # unknown -> plain (unlinked) name
        "control_weapon": "",  # blank -> None
        "zavala_weapon": "Cloudstrike",  # by-name -> full WeaponRef
        "rotator_raids": ["Crota's End", "Vault of Glass"],
        "crucible_3v3": "Clash",
        "crucible_6v6": "",
        "conquests": {"GM": ["Arms Dealer", ""], "Expert": []},
        "iron_banner": True,
        "trials_active": True,  # must be forced off by the IB rule
        "update_label": "",
        "update_url": "https://example.com/notes",
        "notes_text": "Duality is available due to a bug.\n\n  ",
        "links_text": "Guide | https://example.com\nBad | ftp://x",
    }
    ctx = await wr._context_from_payload(payload)
    # Weapon slots resolved server-side (by hash + by name); unknown kept as plain name.
    assert ctx.gm_weapon == wr.WeaponRef("Null Composure", 222, "fusion_rifle")
    assert ctx.zavala_weapon == wr.WeaponRef("Cloudstrike", 333, "sniper_rifle")
    assert ctx.quickplay_weapon == wr.WeaponRef(name="Custom Roll")
    assert ctx.control_weapon is None
    # Iron Banner forces Trials off, regardless of the submitted trials_active.
    assert ctx.iron_banner is True and ctx.trials_active is False
    # Featured crucible mode prefixed with the fixed first mode; empty slot stays empty.
    assert ctx.crucible_3v3 == "Competitive, Clash"
    assert ctx.crucible_6v6 == ""
    # Conquests: blank entries dropped, empty tiers omitted.
    assert ctx.conquests == {"GM": ["Arms Dealer"]}
    # update_link defaults label; notes trimmed to non-blank lines; links http(s) only.
    assert ctx.update_link == {"label": "Update", "url": "https://example.com/notes"}
    assert ctx.notes == ["Duality is available due to a bug."]
    assert ctx.extra_links == [{"label": "Guide", "url": "https://example.com"}]


def test_parse_links_accepts_http_only() -> None:
    links = wr._parse_links(
        "Guide | https://example.com\n"
        "Insecure | http://ok.example\n"
        "Bad scheme | ftp://nope\n"
        "no pipe here\n"
        "  Trimmed  |  https://trim.example  "
    )
    assert links == [
        {"label": "Guide", "url": "https://example.com"},
        {"label": "Insecure", "url": "http://ok.example"},
        {"label": "Trimmed", "url": "https://trim.example"},
    ]


# --- web form: routes (fake-request) ----------------------------------------------


class _FakeRequest:
    """Minimal aiohttp.web.Request stand-in for the weekly-reset route handlers."""

    def __init__(
        self,
        *,
        body: t.Any = None,
        cookies: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        query: dict[str, str] | None = None,
    ) -> None:
        self.query = query or {}
        self.cookies = cookies or {}
        self.headers = headers or {}
        self._body = body

    async def json(self) -> t.Any:
        return self._body


def _req(**kwargs: t.Any) -> aiohttp.web.Request:
    return t.cast(aiohttp.web.Request, _FakeRequest(**kwargs))


# Auth is enforced by the web_auth middleware (see test_web_auth.py); these handlers
# assume an already-authenticated request and carry no auth code of their own.


@pytest.mark.asyncio
async def test_create_publish_returns_problems_for_invalid_draft(monkeypatch) -> None:
    # Create-and-publish an empty draft: it fails validate_post, so publish_draft raises
    # ValueError before it ever touches the bot — a non-None sentinel just clears the
    # 503 "bot unset" gate.
    monkeypatch.setattr(web, "_bot", object())
    await wr.save_meta(wr.DraftMeta())
    resp = await wr._handle_create(_req(body={"reset_ts": 1, "publish": True}))
    assert resp.status == 422
    assert json.loads(resp.text or "")["problems"]


# --- post body markdown ------------------------------------------------------------


class _StubEmoji:
    """A stand-in for a guild ``KnownCustomEmoji`` — only ``.url`` is read."""

    def __init__(self, url: str) -> None:
        self.url = url


def test_build_body_emits_the_markdown_the_renderer_needs() -> None:
    """The producer's half: the constructs are in the body text.

    Rendering them — emoji as <img>, escaping, link validation — belongs to the shared
    renderer, and is pinned by ``dd/anchor/preview_fixtures``.
    """
    body = wr.build_body(_full_ctx())
    assert body.startswith("# ")  # H1 title
    assert "\n### " in body  # sub-headings
    assert "**" in body  # bold
    assert ":Bungie:" in body  # a guild emoji shortcode
    assert "<t:" in body  # a Discord timestamp token
    assert "](https://" in body  # at least one masked link


def test_discord_error_note() -> None:
    # Discord's "Invalid resource" (proxied image URL) -> the specific image hint.
    proxy = wr._discord_error_note(
        ValueError(
            "Unauthorized 401: 'Invalid resource \"https://images-ext-1"
            ".discordapp.net/external/x\"'"
        )
    )
    assert "proxy link" in proxy and "direct image URL" in proxy
    # A media.discordapp.net/external/ link is flagged the same way.
    assert "proxy link" in wr._discord_error_note(
        Exception("media.discordapp.net/external/abc rejected")
    )
    # Anything else passes the (trimmed) Discord message through.
    other = wr._discord_error_note(Exception("Some other Discord failure"))
    assert other.startswith("Discord rejected the post:")
    assert "Some other Discord failure" in other


# --- DraftMeta lifecycle state ----------------------------------------------------


def test_draft_meta_round_trip() -> None:
    meta = wr.DraftMeta(
        message_id=999,
        reset_ts=1783443600,
        crossposted=True,
        status="published",
        last_edited_ts=5,
    )
    assert wr.DraftMeta.from_dict(meta.to_dict()) == meta


def test_draft_meta_back_compat_from_published_message_id() -> None:
    # Pre-lifecycle docs stored published_message_id + status (and the now-dropped
    # card_* fields); message_id reads the old key, crossposted defaults from status.
    meta = wr.DraftMeta.from_dict(
        {
            "published_message_id": 42,
            "status": "published",
            "card_channel_id": 1,
            "card_message_id": 2,
        }
    )
    assert meta.message_id == 42
    assert meta.crossposted is True
    assert meta.status == "published"
    # reset_ts predates per-week tracking, so a legacy doc has no notion of it.
    assert meta.reset_ts == 0


def test_draft_meta_back_compat_unpublished_defaults_not_crossposted() -> None:
    meta = wr.DraftMeta.from_dict({"published_message_id": 0, "status": "draft"})
    assert meta.message_id == 0 and meta.crossposted is False and meta.status == "draft"


def test_draft_meta_is_current() -> None:
    now = 1783443600
    prev, nxt = 1782838800, 1784048400
    # No post -> never current.
    assert wr.DraftMeta(message_id=0, reset_ts=now).is_current(now) is False
    # Post stamped with this week -> current.
    assert wr.DraftMeta(message_id=42, reset_ts=now).is_current(now) is True
    # Post stamped with another (past or future) week -> not current: start fresh.
    assert wr.DraftMeta(message_id=42, reset_ts=prev).is_current(now) is False
    assert wr.DraftMeta(message_id=42, reset_ts=nxt).is_current(now) is False
    # Legacy doc (reset_ts absent -> 0) with a live post -> NOT current: 0 names no
    # known period, so the form offers Create and the legacy post becomes history.
    # (A "current" 0 would never re-stamp and would stay stuck on Edit every week.)
    assert wr.DraftMeta(message_id=42, reset_ts=0).is_current(now) is False


# --- lifecycle: post_or_edit_unpublished / publish_draft / delete (fake bot) -------


class _FakeRest:
    """Records the REST mutations the lifecycle helpers make; no live Discord."""

    def __init__(self) -> None:
        self.edited: list[tuple[t.Any, int]] = []
        self.deleted: list[tuple[t.Any, int]] = []

    async def edit_message(self, channel: t.Any, message: int, **kwargs: t.Any) -> None:
        self.edited.append((channel, message))

    async def delete_message(self, channel: t.Any, message: int) -> None:
        self.deleted.append((channel, message))


class _FakeBot:
    def __init__(self) -> None:
        self.rest = _FakeRest()


def _bot(fake: _FakeBot) -> wr.CachedFetchBot:
    """Cast a recording fake to the ``CachedFetchBot`` the lifecycle helpers expect."""
    return t.cast(wr.CachedFetchBot, fake)


@pytest.fixture
def fake_publish_env(
    monkeypatch: pytest.MonkeyPatch, configured_followables: dict[str, int]
):
    """Stub the render + send/crosspost primitives so lifecycle branches are testable.

    ``format_weekly_reset`` returns a dummy component bundle; ``send_message`` /
    ``crosspost_message_with_retries`` record their calls instead of hitting Discord.

    Depends on ``configured_followables`` because publishing needs somewhere to
    publish: a followable's channel comes from its DB row alone now, so an unconfigured
    feed fails ``spec.validate`` before any of these stubs is reached.
    """
    sent: list[dict[str, t.Any]] = []
    crossposted: list[tuple[t.Any, int]] = []

    async def fake_format(ctx: t.Any, bot: t.Any) -> t.Any:
        return types.SimpleNamespace(components=["cv2"])

    async def fake_send(
        bot: t.Any,
        msg_proto: t.Any,
        channel_id: int,
        crosspost: bool = True,
        deduplicate: bool = False,
    ) -> t.Any:
        sent.append({"channel": channel_id, "crosspost": crosspost})
        return types.SimpleNamespace(id=555)

    async def fake_crosspost(
        bot: t.Any, channel: t.Any, message_id: int, **_kwargs: t.Any
    ) -> None:
        crossposted.append((channel, message_id))

    monkeypatch.setattr(wr, "format_weekly_reset", fake_format)
    monkeypatch.setattr(wr.utils, "send_message", fake_send)
    monkeypatch.setattr(wr.utils, "crosspost_message_with_retries", fake_crosspost)
    return types.SimpleNamespace(sent=sent, crossposted=crossposted)


@pytest.mark.asyncio
async def test_post_or_edit_unpublished_creates_when_no_post(fake_publish_env) -> None:
    bot = _FakeBot()
    meta = await wr.post_or_edit_unpublished(_bot(bot), _full_ctx(), wr.DraftMeta())
    channel = wr.settings.get_followable_channel_sync("weekly_reset")
    # First time: send uncrossposted; never edits; never crossposts.
    assert fake_publish_env.sent == [{"channel": channel, "crosspost": False}]
    assert not bot.rest.edited and fake_publish_env.crossposted == []
    assert meta.message_id == 555 and meta.status == "posted"
    # The new post is stamped with the CURRENT reset week (wall clock), not the draft's
    # own (user-overridable) reset_ts — so is_current stays correct under an override.
    assert meta.reset_ts == wr.current_reset_ts()


@pytest.mark.asyncio
async def test_post_or_edit_unpublished_edits_when_posted(fake_publish_env) -> None:
    bot = _FakeBot()
    meta = await wr.post_or_edit_unpublished(
        _bot(bot), _full_ctx(), wr.DraftMeta(message_id=42, status="posted")
    )
    channel = wr.settings.get_followable_channel_sync("weekly_reset")
    # Already posted: edit in place, no new send, still no crosspost.
    assert fake_publish_env.sent == []
    assert bot.rest.edited == [(channel, 42)]
    assert fake_publish_env.crossposted == []
    assert meta.message_id == 42


@pytest.mark.asyncio
async def test_publish_draft_edits_then_crossposts_existing(fake_publish_env) -> None:
    bot = _FakeBot()
    meta = wr.DraftMeta(message_id=42, status="posted", crossposted=False)
    out, note = await wr.publish_draft(_bot(bot), _full_ctx(), meta)
    channel = wr.settings.get_followable_channel_sync("weekly_reset")
    assert bot.rest.edited == [(channel, 42)] and fake_publish_env.sent == []
    assert fake_publish_env.crossposted == [(channel, 42)]
    assert out.crossposted is True and out.status == "published"
    assert "Published and crossposted" in note


@pytest.mark.asyncio
async def test_publish_draft_posts_then_crossposts_when_absent(
    fake_publish_env,
) -> None:
    bot = _FakeBot()
    out, _note = await wr.publish_draft(_bot(bot), _full_ctx(), wr.DraftMeta())
    channel = wr.settings.get_followable_channel_sync("weekly_reset")
    # Fallback path: post uncrossposted first, then crosspost that new message id.
    assert fake_publish_env.sent == [{"channel": channel, "crosspost": False}]
    assert fake_publish_env.crossposted == [(channel, 555)]
    assert out.message_id == 555 and out.crossposted is True


@pytest.mark.asyncio
async def test_publish_draft_reedit_is_idempotent(fake_publish_env) -> None:
    bot = _FakeBot()
    meta = wr.DraftMeta(message_id=42, status="published", crossposted=True)
    _out, note = await wr.publish_draft(_bot(bot), _full_ctx(), meta)
    channel = wr.settings.get_followable_channel_sync("weekly_reset")
    # Re-publish: sync the edit + (idempotent) crosspost; note reads as an edit.
    assert bot.rest.edited == [(channel, 42)]
    assert fake_publish_env.crossposted == [(channel, 42)]
    assert "Updated the published post" in note


@pytest.mark.asyncio
async def test_publish_draft_raises_and_touches_nothing_on_invalid(
    fake_publish_env,
) -> None:
    bot = _FakeBot()
    empty = wr.WeeklyResetContext(reset_ts=1)
    with pytest.raises(ValueError):
        await wr.publish_draft(_bot(bot), empty, wr.DraftMeta())
    assert fake_publish_env.sent == [] and not bot.rest.edited
    assert fake_publish_env.crossposted == []


@pytest.mark.asyncio
async def test_publish_draft_reraises_and_stays_unpublished_on_crosspost_failure(
    fake_publish_env, monkeypatch
) -> None:
    # Hardened publish: if the crosspost itself fails, publish_draft propagates the
    # error and leaves the post UNpublished — never a false crossposted/published stamp
    # (the old silent-skip bug marked it published even when nothing was broadcast).
    async def boom(*a: t.Any, **k: t.Any) -> None:
        raise wr.utils.CrosspostError("Channel is not an announcement/news channel")

    monkeypatch.setattr(wr.utils, "crosspost_message_with_retries", boom)
    bot = _FakeBot()
    meta = wr.DraftMeta(message_id=42, status="posted", crossposted=False)
    with pytest.raises(wr.utils.CrosspostError):
        await wr.publish_draft(_bot(bot), _full_ctx(), meta)
    # The in-channel edit happened, but the crosspost did not — so it stays unpublished.
    assert meta.crossposted is False and meta.status == "posted"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_handle_edit_publish_returns_502_when_crosspost_fails(
    monkeypatch, stub_indexes
) -> None:
    # End-to-end via the web handler: a failed crosspost returns a 502 with a real error
    # (not an indefinitely-hanging request), and the persisted post is NOT marked
    # published — so a retry from the form is safe.
    monkeypatch.setattr(web, "_bot", _FakeBot())
    monkeypatch.setattr(wr, "current_reset_ts", lambda *a, **k: 1783443600)
    await wr.save_meta(
        wr.DraftMeta(message_id=42, reset_ts=1783443600, status="posted")
    )

    async def boom(spec: t.Any, bot: t.Any, ctx: t.Any, meta: t.Any) -> None:
        raise wr.utils.CrosspostError("Channel is not an announcement/news channel")

    monkeypatch.setattr(wr.hybrid_post_core, "publish_draft", boom)
    resp = await wr._handle_edit(_req(body={"reset_ts": 1783443600, "publish": True}))
    assert resp.status == 502
    problems = json.loads(resp.text or "")["problems"]
    assert problems and "publish" in problems[0].lower()
    saved = await wr.load_meta()
    assert saved.crossposted is False and saved.status == "posted"


# --- autopost toggle + reset-day cron removed --------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bootstrap_has_no_autopost_toggle(stub_indexes) -> None:
    boot = await wr._build_bootstrap(_full_ctx(), wr.DraftMeta())
    assert "autopost_enabled" not in boot


def test_no_autopost_route_handler_or_cron() -> None:
    app = aiohttp.web.Application()
    wr.register_weekly_reset_routes(app)
    paths = {r.resource.canonical for r in app.router.routes() if r.resource}
    assert "/weekly_reset/create" in paths  # the real buttons still route
    assert "/weekly_reset/auto" not in paths  # the toggle route is gone
    # The toggle handler, the reset-day cron, and its spec hooks are all removed.
    assert not hasattr(wr, "_handle_auto")
    assert not hasattr(wr, "run_reset_draft")
    assert not hasattr(wr._SPEC, "get_autopost")
    assert not hasattr(wr._SPEC, "set_autopost")


@pytest.mark.asyncio
async def test_handle_create_posts_and_returns_warnings(
    monkeypatch, fake_publish_env, stub_indexes
) -> None:
    bot = _FakeBot()
    monkeypatch.setattr(web, "_bot", bot)
    # Pin "this week" to the draft's reset boundary so post_this_period reads True.
    monkeypatch.setattr(wr, "current_reset_ts", lambda *a, **k: 1783443600)
    await wr.save_meta(wr.DraftMeta())  # fresh: no post yet
    # A near-empty draft trips validate_post, but Create still succeeds AND posts it —
    # the problems come back as non-blocking warnings, not a 422.
    resp = await wr._handle_create(_req(body={"reset_ts": 1783443600}))
    assert resp.status == 200
    data = json.loads(resp.text or "")
    assert data["ok"] is True and data["warnings"]
    assert data["post_this_period"] is True and data["crossposted"] is False
    channel = wr.settings.get_followable_channel_sync("weekly_reset")
    assert fake_publish_env.sent == [{"channel": channel, "crosspost": False}]
    meta = await wr.load_meta()
    assert meta.message_id == 555 and meta.status == "posted"
    assert meta.reset_ts == 1783443600


@pytest.mark.asyncio
async def test_handle_create_preserves_last_editor(
    monkeypatch, fake_publish_env, stub_indexes
) -> None:
    # Create resets the message-tracking fields but keeps editorial metadata like the
    # last editor (a slash-command edit may have set it) — it isn't wiped to 0.
    bot = _FakeBot()
    monkeypatch.setattr(web, "_bot", bot)
    monkeypatch.setattr(wr, "current_reset_ts", lambda *a, **k: 1783443600)
    await wr.save_meta(wr.DraftMeta(last_edited_by=12345))  # no post yet
    resp = await wr._handle_create(_req(body={"reset_ts": 1783443600}))
    assert resp.status == 200
    meta = await wr.load_meta()
    assert meta.message_id == 555 and meta.last_edited_by == 12345


@pytest.mark.asyncio
async def test_handle_create_reports_problem_when_post_fails(monkeypatch) -> None:
    # If the in-channel send itself fails (e.g. a bad image URL), no message was
    # created, so the handler returns a blocking `problems` 502 — NOT a green ok:true —
    # and the meta is not persisted with a phantom message id.
    bot = _FakeBot()
    monkeypatch.setattr(web, "_bot", bot)
    monkeypatch.setattr(wr, "current_reset_ts", lambda *a, **k: 1783443600)

    async def boom(*a: t.Any, **k: t.Any) -> t.Any:
        raise RuntimeError("Discord rejected the image")

    monkeypatch.setattr(wr, "post_or_edit_unpublished", boom)
    await wr.save_meta(wr.DraftMeta())
    resp = await wr._handle_create(_req(body={"reset_ts": 1783443600}))
    assert resp.status == 502
    data = json.loads(resp.text or "")
    assert data.get("problems") and "ok" not in data
    meta = await wr.load_meta()
    assert meta.message_id == 0  # nothing tracked; not a false success


@pytest.mark.asyncio
async def test_handle_create_refuses_when_post_exists(monkeypatch) -> None:
    # Create is a no-op guard when this week already has a post — the form hides the
    # button, and the server enforces it so a stale tab can't orphan the live message.
    monkeypatch.setattr(web, "_bot", object())
    monkeypatch.setattr(wr, "current_reset_ts", lambda *a, **k: 1783443600)
    await wr.save_meta(
        wr.DraftMeta(message_id=42, reset_ts=1783443600, status="posted")
    )
    resp = await wr._handle_create(_req(body={"reset_ts": 1783443600}))
    assert resp.status == 409
    assert "already exists" in json.loads(resp.text or "")["error"]


@pytest.mark.asyncio
async def test_handle_edit_edits_existing_in_place(
    monkeypatch, fake_publish_env, stub_indexes
) -> None:
    bot = _FakeBot()
    monkeypatch.setattr(web, "_bot", bot)
    monkeypatch.setattr(wr, "current_reset_ts", lambda *a, **k: 1783443600)
    # A published post for this week: Edit updates it in place (re-mirrors) and keeps
    # the crossposted state; no new message is sent.
    await wr.save_meta(
        wr.DraftMeta(
            message_id=42, reset_ts=1783443600, status="published", crossposted=True
        )
    )
    resp = await wr._handle_edit(_req(body={"reset_ts": 1783443600}))
    assert resp.status == 200
    data = json.loads(resp.text or "")
    assert data["ok"] is True
    assert data["post_this_period"] is True and data["crossposted"] is True
    channel = wr.settings.get_followable_channel_sync("weekly_reset")
    assert bot.rest.edited == [(channel, 42)] and fake_publish_env.sent == []
    meta = await wr.load_meta()
    assert meta.message_id == 42


@pytest.mark.asyncio
async def test_handle_edit_refuses_when_no_post(monkeypatch) -> None:
    monkeypatch.setattr(web, "_bot", object())
    monkeypatch.setattr(wr, "current_reset_ts", lambda *a, **k: 1783443600)
    await wr.save_meta(wr.DraftMeta())  # no post this week
    resp = await wr._handle_edit(_req(body={"reset_ts": 1783443600}))
    assert resp.status == 409
    assert "No weekly-reset post" in json.loads(resp.text or "")["error"]


@pytest.mark.asyncio
async def test_handle_edit_refuses_stale_prior_week_post(monkeypatch) -> None:
    # A post tracked from a PRIOR week isn't editable here — the form starts fresh and
    # Edit refuses, so we never accidentally edit last week's message.
    monkeypatch.setattr(web, "_bot", object())
    monkeypatch.setattr(wr, "current_reset_ts", lambda *a, **k: 1783443600)
    await wr.save_meta(
        wr.DraftMeta(message_id=42, reset_ts=1782838800, status="posted")
    )
    resp = await wr._handle_edit(_req(body={"reset_ts": 1783443600}))
    assert resp.status == 409


@pytest.mark.asyncio
async def test_handle_create_503_when_bot_unset(monkeypatch) -> None:
    monkeypatch.setattr(web, "_bot", None)
    resp = await wr._handle_create(_req(body={"reset_ts": 1}))
    assert resp.status == 503


async def _stub_form_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the manifest/API-backed bits of the GET form so it renders offline."""

    async def fake_options() -> dict[str, t.Any]:
        return {
            "items": [],
            "conquests": {},
            "strikes": [],
            "raids": [],
            "dungeons": [],
            "pantheon": [],
            "crucible_modes": [],
            "crucible_3v3_first": "",
            "crucible_6v6_first": "",
        }

    async def fake_build(config: t.Any = None) -> wr.WeeklyResetContext:
        return wr.WeeklyResetContext(reset_ts=wr.current_reset_ts())

    monkeypatch.setattr(wr, "_build_options", fake_options)
    monkeypatch.setattr(wr, "build_draft_context", fake_build)


@pytest.mark.asyncio
async def test_form_get_loads_current_week_post(monkeypatch) -> None:
    # A post tracked for THIS week -> the form opens on its saved draft and reports
    # post_this_period so the client shows Edit/Delete (not the Create buttons).
    monkeypatch.setattr(wr, "current_reset_ts", lambda *a, **k: 1783443600)
    await _stub_form_deps(monkeypatch)
    ctx = _full_ctx()
    ctx.reset_ts = 1783443600
    await wr.save_draft(ctx)
    await wr.save_meta(
        wr.DraftMeta(message_id=42, reset_ts=1783443600, status="posted")
    )
    resp = await wr._handle_form_get(_req())
    assert resp.status == 200
    assert '"post_this_period": true' in (resp.text or "")


@pytest.mark.asyncio
async def test_form_get_starts_fresh_when_no_current_post(monkeypatch) -> None:
    # A post/draft left over from a PRIOR week is stale: the form starts a fresh draft
    # and reports no post this period, so the client shows the Create buttons.
    monkeypatch.setattr(wr, "current_reset_ts", lambda *a, **k: 1783443600)
    await _stub_form_deps(monkeypatch)
    stale = _full_ctx()
    stale.reset_ts = 1782838800  # last week
    await wr.save_draft(stale)
    await wr.save_meta(
        wr.DraftMeta(message_id=42, reset_ts=1782838800, status="posted")
    )
    resp = await wr._handle_form_get(_req())
    assert resp.status == 200
    assert '"post_this_period": false' in (resp.text or "")


@pytest.mark.asyncio
async def test_form_get_legacy_post_offers_create(monkeypatch) -> None:
    # A live post from before per-week tracking has reset_ts=0, which names no known
    # period. The form must NOT treat it as current — it reports post_this_period False
    # so the client shows Create, and the legacy post stays in the channel as history.
    # (Treating 0 as current would stick the form on Edit forever, since editing never
    # re-stamps reset_ts — the real prod bug this fixes.)
    monkeypatch.setattr(wr, "current_reset_ts", lambda *a, **k: 1783443600)
    await _stub_form_deps(monkeypatch)
    ctx = _full_ctx()
    ctx.reset_ts = 1783443600
    await wr.save_draft(ctx)
    await wr.save_meta(
        wr.DraftMeta(message_id=42, status="published", crossposted=True)  # reset_ts=0
    )
    resp = await wr._handle_form_get(_req())
    assert resp.status == 200
    assert '"post_this_period": false' in (resp.text or "")


@pytest.mark.asyncio
async def test_handle_delete_removes_post_and_resets(monkeypatch) -> None:
    bot = _FakeBot()
    monkeypatch.setattr(web, "_bot", bot)
    await wr.save_meta(
        wr.DraftMeta(message_id=77, status="published", crossposted=True)
    )
    resp = await wr._handle_delete(_req())
    assert resp.status == 200 and json.loads(resp.text or "") == {"ok": True}
    assert bot.rest.deleted == [
        (wr.settings.get_followable_channel_sync("weekly_reset"), 77)
    ]
    meta = await wr.load_meta()
    assert meta.message_id == 0 and meta.crossposted is False and meta.status == "draft"


@pytest.mark.asyncio
async def test_handle_delete_noop_when_unposted(monkeypatch) -> None:
    bot = _FakeBot()
    monkeypatch.setattr(web, "_bot", bot)
    await wr.save_meta(wr.DraftMeta(message_id=0, status="draft"))
    resp = await wr._handle_delete(_req())
    assert resp.status == 200 and json.loads(resp.text or "") == {"ok": True}
    assert bot.rest.deleted == []


@pytest.mark.asyncio
async def test_handle_delete_503_when_bot_unset(monkeypatch) -> None:
    monkeypatch.setattr(web, "_bot", None)
    resp = await wr._handle_delete(_req())
    assert resp.status == 503
