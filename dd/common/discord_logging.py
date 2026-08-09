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

"""Forward Python ``logging`` records to the Discord alerts channel.

A single :class:`DiscordLogHandler` is attached to the root logger (at the
``alert_min_level`` Autopost Setting, see :mod:`dd.common.settings`) per process. Its
``emit`` is synchronous and only enqueues a lightweight snapshot; a background coroutine
batches records over a short window, collapses duplicates by *signature*, renders each
group as a Components V2 container, and posts it to the ``alerts_channel_id`` setting.

High-priority flagging:
    - Repeated errors at high frequency: a per-signature rolling window counts
      occurrences; once it crosses ``cfg.alert_freq_threshold`` within
      ``cfg.alert_freq_window`` the alert is *promoted to CRITICAL* (a "storm").
    - Owners are pinged only for CRITICAL alerts (storm-promoted or logged at
      CRITICAL directly, e.g. the mirror failure-ratio flag), debounced by
      ``cfg.alert_escalation_debounce`` so a sustained storm does not re-ping.

Reference codes are deterministic: the same error identity always yields the
same short code, so a user-reported code maps straight to its deduped alert.
"""

import asyncio as aio
import contextlib
import dataclasses
import itertools
import logging
import sys
import time
import traceback
import typing as t
from collections import defaultdict, deque

import hikari as h
import lightbulb as lb

from . import cfg, settings
from .bot import CachedFetchBot
from .components import build_container, cv2_error, respond_cv2

# ``identity_for_exc``/``reference_code`` live in ``utils`` (pure, Discord-free) so
# the mirror subsystem can reuse them without importing this handler. Re-exported
# here to keep this module's public surface unchanged.
from .utils import _normalize, identity_for_exc, reference_code

__all__ = ["identity_for_exc", "reference_code"]

# Records from these logger trees are never forwarded: they are noisy and, more
# importantly, hikari's REST logger can emit during our own send and would feed
# the handler back into itself.
_IGNORED_LOGGER_PREFIXES = ("hikari", "lightbulb", "asyncio", "aiosqlite")

# Severity styling: emoji per level bucket. The accent colour beside it is a DB-backed
# setting for all three levels, resolved at call time by _style() below — warning and
# critical used to be plain cfg constants, which made them the only colours on this path
# that needed a redeploy to change. Each falls back to the literal it held there when no
# row is set (see dd.common.settings._DEFAULTS).
_WARNING_EMOJI = "⚠️"
_CRITICAL_EMOJI = "🚨"
_ERROR_EMOJI = "🛑"

# Discord component budgets (kept conservatively under the hard limits).
_MAX_TRACEBACK_CHARS = 1800
_MAX_MESSAGE_CHARS = 1000

_installed_handler: "DiscordLogHandler | None" = None


def _record_identity(record: logging.LogRecord) -> str:
    exc = record.exc_info[1] if record.exc_info else None
    if exc is not None:
        return identity_for_exc(exc)
    return f"{record.name}: {_normalize(record.getMessage())}"


def _reference_for_record(
    record: logging.LogRecord, identity: str | None = None
) -> str:
    """The reference code for a record, stamping it on for reuse.

    Computed once and cached on the record as ``dd_reference`` so the console
    line (via :class:`_ReferenceFormatter`) and the Discord alert show the same
    code — a user can copy a code off an alert and search the Railway logs for it.

    ``identity`` lets a caller that already computed :func:`_record_identity`
    (e.g. :meth:`DiscordLogHandler.emit`) pass it in to avoid recomputing it; it
    is derived lazily on a cache miss otherwise.
    """
    code = getattr(record, "dd_reference", None)
    if code is None:
        code = reference_code(
            identity if identity is not None else _record_identity(record)
        )
        record.dd_reference = code
    return code


class _ReferenceFormatter(logging.Formatter):
    """Append ``[ref:CODE]`` to ERROR+ console/Railway log lines.

    Wraps the root stream handler's formatter so every forwarded error's log line
    carries the same reference code as its Discord alert, making alerts traceable
    back to their full log context (surrounding lines + untruncated traceback) by
    a plain text search in the Railway dashboard.
    """

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        if record.levelno >= logging.ERROR and not record.name.startswith(
            _IGNORED_LOGGER_PREFIXES
        ):
            return f"{base} [ref:{_reference_for_record(record)}]"
        return base


