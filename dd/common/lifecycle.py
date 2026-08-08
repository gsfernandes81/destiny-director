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

"""Process lifecycle (start / stop) shared by both bots.

On the way up, :func:`apply_oom_score_adj` sets this process's OOM-kill preference.

On the way down:
Termination must work identically from a command invoke **and** a component (button)
callback. hikari runs interaction callbacks as fire-and-forget tasks whose wrapper
drops ``SystemExit``, so ``sys.exit`` raised there is silently swallowed. Instead:
record the desired exit code, schedule ``bot.close()`` on the loop (not awaited inline,
so the interaction reply lands first), let ``bot.run()`` return, and exit on the main
thread via :func:`consume_exit_code` at the end of each ``__main__``.

Railway contract: a clean ``exit 0`` stays down (under an ON_FAILURE restart policy);
a non-zero exit is restarted, and counts against the service's max-retry ceiling. Only
the clean exit is used deliberately — the ``/restart`` command that exited non-zero on
purpose was removed 2026-08-04 (see :mod:`dd.common.controller`).
"""

import asyncio
import logging
import sys
from pathlib import Path

import hikari as h

from dd.common import cfg

STOP_EXIT_CODE = 0

# procfs knob for this process's OOM-kill preference. Injectable in
# :func:`apply_oom_score_adj` so the tests never write to the real one.
OOM_SCORE_ADJ_PATH = Path("/proc/self/oom_score_adj")


def apply_oom_score_adj(score_adj: int | None, path: Path = OOM_SCORE_ADJ_PATH) -> None:
    """Raise this process's OOM-kill preference to ``score_adj`` (no-op when ``None``).

    Linux lets a process ALWAYS raise its own ``/proc/self/oom_score_adj``; LOWERING it
    below the current value requires ``CAP_SYS_RESOURCE``. Rootless podman cannot grant
    that capability, so the design is: the container baseline stays at the default 0
    (protecting supervisord and sshd), and the bot raises ITSELF to a high value so the
    kernel reaps the bot first.

    Never fatal. This only changes *which* process the OOM killer picks, so anything
    that goes wrong — a non-Linux host, no procfs, a write the kernel refuses — is
    logged and stepped over rather than taking the boot down with it. A malformed
    *configured* value is a different matter and is rejected at import time by
    ``cfg._getenv_oom_score_adj``; the range check here is a second line of defence for
    callers that pass a value in directly.
    """
    if score_adj is None:
        return

    if not cfg.OOM_SCORE_ADJ_MIN <= score_adj <= cfg.OOM_SCORE_ADJ_MAX:
        logging.warning(
            "Refusing to write oom_score_adj=%s: outside the kernel's legal range "
            "(%s..%s). Leaving it at the process default.",
            score_adj,
            cfg.OOM_SCORE_ADJ_MIN,
            cfg.OOM_SCORE_ADJ_MAX,
        )
        return

    if sys.platform != "linux":
        logging.debug("oom_score_adj is Linux-only; skipping on %s.", sys.platform)
        return

    if not path.exists():
        logging.debug("%s does not exist; skipping oom_score_adj.", path)
        return

    try:
        path.write_text(f"{score_adj}\n")
    except OSError:
        # Raising is always permitted, so this normally means procfs is masked or
        # read-only rather than a capability problem — either way, not worth dying for.
        logging.warning(
            "Could not write oom_score_adj=%s to %s; continuing.",
            score_adj,
            path,
            exc_info=True,
        )
    else:
        logging.info("Raised this process's oom_score_adj to %s.", score_adj)


_desired_exit_code: int | None = None
# Hold a reference to the scheduled close task so it isn't garbage collected mid-flight.
_shutdown_task: asyncio.Task[None] | None = None


async def request_shutdown(bot: h.GatewayBot, exit_code: int) -> None:
    """Record the desired process exit code and schedule a clean gateway shutdown.

    ``bot.close()`` is scheduled rather than awaited inline so the calling interaction
    callback can finish replying before the REST client is torn down. Once ``close()``
    unwinds the gateway, ``bot.run()`` returns and ``__main__`` exits with the recorded
    code (see :func:`consume_exit_code`).
    """
    global _desired_exit_code, _shutdown_task
    _desired_exit_code = exit_code
    _shutdown_task = asyncio.get_running_loop().create_task(bot.close())


def consume_exit_code() -> int:
    """Return the exit code requested via :func:`request_shutdown` (``0`` if none)."""
    return _desired_exit_code if _desired_exit_code is not None else 0
