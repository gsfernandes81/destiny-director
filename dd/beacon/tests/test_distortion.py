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

"""Pure-logic tests for the hourly Distortion rotation (no Discord/DB/clock dep)."""

import datetime as dt

import hikari as h

from dd.beacon.extensions.distortion import (
    DISTORTION_DESTINATIONS,
    REFERENCE_DATE,
    _format_countdown,
    distortion_at,
    render_distortion,
    rotation_schedule,
)
from dd.common import settings


def test_reference_date_is_cosmodrome():
    current, upcoming, until = distortion_at(REFERENCE_DATE)
    assert current == "Cosmodrome"
    assert upcoming == "European Dead Zone"
    assert until == dt.timedelta(hours=1)


def test_six_hours_later_is_nessus():
    current, upcoming, until = distortion_at(REFERENCE_DATE + dt.timedelta(hours=6))
    assert current == "Nessus"
    assert upcoming == "Cosmodrome"
    assert until == dt.timedelta(hours=1)


def test_seven_hours_wraps_back_to_cosmodrome():
    current, upcoming, _ = distortion_at(REFERENCE_DATE + dt.timedelta(hours=7))
    assert current == "Cosmodrome"
    assert upcoming == "European Dead Zone"


def test_mid_hour_countdown():
    now = REFERENCE_DATE + dt.timedelta(hours=6, minutes=30)
    current, upcoming, until = distortion_at(now)
    assert current == "Nessus"
    assert upcoming == "Cosmodrome"
    assert until == dt.timedelta(minutes=30)


def test_community_tracker_sample():
    # unix 1781446950 is a known Nessus hour on the community tracker.
    now = dt.datetime.fromtimestamp(1781446950, dt.UTC)
    current, _, until = distortion_at(now)
    assert current == "Nessus"
    assert until == dt.timedelta(minutes=37, seconds=30)


def test_format_countdown():
    assert _format_countdown(dt.timedelta(minutes=39)) == "39m"
    assert _format_countdown(dt.timedelta(hours=1, minutes=5)) == "1h 5m"
    assert _format_countdown(dt.timedelta(hours=1)) == "1h 0m"


def test_rotation_schedule_full_cycle():
    schedule = rotation_schedule(REFERENCE_DATE)
    # Every destination appears once, in cycle order starting at the current one.
    assert [dest for dest, _ in schedule] == list(DISTORTION_DESTINATIONS)
    assert len(schedule) == len(DISTORTION_DESTINATIONS)
    # First entry is the current hour's start; entries are 1h apart.
    assert schedule[0][1] == REFERENCE_DATE
    assert schedule[1][1] == REFERENCE_DATE + dt.timedelta(hours=1)
    assert schedule[6][1] == REFERENCE_DATE + dt.timedelta(hours=6)


def test_rotation_schedule_rotates_with_current_index():
    # Six hours in, Nessus is current so the cycle should start at Nessus.
    schedule = rotation_schedule(REFERENCE_DATE + dt.timedelta(hours=6))
    assert schedule[0][0] == "Nessus"
    assert schedule[1][0] == "Cosmodrome"


def test_render_distortion_returns_accent_coloured_container():
    components = render_distortion(REFERENCE_DATE)
    assert len(components) == 1
    container = components[0]
    assert isinstance(container, h.impl.ContainerComponentBuilder)
    assert container.accent_color == settings.get_embed_default_color_sync()