async def _style(levelno: int) -> tuple[str, h.Color]:
    if levelno >= logging.CRITICAL:
        return _CRITICAL_EMOJI, await settings.get_embed_critical_color()
    if levelno >= logging.ERROR:
        return _ERROR_EMOJI, await settings.get_embed_error_color()
    return _WARNING_EMOJI, await settings.get_embed_warning_color()


def _truncate_tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = "…(truncated)…\n"
    return marker + text[-(limit - len(marker)) :]


@dataclasses.dataclass(slots=True)
class _AlertRecord:
    levelno: int
    levelname: str
    logger_name: str
    message: str
    traceback: str | None
    created: float
    identity: str
    signature: str
    reference: str
    operation: str | None = None
    count: int = 1
    # Monotonic per-process ordinal, stamped at emit time. The queue/flush pipeline can
    # batch and coalesce, and the shown timestamp is only whole-second — so this ordinal
    # is the authoritative "which came first" signal, and gaps hint at drops/coalesces.
    seq: int = 0


class DiscordLogHandler(logging.Handler):
    """A logging handler that batches records to the Discord alerts channel.

    ``emit`` is synchronous (logging contract) and merely enqueues a snapshot;
    all Discord I/O happens in the background :meth:`_consumer` coroutine. The
    handler never routes its own failures back through ``logging`` (that would
    feed records straight back into this handler) — it prints to the real
    ``stderr`` instead.
    """

    def __init__(
        self,
        bot: CachedFetchBot,
        *,
        channel_id: int,
        bot_name: str,
        owner_ids: t.Sequence[int],
        level: int = logging.ERROR,
    ) -> None:
        super().__init__(level=level)
        self._bot = bot
        self._channel_id = channel_id
        self._bot_name = bot_name
        self._owner_ids = list(owner_ids)
        self._queue: aio.Queue[_AlertRecord] = aio.Queue(
            maxsize=int(cfg.alert_queue_maxsize)
        )
        self._task: aio.Task[None] | None = None
        # Monotonic ordinal source; ``next`` is atomic (C-level), so it's safe even if a
        # record is emitted from a non-loop thread.
        self._seq = itertools.count(1)
        # Per-signature rolling occurrence timestamps (monotonic) and the last
        # time we escalated that signature, for storm detection + debounce.
        self._sig_times: dict[str, deque[float]] = defaultdict(deque)
        self._last_escalation: dict[str, float] = {}
        self._overflow_warned = False

    # -- logging.Handler contract -----------------------------------------

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name.startswith(_IGNORED_LOGGER_PREFIXES):
                return

            tb = None
            if record.exc_info:
                tb = "".join(traceback.format_exception(*record.exc_info)).rstrip()

            identity = _record_identity(record)
            alert = _AlertRecord(
                levelno=record.levelno,
                levelname=record.levelname,
                logger_name=record.name,
                message=record.getMessage(),
                traceback=tb,
                created=record.created,
                identity=identity,
                signature=f"{record.name}|{record.levelno}|{identity}",
                reference=_reference_for_record(record, identity=identity),
                operation=record.__dict__.get("dd_operation"),
                seq=next(self._seq),
            )
            self._queue.put_nowait(alert)
        except aio.QueueFull:
            if not self._overflow_warned:
                self._overflow_warned = True
                self._stderr("alert queue full; dropping records until it drains")
        except Exception:
            # Never let the handler's own failure escape into logging.
            self.handleError(record)

    # -- background consumer ----------------------------------------------

    async def _consumer(self) -> None:
        while True:
            first = await self._queue.get()
            batch = [first]
            # Collect everything that arrives within the flush window so bursts
            # of duplicates coalesce and we stay well under Discord rate limits.
            await aio.sleep(float(cfg.alert_flush_interval))
            while True:
                try:
                    batch.append(self._queue.get_nowait())
                except aio.QueueEmpty:
                    break
            try:
                await self._flush(batch)
            except Exception as exc:  # noqa: BLE001 - must not re-enter logging
                self._stderr(f"failed to flush alert batch: {exc!r}")

    async def _flush(self, batch: list[_AlertRecord]) -> None:
        # Collapse duplicates: first occurrence represents the group, count sums.
        grouped: dict[str, _AlertRecord] = {}
        for rec in batch:
            existing = grouped.get(rec.signature)
            if existing is None:
                grouped[rec.signature] = rec
            else:
                existing.count += rec.count

        now = time.monotonic()
        self._prune_escalations(now)
        for rec in grouped.values():
            storm = self._is_storm(rec, now)
            await self._send_alert(rec, storm=storm, now=now)

    def _is_storm(self, rec: _AlertRecord, now: float) -> bool:
        """Whether this signature has crossed the frequency threshold in-window."""
        window = self._sig_times[rec.signature]
        window.extend([now] * rec.count)
        cutoff = now - float(cfg.alert_freq_window)
        while window and window[0] < cutoff:
            window.popleft()
        if not window:
            del self._sig_times[rec.signature]
            return False
        return len(window) >= int(cfg.alert_freq_threshold)

    def _ping_allowed(self, signature: str, now: float) -> bool:
        """True (and records the time) unless this signature pinged recently.

        Debounces *all* owner pings — storm escalations and directly-logged
        criticals alike — so a sustained critical condition pages once per window.
        """
        last = self._last_escalation.get(signature, 0.0)
        if now - last < float(cfg.alert_escalation_debounce):
            return False
        self._last_escalation[signature] = now
        return True

    def _prune_escalations(self, now: float) -> None:
        """Evict escalation timestamps past the debounce window.

        ``_ping_allowed`` records one entry per escalated signature but never
        removes any; over uptime that grows with the diversity of signatures that
        reach a ping. An entry older than the debounce window is useless (the next
        ping would be allowed regardless), so drop it — symmetric to the
        ``_sig_times`` cleanup in ``_is_storm``.
        """
        debounce = float(cfg.alert_escalation_debounce)
        stale = [
            sig for sig, last in self._last_escalation.items() if now - last >= debounce
        ]
        for sig in stale:
            del self._last_escalation[sig]

    async def _send_alert(self, rec: _AlertRecord, *, storm: bool, now: float) -> None:
        # A storm promotes any lower level to CRITICAL; pinging is gated solely on
        # the effective level being CRITICAL (and debounced per signature).
        effective_level = logging.CRITICAL if storm else rec.levelno
        ping = (
            effective_level >= logging.CRITICAL
            and bool(self._owner_ids)
            and self._ping_allowed(rec.signature, now)
        )
        components = await self._render(rec, effective_level=effective_level, ping=ping)

        try:
            channel = self._bot.cache.get_guild_channel(
                self._channel_id
            ) or await self._bot.rest.fetch_channel(self._channel_id)
            channel = t.cast(h.TextableChannel, channel)
            await channel.send(
                components=components,
                flags=h.MessageFlag.IS_COMPONENTS_V2,
                user_mentions=self._owner_ids if ping else False,
            )
        except Exception as exc:  # noqa: BLE001 - must not re-enter logging
            self._stderr(f"failed to send alert ({rec.signature}): {exc!r}")

    async def _render(
        self, rec: _AlertRecord, *, effective_level: int, ping: bool
    ) -> list[h.api.ComponentBuilder]:
        emoji, color = await _style(effective_level)
        levelname = "CRITICAL" if effective_level >= logging.CRITICAL else rec.levelname
        code = rec.reference

        header = f"{emoji} **{levelname}** · `{self._bot_name}` · `{code}` · #{rec.seq}"
        if rec.operation:
            header += f" · {rec.operation}"
        if rec.count > 1:
            header += f" · ×{rec.count}"
        if effective_level > rec.levelno:
            # Promoted by storm detection.
            header += (
                f"\n🌩️ error storm: ≥{int(cfg.alert_freq_threshold)} in "
                f"{int(cfg.alert_freq_window)}s"
            )

        meta = (
            f"**{rec.logger_name}** · <t:{int(rec.created)}:T>\n"
            f"{_truncate_tail(rec.message, _MAX_MESSAGE_CHARS)}"
        )

        sections = [header, meta]
        if ping:
            sections.insert(
                0, " ".join(f"<@{owner_id}>" for owner_id in self._owner_ids)
            )
        if rec.traceback:
            sections.append(
                "```\n" + _truncate_tail(rec.traceback, _MAX_TRACEBACK_CHARS) + "\n```"
            )

        return [build_container(sections, accent_color=color)]

    # -- lifecycle ---------------------------------------------------------

    async def aclose(self) -> None:
        logging.getLogger().removeHandler(self)
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(aio.CancelledError):
                await self._task
            self._task = None
        # Best-effort flush of anything still queued.
        remaining: list[_AlertRecord] = []
        while True:
            try:
                remaining.append(self._queue.get_nowait())
            except aio.QueueEmpty:
                break
        if remaining:
            try:
                await self._flush(remaining)
            except Exception as exc:  # noqa: BLE001
                self._stderr(f"failed to flush alerts on close: {exc!r}")

    @staticmethod
    def _stderr(message: str) -> None:
        print(f"[discord_logging] {message}", file=sys.__stderr__)


