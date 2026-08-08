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

"""JSON Schemas for rotation-based posts (``lost_sector``, …).

One schema per post type is the single source of truth for **both** the json-editor
web form *and* server-side validation. The UI-only keywords (``options``, ``watch``,
``enumSource``, ``headerTemplate``) are ignored by the validator, so the same document
that the form produces validates on the server.

The vocab constants below are also imported by
:mod:`dd.sector_accounting.sector_accounting` (``Rotation.from_json``/``to_json``) so
the form, the validator and the domain mapping can never drift apart.
"""

from __future__ import annotations

import typing as t

import fastjsonschema

# --- shared vocab -----------------------------------------------------------------

CHAMPION_TYPES = ["Barrier", "Overload", "Unstoppable"]
SHIELD_ELEMENTS = ["Arc", "Void", "Solar", "Stasis", "Strand"]
# The nine destinations, each an independent daily cycle.
LOST_SECTOR_ZONES = [
    "Cosmodrome",
    "Dreaming City",
    "EDZ",
    "Europa",
    "Moon",
    "Neomuna",
    "Nessus",
    "Pale Heart",
    "Throne World",
]


def _build_lost_sector_schema() -> dict[str, t.Any]:
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "title": "Lost sector rotation",
        "required": ["reference_date", "schedule", "sectors"],
        "additionalProperties": False,
        "properties": {
            "version": {"type": "integer", "options": {"hidden": True}},
            "reference_date": {
                "type": "string",
                "format": "date",
                "title": "Rotation start date",
            },
            "schedule": {
                "type": "object",
                "title": "Daily schedule (per destination)",
                "required": list(LOST_SECTOR_ZONES),
                "additionalProperties": False,
                "properties": {
                    zone: {"$ref": "#/definitions/zoneCycle"}
                    for zone in LOST_SECTOR_ZONES
                },
            },
            "sectors": {
                "type": "array",
                "format": "tabs",
                "title": "Sectors",
                "headerTemplate": "{{ self.name }}",
                "items": {
                    "type": "object",
                    "title": "Sector",
                    "required": ["name", "shortlink_gfx", "expert", "master"],
                    "properties": {
                        "name": {"type": "string", "title": "Name"},
                        "shortlink_gfx": {
                            "type": "string",
                            "format": "uri",
                            "title": "Graphic link",
                        },
                        "expert": {
                            "$ref": "#/definitions/difficulty",
                            "title": "Expert",
                        },
                        "master": {
                            "$ref": "#/definitions/difficulty",
                            "title": "Master",
                        },
                    },
                },
            },
        },
        "definitions": {
            "zoneCycle": {
                "type": "array",
                "title": "Daily sectors",
                "items": {
                    "type": "string",
                    # UI-only: populate the dropdown from the sector list (typo
                    # safety without a hard enum). Ignored by the validator.
                    "watch": {"secs": "sectors"},
                    "enumSource": [{"source": "secs", "value": "{{ item.name }}"}],
                },
            },
            "difficulty": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "champions": {
                        "type": "array",
                        "format": "checkbox",
                        "uniqueItems": True,
                        "items": {"type": "string", "enum": list(CHAMPION_TYPES)},
                    },
                    "shields": {
                        "type": "array",
                        "format": "checkbox",
                        "uniqueItems": True,
                        "items": {"type": "string", "enum": list(SHIELD_ELEMENTS)},
                    },
                },
            },
        },
    }


LOST_SECTOR_SCHEMA: dict[str, t.Any] = _build_lost_sector_schema()


def _build_xur_location_schema() -> dict[str, t.Any]:
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "title": "Xûr location map",
        "required": ["locations"],
        "additionalProperties": False,
        "properties": {
            "version": {"type": "integer", "options": {"hidden": True}},
            "locations": {
                "type": "array",
                "title": "Locations",
                "items": {
                    "type": "object",
                    "title": "Location",
                    # Only the API name is required; a missing friendly name / link
                    # renders as the raw API name (XurLocation.__str__ handles it).
                    "required": ["api_location_name"],
                    "additionalProperties": False,
                    "properties": {
                        "api_location_name": {
                            "type": "string",
                            "title": "API location name",
                        },
                        "friendly_location_name": {
                            "type": "string",
                            "title": "Friendly location name",
                        },
                        "link": {
                            "type": "string",
                            "format": "uri",
                            "title": "Link",
                        },
                    },
                },
            },
        },
    }


