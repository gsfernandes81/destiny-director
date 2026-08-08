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

"""Beacon's bot-administration command group (stop / info).

**Only beacon uses this now.** Anchor's ``/anchor stop | info`` moved to its web
control panel (``/bot/info``, ``/bot/stop``) on 2026-08-05; beacon has no web surface to
move to, so it keeps the commands. The factory keeps its ``bot_name`` parameter rather
than hardcoding "beacon" — it costs nothing, and the anchor wrapper is one file away if
that ever reverses.

Factory mirroring ``make_source_command``: each call builds a *fresh* group. Lightbulb
command objects carry per-client registration state, so a fresh group is built per call
rather than shared across clients. The factory applies ``owner_only`` to each subcommand
itself rather than relying on a client-wide gate (beacon has none). The wrapper scopes
registration to the control guild.

Beacon passes a ``mirror_check`` so ``stop`` warns and requires a DANGER override while
mirror operations are in progress. Termination goes through
:mod:`dd.common.lifecycle` (schedule ``close`` + exit on the main thread) so it works
from a button callback too: a raw ``sys.exit`` in a component callback is swallowed by
hikari's fire-and-forget task wrapper.

``stop`` exits cleanly (code 0) and only stops a service whose restart policy is not
``ALWAYS``. Every Railway service is on the ``ON_FAILURE`` default (prod beacon was
flipped from ``ALWAYS`` on 2026-06-25; re-verified against Railway 2026-08-04, no
service carries an ``ALWAYS`` override and none is set in ``railway.toml``), so
``/beacon stop`` works everywhere.

There is deliberately **no ``restart``** (removed 2026-08-04). It worked by exiting
non-zero so Railway would bring the process back, which made it unusable in prod — a
non-zero exit is a crash there, counted against the service's max-retry ceiling (anchor
sets 7 explicitly, beacon takes the default 10), after which the service stays down. It
was therefore gated off in prod and existed for dev only, where every invocation still
burned one retry from that same budget. Restart by redeploying from Railway instead.
"""

import asyncio
import contextlib
import typing as t

import hikari as h
import lightbulb as lb
from lightbulb import components as lbc

from . import cfg, lifecycle, settings
from .auth import owner_only
from .bot import CachedFetchBot
from .components import (
    CV2_DANGER_COLOR,
    CV2_NEUTRAL_COLOR,
    CV2_WARNING_COLOR,
    build_container,
    cv2_notice,
    respond_cv2,
)
from .schemas import MirroredChannel


