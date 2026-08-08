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

import asyncio
import itertools
import logging
from collections import defaultdict, deque
from unittest.mock import AsyncMock, MagicMock

import pytest

from dd.common import (
    cfg,
    discord_logging as dl,
)

_FILE = "test_discord_logging.py"


def _record(name: str, level: int, msg: str, *args: object) -> logging.LogRecord:
    return logging.LogRecord(name, level, _FILE, 1, msg, args, None)


def test_reference_formatter_tags_errors_with_alert_code():
    """ERROR+ lines gain ``[ref:CODE]`` matching the alert's reference code."""
    rec = _record("dd.error", logging.ERROR, "boom %s", "x")
    out = dl._ReferenceFormatter("%(levelname).1s %(name)s | %(message)s").format(rec)

    expected = dl.reference_code(dl._record_identity(rec))
    assert f"[ref:{expected}]" in out
    # The code is also stamped on the record for the Discord handler to reuse.
    assert getattr(rec, "dd_reference", None) == expected


def test_reference_formatter_leaves_info_and_ignored_loggers_untouched():
    info = _record("dd.x", logging.INFO, "hi")
    assert "[ref:" not in dl._ReferenceFormatter("%(message)s").format(info)

    # hikari/lightbulb/etc. are never forwarded, so they are not tagged either.
    ignored = _record("hikari.rest", logging.ERROR, "noisy")
    assert "[ref:" not in dl._ReferenceFormatter("%(message)s").format(ignored)


def test_emit_carries_operation_and_reference_onto_alert_record():
    """``dd_operation`` and the reference code reach the queued ``_AlertRecord``."""
    handler = dl.DiscordLogHandler.__new__(dl.DiscordLogHandler)
    logging.Handler.__init__(handler, level=logging.ERROR)

    queued: list[dl._AlertRecord] = []
    handler._queue = type("Q", (), {"put_nowait": lambda self, x: queued.append(x)})()
    handler._overflow_warned = False
    handler._seq = itertools.count(1)

    rec = _record("dd.error", logging.ERROR, "Error reference: %s", "ABC")
    rec.dd_operation = "Mirror update"
    handler.emit(rec)

    (alert,) = queued
    assert alert.operation == "Mirror update"
    assert alert.reference == dl.reference_code(dl._record_identity(rec))


@pytest.mark.asyncio
async def test_emit_stamps_a_monotonic_sequence_and_render_shows_it():
    """Each emit gets the next ordinal (the authoritative order signal), and the ordinal
    is rendered into the alert header so it's visible in the channel."""
    handler = dl.DiscordLogHandler.__new__(dl.DiscordLogHandler)
    logging.Handler.__init__(handler, level=logging.ERROR)
    queued: list[dl._AlertRecord] = []
    handler._queue = type("Q", (), {"put_nowait": lambda self, x: queued.append(x)})()
    handler._overflow_warned = False
    handler._seq = itertools.count(1)
    handler._bot_name = "beacon"

    handler.emit(_record("dd.error", logging.ERROR, "first"))
    handler.emit(_record("dd.error", logging.ERROR, "second"))
    assert [a.seq for a in queued] == [1, 2]  # strictly increasing in emit order

    components = await handler._render(
        queued[1], effective_level=logging.ERROR, ping=False
    )
    text = "\n".join(
        getattr(child, "content", "")
        for c in components
        for child in getattr(c, "components", [])
    )
    assert "#2" in text


def test_prune_escalations_bounds_last_escalation():
    """``_last_escalation`` is evicted past the debounce window (memory-leak N1)."""
    handler = dl.DiscordLogHandler.__new__(dl.DiscordLogHandler)
    logging.Handler.__init__(handler, level=logging.ERROR)
    handler._last_escalation = {}

    now = 1_000.0
    for i in range(50):
        assert handler._ping_allowed(f"sig-{i}", now)
    assert len(handler._last_escalation) == 50

    # Inside the debounce window: nothing evicted and a repeat ping stays debounced.
    handler._prune_escalations(now)
    assert len(handler._last_escalation) == 50
    assert not handler._ping_allowed("sig-0", now)

    # Past the debounce window: every entry evicted, so the dict stays bounded.
    handler._prune_escalations(now + float(cfg.alert_escalation_debounce) + 1)
    assert handler._last_escalation == {}


