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

"""The feed catalog: which followables exist, and what each one is called.

A *followable* is a feed anchor posts into a Kyber announcement channel and beacon
reads back — paging it through a navigator command, repeating it, and mirroring it out
to follower guilds. This module is the single declaration of that set.

**Why this is code and not a table.** Enumeration has to be *total in every process*:
anchor's settings page must render all twelve feeds, and anchor has producer modules
for only eight of them. An import-time registry (``dd.anchor.autopost.register_feed``)
is the right answer for *wiring* — which producer coro builds which post — because
wiring is naturally partial per process. It is the wrong answer for enumeration,
because the four beacon-only feeds would simply be missing from the very process that
needs the list. A static catalog is total by construction. It is also never refreshed:
this set changes only when someone writes a new extension module and redeploys, which
restarts the process and rebuilds it from scratch.

**What belongs here.** A fact earns a field if both processes need it, or if it is
currently written more than once. Everything else stays with its single consumer:

- cron schedules and producer/announcer coros — anchor wiring, one copy each, and
  holding the coros would force ``dd.common`` to import ``dd.anchor``;
- navigator wiring (period, reference date, history length, the ``NavPages``
  subclasses) — beacon wiring, heterogeneous, one copy each;
- the settings page's sub-toggle and image-URL rows — page copy for a few feeds.

``channel_key`` and ``has_toggle`` are properties rather than fields for the same
reason: derivable facts are not data. In particular ``channel_key`` is now the ONE
place the ``"<slug>_channel"`` convention lives — it used to exist only as a pattern
visible by eye across twelve hand-written pairs in
``dd.common.settings.FOLLOWABLE_SLUGS``.

This module imports nothing from ``dd`` (``dd.common.settings`` imports *it*), so there
is no cycle and no import-order constraint.
"""

import dataclasses
import enum
import typing as t


class FeedKind(enum.Enum):
    """Who produces a feed's posts — which decides the shape of its settings group.

    Only :attr:`ANCHOR_CRON` feeds have an ``enabled`` toggle, because only they have a
    scheduled producer to switch off; see :attr:`Followable.has_toggle`. The toggles are
    anchor's alone — beacon reads no setting to decide whether to mirror, and mirroring
    is unconditional (see ``dd.beacon.extensions.mirror``).
    """

    #: anchor produces on a schedule (``@aiocron.crontab`` in the producer module).
    ANCHOR_CRON = enum.auto()
    #: anchor produces from a web form (``HybridPostSpec``); a human presses publish, so
    #: there is no schedule to gate and no toggle.
    ANCHOR_FORM = enum.auto()
    #: content arrives in the channel some other way (a human, another bot). Beacon
    #: follows and mirrors it; anchor has no module for these at all.
    EXTERNAL = enum.auto()


@dataclasses.dataclass(frozen=True)
class Followable:
    """One feed's identity — everything about it that is not wiring."""

    #: The canonical id, and the join key across every store and surface: the
    #: ``AutoPostSettings`` toggle row name, ``dd.anchor.autopost.Feed.name``,
    #: ``HybridPostSpec.followable_key``, the ``/feed/{name}/…`` URL segment, and
    #: ``AutopostDailyStat.feed``.
    slug: str
    kind: FeedKind
    #: THE display name. Before this catalog the same feed was named four or five times
    #: — ``Xûr``/``Xur``, ``Ada-1``/``Ada``, ``Lost Sector``/``Lost sector`` — and most
    #: of them disagreed. Where they did, the settings page's label wins: it is the most
    #: deliberate copy, and the one an operator reads while configuring.
    display_name: str
    #: One-line description for the feed's headline settings row (its toggle row when it
    #: has one, otherwise its channel row).
    desc: str
    #: The ``/autopost`` subcommand, when it is not the slug. Only two feeds diverge,
    #: and the values are load-bearing: changing one re-registers a Discord command
    #: that users have muscle memory for.
    command_name: str | None = None
    #: What ``/autopost <command> ✓`` calls the feed in its confirmation, when the
    #: canonical name would read oddly under a command of a different name. Deliberately
    #: bounded to the two feeds above — see the invariant in ``tests/test_feeds.py``.
    follow_confirmation_name: str | None = None

    @property
    def channel_key(self) -> str:
        """This feed's ``AutoPostSettings.name`` row holding its channel id.

        The ``"_channel"`` suffix keeps a feed's on/off (``enabled``) and its where
        (``value``) independently readable and writable, which is why the channel is not
        just stored on the toggle row.
        """
        return f"{self.slug}_channel"

    @property
    def has_toggle(self) -> bool:
        """Whether this feed has an ``enabled`` produce toggle — see
        :class:`FeedKind`."""
        return self.kind is FeedKind.ANCHOR_CRON

    @property
    def effective_command_name(self) -> str:
        """The ``/autopost`` subcommand actually registered for this feed."""
        return self.command_name or self.slug

    @property
    def confirmation_name(self) -> str:
        """What the ``/autopost`` confirmation calls this feed."""
        return self.follow_confirmation_name or self.display_name