async def _run_lifecycle(
    ctx: lb.Context,
    bot: CachedFetchBot,
    *,
    exit_code: int,
    action: str,
    verb: str,
    mirror_check: t.Callable[[], t.Awaitable[int]] | None,
) -> None:
    """Shut the bot down; warn + require a DANGER override if mirrors are live."""
    n = await mirror_check() if mirror_check is not None else 0
    if n == 0:
        await respond_cv2(ctx, cv2_notice(f"Bot is {action} now."), ephemeral=True)
        await lifecycle.request_shutdown(bot, exit_code)
        return

    # Mirror-in-progress override flow (beacon only — anchor passes mirror_check=None,
    # so n is always 0 above). Left as an embed + menu pending the deferred beacon CV2
    # pass; converting an interactive embed+menu to CV2 is out of scope here. Its accent
    # colours now come from the shared CV2_* constants so there's one palette repo-wide.
    decided = False

    async def on_confirm(mctx: lbc.MenuContext) -> None:
        nonlocal decided
        if mctx.user.id not in await bot.fetch_owner_ids():
            await mctx.respond("You are not authorized.", ephemeral=True)
            return
        decided = True
        await mctx.respond(
            edit=True,
            embed=h.Embed(description=f"Bot is {action} now.", color=CV2_DANGER_COLOR),
            components=[],
        )
        await lifecycle.request_shutdown(bot, exit_code)
        mctx.stop_interacting()

    async def on_cancel(mctx: lbc.MenuContext) -> None:
        nonlocal decided
        if mctx.user.id not in await bot.fetch_owner_ids():
            await mctx.respond("You are not authorized.", ephemeral=True)
            return
        decided = True
        await mctx.respond(
            edit=True,
            embed=h.Embed(
                description="Aborted — no action taken.", color=CV2_NEUTRAL_COLOR
            ),
            components=[],
        )
        mctx.stop_interacting()

    menu = lbc.Menu()
    menu.add_interactive_button(
        h.ButtonStyle.DANGER,
        on_confirm,
        custom_id=f"dd_lifecycle_go:{ctx.interaction.id}",
        label=f"{verb} now",
    )
    menu.add_interactive_button(
        h.ButtonStyle.SECONDARY,
        on_cancel,
        custom_id=f"dd_lifecycle_no:{ctx.interaction.id}",
        label="Cancel",
    )

    await ctx.respond(
        embed=h.Embed(
            title="⚠️ Mirrors in progress",
            description=(
                f"{n} mirror operation(s) are still running. {action.capitalize()} now "
                "will interrupt them — already-sent destinations are recorded and the "
                "rest reconcile on the next run. Wait for them to finish, or override."
            ),
            color=CV2_WARNING_COLOR,
        ),
        components=menu,
        ephemeral=True,
    )

    with contextlib.suppress(TimeoutError):
        await menu.attach(ctx.client, timeout=60)
    if not decided:
        # Timed out without a choice — disable the (now stale) buttons.
        await ctx.interaction.edit_initial_response(
            embed=h.Embed(
                description="Timed out — no action taken.", color=CV2_NEUTRAL_COLOR
            ),
            components=[],
        )


def make_controller_group(
    bot_name: str,
    *,
    mirror_check: t.Callable[[], t.Awaitable[int]] | None = None,
) -> lb.Group:
    """Build a fresh bot-administration group named after ``bot_name``.

    Args:
        bot_name: The group name / top-level command, e.g. ``"beacon"`` (yields
            ``/beacon stop`` etc.).
        mirror_check: Optional callable returning the number of in-progress mirror
            operations. When it returns > 0, ``stop`` warns and requires a DANGER
            override. It also gates ``info``'s mirror-status block (present iff set).
    """
    group = lb.Group(bot_name, "Bot administration")

    @group.register
    class Stop(
        lb.SlashCommand,
        name="stop",
        description="Shut down the bot",
        hooks=[owner_only],
    ):
        @lb.invoke
        async def invoke(self, ctx: lb.Context, bot: CachedFetchBot = lb.di.INJECTED):
            await _run_lifecycle(
                ctx,
                bot,
                exit_code=lifecycle.STOP_EXIT_CODE,
                action="shutting down",
                verb="Shut down",
                mirror_check=mirror_check,
            )

    @group.register
    class Info(
        lb.SlashCommand,
        name="info",
        description="Configuration state info",
        hooks=[owner_only],
    ):
        @lb.invoke
        async def invoke(self, ctx: lb.Context):
            lines = [
                f"**Configuration Info — {bot_name}**",
                f"- Control Discord Server ID: {cfg.control_discord_server_id}",
                f"- Test Environment: {cfg.test_env}",
            ]

            if mirror_check is not None:
                lines.append("\n**Mirror status**")
                lines.append(f"- Operations in progress: {await mirror_check()}")
                followed = [
                    (n, c) for n, c in (await settings.get_followables()).items() if c
                ]
                try:
                    counts = await asyncio.gather(
                        *(
                            MirroredChannel.count_dests(c, legacy_only=None)
                            for _, c in followed
                        )
                    )
                except Exception:
                    # A DB blip shouldn't sink the whole diagnostic — the in-memory
                    # mirror_check() count above already rendered.
                    lines.append("- *(mirror counts unavailable)*")
                else:
                    for (name, _), n in zip(followed, counts, strict=True):
                        lines.append(f"- `{name}` → {n} mirror dest(s)")

            await respond_cv2(ctx, build_container(["\n".join(lines)]))

    return group