XUR_LOCATION_SCHEMA: dict[str, t.Any] = _build_xur_location_schema()


# --- Trials of Osiris bonus-focus-pool loot rotation ------------------------------
#
# The Trials "bonus focus pool" cycles through a fixed loop of curated weapon sets, one
# per *active* weekend. Unlike a world-activity rotation it is **not** date-anchored:
# the Trials producer (:mod:`dd.anchor.extensions.trials`) owns a skip-aware cursor so
# Iron Banner "No Trials" weekends don't consume a set. So this type stays OUT of
# ``WORLD_ACTIVITY_SLUGS`` — the editor just stores a pool of named sets + a looping
# schedule of set names; the producer walks it. Weapons only (no armor). The set values
# carry the same ``"Name (Type)"`` shape the editor's item autocomplete produces; the
# producer strips the type suffix before resolving names to manifest items.

#: The baked default loot loop — the single source of truth for both the editor's
#: starting document (:func:`trials_loot_default_doc`) and the producer's fallback when
#: the ``trials_loot`` row is absent. Seeded from the "Trials Bonus Pools" tab of the
#: rotation spreadsheet as a one-off; the bot never reads the sheet at runtime.
TRIALS_DEFAULT_LOOT_SETS: tuple[tuple[str, ...], ...] = (
    (
        "The Scholar",
        "Exile's Curse",
        "Sola's Scar",
        "Forgiveness",
        "Aisha's Embrace",
        "Corundum Hammer",
        "Astral Horizon",
    ),
    (
        "Aisha's Care",
        "Keen Thistle",
        "Willful Hamartia",
        "The Immortal",
        "Burden of Guilt",
        "Unwavering Duty",
        "Cataphract GL3",
    ),
    (
        "Exalted Truth",
        "Eye of Sol",
        "Tomorrow's Answer",
        "Everburning Glitz",
        "Auric Disabler",
        "Aureus Neutralizer",
        "The Martlet",
    ),
)

TRIALS_LOOT_SLUG = "trials_loot"


def _build_trials_loot_schema() -> dict[str, t.Any]:
    """A weapons-only set pool + a looping schedule of set names (no ``reference_date``:
    the producer owns the cursor, so there's nothing to date-anchor)."""
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "title": "Trials loot pool",
        "required": ["schedule", "sets"],
        "additionalProperties": False,
        "properties": {
            "version": {"type": "integer", "options": {"hidden": True}},
            "schedule": {
                "type": "array",
                "title": "Schedule (set names, in order, looping)",
                "items": {"type": "string"},
            },
            "sets": {
                "type": "array",
                "title": "Sets",
                "format": "tabs",
                "headerTemplate": "{{ self.name }}",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "title": "Set",
                    "required": ["name", "weapons"],
                    "additionalProperties": False,
                    "properties": {
                        "name": {"type": "string", "title": "Set name"},
                        "weapons": {
                            "type": "array",
                            "title": "Weapons",
                            "items": {"type": "string"},
                        },
                    },
                },
            },
        },
    }


TRIALS_LOOT_SCHEMA: dict[str, t.Any] = _build_trials_loot_schema()


def trials_loot_default_doc() -> dict[str, t.Any]:
    """The editor's starting document: the baked default sets + a one-loop schedule.

    Also the shape the producer expects. Sets are named ``Pool 1``/``Pool 2``/… and the
    schedule lists them once, in order; the producer loops it. Matches the producer's
    ``TRIALS_DEFAULT_LOOT_SETS`` fallback so an unsaved editor and the runtime agree."""
    names = [f"Pool {i + 1}" for i in range(len(TRIALS_DEFAULT_LOOT_SETS))]
    return {
        "version": 1,
        "schedule": list(names),
        "sets": [
            {"name": name, "weapons": list(weapons)}
            for name, weapons in zip(names, TRIALS_DEFAULT_LOOT_SETS, strict=True)
        ],
    }


# --- Iron Banner bonus-focus-pool + schedule --------------------------------------
#
# Iron Banner runs one week roughly every 4 weeks — on the weeks Trials is NOT live. Its
# post is fully automatic (the ``iron_banner`` anchor producer), so unlike Trials there
# is no cursor and no web form: the schedule is **date-anchored**. Each schedule entry
# is one Iron Banner week (a Tuesday-reset start date) naming the bonus focus pool
# active that week; the producer looks up the event whose window contains "now". Pools
# are named weapon lists (weapons only — the bonus focus pool weapons from the "Iron
# Banner Bonus Pools" tab of the rotation spreadsheet). Like Trials, weapon values carry
# the editor autocomplete's ``"Name (Type)"`` shape; the producer strips the type suffix
# and resolves names to manifest items at render time (light.gg links + weapon-type
# emoji), so this type needs no ``item_links`` baking and stays OUT of
# ``WORLD_ACTIVITY_SLUGS``.

