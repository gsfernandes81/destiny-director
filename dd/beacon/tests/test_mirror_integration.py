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

"""Live integration tests for the beacon mirror pipeline (durable ledger).

These drive the real ``message_*_repeater_impl`` enqueue handlers plus the convergence
worker over Discord's REST API against the dedicated ``dd-test-env`` guild — no gateway
connection is used. Each handler only *enqueues* into the ``mirror_delivery`` ledger;
the :func:`_converge` helper then runs the worker's pick → process → flush cycle to
completion (as the live worker loop would) and the assertions read the ledger's
converged state.

Isolation per run: Discord forbids bot tokens from creating guilds, so the harness
reuses the dedicated test guild (``cfg.test_env[0]``) and isolates runs by channel: it
prefix-sweeps any leftover ``test90931-*`` channels at setup, creates the channels each
test needs, and deletes them on teardown.

Opt-in: marked ``discord`` so the default suite (``make test``, ``-m "not discord"``)
never runs them; use ``make test-integration``. The bot token is reused from
``DISCORD_TOKEN_BEACON`` and the guild from ``TEST_ENV``.
"""

import asyncio as aio
import contextlib
import datetime as dt
import os
import typing as t
from collections.abc import AsyncIterator, Awaitable, Callable

import hikari as h
import pytest
import pytest_asyncio
from sqlalchemy import and_, select

from dd.beacon import (
    mirror_worker as mw,
    utils,
)
from dd.beacon.extensions import mirror
from dd.common import cfg, schemas, settings
from dd.common.bot import CachedFetchBot
from dd.common.components import build_container
from dd.common.schemas import DeliveryState, MirrorDelivery, MirroredChannel

# Reuse existing config — no dedicated test env vars. The beacon token is read with
# os.getenv so a missing token skips (rather than raising at import); the test guild is
# TEST_ENV's first id.
_TOKEN = os.getenv("DISCORD_TOKEN_BEACON")
_GUILD = cfg.test_env[0] if cfg.test_env else None

# Distinctive prefix so the per-run sweep can never match a real channel.
_PREFIX = "test90931-"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.discord,
    pytest.mark.asyncio(loop_scope="module"),
    pytest.mark.skipif(
        not (_TOKEN and _GUILD),
        reason="DISCORD_TOKEN_BEACON and TEST_ENV must be set for live mirror "
        "integration tests",
    ),
]


class _NullCache:
    """A cache whose every lookup misses, so ``fetch_*`` falls through to REST."""

    def get_guild_channel(self, _id: int) -> None:
        return None

    def get_message(self, _id: int) -> None:
        return None

    def get_guild(self, _id: int) -> None:
        return None

    def get_member(self, _guild_id: int, _user_id: int) -> None:
        return None  # forces confirm_dest_unsendable to fall back to rest.fetch_member

    def set_guild_channel(self, _channel: object) -> None:
        return None

    def set_message(self, _message: object) -> None:
        return None


class _RestBot:
    """The slice of the ``CachedFetchBot`` surface the mirror pipeline actually calls.

    Backed by a REST client with no gateway/cache, so every fetch hits Discord.
    """

    def __init__(self, rest: h.api.RESTClient, me: h.OwnUser) -> None:
        self.rest = rest
        self.cache = _NullCache()
        self._me = me

    def get_me(self) -> h.OwnUser:
        # GatewayBot.get_me() reads the gateway cache; a REST-only bot fetches it once
        # at startup instead. confirm_dest_unsendable (the reachability probe) needs the
        # bot's own id to resolve its member/permissions in a destination guild.
        return self._me

    async def fetch_channel(self, channel_id: int) -> h.PartialChannel:
        return await self.rest.fetch_channel(channel_id)

    async def fetch_message(
        self, channel: h.SnowflakeishOr[h.TextableChannel], message_id: int
    ) -> h.Message:
        return await self.rest.fetch_message(channel, message_id)


class _MirrorEnv(t.NamedTuple):
    rest: h.api.RESTClient
    bot: CachedFetchBot
    guild_id: int
    make_channel: Callable[[str], Awaitable[h.GuildTextChannel]]