class _StartupBufferHandler(logging.Handler):
    """Buffer records emitted before the Discord handler is online.

    :func:`install_discord_logging` only attaches the real
    :class:`DiscordLogHandler` once the gateway is up (``StartedEvent``). Errors
    logged earlier — extension-import failures, early ``discord_error_logger``
    calls — would otherwise reach only the console. Attached at import time, this
    handler holds those records (bounded, same logger-name filter) so they can be
    replayed into the Discord queue the moment the bot comes online.
    """

    def __init__(self, *, level: int) -> None:
        super().__init__(level=level)
        self.buffer: deque[logging.LogRecord] = deque(
            maxlen=int(cfg.alert_queue_maxsize)
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.name.startswith(_IGNORED_LOGGER_PREFIXES):
                return
            # The record keeps its args + exc_info (and the traceback object)
            # alive, so replaying it later still renders message and traceback.
            self.buffer.append(record)
        except Exception:
            self.handleError(record)


def _detach_startup_buffer(*, replay_into: "DiscordLogHandler | None") -> None:
    """Remove the startup buffer, replaying its records into ``replay_into``.

    Called once from :func:`install_discord_logging`. When a Discord handler is
    available the buffered startup errors are re-emitted into its queue (so they
    are sent once the bot is online, preserving their original timestamps);
    otherwise they are dropped (already on the console).
    """
    global _startup_buffer
    if _startup_buffer is None:
        return
    # Detach before replaying so the re-emitted records cannot loop back in.
    logging.getLogger().removeHandler(_startup_buffer)
    if replay_into is not None:
        for record in _startup_buffer.buffer:
            replay_into.emit(record)
    _startup_buffer.buffer.clear()
    _startup_buffer = None


def _resolve_level(name: str) -> int:
    level = logging.getLevelName(str(name).upper())
    return level if isinstance(level, int) else logging.ERROR


def _install_reference_formatter() -> None:
    """Wrap the root console handler(s) so ERROR+ lines carry their reference code.

    Re-uses each handler's existing format string/date format and only swaps in
    :class:`_ReferenceFormatter`, which appends ``[ref:CODE]`` to forwarded
    errors. Skips the Discord/startup-buffer handlers (they render their own
    Components V2 output, not text lines).
    """
    for handler in logging.getLogger().handlers:
        if isinstance(handler, (DiscordLogHandler, _StartupBufferHandler)):
            continue
        base = handler.formatter
        handler.setFormatter(
            _ReferenceFormatter(
                getattr(base, "_fmt", None),
                getattr(base, "datefmt", None),
            )
        )


async def install_discord_logging(
    bot: CachedFetchBot, *, bot_name: str
) -> "DiscordLogHandler | None":
    """Attach a :class:`DiscordLogHandler` to the root logger.

    Must be called with the event loop running (e.g. from ``StartedEvent``) so
    the alerts channel can be fetched and owner ids cached. No-ops (returns the
    existing handler) if called twice, or returns ``None`` if no alerts channel
    is configured.
    """
    global _installed_handler
    if _installed_handler is not None:
        return _installed_handler

    alerts_channel = await settings.get_alerts_channel_id()
    if not alerts_channel:
        DiscordLogHandler._stderr(
            "Alerts channel unset (Autopost Settings); Discord logging disabled"
        )
        _detach_startup_buffer(replay_into=None)
        return None

    owner_ids = [int(owner_id) for owner_id in await bot.fetch_owner_ids()]

    handler = DiscordLogHandler(
        bot,
        channel_id=alerts_channel,
        bot_name=bot_name,
        owner_ids=owner_ids,
        level=_resolve_level(await settings.get_alert_min_level()),
    )
    handler._task = aio.create_task(handler._consumer())
    logging.getLogger().addHandler(handler)
    _installed_handler = handler
    # Replay any errors captured before the gateway came online into the queue;
    # the consumer will send them now that the bot is up.
    _detach_startup_buffer(replay_into=handler)
    return handler


async def aclose_discord_logging() -> None:
    """Detach and drain the installed handler, if any (call on shutdown)."""
    global _installed_handler
    if _installed_handler is not None:
        await _installed_handler.aclose()
        _installed_handler = None


def log_command_failure(
    exc: lb.exceptions.ExecutionPipelineFailedException,
    *,
    logger: logging.Logger | None = None,
) -> tuple[str, str]:
    """Log a failed command pipeline, tagged with the command as the operation.

    Routes through ``logger`` (default ``dd.error``) so the failure reaches the
    alerts channel labelled with the command name. Returns ``(name, code)`` for
    callers that also surface them to the user: ``name`` is the command name and
    ``code`` is the deterministic reference code for the cause, computed here from
    the same identity that is logged so the reply and the resulting alert provably
    share it. ``exc_info`` is the real cause rather than the
    ``ExecutionPipelineFailedException`` wrapper the handler is invoked with.
    """
    name = exc.context.command_data.qualified_name
    cause = exc.causes[0] if exc.causes else exc
    code = reference_code(identity_for_exc(cause))
    log = logger if logger is not None else logging.getLogger("dd.error")
    log.error("`/%s` failed", name, exc_info=cause, extra={"dd_operation": f"/{name}"})
    return name, code


async def _report_uncaught_command_error(
    exc: lb.exceptions.ExecutionPipelineFailedException,
) -> bool:
    """Forward any otherwise-unhandled command failure to the alerts channel.

    Registered at a very low priority so command-specific handlers run first; if
    none of them handled the failure, log it through the ``dd.error`` logger
    (which forwards to the alerts channel, tagged with the command name as the
    failed operation) and report it handled to suppress lightbulb's own duplicate
    traceback — which this module's handler ignores and so never surfaces.

    Also shows the invoker a uniform CV2 error so a suppressed traceback doesn't
    leave them with a silent "application did not respond". Best-effort: swallowed
    if the interaction has already responded or expired.
    """
    name, code = log_command_failure(exc)
    with contextlib.suppress(Exception):
        await respond_cv2(
            exc.context,
            cv2_error(
                "Something went wrong",
                f"`/{name}` hit an unexpected error. It's been logged "
                f"(ref: `{code}`) — please try again.",
            ),
            ephemeral=True,
        )
    return True


def install_command_error_reporting(client: lb.Client) -> None:
    """Register the catch-all command error handler on ``client``.

    Shared by both bots so any unhandled command failure reaches the alerts
    channel labelled with the command that failed. Without it lightbulb logs such
    failures only through its own logger, which :class:`DiscordLogHandler`
    ignores, so they never reach Discord.
    """
    client.error_handler(_report_uncaught_command_error, priority=-100)


# Attach the startup buffer at import time (before any bot startup logging) so
# ERROR+ records emitted before ``install_discord_logging`` runs are retained and
# replayed to Discord once the bot is online. This runs before the DB is connected, so
# it can't read the live alert_min_level setting (dd.common.settings) — ERROR matches
# that setting's own default, so an unconfigured deploy behaves identically either way.
# install_discord_logging re-applies the live level to the real handler once it can.
_startup_buffer: "_StartupBufferHandler | None"
_buffer = _StartupBufferHandler(level=logging.ERROR)
logging.getLogger().addHandler(_buffer)
_startup_buffer = _buffer

# Tag ERROR+ console/Railway log lines with their reference code so alerts can be
# traced back to their full log context by searching the code (basicConfig in
# ``cfg`` has already installed the root stream handler by the time we import).
_install_reference_formatter()