IRON_BANNER_SLUG = "iron_banner"

#: Default game modes shown when a schedule entry names none. Kyber reports Iron Banner
#: has run Control / Eruption every event; editable per-week in case that changes.
IRON_BANNER_DEFAULT_MODES = "Control / Eruption"

#: The two bonus focus pools, seeded from the "Iron Banner Bonus Pools" tab as a one-off
#: (the bot never reads the sheet at runtime). Weapons only; bare names resolve to
#: manifest items at render time.
IRON_BANNER_DEFAULT_POOLS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Pool 1",
        (
            "The Forward Path",
            "The Time-Worn Spire",
            "The Wizened Rebuke",
            "Crimil's Dagger",
            "Gunnora's Axe",
            "Felwinter's Lie",
            "Reghusk's Pledge",
        ),
    ),
    (
        "Pool 2",
        (
            "Multimach CCX",
            "Finite Impactor",
            "Occluded Finality",
            "Lethal Abundance",
            "Pressurized Precision",
            "Point of the Stag",
            "Roar of the Bear",
        ),
    ),
)

#: The Iron Banner week schedule, seeded from the "Iron Banner Schedule" tab: one
#: ``(start_date, pool_name)`` per event. Start dates are Tuesday resets; the producer
#: derives the end (start + 7 days). Pools alternate Pool 1 / Pool 2. The list is a
#: starting point the team extends/edits in the rotation editor as Bungie confirms them.
IRON_BANNER_DEFAULT_SCHEDULE: tuple[tuple[str, str], ...] = (
    ("2026-06-30", "Pool 1"),
    ("2026-07-28", "Pool 2"),
    ("2026-08-25", "Pool 1"),
    ("2026-09-22", "Pool 2"),
    ("2026-10-20", "Pool 1"),
    ("2026-11-17", "Pool 2"),
    ("2026-12-15", "Pool 1"),
    ("2027-01-12", "Pool 2"),
    ("2027-02-09", "Pool 1"),
    ("2027-03-09", "Pool 2"),
    ("2027-04-06", "Pool 1"),
    ("2027-05-04", "Pool 2"),
    ("2027-06-01", "Pool 1"),
    ("2027-06-29", "Pool 2"),
)


def _build_iron_banner_schema() -> dict[str, t.Any]:
    """A date-anchored schedule of Iron Banner weeks + a pool of named weapon lists.

    No ``reference_date``/``item_links``: each schedule entry carries its own start
    date, and weapon light.gg links are resolved at render time (not baked)."""
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "title": "Iron Banner",
        "required": ["schedule", "pools"],
        "additionalProperties": False,
        "properties": {
            "version": {"type": "integer", "options": {"hidden": True}},
            "schedule": {
                "type": "array",
                "title": "Schedule (one entry per Iron Banner week)",
                "format": "tabs",
                "headerTemplate": "{{ self.start }} · {{ self.pool }}",
                "items": {
                    "type": "object",
                    "title": "Iron Banner week",
                    "required": ["start", "pool"],
                    "additionalProperties": False,
                    "properties": {
                        "start": {
                            "type": "string",
                            "format": "date",
                            "title": "Start date (a weekly-reset Tuesday)",
                        },
                        "pool": {"type": "string", "title": "Bonus focus pool"},
                        "modes": {
                            "type": "string",
                            "title": "Game modes (blank = Control / Eruption)",
                        },
                    },
                },
            },
            "pools": {
                "type": "array",
                "title": "Bonus focus pools",
                "format": "tabs",
                "headerTemplate": "{{ self.name }}",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "title": "Pool",
                    "required": ["name", "weapons"],
                    "additionalProperties": False,
                    "properties": {
                        "name": {"type": "string", "title": "Pool name"},
                        "weapons": {
                            "type": "array",
                            "title": "Weapons",
                            "items": {"type": "string"},
                        },
                    },
                },
            },
        },
    }


