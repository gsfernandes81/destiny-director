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

"""The feed catalog's pins and invariants.

Two of these are *pins*: they hold a derived value against a literal copied in here, so
that deriving it cannot silently change it. Both guard something that would fail far
from its cause — a renamed DB row would read as "unconfigured" on a live deploy, and a
renamed Discord command would re-register and lose whatever muscle memory users have.
"""

from dd.common import feeds

# The twelve AutoPostSettings row names as they exist in every deployed database, copied
# here as a literal ON PURPOSE. The catalog derives them from `f"{slug}_channel"`; this
# is the independent statement of what that derivation must produce. A change here is a
# data migration, not a refactor.
_CHANNEL_KEYS = {
    "lost_sector": "lost_sector_channel",
    "xur": "xur_channel",
    "eververse": "eververse_channel",
    "ada": "ada_channel",
    "portal_ops": "portal_ops_channel",
    "iron_banner": "iron_banner_channel",
    "twab": "twab_channel",
    "trials": "trials_channel",
    "weekly_reset": "weekly_reset_channel",
    "weekly_nightfall": "weekly_nightfall_channel",
    "free_games": "free_games_channel",
    "emblems_and_cosmetics": "emblems_and_cosmetics_channel",
}

# The `/autopost <name>` subcommands as registered with Discord today. Two do not match
# their feed slug (`twab` → twid, `weekly_nightfall` → nightfall) and that is the whole
# reason `command_name` exists.
_COMMAND_NAMES = {
    "lost_sector": "lost_sector",
    "xur": "xur",
    "eververse": "eververse",
    "ada": "ada",
    "portal_ops": "portal_ops",
    "iron_banner": "iron_banner",
    "twab": "twid",
    "trials": "trials",
    "weekly_reset": "weekly_reset",
    "weekly_nightfall": "nightfall",
    "free_games": "free_games",
    "emblems_and_cosmetics": "emblems_and_cosmetics",
}


def test_channel_keys_match_the_rows_that_exist_in_the_database() -> None:
    assert {f.slug: f.channel_key for f in feeds.FOLLOWABLES} == _CHANNEL_KEYS


def test_command_names_match_what_is_registered_with_discord() -> None:
    assert {
        f.slug: f.effective_command_name for f in feeds.FOLLOWABLES
    } == _COMMAND_NAMES


def test_command_names_are_unique() -> None:
    # They become sibling subcommands of one `/autopost` group, so a collision would be
    # a registration error at boot rather than anything this catalog could catch later.
    names = [f.effective_command_name for f in feeds.FOLLOWABLES]
    assert len(set(names)) == len(names)


def test_slugs_are_unique_and_keyed_consistently() -> None:
    assert len(feeds.FEEDS) == len(feeds.FOLLOWABLES)
    assert all(slug == feed.slug for slug, feed in feeds.FEEDS.items())


def test_a_confirmation_override_exists_only_where_the_command_diverges() -> None:
    """The override is a bounded exception, not a second name field.

    It exists because `/autopost twid` confirming "This Week At Bungie" reads as a
    different feed than the one you typed. A feed whose command IS its slug has no such
    problem, so an override there would just be a display name competing with
    `display_name` — exactly the drift this catalog removes.
    """
    for feed in feeds.FOLLOWABLES:
        if feed.follow_confirmation_name is not None:
            assert feed.command_name is not None, feed.slug


def test_only_cron_feeds_have_a_produce_toggle() -> None:
    # A toggle switches a *schedule* off. Anything unscheduled is published by a human
    # pressing a button or arrives in the channel by other means, so it has none.
    for feed in feeds.FOLLOWABLES:
        assert feed.has_toggle is (feed.kind is feeds.FeedKind.ANCHOR_CRON)


def test_every_feed_has_display_copy() -> None:
    for feed in feeds.FOLLOWABLES:
        assert feed.display_name.strip()
        assert feed.desc.strip()
