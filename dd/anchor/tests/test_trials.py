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

"""Unit tests for the ``trials`` extension's pure logic (no Discord I/O).

Exercises the ``Live until`` reset maths, the exact post-body renderer,
(de)serialisation, the carried-over draft build, validation, the server-side
``_context_from_payload`` (maps split + focus-pool resolution), the shared
publish/route path wired through this producer's spec, and the preview renderer's H3 /
bullet handling. Auth is enforced centrally by the web_auth middleware (see
test_web_auth.py), so there are no session tests here.
"""

import json
import types
import typing as t

import aiohttp.web
import pytest

from dd.anchor import (
    hybrid_post_core as hpc,
)
from dd.anchor.extensions import trials as tr

# ---------------------------------------------------------------------------
# build_body + "Live until" reset maths
# ---------------------------------------------------------------------------

# A known Tuesday 17:00 UTC boundary (the same convention weekly_reset is anchored to).
SAMPLE_RESET = 1783702800


async def _seed_default_loot_rotation() -> None:
    """Pin the shared-DB ``trials_loot`` row to the baked default loop.

    The session DB is shared across test files; test_rotation_editor.py writes a
    ``trials_loot`` doc, so tests here that exercise the loot cursor explicitly reset
    the row to the default (which expands to :data:`tr.DEFAULT_LOOT_SETS`).
    """
    await tr.schemas.RotationData.set_data(
        tr.LOOT_SLUG, tr.rotation_schema.trials_loot_default_doc()
    )


def test_live_until_is_the_reset_ts() -> None:
    # reset_ts IS the live-until boundary now — rendered directly, not next_reset_ts.
    ctx = tr.TrialsContext(reset_ts=SAMPLE_RESET)
    body = tr.build_body(ctx)
    assert f"Live until <t:{SAMPLE_RESET}:f>" in body


def test_build_body_exact_format(monkeypatch) -> None:
    # The guild has a scout_rifle emoji but not (say) a fusion one, so the linked scout
    # gets its type icon and the other weapon falls back to :weapon:.
    monkeypatch.setattr(tr, "_weapon_emoji_names", frozenset({"scout_rifle", "weapon"}))
    ctx = tr.TrialsContext(
        reset_ts=SAMPLE_RESET,
        featured_maps=["Burnout", "Widow's Court", "Endless Vale"],
        focus_pool=[
            tr.WeaponRef("The Scholar", 123, "scout_rifle"),
            tr.WeaponRef("Exile's Curse"),
        ],
    )
    lines = tr.build_body(ctx).split("\n")
    assert lines[0] == "# [Trials *of* Osiris](https://kyber3000.com/Trialspost)"
    assert lines[1] == ""
    assert lines[2] == f"Live until <t:{SAMPLE_RESET}:f>"
    assert lines[3] == "### Featured Maps"
    assert "- Burnout" in lines and "- Widow's Court" in lines
    assert "### Rewards" in lines
    assert "All Trials weapons available" in lines
    assert "Weapon Attunement available" in lines
    assert "**This Week's Bonus Focus Pool**" in lines
    # Type emoji when the guild has it (+ light.gg link); generic :weapon: otherwise. No
    # "- " bullet — the emoji is the marker.
    assert ":scout_rifle: [The Scholar](https://light.gg/db/items/123)" in lines
    assert ":weapon: Exile's Curse" in lines
    assert lines[-1] == "### Good luck in your games!  :gscheer:"


def test_build_body_hides_empty_optional_sections() -> None:
    # No maps -> no Featured Maps header; no focus pool -> no Focus Pool header. Rewards
    # (static) and the footer are always present.
    only_maps = tr.build_body(tr.TrialsContext(reset_ts=1, featured_maps=["Burnout"]))
    assert "### Featured Maps" in only_maps
    assert "Bonus Focus Pool" not in only_maps

    only_pool = tr.build_body(
        tr.TrialsContext(reset_ts=1, focus_pool=[tr.WeaponRef("The Scholar")])
    )
    assert "### Featured Maps" not in only_pool
    assert "**This Week's Bonus Focus Pool**" in only_pool

    both_empty = tr.build_body(tr.TrialsContext(reset_ts=1))
    assert "### Rewards" in both_empty
    assert both_empty.rstrip().endswith("### Good luck in your games!  :gscheer:")


