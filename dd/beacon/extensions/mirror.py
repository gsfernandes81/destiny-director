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

"""Mirror subsystem gateway surface: thin enqueue handlers, the drain watcher, admin
commands, and the reachability/auto-disable sweep, layered over the ``mirror_delivery``
ledger.

The Discord fan-out itself lives in :mod:`dd.beacon.mirror_worker`. This module's
listeners do one transactional enqueue each (send/edit/delete) and start a lightweight
drain watcher that polls the ledger until the run drains, then escalates any failures
and posts one never-edited result line. The live, per-destination view of a run lives
on the anchor web mirror-log page (``/mirror-logs``) — there is no editable Discord
card. A separate low-load task sweeps destination reachability + send perms and disables
mirrors that stay unreachable past a grace window — the delivery hot path does no
perm-probing.
"""

import asyncio as aio
import collections.abc
import contextlib
import datetime as dt
import logging
import math
import typing as t
from random import randint
from time import perf_counter

import dateparser
import hikari as h
import lightbulb as lb
import regex as re

from ...common import cfg, settings
from ...common.auth import owner_only
from ...common.bot import CachedFetchBot
from ...common.emoji_store import AppEmojiStore
from ...common.schemas import (
    MirrorDelivery,
    MirroredChannel,
    MirrorMessageVersion,
    MirrorOperationLog,
    ServerStatistics,
)
from ...common.utils import (
    ErrorClass,
    classify_error,
    discord_error_logger,
    format_duration,
    guild_scope,
    parse_channel_ref,
)
from .. import utils
from ..mirror_core import MirrorOperationType, RunCounts, RunView
from ..mirror_worker import mirror_worker

loader = lb.Loader()

re_markdown_link = re.compile(r"\[(.*?)\]\(.*?\)")


# Bounded transient-retry budget for a gateway handler's single ledger write.
_HANDLER_DB_MAX_TRIES = 5


def _get_message_summary(msg: h.Message, default: str = "Link") -> str:
    # Prefer the first line of the message content; fall back to the first embed
    # title/description when the message has no text content.
    summary = ""
    if msg.content:
        summary = msg.content.split("\n")[0]

    if not summary:
        for embed in msg.embeds:
            if embed.title:
                summary = embed.title
                break
            if embed.description:
                summary = embed.description.split("\n")[0]
                break

    if not summary:
        return default

    summary = summary.replace("*", "")
    summary = summary.replace("_", "")
    summary = summary.replace("#", "")
    summary = summary.strip("{}")
    summary = summary.strip("<>")
    summary = summary.capitalize()

    # Use re_markdown_link to remove links replacing
    # them with just the text unless the text is empty
    summary = re_markdown_link.sub(r"\1", summary) or summary

    return summary


# Strong reference to the one-shot post-restart backlog-recovery task (same weak-ref
# hazard as ``_watchers``); kept alive for the task's lifetime.
_backlog_recovery_task: aio.Task[None] | None = None


# Logger whose records surface to the Discord alerts channel (ERROR/CRITICAL) via the
# root DiscordLogHandler, used for mirror-health escalations.
health_logger = logging.getLogger("dd.beacon.mirror.health")

# Escalate a reachability-sweep disable by its blast radius (count of mirrors disabled):
# critical past the first threshold, error past the second, else just a console warning.
_DISABLE_CRITICAL_MIN = 10
_DISABLE_ERROR_MIN = 5

# Escalate a completed run's failures by blast radius so a broad outage pages the owner
# (health_logger only reaches the Discord alerts channel at ERROR/CRITICAL). Any failed
# target is an ERROR; it becomes CRITICAL once the failure count reaches whichever is
# LARGER of a flat floor (10) or half the run's targets — i.e. a majority-scale failure
# of a big fan-out, or ≥10 of a small one, pages; anything less only errors.
_RUN_FAIL_CRITICAL_MIN = 10
_RUN_FAIL_CRITICAL_RATIO = 0.50

# Idempotent failure alerting. A source message can be converged by more than one run
# over its lifetime (the initial send, then an edit that re-arms every row — the failed
# ones included — and re-attempts them), and each run's card reads the *same* per-source
# ledger state, so without this every run re-pages the same failures. Key the last-paged
# blast radius per src_msg_id; a run whose (failed, total) matches the last page for
# that source is not re-paged. A clean run (0 failed) clears the entry, so a later
# *recurrence* of failures pages again. Bounded (FIFO-evicted) so it can't grow; a
# source only re-pages spuriously after eviction (needs _DEDUP_CAP others between).
_DEDUP_CAP = 2048
_last_alerted_failure: dict[int, tuple[int, int]] = {}