IRON_BANNER_SCHEMA: dict[str, t.Any] = _build_iron_banner_schema()


def iron_banner_default_doc() -> dict[str, t.Any]:
    """The editor's starting document (and the producer's fallback when the row is
    absent): the seeded schedule + the two default pools."""
    return {
        "version": 1,
        "schedule": [
            {"start": start, "pool": pool, "modes": IRON_BANNER_DEFAULT_MODES}
            for start, pool in IRON_BANNER_DEFAULT_SCHEDULE
        ],
        "pools": [
            {"name": name, "weapons": list(weapons)}
            for name, weapons in IRON_BANNER_DEFAULT_POOLS
        ],
    }


# --- legacy world-activity rotations ----------------------------------------------
#
# Each Destiny "legacy" destination (Neomuna, the Moon, Dares of Eternity, …) is one
# rotation document made of independent *activities*. Each activity is a set of
# *elements* (weapon, location, boss, …), and **every element is its own independent,
# variable-length cyclic list** of values — so a 2-cycle mode can sit beside a 4-cycle
# boss, or a Dares weapon slot running 12 unique weeks beside an armor slot on a 3-week
# cycle. One shared schema *builder* + a per-destination *spec* gives DRY code but a
# precise form per destination. The document is self-describing (each activity carries
# its ``key``/``title``/``cadence`` as hidden constants) so the domain model
# (:mod:`dd.sector_accounting.legacy_activities`) never needs the spec.

# An activity is ``(key, title, cadence, elements)`` where ``elements`` is either a list
# of element names (element-based) or the literal ``"sets"`` (set-based, e.g. Dares
# loot). A spec is ``(title, [activity, ...])``.
_Activity = tuple[str, str, t.Literal["daily", "weekly"], list[str] | t.Literal["sets"]]
_DestinationSpec = tuple[str, list[_Activity]]

# DB-facing slug prefix for these rotation post types. Defined once so the stored
# identifier (``world_activity_neomuna``) is decoupled from the internal module names
# (which still say "legacy" — Bungie/Kyber's own term for the activity category). See
# :func:`is_world_activity` for the dispatch predicate that replaced ``startswith``.
ROTATION_SLUG_PREFIX = "world_activity_"


def rotation_slug(key: str) -> str:
    """The DB slug for a destination key (``neomuna`` → ``world_activity_neomuna``)."""
    return f"{ROTATION_SLUG_PREFIX}{key}"


LEGACY_DESTINATIONS: dict[str, _DestinationSpec] = {
    "neomuna": (
        "Neomuna",
        [
            ("vex_incursion", "Vex Incursion Zone", "weekly", ["zone"]),
            ("story_mission", "Story Mission", "weekly", ["mission"]),
            ("partition", "Partition", "weekly", ["variant"]),
            ("terminal_overload", "Terminal Overload", "daily", ["weapon", "location"]),
        ],
    ),
    "moon": (
        "The Moon",
        [
            (
                "wandering_nightmare",
                "Wandering Nightmare (Patrol)",
                "weekly",
                ["nightmare", "location"],
            ),
            (
                "nightmare_hunts",
                "Nightmare Hunts",
                "weekly",
                ["hunt_1", "hunt_2", "hunt_3"],
            ),
            ("altar_of_sorrow", "Altar of Sorrow", "daily", ["weapon", "boss"]),
        ],
    ),
    "dreaming_city": (
        "Dreaming City",
        [
            ("petras_location", "Petra's Location", "weekly", ["location"]),
            ("blind_well", "Blind Well", "weekly", ["charge"]),
            ("quest", "Weekly Quest", "weekly", ["quest"]),
            ("curse_strength", "Curse Strength", "weekly", ["strength"]),
            (
                "ascendant_challenge",
                "Ascendant Challenge",
                "weekly",
                ["challenge", "location"],
            ),
        ],
    ),
    "europa": (
        "Europa",
        [
            ("eclipsed_zone", "Eclipsed Zone", "weekly", ["zone"]),
            ("exo_challenge", "Exo Challenge", "weekly", ["challenge"]),
            ("empire_hunt", "Empire Hunt", "weekly", ["hunt"]),
        ],
    ),
    "dares": (
        "Dares of Eternity",
        [
            ("rounds", "Encounter Rounds", "weekly", ["first", "second", "final"]),
            # Set-based: 4 fixed loot sets + a weekly schedule of which set is live.
            ("loot_table", "Legendary Loot", "weekly", "sets"),
        ],
    ),
    "pale_heart": (
        "The Pale Heart",
        [
            ("overthrow", "Overthrow (Matchmade)", "daily", ["location"]),
        ],
    ),
    "throne_world": (
        "Savathûn's Throne World",
        [
            ("wellspring", "Wellspring", "daily", ["weapon", "mode", "boss"]),
            ("lucent_executioner", "Lucent Executioner", "daily", ["boss", "location"]),
        ],
    ),
    "kepler": (
        "Kepler",
        [
            (
                "story_mission",
                "Weekly Story Mission",
                "weekly",
                ["fabled", "legendary"],
            ),
        ],
    ),
    "rahool": (
        "Rahool",
        [
            ("rahool_focus", "Rahool's Armor Focus", "daily", ["slot"]),
        ],
    ),
}