def test_focus_pool_emoji_bow_aliases_to_combat_bow(monkeypatch) -> None:
    # The guild has no "bow" emoji but has "combat_bow": a bow slug uses that alias, and
    # a type with neither its slug nor an alias falls back to the generic :weapon:.
    monkeypatch.setattr(
        tr, "_weapon_emoji_names", frozenset({"combat_bow", "scout_rifle", "weapon"})
    )
    ctx = tr.TrialsContext(
        reset_ts=1,
        focus_pool=[
            tr.WeaponRef("Wish-Keeper", 7, "bow"),  # alias -> combat_bow
            tr.WeaponRef("Edge of Action", 8, "glaive"),  # absent, no alias -> weapon
            tr.WeaponRef("The Scholar", 9, "scout_rifle"),  # present -> itself
        ],
    )
    body = tr.build_body(ctx)
    assert ":combat_bow: [Wish-Keeper](https://light.gg/db/items/7)" in body
    assert ":weapon: [Edge of Action](https://light.gg/db/items/8)" in body
    assert ":scout_rifle: [The Scholar](https://light.gg/db/items/9)" in body


# ---------------------------------------------------------------------------
# (de)serialisation + carried-over draft
# ---------------------------------------------------------------------------


def test_context_round_trip() -> None:
    ctx = tr.TrialsContext(
        reset_ts=SAMPLE_RESET,
        featured_maps=["A", "B"],
        focus_pool=[tr.WeaponRef("W", 1, "scout_rifle"), tr.WeaponRef("V")],
        image_url="https://x/y.png",
        notes=["n1"],
    )
    assert tr.TrialsContext.from_dict(ctx.to_dict()) == ctx


def test_config_round_trip_and_default_cursor() -> None:
    # A blank config is the "none used yet" cursor; the loop itself lives in the editor
    # doc now (not on the config), so the config carries only the cursor + carry-overs.
    fresh = tr.TrialsConfig.from_dict(None)
    assert fresh.last_loot_set_index == -1
    assert not hasattr(fresh, "loot_sets")
    config = tr.TrialsConfig(
        default_image_url="https://img",
        last_featured_maps=["Burnout"],
        last_loot_set_index=1,
    )
    assert tr.TrialsConfig.from_dict(config.to_dict()) == config


def test_next_and_match_in_rotation() -> None:
    rotation = [["A", "B"], ["C", "D"], ["E"]]
    assert tr._next_in_rotation(rotation, -1) == ["A", "B"]  # first draft -> set 0
    assert tr._next_in_rotation(rotation, 0) == ["C", "D"]
    assert tr._next_in_rotation(rotation, 2) == ["A", "B"]  # wraps
    assert tr._next_in_rotation([], 0) == []  # empty loop -> no default
    # match is order-insensitive + case-insensitive; a non-set returns None.
    assert tr._match_in_rotation(rotation, ["d", "c"]) == 1
    assert tr._match_in_rotation(rotation, ["A", "X"]) is None


def test_expand_loot_rotation_from_doc_and_strips_type() -> None:
    # A stored trials_loot doc expands schedule -> ordered weapon-name lists, dropping a
    # schedule name that no set defines, and strips the editor's " (Type)" suffix so the
    # names resolve as bare manifest names.
    doc = {
        "sets": [
            {
                "name": "Pool A",
                "weapons": ["The Immortal (Submachine Gun)", "Eye of Sol"],
            },
            {"name": "Pool B", "weapons": ["The Scholar"]},
        ],
        "schedule": ["Pool B", "Pool A", "Ghost Pool"],
    }
    assert tr._expand_loot_rotation(doc) == [
        ["The Scholar"],
        ["The Immortal", "Eye of Sol"],
    ]
    # An absent/empty doc falls back to the baked default loop.
    assert tr._expand_loot_rotation(None) == [list(s) for s in tr.DEFAULT_LOOT_SETS]
    assert tr._expand_loot_rotation({"sets": [], "schedule": []}) == [
        list(s) for s in tr.DEFAULT_LOOT_SETS
    ]