def _log_run_summary(view: RunView) -> None:
    """Log a one-line summary of a completed run; escalate to the alerts channel (ERROR,
    or CRITICAL past the blast-radius threshold) the *first* time a given failure set is
    seen for a source (see the dedup note above)."""
    counts = view.counts
    elapsed = perf_counter() - view.start_time
    logging.info(
        "Mirror %s for source %s done in %s — %d ok, %d failed, %d cancelled / %d "
        "targets (%.1f ch/s)",
        view.op.name.lower(),
        view.src_ch_id,
        format_duration(elapsed),
        counts.delivered,
        counts.failed,
        counts.cancelled,
        counts.total,
        counts.throughput_resolved / elapsed if elapsed else 0.0,
    )
    if not counts.failed:
        # Fully resolved — reset the dedup so a later recurrence of failures re-pages.
        _last_alerted_failure.pop(view.src_msg_id, None)
        return

    signature = (counts.failed, counts.total)
    if _last_alerted_failure.get(view.src_msg_id) == signature:
        # Same blast radius already paged for this source (e.g. an edit re-ran the same
        # failed targets). The console info line above still records this run.
        return
    if (
        view.src_msg_id not in _last_alerted_failure
        and len(_last_alerted_failure) >= _DEDUP_CAP
    ):
        _last_alerted_failure.pop(next(iter(_last_alerted_failure)))
    _last_alerted_failure[view.src_msg_id] = signature

    critical_at = max(
        _RUN_FAIL_CRITICAL_MIN,
        math.ceil(_RUN_FAIL_CRITICAL_RATIO * counts.total),
    )
    level = logging.CRITICAL if counts.failed >= critical_at else logging.ERROR
    health_logger.log(
        level,
        "Mirror %s for source %s finished with %d/%d target(s) failed.",
        view.op.name.lower(),
        view.src_ch_id,
        counts.failed,
        counts.total,
    )


# Ledger op enum → the web log's op-type string (send is a "create" to the reader).
_OP_TYPE = {
    MirrorOperationType.SEND: "create",
    MirrorOperationType.UPDATE: "update",
    MirrorOperationType.DELETE: "delete",
}


async def _record_operation(view: RunView) -> None:
    """Append this completed operation's own final counts to the operation log.

    The web mirror-log reads *settled* operations from here (the ledger only holds the
    latest converged state), so each create/update/delete keeps its own numbers. This is
    the durable form of the one-line summary :func:`_log_run_summary` already logs.
    Best-effort — a write failure never affects the run or its summary."""
    try:
        counts = view.counts
        elapsed = max(0.0, perf_counter() - view.start_time)
        finished = dt.datetime.now(dt.UTC).replace(
            tzinfo=None
        )  # naive-UTC, like ledger
        started = finished - dt.timedelta(seconds=elapsed)
        version, attempts = await MirrorDelivery.op_meta(view.src_msg_id)
        refs = (
            [
                {"ref": ref, "error_class": cls_, "count": count, "sample": sample}
                for (
                    ref,
                    cls_,
                    count,
                    sample,
                ) in await MirrorDelivery.failure_breakdown(view.src_msg_id)
            ]
            if counts.failed
            else None
        )
        await MirrorOperationLog.record(
            src_msg_id=view.src_msg_id,
            src_ch_id=view.src_ch_id,
            op_type=_OP_TYPE.get(view.op, view.op.name.lower()),
            version=version,
            started_at=started,
            finished_at=finished,
            total=counts.total,
            delivered=counts.delivered,
            failed=counts.failed,
            cancelled=counts.cancelled,
            attempts=attempts,
            failure_refs=refs,
        )
    except Exception:
        logging.exception(
            "Failed to record mirror operation-log row for %s", view.src_msg_id
        )


# --- drain watcher (card-free run completion) --------------------------------
#
# The web mirror-log page (dd.anchor /mirror-logs) is now the live view of a run, so the
# beacon no longer posts an editable Discord "progress card". Instead one lightweight
# watcher per source polls the ledger until the run drains, then (a) runs the summary /
# failure escalation and (b) posts a single, never-edited result line to the log
# channel. Because nothing is a live-edited message, there is no supersede/cancel/freeze
# surface — a second event for the same source coalesces into the running watcher.

# One live watcher per source message (strong ref: asyncio holds tasks weakly).
_watchers: dict[int, aio.Task[None]] = {}

# How often the watcher re-reads the ledger while a run is in flight.
_WATCHER_POLL_INTERVAL = 5
# Hard cap so a run that somehow never drains can't leave a task polling forever.
_WATCHER_MAX_LIFETIME = 7 * 60 * 60


