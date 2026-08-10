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

"""Snapshotting Railway's variables, and putting them back.

The parser is the interesting half. It is fed a paste of the web Raw Editor, and the
values it mishandles are exactly the ones nobody would notice: a Google service-account
key that comes back with real newlines instead of the literal ``\\n`` it needs, or a
``FOLLOWABLES`` blob whose quotes were stripped one level too far. Both still look like
strings, and both are wrong.
"""

import io
import json
import pathlib

import pytest

from dd.common import railway_vars as rv

#: A verbatim slice of the real Raw Editor output for anchor, ids intact — the format is
#: what is under test, and it is not obvious: values are JSON-encoded, so the inner
#: quotes of FOLLOWABLES arrive escaped, and references arrive unresolved.
RAW = r"""ENABLE_ALPINE_PRIVATE_NETWORKING="true"
FOLLOWABLES="{\"ada\":1093258790817779762,\"xur\":775807486066032700,\"portal_ops\":1519455338422734989}"
KYBER_DISCORD_SERVER_ID="${{shared.KYBER_DISCORD_SERVER_ID}}"
LOG_CHANNEL_ID="${{shared.LOG_CHANNEL_ID}}"
MYSQL_SSL="false"
MYSQL_URL="${{MySQL.MYSQL_URL}}"
"""


def test_the_raw_editors_quoting_round_trips() -> None:
    variables = rv.parse_raw(RAW)

    assert variables["ENABLE_ALPINE_PRIVATE_NETWORKING"] == "true"
    assert variables["MYSQL_URL"] == "${{MySQL.MYSQL_URL}}"
    # The inner quotes come back as quotes — not doubled, not stripped.
    assert json.loads(variables["FOLLOWABLES"])["xur"] == 775807486066032700


def test_a_backslash_escape_in_a_value_is_not_turned_into_a_newline() -> None:
    # SHEETS_PRIVATE_KEY holds a PEM whose newlines are literal backslash-n, and the
    # editor JSON-encodes them as \\n. Decoding by hand would either produce real
    # newlines (breaking the key) or leave the doubled backslash. Only proper JSON
    # decoding gives back the two characters the value actually had.
    raw = (
        r'SHEETS_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\\nMIIEvQ\\n-----END-----\\n"'
    )

    value = rv.parse_raw(raw)["SHEETS_PRIVATE_KEY"]

    assert "\n" not in value
    assert value.count("\\n") == 3


def test_an_unquoted_value_is_taken_verbatim() -> None:
    # So a hand-written or hand-edited file still works.
    assert rv.parse_raw("FOO=bar\n") == {"FOO": "bar"}


def test_blank_lines_and_comments_are_ignored() -> None:
    assert rv.parse_raw('\n# a note\n\nFOO="bar"\n') == {"FOO": "bar"}


def test_a_line_without_an_equals_stops_the_parse() -> None:
    with pytest.raises(rv.RailwayVarsError):
        rv.parse_raw('FOO="bar"\nthis is not a variable\n')


def test_a_mangled_followables_stops_the_parse() -> None:
    # The self-check: FOLLOWABLES is ours and is always JSON, so if it does not survive
    # parsing then the quoting was misread — and a half-working parser here writes
    # plausible nonsense into a live environment.
    with pytest.raises(rv.RailwayVarsError, match="FOLLOWABLES"):
        rv.parse_raw('FOLLOWABLES="{not json at all}"\n')


def test_an_empty_file_is_an_error_not_an_empty_restore() -> None:
    with pytest.raises(rv.RailwayVarsError):
        rv.parse_raw("\n\n# only comments\n")


# --- what gets written back ----------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        # A reference re-links when written, so it can never go stale — restore it
        # whatever it is called, including the database URLs.
        ("DATABASE_URL", "${{Postgres.DATABASE_PUBLIC_URL}}", True),
        ("MYSQL_URL", "${{MySQL.MYSQL_URL}}", True),
        ("SHEETS_CLIENT_ID", "${{shared.SHEETS_CLIENT_ID}}", True),
        # A flattened literal pins a host:port that Railway reassigns when a database
        # service is recreated. Absent fails at boot; stale fails at connect, later.
        (
            "DATABASE_URL",
            "postgresql://u:p@tokaido.proxy.rlwy.net:50841/railway",
            False,
        ),
        ("MYSQL_PRIVATE_URL", "mysql://u:p@mysql.railway.internal:3306/railway", False),
        # Same prefix, not a URL: a prefix-only rule dropped this one.
        ("MYSQL_SSL", "false", True),
        ("DATABASE_SSL", "false", True),
        # Injected per deploy; setting it is meaningless.
        ("RAILWAY_PROJECT_ID", "abc123", False),
        # The ordinary case.
        ("FOLLOWABLES", '{"xur":1}', True),
    ],
)
def test_restorability_is_decided_by_shape_not_just_name(
    name: str, value: str, expected: bool
) -> None:
    allowed, why = rv.restorable(name, value)

    assert allowed is expected
    assert (why == "") is expected  # a refusal always says why


def test_a_plan_marks_unchanged_values_rather_than_rewriting_them() -> None:
    snapshot = {
        "services": {"anchor": {"FOO": "same", "BAR": "new", "RAILWAY_X": "no"}}
    }
    live = {"anchor": {"FOO": "same", "BAR": "old"}}

    plan = rv.plan_restore(snapshot, live)["anchor"]

    assert plan["FOO"][0] == "same"
    assert plan["BAR"][0] == "set"
    assert plan["RAILWAY_X"][0] == "skipped"


def test_the_plan_report_never_prints_a_value() -> None:
    # It runs in a terminal and every value is a secret.
    snapshot = {"services": {"anchor": {"TOKEN": "hunter2"}}}

    report = rv._format_plan(rv.plan_restore(snapshot, {"anchor": {}}), execute=False)

    assert "TOKEN" in report
    assert "hunter2" not in report


# --- reading the paste ---------------------------------------------------------------


def test_a_dash_reads_the_paste_from_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The intended path: you are copying out of a browser, so a file would exist only to
    # be deleted afterwards.
    monkeypatch.setattr("sys.stdin", io.StringIO(RAW))

    text, label = rv._read_raw_source("-")

    assert rv.parse_raw(text)["MYSQL_SSL"] == "false"
    assert label == "the paste"


def test_the_paste_prompt_goes_to_stderr_so_piping_still_works(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", _Tty(RAW))

    rv._read_raw_source("-")

    captured = capsys.readouterr()
    assert "Raw Editor" in captured.err
    assert captured.out == ""


def test_a_file_is_still_accepted(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "raw.txt"
    path.write_text(RAW)

    text, label = rv._read_raw_source(str(path))

    assert rv.parse_raw(text)["MYSQL_SSL"] == "false"
    assert label == "raw.txt"


def test_a_line_truncated_by_the_terminal_is_rejected() -> None:
    # A terminal in canonical mode truncates any line over 4096 bytes. The longest line
    # here is ~1800, so there is headroom — but a truncated one loses its closing quote,
    # and being rejected beats writing half a private key.
    truncated = 'SHEETS_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\\nMIIEvQIBADAN'

    with pytest.raises(rv.RailwayVarsError, match="not valid JSON"):
        rv.parse_raw(truncated)