async def _converge(bot: CachedFetchBot) -> None:
    """Run the worker's pick → process → flush cycle until no due rows remain.

    Stands in for the live worker loop: drives the module-singleton worker over the
    SQLite ledger until it drains. Bounded so a stuck row can't hang the suite.
    """
    mw.mirror_worker._bot = bot
    for _ in range(50):
        now = dt.datetime.now(tz=dt.UTC)
        batch = await MirrorDelivery.pick_batch(100, now=now)
        if not batch:
            break
        outcomes = await mw.mirror_worker._process(batch)
        await MirrorDelivery.flush_outcomes(outcomes)
        # Mirror the live loop's post-flush step so tests see real cache eviction.
        await mw.mirror_worker._evict_resolved_sources(batch)


async def _delivered(src_msg_id: int) -> list[tuple[int, int]]:
    """Converged ``(dest_msg_id, dest_ch_id)`` pairs for a source message."""
    async with schemas.db_session() as session, session.begin():
        rows = (
            await session.execute(
                select(MirrorDelivery.dest_msg_id, MirrorDelivery.dest_ch_id).where(
                    and_(
                        MirrorDelivery.src_msg_id == src_msg_id,
                        MirrorDelivery.state == DeliveryState.DELIVERED.value,
                        MirrorDelivery.dest_msg_id.is_not(None),
                    )
                )
            )
        ).fetchall()
    return [(int(m), int(c)) for m, c in rows]


async def _states(src_msg_id: int) -> dict[int, str]:
    """Per-dest ledger state for a source message."""
    async with schemas.db_session() as session, session.begin():
        rows = (
            await session.execute(
                select(MirrorDelivery.dest_ch_id, MirrorDelivery.state).where(
                    MirrorDelivery.src_msg_id == src_msg_id
                )
            )
        ).fetchall()
    return {int(d): s for d, s in rows}


async def _sweep_test_channels(rest: h.api.RESTClient, guild_id: int) -> None:
    """Delete every ``_PREFIX`` channel in the guild (leftovers from prior runs)."""
    for channel in await rest.fetch_guild_channels(guild_id):
        if channel.name and channel.name.startswith(_PREFIX):
            with contextlib.suppress(Exception):
                await rest.delete_channel(channel.id)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def mirror_env() -> AsyncIterator[_MirrorEnv]:
    """A live REST client + channel factory against the dedicated test guild."""
    assert _TOKEN and _GUILD  # guaranteed by the module skipif
    guild_id = _GUILD

    rest_app = h.RESTApp()
    await rest_app.start()
    created: list[int] = []
    try:
        async with rest_app.acquire(_TOKEN, token_type="Bot") as rest:
            await _sweep_test_channels(rest, guild_id)
            me = await rest.fetch_my_user()  # for the REST-only bot's get_me()

            async def make_channel(name: str) -> h.GuildTextChannel:
                channel = await rest.create_guild_text_channel(
                    guild_id, f"{_PREFIX}{name}"
                )
                created.append(channel.id)
                return channel

            try:
                yield _MirrorEnv(
                    rest=rest,
                    bot=t.cast(CachedFetchBot, _RestBot(rest, me)),
                    guild_id=guild_id,
                    make_channel=make_channel,
                )
            finally:
                for channel_id in created:
                    with contextlib.suppress(Exception):
                        await rest.delete_channel(channel_id)
    finally:
        await rest_app.close()


