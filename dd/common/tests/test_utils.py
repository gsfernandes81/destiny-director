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

# Pure-logic unit tests for dd.common.utils — no database or network.

import logging
import typing as t

import hikari as h
import pytest

from dd.common import utils
from dd.common.utils import (
    FriendlyValueError,
    accumulate,
    check_number_of_layers,
    construct_emoji_substituter,
    ensure_session,
    get_ordinal_suffix,
    guild_scope,
    re_user_side_emoji,
    substitute_guild_emoji,
)
from dd.hmessage import HMessage

# --- guild_scope ---------------------------------------------------------------


def test_guild_scope_drops_the_zero_sentinel():
    assert guild_scope(0, 123) == [123]


def test_guild_scope_dedupes_preserving_order():
    assert guild_scope(3, 1, 3, 2) == [3, 1, 2]


def test_guild_scope_keeps_nonzero_sentinels():
    assert guild_scope(-1) == [-1]


def test_guild_scope_raises_when_empty_after_dropping_zero():
    with pytest.raises(ValueError):
        guild_scope(0)


# --- get_ordinal_suffix --------------------------------------------------------


@pytest.mark.parametrize(
    ("day", "suffix"),
    [
        (1, "st"),
        (2, "nd"),
        (3, "rd"),
        (4, "th"),
        (10, "th"),
        (11, "th"),
        (12, "th"),
        (13, "th"),
        (21, "st"),
        (22, "nd"),
        (23, "rd"),
        (31, "st"),
    ],
)
def test_get_ordinal_suffix(day: int, suffix: str):
    assert get_ordinal_suffix(day) == suffix


# --- accumulate ----------------------------------------------------------------


def test_accumulate_sums_numbers():
    assert accumulate([1, 2, 3]) == 6


def test_accumulate_concatenates_strings():
    assert accumulate(["a", "b", "c"]) == "abc"


def test_accumulate_empty_raises_without_default():
    with pytest.raises(ValueError):
        accumulate([])


def test_accumulate_empty_returns_empty_value():
    assert accumulate([], 0) == 0


# --- check_number_of_layers ----------------------------------------------------


@pytest.mark.parametrize("layers", [["a"], ["a", "b"], ["a", "b", "c"], 1, 3])
def test_check_number_of_layers_accepts_one_to_three(layers: t.Sequence[str] | int):
    check_number_of_layers(layers)  # must not raise


def test_check_number_of_layers_too_many_raises_friendly():
    with pytest.raises(FriendlyValueError):
        check_number_of_layers(["a", "b", "c", "d"])


def test_check_number_of_layers_too_few_raises_plain_value_error():
    with pytest.raises(ValueError) as exc:
        check_number_of_layers([])
    assert not isinstance(exc.value, FriendlyValueError)


# --- construct_emoji_substituter -----------------------------------------------


def test_emoji_substituter_replaces_known_name():
    sub = construct_emoji_substituter({"smile": "<:smile:1>"})
    assert re_user_side_emoji.sub(sub, ":smile:") == "<:smile:1>"


def test_emoji_substituter_is_case_insensitive_fallback():
    sub = construct_emoji_substituter({"smile": "<:smile:1>"})
    assert re_user_side_emoji.sub(sub, ":SMILE:") == "<:smile:1>"


def test_emoji_substituter_leaves_unknown_untouched():
    sub = construct_emoji_substituter({"smile": "<:smile:1>"})
    assert re_user_side_emoji.sub(sub, ":unknown:") == ":unknown:"


# --- substitute_guild_emoji (adapter over HMessage.map_text) --------------------


def test_substitute_guild_emoji_resolves_across_surfaces():
    hmsg = HMessage(content="hi :smile:", embeds=[h.Embed(description="d :smile:")])
    assert substitute_guild_emoji(hmsg, {"smile": "<:smile:1>"}) is hmsg
    assert hmsg.content == "hi <:smile:1>"
    assert hmsg.embeds[0].description == "d <:smile:1>"


def test_substitute_guild_emoji_leaves_qualified_mention():
    hmsg = HMessage(content="already <:smile:99>")
    substitute_guild_emoji(hmsg, {"smile": "<:smile:1>"})
    assert hmsg.content == "already <:smile:99>"


# --- ensure_session ------------------------------------------------------------


class _FakeSession:
    def __init__(self) -> None:
        self.begun = False

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False

    def begin(self) -> "_FakeSession":
        self.begun = True
        return self


class _FakeSessionmaker:
    def __init__(self) -> None:
        self.session = _FakeSession()
        self.called = False

    def __call__(self) -> _FakeSession:
        self.called = True
        return self.session


@pytest.mark.asyncio
async def test_ensure_session_creates_when_absent():
    maker = _FakeSessionmaker()

    @ensure_session(maker)
    async def fn(*, session: t.Any) -> t.Any:
        return session

    result = await fn()
    assert maker.called
    assert maker.session.begun
    assert result is maker.session


@pytest.mark.asyncio
async def test_ensure_session_uses_provided_session():
    maker = _FakeSessionmaker()
    provided = object()

    @ensure_session(maker)
    async def fn(*, session: t.Any) -> t.Any:
        return session

    result = await fn(session=provided)
    assert result is provided
    assert not maker.called


def test_ensure_session_lives_on_utils():
    # Guards the import path the schema models rely on.
    assert callable(utils.ensure_session)


# --- discord_error_logger level flag (escalate to a clean pinging alert) --------


class _CapturingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def dd_error_records():
    handler = _CapturingHandler()
    logger = logging.getLogger("dd.error")
    logger.addHandler(handler)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)


@pytest.mark.asyncio
async def test_error_logger_critical_flag_pings_clean_without_traceback(
    dd_error_records,
):
    code = await utils.discord_error_logger(
        ValueError("Xûr post truncated"),
        operation="Xûr autopost",
        level=logging.CRITICAL,
    )
    (record,) = dd_error_records
    assert record.levelno == logging.CRITICAL  # escalated -> owner ping
    assert record.exc_info is None  # no traceback -> renders as an alert, not an error
    assert record.getMessage() == "Xûr post truncated"
    # The header code (stamped on the record) matches the returned code.
    assert getattr(record, "dd_reference", None) == code
    assert record.__dict__.get("dd_operation") == "Xûr autopost"


@pytest.mark.asyncio
async def test_error_logger_default_level_keeps_the_traceback(dd_error_records):
    await utils.discord_error_logger(ValueError("boom"), operation="Mirror update")
    (record,) = dd_error_records
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None  # real error keeps its traceback
