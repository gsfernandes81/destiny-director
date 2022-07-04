# Copyright © 2019-present gsfernandes81

# This file is part of "mortal-polarity".

# mortal-polarity is free software: you can redistribute it and/or modify it under the
# terms of the GNU Affero General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later version.

# "mortal-polarity" is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
# PARTICULAR PURPOSE. See the GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License along with
# mortal-polarity. If not, see <https://www.gnu.org/licenses/>.

import aiohttp
import hikari
import lightbulb
from pytz import utc
from sector_accounting import Rotation
from sqlalchemy.sql.expression import select
from sqlalchemy.sql.schema import Column
from sqlalchemy.sql.sqltypes import String

from polarity.autoannounce import LostSectorPostSettings

from . import cfg
from .utils import db_session


@lightbulb.add_checks(lightbulb.checks.has_roles(cfg.admin_role))
@lightbulb.command(
    "kyber",
    "Commands for Kyber",
    guilds=[
        cfg.kyber_discord_server_id,
    ],
)
@lightbulb.implements(lightbulb.SlashCommandGroup)
async def kyber():
    # Enables or disables lost sector announcements globally
    pass


@kyber.child
@lightbulb.option(
    "option",
    "Enable or disable",
    type=str,
    choices=["Enabled", "Disabled"],
    required=True,
)
@lightbulb.command(
    "ls_announcements",
    "Enable or disable all automatic lost sector announcements",
    auto_defer=True,
    # The following NEEDS to be included in all privledged commands
    inherit_checks=True,
)
@lightbulb.implements(lightbulb.SlashSubCommand)
async def announcements(ctx: lightbulb.Context):
    option = True if ctx.options.option == "Enabled" else False
    async with db_session() as session:
        async with session.begin():
            settings = await session.get(LostSectorPostSettings, 0)
            if settings is None:
                settings = LostSectorPostSettings(0, option)
                session.add(settings)
            else:
                settings.autoannounce_enabled = option
    await ctx.respond(
        "Lost sector announcements {}".format("Enabled" if option else "Disabled")
    )


def register_all(bot: lightbulb.BotApp) -> None:
    bot.command(kyber)