def _legacy_field_label(name: str) -> str:
    return name.replace("_", " ").title()


def _legacy_element_schema(name: str) -> dict[str, t.Any]:
    label = _legacy_field_label(name)
    return {
        "type": "object",
        "title": label,
        "required": ["name", "values"],
        "additionalProperties": False,
        "properties": {
            # The element name is fixed by the spec (hidden const); only ``values`` —
            # the element's independent cycle — is edited.
            "name": {"type": "string", "const": name, "options": {"hidden": True}},
            "values": {
                "type": "array",
                "title": f"{label} rotation",
                "items": {"type": "string"},
            },
        },
    }


def _legacy_sets_activity_schema(
    key: str, title: str, cadence: str
) -> dict[str, t.Any]:
    """Schema for a set-based activity: a set pool + a weekly schedule of set ids."""
    return {
        "type": "object",
        "title": title,
        "required": ["key", "title", "cadence", "kind", "schedule", "sets"],
        "additionalProperties": False,
        "properties": {
            "key": {"type": "string", "const": key, "options": {"hidden": True}},
            "title": {"type": "string", "const": title, "options": {"hidden": True}},
            "cadence": {
                "type": "string",
                "const": cadence,
                "options": {"hidden": True},
            },
            "kind": {"type": "string", "const": "sets", "options": {"hidden": True}},
            "schedule": {
                "type": "array",
                "title": "Weekly schedule (set names, in order, looping)",
                "items": {"type": "string"},
            },
            "sets": {
                "type": "array",
                "title": "Sets",
                "format": "tabs",
                "headerTemplate": "{{ self.name }}",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "title": "Set",
                    "required": ["name", "weapons", "armor"],
                    "additionalProperties": False,
                    "properties": {
                        "name": {"type": "string", "title": "Set name"},
                        "weapons": {
                            "type": "array",
                            "title": "Weapons",
                            "items": {"type": "string"},
                        },
                        "armor": {
                            "type": "array",
                            "title": "Armor",
                            "items": {"type": "string"},
                        },
                    },
                },
            },
        },
    }


def _legacy_activity_schema(activity: _Activity) -> dict[str, t.Any]:
    key, title, cadence, element_names = activity
    if element_names == "sets":
        return _legacy_sets_activity_schema(key, title, cadence)
    return {
        "type": "object",
        "title": title,
        "required": ["key", "title", "cadence", "elements"],
        "additionalProperties": False,
        "properties": {
            # Structural fields are fixed by the spec — hidden constants so the form
            # only ever edits element values, and the stored doc stays self-describing.
            "key": {"type": "string", "const": key, "options": {"hidden": True}},
            "title": {"type": "string", "const": title, "options": {"hidden": True}},
            "cadence": {
                "type": "string",
                "const": cadence,
                "options": {"hidden": True},
            },
            # A fixed tuple: exactly these elements, in this order.
            "elements": {
                "type": "array",
                "title": f"{title} elements",
                "minItems": len(element_names),
                "maxItems": len(element_names),
                "additionalItems": False,
                "items": [_legacy_element_schema(name) for name in element_names],
            },
        },
    }