def start_drain_watcher(
    bot: CachedFetchBot,
    view: RunView,
    *,
    source_message: h.Message | None = None,
) -> None:
    """Ensure a single drain watcher runs for this source (coalescing).

    One watcher per ``src_msg_id``. A second event while a watcher is still live is a
    no-op: the running watcher polls the ledger and will observe the rows the caller
    just wrote (enqueue/bump/delete all happen before this call). A *finished* watcher
    is replaced. The check-and-register runs with no ``await`` in between, so two
    near-simultaneous starts can't both spawn.
    """
    existing = _watchers.get(view.src_msg_id)
    if existing is not None and not existing.done():
        return
    task = aio.create_task(_run_drain_watcher(bot, view, source_message=source_message))
    _watchers[view.src_msg_id] = task
    task.add_done_callback(
        lambda done, sid=view.src_msg_id: (
            _watchers.pop(sid, None) if _watchers.get(sid) is done else None
        )
    )


async def _run_drain_watcher(
    bot: CachedFetchBot,
    view: RunView,
    *,
    source_message: h.Message | None = None,
) -> None:
    """Poll the ledger until the run drains, then summarise + post one result line.

    Progress is read straight from ``state_counts`` (the single source of truth), so a
    run enlarged by a mid-flight edit (re-armed PENDING rows) simply keeps the watcher
    open until it re-drains — the same "last drain wins" summary the progress card had.
    A run is complete once it has rows and none are still PENDING; a lifetime cap stops
    a stuck run from polling forever (no summary in that case, matching the old card).
    """
    started = perf_counter()
    while True:
        with contextlib.suppress(Exception):
            view.counts = RunCounts.from_state_counts(
                await MirrorDelivery.state_counts(view.src_msg_id)
            )
        complete = view.counts.total > 0 and view.counts.is_complete
        if complete or (perf_counter() - started > _WATCHER_MAX_LIFETIME):
            if complete:
                # Failure escalation + dedup (unchanged), the durable op-log row, then
                # the visible result line.
                _log_run_summary(view)
                await _record_operation(view)
                await _post_run_summary_line(bot, view, source_message)
            return
        await aio.sleep(_WATCHER_POLL_INTERVAL)


async def _post_run_summary_line(
    bot: CachedFetchBot,
    view: RunView,
    source_message: h.Message | None,
) -> None:
    """Post one never-edited result line for a drained run to the log channel.

    Best-effort and strictly cosmetic: it runs *after* ``_log_run_summary`` so a failed
    send can never swallow the failure escalation. Detail lives on the web mirror-log
    page (``/mirror-logs``); this line is the at-a-glance Discord confirmation.
    """
    log_channel_id = await settings.get_log_channel_id()
    if not log_channel_id:
        # No log channel configured (Autopost Settings) — inert by design, not by the
        # suppress(Exception) below happening to swallow a fetch_channel(0) error.
        return

    counts = view.counts
    label = (
        _get_message_summary(source_message)
        if source_message is not None
        else str(view.src_msg_id)
    )
    elapsed = format_duration(perf_counter() - view.start_time)
    icon = "⚠️" if counts.failed else "✅"
    text = (
        f"{icon} Mirror {view.op.name.lower()} for **{label}** — "
        f"{counts.delivered} ok, {counts.failed} failed, {counts.cancelled} cancelled "
        f"in {elapsed}. Detail on the mirror-logs page."
    )
    with contextlib.suppress(Exception):
        log_channel = await bot.fetch_channel(log_channel_id)
        if isinstance(log_channel, h.TextableGuildChannel):
            await log_channel.send(text)


@loader.task(
    lb.uniformtrigger(hours=cfg.mirror_reachability_sweep_hours, wait_first=True),
    max_failures=-1,
)
async def reachability_sweep(bot: CachedFetchBot = lb.di.INJECTED):
    """Probe every enabled legacy destination for reachability + send perms and disable
    the ones unreachable past the grace window.

    A low-load background job (not the hot path): the perm check is cache-first,
    and a pair is disabled only after it has stayed confirmed-unreachable for
    ``cfg.mirror_unreachable_grace_hours``, so a transient blip never disables a mirror.
    """
    if not await settings.get_disable_bad_channels():
        return
    await aio.sleep(randint(30, 300))

    try:
        candidates = await MirroredChannel.fetch_reachability_candidates()
    except Exception as e:
        e.add_note("Fetching mirror reachability candidates failed")
        await discord_error_logger(e, operation="Mirror reachability sweep")
        return
    if not candidates:
        return

    sem = aio.Semaphore(8)

    async def probe(
        pair: tuple[int, int],
    ) -> tuple[tuple[int, int], utils.DestVerdict]:
        async with sem:
            try:
                verdict = await utils.confirm_dest_unsendable(bot, pair[1])
            except Exception:
                verdict = utils.DestVerdict.UNKNOWN
        return pair, verdict

    results = await aio.gather(*(probe(pair) for pair in candidates))
    reachable = [pair for pair, v in results if v is utils.DestVerdict.SENDABLE]
    unreachable = [
        pair
        for pair, v in results
        if v
        in (utils.DestVerdict.CONFIRMED_UNSENDABLE, utils.DestVerdict.CONFIRMED_GONE)
    ]

    try:
        disabled = await MirroredChannel.apply_reachability_sweep(
            reachable, unreachable
        )
    except Exception as e:
        e.add_note("Applying the mirror reachability sweep failed")
        await discord_error_logger(e, operation="Mirror reachability sweep")
        return

    if disabled:
        num = len(disabled)
        message = (
            f"Disabled {num} unreachable mirror(s) (reachability sweep): "
            + ", ".join(f"{src}: {dest}" for src, dest in disabled)
        )
        if num > _DISABLE_CRITICAL_MIN:
            health_logger.critical(message)
        elif num > _DISABLE_ERROR_MIN:
            health_logger.error(message)
        else:
            health_logger.warning(message)


