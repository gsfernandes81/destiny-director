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

"""Collectors that feed the anchor web stats dashboard (``/stats``).

This module no longer exposes any in-Discord ``/stats`` command — the leaderboard,
populations, server list and autopost-reach views all live on the web dashboard now.
What remains here are the two write paths that populate the tables it reads:

- ``track_command_usage`` — counts each user-facing slash-command invocation.
- ``snapshot_autopost_reach`` — daily snapshot of per-feed autopost reach.
"""

import datetime as dt
import logging

import hikari as h
import lightbulb as lb

from ...common import schemas, settings

loader = lb.Loader()

# Top-level command names that are NOT user-facing usage (owner/admin groups).
# Custom user-commands get admin-chosen top-level names that can't collide with
# these (registration rejects collisions), so they're tracked automatically.
_EXCLUDED_ROOTS = frozenset({"autopost", "stats", "mirror", "testing", "command"})


def _should_track(qualified_name: str, command_type: h.CommandType) -> bool:
    """Whether an invocation of this command should be counted.

    Only slash commands (not message/user context menus), and only those whose
    top-level group is not an owner/admin group.
    """
    if command_type is not h.CommandType.SLASH:
        return False
    return qualified_name.split(" ", 1)[0] not in _EXCLUDED_ROOTS


@lb.hook(lb.ExecutionSteps.PRE_INVOKE, skip_when_failed=True)
async def track_command_usage(_pl: lb.ExecutionPipeline, ctx: lb.Context) -> None:
    """Client-wide hook: count each user-facing slash-command invocation.

    Runs at PRE_INVOKE (after CHECKS pass, so owner-gate / permission rejections
    are not counted) and once per leaf-command pipeline. A stats-write failure must
    never break the user's command, so the DB write is swallowed.
    """
    data = ctx.command_data
    if not _should_track(data.qualified_name, data.type):
        return
    try:
        await schemas.CommandUsage.increment(data.qualified_name)
    except Exception:
        logging.getLogger(__name__).warning(
            "Failed to record command usage for %s",
            data.qualified_name,
            exc_info=True,
        )


async def _snapshot_autopost_reach(followables: dict[str, int] | None = None) -> None:
    """Snapshot today's active autopost reach into ``AutopostDailyStat``.

    Counts enabled destinations per feed, split into "follow" (native Discord
    channel-follows, i.e. non-legacy) and "mirror" (legacy mirrored channels). The write
    is an idempotent overwrite, so running this more than once a day — including at
    every boot — simply refreshes today's value rather than double-counting.
    """
    followables = (
        await settings.get_followables() if followables is None else followables
    )
    today = dt.datetime.now(tz=dt.UTC).date()
    for feed, src_id in followables.items():
        follows = await schemas.MirroredChannel.count_dests(src_id, legacy_only=False)
        mirrors = await schemas.MirroredChannel.count_dests(src_id, legacy_only=True)
        await schemas.AutopostDailyStat.record(today, feed, "follow", follows)
        await schemas.AutopostDailyStat.record(today, feed, "mirror", mirrors)


@loader.task(lb.uniformtrigger(hours=24, wait_first=False), max_failures=-1)
async def snapshot_autopost_reach() -> None:
    # Runs daily and once at boot (wait_first=False) so a redeploy always writes the
    # current day's snapshot; the idempotent upsert makes repeated runs safe.
    await _snapshot_autopost_reach()
