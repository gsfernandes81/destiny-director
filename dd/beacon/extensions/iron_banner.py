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

import lightbulb as lb

from ...common import settings
from .autoposts import follow_control_command_maker

loader = lb.Loader()

FOLLOWABLE_CHANNEL = settings.get_followable_channel_sync("iron_banner")

follow_control_command_maker(
    FOLLOWABLE_CHANNEL, "iron_banner", "Iron Banner", "Iron Banner auto posts"
)