@pytest.mark.asyncio
async def test_build_draft_context_defaults_to_next_loot_set(stub_weapon_items) -> None:
    await _seed_default_loot_rotation()
    # last used = set 0 (Pool 1) -> the draft defaults to set 1 (Pool 2), linked.
    config = tr.TrialsConfig(
        default_image_url="https://img",
        last_featured_maps=["Burnout"],
        last_loot_set_index=0,
    )
    ctx = await tr.build_draft_context(config)
    assert ctx.reset_ts == hpc.next_reset_ts(hpc.current_reset_ts())
    assert ctx.featured_maps == ["Burnout"]
    assert [w.name for w in ctx.focus_pool] == list(tr.DEFAULT_LOOT_SETS[1])
    assert ctx.image_url == "https://img"


@pytest.mark.asyncio
async def test_form_loot_sets_resolves_and_marks_current(stub_weapon_items) -> None:
    # The form's set-card picker: named sets resolved to manifest weapons (type
    # suffix stripped, hash linked when known), plus the set the cursor points at as
    # "current" — mirroring the schedule filtering the producer's default uses.
    await tr.schemas.RotationData.set_data(
        tr.LOOT_SLUG,
        {
            "sets": [
                {
                    "name": "Pool A",
                    "weapons": ["The Scholar (Scout Rifle)", "Sola's Scar"],
                },
                {"name": "Pool B", "weapons": ["Exile's Curse"]},
            ],
            "schedule": ["Pool A", "Pool B"],
        },
    )
    await tr.save_config(tr.TrialsConfig(last_loot_set_index=0))  # next = schedule[1]
    sets, current = await tr._form_loot_sets()
    assert current == "Pool B"
    by_name = {s["name"]: s for s in sets}
    a = by_name["Pool A"]["weapons"]
    assert a[0]["name"] == "The Scholar" and a[0]["hash"] == 123  # stripped + linked
    assert a[1]["name"] == "Sola's Scar" and a[1]["hash"] is None  # hand-typed, no link
    assert by_name["Pool B"]["weapons"][0]["hash"] == 456
    await _seed_default_loot_rotation()  # shared DB — restore for cursor tests


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def test_validate_flags_empty_post() -> None:
    problems = tr.validate_post(tr.TrialsContext(reset_ts=1))
    assert any("empty" in p for p in problems)


def test_validate_ok_with_a_single_map(configured_followables: dict[str, int]) -> None:
    # configured_followables: "nowhere to publish" is itself a validation problem now
    # that a followable's channel comes from its DB row alone.
    assert (
        tr.validate_post(tr.TrialsContext(reset_ts=1, featured_maps=["Burnout"])) == []
    )


def test_validate_rejects_bad_image_url() -> None:
    problems = tr.validate_post(
        tr.TrialsContext(reset_ts=1, featured_maps=["X"], image_url="not-a-url")
    )
    assert any("http" in p for p in problems)


def test_validate_flags_overlong_post() -> None:
    ctx = tr.TrialsContext(reset_ts=1, featured_maps=["x" * 5000])
    assert any("too long" in p for p in tr.validate_post(ctx))


# ---------------------------------------------------------------------------
# server-side context from the form payload
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_weapon_items():
    # get_weapon_items() now delegates to the shared, process-wide hybrid_post_core
    # cache; seed that so resolve_weapon sees a known pool.
    saved = hpc._weapon_pool
    hpc._weapon_pool = [
        ("The Scholar", 123, "Scout Rifle", 3, "Legendary"),
        ("Exile's Curse", 456, "Fusion Rifle", 3, "Legendary"),
    ]
    yield
    hpc._weapon_pool = saved


