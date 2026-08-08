"""Pure-lazy per-bot application-emoji store for Destiny item icons.

Discord application emojis only render *inline* in messages posted by the app that owns
them (a foreign app's ``<:name:id>`` degrades to ``:name:`` text), so each bot keeps its
own store, capped at 2000 emojis. Item icons are uploaded on first use, cached
``name -> id`` in :class:`dd.common.schemas.AppEmojiCache`, and reused thereafter. When
the store nears capacity the least-recently-used entries are evicted — which is safe
because Discord keeps serving a *deleted* emoji's CDN image, so already-posted messages
keep rendering.

The store is deliberately ignorant of :class:`DestinyItem`: it speaks only
``(name, icon_url)``, so it can live in ``dd.common`` and be owned by either bot. The
anchor-side :func:`dd.anchor.extensions.bungie_api.emoji.item_emoji` bridge adapts a
``DestinyItem`` into those two strings.
"""

from __future__ import annotations

import asyncio
import logging
import re
import typing as t
import unicodedata
from functools import partial

import hikari as h

from dd.common import schemas
from dd.common.utils import re_user_side_emoji
from dd.hmessage.message import HMessage

logger = logging.getLogger(__name__)

_INVALID = re.compile(r"[^a-z0-9]+")


def emoji_name(display_name: str) -> str:
    """Normalise a Destiny item display name into a valid Discord emoji name.

    Discord requires ``^[A-Za-z0-9_]{2,32}$``, unique per store. This is deterministic
    and *hashless*: a collision gate over every Legendary+Exotic weapon/armour name in
    the frozen manifest (3,796 names) produced zero clashes, so no disambiguating suffix
    is needed. Accents are transliterated (café -> cafe), runs of disallowed characters
    collapse to a single underscore, and the result is trimmed to 2–32 chars.
    """
    ascii_ = (
        unicodedata.normalize("NFKD", display_name)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    slug = _INVALID.sub("_", ascii_).strip("_")[:32].rstrip("_")
    if len(slug) < 2:
        slug = f"{slug}_x"[:2] if slug else "item"
    return slug


class AppEmojiStore:
    """Lazy LRU cache of application emojis backing one bot's Destiny item icons."""

    def __init__(self, bot: h.GatewayBot, capacity: int = 1900) -> None:
        # capacity < 2000 leaves headroom for concurrent uploads and any hand-made
        # application emojis the store must never evict.
        self._bot = bot
        self._capacity = capacity
        self._app_id: int | None = None
        self._emoji: dict[str, h.KnownCustomEmoji] = {}
        self._icon_url: dict[str, str] = {}
        self._lock = asyncio.Lock()
        # LRU bumps are batched: hits accumulate here and a single background task
        # flushes them in one UPDATE (evictions are rare, so exact ordering is fine).
        self._pending_touch: set[str] = set()
        self._touch_task: asyncio.Task[None] | None = None
        _ = bot.listen(h.StartedEvent)(self._on_start)

    @property
    def app_id(self) -> int | None:
        """This store's application id, or ``None`` until warmed (see :meth:`start`)."""
        return self._app_id

    # -- lifecycle -----------------------------------------------------------------

    async def _on_start(self, _event: h.StartedEvent) -> None:
        try:
            await self.start()
        except Exception:
            logger.exception("AppEmojiStore failed to warm up")

    async def start(self) -> None:
        """Resolve the application id and reconcile the live store against the DB.

        The live application-emoji list is the source of truth: DB rows whose emoji no
        longer exists are dropped, and live emojis missing from the DB are adopted (so
        prior-run uploads and hand-made app emojis are reused, never duplicated).
        """
        app = await self._bot.rest.fetch_application()
        self._app_id = app.id
        live = {
            e.name: e for e in await self._bot.rest.fetch_application_emojis(app.id)
        }
        self._emoji = dict(live)
        self._icon_url = {}

        tracked = {r.name: r for r in await schemas.AppEmojiCache.all_for_app(app.id)}
        for name, row in tracked.items():
            if name not in live:
                await schemas.AppEmojiCache.remove(app.id, name)
            else:
                self._icon_url[name] = row.icon_url or ""
        for name, emoji in live.items():
            if name not in tracked:
                # Adopt an untracked live emoji; icon_url unknown, so store "" which
                # get() treats as "never recreate on icon mismatch".
                await schemas.AppEmojiCache.upsert(app.id, name, int(emoji.id), "")
                self._icon_url[name] = ""

    # -- public API ----------------------------------------------------------------

    async def get(self, name: str, icon_url: str) -> h.KnownCustomEmoji | None:
        """Return the cached/uploaded emoji for ``name``; ``None`` on failure.

        Cache hits are lock-free. A miss uploads ``icon_url`` under a lock (evicting the
        LRU entry first if at capacity). A cached name whose stored ``icon_url`` differs
        from the one requested is recreated (name reused for a different icon — not
        expected on a frozen manifest, but logged and self-healed rather than serving
        the wrong image).
        """
        if self._app_id is None:
            await self.start()

        cached = self._emoji.get(name)
        if cached is not None:
            stored = self._icon_url.get(name, "")
            if stored and icon_url and stored != icon_url:
                logger.warning(
                    "AppEmojiStore: icon changed for %r; recreating emoji", name
                )
                async with self._lock:
                    await self._delete(name)
                    return await self._safe_create(name, icon_url)
            self._touch(name)
            return cached

        async with self._lock:
            cached = self._emoji.get(name)  # re-check under lock
            if cached is not None:
                self._touch(name)
                return cached
            return await self._safe_create(name, icon_url)

    async def get_many(
        self, wants: list[tuple[str, str]]
    ) -> dict[str, h.KnownCustomEmoji]:
        """Resolve several ``(name, icon_url)`` pairs; skip any that fail to upload."""
        out: dict[str, h.KnownCustomEmoji] = {}
        for name, icon_url in wants:
            emoji = await self.get(name, icon_url)
            if emoji is not None:
                out[name] = emoji
        return out

    # -- internals -----------------------------------------------------------------

    async def _safe_create(self, name: str, icon_url: str) -> h.KnownCustomEmoji | None:
        try:
            return await self._create(name, icon_url)
        except Exception:
            logger.exception("AppEmojiStore: failed to upload emoji %r", name)
            return None

    async def _create(self, name: str, icon_url: str) -> h.KnownCustomEmoji:
        assert self._app_id is not None
        await self._evict_if_needed()
        emoji = await self._bot.rest.create_application_emoji(
            self._app_id, name, h.URL(icon_url)
        )
        self._emoji[name] = emoji
        self._icon_url[name] = icon_url
        await schemas.AppEmojiCache.upsert(self._app_id, name, int(emoji.id), icon_url)
        return emoji

    async def _delete(self, name: str) -> None:
        assert self._app_id is not None
        emoji = self._emoji.pop(name, None)
        self._icon_url.pop(name, None)
        if emoji is not None:
            try:
                await self._bot.rest.delete_application_emoji(self._app_id, emoji.id)
            except Exception:
                logger.exception("AppEmojiStore: failed to delete emoji %r", name)
        await schemas.AppEmojiCache.remove(self._app_id, name)

    async def _evict_if_needed(self) -> None:
        assert self._app_id is not None
        while len(self._emoji) >= self._capacity:
            victims = await schemas.AppEmojiCache.oldest(self._app_id, 1)
            if not victims:
                return  # nothing evictable we own; refuse to touch foreign emojis
            await self._delete(victims[0])

    def _touch(self, name: str) -> None:
        """Queue an LRU bump; one background task flushes the accumulated batch."""
        if self._app_id is None:
            return
        self._pending_touch.add(name)
        if self._touch_task is None or self._touch_task.done():
            self._touch_task = asyncio.create_task(self._flush_touches())

    async def _flush_touches(self) -> None:
        # Yield once so a same-tick burst of hits (a fan-out resolving many item emoji)
        # coalesces into a single UPDATE rather than one per name.
        await asyncio.sleep(0)
        if self._app_id is None or not self._pending_touch:
            return
        names = sorted(self._pending_touch)
        self._pending_touch.clear()
        try:
            await schemas.AppEmojiCache.touch(self._app_id, names)
        except Exception:
            logger.exception("AppEmojiStore: failed to bump last_used for %r", names)


async def rewrite_item_emoji(store: AppEmojiStore, text: str) -> str:
    """Rewrite another bot's Destiny item-emoji mentions in ``text`` to ``store``'s own.

    Only *Destiny item* emoji are touched — a rendered mention ``<a?:name:id>`` is one
    iff its ``id`` has a row in :class:`~dd.common.schemas.AppEmojiCache` (that table
    holds item icons exclusively). Guild/type emoji, unicode, bare ``:name:`` tokens,
    and emoji already owned by this store are left verbatim. Each distinct foreign id is
    resolved once (uploading to ``store`` on a miss), then substituted in one pass.
    """
    if not text:
        return text

    # group(4) == "<digits>>" and is present only for a *rendered* mention.
    ids: set[int] = {
        int(m.group(4)[:-1]) for m in re_user_side_emoji.finditer(text) if m.group(4)
    }
    if not ids:
        return text

    own = store.app_id
    repl: dict[int, str] = {}
    for emoji_id in ids:
        row = await schemas.AppEmojiCache.get_by_emoji_id(emoji_id)
        if row is None or (own is not None and row.app_id == own):
            continue  # not an item emoji, or already ours → leave verbatim
        emoji = await store.get(row.name, row.icon_url)
        if emoji is not None:
            repl[emoji_id] = str(emoji)

    if not repl:
        return text

    def _sub(m: t.Any) -> str:
        g4 = m.group(4)
        if g4 and (new := repl.get(int(g4[:-1]))):
            return str(new)
        return str(m.group(0))

    return re_user_side_emoji.sub(_sub, text)


async def rewrite_item_emoji_in_message(
    store: AppEmojiStore, hmsg: HMessage
) -> HMessage:
    """Rewrite another bot's item-emoji mentions across every text surface of ``hmsg``.

    A thin adapter over :meth:`HMessage.map_text_async` — the rewrite runs on content,
    embed fields, and CV2 text displays in one pass. See :func:`rewrite_item_emoji`.
    """
    return await hmsg.map_text_async(partial(rewrite_item_emoji, store))