@loader.listener(h.StartedEvent)
async def _start_mirror_worker(
    _event: h.StartedEvent,
    store: AppEmojiStore = lb.di.INJECTED,
) -> None:
    bot = t.cast(CachedFetchBot, _event.app)
    await mirror_worker.start(bot, store)
    # Register a drain watcher for any source with leftover work, so a post-restart
    # backlog still summarises + posts its result line — as a background task so a slow
    # query can't stall startup. Keep a strong reference (asyncio only holds a weak one;
    # see ``_watchers``) so the coroutine can't be garbage-collected mid-query.
    global _backlog_recovery_task
    _backlog_recovery_task = aio.create_task(_recover_backlog_watchers(bot))


async def _recover_backlog_watchers(bot: CachedFetchBot) -> None:
    """Register a drain watcher per source message with non-terminal rows on startup."""
    try:
        backlog = await MirrorDelivery.non_terminal_backlog()
    except Exception:
        logging.exception("mirror backlog recovery query failed")
        return
    for src_msg_id, src_ch_id, _count, any_deleted, any_unsent in backlog:
        op = (
            MirrorOperationType.DELETE
            if any_deleted
            else MirrorOperationType.SEND
            if any_unsent
            else MirrorOperationType.UPDATE
        )
        view = RunView(
            op=op,
            src_ch_id=src_ch_id,
            src_msg_id=src_msg_id,
            start_time=perf_counter(),
        )
        try:
            start_drain_watcher(bot, view)
        except Exception:
            logging.exception(
                "failed to start recovery drain watcher for %s", src_msg_id
            )
    if backlog:
        logging.info(
            "Mirror backlog recovery: %d source message(s) with pending work.",
            len(backlog),
        )


def ignore_non_src_channels(func: collections.abc.Callable[..., t.Any]):
    async def wrapped_func(event: h.MessageEvent):
        if isinstance(event, (h.MessageCreateEvent, h.MessageUpdateEvent)):
            msg = event.message
            if msg is None:
                return
            channel_id, guild_id = msg.channel_id, msg.guild_id
        elif isinstance(event, h.MessageDeleteEvent):
            # A delete carries channel_id/guild_id on the event itself, so an uncached
            # source message (old_message is None) is still propagated — mark_deleted
            # keys on message_id alone and needs no cached body. guild_id lives only on
            # the guild subclass; None (DM) never matches a test guild.
            channel_id = event.channel_id
            guild_id = getattr(event, "guild_id", None)
        else:
            return

        in_src_channel = (
            int(channel_id) in await MirroredChannel.get_or_fetch_all_srcs()
        )
        # In a test env, also process messages that live in one of the test guild(s),
        # so the live test bot can mirror arbitrary channels there. Scoped to guild_id
        # so the bot's presence in *other* servers never drags their channels into the
        # mirror path. guild_id is None in DMs, never a test guild.
        in_test_guild = guild_id is not None and guild_id in cfg.test_env

        if in_src_channel or in_test_guild:
            return await func(event)

    return wrapped_func


# Total time to wait for a source message to be published (crossposted) before giving up
# and mirroring nothing. One ceiling governs BOTH the crosspost wait_for AND the
# transient fetch-retry loop below — so the whole function can never run longer than
# this, however the time splits between waiting for the publish and retrying a flaky
# source fetch.
_CROSSPOST_WAIT_CEILING_SECONDS = 12 * 60 * 60