@pytest.fixture(autouse=True)
def _silence_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop the handlers spawning a drain watcher / posting to the log channel."""

    def _noop(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(mirror, "start_drain_watcher", _noop)


@pytest_asyncio.fixture(loop_scope="module", autouse=True)
async def _fresh_db() -> AsyncIterator[None]:
    """Reset the (SQLite, from conftest) schema between tests for hermetic rows.

    Also resets dd.common.settings' process-global cache — otherwise a setting written
    and cached by one test can still be served by a *_sync getter in a later test
    within the 30s TTL, even though the underlying row was just wiped above.
    """
    await schemas.destroy_all()
    await schemas.create_all()
    settings.reset_cache_for_tests()
    yield


async def _send(env: _MirrorEnv, src: h.GuildTextChannel, content: str) -> h.Message:
    """Post ``content`` in ``src``, enqueue the send, and converge the worker."""
    posted = await env.rest.create_message(src.id, content)
    src_channel = t.cast(h.TextableChannel, await env.rest.fetch_channel(src.id))
    await mirror.message_create_repeater_impl(
        posted, env.bot, src_channel, wait_for_crosspost=False
    )
    await _converge(env.bot)
    return posted


async def test_send_update_delete_lifecycle(mirror_env: _MirrorEnv) -> None:
    """A message mirrored to two dests follows the source through edit and delete."""
    src = await mirror_env.make_channel("src-life")
    dests = [await mirror_env.make_channel(f"dst-life{i}") for i in range(2)]
    for dest in dests:
        await MirroredChannel.add_mirror(
            src.id, dest.id, dest_server_id=mirror_env.guild_id, legacy=True
        )

    # SEND: every dest receives the message and a delivered ledger row is recorded.
    posted = await _send(mirror_env, src, "hello mirror")
    pairs = await _delivered(posted.id)
    assert {ch for _msg, ch in pairs} == {dest.id for dest in dests}
    for dest_msg_id, ch_id in pairs:
        mirrored = await mirror_env.rest.fetch_message(ch_id, dest_msg_id)
        assert mirrored.content == "hello mirror"

    # UPDATE: an edit at the source propagates to every dest.
    await mirror_env.rest.edit_message(src.id, posted.id, "hello edited")
    edited = await mirror_env.rest.fetch_message(src.id, posted.id)
    await mirror.message_update_repeater_impl(edited, mirror_env.bot)
    await _converge(mirror_env.bot)
    for dest_msg_id, ch_id in pairs:
        mirrored = await mirror_env.rest.fetch_message(ch_id, dest_msg_id)
        assert mirrored.content == "hello edited"

    # DELETE: deleting the source removes every mirrored message.
    await mirror.message_delete_repeater_impl(posted.id, None, mirror_env.bot)
    await _converge(mirror_env.bot)
    for dest_msg_id, ch_id in pairs:
        with pytest.raises(h.NotFoundError):
            await mirror_env.rest.fetch_message(ch_id, dest_msg_id)


async def test_failing_dest_is_disabled(
    mirror_env: _MirrorEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A destination that probes unreachable past the grace window is auto-disabled by
    the reachability sweep, while a healthy one keeps delivering."""
    await schemas.AutoPostSettings.set_enabled("disable_bad_channels", True)
    settings.reset_cache_for_tests()

    src = await mirror_env.make_channel("src-fail")
    good = await mirror_env.make_channel("dst-good")
    broken = await mirror_env.make_channel("dst-broken")
    for dest in (good, broken):
        await MirroredChannel.add_mirror(
            src.id, dest.id, dest_server_id=mirror_env.guild_id, legacy=True
        )

    # The healthy dest still gets the message.
    posted = await _send(mirror_env, src, "partial send")
    pairs = await _delivered(posted.id)
    assert good.id in {ch for _msg, ch in pairs}

    # "Break" the dest: delete the channel so a live perm probe confirms it gone.
    await mirror_env.rest.delete_channel(broken.id)

    # Run the reachability sweep's logic against the live bot: probe every candidate,
    # classify, then apply. The first sweep stamps the broken pair's clock; a later
    # sweep past the grace window disables it. The good pair never gets stamped.
    candidates = await MirroredChannel.fetch_reachability_candidates()
    reachable: list[tuple[int, int]] = []
    unreachable: list[tuple[int, int]] = []
    for pair in candidates:
        verdict = await utils.confirm_dest_unsendable(mirror_env.bot, pair[1])
        if verdict in (
            utils.DestVerdict.CONFIRMED_UNSENDABLE,
            utils.DestVerdict.CONFIRMED_GONE,
        ):
            unreachable.append(pair)
        elif verdict is utils.DestVerdict.SENDABLE:
            reachable.append(pair)
    assert (src.id, broken.id) in unreachable

    now1 = dt.datetime.now(tz=dt.UTC)
    await MirroredChannel.apply_reachability_sweep(reachable, unreachable, now=now1)
    now2 = now1 + dt.timedelta(hours=cfg.mirror_unreachable_grace_hours + 1)
    disabled = await MirroredChannel.apply_reachability_sweep(
        reachable, unreachable, now=now2
    )
    assert (src.id, broken.id) in disabled

    enabled_dests = await MirroredChannel.fetch_dests(src.id)
    assert good.id in enabled_dests
    assert broken.id not in enabled_dests


