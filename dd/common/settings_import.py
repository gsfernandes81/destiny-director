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

"""Load the legacy env vars into the DB-backed settings, once, at cutover.

The settings that used to be env vars — the twelve followable channels, the alerts
channel and level, the two embed colours, the default URL, the bad-channel switch and
the two image URLs — now live in ``auto_post_settings`` and are edited on anchor's
Autopost Settings page. This copies the running deployment's values into those rows so
the cutover does not mean re-typing them into a web form.

**Run it under ``railway run``**, which puts the old vars and the new ``DATABASE_URL``
in the same process: ``make cutover-settings`` does exactly that. There is no other
supported source — the vars are deliberately gone from ``cfg.py``, so this module reads
``os.environ`` directly and is the only thing in the tree that still knows their names.

**Dry run by default.** It prints what it would write and exits; ``--execute`` commits.
It never overwrites a row that already holds a value, so a re-run after somebody has
edited the page is a no-op rather than a rollback — pass ``--overwrite`` if replacing
the page's values with the env's is genuinely what you mean.

**It does not validate channels.** The settings page checks a channel is in the right
guild, of a postable type, and that the bot can actually post there; nothing here can,
with no bot in the process. That is the right trade for a migration — these ids are
what prod has been posting to, so they are proven by use in a way a fresh entry is not
— but it does mean a typo already in ``FOLLOWABLES`` is carried across faithfully.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass

from . import (
    cfg,
    feeds as dd_feeds,
    schemas,
)

#: Legacy env var -> settings slug, for the value-column settings. Order is the order of
#: the printed report, grouped the way the settings page groups them.
_VALUE_VARS: tuple[tuple[str, str], ...] = (
    ("ALERTS_CHANNEL_ID", "alerts_channel_id"),
    ("ALERT_MIN_LEVEL", "alert_min_level"),
    ("EMBED_DEFAULT_COLOR", "embed_default_color"),
    ("EMBED_ERROR_COLOR", "embed_error_color"),
    ("DEFAULT_URL", "default_url"),
    ("LOST_SECTOR_GIF_URL", "lost_sector_image_url"),
    ("XUR_IMAGE_URL", "xur_image_url"),
)

#: The one enabled-column setting that was an env var. Everything else on that column
#: (the per-feed toggles) is already an ``auto_post_settings`` row and comes across with
#: the data copy, so it is not this script's business.
_BOOL_VAR = ("DISABLE_BAD_CHANNELS", "disable_bad_channels")

#: LOG_CHANNEL_ID is deliberately absent: Discord log forwarding was removed, so there
#: is no row to put it in and nothing would read one.
_DROPPED_VARS = ("LOG_CHANNEL_ID",)

#: The levels the settings page's <select> offers. A value outside this set matches no
#: option, so the browser shows the first one and the next save of *any* setting on that
#: page silently persists it. Kept in step with autopost_settings._ALERT_LEVELS by the
#: test that pins them equal.
_ALERT_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class SettingsImportError(Exception):
    """A problem that should stop the import rather than be written."""


@dataclass(frozen=True)
class Change:
    """One row this would write, or a reason it would not."""

    slug: str
    source: str  # the env var it came from
    column: str  # "value" or "enabled"
    new: str  # rendered for the report
    current: str | None  # what the DB holds now, rendered; None = no row
    skip: str = ""  # non-empty means "not writing", and why

    @property
    def writes(self) -> bool:
        return not self.skip


def _normalise_color(raw: str) -> str:
    """A legacy colour (``0xEC42A5``, ``EC42A5``, ``#EC42A5``) as the page stores it.

    The old var was read with ``int(x, 16)``, which accepts all three spellings; the
    page and :func:`dd.common.settings._parse_color` want ``#RRGGBB``. Anything
    ``int()`` rejects raises rather than being written — a colour that parsed as black
    would be a silent, plausible-looking wrong answer.
    """
    cleaned = raw.strip().removeprefix("#")
    try:
        value = int(cleaned, 16)
    except ValueError as e:
        raise SettingsImportError(f"{raw!r} is not a hex colour") from e
    if not 0 <= value <= 0xFFFFFF:
        raise SettingsImportError(f"{raw!r} is out of range for a colour")
    return f"#{value:06X}"


def _normalise_channel_id(raw: str) -> str:
    """A channel id as the page stores it: the decimal digits, or "0" for unset."""
    cleaned = raw.strip()
    if not cleaned:
        return "0"
    try:
        return str(int(cleaned))
    except ValueError as e:
        raise SettingsImportError(f"{raw!r} is not a channel id") from e


def _normalise_alert_level(raw: str) -> str:
    """A legacy alert level as the page stores it: upper-case, and one of the five.

    The old ``cfg`` upper-cased on read (``_resolve_level`` does ``.upper()``), so
    ``ALERT_MIN_LEVEL=warning`` is legal and has been working. Written through verbatim
    it matches no option in the page's ``<select>``, the browser falls back to showing
    the first one, and the next save of any setting on that page persists it.

    Anything not a level raises rather than being written, on the same reasoning as the
    colours: refusing while the env var still exists to be read beats a wrong value
    discovered from its effects weeks later. Note this is about what gets *stored* — the
    runtime read fails open, deliberately, and in the other direction (see
    ``settings.get_alert_min_level``).
    """
    resolved = logging.getLevelName(raw.strip().upper())
    if isinstance(resolved, int):
        # Round-trip through the number to land on the canonical spelling: Python
        # registers WARN and FATAL as aliases, both legal under the old cfg (which
        # resolved by name) and neither an option on the page. Rejecting them would
        # stop the cutover over a value that has been working for years.
        canonical = logging.getLevelName(resolved)
        if canonical in _ALERT_LEVELS:
            return canonical
    raise SettingsImportError(
        f"{raw!r} is not an alert level — expected one of " + ", ".join(_ALERT_LEVELS)
    )


def _normalise(slug: str, raw: str) -> str:
    if slug == "alert_min_level":
        return _normalise_alert_level(raw)
    if slug.endswith("_color"):
        return _normalise_color(raw)
    if slug.endswith("_channel_id") or slug.endswith("_channel"):
        return _normalise_channel_id(raw)
    return raw.strip()


def _followable_changes(
    rows: dict[str, tuple[bool | None, str | None]],
) -> list[Change]:
    """One change per catalog feed, from the ``FOLLOWABLES`` JSON blob.

    Keys in the blob that are not catalog feeds are *reported*, not dropped quietly:
    prod's blob still carries entries for feeds that no longer exist (``prime``,
    ``daily_reset``, ...) and the difference between "retired" and "renamed, and you
    just lost its channel" is not one to leave to a silent skip.
    """
    raw = os.environ.get("FOLLOWABLES", "").strip()
    if not raw:
        # One row per feed saying so, rather than an empty list. Returning [] printed a
        # report with no mention of the twelve channels at all, said "N setting(s) to
        # write … Written." and exited 0 — so a FOLLOWABLES defined on another service,
        # renamed, or deleted early read as a clean run, and the channel mapping was
        # gone with the variable. Absence is a finding, and this module's rule is that
        # findings are named.
        return [
            Change(
                slug=followable.channel_key,
                source="FOLLOWABLES",
                column="value",
                new="—",
                current=_render(rows.get(followable.channel_key), "value"),
                skip="FOLLOWABLES is not set in the environment",
            )
            for followable in dd_feeds.FOLLOWABLES
        ]
    try:
        blob = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SettingsImportError(f"FOLLOWABLES is not valid JSON: {e}") from e
    if not isinstance(blob, dict):
        raise SettingsImportError("FOLLOWABLES is not a JSON object")

    changes: list[Change] = []
    for followable in dd_feeds.FOLLOWABLES:
        if followable.slug not in blob:
            changes.append(
                Change(
                    slug=followable.channel_key,
                    source="FOLLOWABLES",
                    column="value",
                    new="—",
                    current=_render(rows.get(followable.channel_key), "value"),
                    skip="absent from FOLLOWABLES",
                )
            )
            continue
        changes.append(
            _change(
                slug=followable.channel_key,
                source="FOLLOWABLES",
                column="value",
                raw=str(blob[followable.slug]),
                rows=rows,
            )
        )

    for key in sorted(set(blob) - {f.slug for f in dd_feeds.FOLLOWABLES}):
        changes.append(
            Change(
                slug=f"{key}_channel",
                source="FOLLOWABLES",
                column="value",
                new=str(blob[key]),
                current=None,
                skip="not a feed in dd.common.feeds",
            )
        )
    return changes


def _render(row: tuple[bool | None, str | None] | None, column: str) -> str | None:
    if row is None:
        return None
    value = row[1] if column == "value" else row[0]
    return None if value is None else str(value)


def _change(
    *,
    slug: str,
    source: str,
    column: str,
    raw: str,
    rows: dict[str, tuple[bool | None, str | None]],
) -> Change:
    new = (
        _normalise(slug, raw)
        if column == "value"
        else str(raw.strip().lower() in cfg.TRUE_VALUES)
    )
    current = _render(rows.get(slug), column)
    skip = ""
    if current == new:
        # Includes a channel already stored as "0": rewriting 0 over 0 is a no-op, and
        # reporting it as a write would make a second run look like it had work to do.
        skip = "unchanged"
    # A stored "0" counts as set, and is deliberately NOT exempted below. It used to be,
    # on the reasoning that 0 means dormant-and-therefore-unset — but the branch above
    # already covers the 0-over-0 case that was about, and the exemption meant an
    # alerts channel somebody had cleared on purpose (the feed channels are unclearable,
    # that one is not) got refilled from the environment on the next run, against this
    # module's promise that a re-run is a no-op rather than a rollback.
    elif current is not None and current != "":
        skip = "already set — kept (--overwrite replaces)"
    return Change(
        slug=slug, source=source, column=column, new=new, current=current, skip=skip
    )


async def collect(
    rows: dict[str, tuple[bool | None, str | None]], *, overwrite: bool
) -> list[Change]:
    changes = _followable_changes(rows)
    for var, slug in _VALUE_VARS:
        raw = os.environ.get(var)
        if raw is None:
            changes.append(
                Change(
                    slug=slug,
                    source=var,
                    column="value",
                    new="—",
                    current=_render(rows.get(slug), "value"),
                    skip="not set in the environment",
                )
            )
            continue
        changes.append(
            _change(slug=slug, source=var, column="value", raw=raw, rows=rows)
        )

    var, slug = _BOOL_VAR
    raw = os.environ.get(var)
    if raw is None:
        changes.append(
            Change(
                slug=slug,
                source=var,
                column="enabled",
                new="—",
                current=_render(rows.get(slug), "enabled"),
                skip="not set in the environment",
            )
        )
    else:
        changes.append(
            _change(slug=slug, source=var, column="enabled", raw=raw, rows=rows)
        )

    if overwrite:
        changes = [
            c
            if not c.skip.startswith("already set")
            else Change(**{**vars(c), "skip": ""})
            for c in changes
        ]
    return changes


def format_report(changes: list[Change], *, execute: bool) -> str:
    width = max((len(c.slug) for c in changes), default=10)
    lines = [
        f"{'setting'.ljust(width)}  {'from':<22} {'current':<14} {'new':<24} note",
        f"{'-' * width}  {'-' * 22} {'-' * 14} {'-' * 24} ----",
    ]
    for c in changes:
        current = "(no row)" if c.current is None else c.current or "(empty)"
        lines.append(
            f"{c.slug.ljust(width)}  {c.source:<22} {current:<14} {c.new:<24} {c.skip}"
        )
    writing = [c for c in changes if c.writes]
    lines.append("")
    lines.append(
        f"{len(writing)} setting(s) to write, {len(changes) - len(writing)} skipped."
        + ("" if execute else "  DRY RUN — nothing written. Re-run with --execute.")
    )
    for var in _DROPPED_VARS:
        if os.environ.get(var):
            lines.append(
                f"note: {var} is set but no longer has a setting — Discord log "
                "forwarding was removed. Nothing to migrate."
            )
    return "\n".join(lines)


async def apply(changes: list[Change]) -> None:
    for change in changes:
        if not change.writes:
            continue
        if change.column == "enabled":
            await schemas.AutoPostSettings.set_enabled(
                change.slug, change.new == "True"
            )
        else:
            # set_value upserts the value column only, leaving `enabled` alone — so a
            # feed's toggle, which came across with the data copy, is not disturbed by
            # writing its channel.
            await schemas.AutoPostSettings.set_value(change.slug, change.new)


async def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m dd.common.settings_import",
        description="Copy the legacy env-var settings into the DB-backed settings.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="actually write. Without this the run is a dry run.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace rows that already hold a value (default is to leave them).",
    )
    args = parser.parse_args(argv)

    await schemas.wait_for_db()
    rows = await schemas.AutoPostSettings.get_all_rows()
    changes = await collect(rows, overwrite=args.overwrite)
    # `changes` is never empty — an unset variable still produces a skip row saying so —
    # so testing it told us nothing, and the run this guard exists to catch (no legacy
    # environment at all) printed its table, said "Written." and exited 0 having written
    # nothing, with the cutover chain carrying on as though the step had succeeded.
    if not any(c.writes for c in changes):
        if any(c.current not in (None, "") for c in changes):
            print(format_report(changes, execute=False))
            print("\nNothing to do: every setting is already imported.")
            return 0
        print("Nothing to import: none of the legacy variables are set here.")
        print("Is this running under `railway run` against the old deployment?")
        return 1

    print(format_report(changes, execute=args.execute))
    if args.execute:
        await apply(changes)
        print("\nWritten. Reload the Autopost Settings page to confirm.")
    return 0


def main() -> int:
    try:
        return asyncio.run(_main())
    except SettingsImportError as e:
        print(f"settings_import: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