#: Every followable, in settings-page order: the six anchor-produced feeds first (each a
#: toggle group), then the six whose content originates elsewhere (each a single channel
#: row). Descriptions are the settings page's own copy.
FOLLOWABLES: tuple[Followable, ...] = (
    Followable(
        "lost_sector",
        FeedKind.ANCHOR_CRON,
        "Lost Sector",
        "Today's Lost Sector — location, champions, and shields.",
    ),
    Followable(
        "xur",
        FeedKind.ANCHOR_CRON,
        "Xûr",
        "Xûr's weekend location and inventory.",
    ),
    Followable(
        "eververse",
        FeedKind.ANCHOR_CRON,
        "Eververse",
        "This week's Eververse featured items and Bright Dust.",
    ),
    Followable(
        "ada",
        FeedKind.ANCHOR_CRON,
        "Ada-1",
        "Ada-1's weekly rotating shaders.",
    ),
    Followable(
        "portal_ops",
        FeedKind.ANCHOR_CRON,
        "Portal Ops",
        "Today's featured Portal Ops and their guaranteed rewards.",
    ),
    Followable(
        "iron_banner",
        FeedKind.ANCHOR_CRON,
        "Iron Banner",
        "Iron Banner weeks — dates, game modes, bonus focus pool, and guide link.",
    ),
    # TWAB/TWID: the command has been `/autopost twid` since the post was renamed "This
    # Week In Destiny", while the feed slug and the channel row kept the older name.
    Followable(
        "twab",
        FeedKind.EXTERNAL,
        "This Week At Bungie",
        "The Kyber channel TWAB posts follow from.",
        command_name="twid",
        follow_confirmation_name="TWID",
    ),
    Followable(
        "trials",
        FeedKind.ANCHOR_FORM,
        "Trials of Osiris",
        "The Kyber channel this feed posts to. Content is edited on the Trials form.",
    ),
    Followable(
        "weekly_reset",
        FeedKind.ANCHOR_FORM,
        "Weekly Reset",
        "The Kyber channel this feed posts to. Content is edited on the Weekly Reset "
        "form.",
    ),
    Followable(
        "weekly_nightfall",
        FeedKind.EXTERNAL,
        "Weekly Nightfall",
        "The Kyber channel weekly nightfall posts follow from.",
        command_name="nightfall",
        follow_confirmation_name="Nightfall",
    ),
    Followable(
        "free_games",
        FeedKind.EXTERNAL,
        "Free Games",
        "The Kyber channel free-games posts follow from.",
    ),
    Followable(
        "emblems_and_cosmetics",
        FeedKind.EXTERNAL,
        "Emblems & Cosmetics",
        "The Kyber channel emblems/cosmetics posts follow from.",
    ),
)

#: :data:`FOLLOWABLES` keyed by slug, for the lookup every consumer actually wants.
FEEDS: dict[str, Followable] = {f.slug: f for f in FOLLOWABLES}


def display_name(slug: str) -> str:
    """``slug``'s display name, or the slug itself if it names no known feed.

    The fallback matters for historical data: the mirror log resolves names for rows
    captured long ago, whose source channel may belong to a feed that no longer exists.
    """
    feed = FEEDS.get(slug)
    return feed.display_name if feed else slug


def slugs() -> t.KeysView[str]:
    """Every followable slug."""
    return FEEDS.keys()