@pytest.mark.asyncio
async def test_context_from_payload_splits_and_resolves(stub_weapon_items) -> None:
    ctx = await tr._context_from_payload(
        {
            "reset_ts": SAMPLE_RESET,
            "maps_text": "Burnout\n  Widow's Court \n\n",
            "focus_pool": ["123", "Sola's Scar", "456"],
            "image_url": "  https://img/y.png  ",
            "notes_text": "note1\n\n",
        }
    )
    assert ctx.reset_ts == SAMPLE_RESET
    assert ctx.featured_maps == ["Burnout", "Widow's Court"]
    # by hash -> linked; free text -> hash-less; by hash -> linked.
    assert ctx.focus_pool[0].name == "The Scholar" and ctx.focus_pool[0].hash == 123
    assert ctx.focus_pool[1].name == "Sola's Scar" and ctx.focus_pool[1].hash is None
    assert ctx.focus_pool[2].name == "Exile's Curse" and ctx.focus_pool[2].hash == 456
    assert ctx.image_url == "https://img/y.png"
    assert ctx.notes == ["note1"]


@pytest.mark.asyncio
async def test_context_from_payload_defaults_reset(stub_weapon_items) -> None:
    ctx = await tr._context_from_payload({"maps_text": "Burnout"})
    assert ctx.reset_ts == hpc.next_reset_ts(hpc.current_reset_ts())


# ---------------------------------------------------------------------------
# publish / route lifecycle (fake bot + fake request)
# ---------------------------------------------------------------------------


class _FakeRest:
    def __init__(self) -> None:
        self.edited: list[tuple[t.Any, int]] = []
        self.deleted: list[tuple[t.Any, int]] = []

    async def edit_message(self, channel: t.Any, message: int, **kwargs: t.Any) -> None:
        self.edited.append((channel, message))

    async def delete_message(self, channel: t.Any, message: int) -> None:
        self.deleted.append((channel, message))


class _FakeBot:
    def __init__(self) -> None:
        self.rest = _FakeRest()


def _bot(fake: _FakeBot) -> hpc.CachedFetchBot:
    return t.cast(hpc.CachedFetchBot, fake)


@pytest.fixture
def fake_publish_env(
    monkeypatch: pytest.MonkeyPatch, configured_followables: dict[str, int]
):
    """Stub render + send/crosspost so the shared publish branches are testable.

    ``format_trials`` (late-bound by the spec) returns a dummy bundle; the ``utils``
    send/crosspost primitives — shared with the core via one module object — record
    calls instead of hitting Discord.

    Depends on ``configured_followables`` because publishing needs somewhere to
    publish: a followable's channel comes from its DB row alone now, so an unconfigured
    feed fails ``spec.validate`` before any of these stubs is reached.
    """
    sent: list[dict[str, t.Any]] = []
    crossposted: list[tuple[t.Any, int]] = []

    async def fake_format(ctx: t.Any, bot: t.Any) -> t.Any:
        return types.SimpleNamespace(components=["cv2"])

    async def fake_send(
        bot: t.Any,
        msg_proto: t.Any,
        channel_id: int,
        crosspost: bool = True,
        deduplicate: bool = False,
    ) -> t.Any:
        sent.append({"channel": channel_id, "crosspost": crosspost})
        return types.SimpleNamespace(id=555)

    async def fake_crosspost(
        bot: t.Any, channel: t.Any, message_id: int, **_kwargs: t.Any
    ) -> None:
        crossposted.append((channel, message_id))

    monkeypatch.setattr(tr, "format_trials", fake_format)
    monkeypatch.setattr(hpc.utils, "send_message", fake_send)
    monkeypatch.setattr(hpc.utils, "crosspost_message_with_retries", fake_crosspost)
    return types.SimpleNamespace(sent=sent, crossposted=crossposted)


