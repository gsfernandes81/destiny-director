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

"""``/distortion`` — which Destiny 2 destination is currently distorted.

Distortions make one destination "distorted" each hour, cycling through 7
destinations on a 7-hour loop. Bungie does not expose the active distortion as a
clean API field, so this is computed from a known cycle (no Bungie API / manifest
call), like ``/weekly reset`` and ``/source_code``. If Bungie ever realigns the
cycle, only ``REFERENCE_DATE`` (or the destination order) needs updating.

The response is rendered as a Discord Components V2 container (mirroring
``render_mirror_progress``) so it shares the bot's accent colour and styling.
"""

import datetime as dt

import hikari as h
import lightbulb as lb

from ...common.components import build_container

loader = lb.Loader()

# Distortion order (index 0->6), then repeats.
DISTORTION_DESTINATIONS: tuple[str, ...] = (
    "Cosmodrome",
    "European Dead Zone",
    "Dreaming City",
    "Savathûn's Throne World",
    "Moon",
    "Europa",
    "Nessus",
)

# Start of a Cosmodrome hour (index 0). Derived from the community tracker's sample
# (unix 1781446950 -> Nessus) re-expressed as a clean anchor; verified ref+0h ->
# Cosmodrome, ref+6h -> Nessus, ref+7h -> Cosmodrome, and 1781446950 -> Nessus.
# Source: https://github.com/MelecaZane/d2-distortions
REFERENCE_DATE = dt.datetime(2026, 6, 14, 8, tzinfo=dt.UTC)


def distortion_at(now: dt.datetime) -> tuple[str, str, dt.timedelta]:
    """Return ``(current_destination, next_destination, time_until_rotation)``.

    Integer-floor hourly indexing against ``REFERENCE_DATE`` (tz-aware UTC).
    """
    hours = int((now - REFERENCE_DATE).total_seconds()) // 3600
    idx = hours % len(DISTORTION_DESTINATIONS)
    next_idx = (idx + 1) % len(DISTORTION_DESTINATIONS)
    next_boundary = REFERENCE_DATE + dt.timedelta(hours=hours + 1)
    return (
        DISTORTION_DESTINATIONS[idx],
        DISTORTION_DESTINATIONS[next_idx],
        next_boundary - now,
    )


def rotation_schedule(now: dt.datetime) -> list[tuple[str, dt.datetime]]:
    """Full distortion cycle as ``(destination, becomes_distorted_at)`` pairs.

    The first entry is the currently-distorted destination (its time is the start of
    the current hour); the remaining six are the rest of the 7-hour loop in order.
    Used to render live ``<t:unix:R>`` Discord timestamps for each rotation.
    """
    hours = int((now - REFERENCE_DATE).total_seconds()) // 3600
    idx = hours % len(DISTORTION_DESTINATIONS)
    current_start = REFERENCE_DATE + dt.timedelta(hours=hours)
    return [
        (
            DISTORTION_DESTINATIONS[(idx + offset) % len(DISTORTION_DESTINATIONS)],
            current_start + dt.timedelta(hours=offset),
        )
        for offset in range(len(DISTORTION_DESTINATIONS))
    ]


def _format_countdown(td: dt.timedelta) -> str:
    """Format a positive timedelta as ``Hh Mm`` (or ``Mm`` under an hour)."""
    total_minutes = int(td.total_seconds()) // 60
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def render_distortion(now: dt.datetime) -> list[h.api.ComponentBuilder]:
    """Render the current distortion + upcoming rotation as a CV2 container.

    Built from scratch each call (like ``render_mirror_progress``). Countdowns use
    Discord's ``<t:unix:R>`` relative-timestamp markdown so they tick live client-side
    rather than freezing at command-invocation time.
    """
    (current, _current_start), *upcoming = rotation_schedule(now)
    next_at = upcoming[0][1]  # when the current destination rotates out

    upcoming_lines = "\n".join(
        f"{dest} — <t:{int(start.timestamp())}:R>" for dest, start in upcoming
    )
    sections = [
        "## 🌀 Distortions",
        f"### 📍 {current}\nDistorted now · rotates <t:{int(next_at.timestamp())}:R>",
        f"**Upcoming**\n{upcoming_lines}",
        "-# Rotation is computed from a known cycle; may drift if Bungie realigns it.",
    ]
    return [build_container(sections)]


class Distortion(
    lb.SlashCommand,
    name="distortion",
    description="See which Destiny 2 destination is currently distorted",
):
    @lb.invoke
    async def invoke(self, ctx: lb.Context):
        await ctx.respond(
            flags=h.MessageFlag.IS_COMPONENTS_V2,
            components=render_distortion(dt.datetime.now(dt.UTC)),
        )


loader.command(Distortion)  # global; no guilds= kwarg