def _alert(
    *,
    signature: str,
    levelno: int = logging.ERROR,
    count: int = 1,
    seq: int = 1,
) -> dl._AlertRecord:
    """Build a fully-populated ``_AlertRecord`` for the pipeline tests."""
    return dl._AlertRecord(
        levelno=levelno,
        levelname=logging.getLevelName(levelno),
        logger_name="dd.error",
        message="boom",
        traceback=None,
        created=0.0,
        identity=signature,
        signature=signature,
        reference="AB12",
        count=count,
        seq=seq,
    )


@pytest.mark.asyncio
async def test_flush_coalesces_by_signature_and_sums_count():
    """Records sharing a signature collapse into one alert with summed count."""
    handler = dl.DiscordLogHandler.__new__(dl.DiscordLogHandler)
    logging.Handler.__init__(handler, level=logging.ERROR)
    handler._sig_times = defaultdict(deque)
    handler._last_escalation = {}
    handler._send_alert = AsyncMock()

    batch = [
        _alert(signature="sig-a", seq=1),
        _alert(signature="sig-a", seq=2),
        _alert(signature="sig-a", seq=3),
        _alert(signature="sig-b", seq=4),
    ]
    await handler._flush(batch)

    # One send per distinct signature.
    assert handler._send_alert.await_count == 2
    sent = {
        call.args[0].signature: call.args[0]
        for call in handler._send_alert.await_args_list
    }
    assert set(sent) == {"sig-a", "sig-b"}
    # The three sig-a records collapse into one group whose count sums.
    assert sent["sig-a"].count == 3
    assert sent["sig-b"].count == 1


def test_is_storm_promotes_only_past_the_frequency_threshold():
    """A signature at/over the threshold in-window is a storm; a lone hit isn't."""
    handler = dl.DiscordLogHandler.__new__(dl.DiscordLogHandler)
    logging.Handler.__init__(handler, level=logging.ERROR)
    handler._sig_times = defaultdict(deque)

    now = 1_000.0
    at_threshold = _alert(signature="storm", count=int(cfg.alert_freq_threshold))
    assert handler._is_storm(at_threshold, now) is True

    single = _alert(signature="quiet", count=1)
    assert handler._is_storm(single, now) is False


@pytest.mark.asyncio
async def test_send_alert_storm_pings_owner_once_then_debounces():
    """A storm promotes to CRITICAL and pings owners, debounced per signature."""
    handler = dl.DiscordLogHandler.__new__(dl.DiscordLogHandler)
    logging.Handler.__init__(handler, level=logging.ERROR)

    channel = MagicMock()
    channel.send = AsyncMock()
    bot = MagicMock()
    bot.cache.get_guild_channel = MagicMock(return_value=channel)

    handler._bot = bot
    handler._channel_id = 999
    handler._bot_name = "beacon"
    handler._owner_ids = [123]
    handler._last_escalation = {}

    rec = _alert(signature="storm", levelno=logging.ERROR)

    # First storm: promoted to CRITICAL, owner ping allowed.
    await handler._send_alert(rec, storm=True, now=1_000.0)
    assert channel.send.await_count == 1
    assert channel.send.await_args is not None
    assert channel.send.await_args.kwargs["user_mentions"] == [123]

    # Second storm within the debounce window: still sent, but no ping.
    await handler._send_alert(rec, storm=True, now=1_050.0)
    assert channel.send.await_count == 2
    assert channel.send.await_args is not None
    assert channel.send.await_args.kwargs["user_mentions"] is False


def test_emit_swallows_queue_full_and_latches_overflow_warning():
    """A full queue drops the record without raising and warns just once."""
    handler = dl.DiscordLogHandler.__new__(dl.DiscordLogHandler)
    logging.Handler.__init__(handler, level=logging.ERROR)

    def _full(_record: object) -> None:
        raise asyncio.QueueFull

    handler._queue = type("Q", (), {"put_nowait": lambda self, x: _full(x)})()
    handler._overflow_warned = False
    handler._seq = itertools.count(1)
    handler._stderr = MagicMock()

    handler.emit(_record("dd.error", logging.ERROR, "overflow"))

    assert handler._overflow_warned is True
    handler._stderr.assert_called_once()