def _build_legacy_rotation_schema(spec: _DestinationSpec) -> dict[str, t.Any]:
    title, activities = spec
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "title": f"{title} (legacy)",
        "required": ["reference_date", "activities"],
        "additionalProperties": False,
        "properties": {
            "version": {"type": "integer", "options": {"hidden": True}},
            # Weapon value → light.gg URL, baked by the editor on save (hidden from the
            # form). Optional; absent/blank means the post renders the name un-linked.
            "item_links": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "options": {"hidden": True},
            },
            "reference_date": {
                "type": "string",
                "format": "date",
                "title": "Rotation start date (a weekly-reset Tuesday)",
            },
            # A fixed tuple: exactly these activities, in this order.
            "activities": {
                "type": "array",
                "title": "Activities",
                "minItems": len(activities),
                "maxItems": len(activities),
                "additionalItems": False,
                "items": [_legacy_activity_schema(a) for a in activities],
            },
        },
    }


def _legacy_default_activity(activity: _Activity) -> dict[str, t.Any]:
    key, title, cadence, element_names = activity
    if element_names == "sets":
        return {
            "key": key,
            "title": title,
            "cadence": cadence,
            "kind": "sets",
            "schedule": [],
            "sets": [],
        }
    return {
        "key": key,
        "title": title,
        "cadence": cadence,
        "elements": [{"name": name, "values": []} for name in element_names],
    }


def legacy_default_doc(post_type: str) -> dict[str, t.Any]:
    """An empty-but-structurally-complete scaffold for a world-activity slug."""
    _title, activities = LEGACY_DESTINATIONS[
        post_type.removeprefix(ROTATION_SLUG_PREFIX)
    ]
    return {
        "version": 1,
        "reference_date": "",
        "activities": [_legacy_default_activity(a) for a in activities],
    }


# Registry keyed by post-type slug (matches AutoPostSettings.name / feed slug).
ROTATION_SCHEMAS: dict[str, dict[str, t.Any]] = {
    "lost_sector": LOST_SECTOR_SCHEMA,
    "xur_location": XUR_LOCATION_SCHEMA,
    # A standalone weapons-only set pool; NOT a world activity (see WORLD_ACTIVITY_SLUGS
    # below) — the Trials producer owns its cursor, so no date-anchoring/bake/reset.
    TRIALS_LOOT_SLUG: TRIALS_LOOT_SCHEMA,
    # Iron Banner: a date-anchored schedule of weeks + bonus focus pools. Also NOT a
    # world activity — links resolve at render time, so no item_links baking/reset.
    IRON_BANNER_SLUG: IRON_BANNER_SCHEMA,
    # Each destination registers under its own ``world_activity_<key>`` slug, so it
    # appears automatically at /rotation edit and gets its own DB row (no migration).
    **{
        rotation_slug(key): _build_legacy_rotation_schema(spec)
        for key, spec in LEGACY_DESTINATIONS.items()
    },
}

# The registered world-activity slugs, and a predicate over them. Editor/loader dispatch
# checks membership here rather than a ``startswith("legacy_")`` string prefix, so the
# discriminator is the registry itself, not a naming convention.
WORLD_ACTIVITY_SLUGS: frozenset[str] = frozenset(
    rotation_slug(key) for key in LEGACY_DESTINATIONS
)


def is_world_activity(post_type: str) -> bool:
    """Whether ``post_type`` is one of the world-activity rotation destinations."""
    return post_type in WORLD_ACTIVITY_SLUGS


_compiled_validators: dict[str, t.Callable[[t.Any], t.Any]] = {}

# json-editor uses ``format`` for widget selection (e.g. ``checkbox`` array pickers,
# ``tabs`` sector cards). fastjsonschema *raises* on a format it doesn't recognise
# (unlike the keywords it ignores), so register the UI-only ones as no-op validators —
# keeping the standard ``date``/``uri`` formats genuinely validated. New UI formats
# added to a schema must be listed here too.
_UI_ONLY_FORMATS: dict[str, t.Callable[[t.Any], bool]] = {
    "checkbox": lambda _value: True,
    "tabs": lambda _value: True,
}


def get_schema(post_type: str) -> dict[str, t.Any]:
    """Return the JSON Schema for ``post_type`` (raises ``KeyError`` if unknown)."""
    return ROTATION_SCHEMAS[post_type]


def validate(post_type: str, data: t.Any) -> None:
    """Validate ``data`` against ``post_type``'s schema.

    Raises :class:`fastjsonschema.JsonSchemaException` on invalid input and
    ``KeyError`` for an unknown post type. Compiled validators are cached per type.
    """
    validator = _compiled_validators.get(post_type)
    if validator is None:
        validator = fastjsonschema.compile(
            get_schema(post_type), formats=_UI_ONLY_FORMATS
        )
        _compiled_validators[post_type] = validator
    validator(data)
