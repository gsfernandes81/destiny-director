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

"""Temporary memory-attribution diagnostic (both bots).

Logs a single ``MEM_DIAG`` block to stdout (visible via ``railway logs``) so we can
split each bot's resident RAM into: live hikari cache (entry counts per type),
Python-heap object count, and — crucially — glibc allocator retention, measured as the
RSS reclaimed by ``gc.collect()`` then ``malloc_trim(0)``. A large trim delta means a
lot of the RSS is freed-but-not-returned heap (exactly what jemalloc / not building the
manifest dict would reclaim).

Everything is defensive: any probe that fails degrades to a sentinel rather than
raising, so this can never take a bot down. Intended to be removed once the numbers are
captured. It reads hikari's private cache dicts directly (stable in the pinned hikari
2.5.x) because the public views nest members/voice-states per guild and would misreport
counts.
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import gc
import logging
import typing as t

import hikari as h

logger = logging.getLogger("dd.mem_diag")


def _vm_kib(field: str) -> int:
    """A field from /proc/self/status in KiB, or -1 if unavailable (non-Linux)."""
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith(field + ":"):
                    return int(line.split()[1])
    except Exception:
        pass
    return -1


def _rss_mb() -> float:
    kib = _vm_kib("VmRSS")
    return round(kib / 1024, 1) if kib >= 0 else -1.0


def _malloc_trim() -> bool:
    """Return freed heap to the OS (glibc only). True if the call was made."""
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        if hasattr(libc, "malloc_trim"):
            libc.malloc_trim(0)
            return True
    except Exception:
        pass
    return False


def _cache_counts(bot: h.GatewayBot) -> dict[str, int]:
    """Entry counts per cache type, read from hikari's private dicts defensively."""
    c: t.Any = bot.cache
    out: dict[str, int] = {}

    def flat(attr: str) -> int:
        try:
            return len(getattr(c, attr))
        except Exception:
            return -1

    out["guilds"] = flat("_guild_entries")
    out["channels"] = flat("_guild_channel_entries")
    out["threads"] = flat("_guild_thread_entries")
    out["roles"] = flat("_role_entries")
    out["emojis"] = flat("_emoji_entries")
    out["stickers"] = flat("_sticker_entries")
    out["invites"] = flat("_invite_entries")
    out["users"] = flat("_user_entries")
    out["messages"] = flat("_message_entries")
    out["dm_channel_ids"] = flat("_dm_channel_entries")

    members = voice = 0
    try:
        for rec in getattr(c, "_guild_entries", {}).values():
            m = getattr(rec, "members", None)
            if m:
                members += len(m)
            v = getattr(rec, "voice_states", None)
            if v:
                voice += len(v)
    except Exception:
        members = voice = -1
    out["members"] = members
    out["voice_states"] = voice
    return out


def format_memory_report(bot: h.GatewayBot, *, label: str) -> str:
    """Build the MEM_DIAG report string. Never raises."""
    try:
        rss0 = _rss_mb()
        hwm = _vm_kib("VmHWM")
        hwm_mb = round(hwm / 1024, 1) if hwm >= 0 else -1.0
        gc.collect()
        rss_gc = _rss_mb()
        trimmed = _malloc_trim()
        rss_trim = _rss_mb()
        counts = _cache_counts(bot)
        n_objs = len(gc.get_objects())
        counts_str = " ".join(f"{k}={v}" for k, v in counts.items())
        return (
            f"MEM_DIAG[{label}] "
            f"rss={rss0}MB hwm={hwm_mb}MB rss_after_gc={rss_gc}MB "
            f"rss_after_trim={rss_trim}MB (trimmed={trimmed}) "
            f"reclaimable={round(max(rss0 - rss_trim, 0), 1)}MB "
            f"gc_objects={n_objs} | cache {counts_str}"
        )
    except Exception as exc:  # never take the bot down
        return f"MEM_DIAG[{label}] report failed: {type(exc).__name__}: {exc}"


def install(
    bot: h.GatewayBot, label: str, *, warmup: int = 120, interval: int = 900
) -> None:
    """Log a MEM_DIAG report ``warmup`` s after start, then every ``interval`` s."""

    async def _on_start(_event: h.StartedEvent) -> None:
        async def _loop() -> None:
            await asyncio.sleep(warmup)
            while True:
                logger.info("%s", format_memory_report(bot, label=label))
                await asyncio.sleep(interval)

        _ = asyncio.create_task(_loop())

    _ = bot.listen(h.StartedEvent)(_on_start)