async def handle_waiting_for_crosspost(
    msg: h.Message,
    bot: CachedFetchBot,
    channel: h.TextableChannel,
    wait_for_crosspost: bool,
):
    deadline = perf_counter() + _CROSSPOST_WAIT_CEILING_SECONDS
    backoff_timer = 30
    while True:
        remaining = deadline - perf_counter()
        if remaining <= 0:
            # The 12h ceiling elapsed while retrying a flaky source fetch — give up
            # rather than loop (and re-alert) forever, like the wait_for's own cap.
            logging.warning(
                "Giving up crosspost wait for message %s in channel %s after %dh.",
                msg.id,
                str(settings.followable_name(id=channel.id)),
                _CROSSPOST_WAIT_CEILING_SECONDS // 3600,
            )
            return
        try:
            channel_name_or_id = str(settings.followable_name(id=channel.id))
            logging.info(
                f"MessageCreateEvent received for message in channel: "
                f"{channel_name_or_id}"
            )

            # The below is to make sure we aren't using a reference to a message that
            # has already changed (in particular, has already been crossposted)
            # Using such a reference would result in us waiting forever for a crosspost
            # event that has already fired
            msg = await bot.rest.fetch_message(msg.channel_id, msg.id)

            if wait_for_crosspost and h.MessageFlag.CROSSPOSTED not in msg.flags:
                logging.info(
                    f"Message in channel {channel_name_or_id} not crossposted, "
                    "waiting..."
                )
                await bot.wait_for(
                    h.MessageUpdateEvent,
                    # Wait for the publish, but only up to the time left on the ceiling.
                    timeout=remaining,
                    predicate=lambda e, msg=msg: bool(
                        e.message.id == msg.id
                        and e.message.flags
                        and h.MessageFlag.CROSSPOSTED in e.message.flags
                    ),
                )
                logging.info(
                    f"Crosspost event received for message in channel "
                    f"{channel_name_or_id}, " + "continuing..."
                )
        except TimeoutError:
            return
        except Exception as e:
            # A permanent error (missing access/perms, unknown channel/message) will
            # never succeed on retry, so retrying would re-log a full traceback roughly
            # every 30s forever. Skip this source's crosspost wait instead of flooding
            # the alerts channel; any genuinely actionable failure surfaces downstream
            # through the mirror send/edit's own classified path.
            if classify_error(e) is ErrorClass.PERMANENT:
                logging.warning(
                    "Skipping crosspost wait for message %s in channel %s: source not "
                    "fetchable (%s).",
                    msg.id,
                    str(settings.followable_name(id=channel.id)),
                    type(e).__name__,
                )
                return
            await discord_error_logger(e, operation="Mirror crosspost")
            # Back off, but never past the ceiling (the top-of-loop check then returns).
            await aio.sleep(min(backoff_timer, max(0.0, deadline - perf_counter())))
            backoff_timer = min(backoff_timer * 2, 600)
        else:
            break


async def _ledger_write_with_retry[T](
    what: str, op: collections.abc.Callable[[], collections.abc.Awaitable[T]]
) -> T | None:
    """Run a gateway handler's single ledger write with bounded transient retries.

    The thin handlers do one write, so without this a momentary DB blip at event time
    would silently drop that send/edit/delete forever (nothing durable recorded, the
    gateway event gone). Retries transient failures with capped backoff; gives up
    (alerts + returns ``None``) on a permanent error or once the cap is hit.
    """
    backoff = 5
    for attempt in range(1, _HANDLER_DB_MAX_TRIES + 1):
        try:
            return await op()
        except Exception as e:
            last = attempt >= _HANDLER_DB_MAX_TRIES
            if classify_error(e) is ErrorClass.PERMANENT or last:
                e.add_note(f"Mirror {what} ledger write failed ({attempt} attempt(s))")
                await discord_error_logger(e, operation=f"Mirror {what} enqueue")
                return None
            logging.warning(
                "Mirror %s ledger write transient failure (attempt %d): %s",
                what,
                attempt,
                e,
            )
            await aio.sleep(backoff)
            backoff = min(backoff * 2, 600)
    return None


@loader.listener(h.MessageCreateEvent)
@ignore_non_src_channels
@utils.ignore_own_user
async def message_create_repeater(event: h.MessageCreateEvent):
    cached_bot = t.cast(CachedFetchBot, event.app)
    await message_create_repeater_impl(
        event.message,
        cached_bot,
        t.cast(
            h.TextableChannel,
            await cached_bot.fetch_channel(event.message.channel_id),
        ),
    )


async def message_create_repeater_impl(
    msg: h.Message,
    bot: CachedFetchBot,
    channel: h.TextableChannel,
    wait_for_crosspost: bool = True,
):
    # Wait for message to be crossposted before mirroring if requested.
    await handle_waiting_for_crosspost(
        msg=msg,
        bot=bot,
        channel=channel,
        wait_for_crosspost=wait_for_crosspost,
    )

    # Enqueue a fresh send fan-out. INSERT-IGNORE makes a duplicate gateway event or a
    # manual re-mirror of an already-enqueued message a no-op (returns 0). Retried on a
    # transient DB blip so the send isn't silently lost.
    inserted = await _ledger_write_with_retry(
        "send", lambda: MirrorDelivery.enqueue_send(channel.id, msg.id)
    )
    if not inserted:
        return

    view = RunView(
        op=MirrorOperationType.SEND,
        src_ch_id=channel.id,
        src_msg_id=msg.id,
        start_time=perf_counter(),
    )

    # Refetch so the progress card's summary reflects any edit near the crosspost.
    with contextlib.suppress(Exception):
        msg = await bot.rest.fetch_message(msg.channel_id, msg.id)

    start_drain_watcher(bot, view, source_message=msg)
    mirror_worker.nudge()