class _FakeRequest:
    def __init__(self, *, body: t.Any = None) -> None:
        self.query: dict[str, str] = {}
        self.cookies: dict[str, str] = {}
        self.headers: dict[str, str] = {}
        self._body = body

    async def json(self) -> t.Any:
        return self._body


def _req(**kwargs: t.Any) -> aiohttp.web.Request:
    return t.cast(aiohttp.web.Request, _FakeRequest(**kwargs))


def _ctx() -> tr.TrialsContext:
    return tr.TrialsContext(reset_ts=SAMPLE_RESET, featured_maps=["Burnout"])


@pytest.mark.asyncio
async def test_post_or_edit_unpublished_creates_then_edits(fake_publish_env) -> None:
    bot = _FakeBot()
    channel = tr.settings.get_followable_channel_sync("trials")
    meta = await hpc.post_or_edit_unpublished(
        tr._SPEC, _bot(bot), _ctx(), tr.DraftMeta()
    )
    assert fake_publish_env.sent == [{"channel": channel, "crosspost": False}]
    assert meta.message_id == 555 and meta.status == "posted"

    await hpc.post_or_edit_unpublished(
        tr._SPEC, _bot(bot), _ctx(), tr.DraftMeta(message_id=42, status="posted")
    )
    assert bot.rest.edited == [(channel, 42)]


@pytest.mark.asyncio
async def test_publish_draft_edits_then_crossposts(fake_publish_env) -> None:
    bot = _FakeBot()
    channel = tr.settings.get_followable_channel_sync("trials")
    meta = tr.DraftMeta(message_id=42, status="posted", crossposted=False)
    out, note = await hpc.publish_draft(tr._SPEC, _bot(bot), _ctx(), meta)
    assert bot.rest.edited == [(channel, 42)]
    assert fake_publish_env.crossposted == [(channel, 42)]
    assert out.crossposted is True and out.status == "published"
    assert "Published and crossposted" in note


@pytest.mark.asyncio
async def test_publish_draft_raises_on_invalid(fake_publish_env) -> None:
    bot = _FakeBot()
    with pytest.raises(ValueError):
        await hpc.publish_draft(
            tr._SPEC, _bot(bot), tr.TrialsContext(reset_ts=1), tr.DraftMeta()
        )
    assert fake_publish_env.sent == [] and fake_publish_env.crossposted == []


@pytest.mark.asyncio
async def test_handle_create_posts_and_returns_warnings(
    monkeypatch, fake_publish_env, stub_weapon_items
) -> None:
    monkeypatch.setattr(tr, "_bot", _FakeBot())
    await tr.save_meta(tr.DraftMeta())  # fresh: no post this period
    # An empty draft trips validate_post, but Create still posts it — the problems come
    # back as non-blocking warnings, not a 422; the post is stamped as this period's.
    resp = await tr._handle_create(_req(body={"reset_ts": SAMPLE_RESET}))
    assert resp.status == 200
    data = json.loads(resp.text or "")
    assert data["ok"] is True and data["warnings"]
    assert data["post_this_period"] is True and data["crossposted"] is False
    channel = tr.settings.get_followable_channel_sync("trials")
    assert fake_publish_env.sent == [{"channel": channel, "crosspost": False}]
    meta = await tr.load_meta()
    assert meta.message_id == 555 and meta.status == "posted"
    assert meta.reset_ts == tr.current_reset_ts()  # stamped to the real period


@pytest.mark.asyncio
async def test_handle_create_refuses_when_post_exists(
    monkeypatch, stub_weapon_items
) -> None:
    monkeypatch.setattr(tr, "_bot", _FakeBot())
    # A live post stamped with the current period is "current" — Create is refused.
    await tr.save_meta(
        tr.DraftMeta(message_id=42, reset_ts=tr.current_reset_ts(), status="posted")
    )
    resp = await tr._handle_create(_req(body={"reset_ts": SAMPLE_RESET}))
    assert resp.status == 409
    assert "already exists" in json.loads(resp.text or "")["error"]