async def test_role_ping_is_appended(mirror_env: _MirrorEnv) -> None:
    """A dest configured with a role mention gets the spoilered ping suffix."""
    role_id = 123456789012345678
    src = await mirror_env.make_channel("src-ping")
    dest = await mirror_env.make_channel("dst-ping")
    await MirroredChannel.add_mirror(
        src.id,
        dest.id,
        dest_server_id=mirror_env.guild_id,
        legacy=True,
        role_mention_id=role_id,
    )

    posted = await _send(mirror_env, src, "ping me")
    pairs = await _delivered(posted.id)
    (dest_msg_id, ch_id) = pairs[0]
    mirrored = await mirror_env.rest.fetch_message(ch_id, dest_msg_id)
    assert mirrored.content == f"ping me\n\n||<@&{role_id}>||"


async def test_edit_reconciles_without_duplicate(mirror_env: _MirrorEnv) -> None:
    """An edit after a send reconciles every dest to the new content with no duplicate:
    each destination ends with exactly one message (the version guard re-edits the
    recorded dest message instead of re-sending)."""
    src = await mirror_env.make_channel("src-takeover")
    dests = [await mirror_env.make_channel(f"dst-takeover{i}") for i in range(3)]
    for dest in dests:
        await MirroredChannel.add_mirror(
            src.id, dest.id, dest_server_id=mirror_env.guild_id, legacy=True
        )

    posted = await _send(mirror_env, src, "before edit")

    await mirror_env.rest.edit_message(src.id, posted.id, "after edit")
    edited = await mirror_env.rest.fetch_message(src.id, posted.id)
    await mirror.message_update_repeater_impl(edited, mirror_env.bot)
    await _converge(mirror_env.bot)

    # Every dest ends with exactly one message carrying the edited content.
    for dest in dests:
        msgs = [m async for m in mirror_env.rest.fetch_messages(dest.id)]
        assert len(msgs) == 1, f"dest {dest.id} has {len(msgs)} messages (expected 1)"
        assert msgs[0].content == "after edit"

    pairs = await _delivered(posted.id)
    assert {ch for _msg, ch in pairs} == {dest.id for dest in dests}
    assert len(pairs) == len(dests)


def _cv2_text(message: h.Message) -> str:
    """Flatten a fetched CV2 message's text-display contents into one string."""
    return " ".join(
        child.content
        for component in message.components
        for child in getattr(component, "components", [])
        if hasattr(child, "content")
    )


async def test_cv2_message_is_mirrored(mirror_env: _MirrorEnv) -> None:
    """A Components V2 source message is rebuilt and mirrored; the dest receives a CV2
    message carrying the same text, and a source edit propagates."""
    src = await mirror_env.make_channel("src-cv2")
    dest = await mirror_env.make_channel("dst-cv2")
    await MirroredChannel.add_mirror(
        src.id, dest.id, dest_server_id=mirror_env.guild_id, legacy=True
    )

    posted = await mirror_env.rest.create_message(
        src.id,
        components=[build_container(["**CV2 header**", "line one\nline two"])],
        flags=h.MessageFlag.IS_COMPONENTS_V2,
    )
    src_channel = t.cast(h.TextableChannel, await mirror_env.rest.fetch_channel(src.id))
    await mirror.message_create_repeater_impl(
        posted, mirror_env.bot, src_channel, wait_for_crosspost=False
    )
    await _converge(mirror_env.bot)

    pairs = await _delivered(posted.id)
    assert {ch for _m, ch in pairs} == {dest.id}
    dest_msg_id, ch_id = pairs[0]
    mirrored = await mirror_env.rest.fetch_message(ch_id, dest_msg_id)
    assert h.MessageFlag.IS_COMPONENTS_V2 in mirrored.flags
    assert "CV2 header" in _cv2_text(mirrored)
    assert "line one" in _cv2_text(mirrored)

    # An edit to the CV2 source reconciles to the dest (CV2 edit branch).
    await mirror_env.rest.edit_message(
        src.id,
        posted.id,
        components=[build_container(["**CV2 header**", "edited body"])],
    )
    edited = await mirror_env.rest.fetch_message(src.id, posted.id)
    await mirror.message_update_repeater_impl(edited, mirror_env.bot)
    await _converge(mirror_env.bot)
    remirrored = await mirror_env.rest.fetch_message(ch_id, dest_msg_id)
    assert "edited body" in _cv2_text(remirrored)