def is_content_edit(message: h.PartialMessage) -> bool:
    """Whether a MessageUpdateEvent reflects a genuine *content* edit.

    Discord deceptively fires a MessageUpdateEvent for things that are not content
    edits at all: publishing/crossposting an announcement, embed unfurls, flag
    changes, etc. Only a real content edit sets ``edited_timestamp``; the others
    leave it ``None`` or ``UNDEFINED`` (both falsy).
    """
    return bool(message.edited_timestamp)


@loader.listener(h.MessageUpdateEvent)
@ignore_non_src_channels
@utils.ignore_own_user
async def message_update_repeater(event: h.MessageUpdateEvent):
    # Cheap early-out for the non-content updates Discord also reports as edits (embed
    # unfurls, flag changes) so an unfurl of an already-delivered message doesn't
    # trigger a needless dest re-edit. The publish/crosspost transition is *not* caught
    # here (a message edited before it was published carries a stale edited_timestamp),
    # so the authoritative guard is bump_for_edit's delivered-baseline gate below: a
    # message that has not been delivered anywhere yet is a true no-op, which is exactly
    # the state of a message at the moment it is published.
    if not is_content_edit(event.message):
        return

    # Ignore edits to messages that haven't been crossposted (published) yet: until
    # publish, the create handler is still waiting to mirror the message, so an edit has
    # nothing to update and acting now would race that pending send.
    flags = event.message.flags
    if not isinstance(flags, h.MessageFlag) or h.MessageFlag.CROSSPOSTED not in flags:
        return

    await message_update_repeater_impl(
        t.cast(h.Message, event.message),
        t.cast(CachedFetchBot, event.app),
    )


async def message_update_repeater_impl(msg: h.Message, bot: CachedFetchBot):
    # Reconcile the edit: bump every non-deleted row at the new version (and, once the
    # message has been delivered somewhere, insert rows for any dests added since the
    # send). One transaction, no locks; retried on a transient DB blip so the edit isn't
    # silently lost.
    result = await _ledger_write_with_retry(
        "edit", lambda: MirrorDelivery.bump_for_edit(msg.channel_id, msg.id)
    )
    if result is None:
        return
    _bumped, _inserted, had_delivered_baseline = result
    if not had_delivered_baseline:
        # Either not a mirrored message, or the edit landed before first delivery — the
        # version bump (if any) folds it into the still-pending send, which fetches the
        # source's live content at delivery time. No separate update run/card, and (the
        # publish transition being exactly this pre-delivery state) no phantom card.
        return

    view = RunView(
        op=MirrorOperationType.UPDATE,
        src_ch_id=msg.channel_id,
        src_msg_id=msg.id,
        start_time=perf_counter(),
    )

    with contextlib.suppress(Exception):
        msg = await bot.rest.fetch_message(msg.channel_id, msg.id)

    start_drain_watcher(bot, view, source_message=msg)
    mirror_worker.nudge()


@loader.listener(h.MessageDeleteEvent)
@ignore_non_src_channels
async def message_delete_repeater(event: h.MessageDeleteEvent):
    msg_id = event.message_id
    msg = event.old_message
    cached_bot = t.cast(CachedFetchBot, event.app)

    await message_delete_repeater_impl(msg_id, msg, cached_bot)


async def message_delete_repeater_impl(
    msg_id: int, msg: h.Message | None, bot: CachedFetchBot
):
    # Flag every row for this source as delete-intent. Never-delivered rows go straight
    # to CANCELLED; delivered rows go to PENDING for the worker to delete Discord-side.
    deletion_work = await _ledger_write_with_retry(
        "delete", lambda: MirrorDelivery.mark_deleted(msg_id)
    )
    if deletion_work is None:
        return

    mirror_worker.nudge()
    if not deletion_work:
        # Not mirrored, or nothing was ever delivered → nothing to delete Discord-side.
        return

    view = RunView(
        op=MirrorOperationType.DELETE,
        src_ch_id=None,
        src_msg_id=msg_id,
        start_time=perf_counter(),
    )
    start_drain_watcher(bot, view, source_message=msg)
    mirror_worker.nudge()