@pytest.mark.asyncio
async def test_handle_create_publish_crossposts(
    monkeypatch, fake_publish_env, stub_weapon_items
) -> None:
    monkeypatch.setattr(tr, "_bot", _FakeBot())
    await tr.save_meta(tr.DraftMeta())
    resp = await tr._handle_create(
        _req(body={"reset_ts": SAMPLE_RESET, "maps_text": "Burnout", "publish": True})
    )
    assert resp.status == 200 and json.loads(resp.text or "")["crossposted"] is True
    channel = tr.settings.get_followable_channel_sync("trials")
    assert fake_publish_env.crossposted == [(channel, 555)]


@pytest.mark.asyncio
async def test_handle_edit_edits_existing_in_place(
    monkeypatch, fake_publish_env, stub_weapon_items
) -> None:
    bot = _FakeBot()
    monkeypatch.setattr(tr, "_bot", bot)
    await tr.save_meta(
        tr.DraftMeta(message_id=42, reset_ts=tr.current_reset_ts(), status="posted")
    )
    resp = await tr._handle_edit(
        _req(body={"reset_ts": SAMPLE_RESET, "maps_text": "Burnout"})
    )
    assert resp.status == 200
    data = json.loads(resp.text or "")
    assert data["ok"] is True and data["post_this_period"] is True
    channel = tr.settings.get_followable_channel_sync("trials")
    assert bot.rest.edited == [(channel, 42)] and fake_publish_env.sent == []


@pytest.mark.asyncio
async def test_handle_edit_refuses_when_absent(monkeypatch, stub_weapon_items) -> None:
    monkeypatch.setattr(tr, "_bot", _FakeBot())
    await tr.save_meta(tr.DraftMeta())  # no post this period
    resp = await tr._handle_edit(_req(body={"reset_ts": SAMPLE_RESET}))
    assert resp.status == 409
    assert "No Trials post" in json.loads(resp.text or "")["error"]


@pytest.mark.asyncio
async def test_create_carries_maps_but_unpublished_does_not_advance_cursor(
    monkeypatch, fake_publish_env, stub_weapon_items
) -> None:
    monkeypatch.setattr(tr, "_bot", _FakeBot())
    await tr.save_config(tr.TrialsConfig())  # reset the shared-DB rotation state (-1)
    await _seed_default_loot_rotation()
    await tr.save_meta(tr.DraftMeta())
    # An UNCROSSPOSTED create carries the maps over but must NOT advance the loot cursor
    # (this is the Iron-Banner-seed-then-delete path — it can't consume a set).
    await tr._handle_create(
        _req(
            body={
                "reset_ts": SAMPLE_RESET,
                "maps_text": "Burnout",
                "focus_pool": list(tr.DEFAULT_LOOT_SETS[1]),
            }
        )
    )
    config = await tr.load_config()
    assert config.last_featured_maps == ["Burnout"]
    assert config.last_loot_set_index == -1


@pytest.mark.asyncio
async def test_publish_advances_cursor_to_matched_set(
    monkeypatch, fake_publish_env, stub_weapon_items
) -> None:
    monkeypatch.setattr(tr, "_bot", _FakeBot())
    await tr.save_config(tr.TrialsConfig())  # cursor -1
    await _seed_default_loot_rotation()
    await tr.save_meta(tr.DraftMeta())
    # Create-&-publish a post whose pool is exactly Pool 2 (index 1) — the crosspost
    # transition advances the cursor to that set, so the next draft defaults to Pool 3.
    await tr._handle_create(
        _req(
            body={
                "reset_ts": SAMPLE_RESET,
                "focus_pool": list(tr.DEFAULT_LOOT_SETS[1]),
                "publish": True,
            }
        )
    )
    config = await tr.load_config()
    assert config.last_loot_set_index == 1
    rotation = await tr.load_loot_rotation()
    assert tr._next_in_rotation(rotation, config.last_loot_set_index) == list(
        tr.DEFAULT_LOOT_SETS[2]
    )