async def test_source_cache_evicted_after_fanout(mirror_env: _MirrorEnv) -> None:
    """After a fan-out fully resolves the worker drops the source's cached content, so
    the per-source content cache does not grow under sustained load."""
    src = await mirror_env.make_channel("src-evict")
    dest = await mirror_env.make_channel("dst-evict")
    await MirroredChannel.add_mirror(
        src.id, dest.id, dest_server_id=mirror_env.guild_id, legacy=True
    )
    mw.mirror_worker._source_cache.clear()

    posted = await _send(mirror_env, src, "evict me")

    assert await _delivered(posted.id)  # delivered (so it *was* cached during the send)
    # Its fan-out has resolved, so the source content is no longer retained.
    assert posted.id not in mw.mirror_worker._source_cache
    assert await MirrorDelivery.sources_needing_source_content([posted.id]) == set()


async def test_graceful_stop_drains_without_duplicates(mirror_env: _MirrorEnv) -> None:
    """The real worker loop delivers, stop() drains the in-flight batch, and a restart
    does not re-send — the dest ends with exactly one copy."""
    src = await mirror_env.make_channel("src-drain")
    dest = await mirror_env.make_channel("dst-drain")
    await MirroredChannel.add_mirror(
        src.id, dest.id, dest_server_id=mirror_env.guild_id, legacy=True
    )

    posted = await mirror_env.rest.create_message(src.id, "drain me")
    src_channel = t.cast(h.TextableChannel, await mirror_env.rest.fetch_channel(src.id))
    await mirror.message_create_repeater_impl(
        posted, mirror_env.bot, src_channel, wait_for_crosspost=False
    )

    await mw.mirror_worker.start(mirror_env.bot)
    try:
        for _ in range(100):  # bounded wait for the loop to deliver
            if await _delivered(posted.id):
                break
            await aio.sleep(0.1)
        assert len(await _delivered(posted.id)) == 1
    finally:
        await mw.mirror_worker.stop()  # drain the loop

    msgs = [m async for m in mirror_env.rest.fetch_messages(dest.id)]
    assert len(msgs) == 1
    # A restart re-picks nothing outstanding, so it never re-sends a duplicate.
    await _converge(mirror_env.bot)
    msgs_after = [m async for m in mirror_env.rest.fetch_messages(dest.id)]
    assert len(msgs_after) == 1


async def test_partial_failure_delivers_good_and_terminalizes_bad(
    mirror_env: _MirrorEnv,
) -> None:
    """A fan-out with one reachable and one broken dest (the shape behind the prod
    failure alerts): the good dest gets the message and the broken one converges to
    FAILED, without the failure blocking the good delivery."""
    src = await mirror_env.make_channel("src-partial")
    good = await mirror_env.make_channel("dst-good")
    broken = await mirror_env.make_channel("dst-broken")
    for dest in (good, broken):
        await MirroredChannel.add_mirror(
            src.id, dest.id, dest_server_id=mirror_env.guild_id, legacy=True
        )
    # Break one dest so its send permanently fails (deleted channel → NotFound).
    await mirror_env.rest.delete_channel(broken.id)

    posted = await _send(mirror_env, src, "partial fan-out")

    delivered = {ch for _m, ch in await _delivered(posted.id)}
    assert good.id in delivered  # the reachable dest still got it
    assert broken.id not in delivered
    states = await _states(posted.id)
    assert states[good.id] == DeliveryState.DELIVERED.value
    assert states[broken.id] == DeliveryState.FAILED.value  # terminal, not stuck
    good_msgs = [m async for m in mirror_env.rest.fetch_messages(good.id)]
    assert len(good_msgs) == 1