# wait_first=True: this weekly task fetches every guild's member count individually
# (~one REST call per guild — thousands of them). Running it on *every* startup
# (wait_first=False) meant rapid redeploys fired that burst repeatedly and tripped
# Discord's per-IP rate limit (Cloudflare 429s on /gateway/bot), crash-looping the bot.
# A weekly population refresh has no reason to run at boot.
@loader.task(lb.uniformtrigger(hours=24 * 7, wait_first=True), max_failures=-1)
async def refresh_server_sizes(bot: CachedFetchBot = lb.di.INJECTED):
    await aio.sleep(randint(30, 60))

    backoff_timer = 30
    while True:
        try:
            server_populations = {}
            async for guild in bot.rest.fetch_my_guilds():
                if not isinstance(guild, h.RESTGuild):
                    guild = await bot.rest.fetch_guild(guild.id)

                try:
                    server_populations[guild.id] = guild.approximate_member_count
                except Exception as e:
                    logging.exception(e)

            existing_servers = await ServerStatistics.fetch_server_ids()
            existing_servers = list(
                set(existing_servers).intersection(set(server_populations.keys()))
            )
            new_servers = list(set(server_populations.keys()) - set(existing_servers))

            new_servers_bins = [
                new_servers[i : i + 50] for i in range(0, len(new_servers), 50)
            ]
            for new_servers_bin in new_servers_bins:
                await ServerStatistics.add_servers_in_batch(
                    new_servers_bin,
                    [server_populations[server_id] for server_id in new_servers_bin],
                )

            existing_servers_bins = [
                existing_servers[i : i + 50]
                for i in range(0, len(existing_servers), 50)
            ]
            for existing_servers_bin in existing_servers_bins:
                await ServerStatistics.update_population_in_batch(
                    existing_servers_bin,
                    [
                        server_populations[server_id]
                        for server_id in existing_servers_bin
                    ],
                )

        except Exception as e:
            should_retry_ = backoff_timer <= 24 * 60 * 60

            exception_note = "Error refreshing server sizes, "
            exception_note += (
                f"backing off for {backoff_timer} minutes"
                if should_retry_
                else "giving up"
            )
            e.add_note(exception_note)

            await discord_error_logger(e, operation="Server size refresh")

            if not should_retry_:
                break

            await aio.sleep(backoff_timer * 60)
            backoff_timer = backoff_timer * 4

        else:
            break


@loader.task(lb.uniformtrigger(hours=24, wait_first=False), max_failures=-1)
async def prune_message_db(bot: CachedFetchBot = lb.di.INJECTED):
    await aio.sleep(randint(120, 1800))
    try:
        await MirrorDelivery.prune()
        # After the ledger prune, drop the version snapshots + operation-log rows
        # orphaned by it (both live as long as their source keeps a delivery row).
        await MirrorMessageVersion.prune()
        await MirrorOperationLog.prune()
    except Exception as e:
        e.add_note("Exception during routine pruning of the mirror delivery ledger")
        await discord_error_logger(e, operation="Mirror DB prune")


# Command group for all mirror commands
mirror_group = lb.Group(
    "mirror",
    "Command group for all mirror control/administration commands",
)


@mirror_group.register
class UndoAutoDisable(
    lb.SlashCommand,
    name="undo_auto_disable",
    description="Undo auto disable of a channel due to repeated post failures",
    hooks=[owner_only],
):
    from_date = lb.string("from_date", "Date to start from")

    @lb.invoke
    async def invoke(self, ctx: lb.Context, bot: CachedFetchBot = lb.di.INJECTED):
        await ctx.defer()

        from_date = dateparser.parse(self.from_date)

        mirrors = await MirroredChannel.undo_auto_disable_for_failure(since=from_date)
        response = f"Undid auto disable since {from_date} for channels {mirrors}"
        logging.info(response)
        await ctx.respond(response)


@mirror_group.register
class ManualAdd(
    lb.SlashCommand,
    name="manual_add",
    description="Manually add a mirror to the database",
    hooks=[owner_only],
):
    src = lb.string("src", "Source channel (link, mention, or id)")
    dest = lb.string("dest", "Destination channel (link, mention, or id)")
    dest_server_id = lb.string(
        "dest_server_id",
        "Destination server id (optional if dest is a channel link)",
        default="",
    )

    @lb.invoke
    async def invoke(self, ctx: lb.Context, bot: CachedFetchBot = lb.di.INJECTED):
        await ctx.defer()

        try:
            src, _ = parse_channel_ref(self.src)
            dest, dest_guild_id = parse_channel_ref(self.dest)
        except ValueError as e:
            await ctx.respond(str(e))
            return

        if self.dest_server_id.strip():
            try:
                dest_server_id = int(self.dest_server_id.strip())
            except ValueError:
                await ctx.respond(f"{self.dest_server_id!r} is not a valid server id")
                return
        elif dest_guild_id is not None:
            dest_server_id = dest_guild_id
        else:
            await ctx.respond(
                "Provide dest_server_id, or pass dest as a full channel link "
                "(which includes the server id)."
            )
            return

        await MirroredChannel.add_mirror(
            src, dest, dest_server_id=dest_server_id, legacy=True
        )
        await ctx.respond(f"Added mirror {src} -> {dest} (server {dest_server_id})")