@pytest.mark.asyncio
async def test_publish_custom_pool_advances_cursor_by_one(
    monkeypatch, fake_publish_env, stub_weapon_items
) -> None:
    monkeypatch.setattr(tr, "_bot", _FakeBot())
    await tr.save_config(tr.TrialsConfig())  # cursor -1
    await _seed_default_loot_rotation()
    await tr.save_meta(tr.DraftMeta())
    # A published pool that matches no known set advances the loop by one (-1 -> 0), so
    # the rotation keeps progressing rather than freezing on a custom week.
    await tr._handle_create(
        _req(
            body={
                "reset_ts": SAMPLE_RESET,
                "focus_pool": ["The Scholar", "Some Custom Weapon"],
                "publish": True,
            }
        )
    )
    assert (await tr.load_config()).last_loot_set_index == 0


@pytest.mark.asyncio
async def test_handle_create_503_when_bot_unset(monkeypatch) -> None:
    monkeypatch.setattr(tr, "_bot", None)
    resp = await tr._handle_create(_req(body={"reset_ts": 1}))
    assert resp.status == 503


@pytest.mark.asyncio
async def test_handle_delete_removes_and_clears_reset_ts(monkeypatch) -> None:
    bot = _FakeBot()
    monkeypatch.setattr(tr, "_bot", bot)
    await tr.save_meta(
        tr.DraftMeta(
            message_id=77, reset_ts=SAMPLE_RESET, status="published", crossposted=True
        )
    )
    resp = await tr._handle_delete(_req())
    assert resp.status == 200 and json.loads(resp.text or "") == {"ok": True}
    assert bot.rest.deleted == [(tr.settings.get_followable_channel_sync("trials"), 77)]
    meta = await tr.load_meta()
    assert meta.message_id == 0 and meta.reset_ts == 0
    assert meta.crossposted is False and meta.status == "draft"


# ---------------------------------------------------------------------------
# autopost toggle + reset-weekend cron removed
# ---------------------------------------------------------------------------


def test_no_autopost_route_handler_or_cron() -> None:
    # Trials is driven entirely by the web form's Create/Publish buttons: the toggle
    # route/handler, the reset-weekend cron, and the spec's autopost hooks are all gone.
    app = aiohttp.web.Application()
    tr.register_trials_routes(app)
    paths = {r.resource.canonical for r in app.router.routes() if r.resource}
    assert "/trials/create" in paths  # the real buttons still route
    assert "/trials/auto" not in paths  # the toggle route is gone
    assert not hasattr(tr, "_handle_auto")
    assert not hasattr(tr, "run_trials_draft")
    assert not hasattr(tr._SPEC, "get_autopost")
    assert not hasattr(tr._SPEC, "set_autopost")


# ---------------------------------------------------------------------------
# preview renderer (H3 headings + bullets, tag whitelist)
# ---------------------------------------------------------------------------


def test_build_body_emits_the_markdown_the_renderer_needs() -> None:
    """The producer's half of the preview: the constructs, in the body text.

    How they *draw* is the shared renderer's business, pinned by the corpus in
    ``dd/anchor/preview_fixtures``. What has to hold here is that build_body writes
    them at all.
    """
    ctx = tr.TrialsContext(
        reset_ts=SAMPLE_RESET,
        featured_maps=["Burnout"],
        focus_pool=[tr.WeaponRef("The Scholar", 123)],
    )
    body = tr.build_body(ctx)
    assert "### " in body  # sub-headings
    assert "\n- " in body  # bullets
    assert "*of*" in body  # the italicised title
    assert "[The Scholar](https://light.gg/db/items/123)" in body  # deep link