@mirror_group.register
class ManualMirrorDelete(
    lb.SlashCommand,
    name="delete_msg",
    description="Manually delete a mirrored message",
    hooks=[owner_only],
):
    message_id = lb.string("message_id", "Message to delete")

    @lb.invoke
    async def invoke(self, ctx: lb.Context, bot: CachedFetchBot = lb.di.INJECTED):
        mid = int(self.message_id)

        initial = await ctx.respond("Deleting message...", ephemeral=True)
        logging.info(f"Manually deleting mirrored message {mid}")
        await message_delete_repeater_impl(mid, bot.cache.get_message(mid), bot)
        await ctx.edit_response(initial, "Deleted messages.")


@mirror_group.register
class MirrorSourceDetails(
    lb.SlashCommand,
    name="source_details",
    description="Show details about a channels mirror sources if any",
    hooks=[owner_only],
):
    channel_id = lb.string(
        "channel_id", "Destination channel to show details of (link, mention, or id)"
    )

    @lb.invoke
    async def invoke(self, ctx: lb.Context, bot: CachedFetchBot = lb.di.INJECTED):
        try:
            channel_id, _ = parse_channel_ref(self.channel_id)
        except ValueError as e:
            await ctx.respond(str(e))
            return

        initial = await ctx.respond("Checking the database...")

        legacy_sources = await MirroredChannel.fetch_srcs(channel_id, legacy=True)
        new_style_sources = await MirroredChannel.fetch_srcs(channel_id, legacy=False)

        sources = {val: key for key, val in (await settings.get_followables()).items()}

        legacy_sources = [
            sources.get(legacy_source, f"Unknown Source: {legacy_source}")
            for legacy_source in legacy_sources
        ]
        new_style_sources = [
            sources.get(new_style_source, f"Unknown Source: {new_style_source}")
            for new_style_source in new_style_sources
        ]

        channel = await bot.fetch_channel(channel_id)
        channel_name = channel.name if channel else "Unknown Channel"

        await ctx.edit_response(
            initial,
            "```\n"
            + f"Details for Channel: {channel_name} ({channel_id})\n"
            + "Legacy sources:\n"
            + ("\n".join(legacy_sources) if legacy_sources else "None")
            + "\n\n"
            + "New style sources:\n"
            + ("\n".join(new_style_sources) if new_style_sources else "None")
            + "\n"
            + "```",
        )


class ManualMirrorSend(
    lb.MessageCommand,
    name="mirror_send",
    description="Manually mirror a message",
    hooks=[owner_only],
):
    @lb.invoke
    async def invoke(
        self,
        ctx: lb.Context,
        bot: CachedFetchBot = lb.di.INJECTED,
    ):
        initial = await ctx.respond("Mirroring message...", ephemeral=True)
        logging.info(f"Manually mirroring for channel id {self.target.channel_id}")
        await message_create_repeater_impl(
            self.target,
            bot,
            t.cast(h.TextableChannel, await bot.fetch_channel(ctx.channel_id)),
            wait_for_crosspost=False,
        )
        await ctx.edit_response(initial, "Mirrored message.")


class ManualMirrorUpdate(
    lb.MessageCommand,
    name="mirror_update",
    description="Manually update a mirrored message",
    hooks=[owner_only],
):
    @lb.invoke
    async def invoke(
        self,
        ctx: lb.Context,
        bot: CachedFetchBot = lb.di.INJECTED,
    ):
        initial = await ctx.respond("Updating message...", ephemeral=True)
        logging.info(
            f"Manually updating mirrored message {self.target.id} "
            f" in channel id {self.target.channel_id}"
        )
        await message_update_repeater_impl(self.target, bot)
        await ctx.edit_response(initial, "Updated message.")


class MirrorCancel(
    lb.MessageCommand,
    name="mirror_cancel",
    description="Manually cancels a message mirror currently in progress",
    hooks=[owner_only],
):
    @lb.invoke
    async def invoke(self, ctx: lb.Context, bot: CachedFetchBot = lb.di.INJECTED):
        message: h.Message = self.target
        cancelled = await MirrorDelivery.cancel_pending(message.id)
        if not cancelled:
            await ctx.respond(
                "This message does not have any sends/updates in progress to cancel.",
                ephemeral=True,
            )
            return
        mirror_worker.nudge()
        await ctx.respond("Cancelled mirror", ephemeral=True)


loader.command(
    mirror_group, guilds=guild_scope(*cfg.test_env, cfg.control_discord_server_id)
)
loader.command(
    ManualMirrorSend,
    guilds=guild_scope(
        *cfg.test_env, cfg.control_discord_server_id, cfg.kyber_discord_server_id
    ),
)
loader.command(
    ManualMirrorUpdate,
    guilds=guild_scope(
        *cfg.test_env, cfg.control_discord_server_id, cfg.kyber_discord_server_id
    ),
)
loader.command(
    MirrorCancel,
    guilds=guild_scope(
        *cfg.test_env, cfg.control_discord_server_id, cfg.kyber_discord_server_id
    ),
)
